"""Session-scoped, synchronous JSON Lines motor RPC; never evaluates code.

run() is an explicit alternate application entry point, not an import side
effect. One caller owns stdin/stdout and the motor UART. Each request finishes
before another is read; clients must keep at most one request in flight.
"""
import binascii
import json
import math
import os
import sys

from enzyflow.continuous_reset import ContinuousReset
from enzyflow.syringe_pump import SyringePump
from enzyflow.motor_bus import MotorBus
from enzyflow.motor_safety import ThreeAxes, MIN_MM, MARGIN_MM, TOLERANCE_PULSES

PROTOCOL = 1
MAX_LINE_BYTES = 2048
CACHE_SIZE = 32
MAX_SEQUENCE = 2147483647


def _fingerprint(value, depth=0):
    """Order-independent, type-sensitive comparison, also bounds JSON depth."""
    if depth > 8:
        raise ValueError('JSON nesting is too deep')
    if value is None:
        return ['null']
    if type(value) is bool:
        return ['bool', value]
    if type(value) is int:
        return ['int', value]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('Non-finite JSON number')
        return ['float', value]
    if type(value) is str:
        if len(value) > 256:
            raise ValueError('JSON string is too long')
        return ['str', value]
    if type(value) is list:
        if len(value) > 16:
            raise ValueError('JSON array is too long')
        return ['list', [_fingerprint(v, depth + 1) for v in value]]
    if type(value) is dict:
        if len(value) > 16 or any(type(k) is not str for k in value):
            raise ValueError('Invalid JSON object')
        return ['dict', [[k, _fingerprint(value[k], depth + 1)] for k in sorted(value)]]
    raise ValueError('Unsupported JSON value')


def _keys(value, required, optional=()):
    if type(value) is not dict:
        raise ValueError('Arguments must be an object')
    if any(k not in value for k in required) or any(k not in required and k not in optional for k in value):
        raise ValueError('Missing or unexpected argument')


def _read_line(stream, supervisor=None, poller=None):
    """Bound memory before JSON parsing, including an overlong/truncated line."""
    parts = []
    count = 0
    oversized = False
    while True:
        if poller is not None:
            supervisor.tick()
            if not poller.poll(20):
                continue
        char = stream.read(1)
        # MicroPython may print warnings for str/bytes comparisons to stdout,
        # which is also our JSON protocol channel. Both empty types are false.
        if not char:
            if not parts and not oversized:
                return None
            raise ValueError('Truncated JSON line')
        if isinstance(char, bytes):
            # Decode the complete line below so split UTF-8 remains supported.
            encoded = char
        else:
            encoded = char.encode('utf-8')
        if encoded == b'\n':
            if oversized:
                raise ValueError('JSON line exceeds 2048 bytes')
            return b''.join(parts).decode('utf-8').rstrip('\r')
        count += len(encoded)
        if count > MAX_LINE_BYTES:
            oversized = True
            parts = []
        elif not oversized:
            parts.append(encoded)


