"""Continuous acquisition. Ctrl+C stops safely; run() restarts calibration."""
from time import sleep_ms
from enzyflow import config, __version__
from enzyflow.acquisition import AS7341Source
from enzyflow.processing import BlockDetector, estimate_rgb
from enzyflow.protocol import Reporter


def run(samples=None):
    report = Reporter()
    source = None
    try:
        report.emit('starting', version=__version__, led='off', rgb_method='band_proxy_uncalibrated')
        source = AS7341Source()
        baseline = config.BASELINE
        if baseline is None:
            report.emit('calibrating', instruction='Keep sensor uncovered')
            values = []
            for _ in range(config.CALIBRATION_SAMPLES):
                frame = source.read()
                if frame['saturated']:
                    raise ValueError('Calibration saturated; reduce illumination or gain')
                values.append(frame['clear'])
                sleep_ms(config.INTERVAL_MS)
            baseline = sorted(values)[len(values) // 2]
        if baseline < config.MIN_BASELINE:
            raise ValueError('Calibration too dark; uncover sensor and restart')
        detector = BlockDetector(baseline, config.BLOCK_RATIO, config.RESTORE_RATIO, config.CONFIRM_SAMPLES)
        report.emit('ready', baseline=baseline, blocked_below=detector.low,
                    light_above=detector.high, gain=config.GAIN, atime=config.ATIME,
                    astep=config.ASTEP, units='raw_counts', sequential_groups=True)
        count = 0
        while samples is None or count < samples:
            frame = source.read()
            blocked = detector.update(frame['clear'])
            state = 'SATURATED' if frame['saturated'] else ('BLOCKED' if blocked else 'LIGHT')
            rgb = estimate_rgb(frame['channels']) if state == 'LIGHT' else None
            report.emit('measurement', state=state, signal=1 if blocked else 0,
                        rgb_approx=rgb, **frame)
            count += 1
            sleep_ms(config.INTERVAL_MS)
    except KeyboardInterrupt:
        report.emit('stopped')
    except Exception as exc:
        report.emit('error', message=str(exc))
        raise
    finally:
        if source is not None:
            source.close()
