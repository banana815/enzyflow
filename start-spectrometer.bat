@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHON=.venv\Scripts\python.exe"
if exist "%PYTHON%" goto check
where py >nul 2>&1
if errorlevel 1 (
    python -m venv .venv || goto failed
) else (
    py -3 -m venv .venv || goto failed
)
:check
"%PYTHON%" -c "import cv2, numpy, pygrabber.dshow_graph" >nul 2>&1
if errorlevel 1 (
    "%PYTHON%" -m pip install -r webcontrol\requirements-spectrometer.txt || goto failed
)
"%PYTHON%" -m webcontrol.spectrometer_server %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo Enzyflow could not start. See the error above.
pause
exit /b 1
