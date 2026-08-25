"""
daq_device.py
==============
USB-4716 数据采集卡硬件接入层。

封装 DAQNavi SDK 4.0.x 的 BufferedAiCtrl, 提供与原 Delphi
TBufferedAiCtrl 等价的接口:
    - configure (通道/采样率/缓冲)
    - prepare / start / stop
    - on_data_ready / on_overrun / on_cache_overflow 回调
    - get_data 读取缓冲数据

同时提供一个 SimulatedDaqDevice 用于离线测试 — 当卡未连接或
DAQNavi SDK 不可用时,程序仍能跑通完整流程。
"""

import os
import sys
import time
import threading
from typing import Callable, Optional, List
import numpy as np

# 仿真数据源 (Excel 加载与查表); 该模块自带回退, 缺失 openpyxl 时返回 None
from sim_data_source import load_sim_profile, lookup_deformation


# =============================================================================
# 尝试导入 DAQNavi Python API
# =============================================================================
DAQNAVI_AVAILABLE = False
DAQNAVI_ERROR = ''

try:
    # DAQNavi SDK 4.0.x 默认 Python 包路径
    _candidate_paths = [
        r'C:\Advantech\DAQNavi\Examples\Python',
        r'C:\Program Files\Advantech\DAQNavi\Examples\Python',
        r'C:\Program Files (x86)\Advantech\DAQNavi\Examples\Python',
    ]
    for _p in _candidate_paths:
        if os.path.exists(_p) and _p not in sys.path:
            sys.path.insert(0, _p)

    # DAQNavi 提供的官方 Python 包
    from Automation.BDaq import (        # type: ignore
        ErrorCode, AccessMode, ValueRange, SignalDrop,
        AiSignalType,
    )
    from Automation.BDaq.WaveformAiCtrl import WaveformAiCtrl  # type: ignore
    from Automation.BDaq.BDaqApi import AdxEnumToString, BioFailed  # type: ignore

    DAQNAVI_AVAILABLE = True
except ImportError as e:
    DAQNAVI_ERROR = f'DAQNavi Python API not found: {e}'


# =============================================================================
# DAQ 设备抽象基类
# =============================================================================
class DaqDeviceBase:
    """通用设备接口,真硬件和模拟器都实现这个接口。"""

    def __init__(self):
        # 采集参数(对应 BufferedAiCtrl 各属性)
        self.channel_start: int = 0
        self.channel_count: int = 8
        self.sample_rate: float = 10000.0    # ConvertClock.Rate
        self.samples: int = 16384             # ScanChannel.Samples
        self.interval_count: int = 8192       # ScanChannel.IntervalCount

        # 回调
        self._cb_data_ready: Optional[Callable[[int, int], None]] = None
        self._cb_overrun: Optional[Callable[[int, int], None]] = None
        self._cb_overflow: Optional[Callable[[int, int], None]] = None

        # 设备描述/状态
        self.device_description: str = ''
        self.is_initialized: bool = False
        self.is_running: bool = False

    # ---------- 配置接口 ----------
    def select_device(self, device_number: int, access_mode=1, mod_index=0):
        raise NotImplementedError

    def configure_channels(self, signal_type: int = 1, value_range: int = 1):
        raise NotImplementedError

    def configure_scan(self):
        """把当前 channel_start/count/samples/interval_count/rate 应用到硬件"""
        raise NotImplementedError

    # ---------- 控制接口 ----------
    def prepare(self) -> bool:
        raise NotImplementedError

    def start(self) -> bool:
        raise NotImplementedError

    def stop(self) -> bool:
        raise NotImplementedError

    def cleanup(self):
        pass

    # ---------- 数据读取 ----------
    def get_data(self, count: int) -> np.ndarray:
        """从缓冲区取 count 个 double 样本"""
        raise NotImplementedError

    @property
    def buffer_capacity(self) -> int:
        raise NotImplementedError

    # ---------- 回调设置 ----------
    def set_on_data_ready(self, cb: Callable[[int, int], None]):
        self._cb_data_ready = cb

    def set_on_overrun(self, cb: Callable[[int, int], None]):
        self._cb_overrun = cb

    def set_on_cache_overflow(self, cb: Callable[[int, int], None]):
        self._cb_overflow = cb


