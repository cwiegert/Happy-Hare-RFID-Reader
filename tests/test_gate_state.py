"""
tests/test_gate_state.py
========================
Unit tests for GateState — no hardware, no Klipper, no mocking required.

Run from the project root:
    python3 -m pytest tests/test_gate_state.py -v
or without pytest:
    python3 tests/test_gate_state.py
"""

import sys
import os
import types

# ── Stub Klipper and driver dependencies so manager.py can be imported ────────
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
      info_both=lambda *a, **k: None,
      warning=lambda *a, **k: None,
      error=lambda *a, **k: None)
_stub('nfc_gates.pn532_driver',
      PN532Driver=object,
      PN532_COMMAND_GETFIRMWAREVERSION=0x02,
      PN532_COMMAND_SAMCONFIGURATION=0x14,
      PN532_COMMAND_INLISTPASSIVETARGET=0x4A,
      get_low_level_debug=lambda config, default=False: default,
      low_level_debug_requested=lambda gcmd: False,
      low_level_debug_help_lines=lambda command_base: [],
      run_low_level_debug=lambda *a, **k: False)
_stub('nfc_gates.spoolman_client', SpoolmanClient=object)
_stub('nfc_gates.vendor',
      __path__=[os.path.join(_EXTRAS, 'nfc_gates', 'vendor')],
      __package__='nfc_gates.vendor')
_stub('nfc_gates.vendor.rfid_tag_parser',
      parse_tag=lambda raw, uid_hex=None: None,
      is_parse_error=lambda info: bool(
          isinstance(info, dict) and (info.get('error') or info.get('parse_error'))))

from nfc_gates.gate_state import (
    CurrentTag, GateState, DIRECT_METADATA_SPOOL,
    EVENT_CHANGED, EVENT_UID_ONLY, EVENT_REMOVED)
from nfc_gates.klipper_interface import KlipperInterface
from nfc_gates.nfc_manager import NFCGate
import nfc_gates.tag_handler as tag_handler


def assert_event(event, expected_type, gate=0, uid=None, spool=None):
    assert event is not None, "Expected an event but got None"
    etype, egate, euid, espool = event
    assert etype  == expected_type, f"Event type: got {etype!r}, want {expected_type!r}"
    assert egate  == gate,          f"Gate: got {egate}, want {gate}"
    if uid   is not None: assert euid   == uid,   f"UID: got {euid!r}, want {uid!r}"
    if spool is not None: assert espool == spool, f"Spool: got {espool}, want {spool}"

def assert_silent(event):
    assert event is None, f"Expected no event but got {event!r}"


def test_empty_gate_stays_silent():
    gs = GateState(gate=0)
    assert_silent(gs.process_read(None, None))
    assert_silent(gs.process_read(None, None))

def test_spool_placed_emits_changed():
    gs = GateState(gate=0)
    event = gs.process_read('A3F200CC', 1042)
    assert_event(event, EVENT_CHANGED, gate=0, uid='A3F200CC', spool=1042)

def test_current_tag_tracks_spool_placed():
    gs = GateState(gate=0)
    gs.process_read('A3F200CC', 1042)
    assert isinstance(gs.current_tag, CurrentTag)
    assert gs.current_tag.uid == 'A3F200CC'
    assert gs.current_tag.spool_id == 1042
    assert gs.current_tag.meta == {}

def test_same_spool_stays_silent():
    gs = GateState(gate=0)
    gs.process_read('A3F200CC', 1042)
    assert_silent(gs.process_read('A3F200CC', 1042))
    assert_silent(gs.process_read('A3F200CC', 1042))

def test_different_spool_same_uid_emits_changed():
    gs = GateState(gate=0)
    gs.process_read('A3F200CC', 1042)
    event = gs.process_read('A3F200CC', 9999)
    assert_event(event, EVENT_CHANGED, spool=9999)
    assert gs.current_tag.uid == 'A3F200CC'
    assert gs.current_tag.spool_id == 9999

def test_different_tag_emits_changed():
    gs = GateState(gate=0)
    gs.process_read('A3F200CC', 1042)
    event = gs.process_read('B1D4A209', 207)
    assert_event(event, EVENT_CHANGED, uid='B1D4A209', spool=207)
    assert gs.current_tag.uid == 'B1D4A209'
    assert gs.current_tag.spool_id == 207

def test_tag_without_spool_emits_uid_only():
    gs = GateState(gate=0)
    event = gs.process_read('A3F200CC', None)
    assert_event(event, EVENT_UID_ONLY, uid='A3F200CC', spool=None)
    assert gs.current_tag.uid == 'A3F200CC'
    assert gs.current_tag.spool_id is None

def test_debounce_suppresses_early_removal():
    gs = GateState(gate=0, absent_threshold=3)
    gs.process_read('A3F200CC', 1042)
    assert_silent(gs.process_read(None, None))
    assert_silent(gs.process_read(None, None))
    event = gs.process_read(None, None)
    assert_event(event, EVENT_REMOVED, gate=0)

def test_removal_with_threshold_1():
    gs = GateState(gate=0, absent_threshold=1)
    gs.process_read('A3F200CC', 1042)
    event = gs.process_read(None, None)
    assert_event(event, EVENT_REMOVED)

def test_removal_clears_state():
    gs = GateState(gate=0, absent_threshold=1)
    gs.process_read('A3F200CC', 1042)
    gs.process_read(None, None)
    assert gs.current_tag is None
    event = gs.process_read('B1D4A209', 207)
    assert_event(event, EVENT_CHANGED, uid='B1D4A209', spool=207)

