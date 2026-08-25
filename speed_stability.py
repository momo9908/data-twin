"""
speed_stability.py
==================
转速保载状态实时辨识 (纯逻辑, 无 UI / 文件 / numpy 依赖)。

职责:
    把"轮盘转速是否进入稳定保载状态"的判定, 从 main_app 的数据回调里抽出来,
    独立实现, 便于离线单元验证 (与 data_processor 同样是可独立测试的纯逻辑层)。

    复现需求文档所述的"滑动窗口 + 滞回"状态机:
        - 平稳性条件: 最近 M 个读数构成滑动窗口 W, 相对极差 Δn/均值 ≤ δ_rel
        - 持续性条件: 上述平稳性须在连续 M 个读数 (整个窗口) 内成立
          (窗口填满且极差/均值 ≤ δ_rel ⇔ 连续 M 个读数同落入一个容差带,
           故两条件由一次判断同时覆盖)
        - 滞回机制: 进入保载用较紧容差带 δ_rel, 退出用较宽滞回带 δ_exit,
          抑制阈值附近的状态抖动
        - 低速保护: 转速均值低于 min_speed 不判保载 (兼作除零兜底)

    每个转速读数 n_k 调用一次 update(n_k), 返回本次的状态跳变事件, 由调用方
    据此做边沿触发 (例如 ENTER_HOLD 时弹出"可进行 DIC 采样")。

依赖:
    - collections.deque / enum (标准库)

被调用方:
    - main_app.MainForm._on_data_ready   → update(g.Realspeed)
    - main_app.MainForm._btn_start_click → reset() (每次新采集复位)
"""

from collections import deque
from enum import Enum


class HoldState(Enum):
    """保载状态机的两个状态。"""
    SPEED_ADJUST = 0   # 调速态: 升速 / 调速中, 转速尚未稳定
    HOLD = 1           # 保载态: 转速已稳定并驻留


class HoldEvent(Enum):
    """update() 每次返回的状态跳变事件 (供调用方做边沿触发)。"""
    NONE = 0          # 无跳变, 维持当前状态
    ENTER_HOLD = 1    # 调速态 → 保载态: 此刻应提示"可进行 DIC 采样"
    EXIT_HOLD = 2     # 保载态 → 调速态: 保载结束, 转回调速


