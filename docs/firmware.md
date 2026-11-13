# ESP32 固件架构与 AS7341

[文档索引](README.md) · [三轴操作手册](motor.md) · [维护工具](../tools/README.md)

板端使用 ESP32-S3 / MicroPython 1.27，源码以 `firmware/` 为准。
本页说明部署布局、模块职责和传感器采集；三轴参数、1 cm 余量和操作步骤统一见
[三轴操作手册](motor.md)，避免在多个文档重复维护同一组参数。

## 两个运行入口

- `/main.py`：上电等待3秒后启动 AS7341 自动采集，保留 `Ctrl+C` 窗口。
- `/motor_debug.py`：在 Thonny 中手动运行的三轴交互入口，不随上电自动运动。

两者在同一个 MicroPython 会话中使用；切换到电机调试前先停止传感器采集。

| 控制层 | 职责 | 不负责的内容 |
|---|---|---|
| `motor_console` | 人工确认实物、单位和命令；显示结果 | 不自行判断机械长短或当前位置 |
| `motor_safety` | 本次位置基准、逐轴导程、目标余量、反馈检查和锁定 | 不把历史位置作为启动默认值 |
| `motor_bus` | UART 报文、校验和、超时、有限脉冲写入 | 不独立提供机械行程保护 |

正常运动必须走 `motor_console → motor_safety → motor_bus`。旧 `motor_config.py`
仅为历史单轴兼容代码；它的100mm参考值不适用于当前三轴控制。

## 文件职责与板上布局

`firmware/` 内的相对路径对应板上根目录下的部署路径。

| 仓库源码 | 板上路径 | 职责 |
|---|---|---|
| [`firmware/main.py`](../firmware/main.py) | `/main.py` | 自动启动，保留 3 秒 `Ctrl+C` 窗口 |
| [`firmware/motor_debug.py`](../firmware/motor_debug.py) | `/motor_debug.py`，或 Thonny 打开本地文件 F5 | 手动启动三丝杆调试，不在开机时运行 |
| [`motor_console.py`](../firmware/lib/enzyflow/motor_console.py) | `/lib/enzyflow/motor_console.py` | 长短轴、机号和逐轴标定提示，独立前进/后退命令 |
| [`motor_safety.py`](../firmware/lib/enzyflow/motor_safety.py) | `/lib/enzyflow/motor_safety.py` | 会话位置基准、按轴行程检查、反馈验证与故障锁定 |
| [`motor_bus.py`](../firmware/lib/enzyflow/motor_bus.py) | `/lib/enzyflow/motor_bus.py` | 有界串口读取、应答校验、有限小段运动；无硬件停止命令 |
| [`config.py`](../firmware/lib/enzyflow/config.py) | `/lib/enzyflow/config.py` | 引脚、增益、时序、基准与检测阈值 |
| [`acquisition.py`](../firmware/lib/enzyflow/acquisition.py) | `/lib/enzyflow/acquisition.py` | AS7341 读取与 LED 关闭 |
| [`processing.py`](../firmware/lib/enzyflow/processing.py) | `/lib/enzyflow/processing.py` | 遮挡状态逻辑与近似 RGB |
| [`protocol.py`](../firmware/lib/enzyflow/protocol.py) | `/lib/enzyflow/protocol.py` | JSON Lines 输出 |
| [`app.py`](../firmware/lib/enzyflow/app.py) | `/lib/enzyflow/app.py` | 校准与连续采样入口 |
| [`as7341.py`](../firmware/lib/as7341.py)、[`as7341_smux_select.py`](../firmware/lib/as7341_smux_select.py) | `/lib/` 下同名文件 | 传感器驱动、测量超时与通道配置 |
| [`as7341_led.py`](../firmware/lib/as7341_led.py) | `/lib/as7341_led.py` | LED 电流设置与开关 |
| [`light_monitor.py`](../firmware/lib/light_monitor.py) | `/lib/light_monitor.py` | 兼容旧导入入口 |
| [`motor_config.py`](../firmware/lib/enzyflow/motor_config.py) | `/lib/enzyflow/motor_config.py` | 历史单轴换算与范围检查；新三轴控制路径不使用其位置基准 |

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

## 测试、维护与证据

- [离线测试](../tests/README.md)：`python tools/test_firmware.py`，不连接设备。
- [维护工具](../tools/README.md)：备份、部署、板端逻辑验证及受监督调试模板。
- [备份清单](../backups/README.md)：区分完整与中断备份。
- [三轴实测记录](motor-session-20261104.md)：机号保存、方向更正、尺量和最终参数。

原 `tools/validate_board.py` 已归档为
[`archive/board_migration_20261028.py`](../archive/board_migration_20261028.py)。
它会重命名板上文件、采样并操作 LED，不是通用验证脚本，不由离线测试入口运行。

AS7341 的既有硬件证据来自2026-10-28：读取、LED控制、上传校验及软重启自动采集；
见[原始记录](history-2026-10-28.md)与[启动日志](../backups/boot_verification.log)。
没有据此验证人工遮挡动作、100/120Hz闪烁检测或绝对辐射定标；曾扫描到的`0x48`未识别。
本次代码与文档整理没有重新连接或运行硬件。