def test_direct_compatibility_fields_sync_current_tag():
    gs = GateState(gate=0)
    gs.current_uid = 'A3F200CC'
    gs.current_spool = 1042
    assert gs.current_tag.uid == 'A3F200CC'
    assert gs.current_tag.spool_id == 1042
    gs.current_spool = None
    assert gs.current_tag.uid == 'A3F200CC'
    assert gs.current_tag.spool_id is None
    gs.current_uid = None
    assert gs.current_tag is None

def test_intermittent_miss_resets_counter():
    gs = GateState(gate=0, absent_threshold=3)
    gs.process_read('A3F200CC', 1042)
    gs.process_read(None, None)
    gs.process_read(None, None)
    gs.process_read('A3F200CC', 1042)
    gs.process_read(None, None)
    assert_silent(gs.process_read(None, None))

def test_removal_only_fires_once():
    gs = GateState(gate=0, absent_threshold=1)
    gs.process_read('A3F200CC', 1042)
    gs.process_read(None, None)
    assert_silent(gs.process_read(None, None))
    assert_silent(gs.process_read(None, None))

def test_gate_index_preserved_in_event():
    for gate_num in range(5):
        gs = GateState(gate=gate_num)
        event = gs.process_read('A3F200CC', 1042)
        assert_event(event, EVENT_CHANGED, gate=gate_num)

def test_uid_only_to_spool_update():
    gs = GateState(gate=0)
    gs.process_read('A3F200CC', None)
    event = gs.process_read('A3F200CC', 1042)
    assert_event(event, EVENT_CHANGED, spool=1042)


class _ResolverSpoolman:
    _timeout = 5.0
    _rfid_key = 'rfid_tag'

    def __init__(self, by_id=None, by_uid=None, base_url='http://spoolman'):
        self.by_id = by_id or {}
        self.by_uid = by_uid or {}
        self.base_url = base_url
        self.calls = []

    def lookup_spool_by_id(self, spool_id):
        self.calls.append(('id', spool_id))
        return self.by_id.get(spool_id)

    def lookup_spool_by_uid(self, uid_hex):
        self.calls.append(('uid', uid_hex))
        return self.by_uid.get(uid_hex)

    def _resolve_base_url(self):
        return self.base_url

    def clear_cache(self):
        self.calls.append(('clear_cache',))

    def set_spool_uid(self, spool_id, uid_hex):
        self.calls.append(('set_uid', spool_id, uid_hex, self._rfid_key))
        return True


def _resolver_gate(uid, meta=None, spoolman=None, tag_parsing=True):
    gate = NFCGate.__new__(NFCGate)
    gate._state = GateState(0)
    gate._state.current_tag = CurrentTag(uid=uid, meta=meta or {})
    gate._spoolman = spoolman
    gate._spoolman_auto_create = False
    gate._bambu_reads = False
    gate._tag_parsing = tag_parsing
    gate._debug = 0
    gate._name = 'lane0'
    gate._gate = 0
    return gate


class _FakeLBSpoolmanClient:
    instances = []

    def __init__(self, base_url, timeout=5.0):
        self.base_url = base_url
        self.timeout = timeout
        self.calls = []
        self.vendor_id = 7
        self.filament_id = 11
        self.spool = {'id': 1234}
        self.spool_id = 1234
        self.raise_on = None
        self.__class__.instances.append(self)

    def auto_create_spool(self, filament_info, uid_hex=None):
        self.calls.append(('auto_create_spool', filament_info, uid_hex))
        if self.raise_on == 'auto_create_spool':
            raise RuntimeError('auto-create failed')
        return self.spool_id

    def find_or_create_vendor(self, name):
        self.calls.append(('vendor', name))
        if self.raise_on == 'vendor':
            raise RuntimeError('vendor failed')
        return self.vendor_id

    def find_or_create_filament(self, **kwargs):
        self.calls.append(('filament', kwargs))
        if self.raise_on == 'filament':
            raise RuntimeError('filament failed')
        return self.filament_id

    def create_spool(self, **kwargs):
        self.calls.append(('spool', kwargs))
        if self.raise_on == 'spool':
            raise RuntimeError('spool failed')
        return self.spool


def _install_fake_lb_client(monkeypatch, cls=_FakeLBSpoolmanClient):
    import sys
    cls.instances = []
    module = types.ModuleType('nfc_gates.vendor.lameandboard_spoolman')
    module.SpoolmanClient = cls
    monkeypatch.setitem(sys.modules, 'nfc_gates.vendor.lameandboard_spoolman',
                        module)
    return cls


def test_resolve_embedded_id_wins_before_uid_lookup():
    spoolman = _ResolverSpoolman(
        by_id={42: {'id': 42}},
        by_uid={'04AABB': 99})
    gate = _resolver_gate('04AABB', {'spoolman_id': 42}, spoolman)

    assert gate._resolve_spool('04AABB') == 42
    assert spoolman.calls == [('id', 42)]


def test_resolve_embedded_id_miss_falls_back_to_uid_lookup():
    spoolman = _ResolverSpoolman(by_uid={'04AABB': 99})
    gate = _resolver_gate('04AABB', {'spoolman_id': 42}, spoolman)

    assert gate._resolve_spool('04AABB') == 99
    assert spoolman.calls == [('id', 42), ('uid', '04AABB')]


