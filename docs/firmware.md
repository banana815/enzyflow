# ESP32-S3 板端固件

[返回文档索引](README.md) · [数据格式](data-format.md) · [电机接线与控制](motor.md)

板端使用 ESP32-S3 / MicroPython 1.27。部署源码以
[`firmware/`](../firmware/) 为准；顶层 `lib -> firmware/lib` 是本地快捷符号链接。
下文运行说明依据原 README、现有源码及
[2026-10-28 历史记录](history-2026-10-28.md)，本次文档整理未连接开发板。

## 文件职责与板上布局

`firmware/` 内的相对路径对应板上根目录下的部署路径。

| 仓库源码 | 板上路径 | 职责 |
|---|---|---|
| [`firmware/main.py`](../firmware/main.py) | `/main.py` | 自动启动，保留 3 秒 `Ctrl+C` 窗口 |
| [`config.py`](../firmware/lib/enzyflow/config.py) | `/lib/enzyflow/config.py` | 引脚、增益、时序、基准与检测阈值 |
| [`acquisition.py`](../firmware/lib/enzyflow/acquisition.py) | `/lib/enzyflow/acquisition.py` | AS7341 读取与 LED 关闭 |
| [`processing.py`](../firmware/lib/enzyflow/processing.py) | `/lib/enzyflow/processing.py` | 遮挡状态逻辑与近似 RGB |
| [`protocol.py`](../firmware/lib/enzyflow/protocol.py) | `/lib/enzyflow/protocol.py` | JSON Lines 输出 |
| [`app.py`](../firmware/lib/enzyflow/app.py) | `/lib/enzyflow/app.py` | 校准与连续采样入口 |
| [`as7341.py`](../firmware/lib/as7341.py)、[`as7341_smux_select.py`](../firmware/lib/as7341_smux_select.py) | `/lib/` 下同名文件 | 传感器驱动、测量超时与通道配置 |
| [`as7341_led.py`](../firmware/lib/as7341_led.py) | `/lib/as7341_led.py` | LED 电流设置与开关 |
| [`light_monitor.py`](../firmware/lib/light_monitor.py) | `/lib/light_monitor.py` | 兼容旧导入入口 |
| [`motor_config.py`](../firmware/lib/enzyflow/motor_config.py) | `/lib/enzyflow/motor_config.py` | 电机坐标换算与软件范围检查；本身不发送运动命令 |

原 README 还记录了板上保留的 `/boot.py`、`/calib.json`、`/valve.json`，以及
移入 `/lib/` 的旧驱动 `tcs34725.py`、`uservo.py`。这些不是当前 `firmware/`
目录中的文件；原始版本保存在
[`backups/before_reorganize/`](../backups/before_reorganize/)。完整部署前备份的
[`manifest.json`](../backups/before_reorganize/manifest.json) 含原始文件的 SHA256。
原本地脚本保存在 [`archive/lib_before_reorganize/`](../archive/lib_before_reorganize/)。

## AS7341 接线与采样设置

| AS7341 | ESP32-S3 |
|---|---|
| VCC | 3.3V |
| GND | GND |
| SDA | GPIO2 |
| SCL | GPIO4 |
| INT、GPIO | 不连接 |

以引脚标识为准，不仅凭线色判断。当前配置为 I2C1、100000 Hz，AS7341 地址
`0x39`；`ATIME=29`、`ASTEP=599`、`GAIN=8`、`INTERVAL_MS=200`。
每帧先读 F1–F4/Clear/NIR，再读 F5–F8/Clear/NIR，两组是顺序采集，
并非同时采样。`INTERVAL_MS` 是帧间等待，不代表包含硬件读取在内的完整周期。

## 自动启动与遮挡检测

下列行为可从当前源码核对；历史硬件验证范围见文末。

- `/main.py` 启动时等待 3 秒，然后调用 `enzyflow.app.run()`。
- 默认 `BASELINE=None`：启动时保持传感器无遮挡，采集 10 帧 Clear，按排序后的
  中间项建立基准；整个校准需要数秒。校准过暗（基准小于 10）或饱和时报错并返回 REPL。
- 遮挡检测期间 LED 保持关闭；不要同时单独控制 LED。
- 连续 3 次读数不高于低阈值后进入 `BLOCKED`；被遮挡后，连续 3 次不低于
  高阈值恢复 `LIGHT`，使用迟滞减少反复切换。
