# Requires calibration_init.py globals in SAME uninterrupted ESP32 REPL session.
# Each successful call adds precisely one commanded revolution in four <=1mm
# segments. Refuse after five total turns, and never resume automatically.
if cal_steps % 4 or not 0 <= cal_steps < 20:
    raise ValueError('Batch incomplete or all5 turns already done; do not retry')
try:
    for _ in range(4):
        cal.jog(CAL_ID, 1.0)
        cal_steps += 1
    after = {i: cal_bus.read_pulses(i) for i in (1,2,3)}
    for i in (1,2,3):
        if i != CAL_ID and abs(after[i] - cal_before[i]) > 640:
            raise ValueError('Unexpected movement on unselected motor')
    print('COMMANDED_TURNS_DONE', cal_steps / 4)
    print('ENCODER_TURNS', CAL_SIGN * (after[CAL_ID]-cal_before[CAL_ID]) / 51200)
    print('ENCODERS', after)
except BaseException:
    cal.lock()
    raise
if cal_steps == 20:
    cal.lock()
    print('FIVE_TURNS_FINISHED_ALL_LOCKED_MEASURE_ACTUAL_TRAVEL_MM')
