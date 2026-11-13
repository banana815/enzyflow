#!/usr/bin/env python3
"""Run all firmware logic tests offline; never connects to the ESP32."""
from pathlib import Path
import sys
import unittest


def main():
    sys.dont_write_bytecode = True
    test_dir = Path(__file__).resolve().parents[1] / 'tests' / 'firmware'
    suite = unittest.defaultTestLoader.discover(str(test_dir), pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
