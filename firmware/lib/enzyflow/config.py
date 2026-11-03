# Board settings; USB spectrometer is acquired separately by the computer.
I2C_ID = 1
SDA = 2
SCL = 4
I2C_HZ = 100000
ATIME = 29
ASTEP = 599
GAIN = 8
CALIBRATION_SAMPLES = 10
MIN_BASELINE = 10
BLOCK_RATIO = 0.20
RESTORE_RATIO = 0.35
CONFIRM_SAMPLES = 3
INTERVAL_MS = 200
BOOT_DELAY_MS = 3000
# For repeatable startup while covered, set a known uncovered Clear baseline.
# None means automatic calibration: leave sensor uncovered at startup.
BASELINE = None
