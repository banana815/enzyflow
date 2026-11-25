"""Test the macOS exposure transport without opening USB hardware."""
from array import array
import sys
from types import SimpleNamespace
import unittest


@unittest.skipUnless(sys.platform == 'darwin', 'macOS USB backend')
class MacExposureTests(unittest.TestCase):
    def setUp(self):
        from webcontrol import macos_uvc
        self.uvc = macos_uvc
        self.backend = FakeBackend()

    def test_only_matching_location_is_opened_and_manual_exposure_is_read_back(self):
        control = self.uvc.ExposureControl('0x11400000c686464', self.backend)
        try:
            self.assertEqual(self.backend.opened, ['selected'])
            self.assertEqual(control.controls()['auto_exposure']['value'], 3)
            control.set_exposure(250)
            self.assertEqual(control.controls(), {
                'auto_exposure': {'value': 1},
                'exposure_time_absolute': {'min': 1, 'max': 5000, 'step': 1, 'default': 157, 'value': 250},
            })
            self.assertEqual(self.backend.writes, [(2, b'\x01'), (4, b'\xfa\x00\x00\x00')])
            with self.assertRaises(ValueError):
                control.set_exposure(5001)
        finally:
            control.close()
        self.assertEqual(self.backend.closed, ['selected'])

    def test_rejects_usb_write_that_does_not_take_effect(self):
        control = self.uvc.ExposureControl('0x11400000c686464', self.backend)
        self.addCleanup(control.close)
        self.backend.ignore_writes = True
        with self.assertRaisesRegex(RuntimeError, '读回'):
            control.set_exposure(250)

    def test_does_not_open_a_same_model_at_a_different_location(self):
        with self.assertRaisesRegex(RuntimeError, '重新扫描'):
            self.uvc.ExposureControl('0x11600000c686464', self.backend)
        self.assertEqual(self.backend.opened, [])


class FakeBackend:
    def __init__(self):
        self.opened, self.closed, self.writes = [], [], []
        self.mode, self.exposure = 8, 157
        self.ignore_writes = False

    def enumerate_devices(self):
        return ['other', 'selected']

    def get_device_descriptor(self, device):
        return SimpleNamespace(idVendor=0x0c68, idProduct=0x6464, bus=1,
                               port_numbers=(1, 4 if device == 'selected' else 5))

    def get_configuration_descriptor(self, device, index):
        return SimpleNamespace(bNumInterfaces=1)

    def get_interface_descriptor(self, device, intf, alt, config):
        return SimpleNamespace(bInterfaceClass=14, bInterfaceSubClass=1, bInterfaceNumber=0,
                               extra_descriptors=bytes.fromhex('1224020101020000000000000000031e0000'))

    def open_device(self, device):
        self.opened.append(device)
        return device

    def close_device(self, handle):
        self.closed.append(handle)

    def ctrl_transfer(self, handle, direction, request, selector, index, data, timeout):
        assert handle == 'selected' and index == 0x100
        selector >>= 8
        if direction == 0xa1:
            value = {(2, 0x84): 9, (2, 0x81): self.mode, (4, 0x81): self.exposure,
                     (4, 0x82): 1, (4, 0x83): 5000, (4, 0x84): 1, (4, 0x87): 157}[selector, request]
            data[:] = array('B', value.to_bytes(len(data), 'little'))
        else:
            assert direction == 0x21 and request == 0x01
            self.writes.append((selector, data.tobytes()))
            if not self.ignore_writes:
                if selector == 2:
                    self.mode = int.from_bytes(data, 'little')
                else:
                    self.exposure = int.from_bytes(data, 'little')
        return len(data)
