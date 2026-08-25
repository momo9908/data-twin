"""
settings_io.py
==============
DeviceSet.txt 配置文件读写模块。

职责:
    把原 main_app.py 中分散在 SpeedFixDialog._on_ok (107-118) 和
    MainForm._form_create (321-331) 的 DeviceSet.txt 读/写逻辑抽取出来,
    集中维护文件路径解析和 5 行格式 (设备号 / VIni / VMax / SMax / SFix)。

文件格式 (5 行, UTF-8, LF 换行):
    第 1 行: device_number (整数)
    第 2 行: g.VoltageIni  (浮点, 转速传感器零点电压)
    第 3 行: g.VoltageMax  (浮点, 转速传感器满量程电压)
    第 4 行: g.SpeedMax    (浮点, 最大转速 RPM)
    第 5 行: g.SpeedfixNum (浮点, 转速修正量)

依赖: public_para.g (只写校准字段, 不读)

被调用方:
    - main_app.MainForm._form_create        → load_device_set()
    - dialogs.SpeedFixDialog._on_ok         → save_device_set(device_number)
"""

import os
import sys
from public_para import g


# 配置文件相对路径 (相对于 exe 所在目录)
DEFAULT_DEVICE_SET = ('Sysdata', 'DeviceSet.txt')


def _exe_dir() -> str:
    """获取可执行程序所在目录。

    Args:
        无

    Returns:
        exe 所在目录的绝对路径; 在源码方式启动时返回 main_app.py 所在目录。

    Side Effects:
        无 (纯路径计算)

    实现:
        与原 main_app.py:108 / 131 一致, 使用 os.path.dirname(os.path.abspath(sys.argv[0]))。
        该写法在 python main_app.py 启动和 PyInstaller 打包成 exe 后行为均正确。
    """
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def device_set_path() -> str:
    """返回 DeviceSet.txt 的绝对路径。

    Args:
        无

    Returns:
        <exe_dir>/Sysdata/DeviceSet.txt 的绝对路径

    Side Effects:
        无
    """
    return os.path.join(_exe_dir(), *DEFAULT_DEVICE_SET)


def load_device_set() -> int:
    """读取 DeviceSet.txt, 把 4 个校准参数写入 g, 返回 device_number。

    Args:
        无

    Returns:
        device_number (int): 第 1 行的设备编号; 文件不存在时返回 0 且不修改 g。

    Side Effects:
        - 当文件存在时, 写入 g.VoltageIni / g.VoltageMax / g.SpeedMax / g.SpeedfixNum
        - 当文件不存在时, g 的字段保持不变 (沿用 public_para 中的默认值)

    行为不变量 (与原 main_app.py:321-331 一致):
        - 文件不存在时 device_number = 0 且不抛异常
        - 用 UTF-8 编码读取; 跳过空行 (使用 strip() 过滤)
        - lines[0] → device_number, lines[1..4] 依次写入 g 的 4 个字段
    """
    path = device_set_path()
    if not os.path.exists(path):
        return 0
    with open(path, 'r', encoding='utf-8') as f:
        lines = [ln.strip() for ln in f.readlines() if ln.strip()]
    device_number = int(lines[0])
    g.VoltageIni = float(lines[1])
    g.VoltageMax = float(lines[2])
    g.SpeedMax = float(lines[3])
    g.SpeedfixNum = float(lines[4])
    return device_number


def save_device_set(device_number: int) -> None:
    """把当前 g 中的 4 个校准参数加上 device_number 写入 DeviceSet.txt。

    Args:
        device_number: 要写入第 1 行的设备编号 (会被 int() 强转)

    Returns:
        无

    Side Effects:
        - 创建 (如不存在) <exe_dir>/Sysdata/ 目录
        - 覆盖写入 DeviceSet.txt (UTF-8 编码, 5 行)

    行为不变量 (与原 main_app.py:107-118 一致):
        - 编码 UTF-8, 行尾使用 Python 默认 ('\\n', 不强制 CRLF)
        - 第 1 行强制 int 化, 其余 4 行直接 str(float) 输出
        - 失败时由调用方 (SpeedFixDialog._on_ok) 捕获异常弹窗提示
    """
    path = device_set_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f'{int(device_number)}\n')
        f.write(f'{g.VoltageIni}\n')
        f.write(f'{g.VoltageMax}\n')
        f.write(f'{g.SpeedMax}\n')
        f.write(f'{g.SpeedfixNum}\n')
