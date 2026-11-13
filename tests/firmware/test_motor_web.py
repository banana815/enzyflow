"""Offline JSON RPC/session safety tests; no real UART or board imports."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'firmware' / 'lib'))
from enzyflow import motor_web
from test_motor_speed import SpeedBus
from test_motor_safety import FakeClock


class MotorWebTests(unittest.TestCase):
    def setUp(self):
        self.bus = SpeedBus()
        self.clock = FakeClock()
        self.rpc = motor_web.MotorRPC(self.bus, self.clock, session='test-session')

    def request(self, op, args=None, **changes):
        seq = self.rpc.last_seq + 1
        request = {'id': 'request-%d' % seq, 'seq': seq, 'session': self.rpc.session,
                   'op': op, 'args': args or {}}
        request.update(changes)
        return request

    def call(self, op, args=None, **changes):
        return self.rpc.handle(self.request(op, args, **changes))

    def ready(self):
        self.assertTrue(self.call('configure', {'ids': [1, 2, 3], 'long_id': 1, 'confirmed': True})['ok'])
        self.assertTrue(self.call('anchor', {'axis': 1, 'mm': 100, 'sign': -1, 'lead': 4, 'confirmed': True})['ok'])

    def assertLocked(self):
        self.assertTrue(all(a.reference is None for a in self.rpc.controller.axes.values()))

    def test_boot_new_nonce_locked_and_no_hardware_reads(self):
        one = motor_web.MotorRPC(self.bus, self.clock)
        two = motor_web.MotorRPC(self.bus, self.clock)
        self.assertNotEqual(one.session, two.session)
        self.assertEqual(len(one.session), 32)
        self.assertEqual(one.ready()['event'], 'ready')
        self.assertFalse(one.state()['configured'])
        self.assertEqual(self.bus.reads, [])
        self.assertEqual(self.bus.moves, [])

    def test_configure_and_anchor_show_correct_bounds_and_only_measured_axis(self):
        self.ready()
        state = self.rpc.state()
        self.assertEqual([(a['id'], a['min_mm'], a['max_mm']) for a in state['axes']],
                         [(1, 30, 310), (2, 30, 210), (3, 30, 210)])
        self.assertEqual([a['position_mm'] for a in state['axes']], [100, None, None])
        self.assertEqual([a['anchored'] for a in state['axes']], [True, False, False])
        self.assertEqual(self.bus.moves, [])

    def test_state_and_hello_are_cached_without_uart(self):
        self.ready()
        self.bus.on_read = lambda ident: (_ for _ in ()).throw(OSError('must not read'))
        self.assertTrue(self.rpc.state()['feedback_cached'])
        response = self.call('hello')
        self.assertTrue(response['ok'])
        self.assertTrue(response['result']['feedback_cached'])

    def test_idle_status_checks_three_axes_without_moving(self):
        self.ready()
        self.bus.reads = []
        response = self.call('status')
        self.assertTrue(response['ok'])
        self.assertEqual(self.bus.reads, [1, 2, 3])
        self.assertEqual(self.bus.moves, [])

    def test_idle_status_drift_or_driver_reset_locks_all(self):
        for ident in (1, 2, 3):
            with self.subTest(ident=ident):
                self.setUp()
                self.ready()
                self.bus.counts[ident] = 0
                response = self.call('status')
                self.assertFalse(response['ok'])
                self.assertEqual(self.bus.moves, [])
                self.assertLocked()
                self.assertEqual(self.rpc.expected_counts, {})

    def test_idle_status_driver_poweroff_timeout_locks_all(self):
        self.ready()
        def offline(ident):
            if ident == 3:
                raise OSError('Driver poweroff: no position response')
            return self.bus.counts[ident]
        self.bus.on_read = offline
        self.assertFalse(self.call('status')['ok'])
        self.assertEqual(self.bus.moves, [])
        self.assertLocked()
        self.assertEqual(self.rpc.expected_counts, {})

    def test_status_without_baseline_returns_locked_state_without_reads(self):
        self.ready()
        self.call('lock')
        self.bus.on_read = lambda ident: (_ for _ in ()).throw(OSError('must not read'))
        response = self.call('status')
        self.assertTrue(response['ok'])
        self.assertTrue(all(not a['anchored'] for a in response['result']['axes']))

    def test_jog_returns_full_state_and_obeys_speed(self):
        self.ready()
        response = self.call('jog', {'axis': 1, 'delta_mm': .5, 'rpm': 12})
        self.assertTrue(response['ok'])
        self.assertEqual(response['result']['axes'][0]['position_mm'], 100.5)
        self.assertEqual(self.bus.moves, [(1, -6400)])
        self.assertEqual(self.bus.rpms, [12])
        self.assertEqual(response['result']['counts']['2'], 2000000)

    def test_exact_duplicate_jog_is_cached_not_reexecuted(self):
        self.ready()
        request = self.request('jog', {'axis': 1, 'delta_mm': 1, 'rpm': 12})
        first = self.rpc.handle(request)
        count = len(self.bus.reads)
        second = self.rpc.handle(json.loads(json.dumps(request)))
        self.assertEqual(second, first)
        self.assertEqual(len(self.bus.moves), 1)
        self.assertEqual(len(self.bus.reads), count)

    def test_duplicate_id_changed_payload_or_numeric_type_rejected(self):
        self.ready()
        request = self.request('jog', {'axis': 1, 'delta_mm': 1, 'rpm': 12})
        self.assertTrue(self.rpc.handle(request)['ok'])
        changed = dict(request)
        changed['args'] = {'axis': 1, 'delta_mm': 1.0, 'rpm': 12}
        self.assertFalse(self.rpc.handle(changed)['ok'])
        self.assertEqual(len(self.bus.moves), 1)
        self.assertLocked()

    def test_session_mismatch_stale_and_skipped_sequence_fail_closed(self):
        for changes in ({'session': 'old'}, {'seq': 1, 'id': 'unused-old'}, {'seq': 999}):
            with self.subTest(changes=changes):
                self.setUp()
                self.ready()
                self.assertFalse(self.call('jog', {'axis': 1, 'delta_mm': 1}, **changes)['ok'])
                self.assertEqual(self.bus.moves, [])
                self.assertLocked()

    def test_evicted_request_cannot_be_replayed(self):
        self.ready()
        request = self.request('jog', {'axis': 1, 'delta_mm': 1})
        self.assertTrue(self.rpc.handle(request)['ok'])
        for _ in range(motor_web.CACHE_SIZE + 1):
            self.assertTrue(self.call('status')['ok'])
        self.assertNotIn(request['id'], self.rpc.cache)
        self.assertFalse(self.rpc.handle(request)['ok'])
        self.assertEqual(len(self.bus.moves), 1)
        self.assertLessEqual(len(self.rpc.cache), 32)
        self.assertLocked()

    def test_business_failure_consumes_sequence_and_is_cached(self):
        self.ready()
        request = self.request('jog', {'axis': 2, 'delta_mm': 1})
        result = self.rpc.handle(request)
        self.assertFalse(result['ok'])
        self.assertEqual(self.rpc.last_seq, request['seq'])
        self.assertEqual(self.rpc.handle(request), result)
        self.assertTrue(self.call('status')['ok'])
        self.assertEqual(self.bus.moves, [])
        self.assertLocked()

    def test_unknown_operations_noise_and_malformed_requests_never_execute(self):
        for line in ('>>> print(1)', 'import machine', '{', '[]', 'null',
                     '{"id":"x","seq":1,"session":"test-session","op":"eval","args":{}}'):
            with self.subTest(line=line):
                self.setUp()
                self.ready()
                self.assertFalse(self.rpc.handle_line(line)['ok'])
                self.assertEqual(self.bus.moves, [])
                self.assertLocked()

    def test_invalid_numbers_and_unexpected_args_fail_closed(self):
        for args in ({'axis': 1, 'delta_mm': float('nan')},
                     {'axis': 1, 'delta_mm': 1, 'rpm': True},
                     {'axis': 1, 'delta_mm': 1, 'rpm': 31},
                     {'axis': 1, 'delta_mm': 1, 'code': 'print(1)'},
                     {'axis': 1, 'delta_mm': 2}):
            with self.subTest(args=args):
                self.setUp()
                self.ready()
                self.assertFalse(self.call('jog', args)['ok'])
                self.assertEqual(self.bus.moves, [])
                self.assertLocked()

    def test_margin_enforced_and_physical20mm_cannot_normally_jog(self):
        self.ready()
        self.assertTrue(self.call('anchor', {'axis': 1, 'mm': 20, 'sign': -1, 'lead': 4, 'confirmed': True})['ok'])
        self.assertFalse(self.call('jog', {'axis': 1, 'delta_mm': 1, 'rpm': 12})['ok'])
        self.assertEqual(self.bus.moves, [])
        self.assertLocked()

    def test_other_axis_fixed_baseline_rejects_cumulative_drift(self):
        self.ready()
        original = dict(self.rpc.expected_counts)
        self.bus.counts[2] += 400
        self.assertTrue(self.call('jog', {'axis': 1, 'delta_mm': 1})['ok'])
        self.assertEqual(self.rpc.expected_counts[2], original[2])
        self.bus.counts[2] += 400
        self.assertFalse(self.call('jog', {'axis': 1, 'delta_mm': 1})['ok'])
        self.assertEqual(len(self.bus.moves), 1)
        self.assertLocked()

    def test_other_axis_movement_during_jog_faults_after_single_segment(self):
        self.ready()
        self.bus.on_move = lambda ident, pulses: self.bus.counts.__setitem__(3, self.bus.counts[3] + 1000)
        self.assertFalse(self.call('jog', {'axis': 1, 'delta_mm': 1})['ok'])
        self.assertEqual(len(self.bus.moves), 1)
        self.assertLocked()

    def test_fault_and_ambiguous_write_are_not_retried(self):
        self.ready()
        def fail(ident, pulses):
            raise OSError('ambiguous write')
        self.bus.on_move = fail
        request = self.request('jog', {'axis': 1, 'delta_mm': 1})
        failure = self.rpc.handle(request)
        self.assertFalse(failure['ok'])
        self.assertEqual(self.rpc.handle(request), failure)
        self.assertFalse(self.call('jog', {'axis': 1, 'delta_mm': 1})['ok'])
        self.assertEqual(len(self.bus.moves), 1)
        self.assertLocked()

    def test_interrupt_locks_and_propagates_to_end_loop(self):
        self.ready()
        def interrupt(ident, pulses):
            raise KeyboardInterrupt()
        self.bus.on_move = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.call('jog', {'axis': 1, 'delta_mm': 1})
        self.assertEqual(len(self.bus.moves), 1)
        self.assertLocked()

    def test_lock_clears_positions_and_requires_new_anchor(self):
        self.ready()
        result = self.call('lock')
        self.assertTrue(result['ok'])
        self.assertTrue(all(a['position_mm'] is None for a in result['result']['axes']))
        self.assertFalse(self.call('jog', {'axis': 1, 'delta_mm': 1})['ok'])
        self.assertEqual(self.bus.moves, [])

    def test_line_reader_bounds_and_discards_whole_oversize_line(self):
        source = io.StringIO('x' * 2049 + '\n{}\n')
        with self.assertRaises(ValueError):
            motor_web._read_line(source)
        self.assertEqual(motor_web._read_line(source), '{}')
        self.assertIsNone(motor_web._read_line(source))
        with self.assertRaises(ValueError):
            motor_web._read_line(io.StringIO('{'))
        self.assertEqual(motor_web._read_line(io.BytesIO('中文\n'.encode())), '中文')

    def test_run_emits_ready_processes_lines_and_locks_on_eof(self):
        requests = [self.request('configure', {'ids': [1, 2, 3], 'long_id': 1, 'confirmed': True})]
        requests.append({'id': 'anchor', 'seq': 2, 'session': self.rpc.session,
                         'op': 'anchor', 'args': {'axis': 1, 'mm': 100, 'sign': -1, 'lead': 4, 'confirmed': True}})
        source = io.StringIO(''.join(json.dumps(r) + '\n' for r in requests))
        output = io.StringIO()
        with patch.object(motor_web, 'MotorRPC', return_value=self.rpc):
            motor_web.run(bus=self.bus, stream_in=source, stream_out=output, clock=self.clock)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[0]['event'], 'ready')
        self.assertTrue(records[1]['ok'])
        self.assertTrue(records[2]['ok'])
        self.assertEqual(self.bus.moves, [])
        self.assertLocked()


if __name__ == '__main__':
    unittest.main()
