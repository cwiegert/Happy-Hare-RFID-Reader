# tests/python/conftest.py
#
# Test setup for klippy/extras/nfc_gates unit tests.
#
# These modules are Klipper "extras" — normally imported by Klipper's own
# module loader, with a real printer/reactor/gcode object graph behind them.
# Outside Klipper we provide two things instead:
#
# 1. sys.path so `import nfc_gates...` resolves to klippy/extras/nfc_gates
#    the same way Klipper's loader would see it.
# 2. A stub logging.FileHandler on the root logger, attached *before* any
#    nfc_gates module is imported. nfc_gates/log.py builds a module-level
#    logger at import time by locating klippy.log's directory via the root
#    logger's FileHandler and falling back to ~/printer_data/logs if none is
#    found — which doesn't exist off-printer and would crash the import.
#    Pointing the root logger at a throwaway temp dir first makes
#    nfc_gates.log resolve its own log file into that same temp dir instead.
import logging
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTRAS_DIR = REPO_ROOT / "klippy" / "extras"
sys.path.insert(0, str(EXTRAS_DIR))

_stub_log_dir = tempfile.mkdtemp(prefix="nfc_gates_test_logs_")
logging.getLogger().addHandler(
    logging.FileHandler(os.path.join(_stub_log_dir, "klippy.log"))
)
