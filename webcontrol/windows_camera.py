"""Windows capture for the spectrometer web service using DirectShow."""
from ctypes import HRESULT, POINTER, c_long
import math
import msvcrt
from pathlib import Path
from queue import Empty, Queue
import re
import tempfile

from comtypes import COMError, COMMETHOD, COINIT_MULTITHREADED, CoInitializeEx, CoUninitialize, GUID, IUnknown
from comtypes.persist import IPropertyBag
from pygrabber.dshow_graph import DeviceCategories, FilterGraph, FilterType, IAMStreamConfig, subtypes, SystemDeviceEnum, VideoInput, qedit

from spectrometer.spectro.camera import DeviceInfo


class IAMCameraControl(IUnknown):
    _iid_ = GUID('{C6E13370-30AC-11D0-A18C-00A0C9118956}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetRange', (['in'], c_long, 'property'),
                  *[(['out'], POINTER(c_long), field)
                    for field in ('minimum', 'maximum', 'step', 'default', 'flags')]),
        COMMETHOD([], HRESULT, 'Set', (['in'], c_long, 'property'),
                  (['in'], c_long, 'value'), (['in'], c_long, 'flags')),
        COMMETHOD([], HRESULT, 'Get', (['in'], c_long, 'property'),
                  (['out'], POINTER(c_long), 'value'), (['out'], POINTER(c_long), 'flags')),
    ]


EXPOSURE = 4
AUTO, MANUAL = 1, 2
EXPOSURE_UNITS = '100 µs (DirectShow exposure converted from log2 seconds)'


CAPTURE_MODES = {
    'yuy2-720p': (1280, 720, 'YUY2'),
    'yuy2-600p': (800, 600, 'YUY2'),
    'yuy2-1080p': (1920, 1080, 'YUY2'),
    'mjpeg-720p': (1280, 720, 'MJPG'),
}
LEGACY_CAPTURE_MODE = 'mjpeg-720p'


def _devices():
    system = SystemDeviceEnum()
    enumerator = system.system_device_enum.CreateClassEnumerator(
        GUID(DeviceCategories.VideoInputDevice), dwFlags=0)
    if not enumerator:
        return
    while True:
        moniker, count = enumerator.Next(1)
        if not count:
            return
        try:
            bag = moniker.BindToStorage(0, 0, IPropertyBag._iid_).QueryInterface(IPropertyBag)
            path = bag.Read('DevicePath', pErrorLog=None).lower()
            name = bag.Read('FriendlyName', pErrorLog=None)
        except (COMError, AttributeError, TypeError):
            continue
        match = re.search(r'vid_([0-9a-f]{4})&pid_([0-9a-f]{4})', path)
        if match:
            yield DeviceInfo(path, name, ':'.join(match.groups()), 'directshow', by_id=path), moniker


def _initialize_com():
    try:
        CoInitializeEx(COINIT_MULTITHREADED)
        return True
    except OSError as exc:
        if exc.winerror != -2147417850:  # RPC_E_CHANGED_MODE: already STA
            raise
        return False


def enumerate_devices():
    initialized = _initialize_com()
    try:
        return [device for device, _ in _devices()]
    finally:
        if initialized:
            CoUninitialize()


def lock_camera():
    guard = (Path(tempfile.gettempdir()) / 'enzyflow-spectrometer.lock').open('a+b')
    try:
        guard.seek(0)
        msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        guard.close()
        raise RuntimeError('光谱仪正在被另一个 Enzyflow 进程使用') from None
    return guard


def _exposure_ticks(log_seconds):
    return max(1, round(10000 * 2.0 ** log_seconds))


