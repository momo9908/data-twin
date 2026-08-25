"""
data_processor.py
=================
数据解算模块 (纯计算, 无 UI / 文件依赖)。

职责:
    把 DAQ 回调中所有 "从原始电压到物理量" 的换算抽离出来, 包括:
        - 实时转速 RPM 反推       (calc_realspeed)
        - 时域变形量计算           (calc_deformation)
    这些函数只读写 g, 不持有任何 Qt / numpy 之外的引用, 便于独立验证。

依赖:
    - numpy
    - public_para.g (读写全局参数)

被调用方:
    - main_app.MainForm._on_data_ready  → calc_realspeed / calc_deformation
"""

import numpy as np
from dataclasses import dataclass
from public_para import g


def calc_realspeed(speed_channel: np.ndarray, j: int) -> float:
    """从转速通道的电压均值反推实时转速 RPM, 并写入 g.Realspeed。

    Args:
        speed_channel: 转速通道的原始采样数组 (V), 未经 TransPara1 缩放;
                       长度通常为 j 或更大, 内部只取前 j 个
        j: 单通道采样数 (= count // ch_count)

    Returns:
        实时转速 (RPM), 同步写入 g.Realspeed

    Side Effects:
        - 修改 g.Realspeed

    算法 (与原 main_app.py:513-522 数学等价, 但不再依赖 TransPara1):
        sum1 = sum(speed_channel) / j                        # 原电压均值
        若 |VoltageMax - VoltageIni| < 1e-9: Realspeed = 0
        否则: Realspeed = (sum1 - VoltageIni) * SpeedMax / (VoltageMax - VoltageIni)
        若 Realspeed < 0, 截断为 0
        最后 Realspeed += SpeedfixNum  (修正量在截断之后施加, 顺序不可变)

    与修复前的差异 (有意, 解决转速通道单位污染):
        - 修复前: 调用方先 data *= TransPara1, 这里再 / (j * trans_para1) 撤销
        - 修复后: 调用方仅对位移通道乘 TransPara1, 转速通道保持 V, 这里只 / j
        - 数学结果完全等价, 但消除了"乘了又除"的脆弱设计、节省 8192 次乘法、
          并让 calc_realspeed 与位移系数完全解耦
    """
    sum1 = float(np.sum(speed_channel)) / j
    denom = g.VoltageMax - g.VoltageIni
    if abs(denom) < 1e-9:
        g.Realspeed = 0.0
    else:
        g.Realspeed = (sum1 - g.VoltageIni) * g.SpeedMax / denom

    if g.Realspeed < 0:
        g.Realspeed = 0.0
    g.Realspeed += g.SpeedfixNum
    return g.Realspeed


def calc_deformation(dis_channel: np.ndarray, N: int):
    """计算当前间距 Dis2、变形量 deformation、横轴 temps。

    Args:
        dis_channel: 已乘以 TransPara1 后的位移通道采样数组 (μm 量纲)
        N: 时域均值窗口长度 (= daq.interval_count, 原代码用 8192)

    Returns:
        (dis2, deformation, temps) 三元组:
            - dis2:        当前间距 (μm), = -mean(dis_channel[:N])  (负号源自原代码)
            - deformation: 变形量 (μm), = dis2 - g.Dis0
            - temps:       散点图横轴值 = 实时转速 g.Realspeed (RPM, 不再平方)
        若 len(dis_channel) < N, 返回 (None, None, None), 由调用方跳过本帧。

    Side Effects:
        - 修改 g.Dis2 (写入新的当前间距)

    行为不变量:
        - Dis2 必须保留负号 (原 main_app.py:530)
        - temps 即实时转速 Realspeed (RPM), 直接作为散点图横坐标
        - 当采样不足时返回 None 三元组而非 0 (与原 main_app.py:526-527 一致)
    """
    if len(dis_channel) < N:
        return (None, None, None)
    recovered = dis_channel[:N]
    g.Dis2 = -float(np.mean(recovered))
    deformation = g.Dis2 - g.Dis0
    temps = g.Realspeed
    return (g.Dis2, deformation, temps)


# =============================================================================
# 分段幂函数拟合 (转速-变形)
# =============================================================================
@dataclass
class PiecewisePowerFit:
    """两段幂函数拟合结果。

    模型 (转速 x → 变形 y):
        x <  xc :  y = a1 * x**b1
        x >= xc :  y = a2 * x**b2

    字段:
        ok:        是否拟合成功
        a1, b1:    左段 (低转速) 系数与幂次
        a2, b2:    右段 (高转速) 系数与幂次
        xc:        断点转速 (RPM)
        r2:        整体确定系数 R^2 (线性空间, 对全部有效点计算)
        n_points:  参与拟合的有效点数 (已剔除 x<=0 / y<=0)
        message:   失败原因 (ok=False 时)
    """
    ok: bool
    a1: float = 0.0
    b1: float = 0.0
    a2: float = 0.0
    b2: float = 0.0
    xc: float = 0.0
    r2: float = 0.0
    n_points: int = 0
    message: str = ''


