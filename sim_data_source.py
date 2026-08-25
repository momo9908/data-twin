"""
sim_data_source.py
==================
模拟数据源加载与查表模块。

职责:
    从 仿真数据源.xlsx 读取 (RPM, 变形) 曲线, 缓存到内存,
    提供 lookup_deformation(rpm) 接口供 SimulatedDaqDevice 使用,
    让模拟器输出的"位移 / 转速"两通道电压在下游 calc_realspeed /
    calc_deformation 解算后能严格还原到 Excel 表中的真实物理量。

依赖:
    - openpyxl (仅在使用 Excel 数据时需要; 缺失时静默回退到 None,
      不影响程序启动)
    - numpy

被调用方:
    - daq_device.SimulatedDaqDevice.__init__ → load_sim_profile()
    - daq_device.SimulatedDaqDevice._produce  → lookup_deformation()

Excel 文件格式约定 (仿真数据源.xlsx):
    第 1 行: 列标题 (如 'P1 - 旋转速度 大小' / 'P16 - 几何体径向变形 最大')
    第 2 行: 单位行 (如 'rev min^-1' / 'mm')
    第 3 行起: 数据
        A 列: RPM (int 或 float, 单位 rev/min)
        B 列: 变形量 (float, 单位 mm)
        若 B 列为空 (None), 整行被丢弃 (实际表中 RPM 16300~20000 段无变形)

被调用方:
    - daq_device.SimulatedDaqDevice 在初始化时一次性加载, 之后只查表
"""

import os
import sys
import numpy as np


# 默认 Excel 文件名 (相对于程序所在目录)
DEFAULT_XLSX_NAME = '仿真数据源.xlsx'


def _exe_dir() -> str:
    """返回程序所在目录的绝对路径。

    Args:
        无

    Returns:
        sys.argv[0] 所在目录的绝对路径

    Side Effects:
        无 (纯路径计算)

    实现说明:
        与 settings_io._exe_dir 等价。在源码方式启动和 PyInstaller 打包后
        行为均正确。
    """
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def load_sim_profile(path: str = None):
    """从 Excel 文件加载 (RPM, 变形_μm) 双数组, 按 RPM 升序排好。

    Args:
        path: Excel 文件绝对路径; 默认使用 <exe_dir>/仿真数据源.xlsx

    Returns:
        (rpm_arr, def_um_arr) 二元 tuple:
            - rpm_arr:    shape (N,), float64, 单位 RPM
            - def_um_arr: shape (N,), float64, 单位 μm (= 表中 mm 列 × 1000)
        若加载失败 (文件不存在 / openpyxl 缺失 / 数据全空), 返回 (None, None),
        调用方应回退到旧的合成波形, 程序不应崩溃。

    Side Effects:
        - 仅读盘, 不修改任何外部状态
        - 加载成功时打印 [INFO] 日志, 含点数和范围
        - 加载失败时打印 [INFO] 或 [WARN] 日志

    数据解析规则:
        - 跳过前 2 行 (表头 + 单位行)
        - 仅保留 A 列 RPM 和 B 列变形 都非 None 的行
        - 变形单位 mm → 内部统一用 μm (× 1000)
        - 按 RPM 升序排序 (稳妥, Excel 本身已升序)
    """
    if path is None:
        path = os.path.join(_exe_dir(), DEFAULT_XLSX_NAME)

    if not os.path.exists(path):
        print(f'[INFO] 仿真数据源 Excel 不存在: {path}')
        return (None, None)

    try:
        import openpyxl
    except ImportError:
        print('[INFO] 未安装 openpyxl, 模拟器将使用合成波形')
        return (None, None)

    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        ws = wb.active
        rpm_list, def_list = [], []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i < 2:
                continue                        # 跳过 2 行表头
            if row[0] is None or row[1] is None:
                continue                        # 丢弃空值行
            rpm_list.append(float(row[0]))
            def_list.append(float(row[1]) * 1000.0)  # mm → μm
        wb.close()

        if not rpm_list:
            print('[INFO] 仿真数据源 Excel 无有效数据行')
            return (None, None)

        rpm_arr = np.array(rpm_list, dtype=np.float64)
        def_arr = np.array(def_list, dtype=np.float64)

        # 按 RPM 升序排序 (Excel 本身已升序, 但稳妥起见再排一次)
        idx = np.argsort(rpm_arr)
        rpm_arr = rpm_arr[idx]
        def_arr = def_arr[idx]

        print(
            f'[INFO] 加载仿真数据源: {len(rpm_arr)} 点, '
            f'RPM {rpm_arr[0]:.0f}~{rpm_arr[-1]:.0f}, '
            f'def {def_arr.min():.2f}~{def_arr.max():.2f} μm'
        )
        return (rpm_arr, def_arr)

    except Exception as ex:
        print(f'[WARN] 加载仿真数据源失败: {ex}')
        return (None, None)


def lookup_deformation(rpm: float, rpm_arr: np.ndarray,
                       def_um_arr: np.ndarray) -> float:
    """按当前 RPM 在曲线上做线性插值, 得到对应变形量 (μm)。

    Args:
        rpm:        当前目标转速 (RPM); 允许超出表范围
        rpm_arr:    load_sim_profile 返回的 RPM 数组 (已升序)
        def_um_arr: load_sim_profile 返回的变形数组 (μm), 与 rpm_arr 同长

    Returns:
        线性插值得到的变形量 (μm) (float)
        - rpm <  rpm_arr[0]:   返回 def_um_arr[0]   (左侧饱和)
        - rpm >  rpm_arr[-1]:  返回 def_um_arr[-1]  (右侧饱和, 避免外推爆数)
        - 其他: np.interp 线性插值

    Side Effects:
        无 (纯函数)

    选用 np.interp 的理由:
        - 自动处理两端饱和 (避免在表外做线性外推产生不切实际的巨大变形)
        - 内部 C 实现, 单次调用 ~ μs 级, 不会成为 _produce 的瓶颈
    """
    return float(np.interp(rpm, rpm_arr, def_um_arr))
