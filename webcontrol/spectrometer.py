"""Independent UVC worker. Never imports or accesses the motor transport.

Camera reads and processing run outside the motor worker and HTTP threads.
No automatic connection/reconnection; only the known spectrometer USB identity
and a freshly enumerated capture interface can be opened.
"""
from __future__ import annotations

import copy
from datetime import datetime
import json
from pathlib import Path
import queue
import re
import sys
import threading
import time
import uuid
from urllib.parse import quote
from . import saturation

if sys.platform == 'win32':
    from . import windows_camera
else:
    import fcntl
    windows_camera = None

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'spectrometer/runtime/web'
USB_ID = '0c68:6464'


def modules():
    # Namespace package imports keep the existing standalone CLI working.
    from spectrometer.spectro import camera, calibration, config, roi, spectrum, recording
    import cv2
    import numpy as np
    return camera, calibration, config, roi, spectrum, recording, cv2, np


def eligible(device):
    if windows_camera:
        return (device.capture and device.vid_pid.lower() == USB_ID
                and device.driver == 'directshow' and bool(device.by_id))
    if sys.platform == 'darwin':
        return (device.capture and device.vid_pid.lower() == USB_ID
                and device.driver == 'avfoundation' and bool(device.by_id))
    return (device.capture and device.vid_pid.lower() == USB_ID
            and re.fullmatch(r'/dev/video\d+', device.path) is not None
            and device.driver == 'uvcvideo')


def identity(device):
    return {'vid_pid': device.vid_pid.lower(), 'by_id': device.by_id, 'name': device.name}


def select_camera(devices, selected):
    matches = [d for d in devices if eligible(d) and identity(d) == selected]
    if len(matches) != 1:
        raise ValueError('光谱仪身份缺失、改变或存在多个相同设备；请重新扫描选择。')
    return matches[0]


def macos_backend():
    try:
        from . import macos_camera
    except ImportError as exc:
        raise RuntimeError('macOS 光谱仪依赖未安装，请安装 webcontrol/requirements-spectrometer.txt') from exc
    return macos_camera


