"""
complexs.py
============
对应 Delphi 单元 Complexs.pas

定义复数运算原语,Python 中直接利用内置 complex 类型,
但为了与原代码保持函数名一致,提供 ComplexAdd / ComplexMag / ComplexPhase 等包装。
"""

import math
import numpy as np
from typing import Union

# Complex number type alias (numpy complex128 or Python complex)
TComplex = complex


# ---------- 常量 ----------
ComplexZero: TComplex = complex(0.0, 0.0)


# ---------- 工厂 ----------
def Complex(re: float, im: float) -> TComplex:
    """构造复数 (对应 Delphi Complex(Re, Im))"""
    return complex(re, im)


# ---------- 基本运算 ----------
def ComplexAdd(c1: TComplex, c2: TComplex) -> TComplex:
    return c1 + c2


def ComplexSub(c1: TComplex, c2: TComplex) -> TComplex:
    return c1 - c2


def ComplexMul(c1: TComplex, c2: TComplex) -> TComplex:
    return c1 * c2


def ComplexScl(scale: float, c: TComplex) -> TComplex:
    """复数标量乘 (Result = scale * c)"""
    return scale * c


# ---------- 幅值与相位 ----------
def ComplexMag(c: TComplex) -> float:
    """模 |c| = sqrt(re² + im²)"""
    return abs(c)


def ComplexPhase(c: TComplex) -> float:
    """相位角(弧度),范围 -π..π,与原 Delphi 实现等价。

    原代码用条件分支避免 ArcTan(0) 错误,Python 用 math.atan2 一次到位。
    """
    if c.real == 0 and c.imag == 0:
        return 0.0
    return math.atan2(c.imag, c.real)


# ---------- 数组向量化版本(高性能,主程序中实际用这些) ----------
def complex_mag_array(arr: np.ndarray) -> np.ndarray:
    """批量取模"""
    return np.abs(arr)


def complex_phase_array(arr: np.ndarray) -> np.ndarray:
    """批量取相位"""
    return np.angle(arr)
