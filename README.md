# Enzyflow

ESP32-S3 / MicroPython 1.27 实验项目：三丝杆 RS485 电机调试、AS7341 遮挡检测，
以及独立运行在电脑上的 USB 摄像头式光谱仪。

## 电机与光谱仪独立运行（2026-11-13）

两个控制台使用独立 Python 进程、HTTP 端口、页面与设备生命周期。光谱仪页面不会申请电机控制权、连接串口或启动电机线程；电机服务不加载光谱仪或 NumPy/OpenCV。两者可分别启动，也可同时运行。

```bash
python3 -m venv .venv
# 两者都使用时安装全部依赖；单设备也可以使用下面的独立依赖文件
.venv/bin/python -m pip install -r webcontrol/requirements.txt
./start-control.sh         # 电机：http://127.0.0.1:8765/
./start-spectrometer.sh    # 光谱仪：http://127.0.0.1:8766/
```

只用电机：安装 `webcontrol/requirements-motor.txt`；只用网页光谱仪：安装 `webcontrol/requirements-spectrometer.txt`。两个脚本都支持 `--demo`、`--no-browser`、`--port`。旧版合并服务仍在运行时，启动器会提示退出旧服务或换端口，不会误用旧页面。迁移前请先正常停止任务、结束光谱记录并断开设备，再退出旧进程。

电机控制、固件保护和光谱算法保持现有行为。校准与采集文件继续使用 `spectrometer/runtime/web/`，电机位置与运行状态仍在 `webcontrol/runtime/`；这些本机数据及虚拟环境不提交到 Git。原光谱仪 CLI 也继续保留。详细操作见[网页手册](docs/web-control.md)。

## 在 macOS 上运行三轴电机控制台

安装 Python 3.10 或更新版本，在 Mac 上进入项目目录并新建虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r webcontrol/requirements-motor.txt
./start-control.sh
```

接上已安装 Enzyflow 固件的 ESP32。控制台会打开 `http://127.0.0.1:8765/`，并识别符合设备 USB 身份的 `/dev/cu.*` 串口。若没有识别到，可运行 `.venv/bin/python -m serial.tools.list_ports -v` 检查 macOS 是否列出了 USB 串口。使用 Thonny 时，请先在 Thonny 中断开连接再打开控制台。

若连接了多块支持的设备，可用 `.venv/bin/python -m webcontrol.server --serial-port /dev/cu.usbmodem101` 指定串口，将示例路径替换成列表中的实际路径。应先退出占用同一端口的控制台服务。也可运行 `./start-control.sh --demo --port 8767`，无需设备即可试用页面。首次换到 Mac 上使用时，按网页提示建立本机位置基准。

## USB 光谱仪网页功能

现已整理为两步校准：①暗场本底扣除；②使用 `Archive/皓宇荧光灯.png`，冻结本机灯谱、对应436/546/610nm主峰并拟合波长，404nm可作独立验证。参考图为整数标注，不能直接使用图片像素或宣称亚纳米精度。

现已增加独立暗场校正：固定手动曝光、完全遮光后平均32帧（可选8–256），逐像素扣底噪，并在导出中保留原始值与底噪。校准及采集设置自动保存，重连后自动恢复；改变ROI或曝光可恢复校准时设置复用原底噪，不必每次重采。

独立光谱仪控制台支持 UVC 实时预览、光谱曲线、ROI、曝光、平均/平滑、峰值保持、零参考、波长标定、CSV/JSON/PNG 导出和 JSONL 连续记录。运行 `./start-spectrometer.sh`，扫描并连接。