# =============================================================================
# 真硬件: 基于 DAQNavi SDK 的 USB-4716
# =============================================================================
class DaqDevice(DaqDeviceBase):
    """USB-4716 真实硬件接入 (基于 DAQNavi SDK 4.x, WaveformAiCtrl)。

    若 DAQNAVI_AVAILABLE=False, 构造时会抛 RuntimeError。

    4.x 移植要点 (与早期 3.x BufferedAiCtrl 版的差异):
        - 缓冲式模拟输入控制类 BufferedAiCtrl → WaveformAiCtrl;
        - 扫描/时钟配置 scanChannel/convertClock/streaming → conversion/record;
        - 4.x Python API 不提供 AI 的"数据就绪"异步事件, 故改用后台轮询线程:
          线程内循环 getData 取一段 → 缓存 → 触发 on_data_ready (与
          SimulatedDaqDevice 的产数方式一致, 对上层 get_data 接口透明);
        - getData 返回多元组 (ErrorCode, 实得数, 数据列表, ...) 而非 (ret, data);
        - 资源释放用 release() + dispose()。
    """

    # USB-4716 在 DAQNavi 中的默认设备描述格式
    DEFAULT_DEVICE_TEMPLATE = 'USB-4716,BID#{bid}'

    def __init__(self, device_description: str = None):
        super().__init__()
        if not DAQNAVI_AVAILABLE:
            raise RuntimeError(
                f'DAQNavi SDK is not installed or not importable.\n'
                f'  Reason: {DAQNAVI_ERROR}\n'
                f'  Please install DAQNavi SDK 4.0.x and ensure '
                f'"C:\\Advantech\\DAQNavi\\Examples\\Python" is on PYTHONPATH.'
            )

        self.device_description = device_description or self.DEFAULT_DEVICE_TEMPLATE.format(bid=0)
        self._ai: Optional[WaveformAiCtrl] = None
        # 轮询取数线程状态 (4.x 无 AI 异步事件, 用后台线程模拟"数据就绪")
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop: Optional[threading.Event] = None
        self._buffer: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        # 采集卡断开回调 (仅真卡有; 运行中 getData 失败判定掉线时触发)
        self._cb_disconnect: Optional[Callable[[], None]] = None

    def set_on_disconnect(self, cb):
        """注册"采集卡断开"回调 (仅真卡 DaqDevice 提供; 基类/模拟器不含此方法)。

        Args:
            cb: 无参回调, 采集中检测到掉线时被调用 (在轮询线程内触发)

        Returns:
            无

        Side Effects:
            - 记录回调引用
        """
        self._cb_disconnect = cb

    # ------------------------------------------------------------------
    def select_device(self, device_number: int, access_mode=1, mod_index=0):
        """按设备号打开 USB-4716 (4.x: selectedDevice 接受设备号/描述/DeviceInformation)。

        Args:
            device_number: 设备号 (来自 DeviceSet.txt)
            access_mode / mod_index: 兼容旧签名, 4.x 此处未使用

        Returns:
            无

        Side Effects:
            - 创建 WaveformAiCtrl 并设 selectedDevice (打不开会抛 ValueError)
            - 写 self.device_description / self.is_initialized

        说明:
            4.x Python API 无 addDataReadyHandler 等便捷事件方法, 故不再注册事件;
            数据就绪改由 start() 启动的轮询线程驱动。
        """
        if self._ai is None:
            self._ai = WaveformAiCtrl()

        # selectedDevice 的 setter 接受 int(设备号)/str(描述)/DeviceInformation;
        # 打不开会抛 ValueError, 由上层 _form_create 捕获提示; 设置成功即代表已打开。
        # 注意: 不再调用 self._ai.initialized 复核 —— 该属性会走 state→Utils.xxx,
        # 而此版 DAQNavi Python 包未向 DaqCtrlBase 暴露 Utils, 会触发
        # "name 'Utils' is not defined" 的 NameError (并非卡未连接)。
        self._ai.selectedDevice = int(device_number)

        sd = self._ai.selectedDevice
        self.device_description = (getattr(sd, 'description', None)
                                   or getattr(sd, 'Description', None) or str(sd))
        self.is_initialized = True

    def configure_channels(self, signal_type: int = 1, value_range: int = 1):
        """配置 channel_count 个通道的 signalType / valueRange。

        Args:
            signal_type: 输入信号类型 (沿用旧默认 1; 4.x 亦接受 AiSignalType 枚举)
            value_range: 量程档位 (沿用旧默认 1; 4.x 亦接受 ValueRange 枚举)

        Returns:
            无

        Side Effects:
            - 逐通道写 channels[i].signalType / valueRange

        4.x 要求传枚举(非整数), 故把传入整数转成对应枚举 (与原程序数值含义一致):
            signal_type=1 → AiSignalType.Differential
            value_range=1 → ValueRange.V_Neg10To10 (±10 V)
        """
        sig = AiSignalType(signal_type)     # int → AiSignalType 枚举
        rng = ValueRange(value_range)       # int → ValueRange 枚举
        for i in range(self.channel_count):
            ch = self._ai.channels[i]
            ch.signalType = sig
            ch.valueRange = rng

    def configure_scan(self):
        """把通道范围 / 采样率 / 段长应用到硬件 (4.x: conversion + record)。

        Args:
            无

        Returns:
            无

        Side Effects:
            - conversion.channelStart / channelCount / clockRate
            - record.sectionCount=0 (流式) / sectionLength=interval_count

        映射 (旧 → 新):
            scanChannel.channelStart/Count → conversion.channelStart/channelCount
            convertClock.rate              → conversion.clockRate
            streaming=True                 → record.sectionCount=0
            scanChannel.intervalCount      → record.sectionLength
        """
        conv = self._ai.conversion
        conv.channelStart = self.channel_start
        conv.channelCount = self.channel_count
        conv.clockRate = self.sample_rate

        rec = self._ai.record
        rec.sectionCount = 0                      # 0 = 流式 (streaming)
        rec.sectionLength = self.interval_count

    def prepare(self) -> bool:
        """准备采集 (WaveformAiCtrl.prepare)。失败抛 RuntimeError。"""
        ret = self._ai.prepare()
        if BioFailed(ret):
            self._raise_error('Prepare', ret)
            return False
        return True

    def start(self) -> bool:
        """启动采集并开启后台轮询取数线程。

        Returns:
            True 成功

        Side Effects:
            - WaveformAiCtrl.start(); 启动 _poll_loop 守护线程; 置 is_running
        """
        ret = self._ai.start()
        if BioFailed(ret):
            self._raise_error('Start', ret)
            return False
        self.is_running = True
        self._poll_stop = threading.Event()
        with self._lock:
            self._buffer = None
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        return True

    def stop(self) -> bool:
        """停止采集: 结束轮询线程 + WaveformAiCtrl.stop。

        Returns:
            True 成功

        Side Effects:
            - 置停止标志; 调 stop() 让阻塞中的 getData 尽快返回; join 线程
        """
        self.is_running = False
        if self._poll_stop is not None:
            self._poll_stop.set()
        ret = None
        if self._ai is not None:
            ret = self._ai.stop()          # 让阻塞中的 getData 提前返回
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=3.0)
            self._poll_thread = None
        if ret is not None and BioFailed(ret):
            self._raise_error('Stop', ret)
            return False
        return True

    def cleanup(self):
        """释放设备资源: 停止采集 + release() + dispose()。"""
        try:
            self.stop()
        except Exception:
            pass
        if self._ai is not None:
            for fn in ('release', 'dispose'):
                try:
                    getattr(self._ai, fn)()
                except Exception:
                    pass
            self._ai = None

    @property
    def buffer_capacity(self) -> int:
        """缓冲容量 (样本数)。4.x 无 bufferCapacity 属性, 按 通道数×段长 估算。"""
        return self.channel_count * self.interval_count

    # ------------------------------------------------------------------
    def get_data(self, count: int) -> np.ndarray:
        """返回轮询线程最近缓存的一段数据 (已按量程标定为电压 V)。

        Args:
            count: 期望样本数 (取缓存前 count 个; 与回调传出的实得数一致)

        Returns:
            numpy float64 数组; 暂无数据时返回空数组

        Side Effects:
            无 (加锁只读拷贝缓存)
        """
        with self._lock:
            if self._buffer is None:
                return np.zeros(0)
            return self._buffer[:count].copy()

    # ------------------------------------------------------------------
    def _poll_loop(self):
        """后台轮询线程: 循环 getData 取一段交错数据 → 缓存 → 触发 on_data_ready。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 周期性写 self._buffer 并调用 self._cb_data_ready(0, 实得数)
            - 采集中 getData 抛异常或返回错误码 (且非正常停止) → 判定采集卡断开:
              调 _notify_disconnect 并结束轮询
            - 收到停止标志 (_poll_stop) 时正常退出, 不误报掉线

        说明 (掉线判据, 仅方式 A, ~1s):
            - 取数量 = 通道数 × 段长 (一段就绪窗口); getData 超时 1s。
            - getData 抛异常或 result[0] 为 BioFailed 错误码即判掉线 → 拔卡后最迟约 1s
              被检出; 正常"停止采集"会先置 _poll_stop, 故其导致的中止不判为掉线。
            - 实得>0 即派发; returned==0 且无错误码 = 普通超时, 继续轮询。
              数据为 8 通道交错排列, 与上层 data[idx::ch] 解析一致。
        """
        n_total = self.channel_count * self.interval_count
        while self._poll_stop is not None and not self._poll_stop.is_set():
            try:
                result = self._ai.getData(n_total, 1000)   # 1s 超时: 拔卡最迟约 1s 检出
            except Exception as ex:
                if self._poll_stop.is_set():
                    break                                   # 正常"停止采集"导致的中止
                print(f'[DaqDevice] getData 异常, 判定采集卡断开: {ex}')
                self._notify_disconnect()
                break
            ret = result[0] if len(result) > 0 else None
            if ret is not None and BioFailed(ret):          # getData 返回错误码 = 掉线
                if self._poll_stop.is_set():
                    break                                   # 正常停止导致
                print(f'[DaqDevice] getData 返回错误 {ret}, 判定采集卡断开')
                self._notify_disconnect()
                break
            returned = result[1] if len(result) > 1 else 0
            data = result[2] if len(result) > 2 else None
            if returned and returned > 0 and data is not None:
                arr = np.asarray(data[:returned], dtype=np.float64)
                with self._lock:
                    self._buffer = arr
                if self._cb_data_ready:
                    try:
                        self._cb_data_ready(0, int(returned))
                    except Exception as ex:
                        print(f'[DaqDevice] data_ready 回调异常: {ex}')
            # returned==0 且无错误码 = 普通超时/暂无数据, 继续轮询 (不判掉线)

    def _notify_disconnect(self):
        """判定采集卡断开: 置 is_running=False 并触发 on_disconnect 回调 (仅一次)。

        Args:
            无

        Returns:
            无

        Side Effects:
            - self.is_running = False
            - 若已注册 _cb_disconnect, 调用之 (在轮询线程内; 回调方需自行跨线程投递)
        """
        self.is_running = False
        if self._cb_disconnect:
            try:
                self._cb_disconnect()
            except Exception as ex:
                print(f'[DaqDevice] disconnect 回调异常: {ex}')

    # ------------------------------------------------------------------
    def _raise_error(self, op: str, err_code):
        """把 DAQNavi 错误码 (ErrorCode 枚举或整数) 转成可读消息并抛 RuntimeError。"""
        code = err_code.value if hasattr(err_code, 'value') else err_code
        try:
            msg = AdxEnumToString('ErrorCode', code, 256)
        except Exception:
            msg = str(err_code)
        raise RuntimeError(f'{op} failed: {msg}')