def test_resolve_ignores_stale_metadata_from_previous_uid():
    spoolman = _ResolverSpoolman()
    gate = _resolver_gate('OLDUID', {'spoolman_id': 42, 'material': 'PLA'},
                          spoolman)

    assert gate._resolve_spool('NEWUID') is None
    assert spoolman.calls == [('uid', 'NEWUID')]


def test_resolve_direct_metadata_requires_current_tag_and_parsing_enabled():
    gate = _resolver_gate('04AABB', {'material': 'PLA'}, spoolman=None,
                          tag_parsing=True)
    assert gate._resolve_spool('04AABB') is DIRECT_METADATA_SPOOL

    gate = _resolver_gate('04AABB', {'material': 'PLA'}, spoolman=None,
                          tag_parsing=False)
    assert gate._resolve_spool('04AABB') is None


def test_resolve_auto_create_uses_vendor_top_level_then_patches_rfid_key(monkeypatch):
    fake_cls = _install_fake_lb_client(monkeypatch)
    spoolman = _ResolverSpoolman()
    spoolman._rfid_key = 'custom_uid'
    gate = _resolver_gate(
        '04AABB',
        {
            'material': 'PLA',
            'brand': 'Bambu Lab',
            'color_hex': 'FF5500',
            'diameter_mm': 1.75,
            'weight_g': 1000,
            'spool_weight_g': 250,
            'tray_uid': 'ABC123',
        },
        spoolman)
    gate._spoolman_auto_create = True

    assert gate._resolve_spool('04AABB') == 1234
    assert spoolman.calls == [
        ('uid', '04AABB'),
        ('set_uid', 1234, '04AABB', 'custom_uid'),
        ('clear_cache',),
    ]
    client = fake_cls.instances[0]
    assert client.base_url == 'http://spoolman'
    assert client.timeout == 5.0
    assert client.calls == [
        ('auto_create_spool', gate._state.current_tag.meta, None),
    ]
    assert gate._state.current_tag.resolution == {
        'path': 'auto_create',
        'spool_id': 1234,
    }


def test_resolve_incomplete_structured_read_defers_auto_create(monkeypatch):
    fake_cls = _install_fake_lb_client(monkeypatch)
    spoolman = _ResolverSpoolman()
    gate = _resolver_gate(
        '04AABB',
        {
            'material': 'PLA',
            'brand': 'Bambu Lab',
            'color_hex': 'FF5500',
        },
        spoolman)
    gate._spoolman_auto_create = True
    gate._state.current_tag.read_incomplete = True
    gate._state.current_tag.read_retry_reason = 'auth failed sectors [2, 3]'

    assert gate._resolve_spool('04AABB') is None
    assert spoolman.calls == [('uid', '04AABB')]
    assert fake_cls.instances == []
    assert gate._state.current_tag.resolution == {
        'path': 'structured_read_incomplete',
    }


def test_resolve_auto_create_failure_falls_back_unresolved(monkeypatch):
    class _FailingLBSpoolmanClient(_FakeLBSpoolmanClient):
        def __init__(self, base_url, timeout=5.0):
            super().__init__(base_url, timeout)
            self.raise_on = 'auto_create_spool'

    _install_fake_lb_client(monkeypatch, _FailingLBSpoolmanClient)
    spoolman = _ResolverSpoolman()
    gate = _resolver_gate('04AABB', {'material': 'PLA'}, spoolman)
    gate._spoolman_auto_create = True

    assert gate._resolve_spool('04AABB') is None
    assert spoolman.calls == [('uid', '04AABB')]
    assert gate._state.current_tag.resolution == {'path': 'unresolved'}


def test_resolve_auto_create_uid_patch_failure_is_unresolved(monkeypatch):
    _install_fake_lb_client(monkeypatch)

    class _PatchFailSpoolman(_ResolverSpoolman):
        def set_spool_uid(self, spool_id, uid_hex):
            self.calls.append(('set_uid', spool_id, uid_hex, self._rfid_key))
            return False

    spoolman = _PatchFailSpoolman()
    gate = _resolver_gate('04AABB', {'material': 'PLA'}, spoolman)
    gate._spoolman_auto_create = True

    assert gate._resolve_spool('04AABB') is None
    assert spoolman.calls == [
        ('uid', '04AABB'),
        ('set_uid', 1234, '04AABB', 'rfid_tag'),
    ]
    assert gate._state.current_tag.resolution == {
        'path': 'auto_create_uid_patch_failed',
        'spool_id': 1234,
    }


def test_auto_create_full_chain_re_poll_finds_spool_via_uid(monkeypatch):
    """Poll 1: UID unknown → auto-create fires, UID patched into Spoolman, cache cleared.
    Poll 2: UID now registered → UID lookup resolves without a second auto-create,
    and process_read returns None (no re-dispatch)."""

    class _LiveSpoolman(_ResolverSpoolman):
        def set_spool_uid(self, spool_id, uid_hex):
            self.calls.append(('set_uid', spool_id, uid_hex, self._rfid_key))
            self.by_uid[uid_hex] = spool_id  # simulate Spoolman persisting the UID
            return True

    _install_fake_lb_client(monkeypatch)
    spoolman = _LiveSpoolman()
    meta = {'material': 'PLA', 'color_hex': 'FF5500'}
    gate = _resolver_gate('04AABB', meta, spoolman)
    gate._spoolman_auto_create = True

    # ── Poll 1: UID unknown → auto-create ────────────────────────────────────
    spool_id = gate._resolve_spool('04AABB')
    assert spool_id == 1234
    assert gate._state.current_tag.resolution == {'path': 'auto_create', 'spool_id': 1234}
    assert spoolman.calls == [
        ('uid', '04AABB'),
        ('set_uid', 1234, '04AABB', 'rfid_tag'),
        ('clear_cache',),
    ]
    event = gate._state.process_read('04AABB', 1234)
    assert event == (EVENT_CHANGED, 0, '04AABB', 1234)

    # ── Poll 2: fresh CurrentTag (as _read_current_tag would produce), cache cleared ─
    gate._state.current_tag = CurrentTag(uid='04AABB', meta=meta)
    spoolman.calls.clear()

    spool_id2 = gate._resolve_spool('04AABB')
    assert spool_id2 == 1234
    # Resolved via UID lookup, not auto-create
    assert gate._state.current_tag.resolution == {'path': 'uid_lookup', 'spool_id': 1234}
    assert spoolman.calls == [('uid', '04AABB')]

    # Same uid + same spool → quiet, no second dispatch
    event2 = gate._state.process_read('04AABB', 1234)
    assert event2 is None