class MotorRPC:
    def __init__(self, bus, clock=None, session=None):
        self.controller = ThreeAxes(bus, clock)
        self.bus = bus
        self.pump = SyringePump(self.controller)
        self.reset = ContinuousReset(self.controller)
        self.pump_available = True
        self.session = session or binascii.hexlify(os.urandom(16)).decode()
        self.last_seq = 0
        self.long_id = None
        self.counts = {}
        self.expected_counts = {}
        self.last_feedback_ms = None
        self.stopped_axes = None
        self.checkpoint_error = None
        self.driver_states = []
        self.shutdown_requested = False
        self.cache = {}
        self.cache_order = []

    def lock(self, preserve=False):
        self.stopped_axes = None
        self.checkpoint_error = None
        self.reset.stop(preserve=True)
        self.pump.stop(preserve=True)
        try:
            if (not self.reset.stop_fault and not self.pump.stop_fault and
                self.controller.axes and any(a.reference is not None for a in self.controller.axes.values())):
                before = {i: a.last if a.reference is not None else self.counts[i] for i,a in self.controller.axes.items()}
                self._capture_counts()
                if any(abs(self.counts[i]-before[i])>TOLERANCE_PULSES for i in before):
                    raise ValueError('停止后位置变化，不能保存')
                self.stopped_axes = self.state()['axes']
        except Exception as exc:
            self.checkpoint_error = str(exc)
        finally:
            if not preserve or self.checkpoint_error or self.pump.stop_fault or self.reset.stop_fault:
                self.controller.lock()
                self.expected_counts = {}
            else:
                self.expected_counts = dict(self.counts)

    def restore_drives(self, confirmed):
        if confirmed is not True or self.pump.active or self.reset.active or self.controller.busy:
            raise ValueError('恢复前必须确认停止并排除故障')
        # No old host job is retained across this recovery connection. FC is
        # re-issued before FB/FA; no F1/F3 trajectory is sent by this operation.
        self.controller.lock()
        self.expected_counts = {}
        before = {}
        try:
            states = []
            for i in (1,2,3):
                state = self.bus.read_driver_flag(i,0x2C)
                stall = self.bus.read_driver_flag(i,0x2D)
                states.append({'id':i,'state':state,'stall':stall})
                if state not in (0,1) or stall != 0:
                    raise ValueError('ID%d驱动器仍有运行/过载/堵转/欠压状态(%d)，拒绝恢复' % (i,state))
                before[i] = self.bus.read_pulses(i)
            self.clock_sleep(200)
            for i in (1,2,3):
                if abs(self.bus.read_pulses(i)-before[i])>32:
                    raise ValueError('恢复前电机未停稳')
            self.bus._brake_all()
            self.clock_sleep(200)
            for i in (1,2,3):
                if abs(self.bus.read_pulses(i)-before[i])>32:
                    raise ValueError('恢复前位置发生变化')
            for i in (1,2,3):
                self.bus._clear_brake(i)
                self.bus._enable(i)
                self.clock_sleep(200)
                # Observe all axes so a resumed old trajectory cannot hide
                # behind the per-axis acknowledgement sequence.
                for j in (1,2,3):
                    if abs(self.bus.read_pulses(j)-before[j])>32:
                        raise ValueError('恢复时编码器变化，已重新急停')
            for item in states:
                i=item['id']
                item.update(state=self.bus.read_driver_flag(i,0x2C),stall=self.bus.read_driver_flag(i,0x2D),
                            disabled=self.bus.read_driver_flag(i,0x2F))
                if item['state'] not in (0,1) or item['stall']!=0 or item['disabled']!=0:
                    raise ValueError('ID%d驱动器恢复未通过状态验证' % i)
            self.clock_sleep(300)
            for i in (1,2,3):
                if abs(self.bus.read_pulses(i)-before[i])>32:
                    raise ValueError('恢复后电机未保持静止')
            self.driver_states = states
            self.pump.stop_fault = self.reset.stop_fault = False
            self.pump.result = self.reset.result = None
            self.stopped_axes = None
            self.checkpoint_error = None
            self.counts = before
        except BaseException:
            # A failed or interrupted re-enable must not leave a subset enabled.
            self.emergency_stop()
            raise

    def clock_sleep(self, ms):
        self.controller.clock.sleep_ms(ms)

    def emergency_stop(self):
        """Out-of-band interrupt handler: brake all three before any readback."""
        embedded = getattr(sys.implementation, 'name', '') == 'micropython'
        if embedded:
            import micropython
            micropython.kbd_intr(-1)
        failures, details = [], []
        ids = (1, 2, 3)
        self.pump.active = self.reset.active = False
        self.reset.result = "急停已执行"
        self.controller.busy = False
        try:
            # First transmission is a broadcast stop. No reads, clear-state,
            # JSON parsing or ordinary cleanup may precede it.
            for method in ('_brake_all', '_disable_all'):
                try:
                    getattr(self.bus, method)()
                except Exception as exc:
                    details.append({'stage':method, 'error':str(exc)})
            first = {}
            for ident in ids:
                try:
                    self.bus._disable(ident)
                    first[ident] = self.bus.read_pulses(ident)
                except Exception as exc:
                    failures.append(ident)
                    details.append({'axis':ident,'stage':'disable/read','error':str(exc)})
            self.controller.clock.sleep_ms(200)
            for ident in first:
                try:
                    last = self.bus.read_pulses(ident)
                    if abs(last-first[ident]) > 32:
                        raise ValueError('失能后编码器仍在变化')
                    self.counts[ident] = last
                except Exception as exc:
                    failures.append(ident)
                    details.append({'axis':ident,'stage':'standstill','error':str(exc)})
            if failures:
                try:
                    self.bus._disable_all()
                except Exception as exc:
                    details.append({'stage':'disable_fallback','error':str(exc)})
            self.pump.stop_fault = self.reset.stop_fault = bool(failures)
            self.pump.result = '急停已失能锁定' if not failures else '急停未确认，请切断电机电源'
            self.controller.lock()
            self.expected_counts = {}
            return {'event': 'emergency_stopped', 'session': self.session,
                    'confirmed': not failures, 'failed_axes': failures,
                    'details':details, 'disabled':not failures}
        finally:
            if embedded:
                micropython.kbd_intr(3)

    def state(self):
        """Return only cached measurements; do not touch UART or move motors."""
        axes = []
        short_index = 0
        for ident in sorted(self.controller.axes):
            axis = self.controller.axes[ident]
            anchored = axis.reference is not None
            if ident == self.long_id:
                name = '长轴'
            else:
                name = '短轴 ' + ('A' if short_index == 0 else 'B')
                short_index += 1
            position = axis.position(self.counts[ident]) if anchored and ident in self.counts else None
            axes.append({
                'id': ident, 'name': name,
                'min_mm': MIN_MM + MARGIN_MM,
                'max_mm': axis.maximum - MARGIN_MM,
                'physical_min_mm': MIN_MM, 'physical_max_mm': axis.maximum,
                'position_mm': position, 'anchored': anchored,
                'lead_mm': axis.lead_mm, 'forward_sign': axis.sign,
                'max_jog_mm': axis.max_jog_mm if anchored else None,
            })
        return {
            'protocol': PROTOCOL, 'axes': axes,
            'capabilities': ['recover', 'jog_group', 'emergency_stop'] + (['pump', 'pump_rates', 'continuous_reset', 'continuous_move', 'stop_preserve', 'drive_restore', 'graceful_shutdown'] if self.pump_available else []),
            'pump': self.pump.state(),
            'driver_states':self.driver_states, 'reset': self.reset.state(), 'stopped_axes': self.stopped_axes, 'checkpoint_error':self.checkpoint_error,
            'counts': {str(i): self.counts[i] for i in self.counts},
            'configured': bool(self.controller.axes), 'long_id': self.long_id,
            'margin_mm': MARGIN_MM,
            'rpm_min': MotorBus.MIN_RPM, 'rpm_max': MotorBus.MAX_RPM,
            'default_rpm': MotorBus.SPEED_RPM,
            'feedback_cached': True, 'last_feedback_ms': self.last_feedback_ms,
        }

    def ready(self):
        return {'event': 'ready', 'protocol': PROTOCOL, 'session': self.session, 'state': self.state()}

    def _capture_counts(self):
        values = {i: self.controller._stable(i) for i in self.controller.axes}
        self.counts = dict(values)
        self.expected_counts = dict(values)
        self.last_feedback_ms = self.controller.clock.ticks_ms()

    def _check_counts(self):
        if set(self.expected_counts) != set(self.controller.axes):
            raise ValueError('Missing current encoder baseline; configure/anchor first')
        for ident in self.controller.axes:
            value = self.bus.read_pulses(ident)
            if abs(value - self.expected_counts[ident]) > TOLERANCE_PULSES:
                raise ValueError('Unexpected encoder change on ID%d; remeasure all axes' % ident)
            self.counts[ident] = value
        self.last_feedback_ms = self.controller.clock.ticks_ms()

    def _sync_reset(self):
        self.last_feedback_ms = self.controller.clock.ticks_ms()
        self.counts = dict(self.reset.counts)
        if not self.reset.active:
            self.expected_counts = dict(self.reset.counts) if self.reset.result == 'done' else {}

    def tick(self):
        self.pump.tick()
        if self.reset.active:
            self.reset.tick()
            self._sync_reset()

    def _dispatch(self, op, args):
        if op == 'restore_drives':
            _keys(args, ('confirmed',))
            self.restore_drives(args['confirmed'])
            return self.state()
        if op == 'shutdown':
            _keys(args, ())
            self.lock()
            if self.checkpoint_error or self.pump.stop_fault or self.reset.stop_fault:
                raise ValueError('停机未确认，不能正常断开')
            self.shutdown_requested = True
            return dict(self.state(), shutdown_ready=True)
        if op == 'stop_motion':
            _keys(args, ())
            self.lock(preserve=True)
            if self.checkpoint_error or self.pump.stop_fault or self.reset.stop_fault:
                raise ValueError(self.checkpoint_error or '停止未确认')
            return self.state()

        if op not in ('status', 'hello', 'lock'):
            self.stopped_axes = None
        if (self.pump.stop_fault or self.reset.stop_fault) and op not in ('status', 'hello', 'lock'):
            raise ValueError('停止未确认，请先切断电机电源并检查')
        if self.reset.active and op not in ('status', 'hello', 'reset_heartbeat', 'lock'):
            raise ValueError('连续复位中，仅允许状态、心跳和停止')
        if self.pump.active and op not in ('status', 'hello', 'pump_heartbeat', 'lock'):
            raise ValueError('注射运行中，仅允许状态、心跳和停止')
        if op == 'move_continuous':
            _keys(args, ('moves', 'confirmed'), ('recovery',))
            if not self.pump_available:
                raise ValueError('板端非阻塞监控不可用')
            self._check_counts()
            self.stopped_axes = None
            self.pump.result = None
            self.reset.start(args['moves'], 12, args['confirmed'], general=True,
                             recovery=args.get('recovery', False))
            self._sync_reset()
            return self.state()
        if op == 'reset_positions':
            _keys(args, ('moves', 'rpm', 'confirmed'))
            if not self.pump_available:
                raise ValueError('板端非阻塞监控不可用，禁止连续复位')
            self._check_counts()
            self.pump.result = None
            self.reset.start(args['moves'], args['rpm'], args['confirmed'])
            self._sync_reset()
            return self.state()
        if op == 'reset_heartbeat':
            _keys(args, ())
            self.reset.heartbeat()
            self._sync_reset()
            return self.state()
        if op == 'pump_start':
            self.reset.result = None
            _keys(args, ('items', 'confirmed'))
            if not self.pump_available:
                raise ValueError('板端非阻塞监控不可用，禁止连续注射')
            self._check_counts()
            self.pump.start(args['items'], self.long_id, args['confirmed'])
            self.counts = dict(self.pump.counts)
            return self.state()
        if op == 'pump_heartbeat':
            _keys(args, ())
            self.pump.heartbeat()
            self.counts = dict(self.pump.counts)
            if not self.pump.active:
                self.expected_counts = dict(self.pump.counts) if all(self.controller.axes[m['axis']].reference is not None for m in self.pump.members) else {}
            return self.state()
        if op in ('hello', 'status'):
            _keys(args, ())
            # The command loop is synchronous: an idle status request cannot
            # overlap jog(). Detect drive loss/drift before reporting trusted
            # coordinates; state() itself remains a cached, UART-free accessor.
            if self.reset.active:
                self.reset.tick(force=True)
                self._sync_reset()
            if self.pump.active:
                self.pump.tick(force=True)
                self.counts = dict(self.pump.counts)
            if self.pump.result is not None and not self.pump.active:
                self.expected_counts = dict(self.pump.counts) if all(self.controller.axes[m['axis']].reference is not None for m in self.pump.members) else {}
            if op == 'status' and self.expected_counts and not self.pump.active and not self.reset.active:
                self._check_counts()
        elif op == 'lock':
            _keys(args, ())
            self.lock()
        elif op == 'configure':
            self.reset.result = None
            if not self.pump.stop_fault:
                self.pump.result = None
            _keys(args, ('ids', 'long_id', 'confirmed'))
            if type(args['ids']) is not list:
                raise ValueError('ids must be a list')
            self.controller.configure(args['ids'], args['long_id'], confirmed=args['confirmed'])
            self.long_id = args['long_id']
            self._capture_counts()
        elif op == 'anchor':
            self.reset.result = None
            self.pump.result = None
            _keys(args, ('axis', 'mm', 'sign', 'lead', 'confirmed'))
            ident = args['axis']
            self.controller._axis(ident)
            if not self.expected_counts:
                self._capture_counts()
            self.controller.anchor(ident, args['mm'], args['sign'], args['lead'], confirmed=args['confirmed'])
            self.expected_counts[ident] = self.controller.axes[ident].last
            self._check_counts()
        elif op in ('jog', 'recover'):
            _keys(args, ('axis', 'delta_mm', 'confirmed') if op == 'recover' else ('axis', 'delta_mm'), ('rpm',))
            if op == 'recover' and args['confirmed'] is not True:
                raise ValueError('请确认向内恢复方向与空间')
            ident = args['axis']
            self.controller._axis(ident)
            self._check_counts()
            result = self.controller.jog(ident, args['delta_mm'], rpm=args.get('rpm', MotorBus.SPEED_RPM), recovery=op == 'recover')
            self.expected_counts[ident] = self.controller.axes[ident].last
            if op == 'jog':
                self.controller.axes[ident].check_target(result)
            self._check_counts()
        elif op == 'jog_group':
            _keys(args, ('moves',))
            self._check_counts()
            self.controller.jog_group(args['moves'])
            for move in args['moves']:
                ident = move['axis']
                self.expected_counts[ident] = self.controller.axes[ident].last
            self._check_counts()
        else:
            raise ValueError('Unknown operation')
        return self.state()

    def failure(self, exc, ident=None, seq=None):
        self.lock()
        return {'id': ident, 'seq': seq, 'session': self.session, 'ok': False,
                'result': None, 'error': {'code': type(exc).__name__, 'message': str(exc)}}

    def handle(self, request):
        ident = request.get('id') if type(request) is dict else None
        seq = request.get('seq') if type(request) is dict else None
        try:
            _keys(request, ('id', 'seq', 'session', 'op', 'args'))
            if type(ident) is not str or not 1 <= len(ident) <= 64:
                raise ValueError('id must be a nonempty string of at most 64 characters')
            if type(seq) is not int or not 1 <= seq <= MAX_SEQUENCE:
                raise ValueError('seq must be a positive integer')
            if request['session'] != self.session:
                raise ValueError('Stale or incorrect device session')
            if type(request['op']) is not str or type(request['args']) is not dict:
                raise ValueError('op must be a string and args an object')
            signature = json.dumps(_fingerprint(request))
            if ident in self.cache:
                previous, response = self.cache[ident]
                if previous != signature:
                    raise ValueError('Request id already used with a different payload')
                return response
            if seq != self.last_seq + 1:
                raise ValueError('Expected sequence %d; old/skipped requests are rejected' % (self.last_seq + 1))
        except Exception as exc:
            return self.failure(exc, ident, seq)
        self.last_seq = seq
        try:
            result = self._dispatch(request['op'], request['args'])
            response = {'id': ident, 'seq': seq, 'session': self.session, 'ok': True,
                        'result': result, 'error': None}
        except Exception as exc:
            response = self.failure(exc, ident, seq)
        self.cache[ident] = (signature, response)
        self.cache_order.append(ident)
        if len(self.cache_order) > CACHE_SIZE:
            del self.cache[self.cache_order.pop(0)]
        return response

    def handle_line(self, line):
        try:
            if len(line.encode('utf-8')) > MAX_LINE_BYTES:
                raise ValueError('JSON line exceeds 2048 bytes')
            return self.handle(json.loads(line))
        except Exception as exc:
            return self.failure(exc)


