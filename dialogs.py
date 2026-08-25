"""
dialogs.py
==========
对话框模块, 当前只含 SpeedFixDialog (转速参数标定窗)。

职责:
    把原 main_app.py:57-119 的 SpeedFixDialog 整体搬过来。
    内部对 DeviceSet.txt 的写文件操作改为调用 settings_io.save_device_set,
    以避免路径解析和 5 行格式逻辑在多处重复。

    (DIC 分析结果改为在 dic_tab.DicAnalysisTab 内联展示, 不再使用任何弹窗,
     故曾经的 DicResultDialog 已移除。)

依赖:
    - PyQt5
    - public_para.g           (读写 5 个校准字段)
    - settings_io.save_device_set (写盘)

被调用方:
    - main_app.MainForm._show_speed_fix_dialog → SpeedFixDialog(self).exec_()
"""

import os
import sys

from PyQt5.QtWidgets import (
    QDialog, QFormLayout, QVBoxLayout, QSpinBox, QDoubleSpinBox,
    QDialogButtonBox, QMessageBox,
)

from public_para import g
from settings_io import save_device_set


class SpeedFixDialog(QDialog):
    """转速参数标定子窗口 (设置 → 转速参数标定...)。

    职责:
        提供 5 个输入框 (设备号 / VIni / VMax / SMax / SFix) 供用户修改,
        点击 OK 后把 4 个浮点字段写入 g, 并把全部 5 项 (含设备号) 持久化到
        DeviceSet.txt。

    维护的状态:
        - 临时控件值 (sp_dev / sp_vini / sp_vmax / sp_smax / sp_sfix)
        - 持久化目标: g.VoltageIni / g.VoltageMax / g.SpeedMax / g.SpeedfixNum
                      + DeviceSet.txt 的设备号 (第 1 行)

    典型用法:
        dlg = SpeedFixDialog(parent)
        dlg.exec_()         # 模态显示, 用户确认后自动落盘
    """

    def __init__(self, parent=None):
        """构造对话框, 把 g 的当前值预填到各输入框。

        Args:
            parent: 父窗口 (QMainWindow), 用于模态约束和居中显示

        Returns:
            无

        Side Effects:
            - 创建 5 个 SpinBox 控件
            - 不写盘、不改 g (用户点击 OK 时才写)

        行为不变量 (与原 main_app.py:58-99 一致):
            - 窗口大小固定 380x240
            - 设备号范围 [0, 31]; VIni/VMax 范围 [-10, 10], 3 位小数
            - SpeedMax 范围 [1, 200000], 0 位小数
            - SpeedfixNum 范围 [-5000, 5000], 1 位小数
        """
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

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)

    def _on_ok(self):
        """点击 OK 时的回调: 把控件值写入 g, 并调用 settings_io 落盘。

        Args:
            无

        Returns:
            无 (通过 self.accept() 关闭对话框)

        Side Effects:
            - 修改 g.VoltageIni / g.VoltageMax / g.SpeedMax / g.SpeedfixNum
            - 调用 save_device_set() 覆盖 <exe_dir>/Sysdata/DeviceSet.txt
            - 写盘失败时弹出 QMessageBox.warning, 但仍会 accept() 关闭对话框
              (与原 main_app.py:117-119 行为一致)

        行为不变量:
            - 4 个浮点字段先于落盘写入 g, 即使落盘失败 g 的修改也保留
            - 错误提示文案 '保存失败' / '写入配置文件失败:\\n{e}' 不变
        """
        g.VoltageIni = float(self.sp_vini.value())
        g.VoltageMax = float(self.sp_vmax.value())
        g.SpeedMax = float(self.sp_smax.value())
        g.SpeedfixNum = float(self.sp_sfix.value())

        try:
            save_device_set(int(self.sp_dev.value()))
        except Exception as e:
            QMessageBox.warning(self, '保存失败', f'写入配置文件失败:\n{e}')
        self.accept()
