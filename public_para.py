"""
public_para.py
==============
对应 Delphi 单元 PublicPara.pas

提供全局共享变量,在多个模块之间传递状态。
(纯时域精简版：已彻底删除所有频域、FFT、振幅相关的全局变量)
"""

class PublicPara:
    """全局参数容器"""

    def __init__(self):
        # ---------- 转换系数 ----------
        self.TransPara1: float = 0.0

        # ---------- 传感器灵敏度 ----------
        self.Sensitivity1: float = 1.0 / 1.8   # 默认激光位移 LK-H080

        # ---------- 数据保存标志 ----------
        self.DataSaveFlag: bool = False

        # ---------- 路径/文件名 ----------
        self.Filestr: str = ''
        self.Bmbstr: str = ''
        self.Filestrtemp: str = ''
        self.TestNoTemp: str = ''

        # ---------- 间距及变形参数 ----------
        self.Dis0: float = 0.0   # 初始间距
        self.Dis1: float = 0.0   # 中间间距
        self.Dis2: float = 0.0   # 当前间距
        self.Deformation0: float = 0.0

        # ---------- 时间参数 ----------
        self.Savetime: float = 2000.0    # 采集时长(秒)
        self.Realspeed: float = 0.0      # 实时转速 RPM

        # ---------- 硬件标定参数(从 DeviceSet.txt 读取) ----------
        self.VoltageIni: float = 1.0    # 转速传感器零点电压
        self.VoltageMax: float = 5.0    # 转速传感器满量程电压
        self.SpeedMax: float = 60000.0  # 最大转速 RPM
        self.SpeedfixNum: float = 0.0   # 转速修正量


# 全局单例(模块导入即可访问)
g = PublicPara()

# 浮点常量
cMinFloat = 1.5e-45
cMaxFloat = 3.4e38