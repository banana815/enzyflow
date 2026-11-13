# Read-only version query across all non-broadcast IDs, never move/clear.
import time, binascii
from machine import UART
uart = UART(1, 115200, tx=5, rx=6, timeout=10)
for motor_id in range(1, 256):
    uart.read(256)
    frame = bytes([0xC5, motor_id, 0x20, (0xC5 + motor_id + 0x20) & 255, 0x5C])
    uart.write(frame)
    time.sleep_ms(60)
    data = uart.read(200) or b''
    if data: print('ID', motor_id, 'version_response_hex', binascii.hexlify(data).decode())
print('SCAN_FINISHED_1_255')
