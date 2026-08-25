"""
ui_builder.py
=============
主窗口 GUI 布局构建模块 (选项卡式)。

职责:
    提供唯一入口函数 build_main_ui(form), 在传入的 form (MainForm 实例) 上构建:
        - 全局浅色蓝调样式 (APP_QSS)
        - 菜单栏 (设置 / 帮助)
        - 一个 QTabWidget, 含三个功能选项卡:
            * 「径向位移测量」: 原实时采集界面 (4 指示器 + 散点图 + 3 控制分组)
            * 「振动信号分析」: vibration_tab.VibrationTab (复刻 Delphi 振动链路,
              与径向页共享同一采集会话)
            * 「DIC 应变分析」: dic_tab.DicAnalysisTab (离线全场最大值分析, 内联展示)
        - 2 个定时器 (timer1 100ms 单次, timer2 200ms 周期)

    实时测量相关的全部控件仍挂到 form.<name> (名称与历史版本一致), 因此 MainForm
    中的事件回调无需改动; 仅承载它们的容器从"中央窗口"变成"测量选项卡页"。

依赖:
    - PyQt5
    - pyqtgraph
    - public_para.g   (lambda 中读写 Savetime)
    - dic_tab.DicAnalysisTab

被调用方:
    - main_app.MainForm.__init__ → build_main_ui(self)

关键约束 (与历史版本一致):
    - 散点 size=4 / brush='#9467bd' / pen=None; 拟合线 'r' width=2
    - 灵敏度下拉默认选 0 (LK-H080); 位移信源默认通道 0, 转速信源默认通道 7
    - 采样频率下拉默认 10000; 采集时长下拉默认 2000
    - 目标变形默认 100, 采集时长默认 2000
    - timer2 间隔 200ms, timer1 单次 100ms
"""

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QWidget, QGroupBox, QLabel, QLineEdit, QPushButton, QComboBox, QProgressBar,
    QHBoxLayout, QVBoxLayout, QFormLayout, QAction,
    QTabWidget,
)
import pyqtgraph as pg

from public_para import g
from dic_tab import DicAnalysisTab
from vibration_tab import VibrationTab


# 全局样式 (浅色 + 蓝色主色, 参考疲劳寿命预测程序的现代观感)
APP_QSS = """
QMainWindow { background: #eef1f6; }

QTabWidget::pane { border: 1px solid #d6dce5; background: #ffffff; top: -1px; }
QTabBar::tab {
    background: #e4eaf2; color: #4a5568;
    padding: 8px 22px; margin-right: 2px;
    border: 1px solid #d6dce5; border-bottom: none;
    border-top-left-radius: 5px; border-top-right-radius: 5px;
}
QTabBar::tab:selected { background: #ffffff; color: #1d4e89; font-weight: bold; }
QTabBar::tab:hover { background: #f0f4f9; }

QGroupBox {
    background: #ffffff; border: 1px solid #e1e6ee;
    border-radius: 6px; margin-top: 12px; padding: 8px;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 5px;
    color: #1d4e89; font-weight: bold;
}
QPushButton {
    background: #1d4e89; color: #ffffff; border: none;
    padding: 7px 14px; border-radius: 4px;
}
QPushButton:hover { background: #245ea6; }
QPushButton:pressed { background: #173f70; }
QPushButton:disabled { background: #aab6c6; }

QProgressBar {
    border: 1px solid #d6dce5; border-radius: 4px;
    text-align: center; background: #eef2f7;
}
QProgressBar::chunk { background: #1d4e89; border-radius: 3px; }

QHeaderView::section {
    background: #eef2f7; color: #34465c; padding: 5px;
    border: none; border-right: 1px solid #d9e0ea; border-bottom: 1px solid #d9e0ea;
    font-weight: bold;
}
QTableWidget { gridline-color: #e8ecf2; background: #ffffff; }
QComboBox, QLineEdit {
    border: 1px solid #cfd7e3; border-radius: 4px; padding: 4px 6px; background: #ffffff;
}
"""


