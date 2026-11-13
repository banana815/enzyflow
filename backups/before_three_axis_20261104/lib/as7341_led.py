"""AS7341 module LED control. SDA=GPIO2, SCL=GPIO4 by default.

Usage:
    from as7341_led import AS7341LED
    led = AS7341LED()
    led.on(4)       # 4..20 mA, even integers only
    led.off()
An existing machine.I2C instance may be passed as i2c.
Use sequentially with spectral acquisition, not from concurrent tasks.
"""
from machine import I2C, Pin


class AS7341LED:
    def __init__(self, i2c=None, address=0x39):
        self.i2c = i2c if i2c is not None else I2C(
            1, scl=Pin(4), sda=Pin(2), freq=100000)
        self.address = address
        cfg = self._read(0xA9)
        try:
            self._write(0xA9, cfg & ~0x10)
            if self._read(0x92) & 0xFC != 0x24:
                raise OSError("AS7341 chip ID mismatch")
        finally:
            self._write(0xA9, cfg)

    def _read(self, register):
        return self.i2c.readfrom_mem(self.address, register, 1)[0]

    def _write(self, register, value):
        self.i2c.writeto_mem(self.address, register, bytes((value,)))

    def set_current(self, current_ma):
        """Set 0 (off), or an even integer from 4 through 20 mA."""
        if type(current_ma) is not int or (
            current_ma != 0 and (current_ma < 4 or current_ma > 20 or current_ma % 2)
        ):
            raise ValueError("Use 0, or an even integer from 4 to 20 mA")
        cfg = self._read(0xA9)
        try:
            self._write(0xA9, cfg & ~0x10)
            if current_ma:
                self._write(0x80, self._read(0x80) | 0x01)
            self._write(0xA9, cfg | 0x10)
            control = self._read(0x70)
            self._write(0x70, (control | 0x08) if current_ma else (control & ~0x08))
            value = (0x80 | ((current_ma - 4) // 2)) if current_ma else 0
            self._write(0x74, value)
            if self._read(0x74) != value:
                raise OSError("LED register verification failed")
        finally:
            self._write(0xA9, cfg)

    def on(self, current_ma=4):
        """Turn on; default 4 mA. Use off() to turn off."""
        if current_ma == 0:
            raise ValueError("Use off() to turn off")
        self.set_current(current_ma)

    def off(self):
        """Turn off the LED without disabling spectral acquisition."""
        self.set_current(0)

    def get_current(self):
        """Read the configured LED current (not an electrical measurement)."""
        cfg = self._read(0xA9)
        try:
            self._write(0xA9, cfg | 0x10)
            control, value = self._read(0x70), self._read(0x74)
            return 4 + 2 * (value & 0x7F) if control & 8 and value & 0x80 else 0
        finally:
            self._write(0xA9, cfg)
