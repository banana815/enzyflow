#!/usr/bin/env bash
set -euo pipefail
enzyflow_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for enzyflow_python in "$enzyflow_root/.venv/bin/python" "$enzyflow_root/spectrometer/.venv/bin/python" python3; do
  if "$enzyflow_python" -c 'import numpy, cv2' >/dev/null 2>&1; then
    exec "$enzyflow_python" "$enzyflow_root/tools/launch_webcontrol.py" "$@" --service spectrometer
  fi
done
printf '%s\n' '请先创建项目 .venv，并安装 webcontrol/requirements-spectrometer.txt 中的依赖。' >&2
exit 1