class _ReaderForCurrentTag:
    def __init__(self, target_info=None, uid='04AABB', raw=None):
        self.target_info = target_info
        self.uid = uid
        self.raw = raw if raw is not None else bytearray([0] * 64)
        self.calls = []

    def read_tag(self):
        self.calls.append('read_tag')
        return self.uid

    def read_target(self):
        self.calls.append('read_target')
        return self.target_info

    def ntag_read_user_memory(self, start_page=4, end_page=67):
        self.calls.append(('ntag_read_user_memory', start_page, end_page))
        return self.raw

    def mifare_read_authenticated_blocks(self, sector_keys, sectors, uid_bytes=None):
        self.calls.append(('mifare_read_authenticated_blocks',
                           sector_keys, sectors, uid_bytes))
        return self.raw

    def _release_current_target(self, reason='manual'):
        self.calls.append(('release', reason))


def _read_gate(reader, tag_parsing=True):
    gate = NFCGate.__new__(NFCGate)
    gate._reader = reader
    gate._state = GateState(0)
    gate._spoolman = None
    gate._tag_parsing = tag_parsing
    gate._bambu_reads = False
    gate._tag_max_pages = 8
    gate._debug = 0
    gate._name = 'lane0'
    gate._gate = 0
    return gate


def _target(uid='04AABB', sak=0x00, atqa=0x0044, uid_length=7):
    return {
        'uid': uid,
        'uid_bytes': [0x04, 0xAA, 0xBB],
        'target': 1,
        'tg': 1,
        'sak': sak,
        'sens_res': atqa,
        'atqa': atqa,
        'uid_length': uid_length,
    }


def test_read_current_tag_uid_only_mode_calls_read_tag_only():
    reader = _ReaderForCurrentTag()
    gate = _read_gate(reader, tag_parsing=False)

    assert gate._read_current_tag() == '04AABB'
    assert reader.calls == ['read_tag']
    assert gate._state.current_tag is None


def test_read_current_tag_deep_mode_reads_ntag_memory():
    reader = _ReaderForCurrentTag(target_info=_target(sak=0x00))
    gate = _read_gate(reader, tag_parsing=True)

    assert gate._read_current_tag() == '04AABB'
    assert reader.calls[0] == 'read_target'
    assert reader.calls[1] == ('ntag_read_user_memory', 4, 11)
    assert gate._state.current_tag.uid == '04AABB'
    assert gate._state.current_tag.target_info['sak'] == 0x00
    assert gate._state.current_tag.raw_tag_data == reader.raw


def test_read_current_tag_spoolman_uid_hit_skips_structured_read():
    reader = _ReaderForCurrentTag(target_info=_target(sak=0x08))
    gate = _read_gate(reader, tag_parsing=True)
    gate._bambu_reads = True
    gate._spoolman = _ResolverSpoolman(by_uid={'04AABB': 42})

    assert gate._read_current_tag() == '04AABB'
    assert gate._resolve_spool('04AABB') == 42
    assert reader.calls == [
        'read_target',
        ('release', 'early_uid_lookup'),
    ]
    assert gate._spoolman.calls == [('uid', '04AABB')]
    assert gate._state.current_tag.meta == {'uid': '04AABB'}
    assert gate._state.current_tag.resolution == {
        'path': 'early_uid_lookup',
        'spool_id': 42,
    }


def test_read_current_tag_spoolman_uid_miss_continues_structured_read(monkeypatch):
    import sys
    monkeypatch.setattr(tag_handler, 'resolve_auth_keys',
                        lambda gate, tag: ({0: b'key'}, None))
    sys.modules['nfc_gates.vendor.rfid_tag_parser'].parse_tag = (
        lambda raw, uid_hex=None: {'uid': uid_hex, 'material': 'PLA'})
    reader = _ReaderForCurrentTag(
        target_info=_target(uid='04AABBCC', sak=0x08),
        raw={'blocks': {0: bytes(16)}, 'auth_failed_sectors': []})
    gate = _read_gate(reader, tag_parsing=True)
    gate._bambu_reads = True
    gate._spoolman = _ResolverSpoolman()

    assert gate._read_current_tag() == '04AABBCC'
    assert gate._spoolman.calls == [('uid', '04AABBCC')]
    assert any(isinstance(call, tuple)
               and call[0] == 'mifare_read_authenticated_blocks'
               for call in reader.calls)
    assert gate._state.current_tag.meta.get('material') == 'PLA'


