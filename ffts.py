"""
ffts.py
========
对应 Delphi 单元 FFTs.pas

提供 ForwardFFT / InverseFFT 接口,与原 Delphi 调用方式保持一致。
内部用 numpy.fft 实现 —— 这是世界上最快的 FFT 实现之一(FFTW/Intel MKL),
功能完全等价于原 Delphi 的混合基数(2/3/4/5/8/10 + 大素因子通用 DFT) FFT。

数值精度: numpy 使用 double(64-bit),比 Delphi 的 single(32-bit) 更高。
"""

import numpy as np
from typing import Sequence, List

from complexs import TComplex


def ForwardFFT(source: Sequence[TComplex],
               dest: List[TComplex],
               count: int) -> None:
    """正向 FFT (对应 ForwardFFT)

    Args:
        source: 输入复数序列(长度 >= count)
        dest: 输出缓冲(长度 >= count),原地写入
        count: 变换点数

    Notes:
        与原 Delphi 一致, source 与 dest 可以相同(原地变换)。
    """
    if count == 0:
        return

    # 转 numpy 数组
    src = np.asarray(source[:count], dtype=np.complex128)
    # numpy FFT
    res = np.fft.fft(src)
    # 写回 dest(保持调用方的可变序列语义)
    for i in range(count):
        dest[i] = complex(res[i])


def InverseFFT(source: Sequence[TComplex],
               dest: List[TComplex],
               count: int) -> None:
    """反向 FFT (对应 InverseFFT)

    与原 Delphi 算法一致:
        InverseFFT(x) = (1/N) * conj( ForwardFFT( conj(x) ) )
    numpy.fft.ifft 已包含 1/N 归一化,所以可以直接调用。
    """
    if count == 0:
        return

    src = np.asarray(source[:count], dtype=np.complex128)
    res = np.fft.ifft(src)
    for i in range(count):
        dest[i] = complex(res[i])


# ---------- 高性能数组接口(主程序实际使用) ----------
def fft_array(data: np.ndarray) -> np.ndarray:
    """正向 FFT,numpy 数组进出"""
    return np.fft.fft(data)


def ifft_array(data: np.ndarray) -> np.ndarray:
    """反向 FFT,numpy 数组进出"""
    return np.fft.ifft(data)