# =============================================================================
# 模拟设备: 用 WaveGenerator 生成数据
# =============================================================================
class SimulatedDaqDevice(DaqDeviceBase):
    """模拟 USB-4716 — 用波形发生器生成数据,定时触发 OnDataReady。

    无需任何硬件即可运行,用于:
      - 在没插卡的环境下调试完整流程
      - 演示 / 单元测试
      - 验证算法链路正确性

    数据布局与真硬件完全一致(8 通道交错排列、电压标定)。
    """

    def __init__(self):
        super().__init__()
        self.device_description = 'Simulated USB-4716'
        self.is_initialized = True
        self._timer = None
        self._buffer: Optional[np.ndarray] = None
        self._sample_counter = 0
        self._lock = threading.Lock()

        # ====== 仿真数据源 (Excel) ======
        # 从 仿真数据源.xlsx 加载 (RPM, 变形_μm) 双数组; 加载失败时返回 (None, None),
        # _produce 会自动回退到旧的合成波形, 不影响程序启动
        self._sim_rpm_arr, self._sim_def_arr = load_sim_profile()
        # 时间扫描参数: 前 _sweep_duration_sec 秒线性把 RPM 从表起点扫到表终点
        self._sweep_duration_sec = 30.0
        # 扫描结束后, 在最大 RPM 附近做 ±2% 慢振荡, 频率为下面这个值
        self._hold_oscillation_hz = 0.1

        # ====== DIC 触发自检剖面参数 ======
        # profile = 'dic_selftest' (默认): 从 0 起依次在多档转速逐级"升速→保载",
        #           用于在无 USB-4716 时验证"转速保载 → 可进行 DIC 采样"弹窗;
        #           = 'sweep': 原 Excel 扫频 + 振荡演示 (保留, 改此一行即可切回)
        self.profile = 'dic_selftest'
        # 逐级保载转速 (RPM): 依次在每一档保载一段时间 (保载档保持不变)
        self._selftest_levels = [8000.0, 9000.0, 10000.0, 11000.0, 12000.0]
        self._selftest_ramp_sec = 8.0        # 升到下一档的升速时长 (s)
        self._selftest_hold_sec = 30.0       # 每档保载时长 (s)
        # 末档(12000)保载后继续升速到 final, 沿途按表继续生成转速/变形数据, 然后保持
        self._selftest_final_rpm = 15000.0   # 继续升到的最高转速 (RPM)
        self._selftest_final_ramp_sec = 24.0 # 12000 → final 的升速时长 (s, ≈125 RPM/s, 与档间同速率)

    def select_device(self, device_number: int, access_mode=1, mod_index=0):
        self.device_description = f'Simulated USB-4716 (#{device_number})'

    def configure_channels(self, signal_type: int = 1, value_range: int = 1):
        pass   # 模拟器不需要

    def configure_scan(self):
        pass

    def prepare(self) -> bool:
        capacity = self.channel_count * self.interval_count
        self._buffer = np.zeros(capacity)
        return True

    def start(self) -> bool:
        # 用 QTimer 在 Qt 主线程中产生数据,避免线程问题
        from PyQt5.QtCore import QTimer
        # 每次启动复位采样计数, 让 elapsed 从 0 起算 (自检剖面每轮重新升速→保载)
        self._sample_counter = 0
        interval_ms = int(self.interval_count / self.sample_rate * 1000)
        interval_ms = max(50, interval_ms)
        self._timer = QTimer()
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._produce)
        self._timer.start()
        self.is_running = True
        return True

    def stop(self) -> bool:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.is_running = False
        return True

    def cleanup(self):
        self.stop()

    @property
    def buffer_capacity(self) -> int:
        return self.channel_count * self.interval_count

    # ------------------------------------------------------------------
    def _produce(self):
        """生成一批模拟数据, 触发 on_data_ready 回调。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 修改 self._buffer (写入新一帧交错数据) 和 self._sample_counter
            - 触发 self._cb_data_ready(0, n * ch_count)

        数据生成策略 (两种模式自动选择):
          A) Excel 数据源模式 — 当 sim_data_source.load_sim_profile() 成功加载时启用
             - 按 self._sweep_duration_sec 在 RPM 表范围内线性扫描目标转速
             - 用 sim_data_source.lookup_deformation 查表 (线性插值) 得变形 μm
             - 反推两通道的原始电压, 使下游 calc_realspeed / calc_deformation
               解算后能严格还原 Excel 中的物理量:
                  V_speed = VoltageIni + (RPM / SpeedMax) * (VoltageMax - VoltageIni)
                  V_disp  = -deformation_um / TransPara1   # 负号源自 Dis2 = -mean(...)
             - 扫描结束后保持在最大 RPM ± 2% 慢振荡
          B) 回退模式 (Excel 未加载) — 与原代码完全一致的合成波形
             - 通道 0: 50/120/250Hz 谐波 + 噪声 + DC -0.3V
             - 通道 1: 0~30s 线性 1V→4V, 之后在 4V 附近小幅波动

        关键不变量:
            - 8 通道交错排列 [pt0_ch0, pt0_ch1, ..., pt1_ch0, ...]
            - 缓冲长度 = interval_count * channel_count
            - 回调签名 (offset, count) 与真硬件一致
        """
        n = self.interval_count
        ch_count = self.channel_count
        fs = self.sample_rate
        t = (self._sample_counter + np.arange(n)) / fs
        elapsed = self._sample_counter / fs

        # 构造 [n, ch] 矩阵
        sig = np.zeros((n, ch_count), dtype=np.float64)

        if self.profile == 'dic_selftest':
            # ============================================================
            # 模式 C: DIC 触发自检剖面 (升速 → 10000 保载 60s → 阶跃 12000)
            # 转速通道零噪声, 确定性恒定, 与位移通道共用同一 target_rpm
            # ============================================================
            self._fill_selftest_frame(sig, elapsed, n, ch_count)

        elif self._sim_rpm_arr is not None:
            # ============================================================
            # 模式 A: Excel 数据源驱动
            # ============================================================
            # 延迟导入 g, 避免硬件抽象层在无业务模式下也加载 public_para
            from public_para import g

            rpm_min = float(self._sim_rpm_arr[0])
            rpm_max = float(self._sim_rpm_arr[-1])

            # 当前目标 RPM: 前 sweep_duration_sec 秒线性扫描, 之后保持
            if elapsed < self._sweep_duration_sec:
                ratio = elapsed / self._sweep_duration_sec
                target_rpm = rpm_min + ratio * (rpm_max - rpm_min)
            else:
                target_rpm = rpm_max * (
                    1.0 + 0.02 * np.sin(2 * np.pi * self._hold_oscillation_hz * elapsed)
                )

            # 查表得对应变形 μm
            target_def_um = lookup_deformation(
                target_rpm, self._sim_rpm_arr, self._sim_def_arr
            )

            # ---- 反推位移通道原始电压 ----
            # 上游: data_arrays[0] = raw_V * TransPara1; Dis2 = -mean(data_arrays[0])
            # 要让最终 Dis2 = target_def_um, 需 raw_V = -target_def_um / TransPara1
            trans = g.TransPara1 if g.TransPara1 != 0 else 1800.0   # 防 0 兜底
            v_disp = -target_def_um / trans
            sig[:, 0] = (
                v_disp
                + 0.005 * np.sin(2 * np.pi * 50 * t)   # 叠加微弱 50Hz 振动
                + 0.001 * np.random.randn(n)           # 白噪声
            )

            # ---- 反推转速通道原始电压 ----
            # 上游: Realspeed = (V_mean - VIni) * SMax / (VMax - VIni) + SpeedfixNum
            # 要让 Realspeed ≈ target_rpm, 需 V_mean = VIni + target_rpm/SMax * (VMax-VIni)
            # (注: SpeedfixNum 修正项无法在这里抵消, 但默认为 0; 用户若设置非 0
            #  会在显示侧加上修正, 这是预期行为而非误差)
            denom = g.VoltageMax - g.VoltageIni
            if abs(denom) < 1e-9:
                v_speed = (g.VoltageMax + g.VoltageIni) / 2.0
            else:
                v_speed = g.VoltageIni + (target_rpm / g.SpeedMax) * denom
            sig[:, 1] = v_speed + 0.003 * np.random.randn(n)

        else:
            # ============================================================
            # 模式 B: 回退合成波形 (与原代码完全一致)
            # ============================================================
            # 通道 0: 振动信号 = 主频 50Hz + 谐波 + 噪声
            amp_main = 0.4 + 0.1 * np.sin(2 * np.pi * 0.05 * t)   # 慢变化
            sig[:, 0] = (
                amp_main * np.sin(2 * np.pi * 50 * t)
                + 0.08 * np.sin(2 * np.pi * 120 * t + 0.5)
                + 0.04 * np.sin(2 * np.pi * 250 * t)
                + 0.02 * np.random.randn(n)
                - 0.3                                              # 直流偏移
            )

            # 通道 1: 转速电压, 0~30s 内 1V→4V 线性升, 之后保持
            if elapsed < 30:
                v_speed = 1.0 + 3.0 * (elapsed / 30.0)
            else:
                v_speed = 4.0 + 0.05 * np.sin(2 * np.pi * 0.1 * elapsed)
            sig[:, 1] = v_speed + 0.003 * np.random.randn(n)

        # 其他通道: 弱噪声 (仅 sweep 模式 A/B; 自检模式已自行填满全部通道)
        if self.profile != 'dic_selftest':
            for ch in range(2, ch_count):
                sig[:, ch] = 0.005 * np.random.randn(n)

        # 按交错格式展平 [pt0_ch0, pt0_ch1, ..., pt0_chN, pt1_ch0, ...]
        flat = sig.flatten()
        with self._lock:
            self._buffer = flat
        self._sample_counter += n

        # 触发回调
        if self._cb_data_ready:
            try:
                self._cb_data_ready(0, n * ch_count)
            except Exception as ex:
                print(f'[SimulatedDaq] callback error: {ex}')

    # ------------------------------------------------------------------
    def _selftest_target_rpm(self, elapsed: float) -> float:
        """返回 DIC 触发自检剖面在 elapsed 时刻的目标转速 (RPM), 逐级保载。

        Args:
            elapsed: 自本轮采集开始的累计时间 (s)

        Returns:
            目标转速 (RPM)。剖面把 _selftest_levels 里的每一档依次走一遍, 每档为
            "升速 (从上一档线性升到本档, 历时 ramp) → 保载 (本档恒定, 历时 hold)";
            全部保载档走完后, 从末档继续升速到 _selftest_final_rpm (默认 15000, 历时
            _selftest_final_ramp_sec) 再保持 —— 该段只延伸转速/变形数据范围,
            不增加保载档 (保载档仍为 _selftest_levels)。

        Side Effects:
            无 (纯函数, 仅读取 self 上的剖面参数)

        设计意图:
            升速段使滑动窗口持续变化 → 不进入保载; 每进入一档保载约 4.1s (5 个读数)
            后, 若该档转速 ≥ 辨识器下限 即触发一次"可进行 DIC 采样"弹窗。
            档与档之间的升速会先退出上一档保载, 故各档互不影响、可逐档复触发。
        """
        levels = self._selftest_levels
        ramp = self._selftest_ramp_sec
        hold = self._selftest_hold_sec
        seg = ramp + hold                                    # 每档总时长 = 升速 + 保载
        prev = 0.0
        for i, lvl in enumerate(levels):
            base = i * seg                                   # 本档起始时刻
            if elapsed < base + ramp:                        # 升速段: prev → lvl
                frac = (elapsed - base) / ramp if ramp > 0 else 1.0
                return prev + (lvl - prev) * max(0.0, min(1.0, frac))
            if elapsed < base + seg:                         # 保载段: 恒定 lvl
                return lvl
            prev = lvl
        # 所有保载档走完后: 从末档继续升速到 final (默认 15000) 再保持 (此段不再保载)
        last = levels[-1]
        t_after = elapsed - len(levels) * seg                # "末档之后"的相对时间
        framp = self._selftest_final_ramp_sec
        if t_after < framp:                                  # 升速段: last → final
            frac = t_after / framp if framp > 0 else 1.0
            return last + (self._selftest_final_rpm - last) * max(0.0, min(1.0, frac))
        return self._selftest_final_rpm                      # 到顶后保持在 final

    def _fill_selftest_frame(self, sig: np.ndarray, elapsed: float,
                             n: int, ch_count: int):
        """填充一帧 DIC 自检数据: 通道0=位移, 通道1..7=转速 (零噪声)。

        Args:
            sig:      (n, ch_count) 待原地填充的交错前矩阵
            elapsed:  本帧起始的累计时间 (s), 决定剖面所处阶段
            n:        单通道采样点数
            ch_count: 通道数

        Returns:
            无 (原地写 sig)

        Side Effects:
            - 写 sig[:, 0] (位移 + 叠加振动成分 + 微噪声) 与
              sig[:, 1:ch_count] (转速, 零噪声)
            - 延迟导入 public_para.g 读取标定参数

        关键不变量:
            - 转速通道零噪声: 保证 calc_realspeed 解算出的 Realspeed 恒等于
              target_rpm, 即使 10000 正好压在辨识器低速护栏 (min_speed) 上也能
              确定性触发 (均值恰为 10000, 不小于护栏值)
            - 转速写入通道 1..7 (覆盖 UI 默认的转速信源通道 7), 使默认配置下
              即可读到转速, 无需手动改下拉框
            - 位移与转速共用同一 target_rpm (经 Excel 查表反推), 满足"同一转速
              驱动两通道"
            - 位移通道叠加 50/120/250 Hz 小幅振动 (相位随全局采样计数连续),
              供振动页 (FFT 频谱/峰峰值) 无卡演示; 正弦均值≈0, 对径向页的
              均值解算无可见影响
        """
        # 延迟导入, 与模式 A 保持一致 (避免无业务模式下加载 public_para)
        from public_para import g

        target_rpm = self._selftest_target_rpm(elapsed)

        # ---- 位移通道 (通道 0): 同一 target_rpm 驱动 ----
        if self._sim_rpm_arr is not None:
            target_def_um = lookup_deformation(
                target_rpm, self._sim_rpm_arr, self._sim_def_arr
            )
        else:
            target_def_um = 0.2 * target_rpm     # 无 Excel 兜底: 简单正比, 仅供演示
        trans = g.TransPara1 if g.TransPara1 != 0 else 1800.0
        v_disp = -target_def_um / trans
        # 叠加振动成分 (时间轴用全局采样计数, 跨帧相位连续, 频谱无拼接毛刺)
        t = (self._sample_counter + np.arange(n)) / self.sample_rate
        vib = (0.02 * np.sin(2 * np.pi * 50 * t)
               + 0.006 * np.sin(2 * np.pi * 120 * t + 0.5)
               + 0.003 * np.sin(2 * np.pi * 250 * t))
        sig[:, 0] = v_disp + vib + 0.001 * np.random.randn(n)   # 位移保留微噪声

        # ---- 转速通道 (通道 1..7): 零噪声, 确定性恒定 ----
        denom = g.VoltageMax - g.VoltageIni
        if abs(denom) < 1e-9:
            v_speed = (g.VoltageMax + g.VoltageIni) / 2.0
        else:
            v_speed = g.VoltageIni + (target_rpm / g.SpeedMax) * denom
        for ch in range(1, ch_count):
            sig[:, ch] = v_speed

    def get_data(self, count: int) -> np.ndarray:
        with self._lock:
            if self._buffer is None:
                return np.zeros(count)
            return self._buffer[:count].copy()