def _make_indicator(label_text: str, init_value: str):
    """创建一个带标题的居中显示数值指示器 (QGroupBox 包 QLabel)。

    Args:
        label_text: 分组标题 (如 '实时转速 RPM')
        init_value: 初始显示的字符串 (如 '0')

    Returns:
        (QGroupBox, QLabel) 二元组: 外框用于布局, 内 Label 用于后续 setText 更新

    Side Effects:
        无 (纯控件构造)

    行为不变量 (与历史版本一致):
        - 内边距 (5, 5, 5, 5)
        - Label 居中对齐, 字号 20px 加粗, 颜色 #1f77b4
    """
    gb = QGroupBox(label_text)
    v = QVBoxLayout(gb)
    v.setContentsMargins(5, 5, 5, 5)
    lbl = QLabel(init_value)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet('font-size: 20px; font-weight: bold; color: #1f77b4;')
    v.addWidget(lbl)
    return (gb, lbl)


def _make_safe_float_setter(attr: str, default: float):
    """生成一个把文本安全转换为浮点并写入 g.<attr> 的闭包。

    用于绑定到 QLineEdit.textChanged 或可编辑 QComboBox.currentTextChanged,
    替代原版的 lambda, 防止用户输入非法字符 (如 '40a'、孤立的 '-'、'1e' 等
    中间态) 时 float() 抛 ValueError 把控制台刷红、并造成 g 字段静默不更新。

    Args:
        attr:    g 上的字段名 (如 'Savetime')
        default: 文本为空字符串时的回退值; 与原 lambda 中的默认值保持一致

    Returns:
        setter(t: str) 函数, 用于 .connect() 绑定

    Side Effects:
        - 文本合法 (能 float() 解析) 时: 修改 g.<attr>
        - 文本为空: 把 g.<attr> 设为 default
        - 文本非法 (float() 抛 ValueError) 时: 静默不动 g, 等用户继续输入
        - 不弹窗、不写日志, 避免在每次按键时打扰用户
    """
    def setter(t: str):
        try:
            setattr(g, attr, float(t) if t else default)
        except ValueError:
            pass
    return setter


