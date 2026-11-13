"""Return ONE known probe after user confirms the physical axis and direction.

Only for this uninterrupted drive session; no automatic return on error.
"""
from machine import UART
from enzyflow.motor_bus import MotorBus
from enzyflow.motor_safety import ThreeAxes
AXIS_ID = None
ORIGINAL_MM = None
ORIGINAL_PULSES = None
AFTER_PROBE_PULSES = None
FORWARD_SIGN = None
if AXIS_ID not in (1,2,3) or FORWARD_SIGN not in (-1,1) or ORIGINAL_MM is None:
    raise ValueError('Fill current-session observations, never historical defaults')
bus = MotorBus(UART(1, 115200, tx=5, rx=6, timeout=10))
controller = ThreeAxes(bus)
controller.configure([1,2,3], 1, confirmed=True)
before = {i: bus.read_pulses(i) for i in (1,2,3)}
current = before[AXIS_ID]
if abs(current - AFTER_PROBE_PULSES) > 640:
    raise ValueError('Position changed after probe; must remeasure')
actual_mm = ORIGINAL_MM + FORWARD_SIGN * (current - ORIGINAL_PULSES) / 12800
controller.anchor(AXIS_ID, actual_mm, FORWARD_SIGN, 4, confirmed=True)
print('RETURN_ID', AXIS_ID, 'FROM_MM', actual_mm, 'TO_MM', ORIGINAL_MM)
try:
    print('FEEDBACK_POSITION_MM', controller.jog(AXIS_ID, ORIGINAL_MM - actual_mm))
finally:
    controller.lock()
after = {i: bus.read_pulses(i) for i in (1,2,3)}
print('BEFORE', before)
print('AFTER', after)
for i in (1,2,3):
    if i != AXIS_ID and abs(after[i]-before[i]) > 640:
        raise ValueError('Unexpected movement on other motor')
print('ALL_LOCKED', all(a.reference is None for a in controller.axes.values()))
