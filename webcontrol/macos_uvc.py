"""USB-ZH exposure controls, alongside (not instead of) AVFoundation capture."""
from array import array
import re

from libusb_package import get_libusb1_backend


VID, PID = 0x0c68, 0x6464
AE_MODE, EXPOSURE = 0x02, 0x04
GET_CUR, GET_MIN, GET_MAX, GET_RES, GET_DEF = 0x81, 0x82, 0x83, 0x84, 0x87
SET_CUR = 0x01
MANUAL = 0x01


def _location(descriptor):
    # Darwin libusb expresses IOKit's locationID as bus + downstream port path.
    ports = descriptor.port_numbers
    if not ports or len(ports) > 6 or any(not 1 <= p <= 15 for p in ports):
        return None
    return (descriptor.bus << 24) | sum(p << (20 - 4 * i) for i, p in enumerate(ports))


def _terminal(descriptors):
    """Return the camera terminal advertising both exposure controls."""
    terminals = []
    pos = 0
    while pos < len(descriptors):
        size = descriptors[pos]
        if size < 3 or pos + size > len(descriptors):
            raise ValueError('USB-ZH 视频控制描述符无效')
        data = descriptors[pos:pos + size]
        if (size >= 16 and data[1:3] == b'\x24\x02' and data[4:6] == b'\x01\x02'
                and 0 < data[14] <= size - 15 and data[15] & 0x0a == 0x0a):
            terminals.append(data[3])
        pos += size
    if len(terminals) != 1:
        raise ValueError('USB-ZH 未提供唯一的手动曝光控制终端')
    return terminals[0]


class ExposureControl:
    def __init__(self, uid, backend=None):
        # External UVC IDs from AVFoundation encode locationID followed by VID/PID.
        match = re.fullmatch(r'0x([0-9a-f]{1,8})0c686464', uid.lower())
        if not match:
            raise ValueError('无法将 AVFoundation 光谱仪身份绑定到对应的 USB 曝光控制')
        location = int(match[1], 16)
        self.backend = backend if backend is not None else get_libusb1_backend()
        self.handle = None
        if self.backend is None:
            raise RuntimeError('无法加载 macOS 光谱仪的 libusb 后端')
        matches = []
        for device in self.backend.enumerate_devices():
            desc = self.backend.get_device_descriptor(device)
            if (desc.idVendor, desc.idProduct) == (VID, PID) and _location(desc) == location:
                matches.append(device)
        if len(matches) != 1:
            raise RuntimeError('找不到与当前光谱仪匹配的 USB 曝光接口，请重新扫描')
        device = matches[0]
        config = self.backend.get_configuration_descriptor(device, 0)
        interfaces = [self.backend.get_interface_descriptor(device, i, 0, 0)
                      for i in range(config.bNumInterfaces)]
        controls = [i for i in interfaces if (i.bInterfaceClass, i.bInterfaceSubClass) == (14, 1)]
        if len(controls) != 1:
            raise RuntimeError('USB-ZH 未提供唯一的视频控制接口')
        interface = controls[0]
        self.index = (_terminal(bytes(interface.extra_descriptors)) << 8) | interface.bInterfaceNumber
        self.handle = self.backend.open_device(device)
        try:
            self.modes = self._read(AE_MODE, GET_RES)
            minimum = self._read(EXPOSURE, GET_MIN)
            maximum = self._read(EXPOSURE, GET_MAX)
            step = self._read(EXPOSURE, GET_RES)
            default = self._read(EXPOSURE, GET_DEF)
            if not self.modes & MANUAL or not (1 <= minimum <= default <= maximum) or step < 1:
                raise RuntimeError('USB-ZH 未报告有效的手动曝光范围')
            self.limits = dict(min=minimum, max=maximum, step=step, default=default)
        except BaseException:
            self.close()
            raise

    def _transfer(self, direction, request, selector, data):
        # Only endpoint-zero class requests. Claiming an interface would
        # conflict with Apple's UVC video driver; never detach or reset it.
        try:
            count = self.backend.ctrl_transfer(self.handle, direction, request, selector << 8,
                                               self.index, data, 1000)
        except OSError as exc:
            raise RuntimeError(f'USB-ZH 曝光控制失败：{exc}') from exc
        if count != len(data):
            raise RuntimeError('USB-ZH 返回的曝光控制数据长度不完整')

    def _read(self, selector, request=GET_CUR):
        data = array('B', [0] * (1 if selector == AE_MODE else 4))
        self._transfer(0xa1, request, selector, data)
        return int.from_bytes(data, 'little')

    def _write(self, selector, value):
        data = array('B', value.to_bytes(1 if selector == AE_MODE else 4, 'little'))
        self._transfer(0x21, SET_CUR, selector, data)
        if self._read(selector) != value:
            raise RuntimeError('USB-ZH 曝光设置的硬件读回与请求不一致')

    def controls(self):
        return {'auto_exposure': {'value': 1 if self._read(AE_MODE) == MANUAL else 3},
                'exposure_time_absolute': dict(self.limits, value=self._read(EXPOSURE))}

    def set_exposure(self, value):
        minimum, maximum, step = (self.limits[k] for k in ('min', 'max', 'step'))
        if type(value) is not int or not minimum <= value <= maximum or (value - minimum) % step:
            raise ValueError('曝光值超出 USB-ZH 硬件范围或不符合曝光步长')
        if self._read(AE_MODE) != MANUAL:
            self._write(AE_MODE, MANUAL)
        self._write(EXPOSURE, value)
        if self._read(AE_MODE) != MANUAL:
            raise RuntimeError('USB-ZH 未保持手动曝光模式')

    def set_control(self, name, value):
        if name == 'exposure_time_absolute':
            self.set_exposure(value)
        elif name == 'auto_exposure' and value == 1:
            self._write(AE_MODE, MANUAL)
        elif name == 'auto_exposure' and value == 3:
            mode = next((m for m in (8, 2) if self.modes & m), None)
            if mode is None:
                raise ValueError('USB-ZH 不支持自动曝光模式')
            self._write(AE_MODE, mode)
        else:
            raise ValueError('macOS UVC 不支持还原参数：' + name)

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            self.backend.close_device(handle)
