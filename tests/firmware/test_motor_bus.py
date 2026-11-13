"""Offline transport tests. Never imports machine or touches a real UART."""
import importlib.util
from pathlib import Path
import struct
import unittest

SOURCE = Path(__file__).resolve().parents[2] / "firmware/lib/enzyflow/motor_bus.py"
SPEC = importlib.util.spec_from_file_location("motor_bus", SOURCE)
bus_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bus_module)
MotorBus = bus_module.MotorBus


class Clock:
    def __init__(self):
        self.now = 0

    def ticks_ms(self):
        return self.now

    def ticks_diff(self, a, b):
        return a - b

    def sleep_ms(self, value):
        self.now += value


class UART:
    def __init__(self, clock, reply=None, partial_write=False, endless=False):
        self.clock = clock
        self.reply = reply
        self.partial_write = partial_write
        self.endless = endless
        self.events = []
        self.pending = bytearray()
        self.writes = []

    def any(self):
        while self.events and self.events[0][0] <= self.clock.now:
            self.pending.extend(self.events.pop(0)[1])
        if self.endless:
            self.pending.extend(b"noise")
        return len(self.pending)

    def read(self, count):
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result

    def write(self, data):
        self.writes.append(bytes(data))
        if self.reply:
            for delay, chunk in self.reply(data):
                self.events.append((self.clock.now + delay, chunk))
        return len(data) - 1 if self.partial_write else len(data)


def response(motor_id=1, pulses=-1277137, byte3=1):
    data = bytes([0xC5, motor_id, 0x2A, byte3]) + struct.pack(">i", pulses)
    return data + bytes([sum(data) & 255, 0x5C])


