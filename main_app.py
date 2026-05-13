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
        - FFT → 频域带通滤波 → IFFT → 振动峰峰值
        - 频谱图(幅值+相位)、振动趋势图、瀑布图缓存
        - CSV 数据存盘
  5. Timer2 定时绘制"变形量 vs 转速²"XY 图
  6. 最小二乘线性拟合得到 Fxa/Fxb
  7. 鼠标在振动趋势上悬停时,显示对应时刻的频谱
  8. 导出瀑布图 CSV
  9. 转速修正子窗口

依赖: PyQt5, pyqtgraph, numpy
   pip install PyQt5 pyqtgraph numpy
"""

import os
import sys
import time
from datetime import datetime
from typing import Optional

import numpy as np

from PyQt5.QtCore import Qt, QTimer, QPointF, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QGroupBox,
    QLabel, QLineEdit, QPushButton, QComboBox, QProgressBar,
    QHBoxLayout, QVBoxLayout, QGridLayout, QFormLayout,
    QMessageBox, QFileDialog, QMenuBar, QAction, QStatusBar,
    QDialog, QDialogButtonBox, QSpinBox, QDoubleSpinBox,
)

import pyqtgraph as pg

# 项目模块
from public_para import g, cMinFloat, cMaxFloat
from complexs import ComplexMag, ComplexPhase, Complex, complex_mag_array, complex_phase_array
from ffts import fft_array, ifft_array
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

        # 二维瀑布图缓存(110 时间槽 × N 频点)
        self.data_sample_freq: Optional[np.ndarray] = None
        # FFT 输入/输出缓冲
        self.input1: Optional[np.ndarray] = None
        self.output1: Optional[np.ndarray] = None
        # 保存 FFT 结果(用于鼠标悬停展示)
        self.fxy: Optional[np.ndarray] = None

        # FFT 参数
        self.N: int = 8192            # FFT 点数 = IntervalCount
        self.Fs: int = 10000          # 采样率
        self.T: float = 1.0 / 10000
        self.D: float = 10000 / 8192  # 频率分辨率

        # 主峰频率/相位/幅值
        self.HzMax: float = 0.0
        self.Phase: float = 0.0
        self.HzMag: float = 0.0

        # 文件
        self.csv_file = None          # 数据 CSV 文件句柄
        self.csv_path = ''
        self.bmp_path = ''
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
        """主测试页签 — 5 个图表 + 实时数据显示"""
        layout = QVBoxLayout(self.tab_main)

        # 顶部数值显示行
        top = QHBoxLayout()
        self.lbl_freq = self._make_indicator('主频 Hz', '0')
        self.lbl_phase = self._make_indicator('相位 °', '0')
        self.lbl_mag = self._make_indicator('频率幅值 μm', '0')
        self.lbl_vib = self._make_indicator('峰峰值 μm', '0')
        self.lbl_speed = self._make_indicator('实时转速 RPM', '0')
        self.lbl_dis2 = self._make_indicator('当前位移 μm', '0')
        self.lbl_left_df = self._make_indicator('残余偏差', '0')
        for w in [self.lbl_freq, self.lbl_phase, self.lbl_mag,
                  self.lbl_vib, self.lbl_speed, self.lbl_dis2, self.lbl_left_df]:
            top.addWidget(w[0])   # 添加组合容器
        layout.addLayout(top)

        # 主体: 左右两栏
        body = QHBoxLayout()

        # ===== 左栏: 控制 + 频谱 =====
        left = QVBoxLayout()

        # 控制按钮
        btn_box = QGroupBox('采集控制')
        bg = QHBoxLayout(btn_box)
        self.btn_start = QPushButton('开始(&S)')
        self.btn_pause = QPushButton('暂停(&P)')
        self.btn_stop = QPushButton('停止(&T)')
        self.btn_save = QPushButton('手动保存数据')
        self.btn_zero = QPushButton('归零(&0)')
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._btn_start_click)
        self.btn_pause.clicked.connect(self._btn_pause_click)
        self.btn_stop.clicked.connect(self._btn_stop_click)
        self.btn_save.clicked.connect(self._btn_save_click)
        self.btn_zero.clicked.connect(self._btn_zero_click)
        bg.addWidget(self.btn_start)
        bg.addWidget(self.btn_pause)
        bg.addWidget(self.btn_stop)
        bg.addWidget(self.btn_save)
        bg.addWidget(self.btn_zero)
        left.addWidget(btn_box)

        # 频谱图(双 Y 轴: 幅值 + 相位)
        grp_spec = QGroupBox('FFT 频谱(幅值 / 相位)')
        gs = QVBoxLayout(grp_spec)
        self.plot1 = pg.PlotWidget()
        self.plot1.setLabel('left', '幅值', units='μm')
        self.plot1.setLabel('bottom', '频率', units='Hz')
        self.plot1.showGrid(x=True, y=True, alpha=0.3)
        self.curve_mag = self.plot1.plot([], [], pen=pg.mkPen('#1f77b4', width=2), name='幅值')
        # 右 Y 轴 (相位)
        self.plot1_p2 = pg.ViewBox()
        self.plot1.scene().addItem(self.plot1_p2)
        self.plot1.getAxis('right').linkToView(self.plot1_p2)
        self.plot1_p2.setXLink(self.plot1)
        self.plot1.showAxis('right')
        self.plot1.getAxis('right').setLabel('相位', units='°')
        self.curve_phase = pg.PlotCurveItem([], [], pen=pg.mkPen('#d62728', width=1, style=Qt.DashLine))
        self.plot1_p2.addItem(self.curve_phase)
        self.plot1.getViewBox().sigResized.connect(self._update_p2_geometry)
        gs.addWidget(self.plot1)
        left.addWidget(grp_spec, 1)

        body.addLayout(left, 1)

        # ===== 右栏: 振动趋势 + 瀑布即时谱 + XY 变形 =====
        right = QVBoxLayout()

        # 振动幅值随时间趋势 (iPlot2)
        grp_trend = QGroupBox('振动峰峰值 vs 时间')
        gt = QVBoxLayout(grp_trend)
        self.plot2 = pg.PlotWidget()
        self.plot2.setLabel('left', '振动幅值', units='μm')
        self.plot2.setLabel('bottom', '时间', units='s')
        self.plot2.showGrid(x=True, y=True, alpha=0.3)
        self.curve_trend = self.plot2.plot([], [], pen=pg.mkPen('#2ca02c', width=2))
        # 鼠标悬停取频谱
        self.plot2.scene().sigMouseMoved.connect(self._on_plot2_hover)
        gt.addWidget(self.plot2)
        right.addWidget(grp_trend, 1)

        # 即时频谱(瀑布图回放) (iPlot3)
        grp_inst = QGroupBox('即时频谱(鼠标悬停在上图取回放)')
        gi = QVBoxLayout(grp_inst)
        self.plot3 = pg.PlotWidget()
        self.plot3.setLabel('left', '幅值', units='μm')
        self.plot3.setLabel('bottom', '频率', units='Hz')
        self.plot3.showGrid(x=True, y=True, alpha=0.3)
        self.curve_inst = self.plot3.plot([], [], pen=pg.mkPen('#ff7f0e', width=2))
        gi.addWidget(self.plot3)
        right.addWidget(grp_inst, 1)

        # XY 变形 - 转速²拟合图 (iXYPlot1)
        grp_xy = QGroupBox('变形量 vs 转速²/10⁶ (线性拟合预应力)')
        gx = QVBoxLayout(grp_xy)
        self.plotxy = pg.PlotWidget()
        self.plotxy.setLabel('left', '变形量', units='μm')
        self.plotxy.setLabel('bottom', '转速²/10⁶', units='RPM²')
        self.plotxy.showGrid(x=True, y=True, alpha=0.3)
        self.scatter_xy = pg.ScatterPlotItem(size=4, brush=pg.mkBrush('#9467bd'), pen=None)
        self.plotxy.addItem(self.scatter_xy)
        self.curve_fit = self.plotxy.plot([], [], pen=pg.mkPen('r', width=2))
        gx.addWidget(self.plotxy)

        # 拟合参数 + 拟合按钮
        fit_row = QHBoxLayout()
        fit_row.addWidget(QLabel('Rpm1:'))
        self.edit_rpm1 = QLineEdit('40')
        self.edit_rpm1.setMaximumWidth(80)
        self.edit_rpm1.textChanged.connect(self._on_edit3_change)
        fit_row.addWidget(self.edit_rpm1)
        fit_row.addWidget(QLabel('Rpm2:'))
        self.edit_rpm2 = QLineEdit('60')
        self.edit_rpm2.setMaximumWidth(80)
        self.edit_rpm2.textChanged.connect(self._on_edit4_change)
        fit_row.addWidget(self.edit_rpm2)
        fit_row.addWidget(QLabel('Fxa:'))
        self.edit_fxa = QLineEdit('10')
        self.edit_fxa.setMaximumWidth(80)
        self.edit_fxa.textChanged.connect(self._on_edit1_change)
        fit_row.addWidget(self.edit_fxa)
        fit_row.addWidget(QLabel('Fxb:'))
        self.edit_fxb = QLineEdit('0')
        self.edit_fxb.setMaximumWidth(80)
        self.edit_fxb.textChanged.connect(self._on_edit2_change)
        fit_row.addWidget(self.edit_fxb)
        self.btn_fit = QPushButton('自动拟合')
        self.btn_fit.clicked.connect(self._btn_fit_click)
        fit_row.addWidget(self.btn_fit)
        self.btn_redraw_fit = QPushButton('重绘拟合线')
        self.btn_redraw_fit.clicked.connect(self._btn_redraw_fit_click)
        fit_row.addWidget(self.btn_redraw_fit)
        gx.addLayout(fit_row)

        # 进度条 + 导出按钮
        bottom_row = QHBoxLayout()
        bottom_row.addWidget(QLabel('保存进度:'))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        bottom_row.addWidget(self.progress)
        self.btn_export_waterfall = QPushButton('导出瀑布图CSV')
        self.btn_export_waterfall.clicked.connect(self._btn_export_waterfall_click)
        bottom_row.addWidget(self.btn_export_waterfall)
        gx.addLayout(bottom_row)

        right.addWidget(grp_xy, 1)

        body.addLayout(right, 1)

        layout.addLayout(body, 1)

        # ---------- 启动用的子定时器 ----------
        # Timer2: 每 200ms 绘制 XY 变形点(对应原 Timer2)
        self.timer2 = QTimer(self)
        self.timer2.setInterval(200)
        self.timer2.timeout.connect(self._on_timer2)

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

        # 振动通道(FreqCH) + 转速通道
        self.cb_freq_ch = QComboBox()
        self.cb_freq_ch.addItems([f'通道{i}' for i in range(16)])
        self.cb_freq_ch.setCurrentIndex(0)
        f.addRow('振动信号通道:', self.cb_freq_ch)

        self.cb_speed_ch = QComboBox()
        self.cb_speed_ch.addItems([f'通道{i}' for i in range(16)])
        self.cb_speed_ch.setCurrentIndex(7)
        f.addRow('转速信号通道:', self.cb_speed_ch)

        outer.addWidget(grp_acq)

        # ===== 信号处理参数 =====
        grp_sig = QGroupBox('信号处理')
        f2 = QFormLayout(grp_sig)
        self.cb_low_stop = QComboBox()
        self.cb_low_stop.setEditable(True)
        self.cb_low_stop.addItems(['1', '2', '5', '10', '20'])
        self.cb_low_stop.setCurrentText('2')
        self.cb_low_stop.currentTextChanged.connect(self._on_cb7_change)
        f2.addRow('低频截止 (Hz):', self.cb_low_stop)

        self.cb_high_stop = QComboBox()
        self.cb_high_stop.setEditable(True)
        self.cb_high_stop.addItems(['200', '500', '1000', '2000', '5000'])
        self.cb_high_stop.setCurrentText('1000')
        self.cb_high_stop.currentTextChanged.connect(self._on_cb10_change)
        f2.addRow('高频截止 (Hz):', self.cb_high_stop)

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

        outer.addWidget(grp_sig)

        # ===== 保存参数 =====
        grp_save = QGroupBox('保存设置')
        f3 = QFormLayout(grp_save)
        self.cb_save_time = QComboBox()
        self.cb_save_time.setEditable(True)
        self.cb_save_time.addItems(['10', '30', '60', '120', '300', '600'])
        self.cb_save_time.setCurrentText('60')
        self.cb_save_time.currentTextChanged.connect(self._on_cb8_change)
        f3.addRow('采集时长 (秒):', self.cb_save_time)

        self.cb_step = QComboBox()
        self.cb_step.addItems(['2', '3', '4', '5'])
        self.cb_step.setCurrentIndex(0)
        self.cb_step.currentIndexChanged.connect(self._on_step_change)
        f3.addRow('瀑布步长 (秒):', self.cb_step)

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

    def _update_p2_geometry(self):
        """同步频谱右 Y 轴(相位)的几何位置"""
        self.plot1_p2.setGeometry(self.plot1.getViewBox().sceneBoundingRect())
        self.plot1_p2.linkedViewChanged(self.plot1.getViewBox(), self.plot1_p2.XAxis)

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
            g.Vib1 = g.Vib2 = g.Vib3 = g.Vib4 = 0.0
            g.Vib5 = g.Vib6 = g.Vib7 = g.Vib8 = 0.0
            g.Rpm1 = 40.0
            g.Rpm2 = 60.0
            g.Savetime = float(self.cb_save_time.currentText() or 60)
            g.Deformation0 = 0.0
            g.Dis0 = 0.0
            g.Dis1 = 0.0
            g.Fxa = 10.0
            g.Fxb = 0.0
            for i in range(20):
                g.SumS2[i] = 1.0
                g.SumS1[i] = 0.0
                g.SumD2[i] = 10.0
                g.SumD1[i] = 0.0
            g.Sensitivity1 = 1.0 / 1.8
            g.steptime = 2
            g.TransPara1 = 1000.0 / g.Sensitivity1

            # 默认带阻
            g.StopFreqLow = int(self.cb_low_stop.currentText() or 2)
            g.StopFreqHigh = int(self.cb_high_stop.currentText() or 1000)

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
            # 清空 XY 图
            self.scatter_xy.clear()
            self.curve_fit.setData([], [])
            self.plotxy.setXRange(0, 130)
            self.plotxy.setYRange(0, 3000)

            g.SumCount1 = 0
            g.SumCount2 = 0
            g.DataSaveFlag = False

            self.is_first_overrun = True

            # 按 IntervalCount 重新设缓冲大小
            ic = self.daq.interval_count
            self.data_arrays = [np.zeros(ic) for _ in range(8)]

            # 初始化 FFT 参数
            self.N = self.daq.interval_count
            self.Fs = int(self.daq.sample_rate)
            self.T = 1.0 / self.Fs
            self.D = 1.0 / (self.N * self.T)

            # 初始化瀑布图缓存
            self.data_sample_freq = np.zeros((110, self.N))
            g.Freqenable = True

            # 拟合系数复位
            g.Fxa = 10.0
            g.Fxb = 0.0

            # 启动定时器
            self.timer2.start()

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

            # 保存 XY 图截图
            try:
                exporter = pg.exporters.ImageExporter(self.plotxy.plotItem)
                if g.Filestrtemp and g.TestNoTemp:
                    bmp = os.path.join(g.Filestrtemp, f'{g.TestNoTemp}.png')
                    exporter.export(bmp)
                    self.bmp_path = bmp
            except Exception as ex:
                print(f'[WARN] 截图保存失败: {ex}')

            self.daq.stop()

            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_stop.setEnabled(False)

            # 清理缓冲
            for i in range(8):
                self.data_arrays[i] = np.zeros(0)

            self.timer2.stop()
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
            self.csv_file.write(f'{self.cb_freq_ch.currentText()}\n')
            self.csv_file.write(f'采样频率: {self.daq.sample_rate}\n')
            self.csv_file.write(f'采集时长: {self.cb_save_time.currentText()}s\n')
            self.csv_file.write('时间 初始位移 当前位移 变形量 振动幅值 转速\n')
            g.DataSaveFlag = True
            self.tick_count = time.time()
            self.statusBar().showMessage(f'保存中: {self.csv_path}')
        except Exception as e:
            QMessageBox.critical(self, '保存失败', str(e))

    def _btn_zero_click(self):
        """对应 Button1Click — 把当前位移定为零点"""
        try:
            g.Dis0 = g.Dis2
            self.btn_zero.setEnabled(False)
            self.statusBar().showMessage(f'已归零, Dis0 = {g.Dis0:.2f}')
        except Exception:
            QMessageBox.warning(self, '失败', '归零失败,请重启动后重试')

    def _btn_fit_click(self):
        """对应 Button2Click — 对 20 个采样点做最小二乘拟合"""
        try:
            y1 = sum(g.SumD1[:20]) / 20.0
            y2 = sum(g.SumD2[:20]) / 20.0
            x1 = sum(g.SumS1[:20]) / 20.0
            x2 = sum(g.SumS2[:20]) / 20.0
            if abs(x2 - x1) < 1e-9:
                QMessageBox.warning(self, '拟合失败', '两个参考点过于接近,无法拟合')
                return
            g.Fxa = (y2 - y1) / (x2 - x1)
            g.Fxb = y1 - g.Fxa * x1
            self.edit_fxa.setText(f'{g.Fxa:.1f}')
            self.edit_fxb.setText(f'{g.Fxb:.1f}')
            self._draw_fit_line()
        except Exception as e:
            QMessageBox.warning(self, '拟合失败', str(e))

    def _btn_redraw_fit_click(self):
        """对应 Button3Click"""
        self._draw_fit_line()

    def _draw_fit_line(self):
        x_max = self.plotxy.viewRange()[0][1]
        if x_max <= 0:
            x_max = 130
        xs = np.array([0, x_max])
        ys = g.Fxa * xs + g.Fxb
        self.curve_fit.setData(xs, ys)

    def _btn_export_waterfall_click(self):
        """对应 bsSkinButton1Click — 导出瀑布图 CSV"""
        if not g.Freqenable or self.data_sample_freq is None:
            QMessageBox.information(self, '提示', '先启动振动采集才有数据可导出')
            return
        path, _ = QFileDialog.getSaveFileName(
            self, '导出瀑布图数据', '', 'CSV Files (*.CSV)'
        )
        if not path:
            return
        if not path.lower().endswith('.csv'):
            path += '.CSV'
        try:
            with open(path, 'w', encoding='utf-8') as f:
                # 表头(转速对应时间槽)
                f.write('频率Hz\\转速rpm;')
                step_idx = self.cb_step.currentIndex() + 1   # 与原代码逻辑一致
                for j in range(1, g.recordnum + 1):
                    temp = int(step_idx * 100 * j)
                    f.write(f'{temp};')
                f.write('\n')

                # 数据
                half = (self.N - 1) // 2
                for i in range(1, half):
                    freq = i * self.D
                    if freq > g.StopFreqHigh:
                        break
                    f.write(f'{freq:.2f};')
                    for j in range(1, g.recordnum + 1):
                        f.write(f'{self.data_sample_freq[j, i]:.2f};')
                    f.write('\n')
            QMessageBox.information(self, '成功', '数据导出成功!')
        except Exception as e:
            QMessageBox.warning(self, '失败', str(e))

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

    def _on_cb7_change(self, txt):
        try:
            g.StopFreqLow = int(float(txt))
        except ValueError:
            pass

    def _on_cb10_change(self, txt):
        try:
            g.StopFreqHigh = int(float(txt))
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

    def _on_step_change(self, idx):
        g.steptime = idx + 2

    def _on_edit1_change(self, txt):
        try:
            g.Fxa = float(txt)
        except ValueError:
            pass

    def _on_edit2_change(self, txt):
        try:
            g.Fxb = float(txt)
        except ValueError:
            pass

    def _on_edit3_change(self, txt):
        try:
            g.Rpm1 = float(txt)
        except ValueError:
            pass

    def _on_edit4_change(self, txt):
        try:
            g.Rpm2 = float(txt)
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

            # 振动通道最小值、转速通道求和
            sum_min = float(np.min(self.data_arrays[0]))
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

            self.lbl_speed[1].setText(f'{g.Realspeed:.1f}')
            g.Dis1 = -sum_min

            # ---- 5) FFT 与频域处理 ----
            N = self.N
            if len(self.data_arrays[0]) < N:
                # 数据不足时跳过(早期数据可能不满 N)
                return

            input1 = self.data_arrays[0][:N].astype(np.complex128)
            output1 = fft_array(input1)

            # 频域带阻
            freqs = np.arange(N) * self.D
            mask_block = (freqs <= (g.StopFreqLow - 1)) | (freqs >= (g.StopFreqHigh - 1))
            output1[mask_block] = 0

            # 反 FFT
            recovered = ifft_array(output1).real

            # ---- 6) 振动统计 ----
            vib1_max = float(np.max(recovered))
            vib1_min = float(np.min(recovered))
            g.Vib1 = vib1_max - vib1_min
            g.Dis2 = -float(np.sum(recovered)) / N

            self.lbl_vib[1].setText(f'{g.Vib1:.0f}')
            self.lbl_dis2[1].setText(f'{g.Dis2:.1f}')

            # ---- 7) 频谱图(幅值 + 相位) ----
            self.fxy = output1.copy()
            half = (N - 1) // 2

            # 只保留 0 < f <= StopFreqHigh 的部分
            ks = np.arange(1, half + 1)
            fs_x = ks * self.D
            mask_show = fs_x <= g.StopFreqHigh
            xs = fs_x[mask_show]
            mags = np.abs(self.fxy[1:half+1][mask_show]) * 2.0 / N
            phs = np.angle(self.fxy[1:half+1][mask_show]) * 180.0 / np.pi

            self.curve_mag.setData(xs, mags)
            self.curve_phase.setData(xs, phs)

            # 主峰(对应 ilabel11/8/14)
            if len(mags) > 0:
                peak_idx = int(np.argmax(mags))
                self.HzMax = float(xs[peak_idx])
                self.HzMag = float(mags[peak_idx])
                self.Phase = float(phs[peak_idx])
            else:
                self.HzMax = 0
                self.HzMag = 0
                self.Phase = 0

            self.lbl_freq[1].setText(f'{self.HzMax:.0f}')
            self.lbl_phase[1].setText(f'{self.Phase:.0f}')
            self.lbl_mag[1].setText(f'{self.HzMag:.0f}')

            # ---- 8) 振动趋势曲线 ----
            t_now = time.time() - self.tick_count1
            # 用历史 X-Y 列表持续累积
            xs_t, ys_t = self.curve_trend.getData()
            if xs_t is None:
                xs_t = np.array([])
                ys_t = np.array([])
            xs_t = np.append(xs_t, t_now)
            ys_t = np.append(ys_t, g.Vib1)
            self.curve_trend.setData(xs_t, ys_t)

            # 瀑布图缓存
            if g.steptime > 0:
                idx = int(t_now) // g.steptime
                if idx < self.data_sample_freq.shape[0]:
                    full_mag = np.abs(self.fxy) * 2.0 / N
                    self.data_sample_freq[idx, :] = full_mag
                    g.freqarr = idx
                    g.recordnum = idx

            # ---- 9) 数据落盘 ----
            if g.DataSaveFlag and self.csv_file is not None:
                elapsed = time.time() - self.tick_count
                self.csv_file.write(
                    f'{elapsed:.4f} {g.Dis0:.4f} {g.Dis2:.4f} '
                    f'{(g.Dis2 - g.Dis0):.4f} {g.Vib1:.4f} {g.Realspeed:.4f}\n'
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
    def _on_timer2(self):
        """对应 Timer2Timer — 周期性绘制 XY 变形点"""
        try:
            deformation = g.Dis0 - g.Dis2
            temps = g.Realspeed ** 2 * 1e-6

            # 加点到散点
            current = self.scatter_xy.data
            self.scatter_xy.addPoints(x=[temps], y=[deformation])

            # 收集拟合采样点
            if temps > g.Rpm1:
                if g.SumCount1 < 20:
                    g.SumD1[g.SumCount1] = deformation
                    g.SumS1[g.SumCount1] = temps
                    g.SumCount1 += 1
            if temps > g.Rpm2:
                if g.SumCount2 < 20:
                    g.SumD2[g.SumCount2] = deformation
                    g.SumS2[g.SumCount2] = temps
                    g.SumCount2 += 1

            # 残余偏差
            g.LeftDF = deformation - g.Fxa * temps - g.Fxb
            self.lbl_left_df[1].setText(f'{g.LeftDF:.1f}')

            if g.Savetime > 0:
                ratio = abs(g.LeftDF) * self.progress.maximum() / g.Savetime
                self.progress.setValue(min(int(ratio), self.progress.maximum()))

            # 动态坐标
            vb = self.plotxy.getViewBox()
            (xmin, xmax), (ymin, ymax) = vb.viewRange()
            if temps * 1.1 > xmax:
                self.plotxy.setXRange(0, temps * 1.2)
            if deformation > ymax:
                self.plotxy.setYRange(0, deformation * 1.2)
        except Exception as e:
            print(f'[Timer2 ERR] {e}')

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
            self.csv_file.write(f'{self.cb_freq_ch.currentText()}\n')
            self.csv_file.write(f'采样频率: {self.daq.sample_rate}\n')
            self.csv_file.write(f'采集时长: {self.cb_save_time.currentText()}s\n')
            self.csv_file.write('时间 初始位移 当前位移 变形量 振动幅值 转速\n')
            g.DataSaveFlag = True
            self.tick_count = time.time()
        except Exception as e:
            QMessageBox.warning(self, '保存出错', f'保存数据出错,程序退出:\n{e}')

    # ============================================================
    # 鼠标在振动趋势曲线上悬停 -> 显示对应时刻的频谱
    # ============================================================
    def _on_plot2_hover(self, evt):
        """对应 iPlot2GetMouseCursorDataCursor"""
        if not g.Freqenable or self.data_sample_freq is None:
            return
        if not self.plot2.sceneBoundingRect().contains(evt):
            return
        mouse_point = self.plot2.plotItem.vb.mapSceneToView(evt)
        tx = mouse_point.x()
        if tx < 0 or g.steptime <= 0:
            return
        ix = int(round(tx)) // g.steptime
        if 0 <= ix <= 100 and ix < self.data_sample_freq.shape[0]:
            half = (self.N - 1) // 2
            ks = np.arange(1, half)
            fs_x = ks * self.D
            mask = fs_x < g.StopFreqHigh
            xs = fs_x[mask]
            ys = self.data_sample_freq[ix, 1:half][mask]
            self.curve_inst.setData(xs, ys)

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
