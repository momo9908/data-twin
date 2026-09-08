# HRSTD 在线变形测量系统（Python 版）

面向高速旋转轮盘试验的桌面测量与分析程序。项目由原 Delphi 系统迁移并模块化重构，以研华 USB-4716 为主要采集硬件，同时内置模拟数据源，可在无采集卡环境下验证径向变形、振动分析、转速保载识别和 DIC 数据关联流程。

主界面包含三个功能页：

- **径向位移测量**：实时转速与变形解算、连续两段幂函数拟合、CSV 和图像保存；
- **振动信号分析**：FFT、频域滤波、峰峰值、主频/相位、趋势回放和瀑布谱导出；
- **DIC 应变分析**：离线读取 DIC Excel，计算全场最大 Mises 等效应变以及指定截面的平均周向、平均径向应变。

> 本项目用于试验测量与研究分析。高速旋转试验具有较高风险，软件输出不能替代经验证的硬件联锁、超速保护和试验安全规程。

## 主要功能

### 1. 径向位移测量

- 支持8通道采集，位移通道与转速通道可选；
- 根据传感器灵敏度将电压换算为当前间距和径向变形；
- 实时绘制“转速（RPM）—变形量”散点图；
- 支持位置归零、目标塑性变形和完成率显示；
- 对正转速、正变形数据执行**连续两段幂函数拟合**：
  - 自动搜索最优分界转速；
  - 每段至少4个有效点；
  - 两段函数在分界点严格连续；
  - 输出拟合函数、分界转速和整体线性空间的 `R²`；
- 拟合结果追加写入本次径向 CSV，停止采集时自动导出散点/拟合图 PNG。

### 2. 振动信号分析

- 振动通道、转速通道、采样率和分析点数独立设置；
- FFT 幅值谱与相位谱双轴显示；
- 计算滤波后时域峰峰值、主频、主频幅值和相位；
- 支持两种频域处理口径：
  - **Delphi 历史兼容口径**：便于与历史 CSV 数据对比；
  - **修正口径**：采用正负频率对称带通，反映物理真实振幅；
- 实时显示振动峰峰值—时间趋势，鼠标悬停可回放相应时刻的历史频谱；
- 支持瀑布谱缓存与 CSV 导出，列标题使用各时间槽的实测转速；
- 停止采集时自动保存振动 CSV 和 FFT 频谱 PNG。

### 3. 转速保载识别与 DIC 联动

- 使用滑动窗口实时识别稳定保载状态；
- 进入保载后弹出“可以开始 DIC 采样”的非模态提示；
- 记录保载转速，并按分析顺序自动提供给 DIC 页面；
- 统计判据计算、触发处理、跨线程投递和界面显示时延；
- 时延报告仅在后台计算和更新，不自动弹出或置顶，也不新增文件保存；保留 100 ms
  阈值判断及控制台输出，计时终点仍为 DIC 采样提示窗首次绘制完成；
- 真卡采集中若 USB-4716 断开，程序会自动停止采集并关闭数据文件，尽可能保留已采数据。

### 4. DIC 应变分析

- 扫描 `DICdata/*.xlsx`，忽略 Excel 临时锁文件；
- 使用 `openpyxl` 只读流式处理大型工作簿；
- 将首个工作表作为参考态，其余工作表作为不同时刻的变形状态；
- 计算并显示：
  - 全时刻、全场最大 Mises 等效应变及其测点位置；
  - 指定子午截面内的平均周向 Mises 应变最大值；
  - 指定圆柱截面内的平均径向 Mises 应变最大值；
- 将三类应变结果与对应转速追加到散点图；
- 可导出累计散点、最大点坐标、数据源文件及分析参数。

DIC Excel 至少应包含以下表头：

- `X坐标`
- `Y坐标`
- 名称中包含 `Mises` 的应变列

建议同时提供 `行ROW`、`列COL`，`Z坐标`为可选列。

## 系统架构

### 绘图区布局

径向位移测量的“变形量—转速”和 DIC 应变分析的“应变—转速”图，均将
**坐标轴内实际绘图区**保持为正方形，而非仅将整个图表控件设为正方形。

- 窗口缩放、最大化及切换选项卡后，绘图区在右栏中尽量放大并居中，多余空间留白；
- 左侧操作区、控件顺序、散点/拟合线样式、图例及原有交互保持不变；
- 标题、轴标签和刻度占用的空间会计入布局，窗口最小尺寸为 1100 × 760
  个 Qt 逻辑像素（若现有控件需要更多空间，则以布局计算的最小尺寸为准）；
