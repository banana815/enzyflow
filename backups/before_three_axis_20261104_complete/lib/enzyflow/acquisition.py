"""AS7341 hardware access, separate from transport and detection."""
from machine import I2C, Pin
from as7341 import AS7341
from as7341_led import AS7341LED
from enzyflow import config


class AS7341Source:
    def __init__(self):
        self.bus = I2C(config.I2C_ID, scl=Pin(config.SCL), sda=Pin(config.SDA), freq=config.I2C_HZ)
        self.sensor = AS7341(self.bus)
        if not self.sensor.isconnected():
            raise OSError('AS7341 not detected at 0x39')
        self.led = AS7341LED(self.bus)
        self.led.off()
        self.sensor.set_atime(config.ATIME)
        self.sensor.set_astep(config.ASTEP)
        self.sensor.set_again_factor(config.GAIN)

    def read(self):
        self.sensor.start_measure('F1F4CN')
        a = self.sensor.get_spectral_data()
        self.sensor.start_measure('F5F8CN')
        b = self.sensor.get_spectral_data()
        if min(a + b) < 0:
            raise OSError('AS7341 read error')
        return {'channels': a[:4] + b[:4], 'clear': (a[4] + b[4]) // 2,
                'nir': (a[5] + b[5]) // 2, 'saturated': max(a + b) >= 65535}

    def close(self):
        try:
            self.led.off()
        finally:
            self.sensor.disable()