def test_read_current_tag_mifare_respects_bambu_reads_disabled():
    reader = _ReaderForCurrentTag(target_info=_target(sak=0x08))
    gate = _read_gate(reader, tag_parsing=True)

    assert gate._read_current_tag() == '04AABB'
    assert 'read_target' in reader.calls
    assert not any(isinstance(call, tuple) and call[0] == 'ntag_read_user_memory'
                   for call in reader.calls)
    assert not any(isinstance(call, tuple)
                   and call[0] == 'mifare_read_authenticated_blocks'
                   for call in reader.calls)
    assert ('release', 'mifare_disabled') in reader.calls
    assert gate._state.current_tag.parse_error == (
        'mifare_classic rich read disabled; uid-only fallback')


def test_read_current_tag_mifare_key_failure_falls_back_without_ntag_read(monkeypatch):
    import sys
    monkeypatch.delattr(sys.modules['nfc_gates.vendor.rfid_tag_parser'],
                        '_bambu_derive_keys', raising=False)
    reader = _ReaderForCurrentTag(target_info=_target(sak=0x08))
    gate = _read_gate(reader, tag_parsing=True)
    gate._bambu_reads = True

    assert gate._read_current_tag() == '04AABB'
    assert not any(isinstance(call, tuple) and call[0] == 'ntag_read_user_memory'
                   for call in reader.calls)
    assert ('release', 'mifare_key_failure') in reader.calls
    assert gate._state.current_tag.parse_error.startswith(
        'mifare auth key derivation failed:')


def test_read_current_tag_mifare_reads_authenticated_blocks_when_keys_exist(monkeypatch):
    import sys
    parser = sys.modules['nfc_gates.vendor.rfid_tag_parser']
    keys = [bytes([idx]) * 6 for idx in range(16)]
    block_dump = {'uid_bytes': bytes([0x04, 0xAA, 0xBB, 0xCC]),
                  'blocks': {2: b'PLA\x00' + (b'\x00' * 12)}}
    monkeypatch.setattr(parser, '_bambu_derive_keys', lambda uid_bytes: keys,
                        raising=False)

    def _parse(raw, uid_hex=None):
        assert raw is block_dump
        return {
            'uid': uid_hex,
            'material': 'PLA',
            'brand': 'Bambu Lab',
            'tag_format': 'bambu',
        }
    monkeypatch.setattr(parser, 'parse_tag', _parse)

    target = _target(uid='04AABBCC', sak=0x08, uid_length=4)
    target['uid_bytes'] = [0x04, 0xAA, 0xBB, 0xCC]
    reader = _ReaderForCurrentTag(target_info=target, raw=block_dump)
    gate = _read_gate(reader, tag_parsing=True)
    gate._bambu_reads = True

    assert gate._read_current_tag() == '04AABBCC'
    assert not any(isinstance(call, tuple) and call[0] == 'ntag_read_user_memory'
                   for call in reader.calls)
    read_calls = [call for call in reader.calls
                  if isinstance(call, tuple)
                  and call[0] == 'mifare_read_authenticated_blocks']
    assert len(read_calls) == 1
    assert read_calls[0][1] is keys
    assert read_calls[0][2] == [0, 1, 2, 3, 4]
    assert read_calls[0][3] == bytes([0x04, 0xAA, 0xBB, 0xCC])
    assert gate._state.current_tag.raw_tag_data is block_dump
    assert gate._state.current_tag.meta['tag_format'] == 'bambu'
    assert gate._state.current_tag.parse_error is None


class _EmptyMifareReader(_ReaderForCurrentTag):
    def mifare_read_authenticated_blocks(self, sector_keys, sectors, uid_bytes=None):
        self.calls.append(('mifare_read_authenticated_blocks',
                           sector_keys, sectors, uid_bytes))
        return None


def test_read_current_tag_mifare_empty_blocks_falls_back_to_uid_meta(monkeypatch):
    import sys
    parser = sys.modules['nfc_gates.vendor.rfid_tag_parser']
    monkeypatch.setattr(parser, '_bambu_derive_keys',
                        lambda uid_bytes: [b'\x00' * 6] * 16,
                        raising=False)

    target = _target(uid='04AABBCC', sak=0x08, uid_length=4)
    target['uid_bytes'] = [0x04, 0xAA, 0xBB, 0xCC]
    reader = _EmptyMifareReader(target_info=target)
    gate = _read_gate(reader, tag_parsing=True)
    gate._bambu_reads = True

    assert gate._read_current_tag() == '04AABBCC'
    assert gate._state.current_tag.meta == {'uid': '04AABBCC'}
    assert gate._state.current_tag.parse_error == 'mifare read returned no blocks'


def test_read_current_tag_unknown_falls_back_without_ntag_read():
    reader = _ReaderForCurrentTag(target_info=_target(sak=0x20))
    gate = _read_gate(reader, tag_parsing=True)

    assert gate._read_current_tag() == '04AABB'
    assert not any(isinstance(call, tuple) and call[0] == 'ntag_read_user_memory'
                   for call in reader.calls)
    assert ('release', 'unsupported_uid_only_fallback') in reader.calls
    assert gate._state.current_tag.parse_error == (
        'unsupported target; uid-only fallback')

# ── _read_current_tag: read_target failure cases ─────────────────────────────

def test_read_target_none_returns_none():
    """read_target() returning None means no tag is present; no CurrentTag created."""
    reader = _ReaderForCurrentTag(target_info=None)
    gate = _read_gate(reader, tag_parsing=True)
    assert gate._read_current_tag() is None
    assert reader.calls == ['read_target']
    assert gate._state.current_tag is None


