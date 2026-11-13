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


def _read_line(stream):
    """Bound memory before JSON parsing, including an overlong/truncated line."""
    parts = []
    count = 0
    oversized = False
    while True:
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
        self.session = session or binascii.hexlify(os.urandom(16)).decode()
        self.last_seq = 0
        self.long_id = None
        self.counts = {}
        self.expected_counts = {}
        self.last_feedback_ms = None
        self.cache = {}
        self.cache_order = []

    def lock(self):
        self.controller.lock()
        self.expected_counts = {}

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
            'capabilities': ['recover', 'jog_group'],
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

    def _dispatch(self, op, args):
        if op in ('hello', 'status'):
            _keys(args, ())
            # The command loop is synchronous: an idle status request cannot
            # overlap jog(). Detect drive loss/drift before reporting trusted
            # coordinates; state() itself remains a cached, UART-free accessor.
            if op == 'status' and self.expected_counts:
                self._check_counts()
        elif op == 'lock':
            _keys(args, ())
            self.lock()
        elif op == 'configure':
            _keys(args, ('ids', 'long_id', 'confirmed'))
            if type(args['ids']) is not list:
                raise ValueError('ids must be a list')
            self.controller.configure(args['ids'], args['long_id'], confirmed=args['confirmed'])
            self.long_id = args['long_id']
            self._capture_counts()
        elif op == 'anchor':
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
        except BaseException as exc:
            response = self.failure(exc, ident, seq)
            if not isinstance(exc, Exception):
                raise
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

    def emit(value):
        stream_out.write(json.dumps(value) + '\n')
        if hasattr(stream_out, 'flush'):
            stream_out.flush()

    try:
        emit(rpc.ready())
        while True:
            try:
                line = _read_line(stream_in)
            except (ValueError, UnicodeError) as exc:
                emit(rpc.failure(exc))
                continue
            if line is None:
                break
            emit(rpc.handle_line(line))
    finally:
        rpc.lock()
