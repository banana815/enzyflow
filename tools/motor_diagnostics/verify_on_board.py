# On-device import/logic verification with a FAKE bus: no UART instantiated.
import sys
for name in ('enzyflow.motor_console', 'enzyflow.motor_safety', 'enzyflow.motor_bus'):
    if name in sys.modules: del sys.modules[name]
from enzyflow.motor_bus import MotorBus
from enzyflow.motor_safety import ThreeAxes, MARGIN_MM
from enzyflow import motor_console
class FakeBus:
    def read_pulses(self, ident): return 123456
    def _move_pulses(self, ident, pulses): raise AssertionError('Unexpected motion')
class Clock:
    def sleep_ms(self, ms): pass
controller = ThreeAxes(FakeBus(), Clock())
controller.configure([1,2,3], 1, confirmed=True)
assert MARGIN_MM == 10.0
assert controller.axes[1].maximum == 320
assert controller.axes[2].maximum == 220
assert controller.axes[3].maximum == 220
for ident in (1,2,3):
    try: controller.jog(ident, 1)
    except ValueError: pass
    else: raise AssertionError('Unlocked before anchor')
controller.anchor(2, 210, -1, 4, confirmed=True)
try: controller.jog(2,1)
except ValueError: pass
else: raise AssertionError('Guard margin ignored')
assert all(a.reference is None for a in controller.axes.values())
controller.anchor(3, 101, -1, 2, confirmed=True)
assert controller.axes[3].max_jog_mm == 0.5
assert controller.axes[3].position(123456 + 25600) == 100
try: controller.jog(3, 1)
except ValueError: pass
else: raise AssertionError('2mm-lead jog cap ignored')
controller.lock()
assert controller.axes[3].lead_mm is None
for ident, upper in ((1,310), (2,210), (3,210)):
    assert controller.axes[ident].check_target(30) == 30
    assert controller.axes[ident].check_target(upper) == upper
    for bad in (29.9, upper + 0.1):
        try: controller.axes[ident].check_target(bad)
        except ValueError: pass
        else: raise AssertionError('1cm margin ignored')
print('ON_BOARD_1CM_MARGIN_GUARDS_OK_NO_UART_NO_MOTION')