def _build_measure_tab(form) -> QWidget:
    """构建「径向位移测量」选项卡页 (原实时采集界面)。

    Args:
        form: MainForm 实例; 控件挂到 form.<name>, 信号连到 form 的方法

    Returns:
        承载测量界面的 QWidget (供 QTabWidget.addTab)

    Side Effects:
        在 form 上挂载: lbl_speed/lbl_dis2/lbl_def/lbl_rate, plotxy/scatter_xy/fit_curve,
        cb_sens/cb_dis_ch/cb_speed_ch/cb_rate, edit_target/cb_save_time,
        btn_fit/lbl_fit_result, btn_zero/btn_start/btn_stop/btn_refresh/progress。
        并绑定相应信号槽。

    布局:
        顶部 4 个指标横跨整宽; 其下左右中分 ——
        左半栏竖排三个功能区 (灵敏度及采样参数 / 变形拟合及目标设定 / 系统控制),
        右半栏为散点图。仅为布局排布, 控件对象/名称/信号连接与历史版本一致,
        不影响 main_app 的事件处理。
    """
    page = QWidget()
    layout = QVBoxLayout(page)

    # ====== 顶部: 4 个数值指示器, 横跨整宽 ======
    ind_row = QHBoxLayout()
    form.lbl_speed = _make_indicator('实时转速 RPM', '0')
    form.lbl_dis2 = _make_indicator('当前间距 μm', '0')
    form.lbl_def = _make_indicator('当前变形量 μm', '0')
    form.lbl_rate = _make_indicator('目标完成率 %', '0')
    for w in [form.lbl_speed, form.lbl_dis2, form.lbl_def, form.lbl_rate]:
        ind_row.addWidget(w[0])
    layout.addLayout(ind_row)

    # ====== 下部: 左右中分 (左=三个控制组竖排, 右=散点图) ======
    mid = QHBoxLayout()

    # ---- 左半栏: 三个功能区竖排 ----
    left_col = QVBoxLayout()

    # ---------- (a) 灵敏度及采样参数 ----------
    grp_a = QGroupBox('灵敏度及采样参数')
    lay_a = QFormLayout(grp_a)

    form.cb_sens = QComboBox()
    form.cb_sens.addItems(['激光位移 LK-H080 (1mV/1.8μm)', '通用 1.0'])
    form.cb_sens.currentIndexChanged.connect(form._on_sens_change)
    lay_a.addRow('传感器灵敏度:', form.cb_sens)

    form.cb_dis_ch = QComboBox()
    form.cb_dis_ch.addItems([f'通道{i}' for i in range(8)])
    form.cb_dis_ch.setCurrentIndex(0)
    lay_a.addRow('位移信源通道:', form.cb_dis_ch)

    form.cb_speed_ch = QComboBox()
    form.cb_speed_ch.addItems([f'通道{i}' for i in range(8)])
    form.cb_speed_ch.setCurrentIndex(7)
    lay_a.addRow('转速信源通道:', form.cb_speed_ch)

    form.cb_rate = QComboBox()
    form.cb_rate.addItems(['5000', '10000', '20000'])
    form.cb_rate.setCurrentText('10000')
    lay_a.addRow('采样频率(Hz):', form.cb_rate)
    left_col.addWidget(grp_a)

    # ---------- (b) 变形曲线控制及设置 ----------
    grp_b = QGroupBox('变形拟合及目标设定')
    lay_b = QFormLayout(grp_b)

    form.edit_target = QLineEdit('100')
    lay_b.addRow('塑性变形目标(μm):', form.edit_target)

    form.cb_save_time = QComboBox()
    form.cb_save_time.setEditable(True)
    form.cb_save_time.addItems(['1000', '2000', '3000'])
    form.cb_save_time.setCurrentText('2000')
    form.cb_save_time.currentTextChanged.connect(_make_safe_float_setter('Savetime', 2000.0))
    lay_b.addRow('采集时长(s):', form.cb_save_time)

    form.btn_fit = QPushButton('开始拟合')
    form.btn_fit.setToolTip('用分段幂函数拟合当前采集的 转速-变形 数据, 在右图绘制拟合曲线')
    form.btn_fit.clicked.connect(form._btn_fit_click)
    lay_b.addRow(form.btn_fit)

    form.lbl_fit_result = QLabel('（尚未拟合）')
    form.lbl_fit_result.setWordWrap(True)
    form.lbl_fit_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
    lay_b.addRow(form.lbl_fit_result)
    left_col.addWidget(grp_b)

    # ---------- (c) 系统控制 ----------
    grp_c = QGroupBox('系统控制')
    lay_c = QVBoxLayout(grp_c)

    form.btn_zero = QPushButton('位置归零')
    form.btn_zero.clicked.connect(form._btn_zero_click)
    lay_c.addWidget(form.btn_zero)

    form.btn_start = QPushButton('开始采集')
    form.btn_start.setStyleSheet(
        "background-color: #28a745; color: white; font-weight: bold; padding: 5px;"
    )
    form.btn_start.clicked.connect(form._btn_start_click)
    lay_c.addWidget(form.btn_start)

    form.btn_stop = QPushButton('停止采集')
    form.btn_stop.setStyleSheet(
        "background-color: #dc3545; color: white; font-weight: bold; padding: 5px;"
    )
    form.btn_stop.setEnabled(False)
    form.btn_stop.clicked.connect(form._btn_stop_click)
    lay_c.addWidget(form.btn_stop)

    # 刷新: 清空已采集的显示数据 (不删除已保存的 CSV); 采集进行中由 MainForm 禁用
    form.btn_refresh = QPushButton('刷新')
    form.btn_refresh.setToolTip('清空当前散点/拟合线/指标等采集显示数据 (不删除已保存的 CSV 文件)')
    form.btn_refresh.clicked.connect(form._btn_refresh_click)
    lay_c.addWidget(form.btn_refresh)

    lay_c.addWidget(QLabel('目标完成率:'))
    form.progress = QProgressBar()
    form.progress.setRange(0, 100)
    lay_c.addWidget(form.progress)
    left_col.addWidget(grp_c)

    left_col.addStretch(1)
    left_widget = QWidget()
    left_widget.setLayout(left_col)
    left_widget.setMinimumWidth(440)
    mid.addWidget(left_widget, 1)      # 与右图各给伸缩因子 1 → 左右严格中分

    # ---- 右半栏: 散点图 (变形量-转速) ----
    form.plotxy = pg.PlotWidget(title='变形量-转速')
    form.plotxy.setLabel('left', '变形量', units='mm')
    # 纵轴关闭自动 SI 前缀: 不足 1mm 的变形量按小数/科学计数显示,
    # 不再被自动套前缀缩放 (避免 "0.99 mm" 被显示成 "990 mmm" 之类)
    form.plotxy.getAxis('left').enableAutoSIPrefix(False)
    form.plotxy.setLabel('bottom', '转速', units='RPM')
    form.plotxy.showGrid(x=True, y=True, alpha=0.3)
    form.plotxy.setMinimumWidth(440)
    form.scatter_xy = pg.ScatterPlotItem(size=4, brush=pg.mkBrush('#9467bd'), pen=None)
    form.plotxy.addItem(form.scatter_xy)
    form.fit_curve = form.plotxy.plot([], [], pen=pg.mkPen('#d62728', width=2))
    mid.addWidget(form.plotxy, 1)

    layout.addLayout(mid, 1)
    return page


