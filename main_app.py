"""
main_app.py
============
对应 Delphi Main.pas — 涡轮超速预应力评估测试控制系统(完整 Python 版)。

功能复现:
  1. 启动时从 Sysdata\\DeviceSet.txt 读取硬件标定参数
  2. 配置 USB-4716: 8 通道 / 10 kHz / 16384 缓冲 / 8192 间隔
  3. Start/Pause/Stop 控制实时连续采集
  4. 数据就绪回调:
        - 通道拆分 → 电压标定 → 计算转速
        - 应变数据趋势显示
        - CSV 数据存盘
  5. 转速修正子窗口

依赖: PyQt5, pyqtgraph, numpy
   pip install PyQt5 pyqtgraph numpy
"""

import os
import sys
import time
from datetime import datetime

import numpy as np

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QGroupBox,
    QLabel, QPushButton, QComboBox, QProgressBar,
    QHBoxLayout, QVBoxLayout, QFormLayout,
    QMessageBox, QAction,
    QDialog, QDialogButtonBox, QSpinBox, QDoubleSpinBox,
)

import pyqtgraph as pg

# 项目模块
from public_para import g, cMinFloat, cMaxFloat
from daq_device import create_daq_device, DAQNAVI_AVAILABLE


# =============================================================================
# 全局视觉设置(白底,清晰打印)
# =============================================================================
pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')
pg.setConfigOption('antialias', True)


