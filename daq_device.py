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
    from Automation.BDaq.BufferedAiCtrl import BufferedAiCtrl  # type: ignore
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
    """USB-4716 真实硬件接入(基于 DAQNavi SDK 4.0.x)。

    若 DAQNAVI_AVAILABLE=False,构造时会抛 RuntimeError。
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
        self._ai: Optional[BufferedAiCtrl] = None

    # ------------------------------------------------------------------
    def select_device(self, device_number: int, access_mode=1, mod_index=0):
        """对应 BufferedAiCtrl1.setSelectedDevice(devNum, accessMode, modIndex)"""
        if self._ai is None:
            self._ai = BufferedAiCtrl(self.device_description)

        # DAQNavi 的 selectedDevice 是 DeviceInformation 结构
        from Automation.BDaq import DeviceInformation     # type: ignore
        dev_info = DeviceInformation(device_number, access_mode, mod_index)
        self._ai.selectedDevice = dev_info

        # 检查是否初始化成功
        if not self._ai.initialized:
            raise RuntimeError(
                f'Failed to open device #{device_number}. '
                'Please select a device with DAQNavi wizard!'
            )

        # 读出实际选择的设备描述
        sd = self._ai.selectedDevice
        self.device_description = sd.description if hasattr(sd, 'description') else str(sd)

        # 注册事件
        self._ai.addDataReadyHandler(self._on_data_ready_evt)
        self._ai.addOverrunHandler(self._on_overrun_evt)
        self._ai.addCacheOverflowHandler(self._on_overflow_evt)

        self.is_initialized = True

    def configure_channels(self, signal_type: int = 1, value_range: int = 1):
        """配置 channel_count 个通道的 SignalType 和 ValueRange。

        signal_type=1 通常对应 Differential 输入;
        value_range=1 对应 USB-4716 的某个固定档位(具体看 DAQNavi 文档)。
        """
        for i in range(self.channel_count):
            ch = self._ai.channels[i]
            ch.signalType = signal_type
            ch.valueRange = value_range

    def configure_scan(self):
        sc = self._ai.scanChannel
        sc.channelStart = self.channel_start
        sc.channelCount = self.channel_count
        sc.samples = self.samples
        sc.intervalCount = self.interval_count

        self._ai.convertClock.rate = self.sample_rate
        self._ai.streaming = True

    def prepare(self) -> bool:
        ret = self._ai.prepare()
        if BioFailed(ret):
            self._raise_error('Prepare', ret)
            return False
        return True

    def start(self) -> bool:
        ret = self._ai.start()
        if BioFailed(ret):
            self._raise_error('Start', ret)
            return False
        self.is_running = True
        return True

    def stop(self) -> bool:
        if self._ai is None:
            return True
        ret = self._ai.stop()
        self.is_running = False
        if BioFailed(ret):
            self._raise_error('Stop', ret)
            return False
        return True

    def cleanup(self):
        if self._ai is not None:
            try:
                self._ai.cleanup()
            except Exception:
                pass
            self._ai = None

    @property
    def buffer_capacity(self) -> int:
        return self._ai.bufferCapacity if self._ai else 0

    # ------------------------------------------------------------------
    def get_data(self, count: int) -> np.ndarray:
        """从缓冲区取 count 个样本(double)。

        DAQNavi 的 getData 签名通常为:
            ret, data = getData(count, timeout_ms)
        返回的 data 已经是按 ValueRange 标定后的浮点电压(V)。
        """
        ret, data = self._ai.getData(count, 5000)   # 5 秒超时
        if BioFailed(ret):
            self._raise_error('GetData', ret)
            return np.zeros(0)

        return np.asarray(data, dtype=np.float64)

    # ------------------------------------------------------------------
    def _raise_error(self, op: str, err_code):
        try:
            msg = AdxEnumToString('ErrorCode', err_code, 256)
        except Exception:
            msg = str(err_code)
        raise RuntimeError(f'{op} failed: {msg}')

    # ---------- 事件桥接 ----------
    def _on_data_ready_evt(self, sender, args):
        if self._cb_data_ready:
            self._cb_data_ready(args.offset, args.count)

    def _on_overrun_evt(self, sender, args):
        if self._cb_overrun:
            self._cb_overrun(args.offset, args.count)

    def _on_overflow_evt(self, sender, args):
        if self._cb_overflow:
            self._cb_overflow(args.offset, args.count)


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
        """生成一批模拟数据,触发回调"""
        n = self.interval_count
        ch_count = self.channel_count
        fs = self.sample_rate
        t = (self._sample_counter + np.arange(n)) / fs

        # 构造 [n, ch] 矩阵
        sig = np.zeros((n, ch_count), dtype=np.float64)

        # 通道 0: 振动信号 = 主频 50Hz + 谐波 + 噪声
        # 模拟"位移传感器"输出: ~0.5V 振幅 → 标定后 ≈ 280μm 振幅
        amp_main = 0.4 + 0.1 * np.sin(2*np.pi*0.05*t)   # 慢变化
        sig[:, 0] = (
            amp_main * np.sin(2*np.pi*50*t)
            + 0.08 * np.sin(2*np.pi*120*t + 0.5)
            + 0.04 * np.sin(2*np.pi*250*t)
            + 0.02 * np.random.randn(n)
            - 0.3                                       # 直流偏移(模拟静态位移)
        )

        # 通道 1: 转速电压
        # 模拟一个 0~30s 内从 1V→4V 缓慢升速然后保持的过程
        elapsed = self._sample_counter / fs
        if elapsed < 30:
            v_speed = 1.0 + 3.0 * (elapsed / 30.0)
        else:
            v_speed = 4.0 + 0.05 * np.sin(2*np.pi*0.1*elapsed)
        sig[:, 1] = v_speed + 0.003 * np.random.randn(n)

        # 其他通道: 弱噪声
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

    def get_data(self, count: int) -> np.ndarray:
        with self._lock:
            if self._buffer is None:
                return np.zeros(count)
            return self._buffer[:count].copy()


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
