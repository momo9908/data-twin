"""独立的材料曲线导入、换算、绘图与轮盘参数设置页面。"""

import math
import os
import threading
from pathlib import Path

import numpy as np
from PyQt5.QtCore import Qt, QObject, QRunnable, QThreadPool, QEvent, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QApplication, QWidget, QGroupBox, QLabel, QLineEdit, QPushButton,
    QProgressBar, QScrollArea, QTableWidget,
    QTableWidgetItem, QAbstractItemView, QHeaderView, QFileDialog,
    QVBoxLayout, QHBoxLayout, QFormLayout,
)
import pyqtgraph as pg
import pyqtgraph.exporters

from material_data import (
    DiscParameters, MaterialImportCancelled, auto_import_material,
    load_disc_parameters, save_disc_parameters,
    export_material_csv,
)
from square_plot_widget import SquarePlotWidget


class _TaskSignals(QObject):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    finished = pyqtSignal()


class _MaterialTask(QRunnable):
    """后台任务只读写数据，不访问任何 QWidget。"""

    def __init__(self, operation):
        super().__init__()
        self.signals = _TaskSignals()
        self.cancel_event = threading.Event()
        self.operation = operation

    def run(self):
        try:
            result = self.operation(self.cancel_event.is_set)
            if self.cancel_event.is_set():
                self.signals.cancelled.emit()
            else:
                self.signals.succeeded.emit(result)
        except MaterialImportCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            self.signals.finished.emit()


