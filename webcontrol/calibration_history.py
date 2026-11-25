"""Named calibration sessions and full-frame dark sampling for the web console."""
import base64
from datetime import datetime
import json
from pathlib import Path
import re
import uuid
import zlib

import numpy as np

from .instrument_profile import dark_matches, read_profile, write_profile


def encode_dark(mean, deviation):
    data = np.stack((mean, deviation)).astype('<f4')
    return base64.b64encode(zlib.compress(data.tobytes(), level=1)).decode('ascii')


def decode_dark(dark, binding):
    shape = (2, binding['height'], binding['width'])
    size = 4 * int(np.prod(shape))
    decoder = zlib.decompressobj()
    raw = decoder.decompress(base64.b64decode(dark['image'], validate=True), size + 1)
    if len(raw) != size or not decoder.eof or decoder.unused_data:
        raise ValueError('暗场图像尺寸不匹配')
    data = np.frombuffer(raw, dtype='<f4').reshape(shape)
    if not np.isfinite(data).all():
        raise ValueError('暗场图像包含无效数据')
    return data


class CalibrationHistory:
    def __init__(self, data_dir, binding, demo=False, archive_dir=None):
        self.binding = binding
        data_dir = Path(data_dir)
        self.directory = data_dir / ('demo-calibrations' if demo else 'calibrations')
        self.archive_dir = Path(archive_dir) if archive_dir is not None and not demo else None
        prefix = 'demo-' if demo else ''
        self.legacy = {
            'legacy': data_dir / (prefix + 'instrument-profile.json'),
            'previous': data_dir / (prefix + 'instrument-profile.previous.json'),
        }
        self.legacy_wave = data_dir / (prefix + 'calibration.json')
        self.legacy_reference = data_dir / (prefix + 'halogen-reference.json')

    def source_path(self, path):
        if self.archive_dir is not None and not path.exists():
            return self.archive_dir / path.relative_to(self.directory.parent)
        return path

    def read(self, key):
        if not isinstance(key, str):
            raise ValueError('请先选择历史记录')
        if key in self.legacy:
            path = self.legacy[key]
        elif re.fullmatch(r'cal-\d{8}-\d{6}-[0-9a-f]{8}', key):
            path = self.directory / (key + '.json')
        else:
            raise ValueError('无效的校正记录名称')
        data = read_profile(self.source_path(path), self.binding)
        if data is None:
            raise ValueError('找不到所选校正记录')
        if key in self.legacy:
            data['roi_saved'] = True
            if key == 'legacy':
                data['wavelength'] = self._legacy_read(self.legacy_wave)
                data['reference'] = self._legacy_read(self.legacy_reference)
        return data

    def _legacy_read(self, path):
        try:
            data = json.loads(self.source_path(path).read_text(encoding='utf-8'))
            if data.get('binding') == self.binding:
                return data
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def scan(self):
        entries = []
        keys = {path.stem for path in self.directory.glob('cal-*.json')}
        if self.archive_dir is not None:
            keys.update(path.stem for path in (self.archive_dir / self.directory.name).glob('cal-*.json'))
        for key in [*sorted(keys, reverse=True), *self.legacy]:
            try:
                data = self.read(key)
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                continue
            settings = data['settings']
            compatible = dark_matches(data.get('dark'), self.binding, settings['roi'],
                                      settings['processing'], settings['controls'])
            entries.append({
                'dark_compatible': compatible,
                'id': key, 'name': self.legacy[key].name if key in self.legacy else key + '.json',
                'dark': bool(data.get('dark')), 'roi': bool(data.get('roi_saved')),
                'peaks': bool(data.get('wavelength')), 'reference': bool(data.get('reference')),
            })
        return entries

    def save(self, data, key=None):
        if key is None or key in self.legacy:
            key = 'cal-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        path = self.directory / (key + '.json')
        write_profile(path, data, keep_previous=False)
        return key, path.name
