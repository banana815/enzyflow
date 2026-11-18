"""Atomic local settings/dark storage for the single UVC spectrometer."""
import json
import math
from pathlib import Path

# Only known camera controls can be restored; never pass arbitrary file keys to V4L2.
CONTROL_ORDER = ('auto_exposure', 'white_balance_automatic', 'autogain',
                 'exposure_time_absolute', 'white_balance_temperature', 'gain',
                 'brightness', 'contrast', 'saturation', 'gamma', 'sharpness',
                 'hue', 'backlight_compensation', 'power_line_frequency', 'exposure_auto_priority', 'exposure_dynamic_framerate')


def read_profile(path, binding):
    path=Path(path)
    if not path.exists():
        return None
    data=json.loads(path.read_text(encoding='utf-8'),parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非有限配置值')))
    if data.get('schema')!=1:
        raise ValueError('不支持的光谱仪配置版本')
    if data.get('binding')!=binding:
        raise ValueError('保存的校准与当前设备或分辨率不匹配')
    validate_settings(data['settings'], binding)
    dark=data.get('dark')
    if dark is not None:
        m=dark['metadata']
        if m.get('binding')!=binding:
            raise ValueError('保存的暗场设备信息不匹配')
        validate_settings(m,binding)
        values=dark['background']
        if (type(values) is not list or len(values)!=m['roi']['w'] or
            any(type(v) not in (int,float) or not math.isfinite(v) for v in values) or
            type(m.get('frames')) is not int or not 8<=m['frames']<=256 or not m.get('id')):
            raise ValueError('保存的暗场数组或采样信息无效')
    return data


def validate_settings(settings,binding):
    r=settings['roi']
    if any(type(r.get(k)) is not int for k in ('x','y','w','h')):
        raise ValueError('保存的ROI无效')
    if (min(r['x'],r['y'])<0 or min(r['w'],r['h'])<1 or
        r['x']+r['w']>binding['width'] or r['y']+r['h']>binding['height']):
        raise ValueError('保存的ROI超出分辨率')
    p=settings['processing']
    if (type(p.get('buffer_size')) is not int or not 1<=p['buffer_size']<=100 or
        p.get('linearize') not in ('off','rec601','rec709','srgb') or
        any(type(p.get(k)) not in (int,float) or not math.isfinite(p[k]) or p[k]<0 for k in ('gain_r','gain_g','gain_b')) or
        type(p.get('savgol_window')) is not int or not 3<=p['savgol_window']<=41 or p['savgol_window']%2!=1 or
        type(p.get('savgol_order')) is not int or not 0<=p['savgol_order']<p['savgol_window'] or
        any(type(p.get(k)) is not bool for k in ('savgol_enabled','hold_peaks'))):
        raise ValueError('保存的处理参数无效')
    if type(settings['controls']) is not dict or any(type(v) not in (int,type(None)) for v in settings['controls'].values()):
        raise ValueError('保存的相机参数无效')


def write_profile(path,data, *, keep_previous=True):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    payload=json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)
    tmp=path.with_suffix('.tmp')
    tmp.write_text(payload, encoding='utf-8')
    if keep_previous and path.exists():
        previous=path.with_suffix('.previous.json')
        previous_tmp=previous.with_suffix('.tmp')
        previous_tmp.write_bytes(path.read_bytes())
        previous_tmp.replace(previous)
    tmp.replace(path)


def dark_matches(dark,binding,roi,processing,controls):
    if not dark:
        return False
    m=dark['metadata']
    return (m['binding']==binding and (dark.get('image') or m['roi']==roi)
            and m['controls']==controls and
            all(m['processing'][k]==processing[k] for k in ('linearize','gain_r','gain_g','gain_b')))


def restore_controls(source,desired):
    """Restore supported controls, then caller verifies all measured values."""
    source._controls_cache=None
    current=source.controls()
    for key in CONTROL_ORDER:
        value=desired.get(key)
        if value is None or current.get(key,{}).get('value')==value:
            continue
        if key=='exposure_time_absolute' and desired.get('auto_exposure')!=1:
            continue
        if key=='white_balance_temperature' and desired.get('white_balance_automatic'):
            continue
        if key=='gain' and desired.get('autogain'):
            continue
        limits=current.get(key)
        if limits is None or type(value) is not int:
            raise ValueError('当前相机不支持保存的参数：'+key)
        if ('min' in limits and value<limits['min']) or ('max' in limits and value>limits['max']):
            raise ValueError('保存的相机参数超出当前范围：'+key)
        source.set_control(key,value)
        source._controls_cache=None
        current=source.controls()
