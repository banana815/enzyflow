# 测试与设备维护工具

[项目总览](../README.md) · [网页使用手册](../docs/web-control.md) · [三轴操作手册](../docs/motor.md) · [离线测试](../tests/README.md)

## 日常入口

| 目的 | 入口 | 是否连接设备 |
|---|---|---|
| 检查固件逻辑 | `python tools/test_firmware.py` | 否；标准Python与模拟总线 |
| 检查网页控制服务 | `python -m unittest discover -s tests/webcontrol -v` | 否；模拟设备及本机 HTTP 测试 |
| 网页操作丝杆 | 根目录双击 `启动三轴控制台.desktop`，或 `./start-control.sh` | 是；需确认对应关系、尺量标定后才运动 |
| Thonny 交互调试 | F5运行 `firmware/motor_debug.py` | 是；按提示确认后才运动 |
| 备份或部署 | `board_tool.py` | 是；借用已连接的Thonny后端 |

离线测试源码位于 `tests/firmware/` 和 `tests/webcontrol/`。不要把下面的板端脚本加入离线测试发现范围。

## 网页启动与串口交接

`start-control.sh` 优先使用项目独立 `.venv`（已安装 `pyserial 3.5`），再运行 `launch_webcontrol.py`。
后者在默认 `http://127.0.0.1:8765/` 启动或复用本机服务并打开浏览器；启动本身不下发运动。
日常不需要Thonny。若它正在占用串口，先手动选择“运行 → 断开”；
可选参数`--handoff-thonny`才会调用旧会话交接工具，保留编辑器和未保存内容。
当前实机握手待修复，见[下次测试计划](../当前历史与下次测试计划.md)。

```bash
./start-control.sh
# 模拟设备，不打开真实串口，另用端口避免与实机服务混淆
./start-control.sh --demo --port 8766
```

服务只有一个设备执行 worker，网页通过固定命令接口操作，不能提交任意 REPL 或原始电机报文。
日志、PID 与运行时记录位于 `webcontrol/runtime/`，不纳入 Git，也不能用作位置基准。
返回 Thonny 维护前，在网页停止后续动作并断开，再在 Thonny 按 `Ctrl+F2` 重连。

## 通过 Thonny 维护 ESP32

`board_tool.py` 在电脑上运行，使用本机 Thonny Python：
`/home/orange/.local/opt/thonny/bin/python3.10`。它要求恰好一个 ESP32 后端，连接端口
固定为 `/dev/ttyACM0`。先在Thonny选择正确解释器和端口，结束正在执行的动作。

工具有互斥锁：维护操作不能并行。它会中断板端程序，暂时暂停Thonny后端的串口访问，
执行完成后恢复后端并交回友好REPL。其他采集程序也不能占用同一串口。

以下命令从仓库根目录执行。备份目录名中的时间占位符应替换为唯一名称，不能复用已存在目录。

```bash
# 先备份；生成文件副本和manifest.json
/home/orange/.local/opt/thonny/bin/python3.10 tools/board_tool.py backup backups/manual_before_update_YYYYMMDD_HHMMSS

# 上传firmware下的.py，写临时文件、校验SHA256后替换；main.py最后安装
/home/orange/.local/opt/thonny/bin/python3.10 tools/board_tool.py deploy firmware

# 板端假总线验证：不创建电机UART，不运动
/home/orange/.local/opt/thonny/bin/python3.10 tools/board_tool.py exec tools/motor_diagnostics/verify_on_board.py
```

`deploy` 不会自动备份，也不会删除板上其他文件。部署根目录的相对路径直接映射到板上根目录；
部分更新也应保留例如 `lib/enzyflow/motor_safety.py` 的目录层级，不能直接把模块目录作为部署根。
上传之后用F5重新运行入口，使新会话重新导入代码、建立位置基准。

`exec` 执行指定文件的全部板端代码。它并非“只读”命令，实际效果取决于文件内容。
工具当前单次执行超时为25秒；标定按小批次完成，不把长时间交互程序交给此入口。

## `motor_diagnostics/`：受监督调试脚本

这些是本次调试保留的板端工具，不是日常前进/后退入口。
含 `None` 占位参数的模板默认拒绝执行；只能填写本次已确认的实物信息。

| 文件 | 行为 | 使用边界 |
|---|---|---|
| `verify_pump_offline.py` | 板端假总线验证双注射器速度、体积和心跳停机 | 不构造真实UART，不驱动电机 |
| `read_only_scan.py` | 查询1–10机号的编码器 | 只读电机参数，不运动 |
| `scan_all_ids.py` | 查询1–255机号的版本 | 只读；无广播 |
| `bounded_probe.py` | 捕获空闲流量及1/2/3机号应答 | 限时限长，只读排查混杂报文 |
| `verify_on_board.py` | 在ESP32执行假总线逻辑验证 | 不访问电机UART；会清理模块缓存和测试变量 |
| `set_single_motor_id.py` | 修改机号并执行0x04保存 | 写驱动参数，不运动；必须仅连接一台，再断电验证保持 |
| `probe_one_axis.py` | 一次方向测试 | **会运动**；需要已确认的机号和实测位置 |
| `calibration_init.py` | 初始化受监督五圈标定 | 自身不运动；验证完整行程余量，使用本次会话状态 |
| `calibration_one_turn.py` | 每次四小段、累计最多五圈 | **会运动**；必须先在同一REPL执行初始化；失败不自动重试 |
| `return_after_probe.py` | 返回本次方向测试起点 | **会运动**；只适用于未中断的驱动会话和已确认的方向 |

五圈脚本按本机已确认的4mm导程组织脉冲；不是任意导程的通用校准工具。
位置或方向未知时，不能以历史记录填空后试转。脚本里的旧会话变量、历史计数也不能跨掉电复用。
普通调节使用网页控制台或 Thonny 中的 `motor_debug.py`，保留 1 cm 目标余量及各自的确认流程。
网页速度可调 3–30 rpm（默认12），Thonny交互保持6 rpm；当前实机已验证6→12 rpm，
尚未逐速验证至30 rpm。

## 历史迁移脚本

原 `validate_board.py` 已移到
[`archive/board_migration_20261028.py`](../archive/board_migration_20261028.py)。它会改动开发板
文件位置并操作AS7341 LED，只保留作历史参考，不作为当前部署或测试步骤。

## 独立光谱仪启动器（2026-11-13）

`./start-control.sh` 固定启动电机服务，`./start-spectrometer.sh` 固定启动光谱仪服务。
底层 `launch_webcontrol.py --service motor|spectrometer` 分别使用默认端口 8765/8766；
复用服务时同时核对设备类型和模拟模式，避免误连旧版合并控制台。光谱仪不调用 Thonny 交接工具。