- 正方形是显示几何约束，不是横纵坐标单位等比；不会修改数据、单位、计算公式
  或原有坐标缩放设置；
- 振动分析绘图不作调整；PNG 导出仍走原有流程，含标题和轴标签的图片外框不保证 1∶1。

本次仅调整 UI 布局，未变更依赖版本、数值计算后端或采集/分析逻辑，也未重新打包 EXE。

### 数据流程

```text
USB-4716 / SimulatedDaqDevice
             │
             ▼
       daq_device.py
             │ 原始交错电压帧
             ▼
        main_app.py
       ┌─────┴───────────────────┐
       │                         │
       ▼                         ▼
径向处理流水线               振动处理流水线
data_processor.py            vibration_processor.py
plot_manager.py              vibration_tab.py
       │                         │
       ├─ 径向 CSV               ├─ 振动 CSV
       └─ 散点/拟合 PNG          ├─ FFT PNG
                                 └─ 瀑布谱 CSV

实时转速 ──► speed_stability.py
                  │
                  ├─ 保载提示与 latency_report.py
                  └─ 保载转速传递给 DIC 页面

DIC Excel ──► dic_analysis.py ──► dic_tab.py
                                 ├─ 最大/截面平均应变
                                 ├─ 应变—转速散点
                                 └─ DIC 散点 CSV
```

## 模块说明

| 文件 | 作用 |
|---|---|
| `main_app.py` | 程序入口、共享采集会话和事件编排 |
| `ui_builder.py` | 主窗口、菜单及三个功能页的布局构建 |
| `square_plot_widget.py` | 共用 UI 控件：动态保持实际绘图区为正方形并居中 |
| `daq_device.py` | DAQNavi 真卡接入、设备枚举和模拟采集器 |
| `data_processor.py` | 转速/位移解算和连续两段幂函数拟合 |
| `plot_manager.py` | 径向散点、拟合曲线和 PNG 导出 |
| `vibration_processor.py` | FFT、滤波、峰峰值、主峰分析和瀑布缓存 |
| `vibration_tab.py` | 振动分析界面及振动文件生命周期 |
| `dic_analysis.py` | DIC Excel 的只读计算和命令行入口 |
| `dic_tab.py` | DIC 分析界面、保载转速关联、绘图与导出 |
| `speed_stability.py` | 转速保载状态机 |
| `latency_report.py` | 保载判定至提示窗显示的时延统计 |
| `csv_logger.py` | 试验目录、CSV 格式和文件生命周期 |
| `settings_io.py` | 设备标定参数的读取与保存 |
| `dialogs.py` | 转速参数标定窗口 |
| `sim_data_source.py` | 仿真 Excel 曲线的加载与插值 |
| `public_para.py` | 跨模块共享参数 |

## 环境要求

### 基础环境

- Windows 10/11；
- 推荐 Python 3.10 或 3.12；
- 真卡模式所用 Python 版本还需受到 DAQNavi SDK 的支持。

安装 Python 依赖：

```powershell
python -m pip install numpy PyQt5 pyqtgraph openpyxl
```

依赖用途：

- `numpy`：采集数据解算、FFT、拟合和模拟信号；
- `PyQt5`：桌面界面和模拟器定时驱动；
- `pyqtgraph`：实时绘图和 PNG 导出；
- `openpyxl`：读取仿真数据源和 DIC Excel。

项目不依赖 SciPy、Pandas 或 Matplotlib。缺少 `openpyxl` 时主程序仍可启动，但 DIC Excel 分析不可用，仿真数据源会回退到内置合成数据。

### 真卡模式

使用真实 USB-4716 时还需要：

- 研华 USB-4716 及对应驱动；
- DAQNavi SDK 4.0.x；
- SDK 提供的 `Automation.BDaq` Python 包。

程序会自动搜索以下 SDK Python 路径：

```text
C:\Advantech\DAQNavi\Examples\Python
C:\Program Files\Advantech\DAQNavi\Examples\Python
C:\Program Files (x86)\Advantech\DAQNavi\Examples\Python
```

如果 SDK 安装在其他位置，请将包含 `Automation` 文件夹的上级目录加入 `PYTHONPATH`。硬件层使用 DAQNavi `WaveformAiCtrl`；启动时程序会枚举并选取首个非 Demo 设备。

