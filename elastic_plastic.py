"""径向升速数据的弹塑性模型与用户定义的残余变形（mm）。

这是拟合判据，不是卸载测量；不修改采集换算、原始缓冲或 CSV。
"""
from dataclasses import dataclass
import numpy as np
from data_processor import PiecewisePowerFit, piecewise_power_curve


# 数值模型选择的保护条件，不是材料屈服常数。
MIN_R2 = 0.95
MIN_BIC_GAIN = 6.0
EXPONENT_SIGMA = 3.0


@dataclass
class ElasticPlasticFit(PiecewisePowerFit):
    plastic: bool = False
    max_rpm: float = 0.0

    @property
    def max_elastic_deformation(self):
        return self.a1 * self.xc ** 2 if self.plastic else None


def fit_elastic_plastic(xs, ys, min_seg_points=4):
    """第一段固定 a*n²，第二段 p>2，在断点处严格连续。

    调用方仅传入升速包络数据（允许相同转速保载）。单位 RPM、mm。
    用 log 空间最小二乘及 BIC 比较模型，R²仍在原始变形空间计算。
    每段至少四个点和四个不同转速；未检出显著塑性时允许纯弹性模型。
    """
    x, y = np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y) & (x > 0) & (y > 0)
    x, y = x[valid], y[valid]
    n = len(x)
    if n < 2 * min_seg_points or len(np.unique(x)) < min_seg_points:
        return ElasticPlasticFit(False, n_points=n, message='升速有效数据不足')
    order = np.argsort(x, kind='stable')
    x, y = x[order], y[order]
    u, v = np.log(x), np.log(y)
    z = v - 2.0 * u
    floor = n * np.finfo(float).eps ** 2

    def r2(observed, predicted):
        sst = float(np.sum((observed - observed.mean()) ** 2))
        return 1.0 - float(np.sum((observed - predicted) ** 2)) / sst if sst > 0 else -np.inf

    c_elastic = float(z.mean())
    elastic_sse = float(np.sum((z - c_elastic) ** 2))
    elastic_bic = n * np.log(max(elastic_sse, floor) / n) + np.log(n)
    a = float(np.exp(c_elastic))
    elastic_r2 = r2(y, a * x ** 2)
    best, best_bic = None, np.inf
    # 仅在不同转速之间搜索，不把同一次保载拆到两段。
    candidates = np.flatnonzero(np.diff(x) > 0) + 1
    for i in candidates:
        if i < min_seg_points or n - i < min_seg_points:
            continue
        if len(np.unique(x[:i])) < min_seg_points or len(np.unique(x[i:])) < min_seg_points:
            continue
        uc = 0.5 * (u[i - 1] + u[i])
        hinge = np.maximum(u - uc, 0.0)
        design = np.column_stack((np.ones(n), hinge))
        coef, _, rank, _ = np.linalg.lstsq(design, z, rcond=None)
        if rank != 2:
            continue
        c, extra = map(float, coef)
        errors = z - design @ coef
        sse = float(errors @ errors)
        spread = float(np.sum((hinge - hinge.mean()) ** 2))
        se = np.sqrt(sse / max(n - 2, 1) / spread) if spread > 0 else np.inf
        if extra <= max(1e-8, EXPONENT_SIGMA * se):
            continue
        bic = n * np.log(max(sse, floor) / n) + 3 * np.log(n)
        if elastic_bic - bic < MIN_BIC_GAIN:
            continue
        xc, a1, a2 = float(np.exp(uc)), float(np.exp(c)), float(np.exp(c - extra * uc))
        candidate = ElasticPlasticFit(True, a1=a1, b1=2., a2=a2, b2=2. + extra,
                                      xc=xc, n_points=n, plastic=True, max_rpm=float(x[-1]))
        predicted = piecewise_power_curve(candidate, x)
        candidate.r2 = r2(y, predicted)
        if not np.all(np.isfinite(predicted)) or candidate.r2 < MIN_R2:
            continue
        if r2(y[:i], predicted[:i]) < MIN_R2:
            continue
        if bic < best_bic:
            best, best_bic = candidate, bic
    if best is not None:
        return best
    if np.isfinite(elastic_r2) and elastic_r2 >= MIN_R2:
        return ElasticPlasticFit(True, a1=a, b1=2., a2=a, b2=2., xc=float(x[-1]),
                                 r2=elastic_r2, n_points=n, max_rpm=float(x[-1]),
                                 message='当前数据符合二次模型，未检出可靠塑性段')
    return ElasticPlasticFit(False, n_points=n, message='数据不支持可靠的弹性/弹塑性模型')


def measured_residual(fit, rpm, deformation_mm):
    """返回 (残余变形或 None, 显示状态)，不截断实测差值、不作卸载推断。"""
    if fit is None or not fit.ok:
        return None, '待拟合判定'
    if not np.isfinite(rpm) or not np.isfinite(deformation_mm) or rpm <= 0:
        return None, '无有效升速数据'
    if not fit.plastic:
        if rpm > fit.max_rpm:
            return None, '超出弹性拟合范围，请重新拟合'
        return 0.0, '弹性（模型判定）'
    if rpm <= fit.xc:
        return 0.0, '弹性（模型判定）'
    return float(deformation_mm - fit.max_elastic_deformation), '塑性（模型判定）'
