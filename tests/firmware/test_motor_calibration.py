"""Offline checks for the supervised five-turn scripts; no UART/device access."""
import contextlib
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'firmware' / 'lib'))
from enzyflow import motor_bus, motor_safety
from enzyflow.motor_safety import ThreeAxes
from test_motor_safety import FakeBus, FakeClock

ROOT = Path(__file__).resolve().parents[2] / 'tools' / 'motor_diagnostics'
INIT = (ROOT / 'calibration_init.py').read_text()
ONE_TURN = (ROOT / 'calibration_one_turn.py').read_text()


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.clock = FakeClock()
        self.controller = ThreeAxes(self.bus, self.clock)
        self.state = {'__name__': '__calibration_test__'}
        self.output = io.StringIO()
        self.uart_calls = []
        self.machine = types.SimpleNamespace(UART=self.uart)

    def uart(self, *args, **kwargs):
        self.uart_calls.append((args, kwargs))
        return object()

    def initialize(self, ident=1, position=70, sign=-1, inert=False, unknown=False):
        source = INIT
        if not inert:
            source = source.replace('CAL_ID = None', 'CAL_ID = %r' % ident)
            source = source.replace('CAL_START_MM = None', 'CAL_START_MM = %r' % position)
            source = source.replace('CAL_SIGN = None', 'CAL_SIGN = %r' % sign)
            source = source.replace('CAL_DIRECTION_UNKNOWN = False', 'CAL_DIRECTION_UNKNOWN = %r' % unknown)
        with patch.dict(sys.modules, {'machine': self.machine}), \
                patch.object(motor_bus, 'MotorBus', return_value=self.bus), \
                patch.object(motor_safety, 'ThreeAxes', return_value=self.controller), \
                contextlib.redirect_stdout(self.output):
            exec(compile(source, str(ROOT / 'calibration_init.py'), 'exec'), self.state)

    def one_turn(self):
        with contextlib.redirect_stdout(self.output):
            exec(compile(ONE_TURN, str(ROOT / 'calibration_one_turn.py'), 'exec'), self.state)

    def assertLocked(self):
        self.assertTrue(all(a.reference is None for a in self.controller.axes.values()))
        self.assertFalse(self.controller.busy)

    def test_unfilled_template_refuses_before_uart_initialization(self):
        with self.assertRaisesRegex(ValueError, 'physical observations'):
            self.initialize(inert=True)
        self.assertEqual(self.uart_calls, [])
        self.assertEqual(self.bus.moves, [])

    def test_initialization_never_moves_or_assumes_reference_for_other_axes(self):
        self.initialize(ident=2, position=70, sign=1)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.state['cal_steps'], 0)
        self.assertIsNone(self.controller.axes[1].reference)
        self.assertIsNone(self.controller.axes[3].reference)
        self.assertEqual(self.controller.axes[2].reference_mm, 70)

    def test_initialization_checks_full_twenty_mm_target_with_short_bounds(self):
        with self.assertRaises(ValueError):
            self.initialize(ident=2, position=201)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])

    def test_known_direction_full_target_cannot_enter_one_cm_margin(self):
        for ident, maximum in ((1, 320), (2, 220), (3, 220)):
            for start in (maximum - 29.999999, maximum - 25):
                with self.subTest(ident=ident, start=start):
                    self.setUp()
                    with self.assertRaisesRegex(ValueError, '安全余量'):
                        self.initialize(ident=ident, position=start, sign=-1)
                    self.assertEqual(self.bus.moves, [])
                    self.assertEqual(self.bus.reads, [])
                    self.assertLocked()

    def test_known_direction_full_test_can_end_exactly_at_safe_upper_limit(self):
        for ident, maximum in ((1, 320), (2, 220), (3, 220)):
            with self.subTest(ident=ident):
                self.setUp()
                self.initialize(ident=ident, position=maximum - 30, sign=-1)
                for _ in range(5):
                    self.one_turn()
                self.assertEqual(self.state['cal_steps'], 20)
                self.assertEqual(len(self.bus.moves), 20)
                self.assertLocked()

    def test_known_direction_calibration_requires_start_outside_margin(self):
        for ident, start in ((1, 29.999999), (2, 20), (3, 29)):
            with self.subTest(ident=ident, start=start):
                self.setUp()
                with self.assertRaisesRegex(ValueError, '安全余量'):
                    self.initialize(ident=ident, position=start, sign=-1)
                self.assertEqual(self.bus.moves, [])
                self.assertEqual(self.bus.reads, [])
                self.assertLocked()

    def test_each_call_has_four_bounded_moves_to_selected_axis_only(self):
        for ident, sign in ((1, -1), (2, 1), (3, -1)):
            with self.subTest(ident=ident, sign=sign):
                self.setUp()
                self.initialize(ident=ident, position=70, sign=sign)
                self.one_turn()
                self.assertEqual(self.bus.moves, [(ident, sign * 12800)] * 4)
                self.assertEqual(self.state['cal_steps'], 4)
                self.assertIn('COMMANDED_TURNS_DONE 1.0', self.output.getvalue())
                self.assertAlmostEqual(self.controller.position(ident), 74)

    def test_five_turn_ceiling_locks_and_refuses_sixth_or_later_calls(self):
        self.initialize()
        for _ in range(5):
            self.one_turn()
        self.assertEqual(self.state['cal_steps'], 20)
        self.assertEqual(self.bus.moves, [(1, -12800)] * 20)
        self.assertLocked()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'all5 turns already done'):
                self.one_turn()
        self.assertEqual(len(self.bus.moves), 20)
        self.assertIn('FIVE_TURNS_FINISHED_ALL_LOCKED', self.output.getvalue())

    def test_first_ambiguous_write_locks_and_cannot_be_retried(self):
        self.initialize()
        def fail(ident, delta):
            raise OSError('first partial write may have moved')
        self.bus.on_move = fail
        with self.assertRaises(OSError):
            self.one_turn()
        self.assertEqual(self.state['cal_steps'], 0)
        self.assertEqual(self.bus.moves, [(1, -12800)])
        self.assertLocked()
        self.bus.on_move = None
        with self.assertRaises(ValueError):
            self.one_turn()
        self.assertEqual(len(self.bus.moves), 1)

    def test_partial_batch_locks_and_cannot_resume_or_repeat(self):
        self.initialize()
        def fail_second(ident, delta):
            if len(self.bus.moves) == 2:
                raise OSError('second partial write may have moved')
        self.bus.on_move = fail_second
        with self.assertRaises(OSError):
            self.one_turn()
        self.assertEqual(self.state['cal_steps'], 1)
        self.assertEqual(len(self.bus.moves), 2)
        self.assertLocked()
        self.bus.on_move = None
        with self.assertRaisesRegex(ValueError, 'Batch incomplete'):
            self.one_turn()
        self.assertEqual(len(self.bus.moves), 2)

    def test_postbatch_read_failure_locks_and_blocks_next_batch(self):
        self.initialize()
        original_read = self.bus.read_pulses
        def fail_unselected(ident):
            if len(self.bus.moves) == 4 and ident == 2:
                raise OSError('postbatch other-axis timeout')
            return original_read(ident)
        self.bus.read_pulses = fail_unselected
        with self.assertRaises(OSError):
            self.one_turn()
        self.assertEqual(self.state['cal_steps'], 4)
        self.assertEqual(len(self.bus.moves), 4)
        self.assertLocked()
        self.bus.read_pulses = original_read
        with self.assertRaises(ValueError):
            self.one_turn()
        self.assertEqual(len(self.bus.moves), 4)

    def test_unselected_axis_drift_locks_and_never_commands_that_axis(self):
        self.initialize(ident=2, position=70, sign=1)
        self.bus.counts[3] += 641
        with self.assertRaisesRegex(ValueError, 'unselected motor'):
            self.one_turn()
        self.assertEqual(self.bus.moves, [(2, 12800)] * 4)
        self.assertLocked()
        with self.assertRaises(ValueError):
            self.one_turn()
        self.assertEqual(len(self.bus.moves), 4)

    def test_control_c_after_motion_locks_and_never_retries(self):
        self.initialize()
        def interrupt(ms):
            if self.bus.moves:
                raise KeyboardInterrupt()
        self.clock.on_sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.one_turn()
        self.assertLocked()
        self.assertEqual(len(self.bus.moves), 1)
        self.clock.on_sleep = None
        with self.assertRaises(ValueError):
            self.one_turn()
        self.assertEqual(len(self.bus.moves), 1)


    def test_unknown_direction_long_axis_at_twenty_six_mm_is_refused(self):
        with self.assertRaises(ValueError):
            self.initialize(ident=1, position=26, sign=1, unknown=True)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])
        self.assertLocked()

    def test_unknown_short_axes_require_full_both_sided_clearance(self):
        for ident in (2, 3):
            for position in (20, 30, 54.999999, 185.000001, 210, 220):
                with self.subTest(ident=ident, position=position):
                    self.setUp()
                    with self.assertRaises(ValueError):
                        self.initialize(ident=ident, position=position, sign=1, unknown=True)
                    self.assertEqual(self.bus.moves, [])
                    self.assertEqual(self.bus.reads, [])
                    self.assertLocked()

    def test_unknown_direction_accepts_measured_short_a_and_b_without_motion(self):
        for ident, position in ((2, 70), (3, 90)):
            with self.subTest(ident=ident, position=position):
                self.setUp()
                self.initialize(ident=ident, position=position, sign=1, unknown=True)
                self.assertEqual(self.bus.moves, [])
                self.assertEqual(self.state['cal_steps'], 0)
                self.assertIs(self.state['CAL_DIRECTION_UNKNOWN'], True)
                self.assertEqual(self.controller.axes[ident].reference_mm, position)
                self.assertEqual(self.controller.axes[ident].sign, 1)

    def test_unknown_direction_exact_thirty_five_mm_clearance(self):
        for ident, maximum in ((1, 320), (2, 220), (3, 220)):
            for position in (55, maximum - 35):
                with self.subTest(ident=ident, position=position):
                    self.setUp()
                    self.initialize(ident=ident, position=position, sign=1, unknown=True)
                    self.assertEqual(self.bus.moves, [])
                    self.assertEqual(self.state['cal_steps'], 0)

    def test_unknown_direction_must_use_positive_encoder_rotation(self):
        with self.assertRaisesRegex(ValueError, 'positive rotation only'):
            self.initialize(ident=2, position=70, sign=-1, unknown=True)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])
        self.assertLocked()

    def test_unknown_direction_initialization_invalidates_old_axis_reference(self):
        self.initialize(ident=1, position=30, sign=-1)
        self.assertIsNotNone(self.controller.axes[1].reference)
        self.initialize(ident=2, position=70, sign=1, unknown=True)
        self.assertIsNone(self.controller.axes[1].reference)
        self.assertIsNotNone(self.controller.axes[2].reference)
        self.assertEqual(self.bus.moves, [])


if __name__ == '__main__':
    unittest.main()
