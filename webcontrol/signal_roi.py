"""Find the envelope of coherent light, excluding noise and isolated hot pixels.

The web worker averages multiple frames, then freezes the detected rectangle.
A sample absorption change must never redefine the extraction geometry.
"""
import cv2
import numpy as np
from spectrometer.spectro.roi import Roi


def detect_signal_roi(frame):
    # Max channel retains dim blue/red bands which a grayscale luminance
    # threshold relative to the strongest green peak would discard.
    level = frame.max(axis=2) if frame.ndim == 3 else frame
    level = np.asarray(level, dtype=np.float32)
    background = float(np.percentile(level, 20))
    lower = level[level <= np.percentile(level, 60)]
    noise = float(np.median(np.abs(lower-np.median(lower)))) * 1.4826
    threshold = background + max(2.0, 5.0*noise)
    mask = (level > threshold).astype(np.uint8)
    # Require spatial support; never expand ROI for single-pixel sensor defects.
    support = cv2.boxFilter(mask, cv2.CV_32F, (3,3), normalize=False,
                            borderType=cv2.BORDER_CONSTANT)
    mask &= (support >= 5).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    parts = [s for s in stats[1:n] if s[cv2.CC_STAT_AREA] >= 8 and
             s[cv2.CC_STAT_WIDTH] >= 2 and s[cv2.CC_STAT_HEIGHT] >= 2]
    if not parts:
        return None
    height,width = level.shape
    x = max(0, min(int(s[0]) for s in parts)-2)
    y = max(0, min(int(s[1]) for s in parts)-2)
    right = min(width, max(int(s[0]+s[2]) for s in parts)+2)
    bottom = min(height, max(int(s[1]+s[3]) for s in parts)+2)
    return Roi(x,y,right-x,bottom-y)


class SignalRoiFinder:
    FRAMES = 8

    def __init__(self):
        self.total = None
        self.count = 0

    def add(self, frame):
        if self.total is None:
            self.total = frame.astype(np.float32)
        else:
            self.total += frame
        self.count += 1
        if self.count < self.FRAMES:
            return None
        found = detect_signal_roi(self.total/self.count)
        self.total = None
        self.count = 0
        return found
