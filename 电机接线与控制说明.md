# ESP32-S3 电机接线与控制说明

本文依据当前 `enzyflow/` 中的开发板资料、示例代码和备份整理。
开发板为 ESP32-S3（MicroPython 1.27，USB 端口 `/dev/ttyACM0`）。

这套实验箱里的“电机”不是同一种东西，接法完全不同：

| 用途 | 电机类型 | 开发板接口 | 通信方式 | 参考文件 |
|---|---|---|---|---|
| 滴定阀开度 | 总线舵机（Fashion Star 协议，35KG） | 右侧“总线舵机接口 x4” | UART2，115200，TX=GPIO16，RX=GPIO17 | `backups/before_reorganize/uservo.py`、`示例代码.zip` 中的 `09_Servo/` |
| 注射泵直线运动 | 一体化步进/伺服电机（RS485） | 左侧“485 通讯接口” | UART1，115200，TX=GPIO5，RX=GPIO6 | `示例代码.zip` 中的 `05-07_.../motor_ctrl.py` |
| 蠕动泵 | 12V 泵电机 | 底部“固态继电器 x5” | GPIO 开关，非调压 | `示例代码.zip` 中的 `08_Pump/main.py` |

下面重点说明滴定阀使用的**总线舵机**；步进电机和蠕动泵放在后面。

---

## 1. 总线舵机（滴定阀）接线

### 1.1 先认清舵机线

总线舵机是 3 线制：

- `-`：GND
- `+`：VDD（按舵机铭牌要求，板上舵机口由 12V 输入供电）
- `S`：SIGNAL（半双工串行信号，不是普通 PWM）

注意：它虽然也是 3 根线，但 **不能当普通 PWM 舵机用**。不要接到普通 GPIO 上用
`machine.PWM` 驱动，也不要自己把 `S` 同时接到 TX 和 RX；板上“总线舵机接口”
已经处理了半双工信号。

### 1.2 接线步骤

1. **断电**：先拔掉开发板 12V 电源和 USB。
2. 把舵机 3 芯插头插到开发板右侧任意一个“总线舵机接口”。
   - 认准插头防呆方向，不要反插；反接可能损坏舵机或开发板。
   - 多个舵机可以级联：一个舵机接到板上，另一个接到前一个舵机的并联口。
3. 给开发板接入 **12V 电源**。只插 USB 时，逻辑部分能工作，但舵机可能没有足够
   功率，表现为不动、抖动或开发板复位。
4. 用 USB 连接电脑，Thonny 选择 MicroPython (ESP32)、端口 `/dev/ttyACM0`。

> 如果舵机铭牌标的是 7.4V 而不是 12V，不要直接插板上 12V 舵机口；先确认该
> 接口的实际电压或使用合适的降压/稳压。以舵机铭牌和开发板丝印为准。

### 1.3 安装阀门前的“调零”

`滴定实验.pptx` 明确要求：**先通电确定舵机初始角度，再装阀门**。否则阀门可能
在错误角度被强行拧动，导致关不严或堵转。

建议流程：

1. 先不装阀门，只让舵机空载。
2. 上电，用下面的测试代码让舵机到中间角度（例如 0° 或 90°），确认方向和行程。
3. 断电，保持舵机角度不动，再把阀门/联轴器装到“关闭”位置。
4. 上电，用很小角度逐步试开/关，确认没有机械顶死。
5. 记录实际的关闭角度和全开角度，更新 `/valve.json`。
6. 最后再装滴定管并通液体测试。

原来的 `valve.json` 记录为：

```json
{"angle_open": 57.0, "angle_closed": 50.0, "deadband": 7.0}
```

这组数值只相差 7°，且 deadband 也是 7°，看起来不像是通用的可靠标定值。
**不要直接照搬**，一定要按实际阀门重新确认开关方向、角度和限位。

---

## 2. 总线舵机控制

### 2.1 先停止当前自动采集程序

开发板上电后会自动运行 AS7341 遮光检测程序。要手动控制舵机，先在 Thonny
里按 `Ctrl+C` 停止；如果错过 3 秒窗口，重新上电并在启动时按住 `Ctrl+C`。

停止后 REPL 应出现 `>>>`。测试完后可重新启动采集：

```python
from enzyflow.app import run
run()
```

> 不要让 Thonny 和电脑端采集服务同时占用同一个串口。

### 2.2 确认 `uservo.py` 在板上

板上备份中有 `/uservo.py`；README 记录为 `/lib/uservo.py`。两个位置都能被
`import uservo` 找到。如果导入失败，把下面任一文件上传到开发板 `/lib/uservo.py`：

