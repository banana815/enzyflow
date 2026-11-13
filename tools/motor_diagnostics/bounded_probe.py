# No movement or parameter writes. Bounded capture distinguishes idle chatter.
import time, binascii, struct
from machine import UART
uart = UART(1, 115200, tx=5, rx=6, timeout=2, rxbuf=2048)
def capture(label, duration=250):
    start = time.ticks_ms()
    sample = bytearray()
    total = 0
    while time.ticks_diff(time.ticks_ms(), start) < duration:
        if uart.any():
            chunk = uart.read(min(uart.any(), 128)) or b''
            total += len(chunk)
            if len(sample) < 200: sample.extend(chunk[:200-len(sample)])
        time.sleep_ms(1)
    print(label, 'bytes', total, 'first200', binascii.hexlify(sample).decode())
    valid = []
    for i in range(len(sample)-9):
        f = sample[i:i+10]
        if f[0] == 0xC5 and f[2] == 0x2A and f[9] == 0x5C and f[8] == sum(f[:8]) & 255:
            valid.append((f[1], f[3], struct.unpack('>i',bytes(f[4:8]))[0]))
    print('valid_position_frames', valid)
capture('IDLE_NO_QUERY')
for ident in (1,2,3):
    for opcode in (0x20,0x2A):
        uart.write(bytes([0xC5,ident,opcode,(0xC5+ident+opcode)&255,0x5C]))
        capture('QUERY_%d_%02x' % (ident,opcode))
print('READ_ONLY_DONE')
