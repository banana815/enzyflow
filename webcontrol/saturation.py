"""Configurable UVC saturation warning and optional acquisition gate."""
import json

DEFAULT = {'block': False, 'threshold': 250}
MIN_RATIO = .002


def validate(value):
    if not isinstance(value, dict) or type(value.get('block')) is not bool:
        raise ValueError('饱和拦截开关必须为布尔值')
    threshold = value.get('threshold')
    if type(threshold) is not int or not 1 <= threshold <= 255:
        raise ValueError('饱和阈值必须是 1–255 的整数（原始 RGB 通道计数）')
    return {'block': value['block'], 'threshold': threshold}


def load(path):
    if not path.exists():
        return dict(DEFAULT)
    return validate(json.loads(path.read_text(encoding='utf-8')))


def save(path, value):
    policy = validate(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(policy), encoding='utf-8')
    temporary.replace(path)
    return policy


def evaluate(image, policy):
    import numpy as np
    ratio = float(np.count_nonzero(image >= policy['threshold']) / max(1, image.size))
    return ratio >= MIN_RATIO, ratio


def annotate(record, image, policy):
    saturated, ratio = evaluate(image, policy)
    record['stats'].update(saturated=saturated, saturated_ratio=ratio)
    record['saturation'] = dict(policy, min_ratio=MIN_RATIO, ratio=ratio,
                                exceeded=saturated, raw_max=int(image.max()))
    return record


def blocked(record, policy=None):
    policy = policy or record.get('saturation') or {'block': True}
    return policy['block'] and record['stats']['saturated']