class Spectrometer:
    def __init__(self, demo=False, data_dir=DATA):
        self.demo = demo
        self.data_dir = Path(data_dir)
        self.lock = threading.Lock()
        self.commands = queue.Queue(maxsize=16)
        self.stop_event = threading.Event()
        self.thread = None
        self.preview = None
        self.exports = {}
        self.state = dict(status='disconnected', connected=False, demo=demo,
                          device=None, record=None, profile=None, lamp=None, calibration_report=None, dark={'status':'none','message':'尚未采集暗场'}, error=None, message='光谱仪未连接', files={})
        self.saturation_path = self.data_dir / ('demo-saturation.json' if demo else 'saturation.json')
        try:
            self.saturation_policy = saturation.load(self.saturation_path)
        except (ValueError, OSError):
            self.saturation_policy = dict(saturation.DEFAULT)
            self.state['error'] = '饱和设置文件无效，已恢复默认（仅提示、不拦截）'
        self.state['saturation'] = dict(self.saturation_policy)
        self.refresh_files()
        # Show saved calibration even before connecting; applying it still checks binding.
        cal_path = self.data_dir / ('demo-calibration.json' if demo else 'calibration.json')
        if cal_path.exists():
            try:
                stored = json.loads(cal_path.read_text(encoding='utf-8'))
                self.state.update(calibration_report=stored.get('report'), saved_lamp=stored.get('lamp'),
                    wavelength_calibration={'saved':True,'path':str(cal_path),'id':stored['calibration'].get('calibration_id')})
            except (OSError, ValueError, KeyError, TypeError):
                self.state['error'] = '保存的波长标定文件无法读取，请检查文件'

    def refresh_files(self):
        paths = sorted((self.data_dir/'captures').glob('*'), key=lambda p:p.stat().st_mtime if p.exists() else 0, reverse=True)
        paths += [self.data_dir/n for n in ('calibration.json','demo-calibration.json','halogen-reference.json','demo-halogen-reference.json')]
        with self.lock:
            self.exports = {p.name:p for p in paths if p.is_file() and not p.is_symlink() and p.suffix in ('.json','.jsonl','.csv','.png')}
            self.state['archive'] = [{'name':p.name, 'url':'/api/spectrometer-file/'+quote(p.name), 'bytes':p.stat().st_size} for p in self.exports.values()]
            self.state['capture_directory'] = str((self.data_dir/'captures').resolve())

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
        if result.get('frame_time'):
            result['age_ms'] = round((time.monotonic()-result['frame_time'])*1000)
        result.pop('frame_time', None)
        return result

    def devices(self):
        if self.demo:
            return [dict(path='synthetic', name='合成光谱（演示）', vid_pid='demo', by_id='synthetic', eligible=True)]
        camera = macos_backend() if sys.platform == 'darwin' else windows_camera or modules()[0]
        return [dict(d.to_dict(), eligible=eligible(d),
                     capture_modes=list(windows_camera.CAPTURE_MODES) if windows_camera and eligible(d) else [])
                for d in camera.enumerate_devices()]

    def command(self, body):
        if type(body) is not dict:
            raise ValueError('光谱仪请求必须是对象')
        op = body.get('op')
        if op == 'connect':
            with self.lock:
                if self.thread and self.thread.is_alive():
                    raise ValueError('光谱仪已连接或尚未结束，请等待断开完成')
                if not self.demo and type(body.get('device')) is not dict:
                    raise ValueError('请先扫描并选择光谱仪')
                capture_mode = body.get('capture_mode')
                if capture_mode is not None and not (windows_camera and not self.demo and
                        isinstance(capture_mode, str) and capture_mode in windows_camera.CAPTURE_MODES):
                    raise ValueError('无效的光谱仪采集模式')
                self.stop_event.clear()
                self.commands = queue.Queue(maxsize=16)
                self.preview = None
                self.state.update(status='connecting', connected=False, record=None, frame_time=None, recording=False, roi_selection=None, profile=None, lamp=None, saved_lamp=None, wavelength_calibration=None, halogen=None, halogen_capture=None, capillary_capture=None, calibration_report=None, dark={'status':'none','message':'正在检查已保存的校准'}, error=None, files={}, message='正在核对光谱仪身份')
                self.thread = threading.Thread(target=self._run, args=(body.get('device'), body.get('guided') is True, capture_mode), daemon=True, name='uvc-spectrometer')
                self.thread.start()
        elif op == 'disconnect':
            self.stop_event.set()
            with self.lock:
                if self.thread and self.thread.is_alive():
                    self.state.update(status='stopping', message='正在释放光谱仪')
        elif op in {'roi', 'auto_roi', 'processing', 'zero', 'clear_zero', 'exposure', 'calibrate', 'clear_calibration', 'save', 'absorbance_save', 'saturation_settings', 'record_start', 'record_stop', 'dark_start', 'dark_clear', 'dark_save', 'dark_reset',
                    'history_load', 'history_roi', 'history_peaks', 'roi_save', 'roi_reset',
                    'peaks_save', 'peaks_reset', 'exposure_auto', 'lamp_capture', 'lamp_calibrate', 'profile_restore', 'halogen_capture',
                    'capillary_begin', 'capillary_ping', 'capillary_collect', 'capillary_end'}:
            with self.lock:
                if (not self.state['connected'] or self.stop_event.is_set() or
                    time.monotonic()-self.state.get('frame_time',0)>3):
                    raise ValueError('请先连接光谱仪并等待首帧')
            try:
                self.commands.put_nowait(copy.deepcopy(body))
            except queue.Full:
                raise ValueError('光谱仪操作队列已满，请等待')
        else:
            raise ValueError('未知光谱仪操作')
        return {'ok': True, 'accepted': True}

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _update(self, **values):
        with self.lock:
            self.state.update(values)

    def _run(self, selected, guided=False, capture_mode=None):
        source = guard = None
        try:
            camera, calibration, config, roi, spectrum, recording, cv2, np = modules()
            calibration_dir = self.data_dir
            windows_capture = bool(windows_camera and not self.demo)
            if self.demo:
                source = camera.SyntheticSource()
                device_info = dict(source.info, name='合成光谱（演示）', vid_pid='demo', by_id='synthetic')
            elif windows_camera:
                guard = windows_camera.lock_camera()
                device = select_camera(windows_camera.enumerate_devices(), selected)
                capture_mode = capture_mode or windows_camera.LEGACY_CAPTURE_MODE
                source = windows_camera.CameraSource(device, capture_mode)
                if capture_mode != windows_camera.LEGACY_CAPTURE_MODE:
                    calibration_dir = self.data_dir / capture_mode
                device_info = dict(source.info, **identity(device), node=device.path)
            else:
                capture_backend = macos_backend() if sys.platform == 'darwin' else camera
                device = select_camera(capture_backend.enumerate_devices(), selected)
                # Cross-process lock distinct from the ESP32 lock.
                guard = open('/tmp/enzyflow-spectrometer.lock', 'a')
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if sys.platform == 'darwin':
                    source = capture_backend.CameraSource(device)
                else:
                    # Use by-id when available; recheck its target before opening.
                    path = '/dev/v4l/by-id/' + device.by_id if device.by_id else device.path
                    if str(Path(path).resolve()) != device.path:
                        raise ValueError('光谱仪节点已改变，请重新扫描')
                    source = camera.CameraSource(path)
                device_info = dict(source.info, **identity(device), node=device.path)
            processing = config.ProcessingConfig()
            processor = spectrum.SpectrumProcessor(processing)
            region = cal = lamp = halogen = saved_lamp = None
            from .signal_roi import SignalRoiFinder
            roi_finder = None if guided else SignalRoiFinder()
            roi_ready = False
            auto_roi_draft = False
            seq = 0
            recording_path = None
            record_interval, record_at = 1.0, 0.0
            exposure = {'auto': None, 'value': None, 'units': getattr(source, 'exposure_units', 'V4L2 exposure_time_absolute (100 us)')}
            if not self.demo:
                controls = source.controls()
                limits = controls.get('exposure_time_absolute', {})
                exposure['min'] = max(1, limits.get('min', 1))
                exposure['max'] = min(5000, limits.get('max', 5000))
                exposure['default'] = max(exposure['min'], min(exposure['max'], limits.get('default', 300)))
                exposure.update(value=source.get_exposure(), auto=controls.get('auto_exposure', {}).get('value') == 3)
            binding = {k: device_info.get(k) for k in ('vid_pid', 'by_id', 'name', 'width', 'height')}
            from .calibration_history import CalibrationHistory, encode_dark, decode_dark
            archive_dir = (ROOT / 'spectrometer/calibration-history' / calibration_dir.relative_to(self.data_dir)
                           if self.data_dir == DATA else None)
            history = CalibrationHistory(calibration_dir, binding, self.demo, archive_dir=archive_dir)
            cal_path = calibration_dir / ('demo-calibration.json' if self.demo else 'calibration.json')
            if cal_path.exists():
                saved = json.loads(cal_path.read_text(encoding='utf-8'))
                if saved.get('binding') == binding:
                    cal = calibration.Calibration.from_dict(saved['calibration'])
                    saved_lamp = saved.get('lamp')
                    self._update(calibration_report=saved.get('report'), wavelength_calibration={'saved':True,'path':str(cal_path),'id':cal.calibration_id}, saved_lamp=saved_lamp)
            halogen_path = calibration_dir / ('demo-halogen-reference.json' if self.demo else 'halogen-reference.json')
            standalone_halogen = None
            halogen_source = history.source_path(halogen_path)
            if halogen_source.exists():
                try:
                    candidate = json.loads(halogen_source.read_text(encoding='utf-8'))
                    if candidate['schema']==1 and candidate['binding']==binding:
                        standalone_halogen = candidate
                except (ValueError, KeyError, TypeError):
                    self._update(error='保存的卤素灯参考文件无效')
            self._update(device=device_info, message='已连接；请核对预览中的光谱带', error=None)
            dark_job = None
            dark_controls = None
            check_controls_at = 0.0
            def hardware_controls():
                if self.demo:
                    return {'auto_exposure':1, 'exposure_time_absolute':100}
                source._controls_cache = None
                return {k:v.get('value') for k,v in source.controls().items()}

            def invalidate_dark(message):
                nonlocal dark_job, dark_controls, lamp
                dark_job = dark_controls = lamp = None
                processor.clear_dark_reference()
                self._update(dark={'status':'none','message':message}, lamp=None)

            from .halogen_reference import BLANK_FRAMES, capture as capture_blank, compare, matches as reference_matches
            from .instrument_profile import read_profile, write_profile, dark_matches, restore_controls
            profile_path = calibration_dir / ('demo-instrument-profile.json' if self.demo else 'instrument-profile.json')
            profile = {'schema':1,'binding':binding,'settings':None,'dark':None}
            initial_save = not guided and not profile_path.exists()
            history_key = None
            working_dark = None
            dark_pending = False
            dark_reset = False
            decoded_dark = None

            def refresh_history():
                self._update(history=history.scan(), active_history=history_key)

            def apply_wave(wave):
                nonlocal cal, lamp, saved_lamp
                candidate = calibration.Calibration.from_dict(wave['calibration']) if wave else None
                if candidate:
                    axis = candidate.wavelengths(0,binding['width'])
                    delta = np.diff(axis)
                    if not np.all(np.isfinite(axis)) or not (np.all(delta>0) or np.all(delta<0)):
                        raise ValueError('保存的波长标定在当前画面内无效')
                cal, lamp = candidate, None
                saved_lamp = wave.get('lamp') if wave else None
                self._update(lamp=None, saved_lamp=saved_lamp,
                    calibration_report=wave.get('report') if wave else None,
                    wavelength_calibration={'saved':cal is not None,'id':cal.calibration_id if cal else None})

            def archive_stage(stage, wave=None, reference=None):
                nonlocal history_key
                if stage == 'dark':
                    wave = dict(binding=binding,calibration=cal.to_dict(),
                                report=cal.verification,lamp=saved_lamp) if cal else None
                    data = dict(schema=1,binding=binding,settings=settings_now(),dark=working_dark,
                                roi_saved=roi_ready and roi_finder is None,wavelength=wave,reference=reference)
                    key = None
                elif stage == 'peaks' and history_key is None:
                    data = dict(schema=1,binding=binding,settings=settings_now(),dark=None,
                                roi_saved=True,wavelength=wave,reference=None)
                    key = None
                else:
                    if history_key is None:
                        raise ValueError('请先保存暗场或选择一份已有校正记录')
                    data = history.read(history_key)
                    key = history_key
                    if stage == 'roi':
                        if not roi_ready or roi_finder is not None:
                            raise ValueError('请先确定采样范围')
                        if data['dark'] and not dark_matches(data['dark'],binding,settings_now()['roi'],vars(processing),hardware_controls()):
                            raise ValueError('当前曝光或采样范围与保存的暗场不匹配')
                        if data['settings']['roi'] != settings_now()['roi']:
                            data.update(wavelength=None, reference=None)
                        data.update(settings=settings_now(),roi_saved=True)
                    elif stage == 'peaks':
                        if not data.get('roi_saved') or data['settings']['roi'] != settings_now()['roi']:
                            raise ValueError('请先保存当前范围')
                        data['wavelength'] = wave
                    elif stage == 'reference':
                        if data.get('roi_saved') and data['settings']['roi'] == settings_now()['roi']:
                            data['reference'] = reference
                        else:
                            raise ValueError('请先保存当前采样范围')
                history_key, name = history.save(data,key)
                refresh_history()
                self._update(history_message='已保存 ' + name)
                return history.directory / name

            def set_region(candidate, message):
                nonlocal region, processor, roi_finder, roi_ready
                if candidate != region:
                    region = candidate
                    invalidate_dark('采样范围已改变，正在检查暗场兼容性')
                    processor = spectrum.SpectrumProcessor(processing)
                roi_finder = None
                roi_ready = True
                self._update(roi_selection={'ready':True,'scanning':False,'message':message})
                activate_saved_dark()

            def settings_now():
                return {'roi':dict(zip(('x','y','w','h'),region.as_tuple())),
                        'processing':copy.deepcopy(vars(processing)), 'controls':hardware_controls()}

            def persist_profile(dark_update=False, dark_value=None):
                nonlocal profile
                updated = copy.deepcopy(profile)
                updated['settings'] = settings_now()
                if dark_update:
                    updated['dark'] = dark_value
                write_profile(profile_path,updated)
                profile = updated
                self._update(profile={'saved':True,'dark_saved':profile['dark'] is not None,
                    'path':str(profile_path),'message':('采集设置与底噪已保存，重连后自动恢复' if profile['dark'] is not None else '采集设置已保存；首次暗场校准仍需采集一次')})

            def activate_saved_dark():
                nonlocal dark_controls
                nonlocal decoded_dark
                d = working_dark if working_dark is not None else (None if dark_reset else profile.get('dark'))
                current_roi = dict(zip(('x','y','w','h'),region.as_tuple()))
                if dark_matches(d,binding,current_roi,vars(processing),hardware_controls()):
                    metadata = copy.deepcopy(d['metadata'])
                    background = d['background']
                    if d.get('image'):
                        if decoded_dark is None or decoded_dark[0] != metadata['id']:
                            decoded_dark = (metadata['id'], decode_dark(d,binding))
                        mean, deviation = (region.crop(array) for array in decoded_dark[1])
                        background = mean.mean(axis=0)
                        metadata.update(mean_counts=float(mean.mean()), max_counts=float(mean.max()),
                                        temporal_std_mean=float(deviation.mean()))
                    processor.set_dark_reference(background,metadata,region.x)
                    dark_controls=copy.deepcopy(metadata['controls'])
                    self._update(dark={'status':'ready','count':metadata['frames'],'total':metadata['frames'],
                        'metadata':metadata,'savable':dark_pending,
                        'message':'已应用匹配的暗场'})
                    return True
                if d:
                    legacy_roi = d['metadata']['roi']
                    if not d.get('image') and legacy_roi != current_roi:
                        message = ('旧暗场只覆盖 x={x}, y={y} 的 {w} × {h} 像素区域。'
                                   '请保持当前采样范围，遮光重采暗场。').format(**legacy_roi)
                    else:
                        message = '保存的暗场与当前曝光或处理设置不匹配。请恢复原设置，或遮光重采暗场。'
                    self._update(dark={'status':'none','message':message})
                return False

            def restore_profile(use_dark_settings=False, saved=None):
                archived = saved is not None
                nonlocal profile, processing, processor, region, lamp, dark_job, dark_controls, roi_finder, roi_ready
                if saved is None:
                    saved=read_profile(profile_path,binding)
                if saved is None:
                    raise ValueError('尚无保存的光谱仪配置，请先完成一次校准')
                settings=(saved['dark']['metadata'] if use_dark_settings and saved.get('dark') else saved['settings'])
                if not self.demo:
                    desired = dict(settings['controls'])
                    desired['auto_exposure'] = 1
                    desired['exposure_time_absolute'] = desired.get('exposure_time_absolute') or 300
                    restore_controls(source,desired)
                if not archived:
                    profile = saved
                processing=config.ProcessingConfig(**settings['processing'])
                processor=spectrum.SpectrumProcessor(processing)
                region=roi.Roi(**settings['roi'])
                roi_finder = None
                roi_ready = not archived or saved.get('roi_saved',False)
                self._update(roi_selection={'ready':roi_ready,'scanning':False,'message':
                    '已恢复保存的采样范围' if roi_ready else '请自动识别或在预览中拖选'})
                lamp=dark_job=dark_controls=None
                measured=hardware_controls()
                exposure.update(auto=measured.get('auto_exposure')==3,value=measured.get('exposure_time_absolute'))
                self._update(lamp=None,profile={'saved':True,'dark_saved':saved.get('dark') is not None,
                    'path':str(profile_path),'message':'已恢复保存的ROI、曝光及处理设置'})
                if not activate_saved_dark() and not saved.get('dark'):
                    self._update(dark={'status':'none','message':'尚未保存暗场，请采集一次'})

            if profile_path.exists():
                try:
                    restore_profile()
                except Exception as exc:
                    self._update(profile={'saved':True,'dark_saved':False,'path':str(profile_path),
                        'message':'保存的配置未应用：'+str(exc)},error='校准配置恢复失败：'+str(exc))

            self._update(history_message='')
            refresh_history()
            if not self.demo:
                measured = hardware_controls()
                if measured.get('auto_exposure') != 1:
                    source.set_exposure(exposure['value'] or 300)
                measured = hardware_controls()
                if measured.get('auto_exposure') != 1:
                    raise ValueError('无法固定手动曝光，请检查相机控制接口')
                exposure.update(auto=False,value=measured.get('exposure_time_absolute'))
            else:
                exposure.update(auto=False,value=100)
            if guided and not roi_ready:
                self._update(roi_selection={'ready':False,'scanning':False,'message':'请自动识别或在预览中拖选'})
            if roi_finder is not None:
                self._update(roi_selection={'ready':False,'scanning':True,'message':'正在平均8帧识别全部光信号；首次请接入完整的卤素灯光谱'})
            last_frame = result = None
            preview_at = 0.0
            photometry = None
            halogen_job = frame_reference = None
            scan_id, scan_until = None, 0.0
            while not self.stop_event.is_set():
                if scan_id is not None and time.monotonic() >= scan_until:
                    scan_id = halogen_job = None
                    self._update(capillary_capture=None, message='毛细管联动已停止')
                completed_dark = None
                auto_message = None
                # Apply settings in the camera's owning thread only.
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    command = None
                if command:
                    try:
                        op = command['op']
                        if scan_id is not None and op not in ('capillary_ping','capillary_collect','capillary_end'):
                            raise ValueError('毛细管联动正在使用光谱仪')
                        if halogen_job is not None and op not in ('capillary_ping','capillary_end'):
                            raise ValueError('正在采集空白 I₀，请等待采集完成')
                        if not roi_ready or roi_finder is not None:
                            if op in ('lamp_capture','lamp_calibrate','peaks_save','halogen_capture','save','absorbance_save','capillary_begin','capillary_collect'):
                                raise ValueError('请先选择采样范围，再采集灯谱或参考')
                        if recording_path is not None and op in {
                            'exposure_auto','exposure','dark_reset','history_load','history_roi',
                            'history_peaks','roi_reset','peaks_reset','peaks_save','halogen_capture','capillary_begin'
                        }:
                            raise ValueError('请先结束连续记录')
                        if dark_job is not None and op not in ('dark_clear','dark_reset','record_stop'):
                            raise ValueError('正在采集暗场，请保持遮光；完成或取消后再操作')
                        if op == 'capillary_begin':
                            requested = command.get('scan_id')
                            if not isinstance(requested,str) or not re.fullmatch(r'[0-9a-f]{32}',requested):
                                raise ValueError('无效的毛细管扫描标识')
                            if (dark_controls is None or result.dark_profile is None
                                or processor.zero_active or hardware_controls() != dark_controls):
                                raise ValueError('联动扫描前请确认手动曝光与暗场匹配，并清除零参考')
                            scan_id, scan_until = requested, time.monotonic()+10
                            self._update(capillary_capture={'scan_id':scan_id,'status':'idle'},error=None)
                        elif op in ('capillary_ping','capillary_end'):
                            if scan_id is None or command.get('scan_id') != scan_id:
                                raise ValueError('毛细管扫描会话已结束')
                            if op == 'capillary_end':
                                scan_id = halogen_job = None
                                self._update(capillary_capture=None, message='毛细管联动已停止')
                            else:
                                scan_until = time.monotonic()+10
                        elif op == 'saturation_settings':
                            self.saturation_policy = saturation.save(self.saturation_path, command)
                            self._update(saturation=dict(self.saturation_policy))
                        elif op == 'dark_start':
                            full = command.get('full_frame') is True
                            if full and processing.linearize != 'off':
                                raise ValueError('整幅暗场需要关闭软件线性化')
                            if not full and (not roi_ready or roi_finder is not None):
                                raise ValueError('请先打开入光口，等待自动识别光谱范围后再遮光采集暗场')
                            frames = command.get('frames',32)
                            if command.get('confirmed') is not True:
                                raise ValueError('请先确认入光口完全遮光')
                            if type(frames) is not int or not 8<=frames<=256:
                                raise ValueError('暗场平均帧数必须为 8–256')
                            if recording_path is not None:
                                raise ValueError('请先结束连续记录，再采集暗场')
                            controls = hardware_controls()
                            if controls.get('auto_exposure') != 1 or controls.get('exposure_time_absolute') is None:
                                raise ValueError('暗场校正需要固定手动曝光；请关闭自动曝光并应用曝光设置')
                            invalidate_dark('正在采集新的暗场')
                            dark_controls = controls
                            sampler_config = copy.deepcopy(processing)
                            sampler_config.buffer_size = 1
                            sampler_config.hold_peaks = sampler_config.savgol_enabled = False
                            dark_job = {'sampler':spectrum.SpectrumProcessor(sampler_config),
                                        'total':frames,'count':0,'discard':3,'sum':None,'sum2':None,'full':full}
                            check_controls_at = 0.0
                            self._update(dark={'status':'collecting','count':0,'total':frames,'message':'请保持遮光，正在丢弃3帧缓存'})
                        elif op == 'dark_save':
                            if not dark_pending or dark_controls is None:
                                raise ValueError('请先采集匹配的新暗场')
                            current_reference = frame_reference if reference_matches(frame_reference,record,binding,settings_now()) else None
                            archive_stage('dark', reference=current_reference)
                            dark_pending = False
                            halogen = current_reference
                            activate_saved_dark()
                        elif op == 'dark_reset':
                            working_dark = decoded_dark = None
                            dark_pending = False
                            dark_reset = True
                            history_key = None
                            invalidate_dark('当前暗场已重置，历史文件仍可选择')
                            refresh_history()
                            self._update(history_message='')
                        elif op == 'history_load':
                            chosen = command.get('id')
                            saved = history.read(chosen)
                            waveform = saved.get('wavelength')
                            if saved.get('dark',{}) and saved['dark'].get('image'):
                                decode_dark(saved['dark'],binding)
                            history_key = chosen
                            working_dark = saved.get('dark')
                            dark_pending = False
                            dark_reset = working_dark is None
                            use_original_dark = chosen in history.legacy and not waveform and not saved.get('reference')
                            restore_profile(use_dark_settings=use_original_dark,saved=saved)
                            apply_wave(waveform)
                            halogen = saved.get('reference')
                            refresh_history()
                            self._update(history_message='已载入所选校正记录'+
                                         ('；暗场保存在文件中，但当前未应用' if working_dark and dark_controls is None else ''))
                        elif op == 'history_roi':
                            saved = history.read(command.get('id'))
                            if not saved.get('roi_saved'):
                                raise ValueError('该记录没有保存采样范围')
                            set_region(roi.Roi(**saved['settings']['roi']), '已载入所选采样范围')
                        elif op == 'roi_save':
                            archive_stage('roi')
                        elif op == 'roi_reset':
                            set_region(roi.Roi(0,0,binding['width'],binding['height']), '范围已重置，请自动识别或在预览拖选')
                            roi_ready = False
                            self._update(roi_selection={'ready':False,'scanning':False,'message':'范围已重置，请自动识别或在预览拖选'})
                        elif op == 'history_peaks':
                            wave = history.read(command.get('id')).get('wavelength')
                            if not wave:
                                raise ValueError('该记录没有保存波长标定')
                            apply_wave(wave)
                        elif op == 'peaks_reset':
                            apply_wave(None)
                        elif op == 'dark_clear':
                            cancelling = dark_job is not None
                            invalidate_dark('暗场已取消' if cancelling else '已清除保存的暗场；波长标定保留')
                            if cancelling:
                                activate_saved_dark()
                            else:
                                persist_profile(dark_update=True,dark_value=None)
                        elif op == 'profile_restore':
                            restore_profile(use_dark_settings=True)
                            persist_profile()
                        elif op in ('roi', 'auto_roi'):
                            if recording_path is not None:
                                raise ValueError('请先结束记录再重新识别光谱范围')
                            if op == 'auto_roi':
                                auto_roi_draft = command.get('draft') is True
                                # Keep the old extraction and calibration until a
                                # supported light signal is actually found.
                                roi_finder = SignalRoiFinder()
                                self._update(roi_selection={'ready':roi_ready,'scanning':True,
                                    'message':'正在平均8帧识别全部光信号；请使用完整的卤素灯光谱'})
                            else:
                                vals = [command.get(k) for k in ('x', 'y', 'w', 'h')]
                                if any(type(v) is not int for v in vals):
                                    raise ValueError('ROI 必须是整数像素')
                                x, y, w, h = vals
                                fh, fw = last_frame.shape[:2]
                                if min(x,y)<0 or min(w,h)<1 or x+w>fw or y+h>fh:
                                    raise ValueError('ROI 超出当前图像范围')
                                candidate = roi.Roi(*vals)
                                set_region(candidate, '已设置采样范围')
                                if command.get('draft') is not True:
                                    persist_profile()
                        elif op == 'processing':
                            n = command.get('average', 8)
                            if type(n) is not int or not 1 <= n <= 100:
                                raise ValueError('平均帧数必须为 1–100')
                            if any(type(command.get(k)) is not bool for k in ('smooth','hold')):
                                raise ValueError('平滑和峰值保持必须为布尔值')
                            invalidate_dark('正在更新显示和平均设置，底噪保留')
                            processing.buffer_size = n
                            processing.savgol_enabled = command['smooth']
                            processing.hold_peaks = command['hold']
                            processor = spectrum.SpectrumProcessor(processing)
                            if not guided:
                                persist_profile()
                            activate_saved_dark()
                        elif op == 'zero':
                            processor.set_zero_reference()
                        elif op == 'clear_zero':
                            processor.clear_zero_reference()
                        elif op == 'exposure_auto':
                            if self.demo:
                                raise ValueError('模拟源使用固定曝光')
                            from .exposure_adjustment import adjust_exposure
                            self._update(message='正在自动调整曝光，请保持光源稳定', error=None)
                            limit = self.saturation_policy['threshold']
                            measured, peak = adjust_exposure(source, region if roi_ready else None,
                                exposure['min'], exposure['max'], self.stop_event, threshold=limit)
                            if measured != exposure['value']:
                                invalidate_dark('曝光已改变，请采集匹配的暗场')
                                processor.clear_buffer()
                                processor.clear_zero_reference()
                                activate_saved_dark()
                            exposure.update(auto=False,value=measured)
                            auto_message = f'已锁定低于 {limit} counts 的最长曝光档，选区实测峰值 {peak} counts'
                            if peak < .9 * limit:
                                auto_message += '，受光强或相机档位限制，尚未接近阈值'
                        elif op == 'exposure':
                            if self.demo:
                                raise ValueError('合成光谱没有曝光控制')
                            auto = command.get('auto', False)
                            value = command.get('value')
                            if auto is not False:
                                raise ValueError('网页采集固定使用手动曝光，以便复用暗场和卤素灯参考')
                            if type(value) is not int or not 1<=value<=5000:
                                raise ValueError('曝光必须为 1–5000 的整数')
                            source.set_exposure(value)
                            measured = source.get_exposure()
                            if measured != exposure['value']:
                                invalidate_dark('曝光设置已改变，请重新采集暗场')
                                processor.clear_buffer()
                                processor.clear_zero_reference()
                                if roi_finder is not None:
                                    roi_finder = SignalRoiFinder()
                            exposure.update(auto=False, value=measured)
                            if command.get('draft') is not True:
                                persist_profile()
                            activate_saved_dark()
                        elif op in ('halogen_capture','capillary_collect'):
                            capillary_point = None
                            if op == 'capillary_collect':
                                position, kind = command.get('position_mm'), command.get('kind')
                                if (scan_id is None or command.get('scan_id') != scan_id
                                    or type(position) is not int or not 0 <= position <= 320
                                    or kind not in ('blank','sample')):
                                    raise ValueError('无效的毛细管扫描会话或采样位置')
                                capillary_point = {'scan_id':scan_id,'position_mm':position,'kind':kind}
                            elif command.get('confirmed') is not True:
                                raise ValueError('请确认当前为空白光路、无样品的卤素灯参考')
                            current_settings = settings_now()
                            if current_settings['controls'].get('auto_exposure') != 1:
                                raise ValueError('请先应用手动曝光')
                            if dark_controls is None or result.dark_profile is None or current_settings['controls'] != dark_controls:
                                raise ValueError('未应用匹配的暗场。请保持采样范围与曝光，遮光重采并保存暗场。')
                            if processor.zero_active:
                                raise ValueError('请先清除样品零参考，再保存 I₀。')
                            # Saving I₀ only depends on applied settings. Use an
                            # existing archive when it describes this exact setup.
                            saved = None
                            if history_key is not None and capillary_point is None:
                                try:
                                    saved = history.read(history_key)
                                except (OSError, ValueError, KeyError, TypeError):
                                    pass
                            saved_dark = saved and saved.get('dark')
                            attach_to_history = bool(saved_dark and saved.get('roi_saved')
                                and saved_dark['metadata']['id'] == result.dark_metadata['id']
                                and saved['settings']['roi'] == current_settings['roi']
                                and saved['settings']['controls'] == current_settings['controls'])
                            current_settings['processing'].update(buffer_size=BLANK_FRAMES, hold_peaks=False)
                            sampler = spectrum.SpectrumProcessor(config.ProcessingConfig(**current_settings['processing']))
                            sampler.set_dark_reference(result.dark_profile,result.dark_metadata,region.x)
                            halogen_job = dict(sampler=sampler, settings=current_settings, attach=attach_to_history,
                                               count=0, max_ratio=0., raw_max=0, capillary=capillary_point)
                            progress = {'status':'collecting','count':0,'total':BLANK_FRAMES}
                            if capillary_point is None:
                                self._update(halogen_capture=progress)
                            else:
                                self._update(capillary_capture={**capillary_point,**progress})
                        elif op == 'lamp_capture':
                            if command.get('confirmed') is not True:
                                raise ValueError('请确认当前入光来自皓宇荧光灯')
                            controls = hardware_controls()
                            if dark_controls is not None and controls != dark_controls:
                                invalidate_dark('相机参数已改变，暗场已暂停')
                                raise ValueError('曝光条件刚发生变化，请等新帧再采集灯谱')
                            if result is None or result.buffer_len < 32:
                                raise ValueError('请将平均帧数设为32或以上，保持灯和光路不动，等待平均完成后再冻结灯谱')
                            from .lamp_calibration import freeze_lamp
                            lamp = freeze_lamp(result,binding,region,seq)
                            lamp['controls'] = controls
                            self._update(lamp=lamp)
                        elif op in ('lamp_calibrate','peaks_save'):
                            if op == 'peaks_save' and lamp is None:
                                if cal is None:
                                    raise ValueError('请先采集灯谱并选峰，或选择既有峰记录')
                                saved_path = archive_stage('peaks', wave={'binding':binding,'calibration':cal.to_dict(),
                                                           'report':cal.verification,'lamp':saved_lamp})
                                self._update(error=None,wavelength_calibration={'saved':True,'path':str(saved_path),
                                                                               'id':cal.calibration_id})
                                continue
                            if recording_path is not None:
                                raise ValueError('请先结束连续记录再更改波长标定')
                            if lamp is None or command.get('snapshot_id')!=lamp['id']:
                                raise ValueError('灯谱快照已失效，请重新采集灯谱')
                            if hardware_controls()!=lamp['controls']:
                                raise ValueError('曝光条件已改变，请重新采集灯谱')
                            from .lamp_calibration import fit_lamp
                            candidate,report = fit_lamp(lamp,command.get('points'),command.get('check_points'),
                                degree=command.get('degree',2), manual=op=='peaks_save')
                            wave = {'binding':binding,'calibration':candidate.to_dict(),'report':report,'lamp':lamp}
                            if op == 'peaks_save':
                                saved_path = archive_stage('peaks', wave=wave)
                            else:
                                write_profile(cal_path,wave)
                                saved_path = cal_path
                            cal = candidate
                            saved_lamp = copy.deepcopy(lamp)
                            self._update(calibration_report=report,saved_lamp=saved_lamp,wavelength_calibration={'saved':True,'path':str(saved_path),'id':cal.calibration_id})
                            self.refresh_files()
                        elif op == 'calibrate':
                            points = command.get('points')
                            if type(points) is not list or not 3<=len(points)<=20:
                                raise ValueError('需要 3–20 组像素:波长标定点')
                            for point in points:
                                if (type(point) is not list or len(point)!=2 or
                                    any(type(v) not in (int,float) or not np.isfinite(v) for v in point) or
                                    not 0<=point[0]<last_frame.shape[1]):
                                    raise ValueError('标定点必须是图像范围内的像素和有限波长')
                            candidate = calibration.Calibration.fit(points, frame_width=last_frame.shape[1])
                            wavelengths = candidate.wavelengths(0,last_frame.shape[1])
                            delta = np.diff(wavelengths)
                            if not np.all(np.isfinite(wavelengths)) or not (np.all(delta>0) or np.all(delta<0)):
                                raise ValueError('拟合在图像范围内不是单调波长，请检查标定点')
                            self.data_dir.mkdir(parents=True,exist_ok=True)
                            write_profile(cal_path,{'binding':binding,'calibration':candidate.to_dict()})
                            cal = candidate
                            self._update(calibration_report=None,saved_lamp=None,wavelength_calibration={'saved':cal is not None,'path':str(cal_path),'id':cal.calibration_id if cal else None})
                            self.refresh_files()
                        elif op == 'clear_calibration':
                            cal_path.unlink(missing_ok=True)
                            cal = None
                            self._update(calibration_report=None,saved_lamp=None,wavelength_calibration={'saved':cal is not None,'path':str(cal_path),'id':cal.calibration_id if cal else None})
                            self.refresh_files()
                        elif op == 'record_start':
                            if not roi_ready or roi_finder is not None:
                                raise ValueError('请等待光谱范围识别完成后再开始记录')
                            interval = command.get('interval', 1)
                            if type(interval) not in (int,float) or not np.isfinite(interval) or not .2<=interval<=60:
                                raise ValueError('记录间隔须为 0.2–60 秒')
                            if recording_path is not None:
                                raise ValueError('已在连续记录中')
                            recording_path = self.data_dir/'captures'/('series-'+uuid.uuid4().hex+'.jsonl')
                            recording_path.parent.mkdir(parents=True,exist_ok=True)
                            recording_path.touch(exist_ok=False)
                            record_interval, record_at = interval, 0
                            with self.lock:
                                self.exports[recording_path.name] = recording_path
                                self.state['files']['jsonl'] = '/api/spectrometer-file/'+recording_path.name
                                self.state['recording'] = True
                            self.refresh_files()
                        elif op == 'record_stop':
                            recording_path = None
                            self._update(recording=False)
                            self.refresh_files()
                        elif op == 'absorbance_save':
                            from .absorbance_capture import save_absorbance
                            capture = save_absorbance(self.data_dir/'captures', record, frame_reference,
                                name=command.get('name',''), processing=vars(processing), history_id=history_key)
                            capture['files'] = {ext:'/api/spectrometer-file/'+quote(Path(path).name)
                                                for ext,path in capture['files'].items()}
                            self.refresh_files()
                            self._update(absorbance_capture=capture)
                        elif op == 'save':
                            if result is None:
                                raise ValueError('还没有可保存的光谱')
                            from spectrometer.spectro.plot import render_spectrum
                            files = recording.save_capture(self.data_dir/'captures', result, calibration=cal,
                                roi=region, source_info=device_info, exposure=exposure, seq=seq,
                                frame=last_frame, graph_image=render_spectrum(result,calibration=cal,roi=region),
                                stem='spectrum-'+uuid.uuid4().hex, extra={'photometry':photometry, 'saturation':record.get('saturation'), 'stats':record['stats']})
                            with self.lock:
                                self.exports.update({Path(v).name:Path(v) for v in files.values()})
                                self.state['files'].update({k:'/api/spectrometer-file/'+Path(v).name for k,v in files.items()})
                            self.refresh_files()
                        if op in ('exposure_auto','halogen_capture','capillary_begin','capillary_ping','capillary_collect','capillary_end') or op.startswith('history_'):
                            self._update(error=None)
                        else:
                            self._update(error=None, message='已保存单幅吸光度曲线' if op=='absorbance_save' else '已保存采集文件' if op=='save' else '设置已应用')
                    except Exception as exc:
                        if op == 'capillary_collect' and halogen_job is None and command.get('scan_id') == scan_id:
                            self._update(capillary_capture={'scan_id':scan_id,'position_mm':command.get('position_mm'),
                                         'status':'error','error':str(exc)})
                        if op == 'exposure_auto':
                            self._update(error=str(exc), message='自动曝光未完成')
                        else:
                            self._update(error=str(exc))
                frame = source.read()
                if self.stop_event.is_set():
                    break
                if frame is None:
                    raise RuntimeError('光谱仪帧读取失败，已停止采集；请检查 USB 后手动重连')
                if region is None:
                    # Preview only until a supported signal is found. Never
                    # persist a guessed central band from a completely dark frame.
                    region = roi.Roi(0,0,frame.shape[1],frame.shape[0])
                if roi_finder is not None and dark_job is None and recording_path is None:
                    candidate = roi_finder.add(frame)
                    if candidate is not None:
                        set_region(candidate, '已识别光谱范围')
                        if not auto_roi_draft:
                            persist_profile()
                        initial_save = False
                        self._update(error=None)
                    elif roi_finder.count == 0:
                        self._update(roi_selection={'ready':roi_ready,'scanning':True,
                            'message':'暂未识别到高于底噪的有效光信号，继续等待入光；不改变已保存范围'})
                if frame.shape[1] != device_info['width'] or frame.shape[0] != device_info['height']:
                    raise RuntimeError('图像分辨率已改变，请断开并重新标定')
                # Manual exposure is enforced even when no dark is active.
                if time.monotonic()>=check_controls_at or (dark_job is not None and dark_job['count']+1>=dark_job['total']):
                    current_controls = hardware_controls()
                    check_controls_at = time.monotonic()+1
                    if dark_controls is not None and current_controls != dark_controls:
                        invalidate_dark('相机参数发生变化，暗场已暂停；请恢复校准时设置')
                    if not self.demo and current_controls.get('auto_exposure') != 1:
                        source.set_exposure(exposure['value'] or 300)
                        current_controls = hardware_controls()
                        if current_controls.get('auto_exposure') != 1:
                            raise ValueError('相机未保持手动曝光，请检查设备控制')
                    exposure.update(auto=False,value=current_controls.get('exposure_time_absolute'))
                if dark_job is not None:
                    if dark_job['discard']:
                        dark_job['discard'] -= 1
                    else:
                        full = dark_job['full']
                        sampled = roi.Roi(0,0,binding['width'],binding['height']) if full else region
                        if full:
                            b,g,r = cv2.split(frame)
                            values = (r*processing.gain_r + g*processing.gain_g + b*processing.gain_b) / 3
                            saturated = saturation.evaluate(frame, self.saturation_policy)[0]
                        else:
                            sample = dark_job['sampler'].update(sampled.crop(frame),sampled.x)
                            values = sample.raw_intensity
                            saturated = saturation.evaluate(sampled.crop(frame), self.saturation_policy)[0]
                        if saturated and self.saturation_policy['block']:
                            invalidate_dark('暗场画面饱和，采集已取消；请检查遮光和曝光')
                        else:
                            values = values.astype(np.float64,copy=False)
                            if dark_job['sum'] is None:
                                dark_job['sum'] = np.zeros_like(values)
                                dark_job['sum2'] = np.zeros_like(values)
                            dark_job['sum'] += values
                            dark_job['sum2'] += values*values
                            dark_job['count'] += 1
                            count,total = dark_job['count'],dark_job['total']
                            if count == total:
                                background = dark_job['sum']/total
                                variance = np.maximum(0,dark_job['sum2']/total-background*background)
                                metadata = {'id':uuid.uuid4().hex,'created':datetime.now().astimezone().isoformat(),
                                            'frames':total,'discarded_frames':3,'binding':binding,
                                            'saturation':dict(self.saturation_policy),
                                            'roi':dict(zip(('x','y','w','h'),sampled.as_tuple())),
                                            'controls':dark_controls,'processing':copy.deepcopy(vars(processing)),
                                            'mean_counts':float(background.mean()),
                                            'max_counts':float(background.max()),
                                            'temporal_std_mean':float(np.sqrt(variance).mean())}
                                dark_job = None
                                if full:
                                    deviation = np.sqrt(variance)
                                    working_dark = {'background':background.mean(axis=0).tolist(),
                                        'metadata':metadata,'image':encode_dark(background,deviation)}
                                    history_key = None
                                    dark_pending = True
                                    dark_reset = False
                                    decoded_dark = (metadata['id'],np.asarray((background,deviation),dtype=np.float32))
                                    activate_saved_dark()
                                    self._update(active_history=None,history_message='暗场已采集，请保存后打开入光口')
                                else:
                                    try:
                                        persist_profile(dark_update=True,dark_value={'background':background.tolist(),'metadata':metadata})
                                    except Exception as exc:
                                        invalidate_dark('底噪保存失败，未启用新校准；请检查磁盘后重试')
                                        self._update(error='底噪保存失败：'+str(exc))
                                        continue
                                    processor.set_dark_reference(background,metadata,region.x)
                                    completed_dark = {'status':'ready','count':total,'total':total,
                                        'metadata':metadata,'message':'暗场已保存并启用，可打开入光口'}
                            else:
                                self._update(dark={'status':'collecting','count':count,'total':total,'message':'正在平均暗场，请保持遮光'})
                if initial_save and roi_ready:
                    initial_save=False
                    try:
                        persist_profile()
                    except Exception as exc:
                        self._update(error='采集设置保存失败：'+str(exc))
                if not self.demo and hasattr(source,'exposure_info'):
                    exposure.update(source.exposure_info())
                elif exposure['value'] is not None:
                    exposure['milliseconds'] = exposure['value'] * .1
                device_info['sample_bits'] = frame.dtype.itemsize * 8
                pixels = region.crop(frame)
                result = processor.update(pixels,region.x)
                if cal:
                    result.wavelengths = cal.wavelengths(region.x,region.w)
                seq += 1
                record = recording.make_record(result,calibration=cal,roi=region,source_info=device_info,exposure=exposure,seq=seq)
                saturation.annotate(record, pixels, self.saturation_policy)
                if halogen_job is not None:
                    job = halogen_job
                    capillary_point = job['capillary']
                    try:
                        if (job['settings']['controls'] != dark_controls or
                            (job['count']+1 == BLANK_FRAMES and hardware_controls() != dark_controls)):
                            raise ValueError('采集空白 I₀ 时相机设置发生变化，请恢复匹配的暗场后重试')
                        if saturation.blocked(record):
                            raise ValueError('采集过程中达到饱和阈值，未保存本点光谱')
                        blank = job['sampler'].update(pixels,region.x)
                        job['count'] = blank.buffer_len
                        job['max_ratio'] = max(job['max_ratio'],record['saturation']['ratio'])
                        job['raw_max'] = max(job['raw_max'],record['saturation']['raw_max'])
                        if job['count'] == BLANK_FRAMES:
                            blank.wavelengths = result.wavelengths
                            current = recording.make_record(blank,calibration=cal,roi=region,source_info=device_info,exposure=exposure,seq=seq)
                            exceeded = job['max_ratio'] >= saturation.MIN_RATIO
                            current['stats'].update(saturated=exceeded,saturated_ratio=job['max_ratio'])
                            current['saturation'] = dict(record['saturation'],exceeded=exceeded,
                                                         ratio=job['max_ratio'],raw_max=job['raw_max'])
                            if capillary_point is not None:
                                data = {'record':current,'settings':job['settings'],'binding':binding}
                                if capillary_point['kind']=='blank':
                                    data = capture_blank(current,binding,job['settings'],True)
                                self._update(capillary_capture={**capillary_point,'status':'done','count':BLANK_FRAMES,
                                             'total':BLANK_FRAMES,'data':data},error=None)
                            else:
                                candidate = capture_blank(current,binding,job['settings'],True)
                                if job['attach']:
                                    archive_stage('reference', reference=candidate)
                                    halogen = candidate
                                else:
                                    write_profile(halogen_path,candidate)
                                    standalone_halogen = candidate
                                self.refresh_files()
                                self._update(halogen_capture={'status':'ready','count':BLANK_FRAMES,'total':BLANK_FRAMES},
                                             message=f'已保存 {BLANK_FRAMES} 帧平均空白 I₀',error=None)
                            halogen_job = None
                        else:
                            progress = {'status':'collecting','count':job['count'],'total':BLANK_FRAMES}
                            if capillary_point is None:
                                self._update(halogen_capture=progress)
                            else:
                                self._update(capillary_capture={**capillary_point,**progress})
                    except Exception as exc:
                        halogen_job = None
                        if capillary_point is None:
                            self._update(halogen_capture={'status':'error','message':str(exc)},error=str(exc))
                        else:
                            self._update(capillary_capture={**capillary_point,'status':'error','error':str(exc)},error=str(exc))
                applied = {'roi':record['roi'],'processing':vars(processing),'controls':dark_controls or {}}
                frame_reference = halogen or standalone_halogen
                if (standalone_halogen is not None and halogen is not None
                    and reference_matches(standalone_halogen,record,binding,applied)
                    and (standalone_halogen['created'] > halogen['created']
                         or not reference_matches(halogen,record,binding,applied))):
                    frame_reference = standalone_halogen
                photometry = compare(frame_reference,record,binding,applied)
                record['photometry'] = photometry
                self._update(halogen={'saved':frame_reference is not None,'path':str(halogen_path),
                    'created':frame_reference['created'] if frame_reference else None,'id':frame_reference['id'] if frame_reference else None,
                    'frames':frame_reference['record']['stats']['buffer_len'] if frame_reference else None,
                    'active':photometry['active'],'message':photometry['message']})
                record['stats']['low_light'] = result.raw_max < 16
                if recording_path is not None and time.monotonic() >= record_at:
                    recording.append_jsonl(recording_path, record)
                    record_at = time.monotonic()+record_interval
                now = time.monotonic()
                refresh_preview = now >= preview_at
                if refresh_preview:
                    preview = frame.copy()
                    cv2.rectangle(preview,(region.x,region.y),(region.x+region.w-1,region.y+region.h-1),(80,220,80),2)
                    if preview.shape[1]>800:
                        preview = cv2.resize(preview,(800,round(preview.shape[0]*800/preview.shape[1])))
                    ok, encoded = cv2.imencode('.jpg',preview,[cv2.IMWRITE_JPEG_QUALITY,75])
                    preview_at = now + .125
                with self.lock:
                    if refresh_preview:
                        self.preview = encoded.tobytes() if ok else None
                    self.state.update(status='connected',connected=True,record=record,processing=copy.deepcopy(vars(processing)),frame_time=time.monotonic())
                    if auto_message is not None:
                        self.state['message'] = auto_message
                    if completed_dark is not None:
                        self.state.update(dark=completed_dark, message='暗场校正完成，后续强度将逐像素扣除底噪')
                last_frame = frame
                if not windows_capture:
                    self.stop_event.wait(.12)
        except Exception as exc:
            self._update(status='error', error=str(exc), message='光谱仪采集已停止')
        finally:
            try:
                if source:
                    source.close()
            except Exception as exc:
                self._update(status='error', error=str(exc), message='释放光谱仪时出错')
            finally:
                if guard:
                    guard.close()
            with self.lock:
                self.state['connected'] = False
                self.state['recording'] = False
                self.state['halogen_capture'] = None
                self.state['capillary_capture'] = None
                self.state['lamp'] = None
                self.state['dark'] = {'status':'none','message':'已断开；保存的校准保留，下次连接自动恢复'}
                if self.state['status'] != 'error':
                    self.state.update(status='disconnected', message='光谱仪已断开')
