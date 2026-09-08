# -*- coding: utf-8 -*-
"""
dic_tab.py
==========
DIC 全场最大 Mises 等效应变离线分析选项卡 (与实时径向位移测量界面完全分离)。

职责:
    提供一个自包含的 QWidget (DicAnalysisTab), 嵌入主窗口的 QTabWidget 中:
        - 顶部: DIC 数据文件下拉选择 (列出 DICdata 下的 .xlsx) + 刷新按钮
        - 左侧: 参数设置 (圆柱截面半径 R / 子午截面角度 θ / 转速 + 导出散点数据 /
                清空散点 按钮) + "分析"按钮 (Mises 等效应变) + 分析结果摘要
                (全场最大值数值、出现时刻、测点网格、坐标(笛卡尔+圆柱)、最大平均周向/径向)
        - 右侧: 应变-转速 散点图 (x=转速RPM, y=应变%, 三系列符号+图例, 悬停显示数值)
        - 底部: 内联进度条 + 取消按钮 + 状态行

    散点累加约定: 每点击一次"分析", 用当前转速作横坐标, 把本次的 全场最大Mises /
    最大平均周向 / 最大平均径向 三个值作纵坐标, 向图中追加一组(三个)散点并保存;
    "导出散点数据"把累计的所有散点(含最大Mises点的笛卡尔/柱坐标)写出 CSV。

    全过程 **不使用任何弹窗**: 进度、结果、错误、导出路径一律就地显示在本选项卡内。

依赖:
    - PyQt5
    - dic_analysis (纯计算: analyze_max_field / list_dic_files / available_metrics)

被调用方:
    - ui_builder.build_main_ui → DicAnalysisTab(form.exe_directory)
"""

import os
import csv
import datetime

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QComboBox, QPushButton, QProgressBar, QLabel, QGroupBox,
    QVBoxLayout, QHBoxLayout, QFormLayout, QDoubleSpinBox, QApplication,
)
import pyqtgraph as pg
from square_plot_widget import SquarePlotWidget

from dic_analysis import (
    analyze_max_field, list_dic_files, available_metrics, to_cylindrical,
)


