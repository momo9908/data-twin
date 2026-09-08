"""仅负责 UI 几何布局：保持坐标轴内的绘图区为居中的正方形。"""

from PyQt5.QtCore import QEvent, QRectF, QTimer
import pyqtgraph as pg


class SquarePlotWidget(pg.PlotWidget):
    """保留 PlotWidget 的数据/交互接口，不锁定横纵坐标的数值比例。

    外部控件仍填满布局分配的右栏；内部 PlotItem 按实际轴标签、刻度和
    标题尺寸留出空间，使 ViewBox 在右栏中居中，并使用可容纳的最大正方形。
    """

    def __init__(self, *args, **kwargs):
        self._square_ready = False
        self._square_layout_active = False
        super().__init__(*args, **kwargs)
        self.setMinimumHeight(360)
        self._square_timer = QTimer(self)
        self._square_timer.setSingleShot(True)
        self._square_timer.timeout.connect(self._layout_square)
        self.plotItem.getViewBox().sigResized.connect(self._queue_square_layout)
        self.plotItem.geometryChanged.connect(self._queue_square_layout)
        self._square_ready = True
        self._queue_square_layout()

    def _queue_square_layout(self, *_):
        if self._square_ready and not self._square_layout_active:
            self._square_timer.start(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._queue_square_layout()

    def showEvent(self, event):
        super().showEvent(event)
        self._queue_square_layout()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._queue_square_layout()

    def _layout_square(self):
        if self.plotItem is None or not self.isVisible():
            return
        self._square_layout_active = True
        try:
            plot = self.plotItem
            # 布局变化（包括刻度文本宽度变化）可能需两轮才能稳定；仅调整几何。
            for _ in range(4):
                plot.layout.activate()
                view = plot.getViewBox().geometry()
                left = max(0.0, view.left())
                top = max(0.0, view.top())
                right = max(0.0, plot.size().width() - view.right())
                bottom = max(0.0, plot.size().height() - view.bottom())
                area = self.range
                side = max(1.0, min(
                    area.width() - 2.0 * max(left, right),
                    area.height() - 2.0 * max(top, bottom),
                ))
                target = QRectF(
                    area.center().x() - side / 2.0 - left,
                    area.center().y() - side / 2.0 - top,
                    side + left + right,
                    side + top + bottom,
                )
                current = plot.geometry()
                if all(abs(a - b) < 0.05 for a, b in zip(
                    current.getRect(), target.getRect()
                )):
                    break
                plot.setGeometry(target)
            plot.layout.activate()
        finally:
            self._square_layout_active = False
