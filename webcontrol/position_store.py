"""Durable last confirmed positions; an interrupted move never restores stale coordinates."""
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read(path):
    return validate(json.loads(Path(path).read_text()))


def validate(value):
    if value.get('schema') != 1 or type(value.get('reliable')) is not bool or value.get('long_id') not in (1, 2, 3):
        raise ValueError('位置记录格式无效')
    axes = value['axes']
    if not axes or len({a['id'] for a in axes}) != len(axes):
        raise ValueError('位置记录机号重复或为空')
    for a in axes:
        maximum = 320 if a['id'] == value['long_id'] else 220
        if (type(a['id']) is not int or a['id'] not in (1, 2, 3) or
            type(a['position_mm']) not in (int, float) or not math.isfinite(a['position_mm']) or
            not 20 <= a['position_mm'] <= maximum or
            a['lead_mm'] not in (2, 4) or type(a['lead_mm']) is bool or
            type(a['forward_sign']) is not int or a['forward_sign'] not in (-1, 1)):
            raise ValueError('位置记录超出机械范围或方向导程无效')
    geometry = value.get('geometry', {})
    if not isinstance(geometry, dict) or any(
        ident not in ('1', '2', '3') or not isinstance(g, dict) or
        type(g.get('forward_sign')) is not int or g['forward_sign'] not in (-1, 1) or
        g.get('lead_mm') not in (2, 4)
        for ident, g in geometry.items()
    ):
        raise ValueError('方向或导程记录无效')
    return value


def checkpoint(axes, long_id, binding):
    return {'schema': 1, 'saved_at': datetime.now(timezone.utc).isoformat(),
            'reliable': True, 'reason': '编码器反馈确认停止', 'binding': binding,
            'long_id': long_id, 'axes': [dict(a) for a in axes if a.get('anchored')]}
