"""Offline fake-bus audit only. Never imports serial, machine, or GPIO."""
import importlib.util
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, '/home/orange/Work/enzyflow/tests/firmware')
sys.path.insert(0, '/home/orange/Work/enzyflow/firmware/lib')
from test_motor_safety import FakeBus, FakeClock
from enzyflow.motor_safety import MARGIN_MM

SCRIPT = '/tmp/enzyflow_id1_test10_control.py'
spec = importlib.util.spec_from_file_location('id1_test10_offline', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Id1Test10 = module.Id1Test10


class Id1Test10Tests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.clock = FakeClock()
        self.initial_counts = dict(self.bus.counts)
        self.session = Id1Test10(self.bus, confirmed=True, clock=self.clock)

    def assertLocked(self):
        self.assertTrue(all(a.reference is None for a in self.session.controller.axes.values()))
        self.assertTrue(self.session.status()['all_locked'])

    def run_rest(self):
        previous = len(self.bus.moves)
        while self.session.state != 'done':
            self.session.batch()
            self.assertLessEqual(len(self.bus.moves) - previous, 3)
            previous = len(self.bus.moves)
            self.assertLessEqual(previous, 12)

    def test_exec_only_defines_class_without_hardware(self):
        namespace = {}
        exec(compile(Path(SCRIPT).read_text(), SCRIPT, 'exec'), namespace)
        self.assertIn('Id1Test10', namespace)
        self.assertNotIn('machine', namespace)
        self.assertEqual(self.bus.moves, [])
        self.assertFalse(hasattr(self.session, 'probe'))
        self.assertFalse(hasattr(self.session, 'verify'))

    def test_init_only_reads_and_anchors_id1_at_51_target61(self):
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.session.start_mm, 51)
        self.assertEqual(self.session.target_mm, 61)
        self.assertEqual(self.session.state, 'ready_for_batches')
        self.assertEqual(self.session.baselines, self.initial_counts)
        self.assertIsNotNone(self.session.controller.axes[1].reference)
        self.assertIsNone(self.session.controller.axes[2].reference)
        self.assertIsNone(self.session.controller.axes[3].reference)
        self.assertEqual(MARGIN_MM, 10)

    def test_confirmation_required_and_missing_axis_not_silently_ignored(self):
        for value in (False, 1, None):
            with self.assertRaises(ValueError):
                Id1Test10(self.bus, confirmed=value, clock=self.clock)
        def missing(ident):
            if ident == 3:
                raise OSError('Missing third motor')
            return self.bus.counts[ident]
        self.bus.on_read = missing
        with self.assertRaises(OSError):
            Id1Test10(self.bus, confirmed=True, clock=self.clock)
        self.assertEqual(self.bus.moves, [])

    def test_first_batch_three_steps_only_id1(self):
        self.session.batch()
        self.assertEqual(self.bus.moves, [(1, -12800)] * 3)
        self.assertEqual(self.session.feedback_mm, 54)
        self.assertEqual(self.bus.counts[2], self.initial_counts[2])
        self.assertEqual(self.bus.counts[3], self.initial_counts[3])

    def test_full_path_exactly_10mm_and_final_lock(self):
        self.run_rest()
        self.assertEqual(self.session.steps, 10)
        self.assertEqual(self.session.feedback_mm, 61)
        self.assertEqual(self.bus.moves, [(1, -12800)] * 10)
        self.assertEqual(self.bus.counts[2], self.initial_counts[2])
        self.assertEqual(self.bus.counts[3], self.initial_counts[3])
        self.assertLocked()
        with self.assertRaises(ValueError):
            self.session.batch()
        self.assertEqual(len(self.bus.moves), 10)

    def test_feedback_extremes_finish_within12_steps_without_reverse(self):
        for error in (-640, 640):
            with self.subTest(error=error):
                self.setUp()
                self.bus.on_move = lambda ident, pulses: self.bus.counts.__setitem__(ident, self.bus.counts[ident] + error)
                self.run_rest()
                self.assertLessEqual(self.session.steps, 12)
                self.assertGreater(self.session.feedback_mm, 60.9)
                self.assertLessEqual(self.session.feedback_mm, 61.05001)
                self.assertTrue(all(ident == 1 and -12800 <= p <= -1280 for ident, p in self.bus.moves))
                self.assertLocked()

    def test_any_axis_unexpected_change_prevents_first_motion(self):
        for ident in (1, 2, 3):
            with self.subTest(ident=ident):
                self.setUp()
                self.bus.counts[ident] += 641
                with self.assertRaises(ValueError):
                    self.session.batch()
                self.assertEqual(self.bus.moves, [])
                self.assertLocked()

    def test_other_axis_cumulative_drift_uses_initial_baseline(self):
        self.bus.counts[2] += 400
        self.session.batch()
        count = len(self.bus.moves)
        self.bus.counts[2] += 400
        with self.assertRaises(ValueError):
            self.session.batch()
        self.assertEqual(len(self.bus.moves), count)
        self.assertEqual(self.session.baselines[2], self.initial_counts[2])
        self.assertLocked()

    def test_other_axis_movement_during_segment_stops_before_next(self):
        self.bus.on_move = lambda ident, pulses: self.bus.counts.__setitem__(2, self.bus.counts[2] + 1000)
        with self.assertRaises(ValueError):
            self.session.batch()
        self.assertEqual(len(self.bus.moves), 1)
        self.assertLocked()

    def test_ambiguous_write_and_interrupt_lock_without_retry(self):
        for exception in (OSError('ambiguous write'), KeyboardInterrupt()):
            with self.subTest(exception=type(exception)):
                self.setUp()
                def fail(ident, pulses):
                    raise exception
                self.bus.on_move = fail
                with self.assertRaises(type(exception)):
                    self.session.batch()
                self.assertEqual(self.session.state, 'faulted')
                self.assertLocked()
                self.bus.on_move = None
                with self.assertRaises(ValueError):
                    self.session.batch()
                self.assertEqual(len(self.bus.moves), 1)

    def test_postmotion_read_failure_never_sends_next(self):
        def read(ident):
            if self.bus.moves and ident == 2:
                raise OSError('Post-move other-axis read failed')
            return self.bus.counts[ident]
        self.bus.on_read = read
        with self.assertRaises(OSError):
            self.session.batch()
        self.assertEqual(len(self.bus.moves), 1)
        self.assertLocked()

    def test_budget_exhaustion_prevents_next_move(self):
        self.session.MAX_STEPS = 2
        with self.assertRaises(ValueError):
            self.session.batch()
        self.assertEqual(len(self.bus.moves), 2)
        self.assertLocked()

    def test_subminimum_tail_does_not_force_another_move(self):
        self.session.controller.anchor(1, 60.95, -1, 4, confirmed=True)
        self.session.batch()
        self.assertEqual(self.session.state, 'done')
        self.assertEqual(self.session.feedback_mm, 60.95)
        self.assertEqual(self.bus.moves, [])
        self.assertLocked()

    def test_excess_target_position_faults_without_motion(self):
        self.session.controller.anchor(1, 61.06, -1, 4, confirmed=True)
        with self.assertRaises(ValueError):
            self.session.batch()
        self.assertEqual(self.bus.moves, [])
        self.assertLocked()

    def test_cancel_locks_and_cannot_continue(self):
        self.session.batch()
        previous = len(self.bus.moves)
        self.session.cancel()
        self.assertEqual(self.session.state, 'cancelled')
        self.assertLocked()
        with self.assertRaises(ValueError):
            self.session.batch()
        self.assertEqual(len(self.bus.moves), previous)


if __name__ == '__main__':
    unittest.main(verbosity=2)
