"""
public_para.py
==============
对应 Delphi 单元 PublicPara.pas

提供全局共享变量,在多个模块之间传递状态。
使用单例 g 来统一访问,等价于原 Delphi 的全局变量区。
"""


class PublicPara:
    """全局参数容器(与 Delphi PublicPara 变量段对应)。"""

    def __init__(self):
        # ---------- 频带阻参数 ----------
        self.StopFreqLow: int = 0    # 低频截止
        self.StopFreqHigh: int = 0   # 高频截止

        # ---------- 转换系数 ----------
        # TransPara1 = 1000 / Sensitivity1, V→μm
        # 例如灵敏度 1.8 mV/μm 时, 1V 对应 1000/1.8 = 555.56 μm
        self.TransPara1: float = 0.0
        self.TransPara2: float = 0.0
        self.TransPara3: float = 0.0

        # ---------- 8 通道振动值(峰峰值) ----------
        self.Vib1: float = 0.0
        self.Vib2: float = 0.0
        self.Vib3: float = 0.0
        self.Vib4: float = 0.0
        self.Vib5: float = 0.0
        self.Vib6: float = 0.0
        self.Vib7: float = 0.0
        self.Vib8: float = 0.0
        # 原 Delphi 还预留了 Vib9..Vib15, Speed16, 这里按需补齐
        self.Vib9 = self.Vib10 = self.Vib11 = self.Vib12 = 0.0
        self.Vib13 = self.Vib14 = self.Vib15 = 0.0
        self.Speed16: float = 0.0

        # ---------- 传感器灵敏度 ----------
        self.Sensitivity1: float = 1.0 / 1.8   # 默认电涡流位移传感器
        self.Sensitivity2: float = 1.0

        # ---------- 数据保存标志 ----------
        self.DataSaveFlag: bool = False

        # ---------- 路径/文件名 ----------
        self.Filestr: str = ''
        self.Bmbstr: str = ''
        self.Filestrtemp: str = ''
        self.TestNoTemp: str = ''

        # ---------- 位移参数 ----------
        self.Dis0: float = 0.0   # 初始位移
        self.Dis1: float = 0.0   # 中间位移
        self.Dis2: float = 0.0   # 当前位移
        self.Deformation0: float = 0.0

        # ---------- 拟合参考点(转速²/10⁶) ----------
        self.Rpm1: float = 40.0
        self.Rpm2: float = 60.0

        # ---------- 时间参数 ----------
        self.Savetime: float = 0.0    # 采集时长(秒)
        self.Realspeed: float = 0.0   # 实时转速 RPM
        self.steptime: int = 2        # 瀑布图时间步长(秒)
        self.freqarr: int = 0         # 当前频谱缓存索引
        self.recordnum: int = 0       # 已记录帧数

        # ---------- 线性拟合 ----------
        # Deformation = Fxa * (Speed^2/1e6) + Fxb
        self.CurveTime: float = 0.0
        self.Fxa: float = 10.0
        self.Fxb: float = 0.0
        self.LeftDF: float = 0.0      # 拟合残差

        # ---------- 累加器(20+1 个槽,与 Delphi array[0..20] 一致) ----------
        self.SumD1 = [0.0] * 21
        self.SumD2 = [10.0] * 21      # 初值 10 (来自原 FormCreate)
        self.SumS1 = [0.0] * 21
        self.SumS2 = [1.0] * 21       # 初值 1
        self.SumCount1: int = 0
        self.SumCount2: int = 0

        # ---------- FFT/瀑布图启用标志 ----------
        self.Freqenable: bool = False

        # ---------- 硬件标定参数(从 DeviceSet.txt 读取) ----------
        self.VoltageIni: float = 1.0    # 转速传感器零点电压
        self.VoltageMax: float = 5.0    # 转速传感器满量程电压
        self.SpeedMax: float = 60000.0  # 最大转速 RPM
        self.SpeedfixNum: float = 0.0   # 转速修正量


# 全局单例(模块导入即可访问)
g = PublicPara()


# 浮点常量(对应 Delphi 中 cMinFloat / cMaxFloat)
cMinFloat = 1.5e-45
cMaxFloat = 3.4e38
