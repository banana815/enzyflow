"""Single finite F3 move per axis, supervised without blocking RPC or stop.

Driver owns the final pulse count; software polls direction, progress, bounds,
standstill and a host lease. Never repeats a position/start instruction.
"""
from enzyflow.motor_safety import number, TOLERANCE_PULSES
import sys


class ContinuousReset:
    LEASE_MS = 1500
    SAMPLE_MS = 50

    def __init__(self, controller):
        self.c = controller
        self.bus = controller.bus
        self.clock = controller.clock
        self.active = False
        self.result = None
        self.members = []
        self.counts = {}
        self.plans = {}
        self.stop_fault = False
        self.last_heartbeat = self.last_sample = 0

    def state(self):
        return {'active': self.active, 'state': self.result, 'members': self.members,
                'stop_fault': self.stop_fault, 'continuous': True}

    def start(self, moves, rpm, confirmed):
        if self.active or self.c.busy or self.stop_fault:
            raise ValueError('已有运动或停止故障，禁止复位')
        if confirmed is not True or type(moves) is not list or len(moves) != 3:
            raise ValueError('连续复位需要确认三轴目标')
        if type(rpm) is not int or not 3 <= rpm <= 30:
            raise ValueError('复位速度必须为3..30rpm整数')
        plans, members = {}, []
        for move in moves:
            if type(move) is not dict or set(move) != {'axis', 'target_mm'}:
                raise ValueError('复位目标参数无效')
            axis = self.c._axis(move['axis'])
            if axis.ident in plans or axis.reference is None:
                raise ValueError('复位轴重复或未建立本次位置基准')
            current = axis.check_target(self.c.position(axis.ident))
            target = axis.check_target(number(move['target_mm']))
            # Retain the existing 0.1mm approach margin at soft endpoints.
            approach = min(axis.maximum-10-0.1, max(30.1, target))
            delta = approach-current
            done = abs(target-current) < 0.1 or abs(delta) < 0.1
            signed = 0 if done else int(abs(delta)*axis.pulses_per_mm)*axis.sign*(1 if delta>0 else -1)
            expected = axis.last + signed
            if not -2147483648 <= expected <= 2147483647 or abs(signed)>8000000:
                raise ValueError('复位编码器行程超出范围')
            axis.check_target(axis.position(expected))
            distance = abs(signed)/axis.pulses_per_mm
            travel_ms = distance/(rpm*axis.lead_mm/60)*1000
            plans[axis.ident] = {'before':axis.last, 'expected':expected, 'signed':signed,
                                 'started':0, 'last_count':axis.last, 'progress_count':axis.last,
                                 'progress_at':0, 'settled':0, 'settled_since':None,
                                 'timeout_ms':int(travel_ms*1.5+3000)}
            members.append({'axis':axis.ident, 'start_mm':current, 'target_mm':target,
                            'approach_mm':axis.position(expected), 'position_mm':current,
                            'rpm':rpm, 'done':done, 'progress':1.0 if done else 0.0})
        if set(plans) != set(self.c.axes):
            raise ValueError('必须包含已配置的三根轴')
        baseline = {i:self.c._stable(i) for i in self.c.axes}
        if any(abs(baseline[i]-plans[i]['before'])>TOLERANCE_PULSES for i in baseline):
            raise ValueError('启动前编码器发生变化')
        self.plans, self.members, self.counts = plans, members, baseline
        now = self.clock.ticks_ms()
        self.last_heartbeat = self.last_sample = now
        self.active = True
        self.c.busy = True
        self.result = 'running'
        try:
            for m in self.members:
                p = plans[m['axis']]
                p['started'] = p['progress_at'] = self.clock.ticks_ms()
                if not m['done']:
                    self.bus._move_reset_pulses(m['axis'], p['signed'], rpm=rpm)
            if all(m['done'] for m in self.members):
                self.active = self.c.busy = False
                self.result = 'done'
        except BaseException:
            self.stop('复位启动通信异常')
            raise

    def heartbeat(self):
        self.tick(force=True)
        if self.active:
            self.last_heartbeat = self.clock.ticks_ms()

    def stop(self, reason='已停止连续复位'):
        if not self.active:
            return
        embedded = getattr(sys.implementation,'name','') == 'micropython'
        if embedded:
            import micropython
            micropython.kbd_intr(-1)
        self.active = False
        failures = []
        try:
            # Do not let one failed brake prevent stopping the other axes.
            for m in self.members:
                try:
                    self.bus._brake(m['axis'])
                except Exception:
                    failures.append(m['axis'])
            for m in self.members:
                i = m['axis']
                if i in failures:
                    continue
                try:
                    self.bus._clear_brake(i)
                    first = self.bus.read_pulses(i)
                    self.clock.sleep_ms(150)
                    last = self.bus.read_pulses(i)
                    if abs(last-first)>32:
                        raise ValueError('复位停止后仍在移动')
                    self.counts[i] = last
                    m['position_mm'] = self.c.axes[i].position(last)
                except Exception:
                    failures.append(i)
            if failures:
                for m in self.members:
                    try:
                        self.bus._brake(m['axis'])
                    except Exception:
                        pass
            self.stop_fault = bool(failures)
            self.result = reason if not failures else '复位停止未确认，请切断电机电源'
            self.c.busy = False
            self.c.lock()
        finally:
            if embedded:
                micropython.kbd_intr(3)

    def tick(self, force=False):
        if not self.active:
            return
        now = self.clock.ticks_ms()
        if self.clock.ticks_diff(now,self.last_heartbeat)>=self.LEASE_MS:
            self.stop('复位主机心跳超时')
            return
        if not force and self.clock.ticks_diff(now,self.last_sample)<self.SAMPLE_MS:
            return
        self.last_sample = now
        try:
            for m in self.members:
                i = m['axis']
                axis, p = self.c.axes[i], self.plans[i]
                value = self.bus.read_pulses(i)
                current_time = self.clock.ticks_ms()
                self.counts[i] = value
                position = axis.check_target(axis.position(value))
                m['position_mm'] = position
                if m['done']:
                    if abs(value-axis.last)>TOLERANCE_PULSES:
                        raise ValueError('已到位轴发生非预期移动')
                    continue
                if not min(p['before'],p['expected'])-TOLERANCE_PULSES <= value <= max(p['before'],p['expected'])+TOLERANCE_PULSES:
                    raise ValueError('复位方向或位移异常')
                elapsed = self.clock.ticks_diff(current_time,p['started'])
                travelled = abs(value-p['before'])
                # Reject excessive speed immediately; allow acceleration and
                # settling but not a stalled axis for the full travel duration.
                expected_max = m['rpm']*51200*elapsed/60000
                if travelled > expected_max*1.5+TOLERANCE_PULSES:
                    raise ValueError('复位速度反馈异常')
                if abs(value-p['progress_count'])>64:
                    p['progress_count'],p['progress_at'] = value,current_time
                near = abs(value-p['expected']) <= TOLERANCE_PULSES
                stable = abs(value-p['last_count']) <= 32
                if near and stable:
                    p['settled'] += 1
                    if p['settled_since'] is None:
                        p['settled_since'] = current_time
                else:
                    p['settled'],p['settled_since'] = 0,None
                p['last_count'] = value
                m['progress'] = min(1.0,travelled/abs(p['signed']))
                if p['settled'] >= 3 and self.clock.ticks_diff(current_time,p['settled_since'])>=200:
                    m.update(done=True,progress=1.0)
                    axis.last = value
                elif elapsed > p['timeout_ms'] or (not near and self.clock.ticks_diff(current_time,p['progress_at'])>1500):
                    raise ValueError('复位未推进或到位超时')
            if all(m['done'] for m in self.members):
                self.active = self.c.busy = False
                self.result = 'done'
        except Exception as exc:
            self.stop(str(exc))