def test_read_target_missing_uid_releases_and_returns_none():
    """Target dict with no 'uid' key releases the target and returns None."""
    target = {k: v for k, v in _target().items() if k != 'uid'}
    reader = _ReaderForCurrentTag(target_info=target)
    gate = _read_gate(reader, tag_parsing=True)
    assert gate._read_current_tag() is None
    assert ('release', 'missing_uid') in reader.calls
    assert gate._state.current_tag is None


def test_read_target_empty_uid_releases_and_returns_none():
    """Target dict with uid='' is treated as missing; releases and returns None."""
    reader = _ReaderForCurrentTag(target_info=_target(uid=''))
    gate = _read_gate(reader, tag_parsing=True)
    assert gate._read_current_tag() is None
    assert ('release', 'missing_uid') in reader.calls
    assert gate._state.current_tag is None


# ── _read_current_tag: NTAG read failure cases ────────────────────────────────

class _RaisingNtagReader(_ReaderForCurrentTag):
    def ntag_read_user_memory(self, start_page=4, end_page=67):
        self.calls.append(('ntag_read_user_memory', start_page, end_page))
        raise OSError("I2C timeout")


def test_ntag_read_exception_returns_uid_with_parse_error():
    """I2C error during NTAG read sets parse_error, read_incomplete, and read_retry_reason."""
    reader = _RaisingNtagReader(target_info=_target(sak=0x00))
    gate = _read_gate(reader, tag_parsing=True)
    assert gate._read_current_tag() == '04AABB'
    tag = gate._state.current_tag
    assert tag is not None
    assert tag.parse_error.startswith('ntag read failed:')
    assert tag.meta == {'uid': '04AABB'}
    assert tag.raw_tag_data is None
    assert tag.read_incomplete is True
    assert tag.read_retry_reason == tag.parse_error


def test_ntag_read_empty_bytes_returns_uid_with_parse_error():
    """Empty NTAG read sets parse_error, read_incomplete, and read_retry_reason."""
    reader = _ReaderForCurrentTag(target_info=_target(sak=0x00), raw=bytearray())
    gate = _read_gate(reader, tag_parsing=True)
    assert gate._read_current_tag() == '04AABB'
    tag = gate._state.current_tag
    assert tag.parse_error == 'empty ntag read'
    assert tag.meta == {'uid': '04AABB'}
    assert tag.read_incomplete is True
    assert tag.read_retry_reason == 'empty ntag read'


class _NoneNtagReader(_ReaderForCurrentTag):
    def ntag_read_user_memory(self, start_page=4, end_page=67):
        self.calls.append(('ntag_read_user_memory', start_page, end_page))
        return None


def test_ntag_read_none_returns_uid_with_parse_error():
    """NTAG read returning None sets parse_error, read_incomplete, and read_retry_reason."""
    reader = _NoneNtagReader(target_info=_target(sak=0x00))
    gate = _read_gate(reader, tag_parsing=True)
    assert gate._read_current_tag() == '04AABB'
    tag = gate._state.current_tag
    assert tag.parse_error == 'empty ntag read'
    assert tag.meta == {'uid': '04AABB'}
    assert tag.read_incomplete is True
    assert tag.read_retry_reason == 'empty ntag read'


# ── _parse_current_tag: parse_tag failure cases ───────────────────────────────
#
# Each test stubs nfc_gates.vendor.rfid_tag_parser.parse_tag to control the
# outcome.  The stub installed at module level (returns None) is the default.

_NON_EMPTY_RAW = bytearray([0x03, 0x0F, 0xD1, 0x01, 0x0B, 0x54, 0x02, 0x65]
                            + [0x00] * 56)


def _parse_gate(parse_tag_fn):
    """Gate wired to a non-empty NTAG read and a controlled parse_tag."""
    import sys
    sys.modules['nfc_gates.vendor.rfid_tag_parser'].parse_tag = parse_tag_fn
    reader = _ReaderForCurrentTag(target_info=_target(sak=0x00), raw=_NON_EMPTY_RAW)
    return _read_gate(reader, tag_parsing=True)


def test_parse_tag_raises_sets_parse_error():
    """parse_tag() raising an exception records parse_error; UID is still returned."""
    def _bad(raw, uid_hex=None):
        raise ValueError("corrupt NDEF")
    gate = _parse_gate(_bad)
    assert gate._read_current_tag() == '04AABB'
    assert gate._state.current_tag.parse_error.startswith('parse failed:')


def test_parse_tag_returns_none_yields_uid_only_meta():
    """parse_tag() returning None leaves meta as {uid: ...} with no parse_error."""
    gate = _parse_gate(lambda raw, uid_hex=None: None)
    assert gate._read_current_tag() == '04AABB'
    tag = gate._state.current_tag
    assert tag.meta == {'uid': '04AABB'}
    assert tag.parse_error is None


def test_parse_tag_error_dict_surfaces_parse_error():
    """parse_tag() returning a dict with parse_error key surfaces that error."""
    def _errored(raw, uid_hex=None):
        return {'uid': uid_hex, 'parse_error': 'unrecognised tag format'}
    gate = _parse_gate(_errored)
    assert gate._read_current_tag() == '04AABB'
    tag = gate._state.current_tag
    assert tag.parse_error == 'unrecognised tag format'
    assert tag.meta.get('uid') == '04AABB'


# ── Metadata preserved through rewind ────────────────────────────────────────

