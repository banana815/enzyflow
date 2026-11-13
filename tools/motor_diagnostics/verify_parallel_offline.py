"""Run on MicroPython with an in-memory bus only; never opens motor UART."""
from enzyflow.motor_web import MotorRPC
class Clock:
    now=0
    def ticks_ms(self): return self.now
    def ticks_diff(self,a,b): return a-b
    def sleep_ms(self,ms): self.now+=ms
class Bus:
    def __init__(self): self.counts={1:0,2:0,3:0}; self.moves=[]
    def read_pulses(self,i): return self.counts[i]
    def _move_pulses(self,i,p,rpm=6): self.moves.append((i,p,rpm)); self.counts[i]+=p
bus=Bus(); rpc=MotorRPC(bus, Clock(), session='offline-check'); seq=0
def call(op,args):
    global seq
    seq+=1
    response=rpc.handle({'id':str(seq),'seq':seq,'session':rpc.session,'op':op,'args':args})
    assert response['ok'],response
    return response
call('configure',{'ids':[1,2,3],'long_id':1,'confirmed':True})
for i in (1,2,3): call('anchor',{'axis':i,'mm':100,'sign':-1,'lead':4,'confirmed':True})
r=call('jog_group',{'moves':[{'axis':i,'delta_mm':.5,'rpm':12} for i in (1,2,3)]})
assert len(bus.moves)==3
assert all(a['position_mm']==100.5 for a in r['result']['axes'])
call('anchor',{'axis':2,'mm':20,'sign':-1,'lead':4,'confirmed':True})
r=call('recover',{'axis':2,'delta_mm':.5,'rpm':6,'confirmed':True})
assert r['result']['axes'][1]['position_mm']==20.5
call('status',{})
call('lock',{})
print('BOARD_FAKE_BUS_PASS: group + recovery, no physical UART')
