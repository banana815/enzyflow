"""One intentionally small direction test after physical mapping/position confirmation.

Inert until selected ID and measured position are filled for the current session.
Never use an old saved position after power-off/manual movement.
"""
from machine import UART
from enzyflow.motor_bus import MotorBus
from enzyflow.motor_safety import ThreeAxes
AXIS_ID = None
MEASURED_MM = None
if AXIS_ID not in (1,2,3) or MEASURED_MM is None:
    raise ValueError('Fill confirmed ID and freshly measured physical position first')
bus = MotorBus(UART(1, 115200, tx=5, rx=6, timeout=10))
controller = ThreeAxes(bus)
controller.configure([1,2,3], 1, confirmed=True)
before = {i: bus.read_pulses(i) for i in (1,2,3)}
print('BEFORE', before)
print('ONE_PROBE_ID', AXIS_ID, 'positive_rotation_equivalent_mm', 0.5)
travel = controller.probe_direction(AXIS_ID, MEASURED_MM, 4, confirmed=True)
after = {i: bus.read_pulses(i) for i in (1,2,3)}
print('AFTER', after)
for i in (1,2,3):
    if i != AXIS_ID and abs(after[i]-before[i]) > 640:
        raise ValueError('Unexpected movement on nonselected motor; inspect power/wiring')
print('SELECTED_ENCODER_TRAVEL_MM', travel)
print('ALL_LOCKED', all(a.reference is None for a in controller.axes.values()))
print('OBSERVE_PHYSICAL_AXIS_AND_DIRECTION_BEFORE_ANY_NEXT_MOVE')