- `backups/before_reorganize/uservo.py`
- 解压 `示例代码.zip` 后的 `示例代码/09_Servo/uservo.py`

Thonny 中：打开文件 → “文件” → “另存为 MicroPython 设备” → 路径填
`/lib/uservo.py`。

### 2.3 最小测试代码

在 Thonny 的 REPL 中逐段运行：

```python
from machine import UART
from uservo import UartServoManager

# 板上总线舵机口：UART2, TX=16, RX=17, 115200
uart = UART(2, tx=16, rx=17, baudrate=115200)

# 先不扫描 0~253 全部 ID，直接 ping 已知的 ID=1
servo = UartServoManager(uart, is_scan_servo=False)

print("ping ID 1:", servo.ping(1))
print("在线舵机:", list(servo.servos))
```

- 返回 `True` 且 `servos` 中出现 `1`，说明供电、接线、ID、波特率基本正确。
- 返回 `False`：先查 12V、插头方向、舵机 ID 和 `uservo` 版本。
- 不知道 ID 时，可以短时间扫描：

```python
servo = UartServoManager(uart, srv_num=10)   # 扫描 0~9
print(list(servo.servos))
```

不要一上来就 `srv_num=254`，扫描 254 个 ID 会很慢。

### 2.4 转到一个角度

```python
import time

# 可选：给这个舵机加软件限位，防止撞机械死点
srv = servo.servos[1]
srv.lowerb = 45.0
srv.upperb = 65.0

# 用 800ms 从当前位置转到 57°（全开附近）
servo.set_servo_angle(1, 57.0, interval=800)
servo.wait()

# 转到 50°（关闭附近）
servo.set_servo_angle(1, 50.0, interval=800)
servo.wait()

# 读回当前角度
print(servo.query_servo_angle(1))
```

`set_servo_angle` 的常用参数：

- `interval=800`：指定运动时间，单位 ms；适合阀门这种慢速动作。
- `velocity=100`：指定转速，单位 °/s；例如 `velocity=50` 更慢。
- `power=400`：功率限制，单位 mW；阀门阻力大时可适当增加。
- `is_mturn=True`：多圈模式；滴定阀一般用单圈，不需要。

### 2.5 使用 `valve.json` 的角度

确认 `/valve.json` 存在后：

```python
import json

with open('/valve.json') as f:
    valve = json.load(f)

print(valve)
servo.set_servo_angle(1, valve['angle_open'], interval=800)
servo.wait()
servo.set_servo_angle(1, valve['angle_closed'], interval=800)
servo.wait()
```

再次强调：`valve.json` 里的 57°/50° 是原板留下的记录，不一定适配你当前的
阀门机构。装好后请重新标定并写回这个文件。

### 2.6 自动滴定中的组合方式

当前 `firmware/lib/enzyflow/app.py` 只做 AS7341 采集，不控制舵机。要做“颜色
突变立即关阀”，需要把舵机控制并入应用逻辑，例如：

1. 启动时创建 `UART(2, ...)` 和 `UartServoManager`；
2. 检测到 `BLOCKED`/颜色突变时调用 `set_servo_angle(..., valve['angle_closed'])`；
3. 采样循环继续运行，但不要在两个程序里同时抢串口。

这属于修改固件，建议先单独验证舵机，再改 `app.py`。需要的话可以在这个项目
里加一个 `valve.py` 模块，把开阀/关阀封装成函数。

---

## 3. 步进电机（注射泵，RS485）

示例代码使用一体化步进/伺服电机，开发板通过左侧 **485 通讯接口** 连接，
不是用 STEP/DIR 脉冲口。

### 3.1 接线

- 电机电源：按电机铭牌接 12V/24V，注意正负极。
- RS485：电机 `A` 接板上 485 `A`，电机 `B` 接板上 485 `B`。
- 地线：电机电源 GND 与开发板 GND 共地。
- 不要把手里的 A/B 直接接到 ESP32 的 GPIO5/6；中间要经过板上的 485 收发器。

### 3.2 代码位置

`MotorController` 在 `示例代码.zip` 的
`示例代码/05-07_电导率+步进+称重测试程序/motor_ctrl.py`，当前本地
`firmware/lib` 中没有这个文件。要使用的话，先解压并上传到开发板
`/lib/motor_ctrl.py`。

### 3.3 最小测试