def test_metadata_preserved_when_tag_absent_on_second_read():
    """current_tag.meta set by a successful deep read survives a subsequent miss.

    During scan-jog rewind the spool moves away from the reader.  Subsequent
    poll ticks return no tag.  The metadata captured on the first read must
    still be on current_tag so _resolve_spool can use it from cache.
    """
    import sys
    captured_meta = {'uid': '04AABB', 'material': 'PLA', 'color_hex': 'FF5500'}
    sys.modules['nfc_gates.vendor.rfid_tag_parser'].parse_tag = (
        lambda raw, uid_hex=None: dict(captured_meta))

    reader = _ReaderForCurrentTag(target_info=_target(sak=0x00), raw=_NON_EMPTY_RAW)
    gate = _read_gate(reader, tag_parsing=True)

    uid = gate._read_current_tag()
    assert uid == '04AABB'
    tag_after_read = gate._state.current_tag
    assert tag_after_read.meta.get('material') == 'PLA'

    # Tag moves away — second read returns None
    reader.target_info = None
    assert gate._read_current_tag() is None

    # current_tag must still hold the metadata from the first read
    assert gate._state.current_tag is tag_after_read
    assert gate._state.current_tag.meta.get('material') == 'PLA'
    assert gate._state.current_tag.meta.get('color_hex') == 'FF5500'


# ── scan_mode parameter ───────────────────────────────────────────────────────

def test_scan_mode_suppresses_miss_count():
    """No-read ticks with scan_mode=True do not increment miss_count."""
    gs = GateState(gate=0, absent_threshold=3)
    gs.process_read('A3F200CC', 1042)
    for _ in range(10):
        assert_silent(gs.process_read(None, None, scan_mode=True))
    assert gs.miss_count == 0, f"miss_count should be 0 in scan mode, got {gs.miss_count}"

def test_scan_mode_tag_found_fires_event():
    """Tag found during scan mode still fires EVENT_CHANGED normally."""
    gs = GateState(gate=0)
    event = gs.process_read('A3F200CC', 1042, scan_mode=True)
    assert_event(event, EVENT_CHANGED, uid='A3F200CC', spool=1042)

def test_scan_mode_off_resumes_miss_count():
    """After scan ends (scan_mode=False), removal threshold applies normally."""
    gs = GateState(gate=0, absent_threshold=2)
    gs.process_read('A3F200CC', 1042)
    # scan ticks do not count toward removal
    gs.process_read(None, None, scan_mode=True)
    gs.process_read(None, None, scan_mode=True)
    # back to normal — threshold should fire after 2 real misses
    assert_silent(gs.process_read(None, None))
    event = gs.process_read(None, None)
    assert_event(event, EVENT_REMOVED)


# ─────────────────────────────────────────────────────────────────────────────
# Item 7 — KlipperInterface._run_gcode() metadata branch
# ─────────────────────────────────────────────────────────────────────────────

class _ImmediateReactor:
    """Executes register_callback payloads synchronously so tests can inspect GCode."""
    def register_callback(self, cb):
        cb(0.0)

class _GCodeCapture:
    def __init__(self):
        self.scripts = []
    def run_script(self, script):
        self.scripts.append(script)

class _MockPrinterKI:
    def __init__(self):
        self._gcode = _GCodeCapture()
    def lookup_object(self, name, default=None):
        return self._gcode if name == 'gcode' else default

def _make_ki():
    p = _MockPrinterKI()
    ki = KlipperInterface(p, _ImmediateReactor(), debug=0)
    return ki, p._gcode

def test_klipper_interface_changed_with_spool_id():
    ki, gcode = _make_ki()
    ki.dispatch(EVENT_CHANGED, gate=0, uid_hex='04AABB', spool_id=42)
    assert gcode.scripts == ['_NFC_SPOOL_CHANGED GATE=0 SPOOL_ID=42 UID=04AABB']

def test_klipper_interface_changed_metadata_only_full_meta():
    ki, gcode = _make_ki()
    ki.dispatch(EVENT_CHANGED, gate=1, uid_hex='04AABB', spool_id=None,
                meta={'material': 'PLA', 'material_detail': 'PLA Basic',
                      'color_hex': 'FF5500', 'brand': 'Bambu Lab',
                      'min_temp': 190, 'max_temp': 230})
    assert len(gcode.scripts) == 1
    s = gcode.scripts[0]
    assert s.startswith('_NFC_SPOOL_CHANGED ')
    assert 'GATE=1' in s
    assert 'NAME=Bambu_PLA_Basic' in s
    assert 'MATERIAL=PLA' in s
    assert 'COLOR=FF5500' in s
    assert 'VENDOR' not in s   # VENDOR dropped — MMU_GATE_MAP has no such param
    assert 'TEMP=230' in s
    assert 'TEMP=190' not in s
    assert 'UID=04AABB' in s
    assert 'SPOOL_ID' not in s

def test_klipper_interface_changed_metadata_only_omits_min_temp():
    ki, gcode = _make_ki()
    ki.dispatch(EVENT_CHANGED, gate=1, uid_hex='04AABB', spool_id=None,
                meta={'material': 'PLA', 'min_temp': 190})
    s = gcode.scripts[0]
    assert 'TEMP=' not in s

def test_klipper_interface_changed_metadata_only_partial_meta():
    ki, gcode = _make_ki()
    ki.dispatch(EVENT_CHANGED, gate=0, uid_hex='04CCDD', spool_id=None,
                meta={'material': 'PETG'})
    s = gcode.scripts[0]
    assert 'NAME=PETG' in s
    assert 'MATERIAL=PETG' in s
    assert 'COLOR' not in s    # empty color omitted entirely
    assert 'VENDOR' not in s

