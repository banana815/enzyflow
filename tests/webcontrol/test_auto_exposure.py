import unittest
from threading import Event

import numpy as np

from webcontrol.exposure_adjustment import adjust_exposure


class Camera:
    def __init__(self, peak_levels, initial=10):
        self.levels = peak_levels
        self.value = self.old_value = initial
        self.frames = 0

    def exposure_options(self):
        return [{'value': value} for value in self.levels]

    def set_exposure(self, value):
        self.old_value, self.value = self.value, value
        self.frames = 0

    def get_exposure(self):
        return self.value

    def read(self):
        self.frames += 1
        sampled_value = self.old_value if self.frames <= 3 else self.value
        level = self.levels[sampled_value]
        frame = np.full((24, 40, 3), 3, dtype=np.uint8)
        frame[3, 4, 1] = level
        frame[-1, -1, :] = 255
        return frame


class Region:
    def crop(self, frame):
        return frame[:-1, :-1]


class AutoExposureTests(unittest.TestCase):
    def test_narrow_peak_and_exact_threshold(self):
        camera = Camera({5:160, 10:240, 20:250, 39:255}, initial=39)
        self.assertEqual(adjust_exposure(camera, Region(), 5, 39, Event()), (10, 240))
        self.assertEqual(camera.value, 10)

    def test_configurable_threshold_and_digital_clipping(self):
        camera = Camera({5:190, 10:220, 20:254, 39:255})
        self.assertEqual(adjust_exposure(camera, Region(), 5, 39, Event(), threshold=205), (5, 190))
        self.assertEqual(adjust_exposure(camera, Region(), 5, 39, Event(), threshold=255), (20, 254))

    def test_saturation_or_below_noise_restores_prior_exposure(self):
        for levels in ({5:255,10:255,20:255}, {5:2,10:4,20:8}):
            camera=Camera(levels)
            with self.assertRaises(ValueError):
                adjust_exposure(camera, Region(), 5, 20, Event())
            self.assertEqual(camera.value, 10)

    def test_fluctuating_peaks_do_not_lock(self):
        class FlickeringCamera(Camera):
            def read(self):
                frame = super().read()
                if self.frames == 5:
                    frame[3, 4, 1] //= 2
                return frame

        camera = FlickeringCamera({5:120, 10:240, 20:255}, initial=5)
        with self.assertRaisesRegex(ValueError, '光强波动'):
            adjust_exposure(camera, Region(), 5, 20, Event())
        self.assertEqual(camera.value, 5)
