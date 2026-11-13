# Inert template: explicitly fill current-session ID, position and observed sign.
from machine import UART
from enzyflow.motor_bus import MotorBus
from enzyflow.motor_safety import ThreeAxes
CAL_ID = None
CAL_START_MM = None
CAL_SIGN = None
CAL_DIRECTION_UNKNOWN = False
if CAL_ID not in (1,2,3) or CAL_SIGN not in (-1,1) or CAL_START_MM is None:
    raise ValueError('Current physical observations required')
if 'cal' in globals():
    cal.lock()
cal_bus = MotorBus(UART(1, 115200, tx=5, rx=6, timeout=10))
cal = ThreeAxes(cal_bus)
cal.configure([1,2,3], 1, confirmed=True)
# For this supervised calibration only: documented4mm lead, positions well
# inside travel. Unknown direction tests use a provisional encoder coordinate,
# and must have room for the ENTIRE5-turn travel in BOTH physical directions,
# plus5mm for approximate ruler readings. Never use near an endpoint.
if CAL_DIRECTION_UNKNOWN:
    if CAL_SIGN != 1:
        raise ValueError('Unknown direction test uses positive rotation only')
    cal._axis(CAL_ID).check_target(CAL_START_MM - 25.0)
    cal._axis(CAL_ID).check_target(CAL_START_MM + 25.0)
cal._axis(CAL_ID).check_target(CAL_START_MM)
cal._axis(CAL_ID).check_target(CAL_START_MM + 20.0)
cal.anchor(CAL_ID, CAL_START_MM, CAL_SIGN, 4, confirmed=True)
cal_before = {i: cal_bus.read_pulses(i) for i in (1,2,3)}
cal_steps = 0
print('CALIBRATION_READY_NO_MOTION', CAL_ID, cal_before)
