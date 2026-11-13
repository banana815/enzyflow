# 离线测试

在仓库根目录运行：

```bash
python tools/test_firmware.py
```

仅需标准 Python 3，不需要 Thonny、串口权限或连接 ESP32。入口关闭字节码缓存写入，
发现 `tests/firmware/test_*.py` 并用退出码表示是否全部通过。

| 文件 | 覆盖内容 |
|---|---|
| `test_motor_bus.py` | 帧校验、分包、超时、写入失败、有限脉冲命令 |
| `test_motor_safety.py` | 长短轴、1 cm 余量、逐轴导程、方向、可调速度与异常锁定 |
| `test_motor_console.py` | 人工确认、单位、命令取消和交互流程 |
| `test_motor_calibration.py` | 受监督标定模板的分段、五圈上限和失败处理 |
| `test_motor_web.py` | 板端控制协议、会话/序号、命令去重、锁定与非运动操作 |
| `test_processing.py` | AS7341 防抖、迟滞及近似 RGB |

本次统一入口通过 138 项测试。测试使用假总线、假时钟和模拟 `machine` 模块，
不会发出真实电机指令。通过测试不代表完成了机械全行程或急停验证。

## 只运行某一组

```bash
python -B -m unittest discover -s tests/firmware -p 'test_motor_safety.py'
```

## 电脑端网页控制

```bash
python -m unittest discover -s tests/webcontrol -v
```

本次通过 26 项测试，覆盖单控制者与心跳过期、分段任务、其他轴漂移、停止与断开、
越界和重复请求，以及串口传输协议、HTTP 来源校验和请求格式。设备通信使用模拟对象，
不连接 ESP32。HTTP 测试需要环境允许绑定本机回环临时端口。

静态网页不需要 Node 才能运行；开发时可额外用 `node --check webcontrol/static/app.js`
检查 JavaScript 语法。模拟端到端 HTTP 操作已验证，浏览器执行环境不可用，本次未完成
浏览器视觉和真实点击验收。模拟服务启动及实机限制见[网页控制台手册](../docs/web-control.md)。

## 电脑端光谱仪

光谱仪源码和测试保持独立，沿用已有虚拟环境：

```bash
cd spectrometer
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```

旧的 `tools/test_motor_*.py`、`tools/test_processing.py` 已移入 `tests/firmware/`；
硬件验证脚本不在测试发现目录中。设备维护请看[工具说明](../tools/README.md)。

## 服务解耦回归（2026-11-13）

`test_service_isolation.py` 验证两个页面只加载自己的脚本、另一设备 API 返回 404、
跨服务 Origin/令牌被拒绝、单个服务关闭后另一个仍可用，并在独立解释器中阻断另一设备模块的导入。
同时覆盖光谱状态、预览、登记文件下载以及启动器拒绝旧服务/错误设备/错误模拟模式。

```bash
.venv/bin/python -B -m unittest discover -s tests/webcontrol -v
node tests/webcontrol/motion_ui.test.cjs
node tests/webcontrol/lamp_preview.test.cjs
```

HTTP 测试只连接本机模拟服务，需要允许本机套接字；不连接真实串口或摄像头。
