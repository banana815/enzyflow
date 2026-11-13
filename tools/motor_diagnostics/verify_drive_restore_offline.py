"""ESP32-side fake driver recovery test; never constructs UART."""
from enzyflow.motor_web import MotorRPC
class Clock:
    now=0
    def ticks_ms(self):return self.now
    def ticks_diff(self,a,b):return a-b
    def sleep_ms(self,ms):self.now+=ms
class Bus:
    def __init__(self):
        self.counts={1:0,2:0,3:0};self.disabled={1:1,2:1,3:1};self.enables=[];self.resume=False
    def read_pulses(self,i):return self.counts[i]
    def read_driver_flag(self,i,op):return self.disabled[i] if op==0x2F else 0
    def _velocity(self,*a):raise AssertionError('No velocity command allowed')
    def _brake_all(self):pass
    def _brake(self,i):pass
    def _clear_brake(self,i):pass
    def _enable(self,i):
        self.disabled[i]=0;self.enables.append(i)
        if self.resume:self.counts[i]+=100
    def _disable_all(self):self.disabled={1:1,2:1,3:1}
    def _disable(self,i):self.disabled[i]=1
clock=Clock();bus=Bus();rpc=MotorRPC(bus,clock,session='restore-offline')
rpc.restore_drives(True)
assert bus.enables==[1,2,3] and len(rpc.driver_states)==3
bus=Bus();bus.resume=True;rpc=MotorRPC(bus,clock,session='restore-failure')
try:rpc.restore_drives(True)
except ValueError:pass
else:raise AssertionError('Motion was not detected')
assert bus.enables==[1] and all(bus.disabled[i]==1 for i in (1,2,3))
print('BOARD_DRIVE_RESTORE_PASS: confirmed recovery, motion detection, disable on failure; fake bus only')
