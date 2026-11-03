# Enzyflow 硬件实验项目

ESP32-S3 + MicroPython 1.27 板端固件，以及 Linux 电脑端 USB 摄像头式光谱仪程序。
板端读取 AS7341 并输出遮挡状态和近似 RGB；电脑端独立采集 UVC 图像、提取曲线并保存数据。
部署源码以 [`firmware/`](firmware/) 为准，顶层 `lib -> firmware/lib` 是本地快捷符号链接。

## 目录

- [项目组成](#项目组成)
- [目录导航](#目录导航)
- [怎么运行](#怎么运行)
- [当前状态与已知限制](#当前状态与已知限制)

## 项目组成

```text
AS7341 → ESP32-S3 → USB 串口 → 电脑接收端
USB 摄像头式光谱仪 → 电脑上的 spectrometer/
```

光谱仪单独连接电脑，不需要 ESP32 充当 USB Host。两条数据流分别使用
`esp32.as7341` 和 `pc.usb_spectrometer`；电脑接收时间只能用于近似对齐。
字段和时间戳边界见 [JSON Lines 数据格式](docs/data-format.md)。

## 目录导航

| 路径 | 内容 |
|---|---|
| [`docs/`](docs/README.md) | 文档索引、固件说明、电机原文、光谱仪概览、数据格式、历史记录 |
| [`firmware/`](firmware/) | 真实部署来源：`main.py` 与 `lib/`；详见[固件说明](docs/firmware.md) |
| [`lib`](lib) | 指向 `firmware/lib` 的符号链接 |
| [`spectrometer/`](spectrometer/README.md) | 电脑端程序及详细操作说明；[概览与验证范围](docs/spectrometer.md) |
| [`tools/`](tools/) | 固定路径的板端维护与测试工具；部分工具会连接开发板 |
| [`archive/`](archive/) | 整理前的本地脚本 |
| [`backups/`](backups/) | 板端原始备份、SHA256 清单与历史验证记录 |
| [`vbnet_source/`](vbnet_source/) | VB.NET 源码预留目录及说明 |
| [`MicroPython1.27.bin`](MicroPython1.27.bin) | 保留的 MicroPython 固件二进制 |

## 怎么运行

电脑端离线单元测试使用已有虚拟环境，不需要连接硬件：

```bash
cd ~/Work/enzyflow/spectrometer && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```

以下是需要操作设备时的运行入口，本次文档整理未执行。`run.sh` 首次运行会创建
`.venv` 并安装依赖；配置、标定和采集文件位于 `spectrometer/runtime/`。

```bash
# 诊断环境、设备与配置
cd ~/Work/enzyflow/spectrometer && ./run.sh doctor
# 打开实时窗口并采集
cd ~/Work/enzyflow/spectrometer && ./run.sh run
```

板端已部署的 `/main.py` 会自动启动，并保留 3 秒 `Ctrl+C` 窗口。
启动校准时保持 AS7341 无遮挡，检测期间关闭 LED。停止后可在开发板 MicroPython REPL 重启：

```python
from enzyflow.app import run
run()
```

接线、阈值、LED 调用及维护工具边界见[固件说明](docs/firmware.md)；
电机操作先读[电机接线与控制](docs/motor.md)。不要让 Thonny 与其他采集程序同时占用同一 ESP32 串口。

## 当前状态与已知限制

下表中的硬件结果来自历史记录，不代表本次重新验证，也不能据此判断当前进程或设备位置。

| 范围 | 已有证据 | 未验证或限制 |
|---|---|---|
| AS7341（2026-10-28） | 历史已验证读取、LED 控制、部署校验、软重启自动运行；状态逻辑测试通过 | 没有做物理遮挡动作；闪烁检测未启用、未验证；`0x48` 未识别 |
| USB 摄像头（2026-11-03） | 历史已打开 Sonix `0c68:6464`，采集节点为 `/dev/video2`，格式 MJPG 1280×720 | **当前记录的画面是室内灯管，不是狭缝光谱**；是否为光谱仪头、狭缝/光栅是否就位仍待确认 |
| RS485 电机（2026-11-03） | 用户确认导程 4 mm/圈、范围 20–220 mm、当时位置 100 mm | 历史“待复测”说明保留；100 mm 不是启动默认位置，掉电/复位/清零/手动移动后须重新建立参照 |

- 遮挡 `signal` 是串口 JSON 字段，**不是 GPIO 电平输出**。
- AS7341 两组波段顺序采样，计数未经光强标定；`rgb_approx` 不是精确 sRGB。
- 相机强度是相机计数，未作绝对辐射定标；不能把室内灯管图像当成光谱。
- ESP32 `t_ms` 会回绕且不是 UTC；两条流没有硬件同步触发。
- 电机范围检查只在 `checked_delta_degrees()` 生效，直接调用底层命令会绕过检查。

完整限定与记录见[文档索引](docs/README.md)和[历史记录](docs/history-2026-10-28.md)。
