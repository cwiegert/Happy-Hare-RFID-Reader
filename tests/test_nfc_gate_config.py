"""
tests/test_nfc_gate_config.py
==============================
Unit tests for NFCGateDefaults — the [nfc_gate] base section handler.
"""

import sys
import os
import types

_EXTRAS = os.path.join(os.path.dirname(__file__), '..', 'klippy', 'extras')
sys.path.insert(0, _EXTRAS)

def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

class _NullLogger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass

_stub('extras')
_stub('extras.bus')
_stub('bus',
      MCU_I2C_from_config=lambda *a, **k: None,
      MCU_SPI_from_config=lambda *a, **k: None,
      MCU_I2C=object,
      MCU_SPI=object)

_nfc_pkg = _stub('nfc_gates')
_nfc_pkg.__path__    = [os.path.join(_EXTRAS, 'nfc_gates')]
_nfc_pkg.__package__ = 'nfc_gates'

_null = _NullLogger()
_stub('nfc_gates.log',
      logger=_null, configure=lambda *a, **k: None,
      info=lambda *a, **k: None,
      warning=lambda *a, **k: None,
      error=lambda *a, **k: None)
_stub('nfc_gates.pn532_driver',
      PN532Driver=object,
      PN532_COMMAND_GETFIRMWAREVERSION=0x02,
      PN532_COMMAND_SAMCONFIGURATION=0x14,
      PN532_COMMAND_INLISTPASSIVETARGET=0x4A)
_stub('nfc_gates.rc522_driver',   RC522Driver=object)
_stub('nfc_gates.spoolman_client', SpoolmanClient=object)

from nfc_gates.NFC_manager import NFCGateDefaults


class _MockGCode:
    def register_command(self, *a, **k): pass


class _MockPrinter:
    def lookup_object(self, name, default=None):
        return _MockGCode()
    def get_reactor(self):
        return None


class MockConfig:
    def __init__(self, values=None, name='nfc_gate'):
        self._values  = dict(values or {})
        self._name    = name
        self._printer = _MockPrinter()

    def get_name(self):
        return self._name

    def get_printer(self):
        return self._printer

    def error(self, msg):
        return ValueError(msg)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def getboolean(self, key, default=None):
        raw = self._values.get(key, default)
        if isinstance(raw, bool):
            return raw
        if raw is None:
            return default
        return str(raw).strip().lower() in ('true', '1', 'yes')

    def getfloat(self, key, default=None, minval=None, maxval=None):
        raw = self._values.get(key, default)
        val = float(raw) if raw is not None else default
        if val is not None:
            if minval is not None and val < minval:
                raise ValueError(f"{key}={val} below minval={minval}")
            if maxval is not None and val > maxval:
                raise ValueError(f"{key}={val} above maxval={maxval}")
        return val

    def getint(self, key, default=None, minval=None, maxval=None):
        raw = self._values.get(key, default)
        val = int(raw) if raw is not None else default
        if val is not None:
            if minval is not None and val < minval:
                raise ValueError(f"{key}={val} below minval={minval}")
            if maxval is not None and val > maxval:
                raise ValueError(f"{key}={val} above maxval={maxval}")
        return val


def test_defaults_built_in_values():
    d = NFCGateDefaults(MockConfig())
    assert d.spoolman_url       == ''
    assert d.spoolman_rfid_key  == 'rfid'
    assert d.spoolman_timeout   == 5.0
    assert d.spoolman_cache_ttl == 300.0
    assert d.startup_polling    == -1
    assert d.startup_poll_delay == 0.0
    assert d.poll_interval      == 30.0
    assert d.absent_threshold   == 3
    assert d.transceive_delay   == 0.250
    assert d.crc_delay          == 0.050
    assert d.debug              == 2

