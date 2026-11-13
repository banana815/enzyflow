"""Haoyu lamp calibration using user-confirmed measured pixel correspondences.

The supplied reference is an integer-labelled PNG, not a traceable spectral
standard or a pixel mapping. Never assign its image coordinates to a camera.
"""
from datetime import datetime
import hashlib
from pathlib import Path
import uuid
import numpy as np
from spectrometer.spectro.calibration import Calibration
from spectrometer.spectro.peaks import find_peaks

REFERENCE = 'Archive/皓宇荧光灯.png'
ANCHORS = (404, 436, 546, 610)


def freeze_lamp(result, binding, roi, seq):
    if result is None or result.dark_profile is None:
        raise ValueError('请先完成第一步暗场底噪校正')
    if result.zero_active:
        raise ValueError('请清除零参考后采集灯谱；灯谱只扣暗场，不扣样品参比')
    if result.saturated:
        raise ValueError('当前灯谱饱和，请降低曝光并重采暗场后再标定')
    values = np.asarray(result.intensity, dtype=float)
    if not np.all(np.isfinite(values)) or np.max(values)-np.min(values)<3 or np.max(values)<3:
        raise ValueError('当前信号过弱，无法可靠选取荧光灯谱线')
    indexes = find_peaks(values,threshold=.10,min_distance=8)
    indexes = [int(i) for i in indexes if 0<int(i)<len(values)-1 and values[i]>0]
    if len(indexes)<3:
        raise ValueError('当前灯谱不足三个可辨识峰，请检查光路、ROI及曝光')
    source = Path(__file__).resolve().parents[1]/REFERENCE
    return {'id':uuid.uuid4().hex,'created':datetime.now().astimezone().isoformat(),
            'source':REFERENCE,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'reference_precision':'图中整数nm标注；不是可溯源高精度标准',
            'binding':binding,'roi':dict(zip(('x','y','w','h'),roi.as_tuple())),
            'dark_id':result.dark_metadata['id'],'seq':seq,
            'pixels':result.pixels.tolist(),'intensity':values.tolist(),
            'peaks':[{'pixel':int(result.pixels[i]),'intensity':float(values[i])} for i in indexes]}


def fit_lamp(snapshot, points, check_points=None, degree=2):
    """Quadratic fit, with optional independently withheld reference checks."""
    if not isinstance(snapshot,dict):
        raise ValueError('请先冻结本次皓宇灯谱')
    if type(degree) is not int or degree not in (1,2):
        raise ValueError('拟合阶数只能为1或2')
    check_points = [] if check_points is None else check_points
    if type(points) is not list or not 3<=len(points)<=4 or type(check_points) is not list or len(check_points)>1:
        raise ValueError('请选择3–4个拟合峰，可另留1个峰验证')
    all_points=points+check_points
    cleaned=[]
    for point in all_points:
        if (type(point) is not list or len(point)!=2 or
            any(type(v) not in (int,float) or not np.isfinite(v) for v in point)):
            raise ValueError('峰位置和波长必须是有限数值')
        pixel, wavelength=point
        if wavelength not in ANCHORS or not snapshot['pixels'][0]<=pixel<=snapshot['pixels'][-1]:
            raise ValueError('仅允许参考图标注的404/436/546/610nm及当前ROI内像素')
        pixel=int(round(pixel))
        if min(abs(pixel-p['pixel']) for p in snapshot['peaks'])>5:
            raise ValueError('所选像素不在冻结灯谱的候选峰附近，请重新选峰')
        cleaned.append([pixel,float(wavelength)])
    if len({p[0] for p in cleaned})!=len(cleaned) or len({p[1] for p in cleaned})!=len(cleaned):
        raise ValueError('同一峰或波长不能重复用于拟合/验证')
    pairs=sorted(cleaned[:len(points)])
    diffs=np.diff([p[1] for p in pairs])
    if not (np.all(diffs>0) or np.all(diffs<0)):
        raise ValueError('谱线对应顺序不一致，请检查436/546/610nm是否选错')
    note=('皓宇荧光灯；参考图整数标注；独立实测像素配对；source='+snapshot['source']+
          '; sha256='+snapshot['source_sha256']+'; dark_id='+snapshot['dark_id'])
    candidate=Calibration.fit(pairs,degree=degree,frame_width=snapshot['binding']['width'],note=note)
    axis=candidate.wavelengths(0,snapshot['binding']['width'])
    delta=np.diff(axis)
    if not np.all(np.isfinite(axis)) or not (np.all(delta>0) or np.all(delta<0)):
        raise ValueError('二次拟合在全幅外推时折返；请核对峰对应，或选择线性拟合并检查各峰残差。缩小ROI不会改变全幅检查范围。')
    def residuals(values):
        return [{'pixel':p,'reference_nm':w,'fitted_nm':candidate.wavelength_at(p),
                 'error_nm':candidate.wavelength_at(p)-w} for p,w in values]
    rows=residuals(pairs)
    checks=residuals(cleaned[len(points):])
    report={'snapshot_id':snapshot['id'],'source':snapshot['source'],'source_sha256':snapshot['source_sha256'],
            'reference_precision':snapshot['reference_precision'],'dark_id':snapshot['dark_id'],
            'captured':snapshot['created'],'roi':snapshot['roi'],'points':rows,'checks':checks,
            'rms_nm':float(np.sqrt(np.mean([r['error_nm']**2 for r in rows]))),
            'pixel_range':[pairs[0][0],pairs[-1][0]],'degree':degree,
            'warning':('线性拟合不会折返，但残差较大时仍需核对峰对应或改进光路；' if degree==1 else '三点二次拟合可恰好穿过三个点，R²=1不代表准确；')+'范围外为外推，整数参考图不能支持亚纳米精度。'}
    candidate.verification = report
    return candidate,report
