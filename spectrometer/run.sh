#!/usr/bin/env bash
# Convenience launcher: create a local virtual environment on first use,
# install the two runtime dependencies, then run the application.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV=".venv"

if [ ! -x "$VENV/bin/python" ]; then
    echo "[enzyflow] creating $VENV ..."
    "$PYTHON_BIN" -m venv "$VENV"
    "$VENV/bin/python" -m pip install --upgrade pip
    "$VENV/bin/python" -m pip install -r requirements.txt
fi

# OpenCV's PyPI Qt build ships without fonts.  Point it at a system font so
# the HighGUI window does not print font warnings on every start.
CV2_FONT_DIR="$("$VENV/bin/python" -c 'import cv2, pathlib; print(pathlib.Path(cv2.__file__).parent / "qt" / "fonts")' 2>/dev/null || true)"
if [ -n "$CV2_FONT_DIR" ] && [ ! -d "$CV2_FONT_DIR" ]; then
    mkdir -p "$CV2_FONT_DIR"
    for font in /usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf /usr/share/fonts/noto/NotoSans-Regular.ttf; do
        if [ -f "$font" ]; then
            ln -sf "$font" "$CV2_FONT_DIR/$(basename "$font")"
            break
        fi
    done
fi

# OpenCV's Qt build on this machine has no Wayland plugin.  The X11/XWayland
# backend works; the plugin loader prints one harmless warning per start, so
# keep Qt's qpa logging quiet unless the user overrides it.
export QT_QPA_PLATFORM="${SPECTRO_QT_PLATFORM:-xcb}"
export QT_LOGGING_RULES="${QT_LOGGING_RULES:-qt.qpa.*=false}"

exec "$VENV/bin/python" -m spectro "$@"
