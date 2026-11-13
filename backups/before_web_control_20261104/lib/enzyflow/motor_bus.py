"""Bounded RS485 transport for the supplied C5/5C motor protocol.

Protocol source: Downloads/示例代码.zip, 05-07_.../motor_ctrl.py.
Position responses accept the 10-byte format with byte 3 == 1 (success),
verified individually on all three motors, 2026-11-04. Manufacturer manual
PDxxS1 section 4.2.1 defines this status; errors fail closed:
https://blog.itpub.net/70040860/viewspace-3175721/
Only position reads and bounded relative moves are implemented. This module
does NOT establish mechanical position, identity, direction, or travel limits;
the application must do that before calling the private motion method.
There is no verified stop opcode. A successful write is not a motion ACK.
Use one owner of this UART, with a short UART timeout; no receiver thread.
"""
import struct
import time


class MotorBusError(OSError):
    pass


class MotorTimeout(MotorBusError):
    pass


class _Clock:
    def ticks_ms(self):
        if hasattr(time, "ticks_ms"):
            return time.ticks_ms()
        return int(time.monotonic() * 1000)

    def ticks_diff(self, a, b):
        if hasattr(time, "ticks_diff"):
            return time.ticks_diff(a, b)
        return a - b

    def sleep_ms(self, value):
        if hasattr(time, "sleep_ms"):
            time.sleep_ms(value)
        else:
            time.sleep(value / 1000)


# Shared clock contract for the higher-level safety controller.
SystemClock = _Clock


def _integer(value, lower, upper, name):
    # bool, fractional floats, NaN, infinity, and strings must never become IDs
    # or pulse counts through implicit conversion.
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("%s must be an integer in %s..%s" % (name, lower, upper))
    return value


def _packet(payload):
    return bytes(payload + [sum(payload) & 0xFF, 0x5C])


class MotorBus:
    MAX_PULSES = 12800  # 1 mm ONLY when the confirmed lead is 4 mm/revolution.
    SPEED_RPM = 6
    ACCELERATION = 100

    def __init__(self, uart, timeout_ms=200, clock=None):
        self.uart = uart
        self.timeout_ms = _integer(timeout_ms, 10, 2000, "timeout_ms")
        self.clock = clock if clock is not None else _Clock()
        self._busy = False

    def _enter(self):
        if self._busy:
            raise MotorBusError("RS485 transaction already active")
        self._busy = True

    def _write(self, packet):
        # Never retry a short/failed write: the driver may have acted already.
        written = self.uart.write(packet)
        if written != len(packet):
            raise MotorBusError("Incomplete UART write; do not retry motion")

    def _drain(self):
        """Require 5 ms of quiet, fail after 20 ms or excessive stale input."""
        start = quiet = self.clock.ticks_ms()
        received = 0
        while self.clock.ticks_diff(self.clock.ticks_ms(), start) < 20:
            count = self.uart.any()
            if count:
                data = self.uart.read(min(count, 128))
                if not data:
                    raise MotorBusError("UART reports data but cannot read it")
                received += len(data)
                if received > 2048:
                    raise MotorBusError("Excessive stale RS485 input")
                quiet = self.clock.ticks_ms()
            elif self.clock.ticks_diff(self.clock.ticks_ms(), quiet) >= 5:
                return
            self.clock.sleep_ms(1)
        raise MotorBusError("RS485 did not become quiet before transaction")

    def read_pulses(self, motor_id):
        """Read a fresh signed encoder count, or raise; never return a default."""
        _integer(motor_id, 1, 255, "motor_id")
        self._enter()
        try:
            self._drain()
            self._write(_packet([0xC5, motor_id, 0x2A]))
            start = self.clock.ticks_ms()
            buffer = bytearray()
            received = 0
            while self.clock.ticks_diff(self.clock.ticks_ms(), start) < self.timeout_ms:
                count = self.uart.any()
                if count:
                    data = self.uart.read(min(count, 128))
                    if not data:
                        raise MotorBusError("UART reports data but cannot read it")
                    received += len(data)
                    if received > 2048:
                        raise MotorBusError("Excessive RS485 input")
                    buffer.extend(data)
                    # Ignore noise only before a candidate frame. A malformed
                    # candidate must fail rather than supplying a guessed count.
                    while buffer and buffer[0] != 0xC5:
                        del buffer[0]
                    if len(buffer) >= 10:
                        frame = buffer[:10]
                        if (frame[1] != motor_id or frame[2] != 0x2A or frame[3] != 1
                                or frame[9] != 0x5C
                                or frame[8] != (sum(frame[:8]) & 0xFF)):
                            raise MotorBusError("Invalid position response (ID/opcode/format/checksum/trailer)")
                        if len(buffer) != 10:
                            raise MotorBusError("Unexpected extra response data")
                        # First payload byte is command status; only 1 is success
                        # (PDxxS1 manual section 4.2.1, also observed on hardware).
                        return struct.unpack(">i", bytes(frame[4:8]))[0]
                self.clock.sleep_ms(1)
            raise MotorTimeout("No complete position response from ID %d" % motor_id)
        finally:
            self._busy = False

    def scan(self, start_id=1, end_id=255):
        """Read-only unicast scan. A missing reply is not a zero position.

        Only absent replies are skipped; corrupt/ambiguous traffic aborts.
        Distinct responding IDs do not prove the physical identity of a screw.
        """
        _integer(start_id, 1, 255, "start_id")
        _integer(end_id, start_id, 255, "end_id")
        found = []
        for motor_id in range(start_id, end_id + 1):
            try:
                self.read_pulses(motor_id)
            except MotorTimeout:
                continue
            found.append(motor_id)
        return found

    def _move_pulses(self, motor_id, signed_pulses):
        """Private raw finite move; caller must first enforce physical limits.

        Positive counts use direction 0, negative counts direction 1. This is
        motor rotation, NOT a universal physical forward/backward direction.
        Only one <=12800-pulse segment is sent, at 6 rpm; there is no retry.
        """
        _integer(motor_id, 1, 255, "motor_id")
        _integer(signed_pulses, -self.MAX_PULSES, self.MAX_PULSES, "signed_pulses")
        if signed_pulses == 0:
            raise ValueError("signed_pulses must not be zero")
        pulses = abs(signed_pulses)
        payload = [0xC5, motor_id, 0xF3, 0 if signed_pulses > 0 else 1,
                   self.ACCELERATION, 0, self.SPEED_RPM,
                   (pulses >> 24) & 255, (pulses >> 16) & 255,
                   (pulses >> 8) & 255, pulses & 255]
        self._enter()
        try:
            self._drain()
            self._write(_packet(payload))
        finally:
            self._busy = False
