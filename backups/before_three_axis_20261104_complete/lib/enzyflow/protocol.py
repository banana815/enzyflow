"""JSON Lines schema v1; one record per line for the computer collector.

source: esp32.as7341; device_id: ESP32 unique ID; seq: record sequence.
t_ms: board ticks_ms (wraps; not UTC). Computer must timestamp receipt for
cross-device alignment. channels contains F1..F8 raw counts; clear/nir are
raw counts. rgb_approx is uncalibrated, peak-normalized display RGB.
Future USB spectrometer records should use their own source/device_id and
wavelength_nm/intensity arrays with explicit units, never label them F1..F8.
"""
import json
from time import ticks_ms
from machine import unique_id
from binascii import hexlify


class Reporter:
    def __init__(self):
        self.device_id = hexlify(unique_id()).decode()
        self.seq = 0

    def emit(self, event, **fields):
        record = {'schema': 1, 'source': 'esp32.as7341', 'device_id': self.device_id,
                  'seq': self.seq, 't_ms': ticks_ms(), 'event': event}
        record.update(fields)
        print(json.dumps(record))
        self.seq += 1
        return record
