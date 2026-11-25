"""AVFoundation discovery and preview for USB spectrometers on macOS."""
from queue import Empty, Full, Queue
import re
from threading import Event

import AVFoundation as av
import CoreMedia
from Foundation import NSObject
import libdispatch
import numpy as np
import objc
import Quartz

from spectrometer.spectro.camera import DeviceInfo
from .macos_uvc import ExposureControl


def _device_info(device):
    match = re.fullmatch(r'UVC Camera VendorID_(\d{1,5}) ProductID_(\d{1,5})', str(device.modelID()))
    uid = device.uniqueID()
    if not match or not uid:
        return None
    vendor, product = map(int, match.groups())
    if not (0 < vendor <= 0xffff and 0 < product <= 0xffff):
        return None
    return DeviceInfo(str(uid), str(device.localizedName()), f'{vendor:04x}:{product:04x}',
                      'avfoundation', by_id=str(uid))


def _devices():
    with objc.autorelease_pool():
        for device in av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeVideo):
            info = _device_info(device)
            if info is not None:
                yield info, device


def enumerate_devices():
    return [info for info, _ in _devices()]


def _select_format(device):
    # A session preset alone is advisory for this UVC device: it delivered
    # 1080p despite the 720p preset. Pick an actual native 720p YUY2 format.
    for fmt in device.formats():
        desc = fmt.formatDescription()
        size = CoreMedia.CMVideoFormatDescriptionGetDimensions(desc)
        if (size.width, size.height) == (1280, 720) and \
                CoreMedia.CMFormatDescriptionGetMediaSubType(desc) == int.from_bytes(b'yuvs', 'big'):
            break
    else:
        return
    ok, error = device.lockForConfiguration_(None)
    if not ok:
        raise RuntimeError(f'无法锁定光谱仪采集格式：{error}')
    try:
        # AVFoundation switches to input priority when activeFormat is set.
        device.setActiveFormat_(fmt)
    finally:
        device.unlockForConfiguration()


def _check_permission():
    device = av.AVCaptureDevice
    status = device.authorizationStatusForMediaType_(av.AVMediaTypeVideo)
    if status == av.AVAuthorizationStatusNotDetermined:
        decided = Event()
        device.requestAccessForMediaType_completionHandler_(av.AVMediaTypeVideo, lambda _: decided.set())
        if not decided.wait(15):
            raise RuntimeError('请在 macOS 相机权限弹窗中允许访问，再重新连接光谱仪')
        status = device.authorizationStatusForMediaType_(av.AVMediaTypeVideo)
    if status != av.AVAuthorizationStatusAuthorized:
        raise RuntimeError('macOS 未授权相机访问。请用 Apple“终端”打开 start-spectrometer-macos.command 并允许访问；Otty 1.6.5 缺少相机权限声明')


class EnzyflowFrameDelegate(NSObject, protocols=[objc.protocolNamed('AVCaptureVideoDataOutputSampleBufferDelegate')]):
    def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sample, connection):
        pixel = CoreMedia.CMSampleBufferGetImageBuffer(sample)
        flags = Quartz.kCVPixelBufferLock_ReadOnly
        if pixel is None or Quartz.CVPixelBufferLockBaseAddress(pixel, flags) != 0:
            return
        try:
            width = Quartz.CVPixelBufferGetWidth(pixel)
            height = Quartz.CVPixelBufferGetHeight(pixel)
            stride = Quartz.CVPixelBufferGetBytesPerRow(pixel)
            data = Quartz.CVPixelBufferGetBaseAddress(pixel)
            if data is not None:
                rows = np.frombuffer(data.as_buffer(stride * height), dtype=np.uint8).reshape(height, stride)
                frame = rows[:, :width * 4].reshape(height, width, 4)[:, :, :3].copy()
                try:
                    self.frames.put_nowait(frame)
                except Full:
                    pass
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(pixel, flags)


class CameraSource:
    exposure_units = 'UVC exposure_time_absolute (100 µs)'

    def __init__(self, selected):
        self.session = self.output = self.delegate = self.callback_queue = None
        self.exposure = self._controls_cache = None
        self.frames = Queue(maxsize=1)
        self.first = None
        try:
            with objc.autorelease_pool():
                _check_permission()
                matches = [device for info, device in _devices() if info == selected]
                if len(matches) != 1:
                    raise RuntimeError('光谱仪已断开或设备身份已改变，请重新扫描')
                device = matches[0]
                session = self.session = av.AVCaptureSession.alloc().init()
                input_, error = av.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
                if input_ is None or not session.canAddInput_(input_):
                    raise RuntimeError(f'无法打开 USB 光谱仪：{error or "不能添加视频输入"}')
                session.addInput_(input_)
                output = self.output = av.AVCaptureVideoDataOutput.alloc().init()
                output.setVideoSettings_({Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA})
                output.setAlwaysDiscardsLateVideoFrames_(True)
                if not session.canAddOutput_(output):
                    raise RuntimeError('AVFoundation 无法建立光谱仪视频输出')
                session.addOutput_(output)
                _select_format(device)
                self.delegate = EnzyflowFrameDelegate.alloc().init()
                self.delegate.frames = self.frames
                self.callback_queue = libdispatch.dispatch_queue_create(b'enzyflow.spectrometer.frames', None)
                output.setSampleBufferDelegate_queue_(self.delegate, self.callback_queue)
                session.startRunning()
                self.first = self.read()
                if self.first is None:
                    raise RuntimeError('没有收到 USB 光谱仪画面，请关闭占用相机的应用后重试')
                height, width = self.first.shape[:2]
                self.exposure = ExposureControl(selected.by_id)
                self.info = dict(kind='uvc_camera', path=selected.path, width=width, height=height,
                                 fourcc='BGRA')
        except BaseException:
            self.close()
            raise

    def read(self):
        if self.first is not None:
            frame, self.first = self.first, None
            return frame
        try:
            return self.frames.get(timeout=2)
        except Empty:
            return None

    def close(self):
        try:
            if self.exposure is not None:
                self.exposure.close()
        finally:
            self.exposure = None
            self._close_video()

    def _close_video(self):
        with objc.autorelease_pool():
            if self.session is not None:
                self.session.stopRunning()
            if self.output is not None and self.callback_queue is not None:
                self.output.setSampleBufferDelegate_queue_(None, None)
            self.session = self.output = self.delegate = self.callback_queue = None

    def controls(self):
        if self._controls_cache is None:
            self._controls_cache = self.exposure.controls()
        return self._controls_cache

    def get_exposure(self):
        return self.controls()['exposure_time_absolute']['value']

    def set_exposure(self, value):
        self.set_control('exposure_time_absolute', value)

    def set_control(self, name, value):
        self._controls_cache = None
        self.exposure.set_control(name, value)
