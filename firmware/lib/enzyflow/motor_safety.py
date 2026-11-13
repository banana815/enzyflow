"""Session-only, guarded three-axis jogging. No persisted position assumptions.

Software bounds depend on correct physical identification/calibration and an
uninterrupted drive session; they cannot replace physical limit switches.
"""
import math
from enzyflow.motor_bus import MotorBus, SystemClock

PULSES_PER_REV = 51200
SUPPORTED_LEADS_MM = (2.0, 4.0)
PULSES_PER_MM = 12800  # Historical 4mm scale; motion uses each axis's scale.
MIN_MM = 20.0
MARGIN_MM = 10.0  # User requested 1cm inside each end of the effective range.
MIN_JOG_MM = 0.1
MAX_JOG_MM = 1.0
TOLERANCE_PULSES = 640  # 0.025mm at 2mm/rev; 0.05mm at 4mm/rev.


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('需要有限数值')
    return value


def motor_id(value):
    if type(value) is not int or not 1 <= value <= 255:
        raise ValueError('机号必须为 1..255 的整数，禁止广播')
    return value


class Axis:
    def __init__(self, ident, maximum):
        self.ident = ident
        self.maximum = maximum
        self.reference = None
        self.reference_mm = None
        self.last = None
        self.sign = None
        self.lead_mm = None
        self.pulses_per_mm = None

    @property
    def max_jog_mm(self):
        if self.reference is None:
            raise ValueError('此轴尚未标定，禁止运动')
        return min(MAX_JOG_MM, MotorBus.MAX_PULSES / self.pulses_per_mm)

    def position(self, pulses):
        if self.reference is None:
            raise ValueError('此轴未建立本次位置基准')
        return self.reference_mm + self.sign * (pulses - self.reference) / self.pulses_per_mm

    def check(self, mm):
        number(mm)
        if not MIN_MM <= mm <= self.maximum:
            raise ValueError('ID%d 位置必须在 %.1f..%.1f mm' % (self.ident, MIN_MM, self.maximum))
        return mm

    def check_target(self, mm):
        self.check(mm)
        if not MIN_MM + MARGIN_MM <= mm <= self.maximum - MARGIN_MM:
            raise ValueError('目标必须保留%g mm安全余量，拒绝移动' % MARGIN_MM)
        return mm

    def lock(self):
        self.reference = None
        self.reference_mm = None
        self.last = None
        self.sign = None
        self.lead_mm = None
        self.pulses_per_mm = None


