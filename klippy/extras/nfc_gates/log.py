# klippy/extras/nfc_gates/log.py
#
# Dedicated logger for all NFC gate modules.
#
# All nfc_gate / nfc_gates output goes to nfc_reader.log (same directory as
# klippy.log) instead of polluting klippy.log.  propagate=False ensures
# messages are NOT forwarded to the root logger.
#
# Usage (from any module in this package):
#   from .log import logger           # inside nfc_gates/
#   from nfc_gates.log import logger  # from nfc_gate.py (top-level extra)

import logging
import os

_LOGGER_NAME = 'nfc_gate'
_LOG_FILENAME = 'nfc_reader.log'


def _find_klipper_log_dir():
    """
    Return the directory that klippy.log lives in by inspecting the root
    logger's FileHandler(s).  Falls back to ~/printer_data/logs if none found.
    """
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            return os.path.dirname(os.path.abspath(handler.baseFilename))
    return os.path.expanduser('~/printer_data/logs')


def _build_logger():
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger  # Already configured (e.g. reloaded config)

    log_path = os.path.join(_find_klipper_log_dir(), _LOG_FILENAME)

    fh = logging.FileHandler(log_path)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'))

    logger.addHandler(fh)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # Do not forward to klippy.log / root logger

    return logger


def configure(path):
    """
    Redirect the NFC logger to *path*.

    Called from NfcGateManager.__init__ after reading log_file from config.
    Replaces the existing FileHandler so the configured path takes effect
    even though the logger was created at import time.
    Expands ~ automatically.  If *path* is a bare filename (no directory
    component), it is placed in the same directory as klippy.log.
    """
    expanded = os.path.expanduser(path)
    if not os.path.dirname(expanded):
        expanded = os.path.join(_find_klipper_log_dir(), expanded)
    _lg = logging.getLogger(_LOGGER_NAME)
    for h in _lg.handlers[:]:
        _lg.removeHandler(h)
        h.close()
    _lg.propagate = False
    fh = logging.FileHandler(expanded)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'))
    _lg.addHandler(fh)


# Module-level singleton — imported by every nfc_gate* module.
logger = _build_logger()