## 安装与运行

```powershell
git clone https://github.com/momo9908/data-twin.git
cd data-twin

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install numpy PyQt5 pyqtgraph openpyxl

python main_app.py
```

程序没有额外命令行开关：

- 检测到真实设备且 DAQNavi 可用时，自动进入真卡模式；
- DAQNavi 不可用或未检测到真实设备时，自动进入模拟模式。

建议在真实试验前先使用 Advantech Navigator 完成 Device Test，并在启动程序前连接采集卡。

## 模拟模式

模拟器会优先读取仓库根目录的 `仿真数据源.xlsx`：

- 第1行为标题；
- 第2行为单位；
- 第3行起，A列为转速（RPM），B列为径向变形（mm）；
- 数据读取后会将变形换算为 μm，并按转速排序、线性插值。

文件不存在、无有效数据或缺少 `openpyxl` 时，模拟器使用内置合成数据。当前默认模拟剖面包含多档升速和保载过程，可用于检查保载提示、DIC 转速关联及共享采集流程。

## 配置文件

`Sysdata/DeviceSet.txt` 为5行文本：

```text
0
1.0
5.0
60000.0
0.0
```

各行依次表示：

1. 设备号；
2. 转速传感器零点电压；
3. 转速传感器满量程电压；
4. 最大转速（RPM）；
5. 转速修正量（RPM）。

后4项用于实时转速换算，可在“设置 → 转速参数标定”中修改并保存。真卡启动时使用自动枚举到的首个非 Demo 设备。

## 数据输出

一次共享采集会话会自动创建：

```text
data/
└── YYYYMMDDNN/
    ├── YYYYMMDDNN.CSV
    ├── YYYYMMDDNN.png
    ├── YYYYMMDDNN_vib.CSV
    └── YYYYMMDDNN_spec.png
```

- `YYYYMMDDNN.CSV`：径向数据，包含时间、初始间距、当前间距、变形量和转速；
- `YYYYMMDDNN.png`：径向“转速—变形”散点及拟合曲线；
- `YYYYMMDDNN_vib.CSV`：振动数据，增加滤波后峰峰值列；
- `YYYYMMDDNN_spec.png`：停止采集时的 FFT 频谱快照。

径向和振动 CSV 均为带 UTF-8 BOM、CRLF 行尾的文本文件。界面中的采集时长会写入文件头，但当前不会自动结束采集，请使用“停止采集”按钮正常结束会话。

其他输出：

- 瀑布谱 CSV：用户选择保存位置，分号分隔；
- DIC 散点 CSV：保存为 `DIC散点数据_YYYYMMDD_HHMMSS.csv`。

## DIC 命令行分析

除图形界面外，也可以单独运行 DIC 分析：

```powershell
# 自动选择 DICdata 中按文件名排序的首个 xlsx
python dic_analysis.py

# 指定数据文件
python dic_analysis.py ".\DICdata\sample.xlsx"

# 指定圆柱截面半径 R（mm）和子午截面角度 θ（°）
python dic_analysis.py ".\DICdata\sample.xlsx" 90000 30
```

命令行参数顺序固定为：

```text
xlsx路径  圆柱截面半径R(mm)  子午截面角度θ(°)
```

## 推荐验证步骤

1. 检查基础依赖：

   ```powershell
   python -c "import numpy, PyQt5, pyqtgraph, openpyxl; print('Python 依赖正常')"
   ```

2. 断开采集卡后运行 `python main_app.py`，确认程序进入模拟模式；
3. 在任一采集页开始采集，确认径向页和振动页同步刷新；
4. 等待模拟转速进入保载，确认出现 DIC 采样提示及提示音；时延报告窗口不应弹出，
   时延判定仍通过原有控制台输出查看；
5. 停止采集，核对同一试验目录中的两份 CSV 和两张 PNG；
6. 将符合格式的 Excel 放入 `DICdata`，在 DIC 页面或命令行执行分析；
7. 真卡环境下先完成设备测试，再核对窗口标题、实时转速及通道数据。

## 仓库内容说明

仓库保存程序源码、运行配置和小型仿真数据源。实际采集结果、DIC 大型工作簿、Python 缓存、本地编辑器配置、日志及开发助手工作目录不属于发布内容，请在本地按需保存。
