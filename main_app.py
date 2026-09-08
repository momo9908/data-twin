"""
main_app.py
============
HRSTD 在线变形测量系统 — 主入口与事件编排。

本模块经过模块化拆分后, 仅承担:
  1. 应用程序入口 (main 函数)
  2. MainForm 信号槽连接与事件编排 (按钮点击 / DAQ 回调 / 定时器)
  3. 在 MainForm 上挂载 DAQ / CSVLogger / PlotManager / UI 控件
  4. 共享采集会话管理 (start/pause/stop_session, 径向页与振动页共用一条数据流)
  5. 关闭时的资源清理

所有具体功能均交由对应模块实现:
  - dialogs.SpeedFixDialog          : 转速参数标定窗
  - settings_io.load_device_set     : DeviceSet.txt 读取
  - ui_builder.build_main_ui        : 主窗口 GUI 构建
  - data_processor.*                : 数据解算 (转速 / 变形)
  - csv_logger.CSVLogger / make_dated_folder : CSV 文件生命周期 + 目录
  - plot_manager.PlotManager        : 散点图 / 拟合线 / 自动缩放 / PNG 导出
  - daq_device.create_daq_device    : 硬件抽象 (真实 / 模拟)
  - vibration_tab / vibration_processor : 振动信号分析页 (FFT/带通/瀑布, 复刻 Delphi)
  - public_para.g                   : 全局参数单例

共享采集会话 (需求确认: 共享一次采集):
  - 径向页与振动页任一页"开始采集"都调 start_session(initiator), 硬件只有
    一条数据流; 每帧在 _on_data_ready 中先走径向流水线, 再分发给振动页
  - 采样率/分析点数以发起方页面的设置为准
  - 一次会话建一个 data\\YYYYMMDD序号\\ 目录: 径向 <编号>.CSV + <编号>.png,
    振动 <编号>_vib.CSV + <编号>_spec.png
  - 任一页"停止"结束整个会话; 振动页"暂停"挂起硬件流 (文件保持打开),
    其后再按"开始"将静默收尾旧档并新建档案 (对应 Delphi BtnPause 语义)

功能复现 (与重构前等价):
  - 径向页纯时域处理: 直接取采集均值作为当前间距, 计算变形量
  - 精确 CSV 输出: UTF-8 with BOM, CRLF, 全角冒号, 去尾零数据
  - 事件驱动: 散点与参考点累加绑定到 DAQ 数据更新事件
"""

import os
import sys
import time
import numpy as np

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QMessageBox,
    QPushButton, QLineEdit, QComboBox, QProgressBar, QLabel,
)
import pyqtgraph as pg

# 项目模块
from public_para import g
from daq_device import create_daq_device, find_real_device, real_device_present
from dialogs import SpeedFixDialog
from settings_io import load_device_set
from ui_builder import build_main_ui
from data_processor import (
    calc_realspeed, calc_deformation, piecewise_power_curve,
)
from elastic_plastic import fit_elastic_plastic
from csv_logger import CSVLogger, make_dated_folder
from plot_manager import PlotManager
from speed_stability import LoadHoldDetector, HoldEvent
from latency_report import LatencyProbe, LatencyReportWindow


# =============================================================================
# 全局视觉
# =============================================================================
pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')
pg.setConfigOption('antialias', True)


