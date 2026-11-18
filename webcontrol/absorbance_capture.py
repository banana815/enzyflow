"""Named, reproducible single-spectrum absorbance exports."""
from __future__ import annotations

import copy
import csv
from datetime import datetime
import io
import json
import math
from pathlib import Path
import re


def finite(value):
    return isinstance(value, (float, int)) and math.isfinite(value)


def capture_name(value):
    if not isinstance(value, str):
        raise ValueError('曲线名称必须为文字')
    name = value.strip()
    if not name:
        return 'absorbance-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]
    if (len(name) > 80 or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or name.endswith('.') or name in ('.', '..')
            or re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])', name.split('.')[0])):
        raise ValueError('名称最多 80 字，不能使用 Windows 保留名称或字符 <>:"/\\|?*，也不能以句点结尾')
    return name


def render_absorbance(record):
    import cv2
    import numpy as np
    image = np.full((480, 1200, 3), (20, 20, 24), np.uint8)
    values = record['photometry']['absorbance']
    wavelengths = record.get('wavelength_nm')
    xs = wavelengths if wavelengths is not None else record['pixel']
    pairs = sorted(zip(xs, values), key=lambda pair: pair[0])
    xmin, xmax = min(xs), max(xs)
    ys = [v for v in values if finite(v)]
    ymin, ymax = min(0, min(ys)), max(0, max(ys))
    pad = max(.05, (ymax-ymin)*.08)
    ymin, ymax = ymin-pad, ymax+pad
    def text(label, x, y, scale=.45):
        cv2.putText(image, label, (x,y), cv2.FONT_HERSHEY_SIMPLEX, scale, (220,220,225), 1, cv2.LINE_AA)
    def point(x,y):
        return (80+round((x-xmin)/max(1e-9,xmax-xmin)*1090), 410-round((y-ymin)/(ymax-ymin)*355))
    text('Absorbance (dimensionless) = -log10(I / I0)', 80, 25, .65)
    for j in range(6):
        f=j/5
        y=410-round(f*355)
        cv2.line(image,(80,y),(1170,y),(60,60,65),1)
        text(f'{ymin+f*(ymax-ymin):.3f}',5,y+5)
        x=80+round(f*1090)
        cv2.line(image,(x,55),(x,410),(60,60,65),1)
        text(f'{xmin+f*(xmax-xmin):.1f}',x-25,432)
    previous=None
    for x,y in pairs:
        if not finite(y):
            previous=None
            continue
        current=point(x,y)
        if previous is not None:
            cv2.line(image,previous,current,(130,220,130),2,cv2.LINE_AA)
        else:
            cv2.circle(image,current,2,(130,220,130),-1)
        previous=current
    text('Wavelength (nm)' if wavelengths is not None else 'Pixel (uncalibrated)',950,464)
    text(str(record.get('t_wall','')) + '  |  gaps = invalid data',80,464)
    ok, encoded = cv2.imencode('.png', image)
    if not ok:
        raise OSError('吸光度曲线图编码失败')
    return encoded.tobytes()


def save_absorbance(directory, record, reference, *, name='', processing=None, history_id=None):
    """Export one coherent processed frame; never overwrite an existing capture."""
    base = capture_name(name)
    record = copy.deepcopy(record)
    photo = (record or {}).get('photometry') or {}
    if not photo.get('active') or not reference or photo.get('reference_id') != reference.get('id'):
        raise ValueError('请先恢复匹配的暗场与空白 I₀，并等待有效吸光度曲线')
    pixels = record['pixel']
    absorbance = photo['absorbance']
    if not any(finite(v) for v in absorbance):
        raise ValueError('没有有效吸光度数据；请检查光源、样品和空白 I₀')
    reference_counts = reference['record']['intensity']
    wavelengths = record.get('wavelength_nm')
    columns = [absorbance, photo['transmittance'], record['intensity'], reference_counts]
    if wavelengths is not None:
        columns.append(wavelengths)
    if any(len(c) != len(pixels) for c in columns):
        raise ValueError('吸光度与参考的数据长度不一致')
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(['pixel','wavelength_nm','absorbance','transmittance','sample_counts','reference_counts','valid'])
    for i,pixel in enumerate(pixels):
        valid=finite(absorbance[i])
        writer.writerow([pixel, wavelengths[i] if wavelengths is not None else '',
                         absorbance[i] if valid else '', photo['transmittance'][i] if valid else '',
                         record['intensity'][i],reference_counts[i],int(valid)])
    payload = dict(schema=1, kind='absorbance_capture', name=base,
                   saved_at=datetime.now().astimezone().isoformat(), record=record,
                   reference=copy.deepcopy(reference), processing=processing, history_id=history_id,
                   units={'absorbance':'dimensionless','transmittance':'dimensionless','intensity':'counts'})
    png = render_absorbance(record)
    directory = Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    suffix=0
    while True:
        stem=base if suffix==0 else f'{base}-{suffix:02d}'
        paths={ext:directory/(stem+'.'+ext) for ext in ('csv','json','png')}
        if any(p.exists() for p in paths.values()):
            suffix+=1
            continue
        payload['name']=stem
        contents={'csv':buffer.getvalue().encode('utf-8-sig'),
                  'json':json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8'),
                  'png':png}
        created=[]
        try:
            for ext,path in paths.items():
                with path.open('xb') as handle:
                    created.append(path)
                    handle.write(contents[ext])
        except BaseException as exc:
            for path in created:
                path.unlink(missing_ok=True)
            if isinstance(exc,FileExistsError):
                suffix+=1
                continue
            raise
        return {'name':stem,'files':{ext:str(p) for ext,p in paths.items()},
                'captured_at':record.get('t_wall'),'seq':record.get('seq'),
                'valid_pixels':sum(finite(v) for v in absorbance)}
