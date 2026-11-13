"""Offline safety regressions. No machine/UART import and no physical motion."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'firmware' / 'lib'))
from enzyflow.motor_safety import ThreeAxes, PULSES_PER_MM, TOLERANCE_PULSES


class FakeClock:
    def __init__(self):
        self.now = 0
        self.on_sleep = None

    def ticks_ms(self):
        return self.now

    def ticks_diff(self, a, b):
        return a - b

    def sleep_ms(self, ms):
        self.now += ms
        if self.on_sleep:
            self.on_sleep(ms)


class FakeBus:
    def __init__(self):
        self.counts = {1: -1000000, 2: 2000000, 3: -3000000}
        self.moves = []
        self.reads = []
        self.on_read = None
        self.on_move = None
        self.respond_to_motion = True

    def read_pulses(self, ident):
        self.reads.append(ident)
        if self.on_read:
            return self.on_read(ident)
        return self.counts[ident]

    def _move_pulses(self, ident, delta):
        self.moves.append((ident, delta))
        if self.on_move:
            self.on_move(ident, delta)
        if self.respond_to_motion:
            self.counts[ident] += delta


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.bus = FakeBus()
        self.controller = ThreeAxes(self.bus, self.clock)

    def configure(self):
        self.controller.configure([1, 2, 3], 3, confirmed=True)

    def anchor(self, ident=1, mm=100.0, sign=-1):
        return self.controller.anchor(ident, mm, sign, 4.0, confirmed=True)

    def ready(self):
        self.configure()
        self.anchor(1)
        self.anchor(2, sign=1)
        self.anchor(3)

    def assertLocked(self):
        self.assertTrue(all(a.reference is None for a in self.controller.axes.values()))
        self.assertFalse(self.controller.busy)

    def test_boot_is_silent_and_unconfigured_jog_refused(self):
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])
        with self.assertRaises(ValueError):
            self.controller.jog(1, 1)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])

    def test_explicit_confirmed_unique_ids_and_exactly_one_long_axis(self):
        for ids, long_id, confirmed in [([1, 2], 1, True), ([1, 2, 3, 4], 1, True),
                ([1, 1, 3], 3, True), ([1, 2, 3], 4, True), ([1, 2, 3], 3, False),
                ([1, 2, 3], 3, 1), ([0, 2, 3], 3, True), ([True, 2, 3], 3, True),
                ([1.0, 2, 3], 3, True), ([1, 2, 256], 1, True)]:
            with self.subTest(ids=ids, long_id=long_id, confirmed=confirmed):
                with self.assertRaises(ValueError):
                    self.controller.configure(ids, long_id, confirmed)
                self.assertEqual(self.controller.axes, {})
        self.configure()
        self.assertEqual([a.maximum for a in self.controller.axes.values()], [220, 220, 320])
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])

    def test_reconfiguration_invalidates_all_references(self):
        self.ready()
        self.controller.configure([1, 2, 3], 1, True)
        self.assertLocked()
        self.assertEqual(self.controller.axes[1].maximum, 320)

    def test_anchor_required_and_never_assumes_historical_position(self):
        self.configure()
        for ident in (1, 2, 3):
            with self.assertRaises(ValueError):
                self.controller.jog(ident, .5)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])

    def test_anchor_needs_explicit_direction_lead_and_confirmation(self):
        self.configure()
        for direction, lead, confirmed in [(0, 4, True), (True, 4, True), (-1.0, 4, True),
                (-1, 3, True), (-1, float('nan'), True), (-1, 4, False), (-1, 4, 1)]:
            with self.subTest(direction=direction, lead=lead, confirmed=confirmed):
                with self.assertRaises(ValueError):
                    self.controller.anchor(1, 100, direction, lead, confirmed)
                self.assertIsNone(self.controller.axes[1].reference)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])

    def test_anchor_accepts_work_range_endpoints_but_not_outside(self):
        self.configure()
        for ident, maximum in ((1, 220), (2, 220), (3, 320)):
            for mm in (20, maximum):
                self.assertEqual(self.anchor(ident, mm), mm)
            for mm in (20 - 1e-8, maximum + 1e-8):
                with self.assertRaises(ValueError):
                    self.anchor(ident, mm)
        self.assertEqual(self.bus.moves, [])

    def test_invalid_numeric_positions_and_deltas_never_move(self):
        self.configure()
        for value in (float('nan'), float('inf'), -float('inf'), True, False, '100', None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.anchor(1, value)
                self.anchor()
                with self.assertRaises(ValueError):
                    self.controller.jog(1, value)
        self.assertEqual(self.bus.moves, [])

    def test_invalid_motor_ids_never_move(self):
        self.ready()
        for ident in (0, 256, True, 1.0, '1', 4, None):
            with self.subTest(ident=ident):
                with self.assertRaises(ValueError):
                    self.controller.jog(ident, .5)
        self.assertEqual(self.bus.moves, [])

    def test_forward_backward_have_independent_direction_signs(self):
        self.ready()
        for ident, sign in ((1, -1), (2, 1), (3, -1)):
            self.assertAlmostEqual(self.controller.jog(ident, 1), 101)
            self.assertEqual(self.bus.moves[-1], (ident, sign * 12800))
            self.assertAlmostEqual(self.controller.jog(ident, -1), 100)
            self.assertEqual(self.bus.moves[-1], (ident, -sign * 12800))
        self.assertEqual(len(self.bus.moves), 6)

    def test_axis_limits_do_not_leak_from_long_to_short(self):
        self.configure()
        self.anchor(3, 250)
        self.assertAlmostEqual(self.controller.jog(3, 1), 251)
        for ident in (1, 2):
            with self.assertRaises(ValueError):
                self.anchor(ident, 250)
        self.assertEqual(len(self.bus.moves), 1)

    def test_one_cm_margin_is_respected_at_both_ends_of_each_axis(self):
        self.configure()
        for ident, maximum in ((1, 220), (2, 220), (3, 320)):
            for start, delta, result in ((30.5, -.5, 30), (maximum - 10.5, .5, maximum - 10)):
                self.anchor(ident, start)
                self.assertAlmostEqual(self.controller.jog(ident, delta), result)
            for start, delta in ((30.5, -.500001), (maximum - 10.5, .500001)):
                self.anchor(ident, start)
                count = len(self.bus.moves)
                with self.assertRaises(ValueError):
                    self.controller.jog(ident, delta)
                self.assertEqual(len(self.bus.moves), count)
                self.assertLocked()

    def test_physical_endpoints_cannot_reach_one_cm_margin_in_one_jog(self):
        self.configure()
        for ident, maximum in ((1, 220), (2, 220), (3, 320)):
            for start, delta in ((20, -1), (20, 1), (maximum, -1), (maximum, 1)):
                with self.subTest(ident=ident, start=start, delta=delta):
                    self.anchor(ident, start)
                    before = len(self.bus.moves)
                    with self.assertRaises(ValueError):
                        self.controller.jog(ident, delta)
                    self.assertEqual(len(self.bus.moves), before)

    def test_single_inward_step_may_enter_safe_interval(self):
        self.configure()
        for ident, maximum in ((1, 220), (2, 220), (3, 320)):
            for start, delta, target in ((29, 1, 30), (maximum - 9, -1, maximum - 10)):
                with self.subTest(ident=ident, start=start):
                    self.anchor(ident, start)
                    self.assertEqual(self.controller.jog(ident, delta), target)

    def test_safe_interval_endpoints_refuse_further_outward_motion(self):
        self.configure()
        for ident, maximum in ((1, 220), (2, 220), (3, 320)):
            for start, delta in ((30, -.1), (maximum - 10, .1)):
                with self.subTest(ident=ident, start=start):
                    self.anchor(ident, start)
                    before = len(self.bus.moves)
                    with self.assertRaises(ValueError):
                        self.controller.jog(ident, delta)
                    self.assertEqual(len(self.bus.moves), before)
                    self.assertLocked()

    def test_jog_size_is_bounded_and_no_subpulse_motion(self):
        self.ready()
        for delta in (0, 1.000001, -1.000001, 100, -100, .5 / PULSES_PER_MM):
            with self.subTest(delta=delta):
                self.anchor()
                with self.assertRaises(ValueError):
                    self.controller.jog(1, delta)
        self.assertEqual(self.bus.moves, [])

    def test_below_point_one_mm_is_rejected_without_reads_or_motion(self):
        self.ready()
        self.bus.reads.clear()
        for delta in (.099999, -.099999, .05, -.05, .00001):
            with self.subTest(delta=delta):
                with self.assertRaises(ValueError):
                    self.controller.jog(1, delta)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])
        self.assertIsNotNone(self.controller.axes[1].reference)

    def test_quantization_truncates_in_both_directions(self):
        self.ready()
        for delta in (.10001, -.10001):
            before = self.controller.position(1)
            result = self.controller.jog(1, delta)
            self.assertLessEqual(abs(result - before), abs(delta))
            self.assertEqual(abs(self.bus.moves[-1][1]), int(abs(delta) * PULSES_PER_MM))

    def test_unexpected_idle_drift_locks_all_axes(self):
        self.ready()
        self.bus.counts[1] += TOLERANCE_PULSES + 1
        with self.assertRaises(ValueError):
            self.controller.jog(1, 1)
        self.assertEqual(self.bus.moves, [])
        self.assertLocked()

    def test_position_query_detects_reset_and_locks_all_axes(self):
        self.ready()
        self.bus.counts[2] = 0
        with self.assertRaises(ValueError):
            self.controller.position(2)
        self.assertLocked()
        self.assertEqual(self.bus.moves, [])

    def test_unstable_encoder_prevents_anchor(self):
        self.ready()
        def drift(ident):
            self.bus.counts[ident] += TOLERANCE_PULSES + 1
            return self.bus.counts[ident]
        self.bus.on_read = drift
        with self.assertRaises(ValueError):
            self.anchor()
        self.assertLocked()
        self.assertEqual(self.bus.moves, [])

    def test_read_failure_before_motion_locks_all_without_write(self):
        self.ready()
        def fail(ident):
            raise OSError('no reply')
        self.bus.on_read = fail
        with self.assertRaises(OSError):
            self.controller.jog(1, 1)
        self.assertLocked()
        self.assertEqual(self.bus.moves, [])

    def test_ambiguous_partial_write_is_never_retried(self):
        self.ready()
        def fail(ident, delta):
            raise OSError('partial write')
        self.bus.on_move = fail
        with self.assertRaises(OSError):
            self.controller.jog(1, 1)
        self.assertEqual(self.bus.moves, [(1, -12800)])
        self.assertLocked()
        with self.assertRaises(ValueError):
            self.controller.jog(1, 1)
        self.assertEqual(len(self.bus.moves), 1)

    def test_read_failure_after_motion_locks_without_retry(self):
        self.ready()
        def fail(ident):
            raise OSError('lost reply after motion')
        self.bus.on_move = lambda ident, delta: setattr(self.bus, 'on_read', fail)
        with self.assertRaises(OSError):
            self.controller.jog(1, 1)
        self.assertEqual(len(self.bus.moves), 1)
        self.assertLocked()

    def test_interrupt_after_motion_locks_without_claiming_hardware_stop(self):
        self.ready()
        def interrupt(ms):
            if self.bus.moves:
                raise KeyboardInterrupt()
        self.clock.on_sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.controller.jog(1, 1)
        self.assertEqual(len(self.bus.moves), 1)
        self.assertLocked()

    def test_failed_motion_times_out_in_bounded_time_and_no_retry(self):
        self.ready()
        self.bus.respond_to_motion = False
        started = self.clock.now
        with self.assertRaisesRegex(ValueError, '期限'):
            self.controller.jog(1, 1)
        self.assertGreaterEqual(self.clock.now - started, 6000)
        self.assertLessEqual(self.clock.now - started, 6200)
        self.assertEqual(len(self.bus.moves), 1)
        self.assertLocked()

    def test_success_waits_for_settling_before_accepting_next_move(self):
        self.ready()
        started = self.clock.now
        self.controller.jog(1, 1)
        self.assertGreaterEqual(self.clock.now - started, 3200)
        self.assertLess(self.clock.now - started, 6200)
        self.assertFalse(self.controller.busy)
        self.assertIsNotNone(self.controller.axes[1].reference)

    def test_opposite_motion_and_overshoot_are_detected(self):
        for delta_after in (1000, -14000):
            with self.subTest(delta_after=delta_after):
                self.setUp()
                self.ready()
                self.bus.respond_to_motion = False
                def wrong(ident, delta):
                    self.bus.counts[ident] += delta_after
                self.bus.on_move = wrong
                with self.assertRaisesRegex(ValueError, '方向或位移异常'):
                    self.controller.jog(1, 1)
                self.assertEqual(len(self.bus.moves), 1)
                self.assertLocked()

    def test_busy_blocks_overlapping_moves_anchor_and_reconfiguration(self):
        self.ready()
        def reentrant(ident, delta):
            for action in (lambda: self.controller.jog(2, 1),
                           lambda: self.anchor(2),
                           lambda: self.controller.configure([1, 2, 3], 3, True)):
                with self.assertRaises(ValueError):
                    action()
        self.bus.on_move = reentrant
        self.assertEqual(self.controller.jog(1, 1), 101)
        self.assertEqual(self.bus.moves, [(1, -12800)])

    def test_encoder_overflow_prevents_motion(self):
        self.configure()
        self.bus.counts[1] = -2147483640
        self.anchor()
        with self.assertRaisesRegex(ValueError, '溢出'):
            self.controller.jog(1, 1)
        self.assertEqual(self.bus.moves, [])
        self.assertLocked()


    def test_direction_probe_is_exactly_one_half_mm_positive_motor_move(self):
        self.ready()
        travel = self.controller.probe_direction(1, 70, 4, confirmed=True)
        self.assertEqual(travel, .5)
        self.assertEqual(self.bus.moves, [(1, 6400)])
        self.assertLocked()
        for axis in self.controller.axes.values():
            self.assertIsNone(axis.sign)
            self.assertIsNone(axis.reference_mm)
        with self.assertRaises(ValueError):
            self.controller.jog(1, .5)
        self.assertEqual(len(self.bus.moves), 1)

    def test_probe_requires_eleven_mm_clearance_at_both_ends(self):
        for ident, maximum in ((1, 220), (2, 220), (3, 320)):
            for actual in (20, 30, 30.999999, maximum - 10.999999, maximum - 10, maximum):
                with self.subTest(ident=ident, actual=actual):
                    self.setUp()
                    self.ready()
                    self.bus.reads.clear()
                    with self.assertRaises(ValueError):
                        self.controller.probe_direction(ident, actual, 4, True)
                    self.assertEqual(self.bus.moves, [])
                    self.assertEqual(self.bus.reads, [])
                    self.assertLocked()

    def test_probe_exact_eleven_mm_clearance_is_valid_for_each_axis(self):
        for ident, maximum in ((1, 220), (2, 220), (3, 320)):
            for actual in (31, maximum - 11):
                with self.subTest(ident=ident, actual=actual):
                    self.setUp()
                    self.ready()
                    self.assertEqual(self.controller.probe_direction(ident, actual, 4, True), .5)
                    self.assertEqual(self.bus.moves, [(ident, 6400)])
                    self.assertLocked()

    def test_probe_requires_explicit_confirmation_and_measured_lead(self):
        for lead, confirmed in ((4, False), (4, 1), (4, 'YES'), (3, True),
                                (True, True), (float('nan'), True), (float('inf'), True)):
            with self.subTest(lead=lead, confirmed=confirmed):
                self.setUp()
                self.ready()
                self.bus.reads.clear()
                with self.assertRaises(ValueError):
                    self.controller.probe_direction(1, 70, lead, confirmed)
                self.assertEqual(self.bus.moves, [])
                self.assertEqual(self.bus.reads, [])
                self.assertLocked()

    def test_probe_rejects_unmeasured_or_nonfinite_positions(self):
        for position in (None, True, '70', float('nan'), float('inf'), -float('inf')):
            with self.subTest(position=position):
                self.setUp()
                self.ready()
                with self.assertRaises(ValueError):
                    self.controller.probe_direction(1, position, 4, True)
                self.assertEqual(self.bus.moves, [])
                self.assertLocked()

    def test_probe_failed_initial_read_locks_all_with_no_motion(self):
        self.ready()
        def fail(ident):
            raise OSError('probe encoder timeout')
        self.bus.on_read = fail
        with self.assertRaises(OSError):
            self.controller.probe_direction(1, 70, 4, True)
        self.assertEqual(self.bus.moves, [])
        self.assertLocked()

    def test_probe_ambiguous_write_locks_all_without_retry(self):
        self.ready()
        def fail(ident, delta):
            raise OSError('probe partial write')
        self.bus.on_move = fail
        with self.assertRaises(OSError):
            self.controller.probe_direction(1, 70, 4, True)
        self.assertEqual(self.bus.moves, [(1, 6400)])
        self.assertLocked()

    def test_probe_stall_times_out_locks_all_and_never_retries(self):
        self.ready()
        self.bus.respond_to_motion = False
        with self.assertRaisesRegex(ValueError, '期限'):
            self.controller.probe_direction(1, 70, 4, True)
        self.assertEqual(self.bus.moves, [(1, 6400)])
        self.assertLocked()

    def test_probe_control_c_after_motion_always_locks(self):
        self.ready()
        def interrupt(ms):
            if self.bus.moves:
                raise KeyboardInterrupt()
        self.clock.on_sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.controller.probe_direction(1, 70, 4, True)
        self.assertEqual(self.bus.moves, [(1, 6400)])
        self.assertLocked()

    def test_probe_cannot_overlap_an_active_move(self):
        self.ready()
        def reentrant(ident, delta):
            with self.assertRaises(ValueError):
                self.controller.probe_direction(2, 70, 4, True)
        self.bus.on_move = reentrant
        self.assertEqual(self.controller.jog(1, .5), 100.5)
        self.assertEqual(self.bus.moves, [(1, -6400)])


    def test_two_mm_lead_encoder_conversion_and_half_mm_move(self):
        self.configure()
        self.controller.anchor(2, 70, 1, 2, True)
        axis = self.controller.axes[2]
        self.assertEqual(axis.lead_mm, 2)
        self.assertEqual(axis.pulses_per_mm, 25600)
        self.assertEqual(axis.max_jog_mm, .5)
        self.assertAlmostEqual(axis.position(axis.reference + 12800), 70.5)
        self.assertAlmostEqual(axis.position(axis.reference - 12800), 69.5)
        self.assertAlmostEqual(self.controller.jog(2, .5), 70.5)
        self.assertEqual(self.bus.moves, [(2, 12800)])
        self.assertAlmostEqual(self.controller.position(2), 70.5)

    def test_two_mm_lead_over_half_mm_is_refused_before_uart_access(self):
        self.configure()
        self.controller.anchor(2, 70, 1, 2, True)
        self.bus.reads.clear()
        for delta in (.500001, -.500001, 1, -1):
            with self.subTest(delta=delta):
                with self.assertRaises(ValueError):
                    self.controller.jog(2, delta)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])

    def test_two_and_four_mm_axes_have_independent_conversion_and_limits(self):
        self.configure()
        self.controller.anchor(1, 70, -1, 4, True)
        self.controller.anchor(2, 90, 1, 2, True)
        self.controller.anchor(3, 100, -1, 2, True)
        self.assertEqual(self.controller.axes[1].max_jog_mm, 1)
        self.assertEqual(self.controller.axes[2].max_jog_mm, .5)
        self.assertEqual(self.controller.axes[3].max_jog_mm, .5)
        self.assertEqual(self.controller.jog(1, .5), 70.5)
        self.assertEqual(self.controller.jog(2, .5), 90.5)
        self.assertEqual(self.controller.jog(3, .5), 100.5)
        self.assertEqual(self.bus.moves, [(1, -6400), (2, 12800), (3, -12800)])
        self.assertEqual(self.controller.jog(1, 1), 71.5)
        self.assertEqual(self.bus.moves[-1], (1, -12800))

    def test_lead_and_scale_are_unknown_on_boot_and_cleared_by_lock(self):
        self.configure()
        for axis in self.controller.axes.values():
            self.assertIsNone(axis.lead_mm)
            self.assertIsNone(axis.pulses_per_mm)
            with self.assertRaises(ValueError):
                unused = axis.max_jog_mm
        self.controller.anchor(1, 70, -1, 4, True)
        self.controller.anchor(2, 90, 1, 2, True)
        self.controller.lock()
        for axis in self.controller.axes.values():
            self.assertIsNone(axis.lead_mm)
            self.assertIsNone(axis.pulses_per_mm)
            self.assertIsNone(axis.reference)
            with self.assertRaises(ValueError):
                unused = axis.max_jog_mm

    def test_invalid_leads_clear_old_scale_and_never_read_or_move(self):
        self.configure()
        for lead in (3, 8, 0, -2, True, False, '2', None, float('nan'), float('inf')):
            with self.subTest(lead=lead):
                self.controller.anchor(2, 70, 1, 2, True)
                self.bus.reads.clear()
                with self.assertRaises(ValueError):
                    self.controller.anchor(2, 70, 1, lead, True)
                axis = self.controller.axes[2]
                self.assertIsNone(axis.lead_mm)
                self.assertIsNone(axis.pulses_per_mm)
                self.assertIsNone(axis.reference)
                self.assertEqual(self.bus.reads, [])
                self.assertEqual(self.bus.moves, [])

    def test_two_mm_probe_is_half_mm_and_clears_temporary_scale(self):
        self.ready()
        self.assertEqual(self.controller.probe_direction(2, 70, 2, True), .5)
        self.assertEqual(self.bus.moves, [(2, 12800)])
        self.assertLocked()
        for axis in self.controller.axes.values():
            self.assertIsNone(axis.lead_mm)
            self.assertIsNone(axis.pulses_per_mm)

    def test_two_mm_lead_physical_limit_and_margin_remain_correct(self):
        self.configure()
        for ident, maximum in ((1, 220), (2, 220), (3, 320)):
            for start, delta, target in ((30.5, -.5, 30), (maximum - 10.5, .5, maximum - 10)):
                self.controller.anchor(ident, start, 1, 2, True)
                self.assertAlmostEqual(self.controller.jog(ident, delta), target)
            for start, delta in ((30.25, -.5), (maximum - 10.25, .5)):
                self.controller.anchor(ident, start, 1, 2, True)
                before = len(self.bus.moves)
                with self.assertRaises(ValueError):
                    self.controller.jog(ident, delta)
                self.assertEqual(len(self.bus.moves), before)
                self.assertLocked()


if __name__ == '__main__':
    unittest.main()
