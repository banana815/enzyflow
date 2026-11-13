"""Pure signal processing; RGB is a band-group display approximation."""
def estimate_rgb(channels):
    # Band-group proxy, not a calibrated CIE/sRGB conversion.
    f1, f2, f3, f4, f5, f6, f7, f8 = channels
    r = (f6 + f7 + f8) / 3
    g = (f4 + f5) / 2
    b = (f1 + f2 + f3) / 3
    peak = max(r, g, b)
    if peak <= 0:
        return (0, 0, 0)
    return tuple(int(255 * (v / peak) ** (1 / 2.2) + 0.5) for v in (r, g, b))


class BlockDetector:
    def __init__(self, baseline, low_ratio=0.20, high_ratio=0.35, confirmations=3):
        self.confirmations = confirmations
        self.low = max(2, baseline * low_ratio)
        self.high = max(self.low + 2, baseline * high_ratio)
        self.blocked = False
        self.votes = 0

    def update(self, clear):
        changed = clear >= self.high if self.blocked else clear <= self.low
        self.votes = self.votes + 1 if changed else 0
        if self.votes >= self.confirmations:
            self.blocked = not self.blocked
            self.votes = 0
        return self.blocked

