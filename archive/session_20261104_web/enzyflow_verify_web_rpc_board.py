# MicroPython compatibility test with a fake bus only; no UART/motor access.
import sys
for name in ('enzyflow.motor_web','enzyflow.motor_safety','enzyflow.motor_bus'):
    if name in sys.modules:
        del sys.modules[name]
from enzyflow.motor_web import MotorRPC
from enzyflow.motor_bus import MotorBus
class WebFakeClock:
    def __init__(self): self.now=0
    def ticks_ms(self): return self.now
    def ticks_diff(self,a,b): return a-b
    def sleep_ms(self,value): self.now+=value
class WebFakeBus:
    def __init__(self): self.counts={1:1000,2:2000,3:3000};self.moves=[]
    def read_pulses(self,i): return self.counts[i]
    def _move_pulses(self,i,p,rpm=6): self.moves.append((i,p,rpm));self.counts[i]+=p
web_fake_bus=WebFakeBus()
web_fake_rpc=MotorRPC(web_fake_bus,WebFakeClock())
assert len(web_fake_rpc.session)==32
web_test_seq=0
def web_test_request(op,args):
    global web_test_seq
    web_test_seq+=1
    return {'id':'board-test-%d'%web_test_seq,'seq':web_test_seq,'session':web_fake_rpc.session,'op':op,'args':args}
assert web_fake_rpc.handle(web_test_request('configure',{'ids':[1,2,3],'long_id':1,'confirmed':True}))['ok']
assert web_fake_rpc.handle(web_test_request('anchor',{'axis':1,'mm':101,'sign':-1,'lead':4,'confirmed':True}))['ok']
web_test_move=web_test_request('jog',{'axis':1,'delta_mm':.1,'rpm':12})
web_test_result=web_fake_rpc.handle(web_test_move)
assert web_test_result['ok'],web_test_result
assert web_fake_rpc.handle(web_test_move)==web_test_result
assert web_fake_bus.moves==[(1,-1280,12)]
web_fake_bus.counts[2]+=1000
assert not web_fake_rpc.handle(web_test_request('status',{}))['ok']
assert all(a.reference is None for a in web_fake_rpc.controller.axes.values())
assert (MotorBus.MIN_RPM,MotorBus.MAX_RPM)==(3,30)
print('MICROPYTHON_WEB_RPC_FAKE_BUS_OK: nonce, configure, anchor, 12rpm, duplicate, idle-drift lock')
