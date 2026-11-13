"""Offline audit of authorized same-session target extension; no hardware."""
import importlib.util
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, '/home/orange/Work/enzyflow/tests/firmware')
sys.path.insert(0, '/home/orange/Work/enzyflow/firmware/lib')
from test_motor_safety import FakeBus, FakeClock
from enzyflow.motor_safety import MARGIN_MM

spec = importlib.util.spec_from_file_location('id1_test10_for_extension', '/tmp/enzyflow_id1_test10_control.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def extend_same_session(session):
    """Audit helper matching proposed guarded transaction; no reference reset."""
    try:
        assert session.state == 'ready_for_batches'
        assert session.start_mm == 51.0
        assert session.target_mm == 61.0
        assert session.steps == 6
        assert MARGIN_MM == 10.0
        assert session.MAX_STEPS == 12
        axis = session.controller.axes[1]
        assert axis.reference is not None
        assert axis.reference_mm == 51.0
        assert axis.sign == -1 and axis.lead_mm == 4
        assert axis.pulses_per_mm == 12800
        assert session.controller.axes[2].reference is None
        assert session.controller.axes[3].reference is None
        session._check_others()
        session._current()
        axis.check_target(101.0)
        session.target_mm = 101.0
        session.MAX_STEPS = 55
        return session.status()
    except BaseException:
        session._fault()
        raise


class ExtensionTests(unittest.TestCase):
    def setup_session(self, error=0):
        self.bus = FakeBus()
        self.clock = FakeClock()
        self.session = module.Id1Test10(self.bus, confirmed=True, clock=self.clock)
        if error:
            self.bus.on_move = lambda ident, pulses: self.bus.counts.__setitem__(ident, self.bus.counts[ident] + error)
        self.initial_baselines = dict(self.session.baselines)
        self.reference = self.session.controller.axes[1].reference
        self.session.batch()
        self.session.batch()
        self.assertEqual(self.session.steps, 6)
        self.assertEqual(self.session.state, 'ready_for_batches')

    def finish(self):
        while self.session.state != 'done':
            count = len(self.bus.moves)
            self.session.batch()
            self.assertLessEqual(len(self.bus.moves) - count, 3)
            self.assertLessEqual(self.session.steps, 55)

    def assertLocked(self):
        self.assertTrue(all(a.reference is None for a in self.session.controller.axes.values()))

    def test_extension_reads_only_and_preserves_reference_and_baselines(self):
        self.setup_session()
        before = list(self.bus.moves)
        extend_same_session(self.session)
        self.assertEqual(self.bus.moves, before)
        self.assertEqual(self.session.target_mm, 101)
        self.assertEqual(self.session.MAX_STEPS, 55)
        self.assertEqual(self.session.steps, 6)
        self.assertEqual(self.session.controller.axes[1].reference, self.reference)
        self.assertEqual(self.session.baselines, self.initial_baselines)

    def test_ideal_extension_finishes_total50_not56(self):
        self.setup_session()
        extend_same_session(self.session)
        self.finish()
        self.assertEqual(self.session.steps, 50)
        self.assertEqual(self.session.feedback_mm, 101)
        self.assertEqual(sum(p for _, p in self.bus.moves), -640000)
        self.assertEqual(self.bus.counts[2], self.initial_baselines[2])
        self.assertEqual(self.bus.counts[3], self.initial_baselines[3])
        self.assertLocked()

    def test_extreme_feedback_errors_finish_within55(self):
        for error in (-640, 640):
            with self.subTest(error=error):
                self.setup_session(error)
                extend_same_session(self.session)
                self.finish()
                self.assertLessEqual(self.session.steps, 55)
                self.assertGreater(self.session.feedback_mm, 100.9)
                self.assertLessEqual(self.session.feedback_mm, 101.05001)
                self.assertTrue(all(i == 1 and -12800 <= p <= -1280 for i, p in self.bus.moves))
                self.assertEqual(self.bus.counts[2], self.initial_baselines[2])
                self.assertEqual(self.bus.counts[3], self.initial_baselines[3])
                self.assertLocked()
                print('tracking_error', error, 'steps', self.session.steps, 'feedback_mm', self.session.feedback_mm)

    def test_repeated_extension_rejected_without_motion(self):
        self.setup_session()
        extend_same_session(self.session)
        with self.assertRaises(AssertionError):
            extend_same_session(self.session)
        self.assertEqual(len(self.bus.moves), 6)
        self.assertEqual(self.session.state, 'faulted')
        self.assertLocked()

    def test_changed_axis_before_extension_faults_without_more_motion(self):
        for ident in (1, 2, 3):
            with self.subTest(ident=ident):
                self.setup_session()
                self.bus.counts[ident] += 641
                with self.assertRaises(ValueError):
                    extend_same_session(self.session)
                self.assertEqual(len(self.bus.moves), 6)
                self.assertEqual(self.session.state, 'faulted')
                self.assertLocked()

    def test_bad_state_or_reference_prevents_extension(self):
        for attribute, value in [('state', 'done'), ('steps', 5), ('start_mm', 52), ('target_mm', 62), ('MAX_STEPS', 13)]:
            with self.subTest(attribute=attribute):
                self.setup_session()
                setattr(self.session, attribute, value)
                with self.assertRaises(AssertionError):
                    extend_same_session(self.session)
                self.assertEqual(len(self.bus.moves), 6)
                self.assertLocked()
        self.setup_session()
        self.session.controller.lock()
        with self.assertRaises(AssertionError):
            extend_same_session(self.session)
        self.assertEqual(len(self.bus.moves), 6)
        self.assertLocked()

    def test_faulted_extended_motion_cannot_replay(self):
        self.setup_session()
        extend_same_session(self.session)
        def fail(ident, pulses):
            raise OSError('ambiguous serial write')
        self.bus.on_move = fail
        with self.assertRaises(OSError):
            self.session.batch()
        self.bus.on_move = None
        with self.assertRaises(ValueError):
            self.session.batch()
        self.assertEqual(len(self.bus.moves), 7)
        self.assertEqual(self.session.state, 'faulted')
        self.assertLocked()

    def test_completed_extended_motion_cannot_replay(self):
        self.setup_session()
        extend_same_session(self.session)
        self.finish()
        with self.assertRaises(ValueError):
            self.session.batch()
        self.assertEqual(len(self.bus.moves), 50)
        self.assertLocked()

    def test_budget55_enforced_before_command56(self):
        self.setup_session()
        extend_same_session(self.session)
        self.session.steps = 55
        with self.assertRaises(ValueError):
            self.session.batch()
        self.assertEqual(len(self.bus.moves), 6)
        self.assertLocked()


if __name__ == '__main__':
    unittest.main(verbosity=2)