def test_klipper_interface_changed_metadata_only_none_meta():
    ki, gcode = _make_ki()
    ki.dispatch(EVENT_CHANGED, gate=0, uid_hex='04CCDD', spool_id=None, meta=None)
    s = gcode.scripts[0]
    assert 'MATERIAL' not in s  # no meta → no MATERIAL or COLOR params
    assert 'SPOOL_ID' not in s

def test_klipper_interface_uid_only():
    ki, gcode = _make_ki()
    ki.dispatch(EVENT_UID_ONLY, gate=2, uid_hex='04CCDD', spool_id=None)
    assert gcode.scripts == ['_NFC_TAG_NO_SPOOL GATE=2 UID=04CCDD']

def test_klipper_interface_removed():
    ki, gcode = _make_ki()
    ki.dispatch(EVENT_REMOVED, gate=3, uid_hex=None, spool_id=7)
    assert gcode.scripts == ['_NFC_SPOOL_REMOVED GATE=3']

def test_klipper_interface_macro_value_sanitises_special_chars():
    ki, gcode = _make_ki()
    # Spaces → underscores, special chars stripped, hash and dot kept
    ki.dispatch(EVENT_CHANGED, gate=0, uid_hex='04AABB', spool_id=None,
                meta={'material': 'ABS & CF', 'color_hex': '#FF5500'})
    s = gcode.scripts[0]
    assert 'MATERIAL=ABS__CF' in s  # space→_ on each side of stripped &
    assert 'COLOR=#FF5500' in s
    assert 'VENDOR' not in s


# ─────────────────────────────────────────────────────────────────────────────
# Item 9 — auto-create re-poll: second scan uses UID lookup, not auto-create
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_auto_create_second_poll_uses_uid_lookup(monkeypatch):
    """After auto-create + UID patch, the next _resolve_spool call must
    reach the UID lookup path (not re-run auto_create_spool)."""
    fake_cls = _install_fake_lb_client(monkeypatch)

    class _StatefulSpoolman(_ResolverSpoolman):
        def __init__(self):
            super().__init__()
            self._registered_uid = None

        def lookup_spool_by_uid(self, uid_hex):
            self.calls.append(('uid', uid_hex))
            return 1234 if self._registered_uid == uid_hex else None

        def set_spool_uid(self, spool_id, uid_hex):
            self.calls.append(('set_uid', spool_id, uid_hex, self._rfid_key))
            self._registered_uid = uid_hex
            return True

        def clear_cache(self):
            self.calls.append(('clear_cache',))

    spoolman = _StatefulSpoolman()
    gate = _resolver_gate('04AABB', {'material': 'PLA'}, spoolman)
    gate._spoolman_auto_create = True

    # First poll: UID unknown → triggers auto-create
    result1 = gate._resolve_spool('04AABB')
    assert result1 == 1234
    assert gate._state.current_tag.resolution == {'path': 'auto_create', 'spool_id': 1234}
    assert ('clear_cache',) in spoolman.calls

    # Second poll: same tag — UID is now registered, should resolve via UID lookup
    result2 = gate._resolve_spool('04AABB')
    assert result2 == 1234
    assert gate._state.current_tag.resolution == {'path': 'uid_lookup', 'spool_id': 1234}

    # auto_create_spool must have been called exactly once across both polls
    auto_create_calls = [c for inst in fake_cls.instances
                         for c in inst.calls if c[0] == 'auto_create_spool']
    assert len(auto_create_calls) == 1


# ---------------------------------------------------------------------------
# get_status tag_present field
# ---------------------------------------------------------------------------

def _status_gate():
    gate = NFCGate.__new__(NFCGate)
    gate._state = GateState(0)
    gate._gate = 0
    gate._failed = False
    return gate


def test_get_status_empty_gate():
    gate = _status_gate()
    s = gate.get_status()
    assert s['tag_present'] is False
    assert s['spool_id'] == -1
    assert s['uid'] == ''
    assert s['resolution'] == ''


def test_get_status_spool_present():
    gate = _status_gate()
    gate._state.current_uid = '04AABBCC'
    gate._state.current_spool = 42
    gate._state.current_tag = CurrentTag(uid='04AABBCC', meta={})
    gate._state.current_tag.resolution = {'path': 'uid_lookup', 'spool_id': 42}
    s = gate.get_status()
    assert s['tag_present'] is True
    assert s['spool_id'] == 42
    assert s['uid'] == '04AABBCC'
    assert s['resolution'] == 'uid_lookup'


def test_get_status_metadata_direct():
    gate = _status_gate()
    gate._state.current_uid = '04AABBCC'
    gate._state.current_spool = DIRECT_METADATA_SPOOL
    s = gate.get_status()
    assert s['tag_present'] is True
    assert s['spool_id'] == -1
    assert s['resolution'] == 'metadata_direct'


def test_get_status_uid_only():
    gate = _status_gate()
    gate._state.current_uid = '04AABBCC'
    gate._state.current_spool = None
    gate._state.current_tag = CurrentTag(uid='04AABBCC', meta={})
    gate._state.current_tag.resolution = {'path': 'unresolved'}
    s = gate.get_status()
    assert s['tag_present'] is True
    assert s['spool_id'] == -1
    assert s['resolution'] == 'unresolved'


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