```python
from machine import UART
from motor_ctrl import MotorController
import time

# 板上 485 接口：UART1, TX=5, RX=6, 115200
uart = UART(1, 115200, tx=5, rx=6, timeout=64)
motor = MotorController(uart, addr=0x01)

motor.scan_motor_id(1, 10)   # 扫描 ID 1~10
time.sleep(1)
motor.clear_motor_angle()    # 当前位置清零
time.sleep(0.5)

# 相对角度：正数正转，负数反转；speed_rps 单位 圈/秒
motor.rotate_motor_angle(0x01, 90, speed_rps=0.2, acc=100)
time.sleep(2)

motor.get_realtime_angle()   # 只发送读取请求
```

注意：`get_realtime_angle()` 只负责发请求，返回数据要靠串口接收线程调用
`motor.rec_chuli(data)` 解析。示例 `main.py` 里的 `re_uart()` 就是做这件事的。
如果只发不读，串口里会留下未处理的应答。

### 当前有效标定（2026-11-03 用户确认）

- 当前位置：100 mm（10 cm），左端机械零点坐标保持不变。
- 可用范围：20–220 mm（2–22 cm）。
- 导程：4 mm/圈；1 cm = 2.5 圈 = 900° = 128000 脉冲。
- 正转减小位置（后退），反转增大位置（前进）。
- 本次读取编码器为 -1277137 脉冲，与 100 mm 对应；未移动或清零电机。
- 配置文件：`firmware/lib/enzyflow/motor_config.py`，板端同名模块在 `/lib/enzyflow/`。
- `checked_delta_degrees(current_mm, target_mm)` 检查当前位置和目标均处于
  20–220 mm 后返回相对角度；此函数本身不发送运动命令。
- 软件边界仅通过该检查函数生效，直接调用底层 `motor_ctrl` 不受此限制。
- 驱动器断电、重启、角度清零或手动移动后，旧编码器参考失效，必须重新标定；
  不可在每次启动时自动假定实际位置仍为 100 mm。

以下为历史实测过程，早期 2 mm/圈换算已被上述 4 mm/圈标定替代。

### 3.4 本次实测记录（2026-11-03）

- 实际连接的是 RS485 步进电机，不是滴定阀的总线舵机。
- 电机 ID：`1`
- 接口：`UART(1, 115200, tx=5, rx=6)`
- 初始角度：约 `9.724°`
- 慢速正转 10°：约 `19.730°`
- 再反转 10°：回到 `9.724°`
- 随后按要求继续正转 10°：停在约 `19.758°`
- 实测确认：**正转是后退，反转是前进**
- 丝杠导程：初测约 `2 mm/圈`；但后续 25 圈实际移动约 10 cm，
  修正为约 `4 mm/圈`（待复测确认）
- 走 1 cm 需要 5 圈，即 `1800°` 或 `256000` 脉冲
- 因标定转了一圈（后退 2 mm），实际反转 6 圈（12 mm）：
  1 圈用于退回标定位置，5 圈用于前进 1 cm
- 随后从当前位置再反转 5 圈（-1800°，-256000 脉冲）前进 1 cm
- 之后又按前进方向反转 10 圈（-3600°，-512000 脉冲），前进 20 mm（2 cm）
- 再反向回左端：按 10 圈/段执行，前 4 段正常，累计 +40 圈（+8 cm）；
  第 5 段后电机不再响应，用户确认滑块已到左端并手动切断电机电源
- 最后确认的多圈角度：约 `+7219.86°`（第 4 段结束）；最终位置以机械左端为准
- 用户确认：左端机械位置定义为 **0 点**
- 因电机电源已手动切断，尚未在驱动器内执行 `clear_motor_angle()` 写零
- 后续重新上电后读数为 `20.13°`（用户视为左端零点），再执行 25 圈；
  用户反馈实际移动约 10 cm，已立即停止
- 当前状态：电机已停止，ESP32 停留在 REPL，未运行采集程序
- 已将 `motor_ctrl.py` 上传到开发板 `/lib/motor_ctrl.py`，大小 5385 字节，
  SHA256 校验通过，`import motor_ctrl` 正常。

> 注意：本记录中的 1 cm/2 cm 换算基于初测 2 mm/圈。后续 25 圈实际约 10 cm，
> 说明实际导程更接近 4 mm/圈；按 4 mm/圈，1 cm = 2.5 圈 = 900° =
> 128000 脉冲。后续必须以复测导程为准。

继续使用前请确认注射泵/执行机构的行程和方向；`rotate_motor_angle()` 是相对
角度，不是绝对角度。正转/反转的方向以本机实测为准，不要仅凭代码符号判断。

