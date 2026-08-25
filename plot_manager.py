"""
plot_manager.py
===============
散点图与拟合线管理模块。

职责:
    把原 main_app.py 中分散的绘图相关操作集中起来:
        - 启动新一轮采集时清空散点 + 重置坐标范围 (原 368-371)
        - 实时加散点                       (原 535)
        - 自动缩放坐标 (Timer2 5Hz 节流)    (原 584-597)
        - 导出 PNG 截图                    (原 432-439)

    控件 (PlotWidget / ScatterPlotItem / PlotDataItem) 仍然由 ui_builder
    创建, 本模块只通过 attach() 持有引用; 这样既保留 ui_builder 对样式
    (size=4 / brush='#9467bd' / pen='r' width=2 等) 的统一掌控, 又把行为
    逻辑收纳在本文件。

依赖:
    - numpy
    - pyqtgraph
    - pyqtgraph.exporters (PNG 导出)
    - public_para.g       (本模块未直接读 g, 但保留导入以备扩展; 已删除以避免无用依赖)

被调用方:
    - main_app.MainForm.__init__         → PlotManager() + attach()
    - main_app.MainForm._btn_start_click → reset_for_new_run()
    - main_app.MainForm._on_data_ready   → add_point()
    - main_app.MainForm._on_timer2       → auto_scale(temps, deformation)
    - main_app.MainForm._btn_stop_click  → export_png(full_dir, test_no)
"""

import os
import pyqtgraph as pg
import pyqtgraph.exporters  # noqa: F401  让 pg.exporters.ImageExporter 可用


class PlotManager:
    """封装 plotxy / scatter_xy 两件套的行为逻辑。

    职责:
        - 在采集启动 / 自动缩放 / PNG 导出等场景下统一操控绘图控件
        - 不持有任何与 g 相关的业务状态; 所有数据都由调用方传入

    维护的状态:
        - self.plotxy:     pg.PlotWidget 引用 (由 attach 注入)
        - self.scatter_xy: pg.ScatterPlotItem 引用

    典型用法:
        plot = PlotManager()
        # ... ui_builder 创建好 plotxy / scatter_xy 后:
        plot.attach(plotxy_widget, scatter_item)

        plot.reset_for_new_run()
        plot.add_point(temps, deformation)
        plot.auto_scale(temps, deformation)
        plot.export_png(full_dir, test_no)
    """

    def __init__(self):
        """构造空 PlotManager (尚未持有任何控件引用)。

        Args:
            无

        Returns:
            无

        Side Effects:
            无
        """
        self.plotxy = None
        self.scatter_xy = None
        self.fit_curve = None
        self._xs = []           # 累加的横坐标 (转速) 缓冲, 供拟合读取
        self._ys = []           # 累加的纵坐标 (变形) 缓冲

    def attach(self, plotxy_widget, scatter_item, fit_curve=None) -> None:
        """注入由 ui_builder 创建好的控件引用。

        Args:
            plotxy_widget: pg.PlotWidget 实例
            scatter_item:  pg.ScatterPlotItem 实例 (主散点图层)
            fit_curve:     pg.PlotDataItem 实例 (分段幂函数拟合曲线), 可选

        Returns:
            无

        Side Effects:
            - 仅记录引用; 不会修改控件本身的样式或数据
        """
        self.plotxy = plotxy_widget
        self.scatter_xy = scatter_item
        self.fit_curve = fit_curve

    def reset_for_new_run(self) -> None:
        """开始新一轮采集时调用: 清空散点、清空拟合线、复位坐标范围。

        Args:
            无

        Returns:
            无

        Side Effects:
            - scatter_xy.clear()                  清空所有散点
            - 清空 (转速,变形) 点缓冲与拟合曲线
            - 开启 X/Y 自动量程 (enableAutoRange), 之后随散点与拟合曲线自适应缩放

        行为变更:
            - 不再使用固定初始范围 (0,130)/(0,3000), 改为坐标自适应 (按需求)
        """
        self.scatter_xy.clear()
        self._xs.clear()
        self._ys.clear()
        if self.fit_curve is not None:
            self.fit_curve.setData([], [])
        self.plotxy.enableAutoRange()

    def add_point(self, temps: float, deformation: float) -> None:
        """实时加一个散点 (变形量 μm → mm 后绘制并存入拟合缓冲)。

        Args:
            temps:       横轴值 (转速 RPM)
            deformation: 变形量 (μm, 调用方按 μm 传入)

        Returns:
            无

        Side Effects:
            - scatter_xy 多一个点 (纵坐标已换算为 mm)
            - _xs/_ys 累积 (转速, 变形 mm), 供拟合读取

        行为不变量:
            - 用 addPoints 累积而非覆盖
            - 变形量在本层统一 μm→mm (÷1000), 故下游拟合/曲线/公式也都是 mm
        """
        d_mm = deformation / 1000.0
        self.scatter_xy.addPoints(x=[temps], y=[d_mm])
        self._xs.append(temps)
        self._ys.append(d_mm)

    def get_points(self):
        """返回当前累加的 (转速, 变形) 原始点 (供分段幂函数拟合用)。

        Returns:
            (xs, ys) 两个列表 (副本)

        Side Effects:
            无
        """
        return list(self._xs), list(self._ys)

    def draw_fit_curve(self, xs, ys) -> None:
        """绘制 / 更新分段幂函数拟合曲线。

        Args:
            xs / ys: 拟合曲线的采样点

        Returns:
            无

        Side Effects:
            - fit_curve.setData(xs, ys); fit_curve 未 attach 时静默忽略
        """
        if self.fit_curve is not None:
            self.fit_curve.setData(xs, ys)

    def clear_fit_curve(self) -> None:
        """清空拟合曲线 (拟合失败或复位时)。

        Returns:
            无

        Side Effects:
            - fit_curve.setData([], [])
        """
        if self.fit_curve is not None:
            self.fit_curve.setData([], [])

    def auto_scale(self, temps: float, deformation: float) -> None:
        """5Hz 重申坐标自适应 (由 Timer2 调用), 使视图随数据点与拟合曲线自适应。

        Args:
            temps / deformation: 兼容旧签名; 自适应模式下不再使用

        Returns:
            无

        Side Effects:
            - 重新开启 X/Y enableAutoRange, 使视图自动贴合所有图元 (散点 + 拟合曲线)
              的数据范围, 既能放大也能缩小

        说明:
            改用 pyqtgraph 内置自动量程; 5Hz 重申是为防止用户交互(缩放/平移)后停在
            手动量程, 保持"始终自适应"。
        """
        self.plotxy.enableAutoRange()

    def export_png(self, full_dir: str, test_no: str) -> str:
        """把当前 plotxy 导出为 PNG 截图。

        Args:
            full_dir: 输出目录 (一般是 g.Filestrtemp, 由 make_dated_folder 创建)
            test_no:  文件名前缀 (一般同 CSV 的 test_no)

        Returns:
            导出的 PNG 文件绝对路径

        Side Effects:
            - 在磁盘上写入 <full_dir>/<test_no>.png
            - 失败时直接抛异常, 由调用方 (MainForm._btn_stop_click) 捕获并打印 [WARN]
              (行为与原 main_app.py:432-439 一致)
        """
        exporter = pg.exporters.ImageExporter(self.plotxy.plotItem)
        bmp = os.path.join(full_dir, f'{test_no}.png')
        exporter.export(bmp)
        return bmp