def run(bus=None, stream_in=None, stream_out=None, clock=None):
    """Run until EOF/interrupt; each invocation starts locked with a new nonce."""
    if bus is None:
        from machine import UART
        bus = MotorBus(UART(1, 115200, tx=5, rx=6, timeout=10))
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stdout
    rpc = MotorRPC(bus, clock=clock)
    poller = None
    if getattr(sys.implementation, 'name', '') == 'micropython':
        import select
        stream_in = getattr(stream_in, 'buffer', stream_in)
        poller = select.poll()
        poller.register(stream_in, select.POLLIN)
    rpc.pump_available = poller is not None

    def emit(value):
        stream_out.write(json.dumps(value) + '\n')
        if hasattr(stream_out, 'flush'):
            stream_out.flush()

    try:
        emit(rpc.ready())
        while True:
            try:
                line = _read_line(stream_in, rpc, poller)
            except (ValueError, UnicodeError) as exc:
                emit(rpc.failure(exc))
                continue
            if line is None:
                break
            emit(rpc.handle_line(line))
            if rpc.shutdown_requested:
                break
    except KeyboardInterrupt:
        # Interrupt can preempt synchronous jog/group feedback waits. The host
        # keeps the channel open until this explicit stop result is received.
        stopped = rpc.emergency_stop()
        stream_out.write("\n")
        emit(stopped)
    finally:
        rpc.lock()
