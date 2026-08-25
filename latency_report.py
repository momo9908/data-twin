"""
latency_report.py
=================
判定—弹窗时延 (t_ui) 的高精度打点容器与报告窗口 (对应文档 4.4.4 / 算法 4.2)。

职责:
    - LatencyProbe:        跨"采集判定 → GUI 弹窗"两阶段的打点容器, 推导
                           S1~S4+S5 各分段耗时与合计 t_ui (= t_show - t0)。
    - LatencyReportWindow: 非模态报告窗 (单例复用), 显示 t0 / t_show / t_ui
                           (与 0.1s 限值比对) 及各流程耗时表; 每次判定成立刷新。

口径 (与 main_app 计时打点一致):
    - 时钟: time.perf_counter() (单调高精度) 计时延; time.time() 仅作墙钟显示。
    - 合计 t_ui 计入 S1 判据计算: t_ui = t_calc + t_trig + t_post + t_render
      = t_show - t_pre (按用户要求: 只展示各流程耗时与合计, 不展示"余量")。

依赖:
    - PyQt5
    - dataclasses / datetime (标准库)

被调用方:
    - main_app.MainForm._on_data_ready    → 构造 LatencyProbe (采集判定侧打点)
    - main_app.MainForm._show_hold_prompt → 补 GUI 侧打点 + update_report
"""

from dataclasses import dataclass
from datetime import datetime

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView,
)


@dataclass
class LatencyProbe:
    """判定→弹窗时延的高精度打点容器 (时间单位: 秒, 取自 perf_counter)。

    采集判定侧字段 (由 _on_data_ready 填):
        t_pre:    判据计算前 (S1 起点)
        t0_perf:  判定成立时刻 (t_ui 起点; perf_counter)
        t0_wall:  判定成立墙钟 (time.time, 仅显示用)
        t_b:      本地触发处理完 (S2 终点, 即将投递)
        k:        就绪窗口读数序号 (锚点)
        plateau:  保载平台均值 RPM (锚点)

    GUI 弹窗侧字段 (由 _show_hold_prompt 填):
        t_c:         槽开始执行 (S3 终点)
        t_show_perf: 弹窗可见时刻 (t_ui 终点; perf_counter)
        t_show_wall: 弹窗可见墙钟 (显示用)

    不变量:
        t_ui = t_calc + t_trig + t_post + t_render = t_show_perf - t_pre (含 S1)。
    """
    t_pre: float
    t0_perf: float
    t0_wall: float
    t_b: float
    k: int
    plateau: float
    t_c: float = 0.0
    t_show_perf: float = 0.0
    t_show_wall: float = 0.0

    def stages(self) -> dict:
        """推导各流程耗时与合计 t_ui (单位: 秒)。

        Args:
            无 (读自身打点)

        Returns:
            dict: 键 't_calc'/'t_trig'/'t_post'/'t_render'/'t_ui', 值为秒。
                  t_calc = S1; t_ui = S1 + S2 + S3 + S4+S5 (含 S1)。

        Side Effects:
            无 (纯计算)

        不变量:
            t_calc + t_trig + t_post + t_render 恒等于 t_ui (四段首尾相接, 无缝隙)。
        """
        return {
            't_calc': self.t0_perf - self.t_pre,         # S1
            't_trig': self.t_b - self.t0_perf,            # S2
            't_post': self.t_c - self.t_b,                # S3
            't_render': self.t_show_perf - self.t_c,      # S4+S5
            't_ui': self.t_show_perf - self.t_pre,        # 合计 (含 S1)
        }


