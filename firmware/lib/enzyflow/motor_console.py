"""Chinese Thonny console; starts locked, never moves without explicit input."""
from enzyflow.motor_bus import MotorBus
from enzyflow.motor_safety import ThreeAxes


def ask(prompt):
    value = input(prompt).strip()
    if value.lower() in ('q', 'quit'):
        raise EOFError()
    return value


def identify(bus):
    print('\n先确认机号与实物。不能从编码器自动判断哪根丝杆更长。')
    print('未知时：断电后只连接一台，重新供电，输入 scan 查询并贴标签；逐台重复。')
    print('若三台机号相同，必须先按驱动器说明逐台设置不同机号；禁止同时连接试转。')
    print('scan: 只读扫描1..10；all: 只读扫描1..255；q: 退出。')
    while True:
        raw = ask('输入已确认的三个不同机号（例如 1 2 3），或 scan/all：')
        if raw in ('scan', 'all'):
            print('只读扫描中，不会运动...')
            print('应答机号：', bus.scan(1, 10 if raw == 'scan' else 255))
            continue
        try:
            ids = [int(v) for v in raw.split()]
            long_id = int(ask('哪一个机号是长丝杆（有效范围2–32cm）？输入机号：'))
            print('长轴：', long_id, '；短轴：', [i for i in ids if i != long_id], '（2–22cm）')
            confirmed = ask('已核对三个机号与实物，唯一长轴正确？输入 YES：') == 'YES'
            return ids, long_id, confirmed
        except ValueError as exc:
            print('输入错误：', exc)


def calibrate(controller, ident):
    axis = controller._axis(ident)
    # Invalidate BEFORE prompting: cancelling midway cannot retain an old anchor.
    axis.lock()
    print('\nID%d %s：请用尺测量滑块距机械零点的坐标，不要撞端点找零。' %
          (ident, '长轴 2–32cm' if axis.maximum == 320 else '短轴 2–22cm'))
    cm = float(ask('当前实测位置（cm，未知请 q 退出，不能填历史位置）：'))
    print('前进 = 坐标增大；后退 = 坐标减小。方向按本轴实际观察填写。')
    direction = ask('已确认前进对应反转填 -1，正转填 1；未知填 ? 做受限方向测试：')
    lead = float(ask('输入本轴实测确认的导程（mm/圈，仅支持2或4，不能套用其他轴）：'))
    if direction == '?':
        print('方向测试：只正转0.5mm等效行程，必须距有效范围两端至少11mm。')
        print('请观察滑块坐标增大还是减小，结束后需重新测量并标定。')
        if ask('已核实本轴位置/导程及两向空间？执行输入 PROBE %d：' % ident) == 'PROBE %d' % ident:
            travel = controller.probe_direction(ident, cm * 10, lead, confirmed=True)
            print('正转编码器等效行程 %.4fmm；全部已锁定。' % travel)
            print('若坐标增大，前进方向填1；若坐标减小，填-1。未观察清楚请退出。')
        return
    sign = int(direction)
    ok = ask('已确认位置、方向、导程且电机静止？输入 YES：') == 'YES'
    controller.anchor(ident, cm * 10, sign, lead, confirmed=ok)
    print('ID%d 本次位置基准已建立；未发送运动命令。' % ident)
    print('ID%d 导程 %g mm/圈；每次允许0.1..%g mm，6rpm。' %
          (ident, axis.lead_mm, axis.max_jog_mm))


def run(bus=None):
    if bus is None:
        from machine import UART
        bus = MotorBus(UART(1, 115200, tx=5, rx=6, timeout=10))
    controller = ThreeAxes(bus)
    print('\n=== 三丝杆安全调试 / Thonny ===')
    print('上电默认锁定，绝不套用历史100mm，也不自动清零/回零/移动。')
    print('短轴2–22cm，长轴2–32cm；两端各保留1cm余量（长轴目标3–31cm，短轴3–21cm）；6rpm。')
    print('每轴导程单独确认：2mm/圈每次最多0.5mm，4mm/圈每次最多1mm。')
    print('没有已验证的硬件急停指令。Ctrl+C只阻止后续命令，已发出的一小段可能继续。')
    print('异常时切断电机电源。断电、复位、手动移动后必须重新运行并测量标定。')
    print('软件限位依赖正确标定，不能代替物理限位开关。输入q可退出。')
    try:
        while not controller.axes:
            try:
                ids, long_id, confirmed = identify(bus)
                controller.configure(ids, long_id, confirmed)
            except (ValueError, OSError) as exc:
                print('未解锁：', exc)
        while any(controller.axes[i].reference is None for i in ids):
            for ident in ids:
                if controller.axes[ident].reference is None:
                    try:
                        calibrate(controller, ident)
                    except (ValueError, OSError) as exc:
                        print('未标定：', exc)
        print('\n命令：f 机号 毫米 = 前进；b 机号 毫米 = 后退；p = 读位置；')
        print('cal 机号 = 重测标定；lock = 锁定全部；q = 退出。每条动作限额按本轴导程。')
        while True:
            try:
                words = ask('motor> ').split()
                if words == ['lock']:
                    controller.lock()
                    print('全部锁定，需逐轴cal重新标定。')
                elif words == ['p']:
                    for ident in ids:
                        if controller.axes[ident].reference is None:
                            print('ID%d 未标定/已锁定' % ident)
                        else:
                            print('ID%d %.4f cm' % (ident, controller.position(ident) / 10))
                elif len(words) == 2 and words[0] == 'cal':
                    calibrate(controller, int(words[1]))
                elif len(words) == 3 and words[0] in ('f', 'b'):
                    ident, distance = int(words[1]), float(words[2])
                    axis = controller._axis(ident)
                    if not 0.1 <= distance <= axis.max_jog_mm:
                        raise ValueError('ID%d 位移必须为0.1..%gmm' % (ident, axis.max_jog_mm))
                    delta = distance if words[0] == 'f' else -distance
                    current = controller.position(ident)
                    axis.check_target(current + delta)
                    print('ID%d：%.4fcm -> %.4fcm；%s %.4fmm' %
                          (ident, current / 10, (current + delta) / 10,
                           '前进' if delta > 0 else '后退', distance))
                    print('请确认驱动器未掉电/复位、机构未被手动移动，且所选机号正确。')
                    if ask('执行请输入 MOVE %d，其他输入取消：' % ident) == 'MOVE %d' % ident:
                        actual = controller.jog(ident, delta)
                        print('编码器反馈位置：%.4fcm；请核对实物。' % (actual / 10))
                    else:
                        print('已取消，未发送运动命令。')
                else:
                    print('命令示例：f 1 0.2 / b 1 0.2 / p / cal 1 / lock / q')
            except (ValueError, OSError) as exc:
                print('拒绝操作/故障：', exc)
                print('若已经开始运动：软件不能终止已下发的一段；异常请切断电机电源。')
    except (KeyboardInterrupt, EOFError):
        print('\n已退出并锁定。没有发送硬件停止指令，已下发的小段仍可能完成。')
    finally:
        controller.lock()
