# Read-only RS485 diagnostics: NEVER sends motion, clear, enable or ID changes.
import sys, os, time, binascii
from machine import UART
print(sys.implementation)
print('ROOT', os.listdir('/'))
uart = UART(1, 115200, tx=5, rx=6, timeout=64)
for motor_id in range(1, 11):
    for _ in range(20):
        if uart.any(): uart.read(min(uart.any(), 128))
        time.sleep_ms(1)
    if uart.any():
        raise OSError("Bus not quiet; isolate motors before scanning")
    frame = bytes([0xC5, motor_id, 0x2A, (0xC5 + motor_id + 0x2A) & 255, 0x5C])
    uart.write(frame)
    time.sleep_ms(150)
    data = uart.read(200) or b''
    print('ID', motor_id, 'angle_response_hex', binascii.hexlify(data).decode())