# =============================================================================
# 主窗口
# =============================================================================
class MainForm(QMainWindow):
    """主窗口: 信号槽连接 + 模块编排薄壳。

    职责:
        - 接收 DAQ 回调, 调度 data_processor / plot / csv 处理一帧数据
        - 处理用户按钮点击, 启停采集 / 归零 / 拟合
        - 维护采集相关的临时状态 (test_no / bmp_path / tick_count 等)

    维护的状态:
        - self.exe_directory: exe 所在目录, 用于 make_dated_folder
        - self.daq:           DAQ 设备 (真实或模拟)
        - self.csv:           CSVLogger 实例
        - self.plot:          PlotManager 实例
        - self.data_arrays:   8 通道临时缓冲数组列表
        - self.tick_count:    采集起始时间戳 (用于 CSV 列 1)
        - self.test_no / self.bmp_path: 当前测试编号与截图路径
        - self.is_first_overrun: 是否首次 overrun 警告

    UI 控件 (由 ui_builder 动态挂载, 类型注解仅供 IDE 提示, 不影响运行):
    """

    # ==== Qt 信号: DAQ 数据就绪 (跨线程桥) ====
    data_ready_signal = pyqtSignal(int, int)

    # ==== Qt 信号: 判定进入保载 → 投递弹窗 (算法 4.2 的 S3 跨线程投递) ====
    # 用 QueuedConnection 连接, 即使同线程也延到下一轮事件循环, 实现"入队即返回"
    hold_entered_signal = pyqtSignal(object)

    # ==== Qt 信号: 采集卡运行中断开 (真卡轮询线程检测到 → 跨线程投递到 GUI 报警) ====
    disconnect_signal = pyqtSignal()

    # —— 由 ui_builder.build_main_ui(self) 动态注入 ——
    # 指示器: (QGroupBox, QLabel) 元组
    lbl_speed:    tuple
    lbl_dis2:     tuple
    lbl_def:      tuple
    lbl_rate:     tuple
    # 绘图控件
    plotxy:       pg.PlotWidget
    scatter_xy:   pg.ScatterPlotItem
    fit_curve:    pg.PlotDataItem
    # 下拉框
    cb_sens:      QComboBox
    cb_dis_ch:    QComboBox
    cb_speed_ch:  QComboBox
    cb_rate:      QComboBox
    cb_save_time: QComboBox
    # 文本输入
    edit_target:  QLineEdit
    # 按钮
    btn_fit:      QPushButton
    btn_zero:     QPushButton
    btn_start:    QPushButton
    btn_stop:     QPushButton
    btn_refresh:  QPushButton
    # 其它
    lbl_fit_result: QLabel
    progress:     QProgressBar
    timer1:       QTimer
    timer2:       QTimer

    def __init__(self):
        """构造主窗口, 完成: 状态初始化 → DAQ/CSV/Plot 创建 → 信号槽连接 → UI 构建。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 创建 DAQ 设备 (调用 create_daq_device 工厂)
            - 创建 CSVLogger / PlotManager 空实例
            - 创建 LoadHoldDetector (转速保载辨识) 并初始化 DIC 提示框/时延报告窗引用
            - 连接 hold_entered_signal (QueuedConnection) 到 _show_hold_prompt
            - 通过 build_main_ui(self) 构造所有 UI 控件并挂到 self
            - 异步调度 _form_create 进行硬件初始化 (QTimer.singleShot(0))
        """
        super().__init__()
        self.exe_directory = os.path.dirname(os.path.abspath(sys.argv[0]))
        self.is_first_overrun: bool = True
        self.data_arrays = [np.zeros(0) for _ in range(8)]
        self.test_no = ''
        self.bmp_path = ''
        self._vib_png = ''
        # 共享采集会话状态: 'idle' / 'running' / 'paused' (两页按钮的唯一状态源)
        self.session_state = 'idle'
        self.tick_count = time.time()
        self.tick_count1 = time.time()

        # 硬件设备 + 跨线程信号桥
        self.daq = create_daq_device()
        self.daq.set_on_data_ready(self._raise_data_ready)
        self.daq.set_on_overrun(self._on_overrun)
        self.daq.set_on_cache_overflow(self._on_cache_overflow)
        # "采集卡断开"回调仅真卡 DaqDevice 提供 (模拟器无此方法, hasattr 守护)
        if hasattr(self.daq, 'set_on_disconnect'):
            self.daq.set_on_disconnect(self._raise_disconnect)
        self.data_ready_signal.connect(self._on_data_ready)
        # 判定→弹窗: 队列连接, 即使同线程也延到下一轮事件循环 ("入队即返回, 不阻塞采集")
        self.hold_entered_signal.connect(self._show_hold_prompt, Qt.QueuedConnection)
        # 采集卡断开: 队列连接 (回调在轮询线程触发, 报警/停止须在 GUI 线程执行)
        self.disconnect_signal.connect(self._on_disconnect)

        # 业务模块
        self.csv = CSVLogger()
        self.plot = PlotManager()

        # 转速保载辨识器: 任何转速保载都提示"可进行 DIC 采样";
        # min_speed 仅作极小下限 (挡静止态 ≈0 RPM 并防除零), 真正在转的保载均会触发
        self.hold_detector = LoadHoldDetector(min_speed=100.0)
        # 当前 DIC 提示框引用 (非 None 表示有提示框未关闭)
        self._dic_ready_box = None
        # 单例时延报告窗 (惰性创建, 单次刷新复用)
        self._latency_report = None
        # 就绪窗口读数序号 k (软触发锚点用), 每帧自增
        self._reading_index = 0
        # 软触发状态量: 判定成立置位 (供后续 DIC 关联用, 当前仅置位不接业务)
        self.dic_request_pending = False
        # 采集卡在线状态 (设备在线监视用; _form_create 按启动检测初始化)
        self._card_present = False
        # 闩锁: 本次运行是否曾检测到真卡 —— 一旦为真, 本次运行不再使用模拟器
        self._card_ever_seen = False
        # 当前是否在用模拟器 (_form_create 按"无卡"初始化)
        self._on_simulator = False

        # UI 构建 (会把控件挂到 self.xxx)
        build_main_ui(self)

        # 把 ui_builder 创建的绘图控件注入 PlotManager
        self.plot.attach(self.plotxy, self.scatter_xy, self.fit_curve,
                         self.residual_curve, self.lbl_residual)

        # 硬件初始化推迟到事件循环启动后执行, 避免阻塞窗口显示
        QTimer.singleShot(0, self._form_create)

    # ============================================================
    # 菜单回调
    # ============================================================
    def _show_speed_fix_dialog(self):
        """打开转速参数标定子窗口。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 弹出模态对话框; 用户确认后由 SpeedFixDialog 自行写盘和修改 g
        """
        dlg = SpeedFixDialog(self)
        dlg.exec_()

    def _show_about(self):
        """弹出 '关于' 信息框 (静态文案, 无副作用)。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 弹出模态 QMessageBox
        """
        QMessageBox.about(
            self, "关于",
            "HRSTD 在线变形测量系统\n"
            "浙江大学高速旋转机械实验室\n"
            "传感器配置: 激光测距仪 LK-H080"
        )

    # ============================================================
    # 启动初始化 (异步, 由 __init__ 末尾的 singleShot 触发)
    # ============================================================
    def _form_create(self):
        """启动时检测真卡 → 配置 DAQ → 初始化数据缓冲 → 准备硬件。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 调用 find_real_device() 枚举安装设备检测真卡 (排除 DemoDevice)
            - 有真卡: 按检测到的设备号 select_device, 不提示
            - 无真卡: _rebind_device 切换到模拟器, 配置完成后弹窗"未接入采集卡"
            - 调用 load_device_set() 读取 DeviceSet.txt
            - 配置 DAQ 设备 (通道 / 采样率 / signal_type / value_range)
            - 修改窗口标题 / g.Savetime / g.Sensitivity1 / g.TransPara1
            - 重新分配 self.data_arrays; 调用 self.daq.prepare()
            - 真卡打开/配置失败时弹 QMessageBox.critical 并 return

        行为不变量:
            - 真卡设备号由 find_real_device 自动选取首个非 Demo 设备 (不再写死 0,
              因 DAQNavi 的 0 号常为 DemoDevice)
            - 通道范围 [0, 8); samples=16384; interval_count=8192
            - signal_type=1 (Differential); value_range=1 (±10 V)
            - per_ch = max(1, buf_cap // (ch_count * 2))
        """
        # 启动检测真卡: 枚举安装设备并排除 DemoDevice; 有真卡用真卡, 否则换自带模拟器。
        real = find_real_device()        # (设备号, 描述) 或 None
        no_card = real is None
        self._card_present = not no_card             # 记录初始在线状态 (供监视器)
        self._on_simulator = no_card                 # 无卡才用模拟器
        if not no_card:
            self._card_ever_seen = True              # 启动即见到真卡 → 置闩锁
        if no_card:
            self._rebind_device(create_daq_device(force_simulate=True))
        # 启动"设备在线监视": 空闲时每 0.5s 轻量枚举, 检测采集卡断开/重连
        if not hasattr(self, 'monitor_timer'):
            self.monitor_timer = QTimer(self)
            self.monitor_timer.setInterval(500)
            self.monitor_timer.timeout.connect(self._monitor_devices)
        self.monitor_timer.start()
        try:
            device_number = load_device_set()          # 读 DeviceSet.txt
            if no_card:
                self.daq.select_device(device_number)   # 模拟器: 仅设描述, 无副作用
            else:
                self.daq.select_device(real[0])         # 真卡: 用检测到的设备号(已排除 Demo)
            self.daq.channel_start = 0
            self.daq.channel_count = 8
            self.daq.sample_rate = float(self.cb_rate.currentText())
            self.daq.samples = 16384
            self.daq.interval_count = 8192
            self.daq.configure_channels(signal_type=1, value_range=1)
            self.daq.configure_scan()

            self.setWindowTitle(f'HRSTD 在线变形测量系统 - {self.daq.device_description}')

            g.Savetime = float(self.cb_save_time.currentText())
            g.Sensitivity1 = 1.0 / 1.8
            g.TransPara1 = 1000.0 / g.Sensitivity1

            buf_cap = self.daq.buffer_capacity
            per_ch = max(1, buf_cap // (self.daq.channel_count * 2))
            self.data_arrays = [np.zeros(per_ch) for _ in range(8)]

            self.is_first_overrun = True
            self.daq.prepare()

        except Exception as e:
            QMessageBox.critical(self, '硬件错误', f'请确认匹配的采集卡连接!\n\n详细: {e}')
            return

        # 无真卡: 已切到模拟器, 弹窗告知 (有真卡则不提示)
        if no_card:
            QMessageBox.information(
                self, '未接入采集卡',
                '未检测到 USB-4716 采集卡，已切换到模拟器运行。\n'
                '（数据为模拟生成，仅供调试 / 演示）'
            )

    def _rebind_device(self, new_dev):
        """把 self.daq 换成 new_dev 并重新绑定数据/告警回调。

        Args:
            new_dev: 新的 DaqDeviceBase 实例 (此处为回退用的 SimulatedDaqDevice)

        Returns:
            无

        Side Effects:
            - self.daq = new_dev; 重新 set_on_data_ready / overrun / cache_overflow

        说明:
            用于启动检测到无真卡时, 把 __init__ 里建好的真卡设备替换为模拟器;
            回调与 __init__ 中的绑定一致, data_ready_signal 等信号槽无需重连。
        """
        self.daq = new_dev
        self.daq.set_on_data_ready(self._raise_data_ready)
        self.daq.set_on_overrun(self._on_overrun)
        self.daq.set_on_cache_overflow(self._on_cache_overflow)

    # ============================================================
    # 按钮事件
    # ============================================================
    def _btn_start_click(self):
        """[径向页-开始采集] 按钮: 转发到共享会话 start_session('radial')。"""
        self.start_session('radial')

    def start_session(self, initiator: str = 'radial'):
        """共享采集会话入口: 两页的"开始采集"都汇聚到此 (需求: 共享一次采集)。

        Args:
            initiator: 'radial' (径向页发起) 或 'vib' (振动页发起);
                       采样率/分析点数以发起方页面的设置为准

        Returns:
            无

        Side Effects:
            - 上一会话处于暂停态时先 _finalize_paused_quietly() 静默收尾旧档
            - 按发起方应用 daq.sample_rate (振动页发起时还应用 interval_count
              = 分析点数 N, samples = 2N), 并 configure_scan() 下发到硬件
            - 复位两页绘图 (plot.reset_for_new_run / vib_tab 在 begin_session 内复位)
            - 重置 g.DataSaveFlag、is_first_overrun、hold_detector、DIC 保载序列
            - 重新分配 self.data_arrays (每通道 interval_count 大小)
            - make_dated_folder() 创建一个共用试验目录; 打开径向 5 列 CSV
              与振动 6 列 CSV (vib_tab.begin_session)
            - 启动 timer2; 重置 tick_count(1); daq.prepare()+start()
            - session_state='running' 并统一刷新两页按钮
            - 失败时弹 QMessageBox.critical
        """
        # 闩锁: 本次运行已检测到真卡且当前在用模拟器 → 模拟器已停用, 拒绝启动并提示重启
        if self._on_simulator and self._card_ever_seen:
            QMessageBox.warning(
                self, '模拟器已停用',
                '本次运行已检测到采集卡，模拟器已停用。\n'
                '请重启程序：接好采集卡启动用真卡，或断开采集卡启动用模拟器。'
            )
            return
        try:
            # 暂停态直接再开始 = 静默收尾旧档 + 新建档案 (对应 Delphi 暂停→开始语义)
            if self.session_state == 'paused':
                self._finalize_paused_quietly()

            # 采集参数以发起方为准 (用户可能临时改了下拉框)
            try:
                if initiator == 'vib':
                    self.daq.sample_rate = self.vib_tab.preferred_rate()
                    n = self.vib_tab.preferred_interval()
                    self.daq.interval_count = n
                    self.daq.samples = n * 2
                else:
                    self.daq.sample_rate = float(self.cb_rate.currentText())
                self.daq.configure_scan()   # 下发到硬件 (模拟器为空操作)
            except Exception:
                pass

            self.plot.reset_for_new_run()

            g.DataSaveFlag = False
            self.is_first_overrun = True
            self.hold_detector.reset()   # 新采集复位保载辨识, 重新从调速态开始
            self._reading_index = 0
            self.dic_request_pending = False
            self.dic_tab.clear_hold_speeds()   # 新采集清空上一轮保载转速序列 (只留最近一轮)

            ic = self.daq.interval_count
            self.data_arrays = [np.zeros(ic) for _ in range(8)]

            # 创建当日数据目录, 同时取得 test_no (作为两页 CSV / PNG 文件名前缀)
            full, test_no = make_dated_folder(self.exe_directory)
            g.Filestrtemp = full
            self.test_no = test_no
            self._vib_png = ''

            # 打开径向 CSV (与原版字节一致) + 振动页 CSV (6 列, <编号>_vib.CSV)
            self.csv.open(full, test_no, int(self.daq.sample_rate), int(g.Savetime))
            self.vib_tab.begin_session(full, test_no, float(self.daq.sample_rate))

            g.DataSaveFlag = True

            self.timer2.start()
            self.tick_count1 = time.time()
            self.tick_count = time.time()

            self.daq.prepare()
            self.daq.start()

            self.session_state = 'running'
            self._apply_run_state()
            src = '（振动页发起）' if initiator == 'vib' else ''
            self.statusBar().showMessage(f'采集进行中...{src}')
        except Exception as e:
            QMessageBox.critical(self, '启动失败', str(e))

    def pause_session(self):
        """暂停共享会话 (对应 Delphi BtnPause): 停硬件流, 两页文件保持打开。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 仅 running 态生效: daq.stop(); timer2 停; session_state='paused'
            - 两份 CSV 不关闭 (无帧到来自然停写); 其后"开始采集"将新建档案
            - 停止硬件失败时弹窗并维持原状态
        """
        if self.session_state != 'running':
            return
        try:
            self.daq.stop()
        except Exception as e:
            QMessageBox.critical(self, '暂停失败', str(e))
            return
        self.timer2.stop()
        self.session_state = 'paused'
        self._apply_run_state()
        self.statusBar().showMessage(
            '采集已暂停（文件保持打开；再按"开始采集"将新建档案）')

    def _finalize_paused_quietly(self):
        """暂停态直接开新会话前的旧档收尾: 关两份 CSV, 不弹窗不截图。

        Side Effects:
            - g.DataSaveFlag=False; 径向/振动 CSV close (幂等);
              session_state='idle' (旧档数据已完整落盘, 可直接开新档)
        """
        g.DataSaveFlag = False
        self.csv.close()
        self.vib_tab.close_csv()
        self.session_state = 'idle'

    def _apply_run_state(self):
        """按 session_state 统一刷新两页按钮使能 (共享会话的唯一状态源)。

        Side Effects:
            - 径向页: 开始 (非 running 可按) / 停止 (running 或 paused) /
              刷新 (仅 idle); 振动页经 vib_tab.set_run_state 同步
        """
        st = self.session_state
        running = st == 'running'
        paused = st == 'paused'
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running or paused)
        self.btn_refresh.setEnabled(st == 'idle')
        self.vib_tab.set_run_state(st)

    def _btn_stop_click(self):
        """[径向页-停止采集] 按钮: 转发到共享会话 stop_session()。"""
        self.stop_session()

    def stop_session(self):
        """结束共享会话: 停 DAQ → 两页各出 PNG → 关振动 CSV → 延时关径向 CSV。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 仅 running/paused 态生效; 设置 g.DataSaveFlag = False
            - 启动 self.timer1 (100ms 后 _on_timer1 关径向 CSV 并弹"保存数据成功")
            - 径向散点图 PNG (plot.export_png) 与振动频谱 PNG + 振动 CSV 关闭
              (vib_tab.end_session); 截图失败仅打印 [WARN]
            - 调用 self.daq.stop(); 停 timer2; session_state='idle' 并刷新两页按钮

        行为不变量 (与重构前 _btn_stop_click 一致):
            - 即使截图失败, CSV 和 DAQ 仍然要正确停止
            - DAQ.stop() 失败才弹 QMessageBox.critical
        """
        if self.session_state == 'idle':
            return
        try:
            g.DataSaveFlag = False
            self.timer1.start()

            # 导出径向散点图 PNG (失败仅警告, 不影响后续流程)
            try:
                if g.Filestrtemp and self.test_no:
                    self.bmp_path = self.plot.export_png(g.Filestrtemp, self.test_no)
            except Exception as ex:
                print(f'[WARN] 截图保存失败: {ex}')

            # 振动页收尾: 频谱 PNG 快照 + 关闭 6 列 CSV (内部自行吞截图异常)
            self._vib_png = self.vib_tab.end_session(export_png=True)

            self.daq.stop()
            self.timer2.stop()
            self.session_state = 'idle'
            self._apply_run_state()
            self.statusBar().showMessage('采集已停止')

        except Exception as e:
            QMessageBox.critical(self, '停止失败', str(e))

    def _btn_zero_click(self):
        """[位置归零] 按钮: 把当前间距 Dis2 设为零点 Dis0。

        Args:
            无

        Returns:
            无

        Side Effects:
            - g.Dis0 = g.Dis2
            - 弹出归零成功的提示框

        行为不变量 (与原 main_app.py:450-452 一致):
            - 弹窗文案 '当前位置已设定为零点\\nDis0 = X.XX μm' 保留 2 位小数
        """
        g.Dis0 = g.Dis2
        self.plot.residual_zero_changed()
        QMessageBox.information(
            self, '归零成功',
            f'当前位置已设定为零点\nDis0 = {g.Dis0:.2f} μm'
        )

    def _btn_refresh_click(self):
        """[刷新] 按钮: 清空已采集的显示数据, 但不删除任何已保存的 CSV 文件。

        Args:
            无

        Returns:
            无

        Side Effects:
            - self.plot.reset_for_new_run(): 清空散点 + 复位坐标范围
            - 4 个指标 Label (转速/间距/变形量/完成率) 复位为 '0'
            - 进度条 self.progress 归零
            - 状态栏提示
            - **不触碰磁盘上的 CSV 文件**, 不停止/启动采集, 不改 Dis0

        行为约束:
            - 仅在未采集时可用 (采集进行中本按钮由 _btn_start_click 禁用,
              _btn_stop_click 恢复), 避免边采边清。
        """
        self.plot.reset_for_new_run()
        self.lbl_speed[1].setText('0')
        self.lbl_dis2[1].setText('0')
        self.lbl_def[1].setText('0')
        self.lbl_rate[1].setText('0')
        self.progress.setValue(0)
        self.statusBar().showMessage('已清空采集显示数据（已保存的 CSV 文件不受影响）')

    def _btn_fit_click(self):
        """仅对径向升速包络拟合弹性/弹塑性模型，更新曲线、残余值及原有公式记录。"""
        xs, ys = self.plot.get_rising_points()
        fit = fit_elastic_plastic(xs, ys)
        if not fit.ok:
            self.plot.clear_fit_curve()
            self.lbl_fit_result.setText(
                f'<span style="color:#c0392b;">拟合失败：{fit.message}</span>')
            self.statusBar().showMessage(f'拟合失败：{fit.message}')
            return

        x_arr = np.linspace(0.0, fit.max_rpm, 300)
        y_arr = piecewise_power_curve(fit, x_arr)
        self.plot.draw_fit_curve(x_arr, y_arr)
        self.plot.set_residual_fit(fit)

        if fit.plastic and fit.has_elastic_segment:
            formula_html = (
                f'<i>N</i> ≤ {fit.xc:#.3g}：<i>δ</i> = {fit.a1:#.3g}·<i>N</i>&#8201;&#8201;<sup>{fit.b1:#.3g}</sup>（弹性）<br>'
                f'<i>N</i> &gt; {fit.xc:#.3g}：<i>δ</i> = {fit.a2:#.3g}·<i>N</i>&#8201;&#8201;<sup>{fit.b2:#.3g}</sup>（塑性）<br>'
                f'最大弹性变形 = {fit.max_elastic_deformation:#.3g} mm<br>')
            formula_lines = [
                f'N <= {fit.xc:.0f} : δ = {fit.a1:.6g} * N^{fit.b1:.12g}',
                f'N > {fit.xc:.0f} : δ = {fit.a2:.6g} * N^{fit.b2:.12g}',
            ]
            heading = '弹塑性分段幂函数拟合'
        elif fit.plastic:
            formula_html = (
                f'<i>δ</i> = {fit.a1:#.3g}·<i>N</i>&#8201;&#8201;<sup>{fit.b1:#.3g}</sup>（塑性模型）<br>'
                '幂指数 &gt; 2.1；缺少弹性分界点，残余变形待判定<br>')
            formula_lines = [
                f'δ = {fit.a1:.6g} * N^{fit.b1:.12g}',
                '幂指数 > 2.1；缺少弹性分界点，残余变形待判定',
            ]
            heading = '塑性幂函数拟合（缺少弹性段）'
        else:
            formula_html = (
                f'<i>δ</i> = {fit.a1:#.3g}·<i>N</i>&#8201;&#8201;<sup>{fit.b1:#.3g}</sup>（弹性模型）<br>'
                f'尚未检出可靠塑性段；有效范围至 {fit.max_rpm:#.3g} RPM<br>')
            formula_lines = [
                f'δ = {fit.a1:.6g} * N^{fit.b1:.12g}',
                f'尚未检出可靠塑性段；有效范围至 {fit.max_rpm:.0f} RPM',
            ]
            heading = '弹性幂函数拟合（1.9≤p≤2.1）'
        # 只精简界面标题；CSV 的原有标题和公式保存精度保持不变。
        display_heading = '弹性幂函数拟合' if not fit.plastic else heading
        self.lbl_fit_result.setText(
            f'<b>{display_heading}</b><br>{formula_html}'
            f'确定系数 R² = {fit.r2:.4f}（{fit.n_points} 点）<br>'
            f'<span style="color:#8a96a6; font-size:13px;">'
            f'（<i>δ</i>=变形 mm，<i>N</i>=转速 RPM；仅升速包络）</span>')

        # 沿用原有 CSV 末尾公式记录机制，不改原始列，不新增残余数据列。
        fit_lines = [
            '', f'{heading}（δ=变形mm, N=转速RPM）',
            *formula_lines,
            f'确定系数 R² = {fit.r2:.4f} （{fit.n_points} 点）',
        ]
        saved = self.csv.append_fit(fit_lines)
        if saved:
            self.statusBar().showMessage(
                f'拟合完成：R²={fit.r2:.4f}；拟合函数已追加到 {os.path.basename(saved)}')
        else:
            self.statusBar().showMessage(
                f'拟合完成：R²={fit.r2:.4f}（暂无已保存 CSV，拟合函数未写盘）')

    # ============================================================
    # 参数变更事件
    # ============================================================
    def _on_sens_change(self, idx: int):
        """灵敏度下拉变化: 切换 g.Sensitivity1 与 g.TransPara1。

        Args:
            idx: 下拉项索引 (0 = LK-H080 1mV/1.8μm; 其它 = 通用 1.0)

        Returns:
            无

        Side Effects:
            - idx==0: Sensitivity1 = 1/1.8
            - idx!=0: Sensitivity1 = 1.0
            - 同步 TransPara1 = 1000 / Sensitivity1

        行为不变量 (与原 main_app.py:481-486 一致)
        """
        if idx == 0:
            g.Sensitivity1 = 1.0 / 1.8
        else:
            g.Sensitivity1 = 1.0
        g.TransPara1 = 1000.0 / g.Sensitivity1

    # ============================================================
    # DAQ 事件回调 (纯时域逻辑)
    # ============================================================
    def _raise_data_ready(self, offset: int, count: int):
        """硬件线程回调: 发射 Qt 信号, 把执行切换到 GUI 线程。

        Args:
            offset: DAQ 缓冲偏移量 (字节)
            count:  本次可读样本数

        Returns:
            无

        Side Effects:
            - 仅发射 data_ready_signal, 不做任何重活

        说明:
            DAQNavi SDK 的 on_data_ready 在采集线程中触发, 直接更新 UI 不安全;
            通过 pyqtSignal -> _on_data_ready (槽函数) 来跨线程派发。
        """
        self.data_ready_signal.emit(offset, count)

    @pyqtSlot(int, int)
    def _on_data_ready(self, offset: int, count: int):
        """GUI 线程的数据处理入口: 从 DAQ 取一帧, 解算并刷新 UI / CSV。

        Args:
            offset: DAQ 缓冲偏移量 (未使用, 仅为信号槽签名匹配)
            count:  本次可读样本数 (= 单帧 ch_count * j)

        Returns:
            无

        Side Effects:
            - 从 DAQ 拷出 count 个样本到 numpy 数组
            - 写入 self.data_arrays[0] (位移) / [1] (转速)
            - 通过 calc_realspeed 修改 g.Realspeed
            - 把 g.Realspeed 喂入 hold_detector; 跳入保载时打 t0 并经队列投递弹窗事件
              (实际弹窗与时延报告在 _show_hold_prompt 中完成)
            - 通过 calc_deformation 修改 g.Dis2
            - 通过 plot.add_point 加散点 (横轴=转速 RPM, 纵轴=变形量)
            - 更新 4 个指示器 Label 和进度条
            - 若 g.DataSaveFlag 为真且 csv 已打开, 写一行到 CSV
            - 异常被吞掉, 仅打印 [DataReady ERR]

        行为不变量 (与原 main_app.py:494-571 一致):
            - 位移通道在切片后乘 TransPara1; 转速通道保留原始电压量纲
            - 位移通道用 cb_dis_ch (默认 0), 转速通道用 cb_speed_ch (默认 7)
            - 当 dis_channel 长度 < interval_count 时, 静默 return
            - 进度条根据 |deformation|/target * 100 计算, 上限 100
        """
        try:
            ch_count = self.daq.channel_count
            j = count // ch_count

            data = self.daq.get_data(count)
            if data is None or len(data) == 0:
                return

            # ---- 共享采集会话: 同一帧原始电压分发给振动页 (第二条流水线) ----
            # vib_tab.on_frame 内部自带 try/except, 不会影响径向流水线
            self.vib_tab.on_frame(data, ch_count, time.time() - self.tick_count)

            dis_idx = min(self.cb_dis_ch.currentIndex(), ch_count - 1)
            speed_idx = min(self.cb_speed_ch.currentIndex(), ch_count - 1)

            # 位移通道: 电压 → μm (只对该通道乘 TransPara1, 避免污染其它通道)
            self.data_arrays[0] = (data[dis_idx::ch_count][:j] * g.TransPara1).copy()
            # 转速通道: 保留原始电压 (V), 由 calc_realspeed 直接对 V 求均值
            self.data_arrays[1] = data[speed_idx::ch_count][:j].copy()

            # ======== 转速反推 (写 g.Realspeed) ========
            calc_realspeed(self.data_arrays[1], j)

            # ======== 转速保载辨识 + 判定→弹窗时延计时 (算法 4.2 前半) ========
            # 务实版口径: 判定与投递均在 GUI 线程; 合计 t_ui 计入 S1 判据计算。
            # 判定成立后入队即返回, 不在数据回调里同步建窗 (不阻塞采集)。
            self._reading_index += 1
            t_pre = time.perf_counter()                  # 【S1 起点】判据计算前
            ev = self.hold_detector.update(g.Realspeed)
            t0 = time.perf_counter()                     # 【t_ui 起点】判定成立时刻
            if ev is HoldEvent.ENTER_HOLD:
                # 记录本次保载转速 (四舍五入), 供 DIC 分析按点击次序自动带入
                self.dic_tab.record_hold_speed(round(g.Realspeed))
                # —— S2 触发本地处理: 置软触发状态量 + 记录锚点 (常数次操作) ——
                self.dic_request_pending = True
                probe = LatencyProbe(
                    t_pre=t_pre, t0_perf=t0, t0_wall=time.time(), t_b=0.0,
                    k=self._reading_index, plateau=self.hold_detector.platform_mean,
                )
                probe.t_b = time.perf_counter()          # 【S2 终点】本地处理完, 即将投递
                # —— S3 跨线程投递: 队列连接, 入队即返回 (采集继续, 不等弹窗) ——
                self.hold_entered_signal.emit(probe)

            # ======== 变形量计算 (写 g.Dis2) ========
            dis2, deformation, temps = calc_deformation(
                self.data_arrays[0], self.daq.interval_count
            )
            if dis2 is None:
                return

            # ======== 散点图 + 拟合参考点累积 ========
            self.plot.add_point(temps, deformation)

            # ======== 更新 UI 指示器 ========
            self.lbl_speed[1].setText(f'{g.Realspeed:.1f}')
            self.lbl_dis2[1].setText(f'{dis2:.1f}')
            self.lbl_def[1].setText(f'{deformation:.1f}')

            try:
                target_def = float(self.edit_target.text())
            except Exception:
                target_def = 100.0

            rate = 0.0
            if target_def > 0:
                rate = min(100.0, abs(deformation) / target_def * 100.0)
            self.lbl_rate[1].setText(f'{rate:.1f}')
            self.progress.setValue(int(rate))

            # ======== CSV 落盘 ========
            if g.DataSaveFlag and self.csv.is_open:
                elapsed = time.time() - self.tick_count
                self.csv.write_row(elapsed, g.Dis0, dis2, deformation, g.Realspeed)

        except Exception as e:
            print(f'[DataReady ERR] {e}')

    @pyqtSlot(object)
    def _show_hold_prompt(self, probe):
        """GUI 线程槽: 取出判定事件 → 弹非模态提示窗 → 刷新时延报告窗 (算法 4.2 后半)。

        Args:
            probe: LatencyProbe, 含采集判定侧打点 (t0 / t_b / k / 平台均值)

        Returns:
            无

        Side Effects:
            - 在 probe 上补打 t_c (S3 终点) 与 t_show (t_ui 终点)
            - 创建/刷新 DIC 提示窗与时延报告窗 (均非模态)
            - 控制台打印 t_ui 及各流程耗时; 超 0.1s 限值则告警

        口径:
            t_c 取本槽开始执行时刻 = 跨线程投递 (S3) 终点; t_show 取提示窗
            show()+repaint() 之后 = 弹窗可见时刻 = t_ui 终点。
        """
        probe.t_c = time.perf_counter()          # 【S3 终点】槽开始执行
        self._present_dic_box(probe)             # 建非模态提示窗 + 强制首绘, 内部填 t_show
        self._present_latency_report(probe)      # 第二个窗口: 单次刷新时延报告
        st = probe.stages()
        if st['t_ui'] >= LatencyReportWindow.LIMIT_S:
            print(f"[t_ui 告警] {st['t_ui'] * 1000:.3f} ms 超过 100 ms 限值")
        else:
            print(f"[t_ui] {st['t_ui'] * 1000:.3f} ms 达标 | "
                  f"S1={st['t_calc'] * 1000:.3f} S2={st['t_trig'] * 1000:.3f} "
                  f"S3={st['t_post'] * 1000:.3f} S4+5={st['t_render'] * 1000:.3f} ms")

    def _present_dic_box(self, probe):
        """创建并显示非模态「可进行 DIC 采样」提示窗, 强制首绘并记录 t_show。

        Args:
            probe: LatencyProbe; 本方法在其上写 t_show_perf / t_show_wall

        Returns:
            无

        Side Effects:
            - 关闭上一个提示窗 (多级保载: 新提示取代旧提示, 非模态不阻塞)
            - 新建 QMessageBox (非模态, WA_DeleteOnClose), show() + repaint()
            - repaint 后立即在 probe 上打 t_show (perf + 墙钟)
            - 不调用 activateWindow (非模态不抢焦点); 触发提示音

        行为不变量:
            - 必须先 show() 再 repaint() 再读 t_show (确保"首次绘制可见")
        """
        if self._dic_ready_box is not None:
            self._dic_ready_box.close()          # finished → _on_dic_ready_closed 置 None
            self._dic_ready_box = None
        box = QMessageBox(self)
        box.setWindowTitle('转速保载')
        box.setIcon(QMessageBox.Information)
        box.setText(f'转速已进入稳定（{probe.plateau:.0f} RPM），可以开始 DIC 采样')
        box.setStandardButtons(QMessageBox.Ok)
        box.setModal(False)
        box.setAttribute(Qt.WA_DeleteOnClose, True)
        box.finished.connect(self._on_dic_ready_closed)
        self._dic_ready_box = box
        box.show()
        box.repaint()                            # 强制首绘, 确保"可见"
        probe.t_show_perf = time.perf_counter()  # 【t_ui 终点】弹窗可见时刻
        probe.t_show_wall = time.time()
        box.raise_()
        try:
            QApplication.beep()
        except Exception:
            pass

    def _present_latency_report(self, probe):
        """惰性创建 / 单次刷新单例「判定—弹窗时延报告」窗口。

        Args:
            probe: 已填齐两侧打点的 LatencyProbe

        Returns:
            无

        Side Effects:
            - 首次调用创建 self._latency_report (LatencyReportWindow), 之后复用刷新
            - 仅在后台更新报告内容，不显示或置顶；计时、阈值判断与控制台输出不变
        """
        if self._latency_report is None:
            self._latency_report = LatencyReportWindow(self)
        self._latency_report.update_report(probe)

    def _on_dic_ready_closed(self, _result: int):
        """DIC 提示框关闭回调: 清空引用, 允许下次保载再次弹窗。

        Args:
            _result: QDialog 完成码 (未使用, 仅匹配 finished 信号签名)

        Returns:
            无

        Side Effects:
            - self._dic_ready_box = None
        """
        self._dic_ready_box = None

    def _on_overrun(self, offset: int, count: int):
        """DAQ 缓冲区 Overrun 警告: 仅首次弹窗, 后续静默。

        Args:
            offset: 偏移量 (未使用)
            count:  样本数 (未使用)

        Returns:
            无

        Side Effects:
            - 首次触发时弹 QMessageBox.warning 并设 self.is_first_overrun = False
            - 后续触发不弹窗 (避免频繁报警)
        """
        if self.is_first_overrun:
            QMessageBox.warning(self, '系统警告', '底层缓冲区发生 Overrun')
            self.is_first_overrun = False

    def _on_cache_overflow(self, offset: int, count: int):
        """驱动层 CacheOverflow 错误: 弹严重错误框, 每次都弹。

        Args:
            offset: 偏移量 (未使用)
            count:  样本数 (未使用)

        Returns:
            无

        Side Effects:
            - 弹 QMessageBox.critical
        """
        QMessageBox.critical(self, '系统错误', '驱动层 CacheOverflow')

    def _raise_disconnect(self):
        """采集卡断开回调 (真卡轮询线程内调用): 发射 Qt 信号切到 GUI 线程处理。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 仅 emit disconnect_signal (线程安全); 报警/停止在 _on_disconnect 完成
        """
        self.disconnect_signal.emit()

    @pyqtSlot()
    def _on_disconnect(self):
        """采集卡断开的 GUI 处理: 自动停止采集 + 状态栏 + 弹窗报警 (仅真卡采集时触发)。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 仅在采集中生效 (防重复): 置 g.DataSaveFlag=False; 容错停 DAQ; 关 CSV;
              复位按钮; 停 timer2
            - 状态栏显示断开; 弹出 QMessageBox.critical 报警

        说明:
            回调在真卡轮询线程触发, 经 disconnect_signal 队列投递到本槽 (GUI 线程),
            故可安全操作界面。此处用容错停止 (不复用 _btn_stop_click), 避免因卡已拔而
            再弹"停止失败"/"保存成功"。
        """
        if self.session_state == 'idle':
            return   # 不在采集中, 忽略 (防重复/延迟触发)
        self._card_present = False   # 同步在线状态, 避免监视器重复报"断开"
        g.DataSaveFlag = False
        try:
            self.daq.stop()          # 卡已拔, 容错停止 (吞掉可能的错误)
        except Exception:
            pass
        self.csv.close()             # 关闭径向数据文件 (幂等, 保住已采数据)
        self.vib_tab.close_csv()     # 关闭振动数据文件 (幂等)
        self.timer2.stop()
        self.session_state = 'idle'
        self._apply_run_state()
        self.statusBar().showMessage('采集卡已断开，采集已自动停止')
        QMessageBox.critical(
            self, '采集卡断开',
            '检测到 USB-4716 采集卡已断开，采集已自动停止。\n'
            '请检查采集卡连接后，重启程序以重新识别采集卡。'
        )

    def _monitor_devices(self):
        """设备在线监视 (monitor_timer, ~0.5s): 空闲时轻量枚举, 检测采集卡断开/重连。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 真卡采集中直接返回 (断开由真卡轮询线程负责, 不枚举以免干扰);
              模拟器运行中仍监视 (以便检出中途插卡)
            - 进入"检测到卡→停用模拟器→待重启"状态后不再处理
            - 用 real_device_present() 查在线状态, 与 _card_present 比较, 翻转则:
              不在→在: 置闩锁 _card_ever_seen; 在用模拟器 → _disable_simulator_for_card
                       (决定 a: 停用模拟器 + 提示重启); 否则(真卡) → 弹"已重新连接"
              在→不在: 弹"采集卡已断开"(空闲, 无采集可停)

        说明:
            闩锁: 本次运行一旦检测到真卡, 就不再用模拟器; 想用模拟器须重启并断开卡。
            状态先更新再弹窗, 防模态期间定时器重入导致重复弹窗。
        """
        # 真卡采集中: 断开由轮询线程负责, 不枚举; 模拟器采集中仍监视 (为检出插卡)
        if self.daq.is_running and not self._on_simulator:
            return
        # 已"检测到卡→停用模拟器→待重启": 不再监视/弹窗
        if self._on_simulator and self._card_ever_seen:
            return
        present = real_device_present()
        if present == self._card_present:
            return
        self._card_present = present
        if present:
            self._card_ever_seen = True              # 置闩锁: 本次运行不再用模拟器
            if self._on_simulator:
                self._disable_simulator_for_card()   # 决定 a: 停用模拟器 + 提示重启
            else:
                self.statusBar().showMessage('采集卡已重新连接')
                QMessageBox.information(
                    self, '采集卡重连',
                    '检测到 USB-4716 采集卡已重新连接。\n'
                    '如需使用真卡采集，请重启程序以重新识别采集卡。'
                )
        else:
            self.statusBar().showMessage('采集卡已断开')
            QMessageBox.warning(
                self, '采集卡断开',
                '检测到 USB-4716 采集卡已断开。'
            )

    def _disable_simulator_for_card(self):
        """检测到真卡时停用模拟器 (决定 a): 停止模拟采集 + 禁用采集按钮 + 提示重启。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 若模拟器正在采集: 置 g.DataSaveFlag=False, 停 DAQ, 关 CSV, 停 timer2
            - 禁用 开始/停止/刷新 按钮 (本次运行模拟器不可再用; 须重启)
            - 状态栏提示 + 弹窗告知"检测到采集卡, 模拟器已停用, 请重启"

        说明:
            闩锁 _card_ever_seen 已置位; 此后即使按钮被别处启用, _btn_start_click 也会
            拦截并提示重启。想用模拟器: 重启程序并断开采集卡。
        """
        if self.daq.is_running:
            g.DataSaveFlag = False
            try:
                self.daq.stop()
            except Exception:
                pass
            self.csv.close()
            self.vib_tab.close_csv()
            self.timer2.stop()
        self.session_state = 'idle'
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        # 振动页同样锁死采集入口 (须重启后才能用真卡)
        self.vib_tab.set_run_state('idle')
        self.vib_tab.btn_start.setEnabled(False)
        self.statusBar().showMessage('检测到采集卡，模拟器已停用，请重启程序以使用真卡')
        QMessageBox.information(
            self, '检测到采集卡',
            '检测到 USB-4716 采集卡已连接，模拟器已停用。\n'
            '请重启程序：接好采集卡启动即用真卡采集。'
        )

    # ============================================================
    # 定时器
    # ============================================================
    def _on_timer2(self):
        """timer2 (200ms 周期): 5Hz 节流的绘图坐标自动缩放。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 读 g.Dis0 / g.Dis2 / g.Realspeed 计算当前 temps 与 deformation
            - 调用 self.plot.auto_scale() 仅放大坐标
            - 异常吞掉仅打印 [Timer2 ERR]

        说明:
            原版的 5Hz 节流是为了避免 _on_data_ready (高频) 中频繁 setRange 导致
            画面抖动; 因此 add_point 立即生效, setRange 仅由本定时器触发。
        """
        try:
            deformation = g.Dis2 - g.Dis0
            temps = g.Realspeed
            self.plot.auto_scale(temps, deformation)
        except Exception as e:
            print(f'[Timer2 ERR] {e}')

    def _on_timer1(self):
        """timer1 (停止采集后 100ms 单次): 关闭 CSV, 弹出保存成功提示。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 若 CSV 仍然打开, 调用 self.csv.close()
            - 弹 QMessageBox.information 显示 CSV / PNG 路径

        行为不变量 (与原 main_app.py:599-610 一致):
            - 文案 '保存数据成功!\\n\\n数据文件: ...' 不变
            - 当 bmp_path / 振动文件路径非空才追加对应行
            - 100ms 延时是为了让 _on_data_ready 写完最后一帧再关文件
        """
        if self.csv.is_open:
            self.csv.close()
            msg = f"保存数据成功!\n\n数据文件: {self.csv.path}"
            vib_csv = getattr(self.vib_tab.csv, 'path', '')
            if vib_csv:
                msg += f"\n振动数据文件: {vib_csv}"
            if self.bmp_path:
                msg += f"\n图形文件: {self.bmp_path}"
            if self._vib_png:
                msg += f"\n频谱图形: {self._vib_png}"
            QMessageBox.information(self, '保存数据成功', msg)

    # ============================================================
    # 关闭窗口
    # ============================================================
    def closeEvent(self, event):
        """窗口关闭事件: 停止 DAQ → 清理硬件 → 关闭 CSV → 接受事件。

        Args:
            event: QCloseEvent 实例

        Returns:
            无 (通过 event.accept() 允许关闭)

        Side Effects:
            - 若 DAQ 仍在跑, 调用 stop() (异常吞掉)
            - 调用 self.daq.cleanup()
            - 调用 self.csv.close() (幂等)
            - event.accept() 允许窗口关闭
        """
        try:
            if self.daq.is_running:
                self.daq.stop()
            self.daq.cleanup()
        except Exception:
            pass
        self.csv.close()
        if hasattr(self, 'vib_tab'):
            self.vib_tab.close_csv()
        event.accept()


# =============================================================================
# 程序入口
# =============================================================================
def main():
    """应用程序入口: 创建 QApplication, 实例化 MainForm, 进入事件循环。

    Args:
        无

    Returns:
        无 (通过 sys.exit() 退出)

    Side Effects:
        - 创建 QApplication 单例 (使用 sys.argv)
        - 设置 'Fusion' 样式
        - 创建并显示主窗口
        - 阻塞执行 app.exec_() 直到窗口关闭
    """
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    win = MainForm()
    win.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
