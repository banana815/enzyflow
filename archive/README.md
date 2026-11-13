# 历史代码

这些文件保留原始内容用于追溯，不参与当前固件部署，也不参与离线测试发现。
当前部署源为 [`firmware/`](../firmware/)。

| 路径 | 来源与边界 |
|---|---|
| `lib_before_reorganize/` | 整理前的本地传感器脚本 |
| `board_migration_20261028.py` | 原`tools/validate_board.py`；会把板上旧驱动移入`/lib`、采集AS7341并操作LED的一次性脚本 |

不能把历史迁移脚本当成当前“检查设备”命令。当前维护方法见[工具说明](../tools/README.md)，
旧电机/舵机/泵资料见[历史硬件说明](../docs/motor-legacy.md)。
