"""Offline speed limits, transport encoding, and feedback timing checks."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'firmware' / 'lib'))
from enzyflow.motor_bus import MotorBus, MotorBusError
from enzyflow.motor_safety import ThreeAxes
from test_motor_bus import Clock, UART
from test_motor_safety import FakeBus, FakeClock


class SpeedBus(FakeBus):
    def __init__(self):
        super().__init__()
        self.rpms = []

    def _move_pulses(self, ident, delta, rpm=6):
        self.rpms.append(rpm)
        super()._move_pulses(ident, delta)


class SpeedTests(unittest.TestCase):
    def ready(self, speed_bus=True, lead=4):
        self.clock = FakeClock()
        self.bus = SpeedBus() if speed_bus else FakeBus()
        self.controller = ThreeAxes(self.bus, self.clock)
        self.controller.configure([1, 2, 3], 1, confirmed=True)
        self.controller.anchor(1, 100, -1, lead, confirmed=True)

    def test_transport_speed_is_two_bytes_and_checksum_valid(self):
        for rpm in (3, 6, 12, 30):
            clock = Clock()
            uart = UART(clock)
            bus = MotorBus(uart, clock=clock)
            bus._move_pulses(1, -12800, rpm=rpm)
            packet = uart.writes[0]
            self.assertEqual(packet[5:7], bytes((0, rpm)))
            self.assertEqual(packet[3], 1)
            self.assertEqual(packet[-2], sum(packet[:-2]) & 255)
            self.assertEqual(len(uart.writes), 1)

    def test_invalid_speeds_never_write(self):
        for rpm in (2, 31, 0, -1, True, 6.0, '6', None, float('nan')):
            clock = Clock()
            uart = UART(clock)
            bus = MotorBus(uart, clock=clock)
            with self.assertRaises(ValueError):
                bus._move_pulses(1, 12800, rpm=rpm)
            self.assertEqual(uart.writes, [])
            self.ready()
            with self.assertRaises(ValueError):
                self.controller.jog(1, 1, rpm=rpm)
            self.assertEqual(self.bus.moves, [])

    def test_default_call_remains_compatible_with_two_argument_bus(self):
        self.ready(speed_bus=False)
        started = self.clock.now
        self.assertEqual(self.controller.jog(1, 1), 101)
        self.assertEqual(self.clock.now - started, 3200)
        self.assertEqual(self.bus.moves, [(1, -12800)])

    def test_speed_dependent_minimum_dwell(self):
        for rpm, minimum in ((3, 5500), (6, 3000), (12, 1750), (30, 1000)):
            with self.subTest(rpm=rpm):
                self.ready()
                started = self.clock.now
                self.controller.jog(1, 1, rpm=rpm)
                elapsed = self.clock.now - started
                self.assertGreaterEqual(elapsed, 200 + minimum)
                self.assertLess(elapsed, 200 + minimum + 100)
                self.assertEqual(self.bus.rpms, [rpm])
                self.assertEqual(self.bus.moves, [(1, -12800)])

    def test_timing_is_pulse_based_for_two_mm_lead(self):
        self.ready(lead=2)
        started = self.clock.now
        self.controller.jog(1, .5, rpm=12)
        self.assertEqual(self.clock.now - started, 2000)
        self.assertEqual(self.bus.moves, [(1, -12800)])

    def test_slow_timeout_still_locks_and_never_retries(self):
        self.ready()
        self.bus.respond_to_motion = False
        started = self.clock.now
        with self.assertRaises(ValueError):
            self.controller.jog(1, 1, rpm=3)
        self.assertGreaterEqual(self.clock.now - started, 8500)
        self.assertEqual(len(self.bus.moves), 1)
        self.assertTrue(all(a.reference is None for a in self.controller.axes.values()))

    def test_limits_and_tracking_still_apply_at_high_speed(self):
        self.ready()
        self.controller.anchor(1, 310, -1, 4, confirmed=True)
        with self.assertRaises(ValueError):
            self.controller.jog(1, .1, rpm=30)
        self.assertEqual(self.bus.moves, [])
        self.ready()
        self.bus.on_move = lambda ident, pulses: self.bus.counts.__setitem__(ident, self.bus.counts[ident] - 1000)
        with self.assertRaises(ValueError):
            self.controller.jog(1, 1, rpm=30)
        self.assertEqual(len(self.bus.moves), 1)
        self.assertTrue(all(a.reference is None for a in self.controller.axes.values()))

    def test_short_write_not_retried_at_new_speed(self):
        clock = Clock()
        uart = UART(clock, partial_write=True)
        bus = MotorBus(uart, clock=clock)
        with self.assertRaises(MotorBusError):
            bus._move_pulses(1, 12800, rpm=30)
        self.assertEqual(len(uart.writes), 1)


if __name__ == '__main__':
    unittest.main()