def build_main_ui(form) -> None:
    """在 form (MainForm) 上构建选项卡式主窗口与定时器。

    Args:
        form: MainForm 实例 (QMainWindow 派生)。测量控件会挂到 form.<name>;
              选项卡控件挂到 form.tabs / form.dic_tab。

    Returns:
        无

    Side Effects:
        - 设置窗口标题/尺寸, 应用 APP_QSS 全局样式
        - 构建菜单栏 (设置 / 帮助)
        - 构建中央容器: QTabWidget (顶部不再放标题表头)
            * 选项卡「径向位移测量」: 由 _build_measure_tab(form) 构建, 挂载全部
              测量控件 (指示器/绘图/参数/控制) — 详见该函数
            * 选项卡「DIC 应变分析」: form.dic_tab = DicAnalysisTab(...)
        - 创建并挂载 form.tabs (QTabWidget)
        - 创建 form.timer1 (单次 100ms) / form.timer2 (周期 200ms)

    信号槽绑定 (form 必须实现以下方法):
        - form._show_speed_fix_dialog       (菜单 → 转速参数标定)
        - form._show_about                  (菜单 → 关于)
        - form._on_sens_change(idx)         (灵敏度下拉变化)
        - form._btn_fit_click / _btn_zero_click / _btn_start_click / _btn_stop_click
                                                                   / _btn_refresh_click
        - form._on_timer1 / _on_timer2

    说明:
        DIC 分析为完全独立的离线模块, 自带文件选择/进度/结果展示, 不依赖 form 的
        任何方法, 故其交互逻辑全部封装在 DicAnalysisTab 内 (无弹窗)。
    """
    form.setWindowTitle('在线变形测试系统')
    form.resize(1200, 800)
    form.setStyleSheet(APP_QSS)

    # ============================================================
    # 菜单栏 (设置 / 帮助)
    # ============================================================
    menubar = form.menuBar()
    m_settings = menubar.addMenu('设置(&S)')
    act_speed_fix = QAction('转速参数标定...', form)
    act_speed_fix.triggered.connect(form._show_speed_fix_dialog)
    m_settings.addAction(act_speed_fix)

    m_help = menubar.addMenu('帮助(&H)')
    act_about = QAction('关于(&A)', form)
    act_about.triggered.connect(form._show_about)
    m_help.addAction(act_about)

    # ============================================================
    # 中央容器: 标题表头 + 选项卡
    # ============================================================
    central_widget = QWidget()
    form.setCentralWidget(central_widget)
    root = QVBoxLayout(central_widget)
    root.setContentsMargins(8, 8, 8, 8)
    root.setSpacing(0)

    form.tabs = QTabWidget()
    form.tabs.setContentsMargins(8, 8, 8, 8)
    root.addWidget(form.tabs, 1)

    # 选项卡 1: 径向位移测量 (实时采集)
    form.tabs.addTab(_build_measure_tab(form), '径向位移测量')

    # 选项卡 2: 振动信号分析 (复刻 Delphi 振动链路, 与径向页共享采集会话)
    form.vib_tab = VibrationTab(form)
    form.tabs.addTab(form.vib_tab, '振动信号分析')

    # 选项卡 3: DIC 应变分析 (离线全场最大值, 内联展示, 无弹窗)
    form.dic_tab = DicAnalysisTab(form.exe_directory)
    form.tabs.addTab(form.dic_tab, 'DIC 应变分析')

    # ============================================================
    # 定时器
    # ============================================================
    # timer2: UI 刷新 (5 Hz 节流, 用于自动缩放绘图坐标)
    form.timer2 = QTimer(form)
    form.timer2.setInterval(200)
    form.timer2.timeout.connect(form._on_timer2)

    # timer1: 停止采集后延时关 CSV 的单次定时器 (100ms)
    form.timer1 = QTimer(form)
    form.timer1.setSingleShot(True)
    form.timer1.setInterval(100)
    form.timer1.timeout.connect(form._on_timer1)
