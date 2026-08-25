# -*- coding: utf-8 -*-
"""
vibration_processor.py
======================
振动信号处理模块 (纯计算, 无 UI 依赖) — 复刻 Delphi 版振动分析链路。

职责:
    把 Delphi 版 (Main.pas BufferedAiCtrl1DataReady) 的振动处理算法独立成
    可单元验证的纯函数/纯类:
        - 每帧处理:  标定 → FFT → 频域清零带通 → IFFT → 峰峰值/间距/单边谱/主峰
          (process_frame)
        - 转速换算:  电压均值 → RPM, 只读 g 标定参数、不写 g (calc_speed_rpm;
          与 data_processor.calc_realspeed 数学一致, 供振动页独立选转速通道)
        - 瀑布缓存:  每隔 steptime 秒存一行幅值谱 + 当时实测转速, 上限与
          Delphi 相同 (110 槽, 索引 ≤100); 导出分号分隔 CSV (WaterfallCache)

    频域清零支持两种口径 (需求已确认: 可切换, 默认 Delphi 口径):
        - MASK_DELPHI : 逐字复刻 Delphi 判据 i*D ≤ (下限-1) 或 i*D ≥ (上限-1),
          其中 i 取全部 N 点 (负频率镜像谱线随 i*D 增大同样被清零), 因此带内
          振动经 IFFT 取实部后幅值约为物理真实值的一半 — 与 Delphi 程序及
          2022 年以来的历史 CSV 读数直接可比。
        - MASK_CORRECT: 数学修正口径, 正负频率对称的规范带通
          (保留 下限 ≤ |f| ≤ 上限), 幅值为物理真实值 (约为 Delphi 口径的 2 倍)。
    两口径只改频域掩码, 链路其余部分完全共用。

    已确认不复刻的 Delphi 缺陷: input 数组虚部残留污染 (本实现每帧从实数序列
    重建频谱)、找主峰后 HzMax:=0 的死代码。

依赖:
    - numpy
    - public_para.g (calc_speed_rpm 只读转速标定 4 参数)

被调用方:
    - vibration_tab.VibrationTab.on_frame → process_frame / calc_speed_rpm
    - vibration_tab.VibrationTab          → WaterfallCache (缓存/悬停回放/导出)
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Optional

from public_para import g


# =============================================================================
# 常量
# =============================================================================
# 频域清零口径 (需求确认: 双口径可切换, 默认 Delphi)
MASK_DELPHI = 'delphi'     # 复刻 Delphi 判据 (含 -1 偏移与镜像清零 → 振幅≈½)
MASK_CORRECT = 'correct'   # 修正口径: 正负频率对称带通 (振幅为物理真值)

# 传感器灵敏度选项: (显示文本, 灵敏度 mV/μm) — 顺序与 Delphi ComboBox9 一致
SENSITIVITY_OPTIONS = [
    ('8mv/um', 8.0),
    ('1mv/mv', 1.0),
    ('4.78mv/um', 4.78),
    ('0.5mv/um', 0.5),
    ('1mv/um', 1.0),
    ('1mv/1.8um', 1.0 / 1.8),   # Delphi 默认档 (电涡流 1mV/1.8μm)
]
DEFAULT_SENSITIVITY_INDEX = 5   # 默认 '1mv/1.8um' → TransPara = 1800 μm/V


def transpara_from_sensitivity(sens_mv_per_um: float) -> float:
    """由灵敏度 (mV/μm) 求电压→微米转换系数 TransPara = 1000/灵敏度。

    Args:
        sens_mv_per_um: 传感器灵敏度 (mV/μm), 应为正数

    Returns:
        转换系数 (μm/V); 灵敏度非正时回退 1800.0 (即默认 1mV/1.8μm 档)

    Side Effects:
        无 (纯函数)
    """
    if sens_mv_per_um <= 0:
        return 1800.0
    return 1000.0 / sens_mv_per_um


def calc_speed_rpm(speed_volts: np.ndarray) -> float:
    """由转速通道电压均值反推转速 RPM (只读 g 标定, 不写 g.Realspeed)。

    Args:
        speed_volts: 转速通道原始电压序列 (V)

    Returns:
        转速 (RPM): (均值-VoltageIni)*SpeedMax/(VoltageMax-VoltageIni),
        负值截断为 0 后加 SpeedfixNum (顺序与 Delphi / calc_realspeed 一致)

    Side Effects:
        无 (与 data_processor.calc_realspeed 数学一致, 但不修改全局状态,
        供振动页按自己的转速通道独立解算, 避免覆盖径向页写入的 g.Realspeed)
    """
    if len(speed_volts) == 0:
        return 0.0
    mean_v = float(np.mean(speed_volts))
    denom = g.VoltageMax - g.VoltageIni
    if abs(denom) < 1e-9:
        rpm = 0.0
    else:
        rpm = (mean_v - g.VoltageIni) * g.SpeedMax / denom
    if rpm < 0:
        rpm = 0.0
    return rpm + g.SpeedfixNum


# =============================================================================
# 每帧处理结果
# =============================================================================
@dataclass
class VibFrameResult:
    """一帧振动处理的全部输出 (供 UI 显示与落盘)。

    字段:
        ok:        本帧是否有效 (采样点不足 N 时为 False, 其余字段无意义)
        vib1:      滤波后时域峰峰值 (μm) — Delphi 的"振幅"
        dis2:      当前间距 (μm) = -mean(滤波后波形) (负号沿用 Delphi)
        xs:        单边谱显示频率轴 (Hz), 已截到 0 < f ≤ 带通上限
        mags:      对应幅值 (μm), = |X[k]|*2/N (清零后的谱, 与 Delphi 一致)
        phases:    对应相位 (度)
        hz_max:    主峰频率 (Hz);  hz_mag: 主峰幅值 (μm);  phase_deg: 主峰相位 (度)
        realspeed: 本帧按振动页转速通道解算的转速 (RPM)
        mags_half: 完整单边幅值谱 (bin 0..(N-1)//2, 含带外零值), 供瀑布缓存
        freq_step: 频率分辨率 D = fs/N (Hz)
    """
    ok: bool
    vib1: float = 0.0
    dis2: float = 0.0
    xs: np.ndarray = field(default_factory=lambda: np.zeros(0))
    mags: np.ndarray = field(default_factory=lambda: np.zeros(0))
    phases: np.ndarray = field(default_factory=lambda: np.zeros(0))
    hz_max: float = 0.0
    hz_mag: float = 0.0
    phase_deg: float = 0.0
    realspeed: float = 0.0
    mags_half: np.ndarray = field(default_factory=lambda: np.zeros(0))
    freq_step: float = 0.0


def process_frame(vib_volts: np.ndarray, speed_volts: np.ndarray,
                  fs: float, n_points: int,
                  stop_low: float, stop_high: float,
                  transpara: float, mask_mode: str = MASK_DELPHI) -> VibFrameResult:
    """处理一帧振动数据: 标定 → FFT → 频域清零 → IFFT → 统计与单边谱。

    Args:
        vib_volts:   振动通道原始电压序列 (V), 长度 ≥ n_points 才有效
        speed_volts: 转速通道原始电压序列 (V), 仅用于本页转速显示/落盘
        fs:          采样频率 (Hz)
        n_points:    分析点数 N (= daq.interval_count)
        stop_low:    带通下限 (Hz);  stop_high: 带通上限 (Hz)
        transpara:   电压→微米转换系数 (μm/V)
        mask_mode:   MASK_DELPHI (默认, 历史兼容) 或 MASK_CORRECT (物理真值)

    Returns:
        VibFrameResult; 采样不足 N 或 N/fs 非法时 ok=False

    Side Effects:
        无 (纯计算; numpy fft 等价于 Delphi FFTs.pas 混合基 FFT)

    算法对照 (Delphi Main.pas BufferedAiCtrl1DataReady):
        - 标定:   dataScaledArray := 原始电压 * TransPara1
        - FFT:    ForwardFFT → numpy.fft.fft
        - 清零:   见 MASK_DELPHI / MASK_CORRECT 说明 (模块 docstring)
        - IFFT:   InverseFFT 取实部 → numpy.fft.ifft(...).real
        - vib1:   max-min (峰峰值);  dis2: -mean
        - 单边谱: |X[k]|*2/N, k=1..(N-1)//2, 显示截到 f ≤ stop_high
        - 主峰:   显示范围内幅值最大谱线的 (频率, 幅值, 相位)
    """
    N = int(n_points)
    if N <= 0 or fs <= 0 or len(vib_volts) < N:
        return VibFrameResult(ok=False)

    D = fs / N                                    # 频率分辨率 (Delphi 的 D)
    vib_um = np.asarray(vib_volts[:N], dtype=np.float64) * transpara

    # ---- FFT (每帧从实数序列重建, 不带上一帧残留 — 不复刻 Delphi 虚部污染) ----
    spec = np.fft.fft(vib_um)

    # ---- 频域清零 (双口径) ----
    if mask_mode == MASK_CORRECT:
        # 修正口径: 正负频率对称带通, 保留 stop_low ≤ |f| ≤ stop_high
        f_abs = np.abs(np.fft.fftfreq(N, d=1.0 / fs))
        mask = (f_abs < stop_low) | (f_abs > stop_high)
    else:
        # Delphi 口径: i*D ≤ (下限-1) 或 i*D ≥ (上限-1), i 取全部 N 点
        freqs_idx = np.arange(N) * D
        mask = (freqs_idx <= (stop_low - 1)) | (freqs_idx >= (stop_high - 1))
    spec[mask] = 0.0

    # ---- IFFT 取实部 = 滤波后时域波形 ----
    recovered = np.fft.ifft(spec).real

    vib1 = float(np.max(recovered) - np.min(recovered))
    dis2 = -float(np.mean(recovered))

    # ---- 单边谱 (用清零后的谱, 与 Delphi 展示一致) ----
    half = (N - 1) // 2
    mags_half = np.abs(spec[:half + 1]) * 2.0 / N          # bin 0..half (含 DC)
    ks = np.arange(1, half + 1)
    fs_x = ks * D
    show = fs_x <= stop_high
    xs = fs_x[show]
    mags = mags_half[1:half + 1][show]
    phases = np.angle(spec[1:half + 1][show]) * 180.0 / np.pi

    # ---- 主峰 ----
    if len(mags) > 0:
        pk = int(np.argmax(mags))
        hz_max, hz_mag, phase_deg = float(xs[pk]), float(mags[pk]), float(phases[pk])
    else:
        hz_max = hz_mag = phase_deg = 0.0

    return VibFrameResult(
        ok=True, vib1=vib1, dis2=dis2,
        xs=xs, mags=mags, phases=phases,
        hz_max=hz_max, hz_mag=hz_mag, phase_deg=phase_deg,
        realspeed=calc_speed_rpm(np.asarray(speed_volts, dtype=np.float64)),
        mags_half=mags_half, freq_step=D,
    )


# =============================================================================
# 瀑布谱缓存
# =============================================================================
def _fmt2(v: float) -> str:
    """浮点格式化为最多 2 位小数并去尾零 (等价 Delphi floattostr(roundto(x,-2)))。

    Args:
        v: 待格式化数值

    Returns:
        字符串; 空/'-' 回退 '0'

    Side Effects:
        无
    """
    s = f'{v:.2f}'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s if s and s != '-' else '0'


class WaterfallCache:
    """瀑布谱缓存: 每隔 steptime 秒存一行单边幅值谱 + 该时刻实测转速。

    容量与 Delphi 完全一致: 110 个时间槽, 只写入槽号 ≤100 的行
    (dataSampleFreq: array[0..109]; if (Round(time) div steptime)<=100)。
    同一槽在其时间窗内被后续帧反复覆盖 (Delphi 行为), 槽内保留的是该窗口
    最后一帧的谱。

    与 Delphi 的差异 (需求确认): 导出列标题不再用名义转速公式
    (步长档+1)×100×列号, 而是记录每槽写入时的实测转速。

    维护的状态:
        - _data:     (110, n_bins) 幅值谱矩阵
        - _rpm:      各槽写入时的实测转速 (RPM)
        - recordnum: 当前最大已写槽号 (Delphi recordnum, 封顶 100)
        - freq_step: 频率分辨率 D (导出/回放的频率轴)
        - steptime:  记录步长 (s)
    """

    SLOTS = 110     # Delphi dataSampleFreq 第一维
    CAP = 100       # Delphi 只写 (time div steptime) ≤ 100 的槽

    def __init__(self):
        """构造空缓存 (未 reset 前 has_data 为 False, 所有写入被忽略)。"""
        self._data: Optional[np.ndarray] = None
        self._rpm = np.zeros(self.SLOTS)
        self.recordnum = 0
        self.freq_step = 0.0
        self.steptime = 2
        self._touched = False

    def reset(self, n_bins: int, freq_step: float, steptime: int):
        """按本次会话的谱长度/分辨率/步长重新分配缓存 (开始采集时调用)。

        Args:
            n_bins:    单边谱 bin 数 (= (N-1)//2 + 1, 含 DC)
            freq_step: 频率分辨率 D = fs/N (Hz)
            steptime:  记录步长 (s), ≥1

        Returns:
            无

        Side Effects:
            - 重新分配 _data 为零矩阵; 清零 _rpm/recordnum/_touched
        """
        self._data = np.zeros((self.SLOTS, max(1, int(n_bins))))
        self._rpm = np.zeros(self.SLOTS)
        self.recordnum = 0
        self.freq_step = float(freq_step)
        self.steptime = max(1, int(steptime))
        self._touched = False

    @property
    def has_data(self) -> bool:
        """是否已有至少一行谱写入 (悬停回放/导出的使能条件)。"""
        return self._touched and self._data is not None

    def store(self, elapsed_s: float, mags_half: np.ndarray, rpm: float):
        """把一帧单边谱写入其时间槽 (槽号 = round(elapsed) // steptime)。

        Args:
            elapsed_s: 距采集开始的秒数
            mags_half: 完整单边幅值谱 (长度须等于 reset 时的 n_bins)
            rpm:       该时刻实测转速 (RPM)

        Returns:
            无

        Side Effects:
            - 槽号 ∈ [0, CAP] 时写入 _data/_rpm 并推进 recordnum (Delphi 语义:
              recordnum = 当前槽号, 同窗内反复覆盖); 槽号超界静默忽略
        """
        if self._data is None:
            return
        idx = int(round(elapsed_s)) // self.steptime
        if idx < 0 or idx > self.CAP:
            return
        n = min(len(mags_half), self._data.shape[1])
        self._data[idx, :n] = mags_half[:n]
        self._rpm[idx] = float(rpm)
        self.recordnum = idx
        self._touched = True

    def slot_for_time(self, t_seconds: float):
        """把趋势图横坐标时间换算成槽号 (悬停回放用)。

        Args:
            t_seconds: 趋势图上的时间坐标 (s)

        Returns:
            合法槽号 (0..min(recordnum, CAP)) 或 None (无数据/越界)

        Side Effects:
            无
        """
        if not self.has_data or t_seconds < 0:
            return None
        idx = int(round(t_seconds)) // self.steptime
        if 0 <= idx <= min(self.recordnum, self.CAP):
            return idx
        return None

    def row(self, idx: int):
        """取某槽的 (实测转速, 单边幅值谱)。

        Args:
            idx: 槽号

        Returns:
            (rpm, mags) 元组; 槽号非法或无数据时返回 (None, None)

        Side Effects:
            无
        """
        if not self.has_data or idx < 0 or idx >= self.SLOTS:
            return (None, None)
        return (float(self._rpm[idx]), self._data[idx])

    def export_csv(self, path: str, stop_high: float) -> int:
        """把瀑布谱导出为分号分隔 CSV (行=频率, 列=时间槽, 列头=实测转速)。

        Args:
            path:      输出文件绝对路径
            stop_high: 带通上限 (Hz); 行写到首个超过上限的频率后停止
                       (Delphi 行为: 写完该行再 Break)

        Returns:
            实际写出的时间槽 (列) 数

        Side Effects:
            - 写盘 (UTF-8 with BOM, CRLF, 分号分隔)

        格式对照 Delphi bsSkinButton1Click:
            首行  '频率Hz\\转速rpm;' + 各槽转速;  数据槽取 1..recordnum
            (Delphi 从 j=1 起, 槽 0 不导出);  数据行 '频率;各槽幅值;',
            数值均按最多 2 位小数去尾零;  行尾补一个空格再换行 (Writeln(' '))
        """
        if not self.has_data or self._data is None:
            return 0
        n_cols = min(self.recordnum, self.CAP)
        if n_cols < 1:
            return 0
        n_bins = self._data.shape[1]
        lines = []
        head = '频率Hz\\转速rpm;' + ';'.join(
            _fmt2(self._rpm[j]) for j in range(1, n_cols + 1)) + '; '
        lines.append(head)
        # Delphi: for i:=1 to trunc((N-1)/2)-1 → bin 1..(half-1)
        for i in range(1, max(2, n_bins - 1)):
            freq = i * self.freq_step
            row = _fmt2(freq) + ';' + ';'.join(
                _fmt2(float(self._data[j, i])) for j in range(1, n_cols + 1)) + '; '
            lines.append(row)
            if freq > stop_high:               # 写完该行再退出 (Delphi 语义)
                break
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            f.write('\r\n'.join(lines) + '\r\n')
        return n_cols
