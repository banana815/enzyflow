# 板端备份

本目录保存当时从ESP32读取的文件或校准记录，不是当前部署源码。
当前源码位于 [`firmware/`](../firmware/)，备份与部署方法见[工具说明](../tools/README.md)。

| 路径 | 性质 |
|---|---|
| `before_reorganize/` | 早期整理前完整备份，包含SHA256清单及原有舵机/传感器代码 |
| `before_three_axis_20261104_complete/` | 三轴调试前完成的板端备份，包含`manifest.json` |
| `before_web_control_20261104/` | 网页控制固件更新前的完整板端备份；22 个文件及 `manifest.json`，已逐文件验证 SHA256 |
| `before_three_axis_20261104/` | 串口冲突中断的不完整副本，已标记`INCOMPLETE.txt`；不要用它恢复 |
| `motor_config_20261103/` | 历史单轴配置记录，不是当前机械位置 |
| `boot_verification.log` | 2026-10-28自动启动验证记录 |

清单中的`/lib/...`等键是当时的板上路径，本地对应路径去掉开头的`/`。
恢复前须选择完整备份并核对文件校验；不要为了恢复旧文件覆盖已经实测确认的三轴程序。
驱动器内保存的RS485机号属于驱动参数，不包含在ESP32文件备份里。
已有备份均保留，没有执行恢复操作。网页控制更新涉及 `motor_bus.py`、`motor_safety.py`、
`motor_web.py`，部署后逐文件核对 SHA256；`main.py` 仍为 AS7341 自动启动入口。