光谱仪仅接受 `0c68:6464` 的 UVC 采集接口，按 USB 身份与 by-id 路径选择；内置摄像头、metadata 和串口被排除。ESP32 仍只接受已知串口 USB 身份并验证 MicroPython 协议；多个串口候选时不再优先抢选 `ttyUSB0`。两个服务进程独立，光谱仪不会自动重连。详细说明见 [网页手册](docs/web-control.md#usb-spectrometer-web)。

网页 Python 环境新增 NumPy 与无界面 OpenCV：`.venv/bin/python -m pip install -r webcontrol/requirements.txt`。原独立光谱仪程序继续保留。

## 从这里开始

**当前交接状态（2026-11-10）：** 已修复当前 USB 转串口设备的识别及板端读取警告污染协议的问题，
实机启动握手与连续 10 次状态查询通过；已增加多轴并行定位与安全区外向内恢复，新增运动功能尚未实机试跑。另已增加双注射器恒速推进（内径19.22mm，A/B独立速率、默认每支1mL/min），需输入本次剩余推杆行程；实际流量尚未校准。已增加三轴软件急停锁存与指定坐标连续复位（默认30rpm，各轴整段移动、分别到位停止），运动中急停和高速回程尚待带载验收。见[当前历史与下次测试计划](当前历史与下次测试计划.md)。
日常使用不需要运行 Thonny。

| 任务 | 入口 | 说明 |
|---|---|---|
| 网页调节三根丝杆 | 双击 [`启动三轴控制台.desktop`](启动三轴控制台.desktop)，或运行 `./start-control.sh` | [网页使用手册](docs/web-control.md) |
| Thonny 交互调试 | 打开 [`firmware/motor_debug.py`](firmware/motor_debug.py)，按 F5 | [三轴参数与 Thonny 手册](docs/motor.md) |
| AS7341 采集 | 板端 [`firmware/main.py`](firmware/main.py) 自动启动 | [固件与接线](docs/firmware.md) |
| 网页光谱仪 | `./start-spectrometer.sh` | [网页使用手册](docs/web-control.md#usb-spectrometer-web) |
| 原电脑端光谱仪 CLI | `cd spectrometer && ./run.sh run` | [光谱仪手册](spectrometer/README.md) |
| 离线检查固件 | `python tools/test_firmware.py` | [测试说明](tests/README.md) |
| 检查网页控制服务 | `python -m unittest discover -s tests/webcontrol -v` | [测试说明](tests/README.md) |
| 备份、部署开发板 | [`tools/board_tool.py`](tools/board_tool.py) | [维护工具说明](tools/README.md) |

## 当前三轴规则

| 机号 | 实物 | 机械有效范围 | 两端各留 1 cm 后的目标范围 |
|---|---|---|---|
| 1 | 长轴 | 2–32 cm | **3–31 cm** |
| 2 | 短轴 A | 2–22 cm | **3–21 cm** |
| 3 | 短轴 B | 2–22 cm | **3–21 cm** |

2026-11-04 尺量确认三轴导程均约 **4 mm/圈**，**反转前进、正转后退**；
机号已保存，并验证断电后三台共线独立通信。每次运行仍须确认实物对应和当前实测位置，
不能加载历史位置代替测量。**网页统一使用 mm**；Thonny 交互标定仍输入 cm，移动输入 mm。

程序默认及重连后锁定。网页可独立设置目标位置、单步和速度（3–30 rpm，默认 12）；
Thonny 交互保留 6 rpm。当前 4 mm/圈配置每段 0.1–1 mm，较长定位逐段反馈后继续，
支持选择多轴并行分段运动；安全区外恢复单独以低速小步向内操作。已实机验证从 6 提高到 12 rpm，尚未逐速验证至 30 rpm。

目标须保留两端各 1 cm；当前位置在安全区外时普通调节也被锁定。例如尺量 20 mm
可以记录为基准，但不能据此直接启动普通移动；需使用向内恢复按钮，每次最多0.5mm、6rpm。软件保护依赖正确位置和方向，不能代替
物理限位；“停止后续动作”和 `Ctrl+C` 都不能保证立即刹停已下发的一小段。

## 代码布局

```text
firmware/                  ESP32 部署源；相对路径对应板上路径
  main.py                  AS7341 自动启动入口
  motor_debug.py           三轴交互调试入口（手动运行）
  lib/enzyflow/            采集应用、运动保护、总线与网页控制协议
  lib/                     传感器驱动与旧导入兼容层
webcontrol/                独立电机/光谱仪 HTTP 服务、串口会话与静态网页
start-control.sh           电机网页启动入口（8765）
start-spectrometer.sh      光谱仪网页启动入口（8766）
spectrometer/              电脑端光谱仪及其独立测试
tests/firmware/           固件离线测试（模拟串口，不连接设备）
tests/webcontrol/          网页服务、会话与 HTTP 离线测试
tools/                     测试入口、Thonny 维护工具、受监督调试模板
docs/                      当前手册、数据格式与历史记录
backups/                   板端备份与 SHA256 清单
archive/                   历史代码和一次性迁移脚本
```

顶层 `lib` 是指向 `firmware/lib` 的兼容符号链接；部署以 `firmware/` 为准。
`MicroPython1.27.bin` 为保留的固件二进制；`vbnet_source/` 是旧源码预留目录。

网页控制链为 `浏览器 → 本机 HTTP 服务 → USB → motor_web → motor_safety → motor_bus → UART1`。
Thonny 控制链仍为 `motor_debug → motor_console → motor_safety → motor_bus → UART1`。
两个网页服务均仅监听 `127.0.0.1`，默认端口为电机 `8765`、光谱仪 `8766`，无需外网和前端构建。
`motor_config.py` 仅保留历史单轴兼容函数，**不是当前三轴配置入口**。
开机 `main.py` 仍启动 AS7341；网页连接时中断采集并进入电机服务。两种模式不同时运行。

## 验证与记录

```bash
cd ~/Work/enzyflow
python tools/test_firmware.py
python -m unittest discover -s tests/webcontrol -v
```

2026-11-13 解耦后已通过 218 项固件测试、124 项网页服务测试、35 项光谱仪测试及两组前端脚本测试，
包含两个服务的接口、依赖与关闭隔离检查。本次没有操作真实电机或摄像头，也未进行浏览器视觉验收。
电脑端光谱仪保持原有测试入口，验证范围见[测试说明](tests/README.md)及[网页手册](docs/web-control.md)。

三轴的接线排查、改号保存、方向更正与尺量依据见
[2026-11-04 调试记录](docs/motor-session-20261104.md)。没有进行全行程撞限位或安全急停验证。
AS7341 既有验证及光谱仪光路限制分别见[固件说明](docs/firmware.md)和
[光谱仪概览](docs/spectrometer.md)。所有文档入口见[文档索引](docs/README.md)。
