"""
tests/test_pn532_driver.py
==========================
Tests for PN532Driver using a mock I2C object.
"""

import sys
import os
import time
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

_nfc_pkg = _stub('nfc_gates')
_nfc_pkg.__path__    = [os.path.join(_EXTRAS, 'nfc_gates')]
_nfc_pkg.__package__ = 'nfc_gates'
_stub('nfc_gates.log',
      logger=_NullLogger(), configure=lambda *a, **k: None,
      info=lambda *a, **k: None,
      info_both=lambda *a, **k: None,
      warning=lambda *a, **k: None,
      error=lambda *a, **k: None)

time.sleep = lambda _: None

# Other manager tests stub this module during collection; always import the
# real driver here so the driver tests do not depend on pytest file order.
sys.modules.pop('nfc_gates.pn532_driver', None)

from nfc_gates.pn532_driver import (
    PN532Driver,
    _CMD_SAMCONFIGURATION, _CMD_GETFIRMWAREVERSION,
    _CMD_INLISTPASSIVETARGET, _CMD_INRELEASE,
    _TFI_HOST_TO_PN532, _TFI_PN532_TO_HOST,
)


class MockI2C:
    def __init__(self, read_responses=None):
        self.writes = []
        self._responses = list(read_responses or [])

    def i2c_write(self, data):
        self.writes.append(list(data))

    def i2c_read(self, _params, read_len):
        if self._responses:
            resp = self._responses.pop(0)
        else:
            resp = [0x00] * read_len
        return {'response': resp}

    def wrote_cmd(self, cmd_byte):
        for w in self.writes:
            if (len(w) > 6
                    and w[5] == _TFI_HOST_TO_PN532
                    and w[6] == cmd_byte):
                return True
        return False


def _make_response(cmd_resp, payload=()):
    data   = [_TFI_PN532_TO_HOST, cmd_resp] + list(payload)
    length = len(data)
    lcs    = (-length) & 0xFF
    dcs    = (-sum(data)) & 0xFF
    return [0x01, 0x00, 0x00, 0xFF, length, lcs] + data + [dcs, 0x00]


def _sam_ok():       return _make_response(0x15)
def _firmware_ok():  return _make_response(0x03, [0x07, 0x01, 0x06, 0x07])
def _release_ok():   return _make_response(0x53, [0x00])
def _busy():         return [0x00] * 32

def _inlist_tag(uid=(0xA3, 0xF2, 0x00, 0xCC)):
    payload = [1, 1, 0x00, 0x04, 0x08, len(uid)] + list(uid)
    return _make_response(0x4B, payload)

def _inlist_no_tag():
    return _make_response(0x4B, [0])


_ACK_FRAME = [0x01, 0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00]

def _cmd(response_frame):
    return [
        [0x01],
        _ACK_FRAME,
        [0x01],
        response_frame,
    ]


def test_init_sends_samconfiguration():
    i2c = MockI2C(read_responses=_cmd(_firmware_ok()) + _cmd(_sam_ok()))
    driver = PN532Driver(i2c, gate=0, crc_delay=0.0)
    driver.init()
    assert i2c.wrote_cmd(_CMD_SAMCONFIGURATION)

def test_is_alive_returns_true_on_firmware_response():
    i2c = MockI2C(read_responses=_cmd(_firmware_ok()))
    driver = PN532Driver(i2c, gate=0, crc_delay=0.0)
    assert driver.is_alive() is True

def test_is_alive_returns_false_on_bad_response():
    i2c = MockI2C(read_responses=[_busy()] * 8)
    driver = PN532Driver(i2c, gate=0, crc_delay=0.0)
    assert driver.is_alive() is False

def test_no_tag_returns_none():
    i2c = MockI2C(read_responses=_cmd(_inlist_no_tag()))
    driver = PN532Driver(i2c, gate=0, transceive_delay=0.0, crc_delay=0.0)
    assert driver.read_tag() is None

def test_tag_present_returns_uid():
    i2c = MockI2C(read_responses=_cmd(_inlist_tag((0xA3, 0xF2, 0x00, 0xCC)))
                                 + _cmd(_release_ok()))
    driver = PN532Driver(i2c, gate=0, transceive_delay=0.0, crc_delay=0.0)
    assert driver.read_tag() == 'A3F200CC'

def test_tag_7byte_uid():
    uid = (0x04, 0xA2, 0x3B, 0xC1, 0xD4, 0x5E, 0x80)
    i2c = MockI2C(read_responses=_cmd(_inlist_tag(uid)) + _cmd(_release_ok()))
    driver = PN532Driver(i2c, gate=0, transceive_delay=0.0, crc_delay=0.0)
    assert driver.read_tag() == '04A23BC1D45E80'

def test_inlist_command_sent():
    i2c = MockI2C(read_responses=_cmd(_inlist_tag()) + _cmd(_release_ok()))
    driver = PN532Driver(i2c, gate=0, transceive_delay=0.0, crc_delay=0.0)
    driver.read_tag()
    assert i2c.wrote_cmd(_CMD_INLISTPASSIVETARGET)

def test_inrelease_sent_after_tag_found():
    i2c = MockI2C(read_responses=_cmd(_inlist_tag()) + _cmd(_release_ok()))
    driver = PN532Driver(i2c, gate=0, transceive_delay=0.0, crc_delay=0.0)
    driver.read_tag()
    assert i2c.wrote_cmd(_CMD_INRELEASE)

def test_build_frame_structure():
    frame = PN532Driver._build_frame([_CMD_SAMCONFIGURATION, 0x01, 0x00, 0x00])
    assert frame[0] == 0x00 and frame[1] == 0x00 and frame[2] == 0xFF
    assert frame[3] == 5
    assert (frame[3] + frame[4]) & 0xFF == 0
    assert frame[5] == _TFI_HOST_TO_PN532
    assert frame[6] == _CMD_SAMCONFIGURATION

def test_check_frame_returns_payload():
    driver  = PN532Driver(MockI2C(), gate=0, crc_delay=0.0)
    raw     = bytearray(_make_response(0x03, [0x07, 0x01, 0x06, 0x07]))
    payload = driver._check_frame(raw, 0x03)
    assert payload == [0x07, 0x01, 0x06, 0x07]

def test_check_frame_rejects_not_ready():
    driver = PN532Driver(MockI2C(), gate=0, crc_delay=0.0)
    raw    = bytearray(_make_response(0x15))
    raw[0] = 0x00
    assert driver._check_frame(raw, 0x15) is None

def test_check_frame_rejects_wrong_cmd():
    driver = PN532Driver(MockI2C(), gate=0, crc_delay=0.0)
    raw    = bytearray(_make_response(0x15))
    assert driver._check_frame(raw, 0x03) is None


if __name__ == '__main__':
    tests  = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
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
