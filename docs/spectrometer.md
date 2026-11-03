# 电脑端 USB 光谱仪

Linux 电脑端程序位于 [spectrometer/](../spectrometer/)，直接读取 UVC 摄像头并保存 CSV、JSON、JSONL 和 PNG。USB 光谱仪单独连接**电脑**，ESP32-S3 不充当 USB Host；该程序不修改 ESP32 固件，也不访问 ESP32 串口。

完整的安装、按键、ROI、波长标定、参考校正、二次开发和故障排查说明见 [spectrometer/README.md](../spectrometer/README.md)。本页只整理项目入口和状态；两条数据流的字段见 [JSON Lines 数据格式](data-format.md)。

## 已验证记录与尚未验证的部分

以下硬件结果来自原 README 和电脑端说明中 **2026-11-03 的记录**，本次文档整理没有重新连接设备验证。

| 项目 | 已有记录 | 结论边界 |
|---|---|---|
| 外接设备 | Sonix `USB 2.0 Camera: USB-ZH`，VID:PID `0c68:6464` | 已识别 UVC 设备，是否确为自制光谱仪头仍待物理确认 |
| 采集节点 | `/dev/video2` 为采集，`/dev/video3` 为 metadata；MJPG 1280×720 @ 31 fps，能打开并读帧 | 节点号是当时记录，不能保证重新插拔后不变 |
| 自动曝光 | 画面接近全黑，最大值约 4–16/255 | 不据此断言光路已经正常 |
| 手动曝光 | 曝光 5000、亮度 0、gamma 100 时看见室内场景/灯管 | **当前记录中的画面是室内灯管，不是狭缝光谱，不能把灯管当成光谱** |

尚未确认该 UVC 节点是否就是光谱仪摄像头、狭缝/光栅是否安装并对准，也未据此验证真实光谱的波长精度。自动 ROI 只寻找最亮水平带，在室内画面上可能框住灯管；这不构成光谱测量。

代码默认排除内置 Bison / `Integrated Camera`，并结合显式设备参数、已保存节点或 VID:PID、设备名称及唯一外接采集节点选择设备。自动选择不能替代对物理光路和预览内容的确认。

## 运行入口

下面是后续实际使用时的命令说明；本次整理没有执行设备检查、GUI 或采集。`doctor` / `devices` 会检查设备，`run` 会打开窗口，`capture` 会启动采集。

```bash
# 检查环境、设备和配置
cd ~/Work/enzyflow/spectrometer && ./run.sh doctor

# 列出设备及控制项
cd ~/Work/enzyflow/spectrometer && ./run.sh devices --controls

# 打开实时窗口
cd ~/Work/enzyflow/spectrometer && ./run.sh run

# 合成光谱界面演示：不证明真实光路或标定有效
cd ~/Work/enzyflow/spectrometer && ./run.sh run --synthetic

# 无界面采集 30 帧并求平均，保存 CSV/JSON/PNG
cd ~/Work/enzyflow/spectrometer && ./run.sh capture --frames 30 --out runtime/captures/lab.csv

# 连续采集 60 秒，每秒一条 JSONL
cd ~/Work/enzyflow/spectrometer && ./run.sh capture --duration 60 --interval 1 --jsonl runtime/captures/run.jsonl
```

首次运行时，`run.sh` 会在缺少可执行的 `.venv/bin/python` 时创建 `.venv` 并安装 `requirements.txt` 中的依赖。现有 `.venv/` 和 `runtime/` 数据应保留；运行配置、标定和采集结果位于 `spectrometer/runtime/`。

无需硬件的单元测试入口：

```bash
cd ~/Work/enzyflow/spectrometer && PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```

测试覆盖合成数据下的标定、ROI、峰检测、处理和记录等行为；测试通过不等于真实光谱仪的光路或测量精度已经验证。

## 与 AS7341 配合

- 电脑端应保留独立的 AS7341 与 USB 光谱仪采集适配器，使用不同的 `source` / `device_id`。
- AS7341 由 ESP32 通过串口输出；不要让另一个采集程序与 Thonny 同时占用 `/dev/ttyACM0`。
- 电脑接收 AS7341 记录时需补记接收时间，再与电脑端光谱仪记录做近似时间对齐。ESP32 原始记录没有 `t_wall`，`t_ms` 不是 UTC。
- 真正同步采集需要另行设计时钟或触发机制，目前没有硬件同步。

## 已知限制

- 相机计数未做辐射定标，不是 W/m²、lux 或绝对光谱辐照度；曝光、增益和光学系统都会影响强度。
- 未做波长标定时，横轴只有像素，`wavelength_nm` 为 `null`；有效的标定文件也不能证明当前画面一定是光谱。
- 波长精度依赖光路和机械稳定性；换分辨率、镜头或改变装配后需重新检查标定。
- 8 位相机可能饱和；相机 RGB 和用于画图的颜色都不是经过验证的 CIE 色度结果，当前没有 CRI、色温或色坐标功能。
- 零参考是逐像素减法，不是严格吸光度；参考响应校正也不是绝对定标。
- 电脑端说明将当前实现描述为按参考项目原理重写，未逐行移植 VB.NET 源码；其中“未找到 VB.NET 工程”的说法属于原文记录，不代表本次对 `vbnet_source/` 做了新的验证。功能映射见 [VB.NET 对照说明](../spectrometer/docs/VBNET_TO_PYTHON.md)。

[返回文档索引](README.md) · [返回项目总览](../README.md)