class DicAnalysisTab(QWidget):
    """DIC 全场最大 Mises 等效应变分析选项卡 (内联展示, 无弹窗)。

    维护的状态:
        - self.exe_directory: 程序所在目录, 用于查找 DICdata 与导出 CSV
        - self.cb_file:       数据文件下拉框 (itemData 存绝对路径)
        - self.sp_radius / sp_theta / sp_speed: 圆柱截面半径 / 子午截面角度 / 转速 输入
        - self.btn_export / btn_clear: 导出散点数据 / 清空散点 按钮
        - self._module_buttons: 分析按钮列表 (当前仅 Mises; 运行时统一禁用/恢复)
        - self.progress / self.lbl_status: 内联进度条与状态行
        - self.lbl_summary:   结果摘要标签 (数值/时刻/网格/坐标/最大平均周向·径向)
        - self.plot / self._sc_mises / _sc_circ / _sc_radial: 右侧散点图与三条散点序列
        - self._scatter_records: 累计散点记录 (每次分析一条, 供重画与导出)
        - self._running / self._cancelled: 运行态与取消标志

    典型用法:
        tab = DicAnalysisTab(exe_directory)
        tabs.addTab(tab, 'DIC 应变分析')
    """

    def __init__(self, exe_directory: str, parent=None):
        """构造 DIC 分析选项卡并填充文件下拉框。

        Args:
            exe_directory: 程序所在目录 (MainForm.exe_directory)
            parent:        父控件 (QTabWidget)

        Returns:
            无

        Side Effects:
            - 创建全部子控件并布局
            - 调用 self._refresh_files() 扫描 DICdata 目录填充下拉框
            - 不读取 DIC 大文件 (仅列目录); 分析在点击按钮时才发生
        """
        super().__init__(parent)
        self.exe_directory = exe_directory
        self._module_buttons = []
        self._running = False
        self._cancelled = False
        # 累计散点: 每次点击分析追加一条 {'speed':转速, 'result':DicFieldResult, 'time':时间}
        self._scatter_records = []
        # 保载转速序列 (本次运行内, 采集时按保载顺序记录; 程序关闭不持久化)
        self._hold_speeds = []
        # 消费指针: 第几次分析用第几个保载转速 (随"开始采集"/"清空散点"归零)
        self._hold_index = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # ---------- 顶部: 数据文件选择 ----------
        grp_file = QGroupBox('DIC 数据文件')
        file_row = QHBoxLayout(grp_file)
        file_row.addWidget(QLabel('数据文件:'))
        self.cb_file = QComboBox()
        self.cb_file.setMinimumWidth(360)
        file_row.addWidget(self.cb_file, 1)
        self.btn_refresh = QPushButton('刷新')
        self.btn_refresh.clicked.connect(self._refresh_files)
        file_row.addWidget(self.btn_refresh)
        root.addWidget(grp_file)

        # ---------- 中部: 左(参数+分析+结果) | 右(散点图) ----------
        mid = QHBoxLayout()
        mid.setSpacing(12)

        # ===== 左半栏 =====
        left_col = QVBoxLayout()
        left_col.setSpacing(10)

        # 参数设置: 圆柱截面半径 R / 子午截面角度 θ / 转速 + 导出/清空按钮
        grp_params = QGroupBox('参数设置')
        pform = QFormLayout(grp_params)
        self.sp_radius = QDoubleSpinBox()
        self.sp_radius.setRange(0.0, 1.0e7)
        self.sp_radius.setDecimals(1)
        self.sp_radius.setSingleStep(1000.0)
        self.sp_radius.setValue(90000.0)
        self.sp_radius.setSuffix(' mm')
        self.sp_radius.setToolTip('圆柱截面(绕圆周一圈)的半径, 用于"平均径向Mises应变"')
        pform.addRow('圆柱截面半径 R:', self.sp_radius)
        self.sp_theta = QDoubleSpinBox()
        self.sp_theta.setRange(-360.0, 360.0)
        self.sp_theta.setDecimals(2)
        self.sp_theta.setSingleStep(1.0)
        self.sp_theta.setValue(30.0)
        self.sp_theta.setSuffix(' °')
        self.sp_theta.setToolTip('子午截面(沿半径一条线)的角度, 用于"平均周向Mises应变"')
        pform.addRow('子午截面角度 θ:', self.sp_theta)
        self.sp_speed = QDoubleSpinBox()
        self.sp_speed.setRange(0.0, 1.0e6)
        self.sp_speed.setDecimals(0)
        self.sp_speed.setSingleStep(100.0)
        self.sp_speed.setValue(500.0)
        self.sp_speed.setSuffix(' RPM')
        self.sp_speed.setToolTip('本次分析对应的转速, 作为散点横坐标; '
                                 '每点一次分析按当前转速记一组散点')
        pform.addRow('转速:', self.sp_speed)
        btn_row = QHBoxLayout()
        self.btn_export = QPushButton('导出散点数据')
        self.btn_export.setToolTip('把图中累计的所有散点导出为 CSV (自动存到程序目录)')
        self.btn_export.clicked.connect(self._on_export)
        self.btn_clear = QPushButton('清空散点')
        self.btn_clear.setToolTip('清空已累计的全部散点, 重新开始')
        self.btn_clear.clicked.connect(self._on_clear)
        btn_row.addWidget(self.btn_export)
        btn_row.addWidget(self.btn_clear)
        pform.addRow(btn_row)
        left_col.addWidget(grp_params)

        # 分析 (Mises 按钮): 每次点击 = 按当前转速向图中添加一组散点
        grp_mod = QGroupBox('分析')
        mod_lay = QVBoxLayout(grp_mod)
        mod_lay.setSpacing(8)
        mod_lay.addWidget(QLabel('点击下方按钮，计算并向右图添加一组散点：'))
        for key, label, unit in available_metrics():
            btn = QPushButton(label)
            btn.setMinimumHeight(40)
            btn.setToolTip(f'分析所选文件: 求全场最大 {label} 与截面平均, '
                           f'并按当前转速在右图添加一组散点')
            btn.clicked.connect(lambda _checked, k=key: self._run_module(k))
            mod_lay.addWidget(btn)
            self._module_buttons.append(btn)
        left_col.addWidget(grp_mod)

        # 分析结果 (移到左侧, 置于分析按钮下方)
        grp_res = QGroupBox('分析结果')
        res_lay = QVBoxLayout(grp_res)
        self.lbl_summary = QLabel(self._hint_html())
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_summary.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        res_lay.addWidget(self.lbl_summary)
        res_lay.addStretch(1)
        left_col.addWidget(grp_res, 1)

        left_widget = QWidget()
        left_widget.setLayout(left_col)
        left_widget.setMinimumWidth(460)
        left_widget.setMinimumHeight(left_col.minimumSize().height())
        mid.addWidget(left_widget, 1)      # 与右图各给伸缩因子 1 → 左右严格中分

        # ===== 右半栏: 应变-转速 散点图 (累加, 悬停显示数值) =====
        self.plot = SquarePlotWidget()
        self.plot.setMinimumWidth(480)
        self.plot.setLabel('bottom', '转速', units='RPM')
        self.plot.setLabel('left', '应变', units='%')
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        legend = self.plot.addLegend(offset=(-10, 10))
        self._sc_mises = pg.ScatterPlotItem(
            pen=None, symbol='o', size=12, brush=pg.mkBrush('#d62728'),
            hoverable=True, hoverPen=pg.mkPen('k', width=1.5),
            tip=lambda x, y, data: f'最大Mises等效应变\n转速 {x:.0f} RPM\n应变 {y:.6g} %')
        self._sc_circ = pg.ScatterPlotItem(
            pen=None, symbol='s', size=12, brush=pg.mkBrush('#1f77b4'),
            hoverable=True, hoverPen=pg.mkPen('k', width=1.5),
            tip=lambda x, y, data: f'平均周向应变\n转速 {x:.0f} RPM\n应变 {y:.6g} %')
        self._sc_radial = pg.ScatterPlotItem(
            pen=None, symbol='t', size=12, brush=pg.mkBrush('#2ca02c'),
            hoverable=True, hoverPen=pg.mkPen('k', width=1.5),
            tip=lambda x, y, data: f'平均径向应变\n转速 {x:.0f} RPM\n应变 {y:.6g} %')
        for sc, nm in ((self._sc_mises, '最大Mises等效应变'),
                       (self._sc_circ, '平均周向应变'),
                       (self._sc_radial, '平均径向应变')):
            self.plot.addItem(sc)
            legend.addItem(sc, nm)
        mid.addWidget(self.plot, 1)

        root.addLayout(mid, 1)

        # ---------- 底部: 进度 + 取消 + 状态 ----------
        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        bottom.addWidget(self.progress, 1)
        self.btn_cancel = QPushButton('取消')
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel)
        bottom.addWidget(self.btn_cancel)
        root.addLayout(bottom)

        self.lbl_status = QLabel('就绪。')
        self.lbl_status.setStyleSheet('color:#5a6b80;')
        root.addWidget(self.lbl_status)

        self._refresh_files()

    # ============================================================
    # 文件下拉
    # ============================================================
    def _refresh_files(self):
        """扫描 <exe>/DICdata 重新填充数据文件下拉框。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 清空并重填 self.cb_file (itemText=文件名, itemData=绝对路径)
            - 更新状态行: 列出文件数; 无文件时给出红色提示
        """
        self.cb_file.clear()
        dic_dir = os.path.join(self.exe_directory, 'DICdata')
        files = list_dic_files(dic_dir)
        for p in files:
            self.cb_file.addItem(os.path.basename(p), p)
        if files:
            self._set_status(f'发现 {len(files)} 个数据文件，点击“{self._only_label()}”开始分析。')
        else:
            self._set_status(f'未在 {dic_dir} 找到 .xlsx 文件，请放入后点“刷新”。',
                             error=True)

    # ============================================================
    # 运行分析
    # ============================================================
    def _run_module(self, metric_key: str):
        """对当前所选文件运行全场最大值分析, 结果就地展示 (仅 4 项关键信息)。

        Args:
            metric_key: dic_analysis 指标键 (当前仅 'mises')

        Returns:
            无

        Side Effects:
            - 运行期间禁用分析按钮/文件框/刷新, 启用取消按钮
            - 通过进度回调刷新 self.progress 并 QApplication.processEvents()
            - 调用 dic_analysis.analyze_max_field (只读大文件, 不写盘)
            - 成功: 填充 self.lbl_summary; 失败/无数据/取消: 仅更新状态行(红色),
              绝不弹窗
            - 防重入: 已在运行时直接忽略再次点击

        实现说明 (单线程 + processEvents):
            169MB 文件全表遍历约数十秒, 期间靠进度回调里的 processEvents 保持界面
            与取消按钮可响应; 运行期已禁用所有可重入入口, 无需窗口模态。
        """
        if self._running:
            return
        path = self.cb_file.currentData()
        if not path:
            self._set_status('请先在上方选择一个 DIC 数据文件。', error=True)
            return

        self._set_running(True)
        self._cancelled = False
        self.lbl_summary.setText(self._busy_html())
        self.progress.setValue(0)
        self._set_status(f'正在分析“{self.cb_file.currentText()}”，请稍候…')
        QApplication.processEvents()

        def _cb(done: int, total: int) -> bool:
            self.progress.setMaximum(total)
            self.progress.setValue(done)
            if done % 10 == 0 or done == total:
                self._set_status(f'分析中… {done}/{total} 时刻')
            QApplication.processEvents()
            return not self._cancelled

        try:
            result = analyze_max_field(
                path, metric_key, progress_cb=_cb,
                circ_theta_deg=self.sp_theta.value(),
                radial_radius=self.sp_radius.value(),
            )
        except KeyboardInterrupt:
            self._set_status('已取消分析。', error=True)
            self.lbl_summary.setText(self._hint_html())
            self._set_running(False)
            return
        except Exception as e:  # noqa: BLE001 - 内联展示错误, 不弹窗
            self._set_status(f'分析失败: {e}', error=True)
            self.lbl_summary.setText(self._hint_html())
            self._set_running(False)
            return

        self._fill_results(result)
        # 记录本次散点 (按当前转速), 累加并刷新右图
        self._scatter_records.append({
            'speed': float(self.sp_speed.value()),
            'result': result,
            'time': datetime.datetime.now(),
        })
        self._refresh_scatter()
        # 保载转速联动: 本次点击消费一个保载转速槽 (按点击先后次序); 用完则回退手动
        n_pair = self._hold_index + 1
        if self._hold_index < len(self._hold_speeds):
            self._hold_index += 1
            self._refresh_speed_prefill()            # 预填下一个待用保载转速
            pair_note = f'（对应第 {n_pair} 个保载转速）'
        elif self._hold_speeds:
            pair_note = '（保载转速已用完，本次为手动转速）'
        else:
            pair_note = '（无保载记录，手动转速）'
        skip_note = ''
        if result.skipped_stages:
            skip_note = f'，已跳过 {len(result.skipped_stages)} 个含异常数据的时刻表'
        self._set_status(
            f'分析完成，已按 {self.sp_speed.value():.0f} RPM 添加一组散点'
            f'{pair_note}，当前共 {len(self._scatter_records)} 组{skip_note}。'
        )
        self._set_running(False)

    def _on_cancel(self):
        """取消按钮: 置取消标志, 由进度回调在下一时刻生效。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 设置 self._cancelled = True; 更新状态行
        """
        if self._running:
            self._cancelled = True
            self._set_status('正在取消…')

    # ============================================================
    # 保载转速联动 (与实时采集的保载检测对接)
    # ============================================================
    def record_hold_speed(self, speed: float) -> None:
        """采集时检测到一次保载, 按顺序记录其转速 (由 MainForm 在 ENTER_HOLD 时调用)。

        Args:
            speed: 该次保载的转速 (RPM, 调用方已四舍五入)

        Returns:
            无

        Side Effects:
            - 追加到 self._hold_speeds
            - 刷新"转速"框预填, 使其显示下一个待用的保载转速
        """
        self._hold_speeds.append(float(speed))
        self._refresh_speed_prefill()

    def clear_hold_speeds(self) -> None:
        """清空保载转速序列并归零消费指针 (新一轮"开始采集"时调用)。

        Args:
            无

        Returns:
            无

        Side Effects:
            - self._hold_speeds 清空; self._hold_index = 0 (只保留最近一轮)
        """
        self._hold_speeds.clear()
        self._hold_index = 0

    def _refresh_speed_prefill(self) -> None:
        """把"转速"框预填为下一个待消费的保载转速 (若还有)。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 当 _hold_index < len(_hold_speeds) 时, setValue 到该保载转速;
              已用完则不动 (保留手动值, 供手动输入)
        """
        if 0 <= self._hold_index < len(self._hold_speeds):
            self.sp_speed.setValue(self._hold_speeds[self._hold_index])

    # ============================================================
    # 散点图 (累加) 与数据导出
    # ============================================================
    # CSV 导出列标题 (与 _export_row 一一对应)
    _EXPORT_HEADERS = [
        '序号', '转速(RPM)', '最大Mises等效应变(%)', '平均周向应变(%)', '平均径向应变(%)',
        '最大Mises出现时刻',
        '最大Mises点-原始笛卡尔X(mm)', '最大Mises点-原始笛卡尔Y(mm)', '最大Mises点-原始笛卡尔Z(mm)',
        '最大Mises点-变形后笛卡尔X(mm)', '最大Mises点-变形后笛卡尔Y(mm)', '最大Mises点-变形后笛卡尔Z(mm)',
        '最大Mises点-原始柱坐标r(mm)', '最大Mises点-原始柱坐标θ(°)', '最大Mises点-原始柱坐标z(mm)',
        '最大Mises点-变形后柱坐标r(mm)', '最大Mises点-变形后柱坐标θ(°)', '最大Mises点-变形后柱坐标z(mm)',
        '数据文件', '圆柱半径R(mm)', '子午角度θ(°)', '跳过时刻数', '跳过时刻表', '记录时间',
    ]

    def _refresh_scatter(self):
        """用累计的全部记录重画三条散点序列 (某项为 None 的记录在该序列中跳过)。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 对 self._sc_mises / _sc_circ / _sc_radial 调用 setData
            - 横坐标取各记录的转速, 纵坐标取对应指标值
        """
        def _xy(getter):
            xs, ys = [], []
            for rec in self._scatter_records:
                v = getter(rec['result'])
                if v is None:
                    continue
                xs.append(rec['speed'])
                ys.append(v)
            return xs, ys

        xm, ym = _xy(lambda r: r.global_max.value if r.global_max else None)
        xc, yc = _xy(lambda r: r.circ_section.value if r.circ_section else None)
        xr, yr = _xy(lambda r: r.radial_section.value if r.radial_section else None)
        self._sc_mises.setData(xm, ym)
        self._sc_circ.setData(xc, yc)
        self._sc_radial.setData(xr, yr)

    def _on_clear(self):
        """[清空散点] 按钮: 清空累计记录并清空右图三条散点序列。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 清空 self._scatter_records; 刷新(清空)散点; 更新状态行
            - 运行分析期间忽略点击
        """
        if self._running:
            return
        self._scatter_records.clear()
        self._refresh_scatter()
        self._hold_index = 0                 # 消费指针随"清空散点"归零, 可从头重配
        self._refresh_speed_prefill()        # 重新预填第 1 个保载转速 (若有)
        self._set_status('已清空全部散点；保载转速配对已归零。')

    def _on_export(self):
        """[导出散点数据] 按钮: 把累计散点写入程序目录下的 CSV (无弹窗)。

        Args:
            无

        Returns:
            无

        Side Effects:
            - 无记录时仅红色状态提示
            - 否则在 self.exe_directory 下以时间戳命名写 CSV (UTF-8 with BOM),
              成功/失败均在状态行显示 (不弹窗)
            - 运行分析期间忽略点击

        CSV 约定:
            - 一行一次点击 (含该次三个散点值), 列见 _EXPORT_HEADERS
            - 附最大 Mises 点的原始/变形后 笛卡尔与柱坐标
        """
        if self._running:
            return
        if not self._scatter_records:
            self._set_status('暂无散点可导出，请先分析。', error=True)
            return
        fname = 'DIC散点数据_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '.csv'
        path = os.path.join(self.exe_directory, fname)
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                w.writerow(self._EXPORT_HEADERS)
                for i, rec in enumerate(self._scatter_records, start=1):
                    w.writerow(self._export_row(i, rec))
        except Exception as e:  # noqa: BLE001 - 内联展示错误, 不弹窗
            self._set_status(f'导出失败: {e}', error=True)
            return
        self._set_status(f'已导出 {len(self._scatter_records)} 组散点到: {path}')

    def _export_row(self, idx: int, rec: dict) -> list:
        """把一条散点记录拼成 CSV 行 (与 _EXPORT_HEADERS 对应)。

        Args:
            idx: 序号 (1-based)
            rec: 记录 dict (含 'speed' / 'result' / 'time')

        Returns:
            字符串/数值列表, 长度与 _EXPORT_HEADERS 相同

        Side Effects:
            无 (纯组装; 柱坐标由 to_cylindrical 实时换算)
        """
        res = rec['result']
        gm = res.global_max

        def _f(v):
            return '' if v is None else f'{v:.6g}'

        if gm is not None:
            mises = gm.value
            ref_c = (gm.ref_x_mm, gm.ref_y_mm, gm.ref_z_mm)
            def_c = (gm.x_mm, gm.y_mm, gm.z_mm)
            ref_cyl = to_cylindrical(gm.ref_x_mm, gm.ref_y_mm, gm.ref_z_mm)
            def_cyl = to_cylindrical(gm.x_mm, gm.y_mm, gm.z_mm)
            stage = gm.stage_name
        else:
            mises = None
            ref_c = def_c = ref_cyl = def_cyl = (None, None, None)
            stage = ''
        circ = res.circ_section.value if res.circ_section else None
        radial = res.radial_section.value if res.radial_section else None

        return [
            idx, f'{rec["speed"]:.0f}', _f(mises), _f(circ), _f(radial), stage,
            _f(ref_c[0]), _f(ref_c[1]), _f(ref_c[2]),
            _f(def_c[0]), _f(def_c[1]), _f(def_c[2]),
            _f(ref_cyl[0]), _f(ref_cyl[1]), _f(ref_cyl[2]),
            _f(def_cyl[0]), _f(def_cyl[1]), _f(def_cyl[2]),
            os.path.basename(res.file_path),
            _f(res.radial_radius), _f(res.circ_theta_deg),
            len(res.skipped_stages), '; '.join(res.skipped_stages),
            rec['time'].strftime('%Y-%m-%d %H:%M:%S'),
        ]

    # ============================================================
    # 结果填充
    # ============================================================
    def _fill_results(self, result):
        """把 DicFieldResult 填入摘要标签 (仅关键信息)。

        仅展示: 全场最大值数值、平均周向应变值、平均径向应变值、最大点坐标
        (笛卡尔 + 圆柱坐标; 参考态 + 变形后)。不再显示出现时刻与测点网格。

        Args:
            result: dic_analysis.analyze_max_field 的返回值

        Returns:
            无

        Side Effects:
            - 改写 self.lbl_summary (富文本)
            - 更新状态行为简短完成提示
        """
        gm = result.global_max
        u = result.unit
        if gm is None:
            self.lbl_summary.setText(
                '<div style="color:#c0392b; font-size:18px;">'
                '分析完成：未找到任何有效数据（该列全为空）。</div>'
            )
            self._set_status('完成，但无有效数据。', error=True)
            return

        # 笛卡尔坐标 + 圆柱坐标 (r, θ, z): r=√(x²+y²), θ=atan2(y,x)°, z=Z坐标
        ref_line = ''
        cyl_ref = ''
        if gm.ref_x_mm is not None and gm.ref_y_mm is not None:
            ref_line = f'参考坐标：({gm.ref_x_mm:.1f}, {gm.ref_y_mm:.1f}) mm<br>'
            rr, thr, zr = to_cylindrical(gm.ref_x_mm, gm.ref_y_mm, gm.ref_z_mm)
            cyl_ref = (f'圆柱坐标(参考)：r={rr:.1f} mm，θ={thr:.3f}°，z={zr:.1f} mm<br>')
        rd, thd, zd = to_cylindrical(gm.x_mm, gm.y_mm, gm.z_mm)
        cyl_def = (f'圆柱坐标(变形后)：r={rd:.1f} mm，θ={thd:.3f}°，z={zd:.1f} mm'
                   if rd is not None else '')

        # 平均周向 / 平均径向 应变 (本次跨时刻最大的截面平均值, 仅显示数值)
        circ = result.circ_section
        radial = result.radial_section
        circ_line = (f'平均周向应变：{circ.value:.6g} {u}<br>'
                     if circ is not None else '平均周向应变：（无数据）<br>')
        radial_line = (f'平均径向应变：{radial.value:.6g} {u}<br>'
                       if radial is not None else '平均径向应变：（无数据）<br>')

        self.lbl_summary.setText(
            f'<div style="font-size:18px; line-height:185%;">'
            f'<b>全场最大 {result.metric_label}</b><br>'
            f'<span style="color:#c0392b; font-size:32px; font-weight:bold;">'
            f'{gm.value:.6g} {u}</span><br>'
            # 除标题与上面的大数值外, 其余各行字号 22px
            f'<span style="font-size:22px;">'
            f'{circ_line}'
            f'{radial_line}'
            f'{ref_line}'
            f'{cyl_ref}'
            f'变形后坐标：({gm.x_mm:.1f}, {gm.y_mm:.1f}) mm<br>'
            f'{cyl_def}'
            f'</span>'
            f'</div>'
        )
        self._set_status(
            f'分析完成：最大 {result.metric_label} = {gm.value:.6g} {u} @ {gm.stage_name}。'
        )

    def _section_html(self, result, unit: str) -> str:
        """生成"最大平均周向/径向Mises应变"两行展示文本 (富文本)。

        Args:
            result: dic_analysis.analyze_max_field 的返回值
            unit:   指标单位 (如 '%')

        Returns:
            富文本字符串 (两行); 未请求或无数据点时给出相应说明。

        Side Effects:
            无 (纯字符串拼装)
        """
        parts = []
        cs = result.circ_section
        if cs is not None:
            parts.append(
                f'最大平均周向Mises应变：'
                f'<b style="color:#1d6f42;">{cs.value:.6g} {unit}</b> @ '
                f'{cs.stage_name}（{cs.param_desc}，{cs.n_points}点）'
            )
        elif result.circ_theta_deg is not None:
            parts.append(
                f'最大平均周向Mises应变：子午截面 θ={result.circ_theta_deg:.1f}° '
                f'范围内无数据点'
            )
        rs = result.radial_section
        if rs is not None:
            parts.append(
                f'最大平均径向Mises应变：'
                f'<b style="color:#1d6f42;">{rs.value:.6g} {unit}</b> @ '
                f'{rs.stage_name}（{rs.param_desc}，{rs.n_points}点）'
            )
        elif result.radial_radius is not None:
            parts.append(
                f'最大平均径向Mises应变：圆柱截面 r={result.radial_radius:.0f} '
                f'范围内无数据点'
            )
        return '<br>'.join(parts)

    # ============================================================
    # 小工具
    # ============================================================
    def _only_label(self) -> str:
        """返回当前唯一分析按钮的中文名 (用于状态提示)。

        Returns:
            指标中文名; 无指标时回退 '分析'

        Side Effects:
            无 (纯函数)
        """
        metrics = available_metrics()
        return metrics[0][1] if metrics else '分析'

    def _set_running(self, running: bool):
        """切换运行态: 统一禁用/恢复可重入入口, 切换取消按钮。

        Args:
            running: True=进入分析中; False=空闲

        Returns:
            无

        Side Effects:
            - 禁用/启用所有分析按钮、文件下拉、刷新按钮
            - 取消按钮与之相反
            - 同步 self._running
        """
        self._running = running
        for b in self._module_buttons:
            b.setEnabled(not running)
        self.cb_file.setEnabled(not running)
        self.btn_refresh.setEnabled(not running)
        self.sp_radius.setEnabled(not running)
        self.sp_theta.setEnabled(not running)
        self.sp_speed.setEnabled(not running)
        self.btn_export.setEnabled(not running)
        self.btn_clear.setEnabled(not running)
        self.btn_cancel.setEnabled(running)

    def _set_status(self, text: str, error: bool = False):
        """更新底部状态行文字与颜色。

        Args:
            text:  状态文本
            error: True 用红色 (错误/警告); False 用常规灰蓝色

        Returns:
            无

        Side Effects:
            - 改写 self.lbl_status 文本与样式
        """
        self.lbl_status.setStyleSheet('color:#c0392b;' if error else 'color:#5a6b80;')
        self.lbl_status.setText(text)

    def _hint_html(self) -> str:
        """结果区初始/复位时的提示文案 (富文本)。

        Returns:
            提示 HTML 字符串

        Side Effects:
            无 (纯函数)
        """
        return (
            '<div style="color:#5a6b80; font-size:16px; line-height:170%;">'
            '请先选择需要分析的 DIC 文件，然后设置需计算周向平均应变的子午截面'
            '以及径向平均应变的圆柱截面，最后点击按钮。'
            '</div>'
        )

    def _busy_html(self) -> str:
        """分析进行中的占位文案 (富文本)。

        Returns:
            占位 HTML 字符串

        Side Effects:
            无 (纯函数)
        """
        return ('<div style="color:#1d4e89; font-size:18px;">正在分析，请稍候…</div>')
