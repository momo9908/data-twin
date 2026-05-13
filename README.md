# 涡轮超速预应力评估测试控制系统 (Python 版)

研华 USB-4716 数据采集卡 + 应变/转速实时显示 + CSV 数据记录
的完整 Python 实现 — 由原 Delphi 项目移植并精简功能。

## 📦 项目结构

```
turbine_test/
├── public_para.py    # 全局共享参数 (对应 PublicPara.pas)
├── complexs.py       # 复数运算(历史模块,保留用于 test_algorithms.py)
├── ffts.py           # FFT 算法(历史模块,保留用于 test_algorithms.py)
├── daq_device.py     # USB-4716 设备接入 + 模拟器
├── main_app.py       # 主程序 / 主窗口 (应变/转速实时显示)
├── Sysdata/
│   └── DeviceSet.txt # 硬件标定参数
└── README.md
```

## 📋 系统要求

- **操作系统**: Windows 7/10/11 (DAQNavi SDK 仅支持 Windows)
- **Python**: 3.8 ~ 3.11
- **硬件**: 研华 USB-4716 (或同系列卡)
- **驱动**: DAQNavi SDK 4.0.x + USB-4716 设备驱动

## 🔧 安装步骤

### 1. 安装 DAQNavi SDK

按之前确认的安装包顺序:
1. 先装 `DAQNavi_SDK_4.0.4.0.exe`
2. 再装 `DAQNavi_USB4716_3.2.11.0.exe`

安装完成后默认目录:
```
C:\Advantech\DAQNavi\
├── Bin\                            # 核心 DLL
├── Examples\
│   └── Python\
│       └── Automation\
│           └── BDaq\               # ⭐ Python API 包
└── Inc\
```

### 2. 安装 Python 依赖

```bash
pip install PyQt5 pyqtgraph numpy
```

### 3. (可选) 把 DAQNavi Python 包加入 PYTHONPATH

代码会自动尝试三个常见路径:
- `C:\Advantech\DAQNavi\Examples\Python`
- `C:\Program Files\Advantech\DAQNavi\Examples\Python`
- `C:\Program Files (x86)\Advantech\DAQNavi\Examples\Python`

如果你的安装路径不同, 修改 `daq_device.py` 中的 `_candidate_paths`。

## 🚀 运行

```bash
cd turbine_test
python main_app.py
```

### 程序行为

- **若检测到 USB-4716 并能正常打开**: 进入真实采集模式, 标题栏显示
  "涡轮超速预应力评估测试控制系统 - USB-4716,BID#0"
- **若卡未连接或 DAQNavi 不可用**: 自动回落到 `SimulatedDaqDevice`,
  用合成信号(50Hz 主频 + 谐波 + 噪声 + 模拟升速过程)演示完整流程

## 🎛️ 功能对照表

| 原 Delphi 功能 | Python 实现 |
|---|---|
| BufferedAiCtrl 设备配置 | `DaqDevice.configure_*` |
| OnDataReady 数据回调 | `_on_data_ready` (Qt 信号桥接, 线程安全) |
| 应变趋势显示 | `plot_strain` |
| 转速趋势显示 | `plot_speed` |
| DataSaveFlag CSV | `csv_file.write(...)` |
| SpeedFix 子窗口 | `SpeedFixDialog` (QDialog) |
| 转速修正菜单 N1 | "设置 → 转速参数标定" |

## 🛠️ 标定参数 (Sysdata/DeviceSet.txt)

5 行配置:
```
0          # 设备号 (BID#0)
1          # 转速传感器零点电压 (V)
5          # 转速传感器满量程电压 (V)
60000      # 最大转速 (RPM)
0          # 转速修正量 (RPM)
```

可在程序内菜单 "设置 → 转速参数标定" 中可视化修改。

## 📊 数据流图

```
USB-4716 (8 通道 × 10 kHz 采样)
        ↓ DMA → DAQNavi Driver
BufferedAiCtrl 缓冲 (16384 点)
        ↓ 每 8192 点触发 DataReady
get_data → np.array (V) → × TransPara1 → (应变单位)
        ↓
拆分: 通道0=应变, 通道7=转速
        ↓
应变: 通道均值作为实时应变
转速: 平均电压 → 线性映射 → Realspeed (RPM)
        ↓
显示: 应变趋势图 / 转速趋势图
存盘: data\YYYYMMDDxx\xxxxx.CSV
```

## 🐛 常见问题

**Q: 程序启动后提示 "硬件错误,请确认匹配的采集卡连接"**
A: 检查 USB-4716 是否插好, 设备管理器是否能看到该卡, 是否安装了驱动。
   也可在 "Advantech Navigator" 中先做一次 Device Test 确认卡正常。

**Q: 提示 "DAQNavi Python API not found"**
A: DAQNavi SDK 没装, 或 Python 包路径不对。检查
   `C:\Advantech\DAQNavi\Examples\Python\Automation\BDaq` 是否存在。

**Q: 无卡情况下能跑吗?**
A: 能。程序会自动回落到模拟设备 (`SimulatedDaqDevice`),
   能验证整个 UI / 算法链路是否正确。