---

## 4. 蠕动泵（12V，固态继电器）

示例代码在 `示例代码.zip` 的 `08_Pump/main.py`，使用 5 路固态继电器：

| 通道 | GPIO |
|---|---|
| 0 | 48 |
| 1 | 38 |
| 2 | 39 |
| 3 | 1 |
| 4 | 40 |

接线要点：

- 泵的红色线接 12V 供电，黄色/黑色线接泵的正负极（按泵的说明）。
- 固态继电器串在 12V 回路中，开发板 GPIO 只做控制信号，不直接给泵供电。
- 泵和开发板必须共地。
- 示例注释里“高电平导通/低电平导通”前后有矛盾；先用电表或听声音确认。
  示例主循环中写的是 `value(1)` 导通、`value(0)` 断开。

最小测试：

```python
from machine import Pin
import time

pump = Pin(48, Pin.OUT, value=0)   # 先关
pump.value(1)                       # 导通（以实际硬件为准）
time.sleep(2)
pump.value(0)                       # 断开
```

---

## 5. 如果是一台普通直流电机

实验箱里的电机不是普通有刷直流电机；如果你另外接普通 DC 电机，不能把电机
两根线直接插到 ESP32 GPIO 上。需要：

- 电机驱动板（如 TB6612FNG、L298N）或 MOS 管模块；
- 电机独立电源（按电机额定电压）；
- 驱动板与 ESP32 共地；
- ESP32 只提供方向信号和 PWM 调速信号。

示意代码（引脚仅作示例，按实际空闲 GPIO 修改）：

```python
from machine import Pin, PWM
import time

in1 = Pin(5, Pin.OUT)
in2 = Pin(6, Pin.OUT)
pwm = PWM(Pin(7), freq=1000)
pwm.duty_u16(0)

def run(speed):              # speed: -65535 ~ 65535
    if speed >= 0:
        in1.value(1)
        in2.value(0)
        pwm.duty_u16(speed)
    else:
        in1.value(0)
        in2.value(1)
        pwm.duty_u16(-speed)

run(30000)
time.sleep(1)
run(0)
```

普通 DC 电机建议加续流二极管/电机驱动板自带的保护；不要用 ESP32 的 3.3V
或 5V 引脚直接给电机供电。

---

## 6. 安全与排错清单

上电前：

- [ ] 确认电机类型，不要混用舵机/步进/泵的接口。
- [ ] 确认电源电压与电机铭牌一致。
- [ ] 确认 3 芯舵机插头方向正确，没有反插。
- [ ] 确认电机/驱动板与 ESP32 共地。
- [ ] 阀门先空载调零，再装联轴器。
- [ ] 设置软件限位，先小角度测试。

舵机不动的排查顺序：

1. 12V 电源是否接入，电压是否正常；
2. 舵机插头是否插在“总线舵机接口”，不是普通 GPIO；
3. `servo.ping(1)` 是否返回 `True`；
4. 舵机 ID 是否真的是 1（试 `srv_num=10` 扫描）；
5. 波特率是否 115200，TX/RX 是否 16/17；
6. `uservo.py` 是否在板上且版本匹配；
7. 是否还有另一个程序占用串口。

动作方向反了：

- 舵机：不要交换电源正负极；改目标角度，或调整机械安装方向。
- 步进：改 `rotate_motor_angle` 的角度正负，或按驱动器说明调整方向。
- 泵：交换泵的两根线（断电操作）。

动作抖动/复位：

- 多为 12V 供电能力不足、线太长或共地不良；
- 舵机和大电流泵不要从开发板 3.3V 取电；
- 先断开负载，只测控制信号，再逐级接上。

---

## 7. 相关文件

- `README.txt`：开发板总体说明、现有固件和端口。
- `firmware/lib/enzyflow/config.py`：AS7341 引脚和采集参数（不含电机引脚）。
- `backups/before_reorganize/uservo.py`：总线舵机驱动备份。
- `backups/before_reorganize/valve.json`：原阀门角度记录。
- `示例代码.zip`：
  - `09_Servo/main.py`：总线舵机官方示例；
  - `09_Servo/uservo.py`：舵机驱动；
  - `05-07_.../motor_ctrl.py`：RS485 步进电机控制；
  - `08_Pump/main.py`：蠕动泵/固态继电器示例。
- `滴定实验.pptx`：滴定系统示意图和“先调零再装舵机”的注意事项。
- `自驱动实验技术指导书 _V1.pdf`、`自主控制实验.pdf`：实验箱接口和实验步骤。