class ThreeAxes:
    def __init__(self, bus, clock=None):
        self.bus = bus
        self.clock = clock or SystemClock()
        self.axes = {}
        self.busy = False

    def configure(self, ids, long_id, confirmed=False):
        if self.busy:
            raise ValueError('运动中禁止改配置')
        self.lock()
        self.axes = {}
        if confirmed is not True or len(ids) != 3:
            raise ValueError('必须人工确认三台机号与实物对应')
        for ident in ids:
            motor_id(ident)
        motor_id(long_id)
        if len(set(ids)) != 3 or long_id not in ids:
            raise ValueError('三台机号必须不同，且必须指定其中唯一长轴')
        self.axes = {i: Axis(i, 320.0 if i == long_id else 220.0) for i in ids}

    def lock(self):
        for axis in self.axes.values():
            axis.lock()

    def _axis(self, ident):
        motor_id(ident)
        if ident not in self.axes:
            raise ValueError('请先确认三轴机号和长短对应关系')
        return self.axes[ident]

    def _stable(self, ident):
        first = self.bus.read_pulses(ident)
        for _ in range(2):
            self.clock.sleep_ms(100)
            current = self.bus.read_pulses(ident)
            if abs(current - first) > TOLERANCE_PULSES:
                raise ValueError('编码器未静止，禁止建立基准/下发新动作')
        return current

    def anchor(self, ident, actual_mm, forward_sign, lead_mm, confirmed=False):
        if self.busy:
            raise ValueError('运动中禁止重建基准')
        axis = self._axis(ident)
        axis.lock()
        axis.check(actual_mm)
        if confirmed is not True or type(forward_sign) is not int or forward_sign not in (-1, 1):
            raise ValueError('必须确认实际位置和前进所对应的正/反转方向')
        if number(lead_mm) not in SUPPORTED_LEADS_MM:
            raise ValueError('本配置仅支持逐轴实测确认的 2 或 4 mm/圈导程')
        try:
            pulses = self._stable(ident)
        except BaseException:
            self.lock()
            raise
        axis.reference_mm = actual_mm
        axis.last = pulses
        axis.sign = forward_sign
        axis.lead_mm = lead_mm
        axis.pulses_per_mm = PULSES_PER_REV / lead_mm
        axis.reference = pulses
        return actual_mm

    def position(self, ident):
        axis = self._axis(ident)
        if axis.reference is None:
            raise ValueError('此轴尚未标定，禁止运动')
        try:
            pulses = self._stable(ident)
            if abs(pulses - axis.last) > TOLERANCE_PULSES:
                raise ValueError('检测到非预期位置变化或驱动复位，请重新测量标定')
            return axis.check(axis.position(pulses))
        except BaseException:
            self.lock()
            raise

    def probe_direction(self, ident, actual_mm, lead_mm, confirmed=False):
        """One +0.5mm-equivalent motor turn, with clearance in BOTH directions.

        Physical sign is intentionally unknown. The temporary + sign measures
        encoder travel only; all references are invalidated afterwards. The
        operator must observe direction and remeasure before normal jogging.
        """
        if self.busy:
            raise ValueError('运动中禁止方向测试')
        self.lock()
        axis = self._axis(ident)
        axis.check(actual_mm)
        # Keep the full margin even if the physical direction is opposite to
        # the provisional sign; add 1mm for the 0.5mm probe and feedback error.
        clearance = MARGIN_MM + 1.0
        if not MIN_MM + clearance <= actual_mm <= axis.maximum - clearance:
            raise ValueError('方向未知时必须距有效行程两端至少%gmm' % clearance)
        if confirmed is not True:
            raise ValueError('先确认本轴实测位置、2或4mm导程及两向空间')
        try:
            self.anchor(ident, actual_mm, 1, lead_mm, confirmed=True)
            return self.jog(ident, 0.5) - actual_mm
        finally:
            self.lock()

    def jog(self, ident, delta_mm, rpm=6, recovery=False):
        """One finite move: <=0.5mm at 2mm/rev, <=1mm at 4mm/rev.

        Positive physical displacement advances, negative retreats.
        """
        if self.busy:
            raise ValueError('已有运动，禁止叠加或排队')
        axis = self._axis(ident)
        number(delta_mm)
        if type(rpm) is not int or not MotorBus.MIN_RPM <= rpm <= MotorBus.MAX_RPM:
            raise ValueError('转速必须为%d..%d rpm的整数' % (MotorBus.MIN_RPM, MotorBus.MAX_RPM))
        if axis.reference is None:
            raise ValueError('未标定，禁止运动')
        if not MIN_JOG_MM <= abs(delta_mm) <= axis.max_jog_mm:
            raise ValueError('ID%d 每次调试位移必须为0.1..%g mm' % (ident, axis.max_jog_mm))
        self.busy = True
        try:
            before = self._stable(ident)
            if abs(before - axis.last) > TOLERANCE_PULSES:
                raise ValueError('位置改变，请重新测量标定')
            current = axis.check(axis.position(before))
            if recovery:
                if rpm > 6 or abs(delta_mm) > 0.5:
                    raise ValueError('恢复仅允许3..6rpm、每次最多0.5mm')
                target = axis.check(current + delta_mm)
                if not ((current < MIN_MM + MARGIN_MM and delta_mm > 0 and target <= MIN_MM + MARGIN_MM + 0.1)
                        or (current > axis.maximum - MARGIN_MM and delta_mm < 0 and target >= axis.maximum - MARGIN_MM - 0.1)):
                    raise ValueError('恢复仅允许从安全区外向内移动')
            else:
                target = axis.check_target(current + delta_mm)
            # Truncate towards zero: never round a requested move outwards.
            pulses = int(abs(delta_mm) * axis.pulses_per_mm)
            if pulses < 1:
                raise ValueError('位移小于单脉冲分辨率')
            signed = pulses * axis.sign * (1 if delta_mm > 0 else -1)
            expected = before + signed
            if not -2147483648 <= expected <= 2147483647:
                raise ValueError('编码器将溢出，拒绝移动')
            axis.check(axis.position(expected))
            # No retries: even an ambiguous write may have started the motor.
            # Keep the two-argument default compatible with existing bus fakes
            # and supervised scripts. A nondefault speed is always explicit.
            if rpm == MotorBus.SPEED_RPM:
                self.bus._move_pulses(ident, signed)
            else:
                self.bus._move_pulses(ident, signed, rpm=rpm)
            travel_ms = (pulses * 60000 + rpm * PULSES_PER_REV - 1) // (rpm * PULSES_PER_REV)
            settle_ms = max(350, travel_ms + 500)
            timeout_ms = max(6000, settle_ms + 3000)
            started = self.clock.ticks_ms()
            settled = 0
            while self.clock.ticks_diff(self.clock.ticks_ms(), started) < timeout_ms:
                self.clock.sleep_ms(100)
                actual = self.bus.read_pulses(ident)
                actual_mm = axis.check(axis.position(actual))
                if actual < min(before, expected) - TOLERANCE_PULSES or actual > max(before, expected) + TOLERANCE_PULSES:
                    raise ValueError('方向或位移异常；请切断电机电源并检查')
                if abs(actual - expected) <= TOLERANCE_PULSES:
                    settled += 1
                else:
                    settled = 0
                # Estimated travel plus settling allowance; 1mm at 6rpm keeps
                # the previous 3s minimum, while slow moves receive more time.
                if settled >= 3 and self.clock.ticks_diff(self.clock.ticks_ms(), started) >= settle_ms:
                    if recovery and (actual_mm - current) * delta_mm <= 0:
                        raise ValueError('恢复反馈没有向安全区内推进')
                    axis.last = actual
                    return actual_mm
            raise ValueError('运动未在期限内完成；请切断电机电源并检查')
        except BaseException:
            self.lock()
            raise
        finally:
            self.busy = False


    def jog_group(self, moves):
        """Validate all axes before writes, then launch bounded segments together.

        Unicast packets have a small start skew; this is not synchronized CNC.
        Any ambiguous write aborts the remaining launches without retry.
        """
        if self.busy or type(moves) is not list or not 1 <= len(moves) <= 3:
            raise ValueError('并行段需要1..3个轴且当前空闲')
        self.busy = True
        try:
            plans = {}
            for move in moves:
                if type(move) is not dict or set(move) != {'axis', 'delta_mm', 'rpm'}:
                    raise ValueError('并行运动参数无效')
                ident = move['axis']
                axis = self._axis(ident)
                if ident in plans or axis.reference is None:
                    raise ValueError('并行轴重复或未标定')
                delta, rpm = number(move['delta_mm']), move['rpm']
                if type(rpm) is not int or not 3 <= rpm <= 30:
                    raise ValueError('转速必须为3..30rpm整数')
                if not MIN_JOG_MM <= abs(delta) <= axis.max_jog_mm:
                    raise ValueError('并行段超出单步距离限制')
                before = self._stable(ident)
                if abs(before - axis.last) > TOLERANCE_PULSES:
                    raise ValueError('位置改变，请重新测量标定')
                current = axis.check_target(axis.position(before))
                axis.check_target(current + delta)
                pulses = int(abs(delta) * axis.pulses_per_mm)
                signed = pulses * axis.sign * (1 if delta > 0 else -1)
                expected = before + signed
                if not -2147483648 <= expected <= 2147483647:
                    raise ValueError('编码器将溢出')
                axis.check_target(axis.position(expected))
                travel_ms = (pulses * 60000 + rpm * PULSES_PER_REV - 1) // (rpm * PULSES_PER_REV)
                plans[ident] = [axis, before, expected, signed, rpm, max(350, travel_ms + 500), 0]
            # Capture stationary axes too, so unintended movement is detected
            # during the group rather than only at its final status response.
            stationary = {i: self._stable(i) for i in self.axes if i not in plans}
            started = self.clock.ticks_ms()
            for ident in plans:
                p = plans[ident]
                self.bus._move_pulses(ident, p[3], rpm=p[4])
            timeout = max(6000, max(p[5] for p in plans.values()) + 3000)
            while self.clock.ticks_diff(self.clock.ticks_ms(), started) < timeout:
                self.clock.sleep_ms(100)
                complete = True
                values = {}
                for ident in plans:
                    p = plans[ident]
                    actual = self.bus.read_pulses(ident)
                    position = p[0].check_target(p[0].position(actual))
                    if not min(p[1], p[2]) - TOLERANCE_PULSES <= actual <= max(p[1], p[2]) + TOLERANCE_PULSES:
                        raise ValueError('并行轴方向或位移异常')
                    p[6] = p[6] + 1 if abs(actual - p[2]) <= TOLERANCE_PULSES else 0
                    values[ident] = (actual, position)
                    if p[6] < 3 or self.clock.ticks_diff(self.clock.ticks_ms(), started) < p[5]:
                        complete = False
                for ident in stationary:
                    if abs(self.bus.read_pulses(ident) - stationary[ident]) > TOLERANCE_PULSES:
                        raise ValueError('检测到未选择的轴发生运动')
                if complete:
                    for ident in plans:
                        plans[ident][0].last = values[ident][0]
                    return {i: values[i][1] for i in values}
            raise ValueError('并行运动未在期限内完成')
        except BaseException:
            self.lock()
            raise
        finally:
            self.busy = False