# =============================================================================
# 设备枚举: 检测是否接入了真实采集卡 (排除 DemoDevice)
# =============================================================================
def find_real_device():
    """枚举 DAQNavi 当前安装的设备, 返回首个真实(非 Demo)设备的 (设备号, 描述); 无则 None。

    Args:
        无

    Returns:
        (device_number, description) 元组 —— 首个描述不含 'Demo' 的已安装设备;
        若 DAQNavi 不可用 / 枚举失败 / 只有 DemoDevice 或无设备, 返回 None。

    Side Effects:
        - 临时创建并 dispose 一个 WaveformAiCtrl 仅用于读取 installedDevices,
          不打开、不占用任何具体设备。

    说明 (为何排除 Demo):
        DAQNavi 始终注册一个 'DemoDevice' (设备号常为 0, 会自动产数), 故"有没有真卡"
        不能看设备总数, 必须排除描述含 'Demo' 的项。USB-4716 等真卡描述形如
        'USB-4716,BID#0'。该枚举不需要打开设备, 不会被"打开能成功"误导, 判断可靠。
    """
    if not DAQNAVI_AVAILABLE:
        return None
    ai = None
    try:
        ai = WaveformAiCtrl()
        for nd in ai.device.installedDevices:
            desc = str(nd.Description)
            if 'demo' not in desc.lower():
                return (int(nd.DevicIntEnumber), desc)
        return None
    except Exception as e:
        print(f'[INFO] 枚举采集设备失败: {e}')
        return None
    finally:
        if ai is not None:
            try:
                ai.dispose()
            except Exception:
                pass


