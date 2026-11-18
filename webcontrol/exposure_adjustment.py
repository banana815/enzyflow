"""Select the longest manual exposure below the output clipping threshold."""


def adjust_exposure(source, region, minimum, maximum, stop_event, *, threshold=250):
    initial = source.get_exposure()
    read_options = getattr(source, 'exposure_options', None)
    if read_options:
        candidates = sorted({item['value'] for item in read_options()
                             if minimum <= item['value'] <= maximum})
    else:
        control = source.controls().get('exposure_time_absolute', {})
        step = max(1, control.get('step') or 1)
        start = control.get('min', minimum)
        first = start + ((minimum - start + step - 1) // step) * step
        candidates = range(first, maximum + 1, step)
    if not candidates:
        raise ValueError('没有可用的手动曝光档位')

    # Leave room below both the user-defined threshold and 8-bit clipping.
    threshold = min(255, threshold)

    def read_peak(value):
        if stop_event.is_set():
            raise RuntimeError('曝光调整已取消')
        source.set_exposure(value)
        actual = source.get_exposure()
        if actual is None:
            raise RuntimeError('无法读回实际曝光档位')
        peaks = []
        for index in range(6):
            if stop_event.is_set():
                raise RuntimeError('曝光调整已取消')
            frame = source.read()
            if frame is None:
                raise RuntimeError('自动调整时未收到相机画面')
            # Discard frames still in flight after the driver changes exposure.
            if index >= 3:
                pixels = region.crop(frame) if region else frame
                peaks.append(int(pixels.max()))
        return actual, min(peaks), max(peaks)

    try:
        # With steady lighting, bracket the clipping boundary without sweeping
        # every supported exposure (particularly costly at slow frame rates).
        low, high = 0, len(candidates) - 1
        best = None
        while low <= high:
            middle = (low + high) // 2
            _, _, peak = read_peak(candidates[middle])
            if peak < threshold:
                best = candidates[middle]
                low = middle + 1
            else:
                high = middle - 1
        if best is None:
            raise ValueError('最短曝光仍达到饱和阈值，请降低光强或检查选区内的亮点')

        actual, minimum_peak, peak = read_peak(best)
        if peak >= threshold:
            raise ValueError('复核时亮度已达到饱和阈值，请稳定光源后重试')
        if peak - minimum_peak > max(12, .2 * peak):
            raise ValueError('复核时光强波动过大，请稳定光源后重试')
        if peak < 12:
            raise ValueError('当前光信号太弱，无法接近饱和。请检查入光口和采样范围')
        return actual, peak
    except Exception:
        if initial is not None:
            source.set_exposure(initial)
        raise
