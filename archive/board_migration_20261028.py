# Execute on the ESP32 via board_tool.py exec.
import os, sys
for name in ('tcs34725.py', 'uservo.py'):
    if name in os.listdir('/'):
        if name in os.listdir('/lib'):
            raise OSError('Destination already exists: '+name)
        os.rename('/'+name, '/lib/'+name)
os.sync()
for name in list(sys.modules):
    if name.startswith('enzyflow') or name in ('as7341','as7341_smux_select','as7341_led','light_monitor'):
        del sys.modules[name]
from enzyflow.app import run
run(samples=3)
from as7341_led import AS7341LED
led = AS7341LED()
try:
    for current in (4, 20, 0):
        led.set_current(current)
        assert led.get_current() == current
finally:
    led.off()
print('DEPLOYMENT_TEST_OK')
print('ROOT',os.listdir('/'))
print('LIB',os.listdir('/lib'))