class LoadHoldDetector:
    """转速保载状态实时辨识器 (滑动窗口 + 滞回状态机)。

    维护的状态:
        - _buf:           最近 window 个转速读数的环形缓冲 (deque, maxlen=window)
        - _state:         当前状态 (HoldState)
        - _mean_platform: 进入保载时冻结的平台均值 (RPM), 退出判据的参考基准

    典型用法:
        det = LoadHoldDetector(min_speed=10000.0)
        det.reset()                       # 每次新采集前复位
        for n_k in speed_readings:        # 每个就绪窗口一个读数
            if det.update(n_k) is HoldEvent.ENTER_HOLD:
                notify_dic_ready()        # 边沿触发: 仅在跳入保载那一刻提示一次

    行为不变量:
        - 窗口未填满 (< window) 时绝不判定进入保载 (保证持续性条件)
        - 平台均值在进入保载时一次性冻结, 保载期间不更新 (退出判据始终对齐同一基准)
        - δ_rel < δ_exit (容差带紧于滞回带), 否则失去抗抖动意义
    """

    def __init__(self, window: int = 5, delta_rel: float = 0.003,
                 delta_exit: float = 0.005, min_speed: float = 10000.0):
        """构造保载辨识器并设定判据参数。

        Args:
            window:     滑动窗口读数个数 M (默认 5; 10kHz/8192 下约对应 4.1s 驻留)
            delta_rel:  进入容差带 δ_rel (相对极差阈值, 默认 0.003 即 0.3%)
            delta_exit: 退出滞回带 δ_exit (相对偏离阈值, 默认 0.005 即 0.5%)
            min_speed:  最低保载转速 (RPM): 窗口均值低于此值不判保载, 默认 10000

        Returns:
            无

        Side Effects:
            - 初始化内部缓冲与状态 (等价于构造后立即处于 reset() 后的状态)
        """
        self.window = int(window)
        self.delta_rel = float(delta_rel)
        self.delta_exit = float(delta_exit)
        self.min_speed = float(min_speed)
        self._buf = deque(maxlen=self.window)
        self._state = HoldState.SPEED_ADJUST
        self._mean_platform = 0.0

    # ---------- 只读属性 ----------
    @property
    def state(self) -> HoldState:
        """当前状态 (HoldState), 只读。"""
        return self._state

    @property
    def platform_mean(self) -> float:
        """进入保载时冻结的平台均值 (RPM); 调速态下为 0.0。"""
        return self._mean_platform

    @property
    def is_holding(self) -> bool:
        """当前是否处于保载态。"""
        return self._state is HoldState.HOLD

    # ---------- 控制 ----------
    def reset(self):
        """复位到调速态, 清空滑动窗口与平台均值。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 清空 _buf, 置 _state=SPEED_ADJUST, _mean_platform=0.0

        说明:
            每次开始新一轮采集前调用, 避免上一轮的历史读数影响新一轮判定。
        """
        self._buf.clear()
        self._state = HoldState.SPEED_ADJUST
        self._mean_platform = 0.0

    # ---------- 主入口 ----------
    def update(self, speed: float) -> HoldEvent:
        """喂入一个转速读数 n_k, 推进状态机一步, 返回本次的跳变事件。

        Args:
            speed: 本就绪窗口的转速读数 n_k (RPM), 通常为 g.Realspeed

        Returns:
            HoldEvent: 本次调用引发的状态跳变
                - ENTER_HOLD: 调速态 → 保载态 (调用方应在此刻提示 DIC 采样)
                - EXIT_HOLD:  保载态 → 调速态
                - NONE:       状态未变化

        Side Effects:
            - 把 speed 追加进 _buf (满则挤出最旧读数)
            - 可能修改 _state 与 _mean_platform

        算法 (与需求文档判据等价):
            调速态: 窗口填满 且 均值 ≥ min_speed 且 极差/均值 ≤ δ_rel
                    → 进入保载, 冻结平台均值 = 窗口均值, 返回 ENTER_HOLD
            保载态: 新读数对平台均值的相对偏离 > δ_exit
                    → 退出保载, 返回 EXIT_HOLD; 否则维持 (NONE)
        """
        self._buf.append(float(speed))
        if self._state is HoldState.SPEED_ADJUST:
            return self._step_speed_adjust()
        return self._step_hold(float(speed))

    # ---------- 内部: 各状态单步 ----------
    def _step_speed_adjust(self) -> HoldEvent:
        """调速态单步: 判定是否满足"平稳性 + 持续性 + 低速保护"而进入保载。

        Args:
            无 (读 self._buf)

        Returns:
            HoldEvent.ENTER_HOLD 或 HoldEvent.NONE

        Side Effects:
            - 满足进入条件时: 置 _state=HOLD, 冻结 _mean_platform=窗口均值

        判据不变量:
            - 窗口未填满 (len < window) 直接返回 NONE (持续性未知)
            - 均值 < min_speed 直接返回 NONE (低速保护, 兼防除零)
            - 极差 Δn / 均值 ≤ δ_rel 才进入保载
        """
        if len(self._buf) < self.window:
            return HoldEvent.NONE
        mean = sum(self._buf) / len(self._buf)
        if mean < self.min_speed:
            return HoldEvent.NONE
        span = max(self._buf) - min(self._buf)
        if span / mean <= self.delta_rel:
            self._state = HoldState.HOLD
            self._mean_platform = mean
            return HoldEvent.ENTER_HOLD
        return HoldEvent.NONE

    def _step_hold(self, speed: float) -> HoldEvent:
        """保载态单步: 判定新读数是否偏离平台均值超过滞回带而退出保载。

        Args:
            speed: 最新转速读数 n_k (RPM)

        Returns:
            HoldEvent.EXIT_HOLD 或 HoldEvent.NONE

        Side Effects:
            - 退出条件成立时: 置 _state=SPEED_ADJUST

        判据不变量:
            - 以进入保载时冻结的 _mean_platform 为基准 (保载期间不更新)
            - 相对偏离 |speed - 平台均值| / 平台均值 > δ_exit 才退出
            - _mean_platform <= 0 视为异常, 直接退出 (除零兜底)
        """
        if self._mean_platform <= 0.0:
            self._state = HoldState.SPEED_ADJUST
            return HoldEvent.EXIT_HOLD
        deviation = abs(speed - self._mean_platform) / self._mean_platform
        if deviation > self.delta_exit:
            self._state = HoldState.SPEED_ADJUST
            return HoldEvent.EXIT_HOLD
        return HoldEvent.NONE