class DiscParametersTab(QWidget):
    """自包含第四页；材料数据和参数不写入现有采集或 DIC 的状态。"""

    def __init__(self, exe_directory: str, parent=None):
        super().__init__(parent)
        self.exe_directory = os.path.abspath(exe_directory)
        self.parameters_path = os.path.join(self.exe_directory, 'Sysdata', 'DiscParameters.json')
        self._source_path = ''
        self._requested_path = ''
        self._preview = None
        self._curve = None
        self._active_task = None
        self._workers = set()
        self._closing = False
        self._observed_window = None
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._parameters = DiscParameters()
        self._parameters_dirty = False
        self._restore_failed = False
        self._build_ui()
        self._restore_parameters()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        middle = QHBoxLayout()
        middle.setSpacing(12)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(9)
        group_file = QGroupBox('材料数据导入')
        file_layout = QVBoxLayout(group_file)
        row_file = QHBoxLayout()
        self.edit_file = QLineEdit()
        self.edit_file.setReadOnly(True)
        self.edit_file.setPlaceholderText('选择工程应力—应变数据文件')
        self.btn_import = QPushButton('导入并绘图')
        self.btn_import.clicked.connect(self._select_file)
        row_file.addWidget(self.edit_file, 1)
        row_file.addWidget(self.btn_import)
        file_layout.addLayout(row_file)

        self.lbl_detection = QLabel()
        self.lbl_detection.setWordWrap(True)
        self.lbl_detection.setTextInteractionFlags(Qt.TextSelectableByMouse)
        file_layout.addWidget(self.lbl_detection)
        self.lbl_detection.hide()
        self.table_preview = QTableWidget(0, 0)
        self.table_preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_preview.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_preview.setMinimumHeight(140)
        self.table_preview.setMaximumHeight(220)
        self.table_preview.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table_preview.verticalHeader().setDefaultSectionSize(22)
        file_layout.addWidget(self.table_preview)

        self.btn_reload = QPushButton('重新识别并绘图')
        self.btn_reload.setEnabled(False)
        self.btn_reload.clicked.connect(self._reload_material)
        file_layout.addWidget(self.btn_reload)
        left_layout.addWidget(group_file)

        parameters_group = QGroupBox('轮盘参数')
        parameters_form = QFormLayout(parameters_group)
        self.edit_diameter = QLineEdit('1')
        self.edit_circumferential = QLineEdit('1')
        self.edit_radial = QLineEdit('1')
        self.edit_diameter.setToolTip('轮盘外径，单位 mm，必须大于 0')
        self.edit_circumferential.setToolTip('无量纲；不限制上下界，须为有限数值')
        self.edit_radial.setToolTip('无量纲；不限制上下界，须为有限数值')
        parameters_form.addRow('轮盘外径（mm）：', self.edit_diameter)
        parameters_form.addRow('周向应力利用系数：', self.edit_circumferential)
        parameters_form.addRow('径向应力利用系数：', self.edit_radial)
        self.btn_save_parameters = QPushButton('保存参数')
        self.btn_save_parameters.clicked.connect(self.save_parameters)
        parameters_form.addRow('', self.btn_save_parameters)
        self.lbl_parameters = QLabel('')
        self.lbl_parameters.setWordWrap(True)
        parameters_form.addRow(self.lbl_parameters)
        self.lbl_parameters.hide()
        for editor in (self.edit_diameter, self.edit_circumferential, self.edit_radial):
            editor.textEdited.connect(self._mark_parameters_dirty)
            editor.editingFinished.connect(self._autosave_parameters)
        left_layout.addWidget(parameters_group)

        group_export = QGroupBox('数据与图像导出')
        exports = QHBoxLayout(group_export)
        self.btn_export_csv = QPushButton('导出 CSV')
        self.btn_export_png = QPushButton('保存高清图片')
        self.btn_export_csv.clicked.connect(self._choose_export_csv)
        self.btn_export_png.clicked.connect(self._choose_export_png)
        self.btn_export_csv.setEnabled(False)
        self.btn_export_png.setEnabled(False)
        exports.addWidget(self.btn_export_csv)
        exports.addWidget(self.btn_export_png)
        left_layout.addWidget(group_export)
        left_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(left)
        scroll.setMinimumWidth(420)
        middle.addWidget(scroll, 1)

        right = QVBoxLayout()
        self.plot = SquarePlotWidget(background='w')
        self.plot.setMinimumWidth(460)
        self.plot.setLabel('bottom', '应变（无量纲）', **{'color': '#202020', 'font-size': '12pt'})
        self.plot.setLabel('left', '应力（MPa）', **{'color': '#202020', 'font-size': '12pt'})
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        for name in ('bottom', 'left'):
            axis = self.plot.getAxis(name)
            axis.enableAutoSIPrefix(False)
            axis.setPen(pg.mkPen('#303030'))
            axis.setTextPen(pg.mkPen('#303030'))
        self.plot.addLegend(offset=(-12, 12), labelTextColor='#303030',
                            labelTextSize='11pt', brush=pg.mkBrush(255, 255, 255, 220))
        self.engineering_curve = self.plot.plot([], [], name='工程应力—应变',
                                                pen=pg.mkPen('#1565c0', width=2.2))
        self.true_curve = self.plot.plot([], [], name='真实应力—应变',
                                         pen=pg.mkPen('#d84315', width=2.2))
        right.addWidget(self.plot, 1)
        self.lbl_summary = QLabel()
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right.addWidget(self.lbl_summary)
        self.lbl_summary.hide()
        middle.addLayout(right, 1)
        root.addLayout(middle, 1)

        status_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setMaximumWidth(160)
        self.progress.setTextVisible(False)
        self.btn_cancel = QPushButton('取消读取')
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_import)
        self.lbl_status = QLabel('就绪。请选择材料文件。')
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status_row.addWidget(self.progress)
        status_row.addWidget(self.lbl_status, 1)
        status_row.addWidget(self.btn_cancel)
        root.addLayout(status_row)

    @property
    def material_curve(self):
        return self._curve

    @property
    def parameters(self):
        """最近一次有效并成功保存的参数。"""
        return self._parameters

    @property
    def is_busy(self):
        return self._active_task is not None

    def _set_status(self, text, error=False):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet('color:#b42318;' if error else 'color:#53657b;')

    def _set_busy(self, busy):
        self.btn_import.setEnabled(not busy)
        self.btn_reload.setEnabled(not busy and bool(self._requested_path or self._source_path))
        self.btn_export_csv.setEnabled(not busy and self._curve is not None)
        self.btn_export_png.setEnabled(not busy and self._curve is not None)
        self.btn_cancel.setEnabled(busy)
        self.progress.setRange(0, 0 if busy else 1)
        if not busy:
            self.progress.setValue(1 if self._curve is not None else 0)

    def _run_task(self, operation, success, text):
        if self._closing or self.is_busy:
            return
        task = _MaterialTask(operation)
        self._active_task = task
        self._workers.add(task)
        task.signals.succeeded.connect(success)
        task.signals.failed.connect(self._on_task_failure)
        task.signals.cancelled.connect(self._on_task_cancelled)
        task.signals.finished.connect(lambda: self._finish_task(task))
        self._set_busy(True)
        self._set_status(text)
        self._pool.start(task)

    def _on_task_failure(self, error):
        name = os.path.basename(self._requested_path)
        self._set_status(f'自动导入失败（{name}）：{error}。原有曲线保留。', True)

    def _on_task_cancelled(self):
        self._set_status('已取消读取；原有曲线保留。')

    def _finish_task(self, task):
        self._workers.discard(task)
        if self._active_task is task:
            self._active_task = None
        if not self._closing:
            self._set_busy(False)

    def _select_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '导入材料工程应力—应变数据', self.exe_directory,
            '材料数据 (*.xlsx *.xlsm *.csv *.txt *.tsv *.dat);;所有文件 (*)')
        if path:
            self.load_file(path)

    def load_file(self, path):
        """一次后台任务完成识别、换算和绘图，无需选择格式或数据列。"""
        if self._closing or self.is_busy:
            return
        path = os.path.abspath(path)
        self._requested_path = path
        self._run_task(
            lambda cancelled: auto_import_material(path, cancelled=cancelled),
            self._apply_import, '正在自动识别材料格式、数据列与单位并转换…')

    def _reload_material(self):
        path = self._requested_path or self._source_path
        if path:
            self.load_file(path)

    def _apply_import(self, result):
        if self._closing:
            return
        preview = result.preview
        self._preview = preview
        self._source_path = preview.path
        self.edit_file.setText(preview.path)
        self.edit_file.setToolTip(preview.path)
        separator = {',': '逗号', '\t': '制表符', ';': '分号', 'whitespace': '空白'}.get(
            preview.delimiter, preview.delimiter)
        location = '工作表：'+str(preview.sheet) if preview.sheet is not None else (
            f'编码：{preview.encoding}；分隔符：{separator}')
        details = (f'{location}；工程应变第 {preview.suggested_strain_column+1} 列'
                   f'（{preview.strain_unit}）；工程应力第 {preview.suggested_stress_column+1} 列'
                   f'（{preview.stress_unit}）；数据从第 {preview.suggested_start_row} 行开始。')
        if result.notes:
            details += ' '+'；'.join(result.notes)
        self.lbl_detection.setText(details)
        self.lbl_detection.show()
        self.table_preview.setRowCount(len(preview.rows))
        self.table_preview.setColumnCount(len(preview.column_labels))
        self.table_preview.setHorizontalHeaderLabels(
            [str(i+1) for i in range(len(preview.column_labels))])
        if preview.source_rows:
            self.table_preview.setVerticalHeaderLabels([str(row) for row in preview.source_rows])
        for row_index, row in enumerate(preview.rows):
            for col_index, value in enumerate(row):
                if col_index < self.table_preview.columnCount():
                    item = QTableWidgetItem(str(value))
                    item.setToolTip(str(value))
                    self.table_preview.setItem(row_index, col_index, item)
        for col in range(self.table_preview.columnCount()):
            self.table_preview.setColumnWidth(col, 135)
        self._apply_curve(result.curve)

    @staticmethod
    def _plot_coordinates(x, y, breaks):
        """无效源行之间断线，保持有效点本身及其原始顺序。"""
        xx, yy = [], []
        for index, (vx, vy) in enumerate(zip(x, y)):
            if index and bool(breaks[index]):
                xx.append(np.nan)
                yy.append(np.nan)
            xx.append(float(vx))
            yy.append(float(vy))
        return np.asarray(xx), np.asarray(yy)

    def _apply_curve(self, curve):
        if self._closing:
            return
        self._curve = curve
        self.engineering_curve.setData(
            *self._plot_coordinates(curve.engineering_strain, curve.engineering_stress, curve.break_before),
            connect='finite')
        self.true_curve.setData(
            *self._plot_coordinates(curve.true_strain, curve.true_stress, curve.break_before),
            connect='finite')
        self.plot.enableAutoRange()
        self.plot.autoRange()
        summary = (f'当前曲线：{os.path.basename(curve.path)}；有效数据 {curve.point_count} 点；'
                   f'工程应变 {np.min(curve.engineering_strain):.6g}～{np.max(curve.engineering_strain):.6g}；'
                   f'工程应力 {np.min(curve.engineering_stress):.6g}～{np.max(curve.engineering_stress):.6g} MPa。')
        if curve.rejected_rows:
            details = '；'.join(f'第 {row} 行：{reason}' for row, reason in curve.rejected_rows[:8])
            summary += f' 跳过 {len(curve.rejected_rows)} 行无效数据（悬停查看原因）。'
            self.lbl_summary.setToolTip(details)
        else:
            self.lbl_summary.setToolTip('')
        if curve.blank_rows:
            summary += f' 空白行 {curve.blank_rows} 行。'
        if curve.warnings:
            summary += ' '+'；'.join(curve.warnings)
        self.lbl_summary.setText(summary)
        self.lbl_summary.show()
        self._set_status('转换完成。两条曲线按全部有效数据点及原始行序绘制，可导出 CSV 或高清图片。')

    def _restore_parameters(self):
        try:
            self._parameters = load_disc_parameters(self.parameters_path)
            existed = os.path.exists(self.parameters_path)
            self.lbl_parameters.setText('已恢复上次保存的参数。' if existed else '')
        except Exception as exc:
            self._parameters = DiscParameters()
            self._restore_failed = True
            self.lbl_parameters.setText('参数文件无法读取，当前使用默认值 1：'+str(exc))
            self.lbl_parameters.setStyleSheet('color:#b42318;')
        self.lbl_parameters.setVisible(bool(self.lbl_parameters.text()))
        for editor, value in ((self.edit_diameter, self._parameters.outer_diameter_mm),
                              (self.edit_circumferential, self._parameters.circumferential_coefficient),
                              (self.edit_radial, self._parameters.radial_coefficient)):
            text = repr(float(value))
            editor.setText(text[:-2] if text.endswith('.0') else text)

    def _mark_parameters_dirty(self, _text):
        self._parameters_dirty = True

    def _autosave_parameters(self):
        if self._parameters_dirty:
            self.save_parameters()

    def save_parameters(self):
        try:
            values = []
            for label, editor in (('轮盘外径', self.edit_diameter),
                                  ('周向应力利用系数', self.edit_circumferential),
                                  ('径向应力利用系数', self.edit_radial)):
                try:
                    value = float(editor.text().strip())
                except ValueError:
                    raise ValueError(label+'必须填写数值。') from None
                if not math.isfinite(value):
                    raise ValueError(label+'必须为有限数值。')
                values.append(value)
            if values[0] <= 0:
                raise ValueError('轮盘外径必须大于 0 mm。')
            parameters = DiscParameters(*values)
            save_disc_parameters(self.parameters_path, parameters)
            self._parameters = parameters
            self._parameters_dirty = False
            self._restore_failed = False
            self.lbl_parameters.setText('参数已保存，下次启动自动恢复。')
            self.lbl_parameters.setStyleSheet('color:#53657b;')
            self.lbl_parameters.show()
            return True
        except Exception as exc:
            self.lbl_parameters.setText('参数未保存：'+str(exc))
            self.lbl_parameters.setStyleSheet('color:#b42318;')
            self.lbl_parameters.show()
            return False

    def _default_export_path(self, suffix):
        name = Path(self._curve.path).stem if self._curve is not None else '材料'
        return os.path.join(self.exe_directory, name+'_应力应变'+suffix)

    def _choose_export_csv(self):
        if self._curve is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, '导出工程与真实应力应变数据',
                                             self._default_export_path('.csv'), 'CSV 文件 (*.csv)')
        if path:
            self.export_csv(path if path.lower().endswith('.csv') else path+'.csv')

    def export_csv(self, path):
        """导出当前有效曲线；直接路径入口便于集成测试。"""
        if self._curve is None:
            self._set_status('请先转换材料数据。', True)
            return False
        try:
            self._check_export_path(path, '.csv')
            export_material_csv(path, self._curve)
            self._set_status('CSV 已保存：'+str(path))
            return True
        except Exception as exc:
            self._set_status('CSV 导出失败：'+str(exc), True)
            return False

    def _choose_export_png(self):
        if self._curve is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, '保存高清应力应变曲线',
                                             self._default_export_path('.png'), 'PNG 图片 (*.png)')
        if path:
            self.export_png(path if path.lower().endswith('.png') else path+'.png')

    def export_png(self, path):
        if self._curve is None:
            self._set_status('请先转换材料数据。', True)
            return False
        try:
            self._check_export_path(path, '.png')
            self.plot._layout_square()
            exporter = pg.exporters.ImageExporter(self.plot.plotItem)
            exporter.parameters()['width'] = 2600
            exporter.parameters()['background'] = QColor('white')
            exporter.export(str(path))
            self._set_status('高清图片已保存：'+str(path))
            return True
        except Exception as exc:
            self._set_status('图片导出失败：'+str(exc), True)
            return False

    def _check_export_path(self, path, extension):
        if not str(path).lower().endswith(extension):
            raise ValueError('导出文件应使用 '+extension+' 扩展名。')
        for source in (self._source_path, self._requested_path,
                       self._curve.path if self._curve is not None else ''):
            if not source:
                continue
            same = os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(source))
            if not same and os.path.exists(path):
                try:
                    same = os.path.samefile(path, source)
                except OSError:
                    pass
            if same:
                raise ValueError('导出路径不能与导入的材料源文件相同。')

    def cancel_import(self):
        if self._active_task is not None:
            self._active_task.cancel_event.set()
            self.btn_cancel.setEnabled(False)
            self._set_status('正在取消读取…')

    def showEvent(self, event):
        super().showEvent(event)
        window = self.window()
        if window is not self and window is not self._observed_window:
            if self._observed_window is not None:
                self._observed_window.removeEventFilter(self)
            self._observed_window = window
            window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self._observed_window and event.type() == QEvent.Close:
            self.shutdown()
        return super().eventFilter(watched, event)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self):
        """请求取消并等待本页任务退出，避免关闭时仍访问已销毁控件。"""
        if self._closing:
            return
        if self._parameters_dirty:
            self.save_parameters()
        self._closing = True
        for task in self._workers:
            task.cancel_event.set()
        self._pool.waitForDone()
