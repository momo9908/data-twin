"""转速平方显示轴：数据坐标为 n²，刻度文字仍以 n² 的形式书写。"""

import math

from PyQt5.QtGui import QFontMetricsF
import pyqtgraph as pg


class SpeedSquaredAxis(pg.AxisItem):
    """在真实平方坐标上放置刻度；不改变数据、拟合模型或坐标范围。"""

    def __init__(self, orientation='bottom', **kwargs):
        super().__init__(orientation=orientation, **kwargs)
        self.enableAutoSIPrefix(False)

    @staticmethod
    def _label(squared):
        if not math.isfinite(squared) or squared < 0:
            return ''
        return f'{math.sqrt(squared):.12g}²'

    def tickValues(self, minVal, maxVal, size):
        lo, hi = sorted((float(minVal), float(maxVal)))
        if not all(math.isfinite(v) for v in (lo, hi, size)) or hi <= lo or size <= 0:
            return []
        # 保留原有图表的可选对数显示；默认仍是线性的 n² 轴。
        try:
            lower = 10.0 ** lo if self.logMode else max(0.0, lo)
            upper = 10.0 ** hi if self.logMode else hi
        except OverflowError:
            return []
        if upper <= 0 or not math.isfinite(upper):
            return []
        n_min, n_max = math.sqrt(lower), math.sqrt(upper)
        span = n_max - n_min
        if span <= 0:
            return []
        target_count = max(2, min(12, int(size / 90)))
        raw_step = span / target_count
        magnitude = 10.0 ** math.floor(math.log10(raw_step))
        step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw_step)
        first = math.ceil(n_min / step - 1e-12)
        last = math.floor(n_max / step + 1e-12)
        font = self.style.get('tickFont') or self.font()
        metrics = QFontMetricsF(font)
        values = []
        last_pixel = last_width = None
        for index in range(first, last + 1):
            n = index * step
            squared = n * n
            if self.logMode and squared <= 0:
                continue
            position = math.log10(squared) if self.logMode else squared
            if position < lo or position > hi:
                continue
            pixel = (position - lo) / (hi - lo) * size
            width = metrics.horizontalAdvance(self._label(squared))
            # 平方后的刻度间距不均匀；逐个检查屏幕距离，避免低速端标签堆叠。
            if last_pixel is not None and pixel - last_pixel < (last_width + width) / 2 + 12:
                continue
            values.append(position)
            last_pixel, last_width = pixel, width
        return [(None, values)]

    def tickStrings(self, values, scale, spacing):
        labels = []
        for value in values:
            try:
                squared = 10.0 ** value if self.logMode else value
                labels.append(self._label(squared))
            except OverflowError:
                labels.append('')
        return labels
