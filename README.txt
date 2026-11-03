Enzyflow ESP32-S3 / MicroPython 1.27

Source of truth: firmware/ (main.py plus lib/).
lib is a convenience symlink to firmware/lib.
Original local scripts: archive/lib_before_reorganize/.
Complete pre-deployment board backup: backups/before_reorganize/.
The backup manifest contains SHA256 values for all original files.

Board layout:
  /boot.py                  original minimal boot retained
  /main.py                  automatic startup, 3-second Ctrl+C window
  /lib/enzyflow/config.py    pins, gain, timing, detection thresholds
  /lib/enzyflow/acquisition.py  AS7341 hardware reads
  /lib/enzyflow/processing.py   blocked-state logic and approximate RGB
  /lib/enzyflow/protocol.py     JSON Lines output schema
  /lib/enzyflow/app.py          application entry point
  /lib/as7341*.py               spectral and LED drivers
  /lib/light_monitor.py         compatibility import
  /lib/tcs34725.py, /lib/uservo.py  preserved older drivers
  /calib.json, /valve.json      original calibration data, unchanged

Operation:
  SDA=GPIO2, SCL=GPIO4, 3.3V, GND. LED remains off for blockage detection.
  Keep sensor uncovered during initial automatic baseline calibration.
  BLOCKED: three readings <=20% of baseline.
  LIGHT: three readings >=35% of baseline after blockage.
  Output is serial text, NOT a physical GPIO output.
  Ctrl+C stops; restart using:
    from enzyflow.app import run
    run()
  LED control remains available using:
    from as7341_led import AS7341LED
    led = AS7341LED()
    led.on(4)
    led.off()
  Do not independently control the LED while running blockage detection.
  Startup in darkness produces an error and returns to the REPL.
  A known uncovered baseline may be set in config.BASELINE for repeatability.

Data:
  One JSON record per line; schema=1, source=esp32.as7341, device_id, seq,
  t_ms (wrapping device ticks, NOT UTC), event.
  Measurement: state, signal, channels (F1..F8), clear, nir, saturated,
  rgb_approx. Counts are uncalibrated ADC counts. RGB is an uncalibrated
  band-group approximation normalized to its strongest component.
  The two spectral groups are sequential, not simultaneous.
  ready record reports acquisition settings and baseline thresholds.

Future homemade USB spectrometer:
  Implemented in spectrometer/ for the current Linux computer (2026-11-03).
  Detected device: Sonix "USB 2.0 Camera: USB-ZH", 0c68:6464, /dev/video2
  (capture) and /dev/video3 (metadata), MJPG 1280x720. The integrated Bison
  camera is excluded automatically. Run "cd spectrometer && ./run.sh doctor".
  The test frame is currently almost black (max 4-16/255) at auto exposure.
  At manual exposure 5000 it shows a room/ceiling light, not a slit spectrum.
  Verify whether this UVC node is really the spectrometer head and whether
  its slit/grating is attached; do not treat the room light as a spectrum.
  Connect the spectrometer separately to the COMPUTER. ESP32 does not act as
  USB host. The PC app reads the UVC camera directly and writes CSV/JSONL;
  it does not touch the ESP32 firmware or serial port.
  Computer collector should keep separate AS7341 and spectrometer adapters.
  Preserve distinct source/device_id values. Spectrometer payload uses
  wavelength_nm and intensity arrays plus explicit units and exposure metadata.
  Timestamp each received record on the computer for approximate alignment;
  true synchronized acquisition needs a separate timing/trigger design.
  Do not open the ESP32 serial port concurrently with Thonny.
  See spectrometer/README.md for calibration, keys, data formats and limits.

Maintenance:
  Host interpreter: /home/orange/.local/opt/thonny/bin/python3.10
  tools/board_tool.py provides backup, deploy, exec using the current Thonny
  backend and /dev/ttyACM0. Run only with exactly one ESP backend connected.
  Deploy checks SHA256 and installs main.py last. Back up before deployment.
  tools/test_processing.py tests state transitions and RGB boundary cases.
  tools/validate_board.py is a one-time deployment validation/migration script.

Stepper calibration (2026-11-03):
  firmware/lib/enzyflow/motor_config.py: UART1 TX5 RX6, ID1, 115200.
  User-confirmed current 100 mm, usable 20..220 mm, lead 4 mm/revolution.
  Positive rotation decreases position; 1 cm = 900 degrees = 128000 pulses.
  checked_delta_degrees validates software bounds but does not send motion.
  Raw motor_ctrl commands bypass these bounds. Re-anchor after driver reset,
  power loss, angle clearing, or manual movement; never assume 100 mm on boot.