def test_defaults_all_keys_overridden():
    d = NFCGateDefaults(MockConfig({
        'spoolman_url':       'http://192.168.1.50:7912',
        'spoolman_rfid_key':  'nfc_uid',
        'spoolman_timeout':   10.0,
        'spoolman_cache_ttl': 600.0,
        'startup_polling':    1,
        'startup_poll_delay': 2.5,
        'poll_interval':      60.0,
        'absent_threshold':   5,
        'transceive_delay':   0.5,
        'crc_delay':          0.1,
        'debug':              2,
    }))
    assert d.spoolman_url       == 'http://192.168.1.50:7912'
    assert d.spoolman_rfid_key  == 'nfc_uid'
    assert d.spoolman_timeout   == 10.0
    assert d.spoolman_cache_ttl == 600.0
    assert d.startup_polling    == 1
    assert d.startup_poll_delay == 2.5
    assert d.poll_interval      == 60.0
    assert d.absent_threshold   == 5
    assert d.transceive_delay   == 0.5
    assert d.crc_delay          == 0.1
    assert d.debug              == 2

def test_defaults_partial_override():
    d = NFCGateDefaults(MockConfig({
        'spoolman_url': 'http://mainsailos.local:7912',
        'debug':        0,
    }))
    assert d.spoolman_url       == 'http://mainsailos.local:7912'
    assert d.debug              == 0
    assert d.startup_polling    == -1
    assert d.startup_poll_delay == 0.0
    assert d.poll_interval      == 30.0
    assert d.absent_threshold   == 3

def test_defaults_poll_interval_below_min_raises():
    try:
        NFCGateDefaults(MockConfig({'poll_interval': 0.5}))
        assert False, "Expected ValueError for poll_interval below minval"
    except (ValueError, Exception):
        pass

def test_defaults_startup_polling_below_min_raises():
    try:
        NFCGateDefaults(MockConfig({'startup_polling': -2}))
        assert False, "Expected ValueError for startup_polling below minval"
    except (ValueError, Exception):
        pass

def test_defaults_startup_polling_above_max_raises():
    try:
        NFCGateDefaults(MockConfig({'startup_polling': 2}))
        assert False, "Expected ValueError for startup_polling above maxval"
    except (ValueError, Exception):
        pass

def test_defaults_startup_poll_delay_below_min_raises():
    try:
        NFCGateDefaults(MockConfig({'startup_poll_delay': -0.1}))
        assert False, "Expected ValueError for startup_poll_delay below minval"
    except (ValueError, Exception):
        pass

def test_defaults_debug_above_max_raises():
    try:
        NFCGateDefaults(MockConfig({'debug': 5}))
        assert False, "Expected ValueError for debug above maxval"
    except (ValueError, Exception):
        pass

def test_defaults_absent_threshold_zero_raises():
    try:
        NFCGateDefaults(MockConfig({'absent_threshold': 0}))
        assert False, "Expected ValueError for absent_threshold=0"
    except (ValueError, Exception):
        pass

# ── Scan-jog config keys ──────────────────────────────────────────────────────

def test_scan_defaults():
    d = NFCGateDefaults(MockConfig())
    assert d.scan_jog_mm   == 50.0
    assert d.scan_max_mm   == 600.0
    assert d.scan_interval == 2.0
    assert d.scan_enabled  == True

def test_scan_keys_overridden():
    d = NFCGateDefaults(MockConfig({
        'scan_jog_mm':   25.0,
        'scan_max_mm':   300.0,
        'scan_interval': 1.5,
        'scan_enabled':  False,
    }))
    assert d.scan_jog_mm   == 25.0
    assert d.scan_max_mm   == 300.0
    assert d.scan_interval == 1.5
    assert d.scan_enabled  == False

def test_scan_jog_mm_below_min_raises():
    try:
        NFCGateDefaults(MockConfig({'scan_jog_mm': 0.5}))
        assert False, "Expected error for scan_jog_mm below minval"
    except (ValueError, Exception):
        pass

def test_scan_max_mm_below_min_raises():
    try:
        NFCGateDefaults(MockConfig({'scan_max_mm': 5.0}))
        assert False, "Expected error for scan_max_mm below minval"
    except (ValueError, Exception):
        pass

def test_scan_interval_below_min_raises():
    try:
        NFCGateDefaults(MockConfig({'scan_interval': 0.1}))
        assert False, "Expected error for scan_interval below minval"
    except (ValueError, Exception):
        pass


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL  {fn.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
