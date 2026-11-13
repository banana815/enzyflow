"""Board-side three-axis emergency stop check using Python counters only."""
from enzyflow.motor_web import MotorRPC
class Clock:
    now=0
    def ticks_ms(self):return self.now
    def ticks_diff(self,a,b):return a-b
    def sleep_ms(self,ms):self.now+=ms
class Bus:
    def __init__(self):self.stops=[];self.clears=[]
    def _brake(self,i):self.stops.append(i)
    def _clear_brake(self,i):self.clears.append(i)
    def _brake_all(self):
        for i in (1,2,3):self._brake(i)
    def _disable_all(self):pass
    def _disable(self,i):pass
    def read_pulses(self,i):return 0
b=Bus();r=MotorRPC(b,Clock(),session='estop-offline')
result=r.emergency_stop()
assert result['confirmed'] and b.stops==[1,2,3] and b.clears==[]
print('BOARD_EMERGENCY_FAKE_PASS: broadcast brake, disable, verify standstill, no clear, no real UART')
