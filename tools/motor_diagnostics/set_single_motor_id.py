"""ONE isolated motor only. Edit TARGET_ID before sending through board_tool.

Never run on a shared bus: duplicate source IDs cannot be detected reliably.
Uses vendor opcodes0x60 then0x04 once each; no move/reset/clear.
Save reference: https://blog.itpub.net/70040860/viewspace-3175721/ section4.2.4
"""
from machine import UART
import time, binascii
from enzyflow.motor_bus import MotorBus, MotorTimeout
SOURCE_ID = 1
TARGET_ID = None  # Deliberately inert until an isolated motor is confirmed.
if type(TARGET_ID) is not int or TARGET_ID not in (2, 3):
    raise ValueError('Set target2/3 only after confirming ONE isolated short motor')
uart = UART(1, 115200, tx=5, rx=6, timeout=2)
bus = MotorBus(uart)
print('SOURCE_POSITION', bus.read_pulses(SOURCE_ID))
try:
    bus.read_pulses(TARGET_ID)
except MotorTimeout:
    pass
else:
    raise ValueError('Target ID already responds; do not write')
payload = [0xC5, SOURCE_ID, 0x60, TARGET_ID]
packet = bytes(payload + [sum(payload) & 255, 0x5C])
if uart.write(packet) != len(packet):
    raise OSError('Ambiguous ID write; do not repeat blindly')
time.sleep_ms(100)
reply = uart.read(128) or b''
print('ID_WRITE_REPLY', binascii.hexlify(reply).decode())
expected = [0xC5, SOURCE_ID, 0x60, 1, TARGET_ID]
expected = bytes(expected + [sum(expected) & 255, 0x5C])
if reply != expected:
    raise OSError('ID write reply invalid; inspect, do not repeat blindly')
print('NEW_ID', TARGET_ID, 'POSITION', bus.read_pulses(TARGET_ID))
try:
    bus.read_pulses(SOURCE_ID)
except MotorTimeout:
    print('OLD_ID_NO_LONGER_RESPONDS')
else:
    raise ValueError('Old ID still responds: isolate and inspect wiring')
# Persist the new address:0x60 alone changes only current runtime settings.
time.sleep_ms(20)
payload = [0xC5, TARGET_ID, 0x04]
packet = bytes(payload + [sum(payload) & 255, 0x5C])
if uart.write(packet) != len(packet):
    raise OSError('Ambiguous save write; inspect before retry')
time.sleep_ms(200)
reply = uart.read(128) or b''
print('SAVE_REPLY', binascii.hexlify(reply).decode())
expected = [0xC5, TARGET_ID, 0x04, 1]
expected = bytes(expected + [sum(expected) & 255, 0x5C])
if reply != expected:
    raise OSError('Save success not confirmed; do not assume persisted')
print('SAVED_NEW_ID', TARGET_ID, 'POSITION', bus.read_pulses(TARGET_ID))
print('SAVE_ACK_OK_POWER_CYCLE_VERIFICATION_REQUIRED_NO_MOTION')
