#!/bin/bash
# Open this file with Apple's Terminal.app so macOS can prompt for access.
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' '请先创建项目 .venv，再安装 webcontrol/requirements-spectrometer.txt。' >&2
  exit 1
fi
exec .venv/bin/python -m webcontrol.spectrometer_server "$@"