# =============================================================================
# 转速修正子窗口(对应 SpeedFix.pas)
# =============================================================================
class SpeedFixDialog(QDialog):
    """转速参数修正对话框(对应 TSpeedFixForm)。

    允许用户编辑 DeviceSet.txt 中的标定参数。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('转速参数设置')
        self.setFixedSize(380, 240)

        form = QFormLayout()

        self.sp_dev = QSpinBox()
        self.sp_dev.setRange(0, 31)
        self.sp_dev.setValue(0)
        form.addRow('设备号:', self.sp_dev)

        self.sp_vini = QDoubleSpinBox()
        self.sp_vini.setDecimals(3)
        self.sp_vini.setRange(-10, 10)
        self.sp_vini.setValue(g.VoltageIni)
        form.addRow('零点电压 V(零):', self.sp_vini)

        self.sp_vmax = QDoubleSpinBox()
        self.sp_vmax.setDecimals(3)
        self.sp_vmax.setRange(-10, 10)
        self.sp_vmax.setValue(g.VoltageMax)
        form.addRow('满量程电压 V(满):', self.sp_vmax)

        self.sp_smax = QDoubleSpinBox()
        self.sp_smax.setDecimals(0)
        self.sp_smax.setRange(1, 200000)
        self.sp_smax.setValue(g.SpeedMax)
        form.addRow('最大转速 RPM:', self.sp_smax)

        self.sp_sfix = QDoubleSpinBox()
        self.sp_sfix.setDecimals(1)
        self.sp_sfix.setRange(-5000, 5000)
        self.sp_sfix.setValue(g.SpeedfixNum)
        form.addRow('转速修正量:', self.sp_sfix)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)

    def _on_ok(self):
        """保存到 DeviceSet.txt 并更新全局变量"""
        g.VoltageIni = float(self.sp_vini.value())
        g.VoltageMax = float(self.sp_vmax.value())
        g.SpeedMax = float(self.sp_smax.value())
        g.SpeedfixNum = float(self.sp_sfix.value())

        # 写回配置文件
        try:
            exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
            path = os.path.join(exe_dir, 'Sysdata', 'DeviceSet.txt')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(f'{int(self.sp_dev.value())}\n')
                f.write(f'{g.VoltageIni}\n')
                f.write(f'{g.VoltageMax}\n')
                f.write(f'{g.SpeedMax}\n')
                f.write(f'{g.SpeedfixNum}\n')
        except Exception as e:
            QMessageBox.warning(self, '保存失败', f'写入配置文件失败:\n{e}')

        self.accept()


# =============================================================================
# 主窗口(对应 TMainForm)
# =============================================================================
class MainForm(QMainWindow):

    # 用于跨线程触发数据就绪事件(模拟设备在主线程,真硬件可能在其他线程)
    data_ready_signal = pyqtSignal(int, int)

    def __init__(self):
        super().__init__()

        # ---------- 路径 ----------
        self.exe_directory = os.path.dirname(os.path.abspath(sys.argv[0]))

        # ---------- 内部状态(对应 private 字段) ----------
        self.is_first_overrun: bool = True

        # 通道数据数组(0..7),对应 dataScaledArray1..8
        self.data_arrays = [np.zeros(0) for _ in range(8)]
        # 原始拼合数据(交错格式)
        self.data_scaled = np.zeros(0)

        # 实时显示值
        self.current_strain: float = 0.0
        self.current_speed: float = 0.0

        # 文件
        self.csv_file = None          # 数据 CSV 文件句柄
        self.csv_path = ''
        self.test_no = ''

        # 时钟
        self.tick_count = time.time()      # 数据保存起点
        self.tick_count1 = time.time()     # XY 曲线起点

        # ---------- 数据采集设备 ----------
        self.daq = create_daq_device()
        self.daq.set_on_data_ready(self._raise_data_ready)
        self.daq.set_on_overrun(self._on_overrun)
        self.daq.set_on_cache_overflow(self._on_cache_overflow)
        # 跨线程把回调切回 Qt 主线程
        self.data_ready_signal.connect(self._on_data_ready)

        # ---------- UI 构建 ----------
        self._build_ui()

        # ---------- 启动初始化(对应 FormCreate) ----------
        QTimer.singleShot(0, self._form_create)

    # ============================================================
    # UI 构建
    # ============================================================
    def _build_ui(self):
        self.setWindowTitle('涡轮超速预应力评估测试控制系统')
        self.resize(1280, 820)

        # ---------- 菜单 ----------
        menubar = self.menuBar()
        m_settings = menubar.addMenu('设置(&S)')
        act_speed_fix = QAction('转速参数标定...', self)
        act_speed_fix.triggered.connect(self._show_speed_fix_dialog)
        m_settings.addAction(act_speed_fix)

        m_file = menubar.addMenu('文件(&F)')
        act_exit = QAction('退出(&X)', self)
        act_exit.triggered.connect(self.close)
        m_file.addAction(act_exit)

        # ---------- TabWidget(主页签 + 设置页签) ----------
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # 主测试页
        self.tab_main = QWidget()
        self.tabs.addTab(self.tab_main, '实时测试')
        self._build_main_tab()

        # 设置页
        self.tab_setup = QWidget()
        self.tabs.addTab(self.tab_setup, '采集参数')
        self._build_setup_tab()

        # 颜色页
        self.tab_color = QWidget()
        self.tabs.addTab(self.tab_color, '显示颜色')
        self._build_color_tab()

        # ---------- 状态栏 ----------
        self.statusBar().showMessage('就绪')

    # ------------------------------------------------------------
    def _build_main_tab(self):
        """主测试页签 — 实时应变/转速显示与趋势图"""
        layout = QVBoxLayout(self.tab_main)

        # 顶部数值显示行
        top = QHBoxLayout()
        self.lbl_strain = self._make_indicator('实时应变', '0')
        self.lbl_speed = self._make_indicator('实时转速 RPM', '0')
        for w in [self.lbl_strain, self.lbl_speed]:
            top.addWidget(w[0])
        layout.addLayout(top)

        # 控制按钮 + 进度
        btn_box = QGroupBox('采集控制')
        box_layout = QVBoxLayout(btn_box)
        btn_row = QHBoxLayout()
        self.btn_start = QPushButton('开始(&S)')
        self.btn_pause = QPushButton('暂停(&P)')
        self.btn_stop = QPushButton('停止(&T)')
        self.btn_save = QPushButton('手动保存数据')
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._btn_start_click)
        self.btn_pause.clicked.connect(self._btn_pause_click)
        self.btn_stop.clicked.connect(self._btn_stop_click)
        self.btn_save.clicked.connect(self._btn_save_click)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_pause)
        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_save)
        box_layout.addLayout(btn_row)

        progress_row = QHBoxLayout()
        progress_row.addWidget(QLabel('保存进度:'))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        progress_row.addWidget(self.progress)
        box_layout.addLayout(progress_row)
        layout.addWidget(btn_box)

        # 应变趋势图
        grp_strain = QGroupBox('应变趋势 vs 时间')
        gt = QVBoxLayout(grp_strain)
        self.plot_strain = pg.PlotWidget()
        self.plot_strain.setLabel('left', '应变')
        self.plot_strain.setLabel('bottom', '时间', units='s')
        self.plot_strain.showGrid(x=True, y=True, alpha=0.3)
        self.curve_strain = self.plot_strain.plot([], [], pen=pg.mkPen('#2ca02c', width=2))
        gt.addWidget(self.plot_strain)
        layout.addWidget(grp_strain, 1)

        # 转速趋势图
        grp_speed = QGroupBox('转速趋势 vs 时间')
        gs = QVBoxLayout(grp_speed)
        self.plot_speed = pg.PlotWidget()
        self.plot_speed.setLabel('left', '转速', units='RPM')
        self.plot_speed.setLabel('bottom', '时间', units='s')
        self.plot_speed.showGrid(x=True, y=True, alpha=0.3)
        self.curve_speed = self.plot_speed.plot([], [], pen=pg.mkPen('#1f77b4', width=2))
        gs.addWidget(self.plot_speed)
        layout.addWidget(grp_speed, 1)

        # ---------- 启动用的子定时器 ----------
        # Timer1: 保存完成提示
        self.timer1 = QTimer(self)
        self.timer1.setSingleShot(True)
        self.timer1.setInterval(100)
        self.timer1.timeout.connect(self._on_timer1)

        # Timer4: 启动后延时创建数据文件
        self.timer4 = QTimer(self)
        self.timer4.setSingleShot(True)
        self.timer4.setInterval(500)
        self.timer4.timeout.connect(self._on_timer4)

    # ------------------------------------------------------------
    def _build_setup_tab(self):
        """采集参数页签"""
        outer = QVBoxLayout(self.tab_setup)

        # ===== 采集参数 =====
        grp_acq = QGroupBox('数据采集参数')
        f = QFormLayout(grp_acq)
        self.cb_ch_start = QComboBox()
        self.cb_ch_start.addItems([str(i) for i in range(16)])
        self.cb_ch_start.currentIndexChanged.connect(self._on_cb3_change)
        f.addRow('起始通道:', self.cb_ch_start)

        self.cb_ch_count = QComboBox()
        self.cb_ch_count.addItems(['1', '2', '4', '8', '16'])
        self.cb_ch_count.setCurrentText('8')
        self.cb_ch_count.currentTextChanged.connect(self._on_cb4_change)
        f.addRow('扫描通道数:', self.cb_ch_count)

        self.cb_rate = QComboBox()
        self.cb_rate.addItems(['1000', '5000', '10000', '20000', '50000', '100000', '200000'])
        self.cb_rate.setCurrentText('10000')
        self.cb_rate.currentTextChanged.connect(self._on_cb5_change)
        f.addRow('采样率 (Hz):', self.cb_rate)

        self.cb_samples = QComboBox()
        self.cb_samples.addItems(['1024', '2048', '4096', '8192', '16384'])
        self.cb_samples.setCurrentText('8192')
        self.cb_samples.currentTextChanged.connect(self._on_cb6_change)
        f.addRow('每次采样点数:', self.cb_samples)

        # 应变通道 + 转速通道
        self.cb_freq_ch = QComboBox()
        self.cb_freq_ch.addItems([f'通道{i}' for i in range(16)])
        self.cb_freq_ch.setCurrentIndex(0)
        f.addRow('应变信号通道:', self.cb_freq_ch)

        self.cb_speed_ch = QComboBox()
        self.cb_speed_ch.addItems([f'通道{i}' for i in range(16)])
        self.cb_speed_ch.setCurrentIndex(7)
        f.addRow('转速信号通道:', self.cb_speed_ch)

        outer.addWidget(grp_acq)

        # ===== 传感器灵敏度 =====
        grp_sens = QGroupBox('传感器灵敏度')
        f2 = QFormLayout(grp_sens)
        self.cb_sens = QComboBox()
        self.cb_sens.addItems([
            '加速度计(8 mV/(m/s²))',
            '通用 1 mV/V',
            '电荷放大器(4.78)',
            '加速度计(0.5)',
            '通用 1.0',
            '电涡流位移(1.8 mV/μm)',
        ])
        self.cb_sens.setCurrentIndex(5)
        self.cb_sens.currentIndexChanged.connect(self._on_cb9_change)
        f2.addRow('传感器灵敏度:', self.cb_sens)

        outer.addWidget(grp_sens)

        # ===== 保存参数 =====
        grp_save = QGroupBox('保存设置')
        f3 = QFormLayout(grp_save)
        self.cb_save_time = QComboBox()
        self.cb_save_time.setEditable(True)
        self.cb_save_time.addItems(['10', '30', '60', '120', '300', '600'])
        self.cb_save_time.setCurrentText('60')
        self.cb_save_time.currentTextChanged.connect(self._on_cb8_change)
        f3.addRow('采集时长 (秒):', self.cb_save_time)

        outer.addWidget(grp_save)
        outer.addStretch()

    # ------------------------------------------------------------
    def _build_color_tab(self):
        """颜色设置页签"""
        outer = QVBoxLayout(self.tab_color)
        outer.addWidget(QLabel('(此页签用于自定义曲线颜色,功能与原版等价,'
                                '简化版默认使用预定义配色。)'))
        outer.addStretch()

    # ------------------------------------------------------------
    def _make_indicator(self, label_text: str, init_value: str):
        """构造数值指示器组件(标签+大字数值)"""
        gb = QGroupBox(label_text)
        v = QVBoxLayout(gb)
        v.setContentsMargins(8, 12, 8, 8)
        lbl = QLabel(init_value)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet('font-size: 24px; font-weight: bold; color: #1f77b4;')
        v.addWidget(lbl)
        return (gb, lbl)

    # ============================================================
    # FormCreate —— 启动初始化
    # ============================================================
    def _form_create(self):
        try:
            # ---- 1) 读取 DeviceSet.txt ----
            path = os.path.join(self.exe_directory, 'Sysdata', 'DeviceSet.txt')
            if not os.path.exists(path):
                # 找不到时使用默认值
                self.statusBar().showMessage(f'未找到 {path}, 使用默认参数')
                device_number = 0
                g.VoltageIni = 1.0
                g.VoltageMax = 5.0
                g.SpeedMax = 60000.0
                g.SpeedfixNum = 0.0
            else:
                with open(path, 'r', encoding='utf-8') as f:
                    lines = [ln.strip() for ln in f.readlines() if ln.strip()]
                device_number = int(lines[0])
                g.VoltageIni = float(lines[1])
                g.VoltageMax = float(lines[2])
                g.SpeedMax = float(lines[3])
                g.SpeedfixNum = float(lines[4])

            # ---- 2) 配置 DAQ 设备 ----
            self.daq.select_device(device_number)
            self.daq.channel_start = 0
            self.daq.channel_count = 8
            self.daq.sample_rate = 10000.0
            self.daq.samples = 16384
            self.daq.interval_count = 8192
            self.daq.configure_channels(signal_type=1, value_range=1)
            self.daq.configure_scan()

            # 更新标题
            self.setWindowTitle(
                f'涡轮超速预应力评估测试控制系统 - {self.daq.device_description}'
            )

            # ---- 3) 全局参数初始化 ----
            g.Savetime = float(self.cb_save_time.currentText() or 60)
            g.Sensitivity1 = 1.0 / 1.8
            g.TransPara1 = 1000.0 / g.Sensitivity1

            # ---- 4) 分配数据缓冲 ----
            buf_cap = self.daq.buffer_capacity
            per_ch = max(1, buf_cap // (self.daq.channel_count * 2))
            self.data_arrays = [np.zeros(per_ch) for _ in range(8)]

            # ---- 5) 准备硬件 ----
            self.is_first_overrun = True
            self.daq.prepare()
            self.data_scaled = np.zeros(max(buf_cap, self.daq.interval_count * self.daq.channel_count))

            # 状态栏
            mode_str = '真实硬件' if DAQNAVI_AVAILABLE and not self.daq.device_description.startswith('Sim') else '模拟模式'
            self.statusBar().showMessage(f'初始化完成 - {mode_str}')

        except Exception as e:
            QMessageBox.critical(
                self, '硬件错误',
                f'硬件错误,请确认匹配的采集卡连接!\n\n详细: {e}'
            )
            # 原 Delphi 用 Timer3 延时关闭,这里 3 秒后退出
            QTimer.singleShot(3000, self.close)

    # ============================================================
    # 按钮事件
    # ============================================================
    def _btn_start_click(self):
        """对应 BtnStartClick"""
        try:
            g.DataSaveFlag = False

            self.is_first_overrun = True

            # 按 IntervalCount 重新设缓冲大小
            ic = self.daq.interval_count
            self.data_arrays = [np.zeros(ic) for _ in range(8)]

            self.current_strain = 0.0
            self.current_speed = 0.0
            self.curve_strain.setData([], [])
            self.curve_speed.setData([], [])
            self.tick_count1 = time.time()

            # 准备 + 启动采集
            self.daq.prepare()
            self.data_scaled = np.zeros(self.daq.samples * 8)

            # 延时创建数据文件
            self.timer4.start()

            # 启动硬件采集
            self.daq.start()

            # UI 状态
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(True)
            self.btn_stop.setEnabled(True)

            self.statusBar().showMessage('采集进行中...')

        except Exception as e:
            QMessageBox.critical(self, '启动失败', str(e))

    def _btn_pause_click(self):
        try:
            self.daq.stop()
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.statusBar().showMessage('已暂停')
        except Exception as e:
            QMessageBox.critical(self, '暂停失败', str(e))

    def _btn_stop_click(self):
        """对应 BtnStopClick"""
        try:
            g.DataSaveFlag = False
            self.timer1.start()    # 触发"保存成功"提示

            self.daq.stop()

            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_stop.setEnabled(False)

            # 清理缓冲
            for i in range(8):
                self.data_arrays[i] = np.zeros(0)

            self.data_scaled = np.zeros(0)

            self.statusBar().showMessage('采集已停止')

        except Exception as e:
            QMessageBox.critical(self, '停止失败', str(e))

    def _btn_save_click(self):
        """对应 BtnSaveClick — 手动创建一个新的数据文件"""
        try:
            now = datetime.now()
            date_str = now.strftime('%Y%m%d')
            base = os.path.join(self.exe_directory, 'data')
            i = 1
            s1 = '01'
            sub = f'{date_str}{s1}'
            full = os.path.join(base, sub)
            while os.path.exists(full):
                i += 1
                s1 = f'{i:02d}'
                sub = f'{date_str}{s1}'
                full = os.path.join(base, sub)
            os.makedirs(full, exist_ok=True)
            self.test_no = f'{date_str}{s1}'
            self.csv_path = os.path.join(full, f'{self.test_no}.CSV')
            self.csv_file = open(self.csv_path, 'w', encoding='utf-8')
            self.csv_file.write(f'应变通道: {self.cb_freq_ch.currentText()}\n')
            self.csv_file.write(f'采样频率: {self.daq.sample_rate}\n')
            self.csv_file.write(f'采集时长: {self.cb_save_time.currentText()}s\n')
            self.csv_file.write('时间 应变 转速\n')
            g.DataSaveFlag = True
            self.tick_count = time.time()
            self.statusBar().showMessage(f'保存中: {self.csv_path}')
        except Exception as e:
            QMessageBox.critical(self, '保存失败', str(e))

    def _show_speed_fix_dialog(self):
        """对应 N1Click — 打开转速修正子窗口"""
        dlg = SpeedFixDialog(self)
        dlg.exec_()

    # ============================================================
    # 参数变更事件
    # ============================================================
    def _on_cb3_change(self, idx):
        """起始通道变更"""
        self.daq.channel_start = idx
        self.cb_freq_ch.setCurrentIndex(idx)

    def _on_cb4_change(self, txt):
        try:
            self.daq.channel_count = int(txt)
        except ValueError:
            pass

    def _on_cb5_change(self, txt):
        try:
            self.daq.sample_rate = float(txt)
            self.daq.samples = int(self.cb_samples.currentText()) * 2
            self.daq.interval_count = int(self.cb_samples.currentText())
        except ValueError:
            pass

    def _on_cb6_change(self, txt):
        try:
            self.daq.sample_rate = float(self.cb_rate.currentText())
            self.daq.samples = int(txt) * 2
            self.daq.interval_count = int(txt)
        except ValueError:
            pass

    def _on_cb9_change(self, idx):
        """传感器灵敏度选择(对应 ComboBox9Change)"""
        table = [8.0, 1.0, 4.78, 0.5, 1.0, 1.0/1.8]
        if 0 <= idx < len(table):
            g.Sensitivity1 = table[idx]
            g.TransPara1 = 1000.0 / g.Sensitivity1

    def _on_cb8_change(self, txt):
        try:
            g.Savetime = float(txt)
            self.progress.setRange(0, int(g.Savetime))
        except ValueError:
            pass

    # ============================================================
    # DAQ 事件回调
    # ============================================================
    def _raise_data_ready(self, offset: int, count: int):
        """从工作线程切换到 Qt 主线程"""
        self.data_ready_signal.emit(offset, count)

    @pyqtSlot(int, int)
    def _on_data_ready(self, offset: int, count: int):
        """对应 BufferedAiCtrl1DataReady — 核心算法"""
        try:
            ch_count = self.daq.channel_count
            j = count // ch_count
            if j <= 0:
                return

            # ---- 1) 读取数据 ----
            data = self.daq.get_data(count)
            if data is None or len(data) == 0:
                return

            # ---- 2) 电压标定 V → μm ----
            data = data * g.TransPara1

            # ---- 3) 拆分通道 ----
            freq_idx = self.cb_freq_ch.currentIndex()
            speed_idx = self.cb_speed_ch.currentIndex()

            # 安全裁剪到实际通道数
            freq_idx = min(freq_idx, ch_count - 1)
            speed_idx = min(speed_idx, ch_count - 1)

            # 用 numpy 切片高效拆分(等价于原 for 循环)
            # data 布局: [pt0_ch0, pt0_ch1, ..., pt0_chN-1, pt1_ch0, ...]
            self.data_arrays[0] = data[freq_idx::ch_count][:j].copy()
            self.data_arrays[1] = data[speed_idx::ch_count][:j].copy()

            # 应变通道均值、转速通道均值
            strain_value = float(np.mean(self.data_arrays[0]))
            sum1 = float(np.sum(self.data_arrays[1])) / (j * g.TransPara1)   # 还原成电压

            # ---- 4) 实时转速 ----
            denom = (g.VoltageMax - g.VoltageIni)
            if abs(denom) < 1e-9:
                g.Realspeed = 0.0
            else:
                g.Realspeed = (sum1 - g.VoltageIni) * g.SpeedMax / denom
            if g.Realspeed < 0:
                g.Realspeed = 0.0
            g.Realspeed += g.SpeedfixNum

            self.current_strain = strain_value
            self.current_speed = g.Realspeed
            self.lbl_strain[1].setText(f'{strain_value:.4f}')
            self.lbl_speed[1].setText(f'{g.Realspeed:.1f}')

            # ---- 5) 应变/转速趋势曲线 ----
            t_now = time.time() - self.tick_count1
            xs_s, ys_s = self.curve_strain.getData()
            if xs_s is None:
                xs_s = np.array([])
                ys_s = np.array([])
            xs_s = np.append(xs_s, t_now)
            ys_s = np.append(ys_s, strain_value)
            self.curve_strain.setData(xs_s, ys_s)

            xs_r, ys_r = self.curve_speed.getData()
            if xs_r is None:
                xs_r = np.array([])
                ys_r = np.array([])
            xs_r = np.append(xs_r, t_now)
            ys_r = np.append(ys_r, g.Realspeed)
            self.curve_speed.setData(xs_r, ys_r)

            # ---- 6) 数据落盘 ----
            if g.DataSaveFlag and self.csv_file is not None:
                elapsed = time.time() - self.tick_count
                self.csv_file.write(
                    f'{elapsed:.4f} {strain_value:.4f} {g.Realspeed:.4f}\n'
                )
                self.csv_file.flush()
                if g.Savetime > 0:
                    self.progress.setValue(min(int(elapsed), int(g.Savetime)))

        except Exception as e:
            print(f'[DataReady ERR] {e}')
            import traceback
            traceback.print_exc()
            # 出错时安全停止
            try:
                self.daq.stop()
            except Exception:
                pass
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)

    def _on_overrun(self, offset: int, count: int):
        if self.is_first_overrun:
            QMessageBox.warning(self, 'StreamingAI', 'BufferedAiOverrun')
            self.is_first_overrun = False

    def _on_cache_overflow(self, offset: int, count: int):
        QMessageBox.critical(self, 'StreamingAI', 'BufferedAiCacheOverflow')

    # ============================================================
    # 定时器
    # ============================================================
    def _on_timer1(self):
        """对应 Timer1Timer — 保存完成提示"""
        if self.csv_file is not None:
            try:
                self.csv_file.close()
            except Exception:
                pass
            self.csv_file = None
            QMessageBox.information(self, '提示', '保存数据成功!')
            self.progress.setValue(0)

    def _on_timer4(self):
        """对应 Timer4Timer — 启动后延时建数据文件"""
        try:
            now = datetime.now()
            date_str = now.strftime('%Y%m%d')
            base = os.path.join(self.exe_directory, 'data')
            i = 1
            s1 = '01'
            sub = f'{date_str}{s1}'
            full = os.path.join(base, sub)
            while os.path.exists(full):
                i += 1
                s1 = f'{i:02d}'
                sub = f'{date_str}{s1}'
                full = os.path.join(base, sub)
            os.makedirs(full, exist_ok=True)
            g.Filestrtemp = full
            self.test_no = f'{date_str}{s1}'
            g.TestNoTemp = self.test_no
            self.csv_path = os.path.join(full, f'{self.test_no}.CSV')

            self.csv_file = open(self.csv_path, 'w', encoding='utf-8')
            self.csv_file.write(f'应变通道: {self.cb_freq_ch.currentText()}\n')
            self.csv_file.write(f'采样频率: {self.daq.sample_rate}\n')
            self.csv_file.write(f'采集时长: {self.cb_save_time.currentText()}s\n')
            self.csv_file.write('时间 应变 转速\n')
            g.DataSaveFlag = True
            self.tick_count = time.time()
        except Exception as e:
            QMessageBox.warning(self, '保存出错', f'保存数据出错,程序退出:\n{e}')

    # ============================================================
    # 关闭窗口
    # ============================================================
    def closeEvent(self, event):
        try:
            if self.daq.is_running:
                self.daq.stop()
            self.daq.cleanup()
        except Exception:
            pass
        if self.csv_file is not None:
            try:
                self.csv_file.close()
            except Exception:
                pass
        event.accept()


# =============================================================================
# 程序入口
# =============================================================================
def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    win = MainForm()
    win.show()

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
