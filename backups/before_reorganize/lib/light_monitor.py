"""AS7341 occlusion detection and approximate, uncalibrated RGB display.
Keep the sensor uncovered during startup calibration.
Output is serial text, not a GPIO electrical signal.
"""
from machine import I2C, Pin
from time import sleep_ms
from as7341 import AS7341
from as7341_led import AS7341LED


def estimate_rgb(channels):
    # Band-group proxy, not a calibrated CIE/sRGB conversion.
    f1, f2, f3, f4, f5, f6, f7, f8 = channels
    r = (f6 + f7 + f8) / 3
    g = (f4 + f5) / 2
    b = (f1 + f2 + f3) / 3
    peak = max(r, g, b)
    if peak <= 0:
        return (0, 0, 0)
    return tuple(int(255 * (v / peak) ** (1 / 2.2) + 0.5) for v in (r, g, b))


class BlockDetector:
    def __init__(self, baseline):
        self.low = max(2, baseline * 0.20)
        self.high = max(self.low + 2, baseline * 0.35)
        self.blocked = False
        self.votes = 0

    def update(self, clear):
        changed = clear >= self.high if self.blocked else clear <= self.low
        self.votes = self.votes + 1 if changed else 0
        if self.votes >= 3:
            self.blocked = not self.blocked
            self.votes = 0
        return self.blocked


def run(samples=None):
    i2c = I2C(1, scl=Pin(4), sda=Pin(2), freq=100000)
    sensor = AS7341(i2c)
    if not sensor.isconnected():
        raise OSError('AS7341 not detected at 0x39')
    led = AS7341LED(i2c)
    try:
        led.off()
        sensor.set_atime(29)
        sensor.set_astep(599)
        sensor.set_again_factor(8)
        print('CALIBRATING: keep sensor uncovered for 3 seconds')
        values = []
        for _ in range(10):
            sensor.start_measure('F1F4CN')
            values.append(sensor.get_spectral_data()[4])
            sleep_ms(200)
        baseline = sorted(values)[len(values)//2]
        if baseline < 10:
            raise ValueError('Calibration too dark: uncover sensor, add ambient light, restart')
        detector = BlockDetector(baseline)
        print('READY baseline={} blocked_below={} light_above={} LED=OFF RGB=APPROX'.format(
            baseline, detector.low, detector.high))
        count = 0
        while samples is None or count < samples:
            sensor.start_measure('F1F4CN')
            first = sensor.get_spectral_data()
            sensor.start_measure('F5F8CN')
            second = sensor.get_spectral_data()
            clear = (first[4] + second[4]) // 2
            channels = first[:4] + second[:4]
            blocked = detector.update(clear)
            if blocked:
                print('BLOCKED signal=1 clear={}'.format(clear))
            elif max(first + second) >= 65535:
                print('SATURATED signal=0 clear={} reduce_light_or_gain'.format(clear))
            else:
                print('LIGHT signal=0 RGB_APPROX={} clear={} nir={}'.format(
                    estimate_rgb(channels), clear, (first[5] + second[5]) // 2))
            count += 1
            sleep_ms(200)
    except KeyboardInterrupt:
        print('STOPPED')
    finally:
        try:
            led.off()
        finally:
            sensor.disable()
