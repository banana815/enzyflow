"""Bounded continuous syringe feed supervised on ESP32, without pressure control.

PD42S1 F1 velocity, FC brake, FB clear per manufacturer manual sections
4.4.2, 4.4.12, 4.4.13. Never replay a start. Host renews a 1500ms lease.
"""
import math
from enzyflow.motor_safety import number, TOLERANCE_PULSES

DIAMETER_MM = 19.22
FLOW_ML_MIN = 1.0  # Default and reference flow; each axis can override it.
AREA_MM2 = math.pi * (DIAMETER_MM / 2) ** 2
MM_MIN = FLOW_ML_MIN * 1000 / AREA_MM2


class SyringePump:
    LEASE_MS = 1500
    SAMPLE_MS = 100
    END_MARGIN_MM = 1.0

    def __init__(self, controller):
        self.c = controller
        self.bus = controller.bus
        self.clock = controller.clock
        self.active = False
        self.members = []
        self.result = None
        self.last_heartbeat = 0
        self.last_sample = 0
        self.started = 0
        self.baseline = {}
        self.counts = {}
        self.stop_fault = False

    def state(self):
        return {'active': self.active, 'state': self.result, 'members': self.members,
                'diameter_mm': DIAMETER_MM, 'reference_flow_ml_min': FLOW_ML_MIN,
                'total_flow_ml_min': sum(m['flow_ml_min'] for m in self.members),
                'stop_fault': self.stop_fault}

    def start(self, items, long_id, confirmed):
        if self.active or self.stop_fault or self.c.busy:
            raise ValueError('注射运行中或停止故障尚未处理')
        if confirmed is not True or type(items) is not list or len(items) != 2:
            raise ValueError('必须确认两个短轴及注射器剩余行程')
        shorts = set(self.c.axes) - {long_id}
        plans = []
        segmented = all(type(i) is dict and 'segments' in i for i in items)
        for item in items:
            if type(item) is not dict or set(item) not in ({'axis', 'direction', 'remaining_mm'}, {'axis', 'direction', 'remaining_mm', 'flow_ml_min'},
                    {'axis', 'direction', 'remaining_mm', 'flow_ml_min', 'segments', 'target_volume_ml'}):
                raise ValueError('注射参数无效')
            axis = self.c._axis(item['axis'])
            if axis.ident not in shorts or any(m['axis'] == axis.ident for m in plans):
                raise ValueError('注射只能选择两个不同的短轴')
            direction = item['direction']
            if type(direction) is not int or direction not in (-1, 1):
                raise ValueError('请确认推动注射器的物理方向')
            remaining = number(item['remaining_mm'])
            if not 1.5 <= remaining <= 200:
                raise ValueError('请填写1.5..200mm内的实际剩余推杆行程')
            current = self.c.position(axis.ident)
            axis.check_target(current)
            flow = number(item.get('flow_ml_min', FLOW_ML_MIN))
            mm_min = flow * 1000 / AREA_MM2
            rpm = mm_min / axis.lead_mm
            if not 0.1 <= rpm <= 2:
                raise ValueError('注射速度超出已支持范围')
            mechanical_room = (axis.maximum - 10 - current) if direction > 0 else (current - 30)
            distance = min(remaining - self.END_MARGIN_MM, mechanical_room - 0.5)
            if distance < 0.5:
                raise ValueError('剩余安全行程不足0.5mm')
            phases = []
            if 'segments' in item:
                segments = item['segments']
                if not segmented or type(segments) is not list or not 1 <= len(segments) <= 12:
                    raise ValueError('两个短轴各须设置1–12段')
                total = 0.0
                for segment in segments:
                    if type(segment) is not dict or set(segment) != {'distance_mm', 'flow_ml_min'}:
                        raise ValueError('注射分段格式无效')
                    length = number(segment['distance_mm'])
                    rate = number(segment['flow_ml_min'])
                    if length < 0.5 or not 0.1 <= rate*1000/AREA_MM2/axis.lead_mm <= 2:
                        raise ValueError('每段至少0.5mm，注射速度须在0.1–2rpm')
                    total += length
                    phases.append((total, rate))
                if abs(total-number(item['target_volume_ml'])*1000/AREA_MM2) > 0.000001 or total > distance:
                    raise ValueError('分段总距离不符或超出安全行程')
                distance = total
                flow = phases[0][1]
                mm_min = flow*1000/AREA_MM2
                rpm = mm_min/axis.lead_mm
            end = current + direction * distance
            axis.check_target(end)
            expected = axis.last + int(direction * distance * axis.sign * axis.pulses_per_mm)
            if not -2147483648 <= expected <= 2147483647:
                raise ValueError('注射行程将导致编码器溢出')
            plans.append({'axis': axis.ident, 'direction': direction, 'start_mm': current,
                          'position_mm': current, 'end_mm': end, 'distance_mm': distance,
                          'rpm': rpm, 'flow_ml_min': flow, 'mm_min': mm_min, 'volume_ml': 0.0, 'progress': 0.0})
            if phases:
                plans[-1].update(phases=phases, phase=0, phase_origin=0.0, done=False)
        baseline = {i: self.c._stable(i) for i in self.c.axes}
        for m in plans:
            axis = self.c.axes[m['axis']]
            if abs(baseline[axis.ident] - axis.last) > TOLERANCE_PULSES:
                raise ValueError('启动前检测到位置变化')
        self.members = plans
        self.baseline = baseline
        self.counts = dict(baseline)
        self.started = self.last_heartbeat = self.last_sample = self.clock.ticks_ms()
        self.active = True  # Set BEFORE first write: ambiguous start must brake all.
        self.c.busy = True
        self.result = 'running'
        try:
            for member in self.members:
                axis = self.c.axes[member['axis']]
                if segmented:
                    member['phase_at'] = self.clock.ticks_ms()
                self.bus._velocity(axis.ident, member['rpm'] * member['direction'] * axis.sign)
        except Exception:
            self.stop('启动通信异常；已请求停止两轴')
            raise

    def heartbeat(self):
        # Never revive an expired feed even if a late packet arrives.
        self.tick(force=True)
        if self.active:
            self.last_heartbeat = self.clock.ticks_ms()

    def stop(self, reason='已停止注射', preserve=False):
        # Keep Ctrl+C enabled: emergency interrupt bypasses ordinary cleanup.
        return self._stop(reason, preserve)

    def _stop(self, reason, preserve=False):
        if not self.active:
            return
        self.active = False
        failures = []
        # Try every brake even if the first UART operation fails.
        for m in self.members:
            try:
                self.bus._brake(m['axis'])
            except Exception:
                failures.append(m['axis'])
        # Clearing brake follows the manufacturer's heating warning. Verify
        # standstill afterwards; never send another velocity command here.
        for m in self.members:
            if m['axis'] in failures:
                continue
            try:
                first = self.bus.read_pulses(m['axis'])
                self.clock.sleep_ms(150)
                if abs(self.bus.read_pulses(m['axis'])-first)>32:
                    raise ValueError('刹车后仍在移动，禁止解除刹车')
                self.bus._clear_brake(m['axis'])
                first = self.bus.read_pulses(m['axis'])
                self.clock.sleep_ms(150)
                last = self.bus.read_pulses(m['axis'])
                if abs(last-first) > 32:
                    raise ValueError('停机后编码器仍在变化')
                self.counts[m['axis']] = last
                axis = self.c.axes[m['axis']]
                if m.get('done') and abs(last-axis.last) > 32:
                    raise ValueError('已完成注射的轴发生移动')
                m['position_mm'] = axis.position(last)
                m['volume_ml'] = max(0, (m['position_mm']-m['start_mm'])*m['direction'])*AREA_MM2/1000
            except Exception:
                failures.append(m['axis'])
        self.c.busy = False
        if preserve and not failures:
            for i, value in self.counts.items():
                self.c.axes[i].last = value
        else:
            self.c.lock()
        self.stop_fault = bool(failures)
        self.result = reason if not failures else '停止未确认，请切断电机电源并检查'
        if failures:
            try:
                self.bus._disable_all()
            except Exception:
                pass

    def tick(self, force=False):
        if not self.active:
            return
        now = self.clock.ticks_ms()
        elapsed = self.clock.ticks_diff(now, self.started)
        if self.clock.ticks_diff(now, self.last_heartbeat) >= self.LEASE_MS:
            self.stop('主机心跳超时，已停止注射')
            return
        if not force and self.clock.ticks_diff(now, self.last_sample) < self.SAMPLE_MS:
            return
        self.last_sample = now
        try:
            moving = {m['axis']: m for m in self.members}
            for ident in self.c.axes:
                value = self.bus.read_pulses(ident)
                self.counts[ident] = value
                if ident not in moving:
                    if abs(value-self.baseline[ident]) > TOLERANCE_PULSES:
                        raise ValueError('非注射轴发生变化')
                    continue
                m = moving[ident]
                axis = self.c.axes[ident]
                if m.get('done'):
                    if abs(value-axis.last) > 32:
                        raise ValueError('已完成注射的轴发生移动')
                    continue
                position = axis.check_target(axis.position(value))
                travelled = (position-m['start_mm'])*m['direction']
                phase_elapsed = self.clock.ticks_diff(self.clock.ticks_ms(), m['phase_at']) if 'phases' in m else elapsed
                origin = m.get('phase_origin', 0)
                expected = m['mm_min'] * phase_elapsed/60000
                # Encoder supervision catches reverse, stall and unexpected
                # speed. These are feed checks, not pressure measurements.
                if travelled-origin < -0.025 or abs(travelled-origin-expected) > 0.10 + expected*0.25:
                    raise ValueError('注射方向、速度或编码器反馈异常')
                m['position_mm'] = position
                m['volume_ml'] = max(0, travelled)*AREA_MM2/1000
                m['progress'] = min(1.0, max(0, travelled/m['distance_mm']))
                axis.last = value
                if 'phases' in m:
                    self._advance_phase(m, axis, travelled, phase_elapsed)
                    continue
                if travelled >= m['distance_mm'] - 0.05 or elapsed >= m['distance_mm']/m['mm_min']*60000:
                    self.stop('已到达设定行程，停止两支注射器', preserve=True)
                    return
            if all(m.get('done') for m in self.members):
                self.stop('两支注射器均已到达设定行程', preserve=True)
        except Exception as exc:
            self.stop(str(exc))

    def _advance_phase(self, m, axis, travelled, elapsed):
        end, _ = m['phases'][m['phase']]
        if travelled > end + 0.1:
            raise ValueError('注射超出本段目标距离')
        if travelled < end - 0.01:
            if elapsed > (end-m['phase_origin']+0.1)/m['mm_min']*60000*1.5 + 1000:
                raise ValueError('注射未在允许时间内到达本段目标')
            return
        if m['phase'] + 1 == len(m['phases']):
            ident = axis.ident
            self.bus._brake(ident)
            first = self.bus.read_pulses(ident)
            self.clock.sleep_ms(150)
            last = self.bus.read_pulses(ident)
            if abs(last-first) > 8:
                raise ValueError('单轴注射完成后仍在移动')
            self.bus._clear_brake(ident)
            first = self.bus.read_pulses(ident)
            self.clock.sleep_ms(150)
            last = self.bus.read_pulses(ident)
            if abs(last-first) > 8:
                raise ValueError('解除刹车后编码器仍在移动')
            position = axis.check_target(axis.position(last))
            travel = (position-m['start_mm'])*m['direction']
            if abs(travel-end) > 0.1:
                raise ValueError('停稳后注射距离超出误差范围')
            axis.last = self.counts[ident] = last
            m.update(done=True, position_mm=position, flow_ml_min=0.0, rpm=0.0,
                     volume_ml=travel*AREA_MM2/1000, progress=1.0)
            return
        m['phase'] += 1
        flow = m['phases'][m['phase']][1]
        mm_min = flow*1000/AREA_MM2
        rpm = mm_min/axis.lead_mm
        m.update(phase_origin=travelled, phase_at=self.clock.ticks_ms(),
                 flow_ml_min=flow, mm_min=mm_min, rpm=rpm)
        self.bus._velocity(axis.ident, rpm*m['direction']*axis.sign)