- 默认比例为基准的 20% / 35%；源码还保留最小间隔：
  `low=max(2, baseline*0.20)`，`high=max(low+2, baseline*0.35)`。
  实际阈值以 `ready` 记录中的 `blocked_below` / `light_above` 为准。
- `signal=1` 表示遮挡，`signal=0` 表示未遮挡。这是 USB 串口文本/JSON 输出，
  **不是物理 GPIO 电平**。饱和时 `state` 优先报告 `SATURATED`。
- 若需要重复使用已测得的无遮挡基准，可在 `config.BASELINE` 中指定；不要把未知
  光照条件下的数值当成已验证基准。

在开发板的 MicroPython REPL 中，`Ctrl+C` 停止应用。默认 `BASELINE=None` 时，
重新运行会重新执行启动校准：

```python
from enzyflow.app import run
run()
```

停止遮挡检测后，可在同一开发板 REPL 单独调用 LED：

```python
from as7341_led import AS7341LED
led = AS7341LED()
led.on(4)
led.off()
```

`on()` 默认 4 mA，可设置 4–20 mA 的偶数值；`set_current(0)` 关闭。
`get_current()` 读取配置值，不是电流实测值。

## 输出与两条数据流

每行一个 JSON 对象，`schema=1`、`source=esp32.as7341`。公共字段为
`device_id`、`seq`、`t_ms`、`event`；`ready` 报告基准、阈值和采样设置，
`measurement` 包含 `state`、`signal`、`channels`（F1–F8）、`clear`、`nir`、
`saturated`、`rgb_approx`。

计数未经光强标定；RGB 是波段分组后按最强分量归一化的显示近似，未经颜色标定。
`t_ms` 是会回绕的板载计时，不是 UTC。与电脑端光谱仪的 `pc.usb_spectrometer`
流应保留不同的 `source` / `device_id`，由电脑记录接收时间以近似对齐；真正同步
需要另行设计触发/时钟方案。完整字段说明见[数据格式](data-format.md)。

## 维护工具与电机配置

维护工具使用的主机解释器为 `/home/orange/.local/opt/thonny/bin/python3.10`。

| 工具 | 用途与边界 |
|---|---|
| [`tools/board_tool.py`](../tools/board_tool.py) | `backup`、`deploy`、`exec`；使用当前 Thonny 后端和固定 `/dev/ttyACM0`，要求恰好一个 ESP 后端。会中断应用、短暂暂停后端串口访问后恢复；部署前先备份，部署时校验 SHA256，最后安装 `main.py` |
| [`tools/test_processing.py`](../tools/test_processing.py) | 纯处理逻辑的防抖、迟滞及 RGB 边界测试 |
| [`tools/validate_board.py`](../tools/validate_board.py) | 一次性部署验证/迁移脚本，会操作开发板，不是普通离线测试 |

维护前需重新确认当时的连接及进程状态，不能依赖历史进程号；不要让电脑采集程序
与 Thonny 同时占用 ESP32 串口。此次整理没有执行这些维护工具。

2026-11-03 用户确认的电机配置为 UART1、TX5、RX6、ID1、115200，导程
4 mm/圈，可用范围 20–220 mm，当时位置为 100 mm；正转使位置减小，
1 cm 对应 900° / 128000 脉冲。`checked_delta_degrees()` 只检查范围并返回角度，
原始 `motor_ctrl` 命令绕过该范围检查。驱动复位、掉电、清零或手动移动后必须重新
建立位置参照，不能每次启动都假定仍为 100 mm。接线和历史实测见[电机文档](motor.md)。

## 已验证与未验证

- **历史已验证（2026-10-28）**：Thonny 通信、AS7341 读取、LED 开关及配置电流
  对采样的影响、逐文件部署 SHA256 校验、软重启自动运行。软重启日志见
  [`backups/boot_verification.log`](../backups/boot_verification.log)。
- **历史测试范围有限**：实际光照采样与人工构造的状态切换逻辑测试通过，但没有
  替用户进行物理遮挡动作。未运行舵机或阀门的说明属于该日期记录；电机后续状态
  以[电机文档](motor.md)中的日期和限定为准。
- **未验证或未标定**：AS7341 驱动的 100/120 Hz 闪烁检测未启用、未验证；
  扫描到的 `0x48` 未进一步识别；原始计数与 RGB 未作光强/颜色标定。
- **本次未复测硬件**：历史采集进程、接线、板上文件与电机位置都不能据旧记录
  推定仍处于同一状态。
