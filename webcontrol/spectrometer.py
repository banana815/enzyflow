"""Independent UVC worker. Never imports or accesses the motor transport.

Camera reads and processing run outside the motor worker and HTTP threads.
No automatic connection/reconnection; only the known spectrometer USB identity
and a freshly enumerated capture interface can be opened.
"""
from __future__ import annotations

import copy
from datetime import datetime
import fcntl
import json
from pathlib import Path
import queue
import re
import threading
import time
import uuid

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
        self.refresh_files()
        # Show saved calibration even before connecting; applying it still checks binding.
        cal_path = self.data_dir / ('demo-calibration.json' if demo else 'calibration.json')
        if cal_path.exists():
            try:
                stored = json.loads(cal_path.read_text())
                self.state.update(calibration_report=stored.get('report'), saved_lamp=stored.get('lamp'),
                    wavelength_calibration={'saved':True,'path':str(cal_path),'id':stored['calibration'].get('calibration_id')})
            except (OSError, ValueError, KeyError, TypeError):
                self.state['error'] = '保存的波长标定文件无法读取，请检查文件'

    def refresh_files(self):
        paths = sorted((self.data_dir/'captures').glob('*'), key=lambda p:p.stat().st_mtime if p.exists() else 0, reverse=True)
        paths += [self.data_dir/n for n in ('calibration.json','demo-calibration.json','halogen-reference.json','demo-halogen-reference.json')]
        with self.lock:
            self.exports = {p.name:p for p in paths if p.is_file() and not p.is_symlink() and p.suffix in ('.json','.jsonl','.csv','.png')}
            self.state['archive'] = [{'name':p.name, 'url':'/api/spectrometer-file/'+p.name, 'bytes':p.stat().st_size} for p in self.exports.values()]
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
        camera = modules()[0]
        return [dict(d.to_dict(), eligible=eligible(d)) for d in camera.enumerate_devices()]

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
                self.stop_event.clear()
                self.commands = queue.Queue(maxsize=16)
                self.preview = None
                self.state.update(status='connecting', connected=False, record=None, frame_time=None, recording=False, roi_selection=None, profile=None, lamp=None, saved_lamp=None, wavelength_calibration=None, halogen=None, calibration_report=None, dark={'status':'none','message':'正在检查已保存的校准'}, error=None, files={}, message='正在核对光谱仪身份')
                self.thread = threading.Thread(target=self._run, args=(body.get('device'),), daemon=True, name='uvc-spectrometer')
                self.thread.start()
        elif op == 'disconnect':
            self.stop_event.set()
            with self.lock:
                if self.thread and self.thread.is_alive():
                    self.state.update(status='stopping', message='正在释放光谱仪')
        elif op in {'roi', 'auto_roi', 'processing', 'zero', 'clear_zero', 'exposure', 'calibrate', 'clear_calibration', 'save', 'record_start', 'record_stop', 'dark_start', 'dark_clear', 'lamp_capture', 'lamp_calibrate', 'profile_restore', 'halogen_capture'}:
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

    def _run(self, selected):
        source = guard = None
        try:
            camera, calibration, config, roi, spectrum, recording, cv2, np = modules()
            if self.demo:
                source = camera.SyntheticSource()
                device_info = dict(source.info, name='合成光谱（演示）', vid_pid='demo', by_id='synthetic')
            else:
                device = select_camera(camera.enumerate_devices(), selected)
                # Cross-process lock distinct from the ESP32 lock.
                guard = open('/tmp/enzyflow-spectrometer.lock', 'a')
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            roi_finder = SignalRoiFinder()
            roi_ready = False
            seq = 0
            recording_path = None
            record_interval, record_at = 1.0, 0.0
            exposure = {'auto': None, 'value': None, 'units': 'V4L2 exposure_time_absolute (100 us)'}
            if not self.demo:
                controls = source.controls()
                exposure.update(value=source.get_exposure(), auto=controls.get('auto_exposure', {}).get('value') == 3)
            binding = {k: device_info.get(k) for k in ('vid_pid', 'by_id', 'name', 'width', 'height')}
            cal_path = self.data_dir / ('demo-calibration.json' if self.demo else 'calibration.json')
            if cal_path.exists():
                saved = json.loads(cal_path.read_text())
                if saved.get('binding') == binding:
                    cal = calibration.Calibration.from_dict(saved['calibration'])
                    saved_lamp = saved.get('lamp')
                    self._update(calibration_report=saved.get('report'), wavelength_calibration={'saved':True,'path':str(cal_path),'id':cal.calibration_id}, saved_lamp=saved_lamp)
            halogen_path = self.data_dir / ('demo-halogen-reference.json' if self.demo else 'halogen-reference.json')
            if halogen_path.exists():
                try:
                    candidate = json.loads(halogen_path.read_text())
                    if candidate['schema']==1 and candidate['binding']==binding:
                        halogen = candidate
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

            from .instrument_profile import read_profile, write_profile, dark_matches, restore_controls
            profile_path = self.data_dir / ('demo-instrument-profile.json' if self.demo else 'instrument-profile.json')
            profile = {'schema':1,'binding':binding,'settings':None,'dark':None}
            initial_save = not profile_path.exists()

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
                if dark_matches(profile.get('dark'),binding,
                        dict(zip(('x','y','w','h'),region.as_tuple())),vars(processing),hardware_controls()):
                    d=profile['dark']; m=d['metadata']
                    processor.set_dark_reference(d['background'],m,region.x)
                    dark_controls=copy.deepcopy(m['controls'])
                    self._update(dark={'status':'ready','count':m['frames'],'total':m['frames'],
                        'metadata':m,'message':'已恢复保存的暗场校正，无需重新遮光采集'})
                    return True
                return False

            def restore_profile(use_dark_settings=False):
                nonlocal profile, processing, processor, region, lamp, dark_job, dark_controls, roi_finder, roi_ready
                saved=read_profile(profile_path,binding)
                if saved is None:
                    raise ValueError('尚无保存的光谱仪配置，请先完成一次校准')
                settings=(saved['dark']['metadata'] if use_dark_settings and saved.get('dark') else saved['settings'])
                if not self.demo:
                    desired = dict(settings['controls'])
                    desired['auto_exposure'] = 1
                    desired['exposure_time_absolute'] = desired.get('exposure_time_absolute') or 300
                    restore_controls(source,desired)
                profile=saved
                processing=config.ProcessingConfig(**settings['processing'])
                processor=spectrum.SpectrumProcessor(processing)
                region=roi.Roi(**settings['roi'])
                roi_finder = None
                roi_ready = True
                self._update(roi_selection={'ready':True,'scanning':False,'message':'已恢复保存的光谱范围；光路不变时无需重新识别'})
                lamp=dark_job=dark_controls=None
                measured=hardware_controls()
                exposure.update(auto=measured.get('auto_exposure')==3,value=measured.get('exposure_time_absolute'))
                self._update(lamp=None,profile={'saved':True,'dark_saved':saved.get('dark') is not None,
                    'path':str(profile_path),'message':'已恢复保存的ROI、曝光及处理设置'})
                if not activate_saved_dark():
                    self._update(dark={'status':'none','message':
                        '底噪已保存，但当前参数不匹配；可点击“恢复校准时设置”' if saved.get('dark') else '尚未保存暗场，请采集一次'})

            if profile_path.exists():
                try:
                    restore_profile()
                except Exception as exc:
                    self._update(profile={'saved':True,'dark_saved':False,'path':str(profile_path),
                        'message':'保存的配置未应用：'+str(exc)},error='校准配置恢复失败：'+str(exc))

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
            if roi_finder is not None:
                self._update(roi_selection={'ready':False,'scanning':True,'message':'正在平均8帧识别全部光信号；首次请接入完整的卤素灯光谱'})
            last_frame = result = None
            photometry = None
            while not self.stop_event.is_set():
                completed_dark = None
                # Apply settings in the camera's owning thread only.
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    command = None
                if command:
                    try:
                        op = command['op']
                        if roi_finder is not None and op in ('lamp_capture','lamp_calibrate','halogen_capture','save'):
                            raise ValueError('请等待光谱范围识别完成后再采集参考或标定')
                        if dark_job is not None and op not in ('dark_clear','record_stop'):
                            raise ValueError('正在采集暗场，请保持遮光；完成或取消后再操作')
                        if op == 'dark_start':
                            if not roi_ready or roi_finder is not None:
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
                                        'total':frames,'count':0,'discard':3,'sum':None,'sum2':None}
                            check_controls_at = 0.0
                            self._update(dark={'status':'collecting','count':0,'total':frames,'message':'请保持遮光，正在丢弃3帧缓存'})
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
                                # Keep the old extraction and calibration until a
                                # supported light signal is actually found.
                                roi_finder = SignalRoiFinder()
                                self._update(roi_selection={'ready':roi_ready,'scanning':True,
                                    'message':'正在平均8帧识别全部光信号；请使用完整的卤素灯光谱'})
                            else:
                                # Compatibility for existing scripts; no manual ROI UI.
                                vals = [command.get(k) for k in ('x', 'y', 'w', 'h')]
                                if any(type(v) is not int for v in vals):
                                    raise ValueError('ROI 必须是整数像素')
                                x, y, w, h = vals
                                fh, fw = last_frame.shape[:2]
                                if min(x,y)<0 or min(w,h)<1 or x+w>fw or y+h>fh:
                                    raise ValueError('ROI 超出当前图像范围')
                                candidate = roi.Roi(*vals)
                                if candidate != region:
                                    region = candidate
                                    invalidate_dark('光谱范围已改变，暂停底噪；可恢复校准时设置')
                                    processor = spectrum.SpectrumProcessor(processing)
                                roi_finder = None
                                roi_ready = True
                                self._update(roi_selection={'ready':True,'scanning':False,'message':'已设置光谱范围'})
                                persist_profile()
                                activate_saved_dark()
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
                            persist_profile()
                            activate_saved_dark()
                        elif op == 'zero':
                            processor.set_zero_reference()
                        elif op == 'clear_zero':
                            processor.clear_zero_reference()
                        elif op == 'exposure':
                            if self.demo:
                                raise ValueError('合成光谱没有曝光控制')
                            auto = command.get('auto', False)
                            value = command.get('value')
                            if auto is not False:
                                raise ValueError('网页采集固定使用手动曝光，以便复用暗场和卤素灯参考')
                            if type(value) is not int or not 1<=value<=5000:
                                raise ValueError('曝光必须为 1–5000 的整数')
                            invalidate_dark('曝光设置已改变，请重新采集暗场')
                            source.set_exposure(value)
                            if roi_finder is not None:
                                roi_finder = SignalRoiFinder()
                            exposure.update(auto=False, value=source.get_exposure())
                            persist_profile()
                            activate_saved_dark()
                        elif op == 'halogen_capture':
                            from .halogen_reference import capture
                            from .instrument_profile import write_profile
                            current = recording.make_record(result,calibration=cal,roi=region,source_info=device_info,exposure=exposure,seq=seq)
                            current_settings = settings_now()
                            if dark_controls is None or current_settings['controls'] != dark_controls:
                                raise ValueError('相机参数变化，不能保存卤素灯参考')
                            candidate = capture(current,binding,current_settings,command.get('confirmed'))
                            write_profile(halogen_path,candidate)
                            halogen = candidate
                            self.refresh_files()
                        elif op == 'lamp_capture':
                            if command.get('confirmed') is not True:
                                raise ValueError('请确认当前入光来自皓宇荧光灯')
                            if dark_controls is None or hardware_controls()!=dark_controls:
                                invalidate_dark('相机参数已改变，请重新采集暗场')
                                raise ValueError('请先完成有效暗场校正，再冻结灯谱')
                            if result is None or result.buffer_len < 32:
                                raise ValueError('请将平均帧数设为32或以上，保持灯和光路不动，等待平均完成后再冻结灯谱')
                            from .lamp_calibration import freeze_lamp
                            lamp = freeze_lamp(result,binding,region,seq)
                            self._update(lamp=lamp)
                        elif op == 'lamp_calibrate':
                            if recording_path is not None:
                                raise ValueError('请先结束连续记录再更改波长标定')
                            if (lamp is None or command.get('snapshot_id')!=lamp['id'] or
                                result.dark_metadata is None or lamp['dark_id']!=result.dark_metadata['id']):
                                raise ValueError('灯谱快照已失效，请完成暗场后重新冻结灯谱')
                            if hardware_controls()!=dark_controls:
                                invalidate_dark('相机参数已改变，请重新采集暗场')
                                raise ValueError('暗场条件已改变，拒绝保存旧快照标定')
                            from .lamp_calibration import fit_lamp
                            candidate,report = fit_lamp(lamp,command.get('points'),command.get('check_points'),degree=command.get('degree',2))
                            self.data_dir.mkdir(parents=True,exist_ok=True)
                            write_profile(cal_path,{'binding':binding,'calibration':candidate.to_dict(),'report':report,'lamp':lamp})
                            cal = candidate
                            saved_lamp = copy.deepcopy(lamp)
                            self._update(calibration_report=report,saved_lamp=saved_lamp,wavelength_calibration={'saved':True,'path':str(cal_path),'id':cal.calibration_id})
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
                        elif op == 'save':
                            if result is None:
                                raise ValueError('还没有可保存的光谱')
                            from spectrometer.spectro.plot import render_spectrum
                            files = recording.save_capture(self.data_dir/'captures', result, calibration=cal,
                                roi=region, source_info=device_info, exposure=exposure, seq=seq,
                                frame=last_frame, graph_image=render_spectrum(result,calibration=cal,roi=region),
                                stem='spectrum-'+uuid.uuid4().hex, extra={'photometry':photometry})
                            with self.lock:
                                self.exports.update({Path(v).name:Path(v) for v in files.values()})
                                self.state['files'].update({k:'/api/spectrometer-file/'+Path(v).name for k,v in files.items()})
                            self.refresh_files()
                        self._update(error=None, message='已保存采集文件' if op=='save' else '设置已应用')
                    except Exception as exc:
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
                        changed = candidate != region
                        if changed:
                            region = candidate
                            invalidate_dark('已自动识别新的完整光谱范围；请在此范围采集一次暗场，或恢复原校准设置')
                            processor = spectrum.SpectrumProcessor(processing)
                        roi_ready = True
                        roi_finder = None
                        persist_profile()
                        initial_save = False
                        activate_saved_dark()
                        self._update(error=None,roi_selection={'ready':True,'scanning':False,
                            'message':'已自动识别并保存完整光信号范围；测量与遮光时保持固定'})
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
                        sample = dark_job['sampler'].update(region.crop(frame),region.x)
                        if sample.saturated:
                            invalidate_dark('暗场画面饱和，采集已取消；请检查遮光和曝光')
                        else:
                            values = sample.raw_intensity.astype(np.float64)
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
                                            'roi':dict(zip(('x','y','w','h'),region.as_tuple())),
                                            'controls':dark_controls,'processing':copy.deepcopy(vars(processing)),
                                            'mean_counts':float(background.mean()),
                                            'max_counts':float(background.max()),
                                            'temporal_std_mean':float(np.sqrt(variance).mean())}
                                # Commit to disk before marking calibration complete.
                                try:
                                    persist_profile(dark_update=True,dark_value={'background':background.tolist(),'metadata':metadata})
                                except Exception as exc:
                                    invalidate_dark('底噪保存失败，未启用新校准；请检查磁盘后重试')
                                    self._update(error='底噪保存失败：'+str(exc))
                                    continue
                                processor.set_dark_reference(background,metadata,region.x)
                                dark_job = None
                                completed_dark = {'status':'ready','count':total,'total':total,
                                    'metadata':metadata,'message':'暗场已保存并启用，可打开入光口；下次连接自动恢复'}
                            else:
                                self._update(dark={'status':'collecting','count':count,'total':total,'message':'正在平均暗场，请保持遮光'})
                if initial_save and roi_ready:
                    initial_save=False
                    try:
                        persist_profile()
                    except Exception as exc:
                        self._update(error='采集设置保存失败：'+str(exc))
                result = processor.update(region.crop(frame),region.x)
                if cal:
                    result.wavelengths = cal.wavelengths(region.x,region.w)
                seq += 1
                record = recording.make_record(result,calibration=cal,roi=region,source_info=device_info,exposure=exposure,seq=seq)
                from .halogen_reference import compare
                photometry = compare(halogen,record,binding,{'roi':record['roi'],'processing':vars(processing),'controls':dark_controls or {}})
                record['photometry'] = photometry
                self._update(halogen={'saved':halogen is not None,'path':str(halogen_path),
                    'created':halogen['created'] if halogen else None,'id':halogen['id'] if halogen else None,
                    'active':photometry['active'],'message':photometry['message']})
                record['stats']['low_light'] = result.raw_max < 16
                if recording_path is not None and time.monotonic() >= record_at:
                    recording.append_jsonl(recording_path, record)
                    record_at = time.monotonic()+record_interval
                preview = frame.copy()
                cv2.rectangle(preview,(region.x,region.y),(region.x+region.w-1,region.y+region.h-1),(80,220,80),2)
                if preview.shape[1]>800:
                    preview = cv2.resize(preview,(800,round(preview.shape[0]*800/preview.shape[1])))
                ok, encoded = cv2.imencode('.jpg',preview,[cv2.IMWRITE_JPEG_QUALITY,75])
                with self.lock:
                    self.preview = encoded.tobytes() if ok else None
                    self.state.update(status='connected',connected=True,record=record,processing=copy.deepcopy(vars(processing)),frame_time=time.monotonic())
                    if completed_dark is not None:
                        self.state.update(dark=completed_dark, message='暗场校正完成，后续强度将逐像素扣除底噪')
                last_frame = frame
                self.stop_event.wait(.12)
        except Exception as exc:
            self._update(status='error', error=str(exc), message='光谱仪采集已停止')
        finally:
            if source:
                source.close()
            if guard:
                guard.close()
            with self.lock:
                self.state['connected'] = False
                self.state['recording'] = False
                self.state['lamp'] = None
                self.state['dark'] = {'status':'none','message':'已断开；保存的校准保留，下次连接自动恢复'}
                if self.state['status'] != 'error':
                    self.state.update(status='disconnected', message='光谱仪已断开')