class LatencyReportWindow(QDialog):
    """非模态「判定—弹窗时延报告」窗口 (单例复用, 每次判定成立刷新)。

    职责:
        展示一次判定成立的: 判定时刻 t0、弹窗呈现时刻 t_show、判定→弹窗时延 t_ui
        (与 0.1s 限值比对), 以及 S1~S4+S5 各流程耗时与合计 t_ui 的表格。

    维护的状态:
        - 顶部标签 (t0 / t_show / t_ui / 平台) 与一个流程耗时表 (QTableWidget)
    """

    LIMIT_S = 0.100   # t_ui 达标限值 (s)

    # 流程表行: (步骤, 环节, stages() 键); 合计行单独处理
    _STAGE_ROWS = [
        ('S1', '判据计算 t_calc', 't_calc'),
        ('S2', '触发本地处理 t_trig', 't_trig'),
        ('S3', '跨线程投递 t_post', 't_post'),
        ('S4+S5', '界面渲染 t_render', 't_render'),
    ]

    def __init__(self, parent=None):
        """构造报告窗 (非模态), 搭建标签与流程表骨架。

        Args:
            parent: 父窗口 (MainForm)

        Returns:
            无

        Side Effects:
            - 创建标签与 QTableWidget 控件并布局; 设非模态
            - 不填数据 (待 update_report 调用时刷新)
        """
        super().__init__(parent)
        self.setWindowTitle('判定—弹窗时延报告')
        self.setModal(False)
        self.resize(460, 330)

        lay = QVBoxLayout(self)
        self.lbl_t0 = QLabel('判定进入保载 t0: —')
        self.lbl_show = QLabel('弹窗呈现 t_show: —')
        self.lbl_ui = QLabel('判定→弹窗时延 t_ui: —')
        self.lbl_ui.setStyleSheet('font-size: 15px; font-weight: bold;')
        self.lbl_meta = QLabel('保载平台: —    读数序号 k: —')
        self.lbl_meta.setStyleSheet('color: #8a96a6;')
        for w in (self.lbl_t0, self.lbl_show, self.lbl_ui, self.lbl_meta):
            lay.addWidget(w)

        self.table = QTableWidget(len(self._STAGE_ROWS) + 1, 3)
        self.table.setHorizontalHeaderLabels(['步骤', '环节', '耗时'])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        lay.addWidget(self.table)

    @staticmethod
    def _fmt_ms(seconds: float) -> str:
        """把秒格式化为毫秒字符串 (3 位小数)。

        Args:
            seconds: 时长 (s)

        Returns:
            形如 '3.142 ms' 的字符串

        Side Effects:
            无
        """
        return f'{seconds * 1000.0:.3f} ms'

    @staticmethod
    def _fmt_clock(wall: float) -> str:
        """把墙钟时间戳格式化为 时:分:秒.微秒。

        Args:
            wall: time.time() 时间戳 (s)

        Returns:
            形如 '14:03:27.512874' 的字符串

        Side Effects:
            无
        """
        return datetime.fromtimestamp(wall).strftime('%H:%M:%S.%f')

    def update_report(self, probe: LatencyProbe):
        """用一次判定的打点刷新报告窗内容。

        Args:
            probe: 已填齐两侧打点的 LatencyProbe

        Returns:
            无

        Side Effects:
            - 刷新顶部标签 (t0/t_show/t_ui/平台) 与流程表各单元
            - t_ui 达标显示绿色、超限显示红色

        行为不变量:
            - 合计行耗时取 stages()['t_ui'] (= S1+S2+S3+S4+S5), 与各分段口径一致
        """
        st = probe.stages()
        self.lbl_t0.setText(f'判定进入保载 t0: {self._fmt_clock(probe.t0_wall)}')
        self.lbl_show.setText(f'弹窗呈现 t_show: {self._fmt_clock(probe.t_show_wall)}')
        ok = st['t_ui'] < self.LIMIT_S
        color = '#1e7e34' if ok else '#c0392b'
        verdict = '✓ 达标' if ok else '✗ 超限'
        self.lbl_ui.setText(
            f'判定→弹窗时延 t_ui: <span style="color:{color};">'
            f'{self._fmt_ms(st["t_ui"])}（限值 {int(self.LIMIT_S * 1000)} ms）{verdict}</span>'
        )
        self.lbl_meta.setText(f'保载平台: {probe.plateau:.0f} RPM    读数序号 k: {probe.k}')

        for r, (step, env, key) in enumerate(self._STAGE_ROWS):
            self._set_row(r, step, env, self._fmt_ms(st[key]))
        # 合计行 (加粗)
        last = len(self._STAGE_ROWS)
        self._set_row(last, '合计', 't_ui（= S1+S2+S3+S4+S5）', self._fmt_ms(st['t_ui']), bold=True)

    def _set_row(self, r: int, step: str, env: str, dur: str, bold: bool = False):
        """填写流程表的一行 (步骤 / 环节 / 耗时)。

        Args:
            r:    行号
            step: 步骤标识 (如 'S2')
            env:  环节描述
            dur:  耗时字符串 (已格式化)
            bold: 是否加粗 (合计行用)

        Returns:
            无

        Side Effects:
            - 写入 self.table 第 r 行的 3 个单元格 (耗时列右对齐)
        """
        for c, text in enumerate((step, env, dur)):
            item = QTableWidgetItem(text)
            if c == 2:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if bold:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            self.table.setItem(r, c, item)