class BusTests(unittest.TestCase):
    def make_bus(self, **kwargs):
        self.clock = Clock()
        self.uart = UART(self.clock, **kwargs)
        self.bus = MotorBus(self.uart, timeout_ms=20, clock=self.clock)
        return self.bus

    def test_driver_state_reads_and_enable_ack(self):
        def reply(data):
            body=bytes([0xC5,data[1],data[2],1,0])
            return [(1,body+bytes([sum(body)&255,0x5C]))]
        bus=self.make_bus(reply=reply)
        for op in (0x2C,0x2D,0x2F):self.assertEqual(bus.read_driver_flag(1,op),0)
        bus._enable(1)
        self.assertEqual(self.uart.writes[-1][2:4],bytes([0xFA,0]))

    def test_bad_driver_state_checksum_is_rejected(self):
        bus=self.make_bus(reply=lambda data:[(1,bytes([0xC5,1,0x2C,1,0,0,0x5C]))])
        with self.assertRaises(Exception):bus.read_driver_flag(1,0x2C)

    def test_fragmented_noisy_reply_and_signed_values(self):
        for pulses in (-2147483648, -1277137, 0, 2147483647):
            packet = response(pulses=pulses)
            bus = self.make_bus(reply=lambda data: [(1, b"noise" + packet[:3]),
                                                     (4, packet[3:8]), (7, packet[8:])])
            self.assertEqual(bus.read_pulses(1), pulses)
            self.assertEqual(self.uart.writes, [bytes([0xC5, 1, 0x2A, 0xF0, 0x5C])])

    def test_reject_bad_checksum_address_function_trailer(self):
        for index in (1, 2, 8, 9):
            packet = bytearray(response())
            packet[index] ^= 1
            bus = self.make_bus(reply=lambda data: [(0, packet)])
            with self.assertRaises(bus_module.MotorBusError):
                bus.read_pulses(1)
            self.assertFalse(bus._busy)
            self.assertEqual(len(self.uart.writes), 1)

    def test_wrong_address_with_valid_checksum(self):
        bus = self.make_bus(reply=lambda data: [(0, response(2))])
        with self.assertRaises(bus_module.MotorBusError):
            bus.read_pulses(1)

    def test_unobserved_byte3_rejected_even_with_valid_checksum(self):
        for byte3 in (0, 2, 0xA3, 255):
            bus = self.make_bus(reply=lambda data: [(0, response(byte3=byte3))])
            with self.assertRaises(bus_module.MotorBusError):
                bus.read_pulses(1)
            self.assertEqual(len(self.uart.writes), 1)

    def test_two_responses_are_ambiguous(self):
        bus = self.make_bus(reply=lambda data: [(0, response() + response())])
        with self.assertRaises(bus_module.MotorBusError):
            bus.read_pulses(1)

    def test_timeout_and_incomplete_frame(self):
        for packet in (b"", response()[:9], b"noise"):
            bus = self.make_bus(reply=lambda data: [(0, packet)])
            with self.assertRaises(bus_module.MotorTimeout):
                bus.read_pulses(1)
            self.assertLessEqual(self.clock.now, 25)
            self.assertEqual(len(self.uart.writes), 1)

    def test_stale_input_is_drained_before_query(self):
        bus = self.make_bus(reply=lambda data: [(0, response(pulses=50))])
        self.uart.pending.extend(response(pulses=999))
        self.assertEqual(bus.read_pulses(1), 50)

    def test_endless_input_is_bounded_and_prevents_query(self):
        bus = self.make_bus(endless=True)
        with self.assertRaises(bus_module.MotorBusError):
            bus.read_pulses(1)
        self.assertLessEqual(self.clock.now, 20)
        self.assertEqual(self.uart.writes, [])

    def test_partial_writes_raise_without_retry(self):
        for motion in (False, True):
            bus = self.make_bus(partial_write=True)
            with self.assertRaises(bus_module.MotorBusError):
                bus._move_pulses(1, 100) if motion else bus.read_pulses(1)
            self.assertEqual(len(self.uart.writes), 1)
            self.assertFalse(bus._busy)

    def test_motion_packet_limits_and_direction(self):
        for pulses in (-12800, -1, 1, 12800):
            bus = self.make_bus()
            bus._move_pulses(255, pulses)
            packet, = self.uart.writes
            self.assertEqual(packet[:7], bytes([0xC5, 255, 0xF3,
                                               0 if pulses > 0 else 1, 100, 0, 6]))
            self.assertEqual(struct.unpack(">I", packet[7:11])[0], abs(pulses))
            self.assertEqual(packet[11], sum(packet[:11]) & 255)
            self.assertEqual(packet[12], 0x5C)

    def test_invalid_inputs_never_write(self):
        bus = self.make_bus()
        for motor_id in (0, -1, 256, 1.0, True, float("nan"), float("inf"), "1"):
            with self.assertRaises(ValueError):
                bus.read_pulses(motor_id)
            with self.assertRaises(ValueError):
                bus._move_pulses(motor_id, 100)
        for pulses in (0, -12801, 12801, 0.5, True, float("nan"), float("inf"), "100"):
            with self.assertRaises(ValueError):
                bus._move_pulses(1, pulses)
        self.assertEqual(self.uart.writes, [])

    def test_scan_is_read_only_and_includes_last_id(self):
        bus = self.make_bus(reply=lambda data: [(0, response(data[1]))]
                            if data[1] in (1, 3, 255) else [])
        self.assertEqual(bus.scan(), [1, 3, 255])
        self.assertEqual(len(self.uart.writes), 255)
        self.assertTrue(all(packet[2] == 0x2A for packet in self.uart.writes))

    def test_scan_aborts_corrupt_input(self):
        bus = self.make_bus(reply=lambda data: [(0, response(99))])
        with self.assertRaises(bus_module.MotorBusError):
            bus.scan(1, 3)
        self.assertEqual(len(self.uart.writes), 1)

    def test_reentrancy_rejected_without_writes(self):
        bus = self.make_bus()
        bus._busy = True
        with self.assertRaises(bus_module.MotorBusError):
            bus.read_pulses(1)
        with self.assertRaises(bus_module.MotorBusError):
            bus._move_pulses(1, 100)
        self.assertEqual(self.uart.writes, [])


if __name__ == "__main__":
    unittest.main()

class PriorityStopPacketTests(unittest.TestCase):
    def test_broadcast_stop_bypasses_busy_and_noisy_receive_and_preserves_gap(self):
        c=Clock();u=UART(c,endless=True);b=MotorBus(u,clock=c)
        b._busy=True
        b._brake_all();first=c.now;b._disable_all()
        self.assertEqual(u.writes,[bytes.fromhex('c500fcc15c'),bytes.fromhex('c500fa01c05c')])
        self.assertGreaterEqual(c.now-first,3)
    def test_disable_requires_matching_seven_byte_ack(self):
        c=Clock()
        def reply(p):
            data=bytes([0xC5,p[1],0xFA,1,1]);return [(1,data+bytes([sum(data)&255,0x5c]))]
        u=UART(c,reply=reply);b=MotorBus(u,clock=c);b._disable(2)
        self.assertEqual(u.writes,[bytes.fromhex('c502fa01c25c')])
    def test_unicast_brakes_respect_wire_time_and_gap(self):
        c=Clock();u=UART(c);b=MotorBus(u,clock=c)
        b._brake(1);t=c.now;b._brake(2)
        self.assertGreaterEqual(c.now-t,3)
