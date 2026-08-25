# -*- coding: utf-8 -*-
"""
vibration_tab.py
================
「振动信号分析」选项卡 — 复刻 Delphi 版振动采集/分析界面 (第三选项卡)。

职责:
    提供自包含 QWidget (VibrationTab), 嵌入主窗口 QTabWidget:
        - 顶部: 7 个数值指示器 (振幅/主频/相位/频幅/转速/当前间距/变形量)
        - 左栏: 采集参数 (采样率/分析点数/振动/转速通道/灵敏度)、频域滤波
                (带通下限/上限/口径切换)、记录与瀑布 (采样时间/步长/导出)、
                曲线颜色 (4 个拾色钮)、系统控制 (清零/开始/暂停/停止)
        - 右栏: ①FFT 频谱图 (幅值+相位双 Y 轴) ②振动峰峰值-时间趋势图
                (鼠标悬停回放) ③即时频谱图 (瀑布回放)
    功能对照 Delphi Main.pas 振动链路; 处理算法全部在 vibration_processor
    (纯计算), 本模块只做 UI 编排与文件生命周期。

    共享采集会话 (需求已确认): 本页开始/暂停/停止按钮直接调用 MainForm 的
    start_session('vib') / pause_session() / stop_session(); 数据帧由
    MainForm._on_data_ready 分发进 on_frame; 本页 CSV (6 列, 文件名
    <test_no>_vib.CSV) 与径向页 CSV 存于同一试验目录。

    本页有独立的间距清零基准 (_dis0), 与径向页 g.Dis0 互不影响 —— 本页
    间距/变形量按 Delphi 逻辑取"滤波后波形均值", 与径向页的原始均值口径
    不同, 分开清零避免互相干扰。

依赖:
    - PyQt5 / pyqtgraph / numpy
    - vibration_processor (process_frame / WaterfallCache / 灵敏度表)
    - csv_logger.CSVLogger (6 列模式)
    - public_para.g (DataSaveFlag / Savetime)

被调用方:
    - ui_builder.build_main_ui       → VibrationTab(form)
    - main_app.MainForm._on_data_ready → on_frame(data, ch_count, elapsed)
    - main_app.MainForm.start/pause/stop_session
        → begin_session / suspend / end_session / set_run_state
"""

import os
import datetime

import numpy as np
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QGroupBox, QLabel, QPushButton, QComboBox, QMessageBox,
    QFileDialog, QHBoxLayout, QVBoxLayout, QFormLayout,
)
import pyqtgraph as pg
import pyqtgraph.exporters  # noqa: F401  让 pg.exporters.ImageExporter 可用

from public_para import g
from csv_logger import CSVLogger
from vibration_processor import (
    process_frame, WaterfallCache,
    MASK_DELPHI, MASK_CORRECT,
    SENSITIVITY_OPTIONS, DEFAULT_SENSITIVITY_INDEX,
    transpara_from_sensitivity,
)

# 振动页 CSV 的 6 列表头 (Delphi: '时间 初始间距 当前间距 变形量 振幅 转速')
VIB_CSV_COLUMNS = '时间 初始间距 当前间距 变形量 振幅 转速'
# 共享会话下振动 CSV 的文件名后缀 (与径向 <test_no>.CSV 同目录共存)
VIB_CSV_SUFFIX = '_vib'

# 曲线默认颜色 (幅值谱 / 相位谱 / 趋势 / 即时谱)
_COLOR_MAG = '#1f77b4'
_COLOR_PHASE = '#d62728'
_COLOR_TREND = '#2ca02c'
_COLOR_INST = '#ff7f0e'


def _make_indicator(label_text: str, init_value: str):
    """创建带标题的居中数值指示器 (QGroupBox 包 QLabel)。

    与 ui_builder._make_indicator 同构 (本地副本, 避免 ui_builder ↔ 本模块
    的循环导入); 样式保持一致: 20px 加粗 #1f77b4 居中。

    Args:
        label_text: 分组标题;  init_value: 初始文本

    Returns:
        (QGroupBox, QLabel) 二元组

    Side Effects:
        无 (纯控件构造)
    """
    gb = QGroupBox(label_text)
    v = QVBoxLayout(gb)
    v.setContentsMargins(5, 5, 5, 5)
    lbl = QLabel(init_value)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet('font-size: 20px; font-weight: bold; color: #1f77b4;')
    v.addWidget(lbl)
    return (gb, lbl)


