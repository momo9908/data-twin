"""径向升速数据的幂指数判定与用户定义的残余变形（mm）。

这是用户指定的拟合判据，不是卸载测量；不修改采集换算、原始缓冲或 CSV。
"""
from dataclasses import dataclass
import numpy as np
from data_processor import PiecewisePowerFit, piecewise_power_curve


MIN_R2 = 0.95
ELASTIC_EXPONENT_MIN = 1.9
ELASTIC_EXPONENT_MAX = 2.1
# 只吸收最小二乘运算在区间端点处的浮点舍入，不作为测量容差。
_EXPONENT_ROUNDOFF = 1e-12


def exponent_state(exponent):
    """使用未作显示舍入的指数：闭区间 [1.9, 2.1] 弹性，超过 2.1 塑性。"""
    if not np.isfinite(exponent):
        return 'undetermined'
    if exponent > ELASTIC_EXPONENT_MAX + _EXPONENT_ROUNDOFF:
        return 'plastic'
    if exponent >= ELASTIC_EXPONENT_MIN - _EXPONENT_ROUNDOFF:
        return 'elastic'
    return 'undetermined'


@dataclass
class ElasticPlasticFit(PiecewisePowerFit):
    plastic: bool = False
    max_rpm: float = 0.0
    has_elastic_segment: bool = True

    @property
    def max_elastic_deformation(self):
        if self.plastic and self.has_elastic_segment:
            return self.a1 * self.xc ** self.b1
        return None


def fit_elastic_plastic(xs, ys, min_seg_points=4):
    """自由拟合指数，按用户指定的 [1.9, 2.1] / >2.1 规则分类。

    调用方仅传入升速包络数据（允许相同转速保载）。单位 RPM、mm。
    log 空间最小二乘，R²及最优分段选择仍在原始变形空间计算。
    不再使用指数显著性或 BIC 改善门槛。每段至少四个点和四个不同转速。
    如果只有高次幂数据，可以判定塑性特征，但不能编造弹性分界点或残余值。
    """
    x, y = np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y) & (x > 0) & (y > 0)
    x, y = x[valid], y[valid]
    n = len(x)
    if n < 2 * min_seg_points or len(np.unique(x)) < min_seg_points:
        return ElasticPlasticFit(False, n_points=n, message='升速有效数据不足')
    order = np.argsort(x, kind='stable')
    x, y = x[order], y[order]
    # 居中 log(n) 改善条件数，避免 RPM 的数值量级影响指数端点比较。
    log_x, v = np.log(x), np.log(y)
    origin = float(log_x.mean())
    u = log_x - origin

    def r2(observed, predicted):
        sst = float(np.sum((observed - observed.mean()) ** 2))
        return 1.0 - float(np.sum((observed - predicted) ** 2)) / sst if sst > 0 else -np.inf

    try:
        single_coef, _, single_rank, _ = np.linalg.lstsq(
            np.column_stack((np.ones(n), u)), v, rcond=None)
    except np.linalg.LinAlgError:
        return ElasticPlasticFit(False, n_points=n, message='最小二乘求解失败')
    if single_rank != 2:
        return ElasticPlasticFit(False, n_points=n, message='转速跨度不足，无法拟合指数')
    single_c, single_p = map(float, single_coef)
    single_a = float(np.exp(single_c - single_p * origin))
    single_r2 = r2(y, np.exp(single_c + single_p * u))

    best = None
    # 仅在不同转速之间搜索，不把同一次保载拆到两段。
    candidates = np.flatnonzero(np.diff(x) > 0) + 1
    for i in candidates:
        if i < min_seg_points or n - i < min_seg_points:
            continue
        if len(np.unique(x[:i])) < min_seg_points or len(np.unique(x[i:])) < min_seg_points:
            continue
        uc = 0.5 * (u[i - 1] + u[i])
        hinge = np.maximum(u - uc, 0.0)
        design = np.column_stack((np.ones(n), u, hinge))
        try:
            coef, _, rank, _ = np.linalg.lstsq(design, v, rcond=None)
        except np.linalg.LinAlgError:
            continue
        if rank != 3:
            continue
        c, pe, extra = map(float, coef)
        pp = pe + extra
        if exponent_state(pe) != 'elastic' or exponent_state(pp) != 'plastic':
            continue
        log_nc = uc + origin
        xc = float(np.exp(log_nc))
        a1 = float(np.exp(c - pe * origin))
        a2 = float(np.exp(c - pe * origin - extra * log_nc))
        candidate = ElasticPlasticFit(True, a1=a1, b1=pe, a2=a2, b2=pp,
                                      xc=xc, n_points=n, plastic=True, max_rpm=float(x[-1]))
        predicted = piecewise_power_curve(candidate, x)
        candidate.r2 = r2(y, predicted)
        if not np.all(np.isfinite(predicted)) or candidate.r2 < MIN_R2:
            continue
        if r2(y[:i], predicted[:i]) < MIN_R2:
            continue
        if best is None or candidate.r2 > best.r2:
            best = candidate
    if best is not None:
        return best
    state = exponent_state(single_p)
    if np.isfinite(single_r2) and single_r2 >= MIN_R2 and state != 'undetermined':
        plastic = state == 'plastic'
        return ElasticPlasticFit(
            True, a1=single_a, b1=single_p, a2=single_a, b2=single_p,
            xc=0. if plastic else float(x[-1]), r2=single_r2,
            n_points=n, max_rpm=float(x[-1]), plastic=plastic,
            has_elastic_segment=not plastic,
            message='幂指数超过2.1；缺少弹性分界点，残余变形待判定' if plastic
            else '幂指数处于[1.9, 2.1]，判为弹性')
    return ElasticPlasticFit(False, n_points=n,
                             message='拟合不可靠或幂指数低于1.9，弹塑性待判定')


def measured_residual(fit, rpm, deformation_mm):
    """返回 (残余变形或 None, 显示状态)，不截断实测差值、不作卸载推断。"""
    if fit is None or not fit.ok:
        return None, '待拟合判定'
    if not np.isfinite(rpm) or not np.isfinite(deformation_mm) or rpm <= 0:
        return None, '无有效升速数据'
    if fit.plastic and not fit.has_elastic_segment:
        return None, '塑性（指数>2.1），缺少弹性分界点'
    if not fit.plastic:
        if rpm > fit.max_rpm:
            return None, '超出弹性拟合范围，请重新拟合'
        return 0.0, '弹性（模型判定）'
    if rpm <= fit.xc:
        return 0.0, '弹性（模型判定）'
    return float(deformation_mm - fit.max_elastic_deformation), '塑性（模型判定）'
