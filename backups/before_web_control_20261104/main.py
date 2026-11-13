"""Enzyflow autostart. Press Ctrl+C during startup to enter the REPL."""
from time import sleep_ms
from enzyflow.config import BOOT_DELAY_MS
try:
    sleep_ms(BOOT_DELAY_MS)
    from enzyflow.app import run
    run()
except KeyboardInterrupt:
    pass
except Exception:
    # app emits a structured error and leaves REPL available for repair.
    pass
