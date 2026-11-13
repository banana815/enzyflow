"""RS485 screw calibration, confirmed by user 2026-11-03.

Coordinates increase away from the left mechanical zero. This module does
not move the motor. Reference is invalid after driver reset/power loss,
angle clearing, or manual screw movement; re-anchor before motion.
Software bounds apply to checked_delta_degrees only, not raw motor_ctrl calls.
"""
import math

UART_ID = 1
BAUDRATE = 115200
TX = 5
RX = 6
MOTOR_ID = 1
LEAD_MM_PER_REV = 4.0
PULSES_PER_REV = 51200
MIN_POSITION_MM = 20.0
MAX_POSITION_MM = 220.0
REFERENCE_POSITION_MM = 100.0
REFERENCE_ENCODER_PULSES = -1277137
# Positive motor rotation retreats toward the left zero.
POSITION_SIGN = -1


def _in_range(position_mm):
    if not math.isfinite(position_mm) or not MIN_POSITION_MM <= position_mm <= MAX_POSITION_MM:
        raise ValueError("Position must be within 20..220 mm")
    return position_mm


def position_mm(encoder_pulses, reference_pulses):
    """Pass a reference from the current uninterrupted driver session."""
    return REFERENCE_POSITION_MM + POSITION_SIGN * (encoder_pulses - reference_pulses) * LEAD_MM_PER_REV / PULSES_PER_REV


def checked_delta_degrees(current_mm, target_mm):
    """Validate measured current and requested target; return relative angle."""
    _in_range(current_mm)
    _in_range(target_mm)
    return POSITION_SIGN * (target_mm - current_mm) * 360.0 / LEAD_MM_PER_REV