def real_device_present() -> bool:
    """轻量检测当前是否接入了真实(非 Demo)采集卡 (供设备在线监视高频调用)。

    Args:
        无

    Returns:
        bool: 已安装设备中存在描述不含 'Demo' 的设备 → True; 否则或失败 → False。

    Side Effects:
        - 仅调用 DAQNavi 静态设备枚举 (getInstalledDevices), 不新建/销毁控制对象,
          故可高频轮询 (~0.5s) 而开销很小。

    说明:
        判据与 find_real_device 一致 (排除 DemoDevice), 但更轻量 (不建 WaveformAiCtrl),
        用于"设备在线监视"定时器检测断开/重连。
    """
    if not DAQNAVI_AVAILABLE:
        return False
    try:
        from Automation.BDaq.BDaqApi import TDeviceCtrl, TArray   # type: ignore
        nodes = TArray.toDeviceTreeNode(TDeviceCtrl.getInstalledDevices(), True)
        for nd in nodes:
            if 'demo' not in str(nd.Description).lower():
                return True
        return False
    except Exception as e:
        print(f'[real_device_present] 枚举失败: {e}')
        return False


# =============================================================================
# 工厂函数: 自动选择真硬件或模拟器
# =============================================================================
def create_daq_device(force_simulate: bool = False) -> DaqDeviceBase:
    """创建 DAQ 设备,根据环境自动选择真硬件或模拟器。

    Args:
        force_simulate: 强制使用模拟器(用于调试)

    Returns:
        DaqDeviceBase 实例
    """
    if force_simulate or not DAQNAVI_AVAILABLE:
        if not DAQNAVI_AVAILABLE:
            print(f'[INFO] DAQNavi unavailable: {DAQNAVI_ERROR}')
            print('[INFO] Using SimulatedDaqDevice')
        else:
            print('[INFO] Force-simulate mode: Using SimulatedDaqDevice')
        return SimulatedDaqDevice()

    try:
        dev = DaqDevice()
        return dev
    except Exception as e:
        print(f'[WARN] Failed to create real device: {e}')
        print('[INFO] Falling back to SimulatedDaqDevice')
        return SimulatedDaqDevice()