class CameraSource:
    paced = False
    exposure_units = EXPOSURE_UNITS

    def __init__(self, device, capture_mode=LEGACY_CAPTURE_MODE):
        self.graph = self.control = None
        self.frames = Queue(maxsize=1)
        self._controls_cache = None
        self.com_initialized = _initialize_com()
        try:
            graph = self.graph = FilterGraph()
            for current, moniker in _devices():
                if current.by_id == device.by_id and current.vid_pid == device.vid_pid:
                    base_filter = moniker.BindToObject(0, 0, qedit.IBaseFilter._iid_).QueryInterface(qedit.IBaseFilter)
                    break
            else:
                raise RuntimeError('光谱仪已断开或设备路径已改变，请重新扫描')
            video = VideoInput((base_filter, device.name), graph.capture_builder)
            graph.filter_graph.AddFilter(video.instance, video.Name)
            graph.filters[FilterType.video_input] = video
            self.control = base_filter.QueryInterface(IAMCameraControl)
            self.exposure_range = self.control.GetRange(EXPOSURE)

            requested = CAPTURE_MODES[capture_mode]
            chosen = next((fmt for fmt in video.get_formats()
                           if (fmt['width'], fmt['height'], fmt['media_type_str']) == requested), None)
            if chosen is None:
                raise ValueError(f'此相机不支持所选采集模式：{capture_mode}')
            video.set_format(chosen['index'])
            graph.add_sample_grabber(self.frames.put_nowait)
            graph.add_null_render()
            graph.prepare_preview_graph()
            stream_config = video.get_out().QueryInterface(IAMStreamConfig)
            media = stream_config.GetFormat()
            negotiated = subtypes.get(str(media.contents.subtype))
            if negotiated != requested[2]:
                raise RuntimeError(f'相机实际输出 {negotiated}，与所选 {requested[2]} 不符')
            graph.run()
            first = self.read()
            if first is None:
                raise RuntimeError('DirectShow 未收到光谱仪画面')
            height, width = first.shape[:2]
            if (width, height) != requested[:2]:
                raise RuntimeError('相机实际输出分辨率与所选模式不符')
            self.info = dict(kind='uvc_camera', path=device.path, width=width, height=height,
                             fps=chosen['max_framerate'], fourcc='RGB24',
                             capture_mode=capture_mode, source_fourcc=negotiated,
                             exposure_options=self.exposure_options())
        except BaseException:
            self.close()
            raise

    def read(self):
        if self.frames.empty():
            self.graph.grab_frame()
        try:
            return self.frames.get(timeout=2)
        except Empty:
            return None

    def controls(self):
        if self._controls_cache is None:
            value, flags = self.control.Get(EXPOSURE)
            minimum, maximum, _, default, _ = self.exposure_range
            self._exposure_details = {'native': value, 'milliseconds': 1000 * 2.0 ** value}
            self._controls_cache = {
                'auto_exposure': {'value': 3 if flags & AUTO else 1},
                'exposure_time_absolute': {'value': _exposure_ticks(value),
                                           'min': _exposure_ticks(minimum),
                                           'max': _exposure_ticks(maximum), 'default': _exposure_ticks(default)},
            }
        return self._controls_cache

    def exposure_options(self):
        minimum, maximum, step, _, _ = self.exposure_range
        options = {}
        for setting in range(minimum, maximum + 1, max(1, step)):
            value = _exposure_ticks(setting)
            if value > 5000:
                break
            # Several very short settings can round to the same 100 us tick.
            # Show the time the existing rounded-value API would select.
            native = self._nearest_exposure(value)
            options[value] = {'value': value, 'milliseconds': 1000 * 2.0 ** native}
        return list(options.values())

    def exposure_info(self):
        self.controls()
        return self._exposure_details

    def get_exposure(self):
        return self.controls()['exposure_time_absolute']['value']

    def _nearest_exposure(self, value):
        minimum, maximum, step, _, _ = self.exposure_range
        target = math.log2(value / 10000)
        step = max(1, step)
        setting = minimum + round((target - minimum) / step) * step
        return max(minimum, min(maximum, setting))

    def set_exposure(self, value):
        # Round in the same units as the webpage. This also preserves settings
        # such as 1 tick when several driver values round to that value.
        current, _ = self.control.Get(EXPOSURE)
        if value == _exposure_ticks(current):
            setting = current
        else:
            setting = self._nearest_exposure(value)
        self.control.Set(EXPOSURE, setting, MANUAL)
        self._controls_cache = None

    def set_control(self, name, value):
        if name == 'exposure_time_absolute':
            self.set_exposure(value)
        elif name == 'auto_exposure':
            current, _ = self.control.Get(EXPOSURE)
            self.control.Set(EXPOSURE, current, AUTO if value == 3 else MANUAL)
            self._controls_cache = None
        else:
            raise ValueError('DirectShow 不支持还原参数：' + name)

    def close(self):
        graph, self.graph = self.graph, None
        try:
            if graph is not None:
                try:
                    graph.stop()
                finally:
                    graph.remove_filters()
        finally:
            self.control = None
            del graph
            if self.com_initialized:
                self.com_initialized = False
                CoUninitialize()