class VibrationTab(QWidget):
    """振动信号分析选项卡 (共享采集会话的第二条处理流水线)。

    维护的状态:
        - self.form:       MainForm 引用 (会话控制 + daq 参数读取)
        - self.csv:        本页 6 列 CSVLogger
        - self.cache:      WaterfallCache (瀑布谱)
        - self._dis0:      本页间距清零基准 (μm, 滤波后口径)
        - self._last_dis2: 最近一帧的当前间距 (清零按钮取值)
        - self._trend_t/_trend_v: 趋势曲线累积缓冲
        - self._run_state: 'idle' / 'running' / 'paused' (按钮使能依据)
    """

    def __init__(self, form, parent=None):
        """构造振动选项卡: 状态初始化 → 控件构建 → 信号绑定。

        Args:
            form:   MainForm 实例 (须提供 start_session/pause_session/
                    stop_session 与 .daq / .exe_directory)
            parent: 父控件

        Returns:
            无

        Side Effects:
            - 创建全部子控件并布局; 不接触硬件、不建任何文件
        """
        super().__init__(parent)
        self.form = form
        self.csv = CSVLogger()
        self.cache = WaterfallCache()
        self._dis0 = 0.0
        self._last_dis2 = 0.0
        self._trend_t = []
        self._trend_v = []
        self._run_state = 'idle'

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)

        # ==================== 顶部: 7 个指示器 ====================
        ind_row = QHBoxLayout()
        self.lbl_vib = _make_indicator('振幅(峰峰值) μm', '0')
        self.lbl_freq = _make_indicator('主频 Hz', '0')
        self.lbl_phase = _make_indicator('相位 °', '0')
        self.lbl_mag = _make_indicator('频幅 μm', '0')
        self.lbl_speed = _make_indicator('实时转速 RPM', '0')
        self.lbl_dis2 = _make_indicator('当前间距 μm', '0')
        self.lbl_def = _make_indicator('变形量 μm', '0')
        for w in (self.lbl_vib, self.lbl_freq, self.lbl_phase, self.lbl_mag,
                  self.lbl_speed, self.lbl_dis2, self.lbl_def):
            ind_row.addWidget(w[0])
        root.addLayout(ind_row)

        # ==================== 中部: 左参数栏 | 右绘图区 ====================
        mid = QHBoxLayout()
        mid.setSpacing(10)
        mid.addWidget(self._build_left_panel())
        mid.addWidget(self._build_plots(), 1)
        root.addLayout(mid, 1)

        self.set_run_state('idle')

    # ============================================================
    # UI 构建
    # ============================================================
    def _build_left_panel(self) -> QWidget:
        """构建左侧参数/控制栏 (采集参数 / 频域滤波 / 记录与瀑布 / 颜色 / 控制)。

        Returns:
            承载全部参数控件的 QWidget (固定宽度)

        Side Effects:
            - 在 self 上挂载全部下拉框/按钮并绑定信号
        """
        col = QVBoxLayout()
        col.setSpacing(8)

        # ---------- (a) 采集参数 (对应 Delphi ComboBox5/6/FreqCH/SpeedComboBOX/9) ----------
        grp_a = QGroupBox('采集参数')
        fa = QFormLayout(grp_a)
        self.cb_rate = QComboBox()
        self.cb_rate.addItems(['500', '1000', '2000', '2500', '5000', '10000',
                               '20000', '30000', '100000', '200000'])
        self.cb_rate.setCurrentText('10000')
        fa.addRow('采样频率(Hz):', self.cb_rate)

        self.cb_points = QComboBox()
        self.cb_points.addItems(['1024', '2048', '4096', '8192'])
        self.cb_points.setCurrentText('8192')
        fa.addRow('分析点数 N:', self.cb_points)

        self.cb_vib_ch = QComboBox()
        self.cb_vib_ch.addItems([f'通道{i}' for i in range(8)])
        self.cb_vib_ch.setCurrentIndex(0)
        fa.addRow('振动信号通道:', self.cb_vib_ch)

        self.cb_speed_ch = QComboBox()
        self.cb_speed_ch.addItems([f'通道{i}' for i in range(8)])
        self.cb_speed_ch.setCurrentIndex(7)
        fa.addRow('转速信号通道:', self.cb_speed_ch)

        self.cb_sens = QComboBox()
        self.cb_sens.addItems([t for t, _ in SENSITIVITY_OPTIONS])
        self.cb_sens.setCurrentIndex(DEFAULT_SENSITIVITY_INDEX)
        fa.addRow('传感器灵敏度:', self.cb_sens)
        col.addWidget(grp_a)

        # ---------- (b) 频域滤波 (对应 Delphi ComboBox7/10 + 口径开关) ----------
        grp_b = QGroupBox('频域滤波')
        fb = QFormLayout(grp_b)
        self.cb_low = QComboBox()
        self.cb_low.setEditable(True)
        self.cb_low.addItems(['0', '5', '10', '15', '20'])
        self.cb_low.setCurrentText('0')
        fb.addRow('带通下限(Hz):', self.cb_low)

        self.cb_high = QComboBox()
        self.cb_high.setEditable(True)
        self.cb_high.addItems(['20', '50', '100', '200', '300', '400', '500',
                               '1000', '10000', '20000', '50000'])
        self.cb_high.setCurrentText('100')
        fb.addRow('带通上限(Hz):', self.cb_high)

        self.cb_mask = QComboBox()
        self.cb_mask.addItems(['Delphi 口径(历史兼容)', '修正口径(物理真值)'])
        self.cb_mask.setToolTip(
            'Delphi 口径: 逐字复刻原程序清零判据(负频率镜像同被清零),\n'
            '带内振幅显示约为物理真实值的一半, 与历史 CSV 可直接对比;\n'
            '修正口径: 正负频率对称的规范带通, 振幅为物理真值(约为前者 2 倍)。')
        fb.addRow('滤波口径:', self.cb_mask)
        col.addWidget(grp_b)

        # ---------- (c) 记录与瀑布 (对应 Delphi ComboBox8 / bsSkinComboBox1 / 导出) ----------
        grp_c = QGroupBox('记录与瀑布谱')
        fc = QFormLayout(grp_c)
        self.cb_save_time = QComboBox()
        self.cb_save_time.setEditable(True)
        self.cb_save_time.addItems(['500', '800', '1000', '1200', '1500', '2000'])
        self.cb_save_time.setCurrentText('2000')
        self.cb_save_time.currentTextChanged.connect(self._on_savetime_change)
        fc.addRow('采样时间(s):', self.cb_save_time)

        self.cb_step = QComboBox()
        self.cb_step.addItems(['2', '3', '4', '5', '6'])
        self.cb_step.setCurrentText('2')
        fc.addRow('瀑布步长(s):', self.cb_step)

        self.btn_export_wf = QPushButton('导出瀑布谱 CSV')
        self.btn_export_wf.setToolTip('把各时刻幅值谱导出为分号分隔 CSV, 列头为该时刻实测转速')
        self.btn_export_wf.clicked.connect(self._on_export_waterfall)
        fc.addRow(self.btn_export_wf)
        col.addWidget(grp_c)

        # ---------- (d) 曲线颜色 (对应 Delphi 4 个颜色下拉) ----------
        grp_d = QGroupBox('曲线颜色')
        fd = QFormLayout(grp_d)
        self.color_btns = {}
        for key, label, color in (
                ('mag', '幅值谱:', _COLOR_MAG), ('phase', '相位谱:', _COLOR_PHASE),
                ('trend', '趋势线:', _COLOR_TREND), ('inst', '即时谱:', _COLOR_INST)):
            btn = pg.ColorButton(color=color)
            btn.sigColorChanged.connect(
                lambda b, k=key: self._on_color_changed(k, b.color()))
            self.color_btns[key] = btn
            fd.addRow(label, btn)
        col.addWidget(grp_d)

        # ---------- (e) 系统控制 (对应 Delphi Button1/BtnStart/Pause/Stop) ----------
        grp_e = QGroupBox('系统控制')
        fe = QVBoxLayout(grp_e)
        self.btn_zero = QPushButton('间距清零')
        self.btn_zero.setToolTip('把当前间距(滤波后均值)设为本页变形量的零点基准')
        self.btn_zero.clicked.connect(self._on_zero)
        fe.addWidget(self.btn_zero)

        self.btn_start = QPushButton('开始采集')
        self.btn_start.setStyleSheet(
            'background-color: #28a745; color: white; font-weight: bold; padding: 5px;')
        self.btn_start.clicked.connect(lambda: self.form.start_session('vib'))
        fe.addWidget(self.btn_start)

        self.btn_pause = QPushButton('暂停')
        self.btn_pause.setToolTip('挂起采集(文件保持打开); 再按"开始采集"将新建档案')
        self.btn_pause.clicked.connect(self.form.pause_session)
        fe.addWidget(self.btn_pause)

        self.btn_stop = QPushButton('停止采集')
        self.btn_stop.setStyleSheet(
            'background-color: #dc3545; color: white; font-weight: bold; padding: 5px;')
        self.btn_stop.clicked.connect(self.form.stop_session)
        fe.addWidget(self.btn_stop)
        col.addWidget(grp_e)

        col.addStretch(1)
        w = QWidget()
        w.setLayout(col)
        w.setFixedWidth(300)
        return w

    def _build_plots(self) -> QWidget:
        """构建右侧绘图区: 频谱(双Y) + 趋势(悬停回放) + 即时谱。

        Returns:
            承载三个绘图分组的 QWidget

        Side Effects:
            - 挂载 self.plot1/2/3 与各曲线; 绑定趋势图鼠标悬停信号
        """
        col = QVBoxLayout()
        col.setSpacing(8)

        # ---------- ① FFT 频谱 (幅值 + 相位 双 Y 轴, 对应 iPlot1) ----------
        grp1 = QGroupBox('FFT 频谱 (幅值 / 相位)')
        g1 = QVBoxLayout(grp1)
        self.plot1 = pg.PlotWidget()
        self.plot1.setLabel('left', '幅值', units='μm')
        self.plot1.setLabel('bottom', '频率', units='Hz')
        self.plot1.getAxis('left').enableAutoSIPrefix(False)
        self.plot1.getAxis('bottom').enableAutoSIPrefix(False)
        self.plot1.showGrid(x=True, y=True, alpha=0.3)
        self.curve_mag = self.plot1.plot([], [], pen=pg.mkPen(_COLOR_MAG, width=2))
        # 相位曲线放到独立 ViewBox, 挂右轴 (固定 ±200° 量程)
        p1 = self.plot1.plotItem
        p1.showAxis('right')
        p1.getAxis('right').setLabel('相位', units='°')
        p1.getAxis('right').enableAutoSIPrefix(False)
        self._vb_phase = pg.ViewBox()
        p1.scene().addItem(self._vb_phase)
        p1.getAxis('right').linkToView(self._vb_phase)
        self._vb_phase.setXLink(p1.vb)
        self._vb_phase.setYRange(-200.0, 200.0, padding=0)
        self.curve_phase = pg.PlotDataItem([], [], pen=pg.mkPen(_COLOR_PHASE, width=1))
        self._vb_phase.addItem(self.curve_phase)
        p1.vb.sigResized.connect(self._sync_phase_vb)
        g1.addWidget(self.plot1)
        col.addWidget(grp1, 3)

        # ---------- ②③ 趋势 + 即时谱 (对应 iPlot2 / iPlot3) ----------
        bottom = QHBoxLayout()
        bottom.setSpacing(8)

        grp2 = QGroupBox('振动峰峰值 - 时间 (悬停回放历史谱)')
        g2 = QVBoxLayout(grp2)
        self.plot2 = pg.PlotWidget()
        self.plot2.setLabel('left', '振幅', units='μm')
        self.plot2.setLabel('bottom', '时间', units='s')
        self.plot2.getAxis('left').enableAutoSIPrefix(False)
        self.plot2.showGrid(x=True, y=True, alpha=0.3)
        self.curve_trend = self.plot2.plot([], [], pen=pg.mkPen(_COLOR_TREND, width=2))
        self.plot2.scene().sigMouseMoved.connect(self._on_trend_hover)
        g2.addWidget(self.plot2)
        bottom.addWidget(grp2, 1)

        grp3 = QGroupBox('即时频谱 (瀑布回放)')
        g3 = QVBoxLayout(grp3)
        self.plot3 = pg.PlotWidget()
        self.plot3.setLabel('left', '幅值', units='μm')
        self.plot3.setLabel('bottom', '频率', units='Hz')
        self.plot3.getAxis('left').enableAutoSIPrefix(False)
        self.plot3.getAxis('bottom').enableAutoSIPrefix(False)
        self.plot3.showGrid(x=True, y=True, alpha=0.3)
        self.curve_inst = self.plot3.plot([], [], pen=pg.mkPen(_COLOR_INST, width=2))
        g3.addWidget(self.plot3)
        bottom.addWidget(grp3, 1)

        col.addLayout(bottom, 2)
        w = QWidget()
        w.setLayout(col)
        return w

    def _sync_phase_vb(self):
        """把相位 ViewBox 的几何区域同步到主 ViewBox (双 Y 轴联动的标准做法)。

        Side Effects:
            - setGeometry + linkedViewChanged (X 轴跟随主图缩放)
        """
        p1 = self.plot1.plotItem
        self._vb_phase.setGeometry(p1.vb.sceneBoundingRect())
        self._vb_phase.linkedViewChanged(p1.vb, self._vb_phase.XAxis)

    # ============================================================
    # 参数读取辅助
    # ============================================================
    def _stop_low(self) -> float:
        """读带通下限 (Hz); 文本非法回退 0。"""
        try:
            return float(self.cb_low.currentText())
        except ValueError:
            return 0.0

    def _stop_high(self) -> float:
        """读带通上限 (Hz); 文本非法回退 100。"""
        try:
            return float(self.cb_high.currentText())
        except ValueError:
            return 100.0

    def _transpara(self) -> float:
        """当前灵敏度档 → 电压-微米转换系数 (μm/V)。"""
        idx = self.cb_sens.currentIndex()
        if 0 <= idx < len(SENSITIVITY_OPTIONS):
            return transpara_from_sensitivity(SENSITIVITY_OPTIONS[idx][1])
        return 1800.0

    def _mask_mode(self) -> str:
        """当前滤波口径 (下拉索引 0=Delphi, 1=修正)。"""
        return MASK_CORRECT if self.cb_mask.currentIndex() == 1 else MASK_DELPHI

    def _steptime(self) -> int:
        """瀑布记录步长 (s), 文本非法回退 2。"""
        try:
            return max(1, int(self.cb_step.currentText()))
        except ValueError:
            return 2

    def preferred_rate(self) -> float:
        """本页发起会话时希望应用的采样率 (Hz)。"""
        try:
            return float(self.cb_rate.currentText())
        except ValueError:
            return 10000.0

    def preferred_interval(self) -> int:
        """本页发起会话时希望应用的分析点数 N (= interval_count)。"""
        try:
            return int(self.cb_points.currentText())
        except ValueError:
            return 8192

    def _on_savetime_change(self, t: str):
        """采样时间下拉变化 → 写 g.Savetime (与径向页控件同一全局, 后改者生效)。"""
        try:
            g.Savetime = float(t) if t else 2000.0
        except ValueError:
            pass

    def _on_color_changed(self, key: str, color):
        """拾色钮回调: 更新对应曲线画笔颜色。

        Args:
            key:   'mag' / 'phase' / 'trend' / 'inst'
            color: QColor
        """
        width = 2 if key != 'phase' else 1
        pen = pg.mkPen(color, width=width)
        {'mag': self.curve_mag, 'phase': self.curve_phase,
         'trend': self.curve_trend, 'inst': self.curve_inst}[key].setPen(pen)

    # ============================================================
    # 会话生命周期 (由 MainForm 调用)
    # ============================================================
    def reset_for_new_run(self):
        """清空三张图与趋势缓冲 (新会话开始时调用; 不动清零基准 _dis0)。

        Side Effects:
            - 清空曲线数据、趋势缓冲; 三图恢复自动量程
        """
        self._trend_t = []
        self._trend_v = []
        self.curve_mag.setData([], [])
        self.curve_phase.setData([], [])
        self.curve_trend.setData([], [])
        self.curve_inst.setData([], [])
        self.plot3.setTitle('')
        self.plot1.enableAutoRange()
        self.plot2.enableAutoRange()
        self.plot3.enableAutoRange()

    def begin_session(self, full_dir: str, test_no: str, sample_rate: float):
        """新会话开始: 复位图形/瀑布缓存, 打开本页 6 列 CSV。

        Args:
            full_dir:    本次试验数据目录 (与径向页共用)
            test_no:     试验编号 (文件名前缀)
            sample_rate: 会话实际采样率 (Hz)

        Returns:
            无

        Side Effects:
            - reset_for_new_run(); cache.reset(按会话 N 与步长)
            - 打开 <full_dir>/<test_no>_vib.CSV 并写 4 行头
              (头第 1 行 = 当前振动通道名, 对应 Delphi Writeln(F, FreqCH.text))
        """
        self.reset_for_new_run()
        n = int(self.form.daq.interval_count)
        n_bins = (n - 1) // 2 + 1
        d = sample_rate / n if n > 0 else 0.0
        self.cache.reset(n_bins, d, self._steptime())
        self.csv.open(
            full_dir, test_no, int(sample_rate), int(g.Savetime),
            channel_name=self.cb_vib_ch.currentText(),
            columns=VIB_CSV_COLUMNS, file_suffix=VIB_CSV_SUFFIX,
        )

    def end_session(self, export_png: bool = True) -> str:
        """会话结束: 关闭本页 CSV, (可选)导出频谱图 PNG 快照。

        Args:
            export_png: True 时导出 <test_no>_spec.png 到试验目录

        Returns:
            PNG 绝对路径 (成功导出时), 否则 ''

        Side Effects:
            - csv.close(); 失败的截图仅打印 [WARN], 不影响关闭流程
        """
        png = ''
        if export_png:
            try:
                if g.Filestrtemp and self.form.test_no:
                    exporter = pg.exporters.ImageExporter(self.plot1.plotItem)
                    png = os.path.join(
                        g.Filestrtemp, f'{self.form.test_no}_spec.png')
                    exporter.export(png)
            except Exception as ex:
                print(f'[WARN] 振动频谱截图保存失败: {ex}')
                png = ''
        self.csv.close()
        return png

    def close_csv(self):
        """静默关闭本页 CSV (暂停后直接开新会话时的旧档收尾; 幂等)。"""
        self.csv.close()

    def set_run_state(self, state: str):
        """按会话状态切换按钮/参数控件使能 (由 MainForm 统一驱动)。

        Args:
            state: 'idle' / 'running' / 'paused'

        Side Effects:
            - 开始: idle/paused 可按; 暂停: 仅 running; 停止: running/paused
            - 采样率 / 分析点数 / 灵敏度 / 步长在会话期间锁定
              (通道与带通上下限允许运行中切换, 与 Delphi 一致)
        """
        self._run_state = state
        running = state == 'running'
        paused = state == 'paused'
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running or paused)
        lock = running or paused
        for cb in (self.cb_rate, self.cb_points, self.cb_sens, self.cb_step):
            cb.setEnabled(not lock)

    # ============================================================
    # 数据帧处理 (由 MainForm._on_data_ready 分发)
    # ============================================================
    def on_frame(self, data, ch_count: int, elapsed: float):
        """处理一帧共享采集数据: 解算 → 刷新指示器/图 → 瀑布 → CSV。

        Args:
            data:     原始交错电压数组 (V, numpy), 布局 [pt0_ch0, pt0_ch1, ...]
            ch_count: 通道数
            elapsed:  距本次会话开始的秒数 (与径向页 CSV 同一时间基准)

        Returns:
            无

        Side Effects:
            - 更新 7 个指示器与三张图; cache.store 写瀑布槽
            - g.DataSaveFlag 且本页 CSV 打开时写一行 6 列数据
            - 异常吞掉仅打印 [VibTab ERR] (不中断径向页处理)
        """
        try:
            j = len(data) // ch_count
            if j <= 0:
                return
            vib_idx = min(self.cb_vib_ch.currentIndex(), ch_count - 1)
            speed_idx = min(self.cb_speed_ch.currentIndex(), ch_count - 1)
            vib_volts = data[vib_idx::ch_count][:j]
            speed_volts = data[speed_idx::ch_count][:j]

            res = process_frame(
                vib_volts, speed_volts,
                fs=float(self.form.daq.sample_rate),
                n_points=int(self.form.daq.interval_count),
                stop_low=self._stop_low(), stop_high=self._stop_high(),
                transpara=self._transpara(), mask_mode=self._mask_mode(),
            )
            if not res.ok:
                return

            self._last_dis2 = res.dis2
            deformation = res.dis2 - self._dis0

            # ---- 指示器 (取整/1 位小数与 Delphi 一致) ----
            self.lbl_vib[1].setText(f'{res.vib1:.0f}')
            self.lbl_freq[1].setText(f'{res.hz_max:.0f}')
            self.lbl_phase[1].setText(f'{res.phase_deg:.0f}')
            self.lbl_mag[1].setText(f'{res.hz_mag:.0f}')
            self.lbl_speed[1].setText(f'{res.realspeed:.1f}')
            self.lbl_dis2[1].setText(f'{res.dis2:.1f}')
            self.lbl_def[1].setText(f'{deformation:.1f}')

            # ---- 频谱 (幅值 + 相位) ----
            self.curve_mag.setData(res.xs, res.mags)
            self.curve_phase.setData(res.xs, res.phases)

            # ---- 趋势 (峰峰值-时间, 对应 iPlot2) ----
            self._trend_t.append(elapsed)
            self._trend_v.append(res.vib1)
            self.curve_trend.setData(self._trend_t, self._trend_v)

            # ---- 瀑布槽 (记录实测转速) ----
            self.cache.store(elapsed, res.mags_half, res.realspeed)

            # ---- CSV 落盘 (6 列, 与径向页共用 DataSaveFlag) ----
            if g.DataSaveFlag and self.csv.is_open:
                self.csv.write_row6(elapsed, self._dis0, res.dis2,
                                    deformation, res.vib1, res.realspeed)
        except Exception as e:
            print(f'[VibTab ERR] {e}')

    # ============================================================
    # 交互回调
    # ============================================================
    def _on_zero(self):
        """[间距清零]: 把本页最近一帧当前间距设为变形量零点 (仅影响本页)。

        Side Effects:
            - self._dis0 = self._last_dis2; 状态栏提示
        """
        self._dis0 = self._last_dis2
        self.form.statusBar().showMessage(
            f'振动页间距已清零: Dis0 = {self._dis0:.2f} μm')

    def _on_trend_hover(self, pos):
        """趋势图鼠标悬停: 回放该时刻的瀑布谱到即时频谱图 (对应 iPlot2→iPlot3)。

        Args:
            pos: 场景坐标 (sigMouseMoved 信号参数)

        Side Effects:
            - 命中有效时间槽时刷新 plot3 曲线与标题; 无数据时不动
        """
        if not self.cache.has_data:
            return
        # PlotWidget 是 QGraphicsView, 命中判定须用其 plotItem (QGraphicsItem)
        if not self.plot2.plotItem.sceneBoundingRect().contains(pos):
            return
        t = float(self.plot2.plotItem.vb.mapSceneToView(pos).x())
        idx = self.cache.slot_for_time(t)
        if idx is None:
            return
        rpm, row = self.cache.row(idx)
        if row is None:
            return
        d = self.cache.freq_step
        high = self._stop_high()
        n_bins = len(row)
        ks = np.arange(1, n_bins)
        fs_x = ks * d
        show = fs_x <= high
        self.curve_inst.setData(fs_x[show], row[1:][show])
        self.plot3.setTitle(
            f't = {idx * self.cache.steptime} s   转速 ≈ {rpm:.0f} RPM')

    def _on_export_waterfall(self):
        """[导出瀑布谱 CSV]: 选路径导出; 无数据时提示 (文案与 Delphi 一致)。

        Side Effects:
            - 有数据: QFileDialog 选保存路径 → cache.export_csv → 成功弹窗
            - 无数据: 弹 '暂无振动测量数据可以导出！'
        """
        if not self.cache.has_data or self.cache.recordnum < 1:
            QMessageBox.information(self, '提示', '暂无振动测量数据可以导出！')
            return
        default = os.path.join(
            self.form.exe_directory,
            '瀑布谱_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '.CSV')
        path, _ = QFileDialog.getSaveFileName(
            self, '导出瀑布谱数据', default, 'CSV 文件 (*.CSV *.csv)')
        if not path:
            return
        try:
            n = self.cache.export_csv(path, self._stop_high())
        except Exception as e:
            QMessageBox.critical(self, '导出失败', str(e))
            return
        QMessageBox.information(self, '提示', f'数据导出成功！(共 {n} 个时刻)')
