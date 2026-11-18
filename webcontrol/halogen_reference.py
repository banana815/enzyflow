"""Blank I0 storage and dark-corrected per-pixel transmission (no peak hold)."""
from datetime import datetime, timezone
import math
import uuid
from .saturation import blocked


BLANK_FRAMES = 32


def capture(record, binding, settings, confirmed):
    stats = record['stats']
    if confirmed is not True:
        raise ValueError('请确认当前为空白光路、无样品的卤素灯参考')
    if not stats['dark_corrected']:
        raise ValueError('当前采样范围和曝光需要匹配的暗场。')
    if stats['zero_reference']:
        raise ValueError('请先清除样品零参考，再保存 I₀。')
    if blocked(record):
        raise ValueError('卤素灯光谱达到设定的饱和阈值。可修改阈值、关闭饱和拦截，或降低曝光后重采暗场。')
    if stats['buffer_len'] < BLANK_FRAMES:
        raise ValueError(f'空白参考至少需要 {BLANK_FRAMES} 帧')
    if max(record['intensity']) <= 1:
        raise ValueError('参考光太弱。请打开入光口，对准卤素灯并移走样品。')
    return {'schema':1, 'id':uuid.uuid4().hex, 'created':datetime.now(timezone.utc).isoformat(),
            'binding':binding, 'settings':settings, 'record':record}


def matches(reference, record, binding, settings):
    if reference is None or not record['stats']['dark_corrected'] or record['stats']['zero_reference']:
        return False
    base = reference['record']
    a, b = reference['settings'], settings
    return (reference['binding'] == binding and a['roi'] == b['roi'] and a['controls'] == b['controls']
            and all(a['processing'][k] == b['processing'][k] for k in ('linearize','gain_r','gain_g','gain_b'))
            and base['pixel'] == record['pixel']
            and base['dark_correction']['metadata']['id'] == (record['dark_correction']['metadata'] or {}).get('id'))


def compare(reference, record, binding, settings):
    if reference is None:
        return {'active':False, 'message':'尚未保存空白卤素灯参考 I₀'}
    if not matches(reference, record, binding, settings):
        return {'active':False, 'message':'I₀已保存，当前曝光、ROI或暗场条件不匹配；请恢复参考采集条件'}
    if blocked(record):
        return {'active':False,'message':'样品谱达到设定的饱和阈值，已按设置暂停比值计算'}
    base = reference['record']
    noise = base['dark_correction']['metadata'].get('temporal_std_mean', 0)
    threshold = max(1.0, 3*noise/math.sqrt(base['stats']['buffer_len']))
    ratios, absorbance = [], []
    for i0, value in zip(base['intensity'], record['intensity']):
        valid = math.isfinite(i0) and math.isfinite(value) and i0 > threshold and value > 0
        t = value/i0 if valid else None
        ratios.append(t)
        absorbance.append(-math.log10(t) if valid else None)
    return {'active':True, 'reference_id':reference['id'], 'reference_created':reference['created'],
            'message':'透过率 I/I₀；吸光度 −log₁₀(I/I₀)。弱参考或非正样品值留空，不裁剪比值。',
            'saturation_warning':bool(record['stats']['saturated'] or base['stats']['saturated']),
            'reference_min_counts':threshold, 'valid_pixels':sum(v is not None for v in ratios),
            'transmittance':ratios, 'absorbance':absorbance}
