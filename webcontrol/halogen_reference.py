"""Blank I0 storage and dark-corrected per-pixel transmission (no peak hold)."""
from datetime import datetime, timezone
import math
import uuid


def capture(record, binding, settings, confirmed):
    stats = record['stats']
    if confirmed is not True:
        raise ValueError('请确认当前为空白光路、无样品的卤素灯参考')
    if not stats['dark_corrected'] or stats['zero_reference'] or stats['saturated']:
        raise ValueError('卤素灯参考需要有效暗场、关闭零参考且不饱和')
    if stats['buffer_len'] < 32:
        raise ValueError('请设置至少32帧平均并等待采集完成')
    if max(record['intensity']) <= 1:
        raise ValueError('参考光太弱，请检查卤素灯及光路')
    return {'schema':1, 'id':uuid.uuid4().hex, 'created':datetime.now(timezone.utc).isoformat(),
            'binding':binding, 'settings':settings, 'record':record}


def compare(reference, record, binding, settings):
    if reference is None:
        return {'active':False, 'message':'尚未保存空白卤素灯参考 I₀'}
    base = reference['record']
    a, b = reference['settings'], settings
    if (reference['binding'] != binding or a['roi'] != b['roi'] or a['controls'] != b['controls'] or
        any(a['processing'][k] != b['processing'][k] for k in ('linearize','gain_r','gain_g','gain_b')) or
        base['pixel'] != record['pixel'] or not record['stats']['dark_corrected'] or
        base['dark_correction']['metadata']['id'] != (record['dark_correction']['metadata'] or {}).get('id') or
        record['stats']['zero_reference']):
        return {'active':False, 'message':'I₀已保存，当前曝光、ROI或暗场条件不匹配；请恢复参考采集条件'}
    if record['stats']['saturated']:
        return {'active':False,'message':'样品谱饱和，暂停比值计算'}
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
            'reference_min_counts':threshold, 'valid_pixels':sum(v is not None for v in ratios),
            'transmittance':ratios, 'absorbance':absorbance}