def fit_piecewise_power(xs, ys, min_seg_points: int = 4) -> PiecewisePowerFit:
    """对 (转速 xs, 变形 ys) 做两段幂函数最小二乘拟合, 自动搜索最优断点。

    Args:
        xs: 转速序列 (RPM)
        ys: 变形序列 (μm)
        min_seg_points: 每段至少参与拟合的点数 (默认 4)

    Returns:
        PiecewisePowerFit: ok=True 时含两段系数 / 断点 / R^2; 失败时 ok=False
        且 message 给出原因。两段在断点处*严格连续* (a1*xc**b1 == a2*xc**b2)。

    Side Effects:
        无 (纯计算; 仅依赖 numpy, 不需要 scipy)

    算法 (log-log 空间连续折线最小二乘, 保证连续、稳定):
        - 仅用 x>0 且 y>0 的有限点 (幂函数定义域; 升速段变形一般为正)。
        - 令 u=ln x, v=ln y。幂函数取对数后为直线, "连续两段幂" 等价于 (u,v)
          上一条带拐点的连续折线。用铰链基拟合:
              v ≈ c0 + c1*u + c2*max(u - uc, 0)
          它在拐点 uc 天然连续; 用 np.linalg.lstsq 解 (c0,c1,c2)。
          反解: 左段 b1=c1, a1=exp(c0); 右段 b2=c1+c2, a2=exp(c0 - c2*uc);
          断点 xc=exp(uc), 连续性 a2*xc**b2 == a1*xc**b1 自动成立。
        - 候选拐点取排序后相邻点 (log) 中点, 保证左右各 >= min_seg_points 点;
          取使整体 R^2 (=1-SSE/SST, 在*线性空间*对全部有效点计算) 最大者。
    """
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y) & (x > 0) & (y > 0)
    x, y = x[mask], y[mask]
    n = int(x.size)
    if n < 2 * min_seg_points:
        return PiecewisePowerFit(
            ok=False, n_points=n,
            message=f'有效数据点不足 ({n}), 每段至少需 {min_seg_points} 点')

    order = np.argsort(x)
    x, y = x[order], y[order]
    u = np.log(x)
    v = np.log(y)

    sst = float(np.sum((y - np.mean(y)) ** 2))
    if sst <= 0:
        return PiecewisePowerFit(ok=False, n_points=n, message='变形值无变化, 无法拟合')

    best = None
    ones = np.ones_like(u)
    for i in range(min_seg_points, n - min_seg_points + 1):
        uc = 0.5 * (u[i - 1] + u[i])
        # 铰链基 [1, u, max(u-uc,0)] → log-log 上的连续折线
        A = np.column_stack([ones, u, np.maximum(u - uc, 0.0)])
        try:
            coef, *_ = np.linalg.lstsq(A, v, rcond=None)
        except Exception:
            continue
        c0, c1, c2 = float(coef[0]), float(coef[1]), float(coef[2])
        b1, b2 = c1, c1 + c2
        a1 = float(np.exp(c0))
        xc = float(np.exp(uc))
        a2 = float(np.exp(c0 - c2 * uc))   # 由连续性确定, 断点处无跳变
        yp = np.where(x < xc, a1 * np.power(x, b1), a2 * np.power(x, b2))
        sse = float(np.sum((y - yp) ** 2))
        r2 = 1.0 - sse / sst
        if best is None or r2 > best.r2:
            best = PiecewisePowerFit(ok=True, a1=a1, b1=b1, a2=a2, b2=b2,
                                     xc=xc, r2=r2, n_points=n)
    if best is None:
        return PiecewisePowerFit(ok=False, n_points=n, message='拟合失败 (lstsq 未得解)')
    return best


def piecewise_power_curve(fit: PiecewisePowerFit, x_array):
    """按拟合结果在 x_array 上计算分段幂曲线 y (用于绘制拟合曲线)。

    Args:
        fit:     fit_piecewise_power 的成功结果
        x_array: 横坐标采样点 (转速 RPM, 应为正)

    Returns:
        y 数组 (与 x_array 等长): x<xc 用左段, x>=xc 用右段

    Side Effects:
        无 (纯计算)
    """
    x = np.asarray(x_array, dtype=float)
    yl = fit.a1 * np.power(x, fit.b1)
    yr = fit.a2 * np.power(x, fit.b2)
    return np.where(x < fit.xc, yl, yr)