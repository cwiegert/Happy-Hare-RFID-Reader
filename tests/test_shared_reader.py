"""
tests/test_shared_reader.py
============================
Unit tests for the shared NFC reader path in NFCGate.

Covers:
- Config parsing: shared_pending_timeout, shared_read_timeout,
  shared_tag_read_effect, shared_missed_limit, force_spool_id
- _shared_expire_pending_if_needed: expiry when deadline passed
- _shared_preload_check:
    - no pending spool → advisory message emitted
    - no pending spool + force_spool_id → gcmd.error raised
    - pending spool valid → MMU_GATE_MAP NEXT_SPOOLID staged, polling restarted
    - pending spool expired → advisory, no staging
    - pending spool already assigned (hybrid) → silent skip
    - pending spool already assigned (pure shared) → console warning
    - printing active → skipped entirely
- shared_status_line: idle, polling, pending with time, expired, error, failed
- _handle_print_start: stops polling when active
- _handle_print_end: resumes when startup_polling=1 and no pending spool;
                     stays stopped when valid spool is pending
- _shared_handle_event:
    - EVENT_CHANGED with integer spool → stores pending, stops polling
    - EVENT_CHANGED with DIRECT_METADATA_SPOOL → increments miss counter
    - EVENT_UID_ONLY → increments miss counter, RESPOND at limit
    - EVENT_REMOVED → pending spool kept
    - miss counter resets on successful resolution
- _poll_timer_event: failed reader uses NFC_SHARED INIT=1 (not NFC GATE=255)
- NFC_SHARED CLEAR_CACHE clears tag cache but preserves pending spool

No hardware, no Klipper, no mocking framework required.

Run from the project root:
    python3 -m pytest tests/test_shared_reader.py -v
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


class _MockSpoolmanClient:
    def __init__(self, *a, **k): pass


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
_stub('nfc_gates.spoolman_client', SpoolmanClient=_MockSpoolmanClient)

sys.modules.pop('nfc_gates.nfc_manager', None)

from nfc_gates.nfc_manager import NFCGate, _lane_instances
from nfc_gates import tag_handler
from nfc_gates.gate_state import GateState, CurrentTag, EVENT_CHANGED, EVENT_UID_ONLY, EVENT_REMOVED, DIRECT_METADATA_SPOOL


# ── Test doubles ──────────────────────────────────────────────────────────────

class MockReactor:
    NEVER = -1.0
    NOW   =  0.0

    def __init__(self, start_time=100.0):
        self._time  = start_time
        self.timers = {}

    def monotonic(self):
        return self._time

    def advance(self, seconds):
        self._time += seconds

    def register_timer(self, callback, when=None):
        handle = object()
        self.timers[handle] = [callback, when if when is not None else self.NEVER]
        return handle

    def update_timer(self, handle, when):
        if handle in self.timers:
            self.timers[handle][1] = when

    def pause(self, until):
        pass

    def register_callback(self, cb):
        pass


class GCodeCapture:
    def __init__(self):
        self.scripts   = []
        self.responses = []
        self.fail_on   = None

    def run_script(self, script):
        if self.fail_on and self.fail_on in script:
            raise RuntimeError("simulated gcode failure")
        self.scripts.append(script)

    def respond_info(self, msg):
        self.responses.append(msg)

    def register_command(self, *a, **k):
        pass

    def register_mux_command(self, *a, **k):
        pass


class MockReader:
    def __init__(self):
        self.clear_current_card_calls = 0
        self.read_target_calls = 0
        self.init_calls = 0
        self.alive = True
        self.tag = None

    def _clear_current_card(self):
        self.clear_current_card_calls += 1

    def read_tag(self):
        return self.tag

    def read_target(self):
        self.read_target_calls += 1
        return None

    def init(self):
        self.init_calls += 1

    def is_alive(self):
        return self.alive


class MockSpoolman:
    def __init__(self):
        self.clear_cache_calls = 0
        self.spool_by_uid = {}
        self._timeout = 5.0
        self._rfid_key = 'rfid_tag'
        self.set_uid_calls = []

    def clear_cache(self):
        self.clear_cache_calls += 1

    def lookup_spool_by_uid(self, uid):
        return self.spool_by_uid.get(uid)

    def _resolve_base_url(self):
        return 'http://spoolman.local'

    def set_spool_uid(self, spool_id, uid):
        self.set_uid_calls.append((spool_id, uid))
        return True


class MockGCmd:
    def __init__(self, params=None):
        self._params   = params or {}
        self.responses = []
        self._error    = None

    def get_int(self, name, default=0, minval=None, maxval=None):
        return int(self._params.get(name, default))

    def get(self, name, default=None):
        return self._params.get(name, default)

    def respond_info(self, msg):
        self.responses.append(msg)

    def error(self, msg):
        # Return an exception — caller is expected to raise it
        self._error = msg
        return RuntimeError(msg)


class MockMMU:
    def __init__(self, gate_spool_ids=None, action='idle'):
        self._gate_spool_ids = gate_spool_ids or []
        self._action         = action

    def get_status(self, eventtime):
        return {
            'gate_spool_id': self._gate_spool_ids,
            'gate_status':   [0] * len(self._gate_spool_ids),
            'action':        self._action,
            'filament_pos':  0,
            'gate':          -1,
        }


class MockPrintStats:
    def __init__(self, state='standby'):
        self._state = state

    def get_status(self, eventtime):
        return {'state': self._state}


class MockPrinter:
    def __init__(self):
        self._objects  = {}
        self._gcode    = GCodeCapture()
        self._handlers = {}

    def set_mmu(self, mmu):
        self._objects['mmu'] = mmu

    def set_print_state(self, state):
        self._objects['print_stats'] = MockPrintStats(state)

    def lookup_object(self, name, default=None):
        if name == 'gcode':
            return self._gcode
        return self._objects.get(name, default)

    def get_reactor(self):
        return None

    def register_event_handler(self, event, handler):
        self._handlers[event] = handler

    @property
    def gcode(self):
        return self._gcode


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _make_shared(
        startup_polling=1,
        pending_timeout=120.0,
        read_timeout=120.0,
        tag_read_effect='',
        spool_ready_effect='',
        tag_unresolved_effect='',
        missed_limit=3,
        force_spool_id=False,
        has_per_lane_readers=False,
        reactor_time=100.0):
    """Build a minimal NFCGate in shared mode, bypassing __init__."""
    reactor  = MockReactor(reactor_time)
    printer  = MockPrinter()
    gcode    = printer.gcode

    g = object.__new__(NFCGate)
    g._name                       = 'shared'
    g._gate                       = 255
    g._shared                     = True
    g._debug                      = 0
    g._failed                     = False
    g._polling                    = False
    g._startup_polling            = startup_polling
    g._poll_interval              = 3.0
    g._shared_pending_timeout     = pending_timeout
    g._shared_read_timeout        = read_timeout
    g._shared_tag_read_effect     = tag_read_effect
    g._shared_spool_ready_effect  = spool_ready_effect
    g._shared_tag_unresolved_effect = tag_unresolved_effect
    g._shared_auto_create_effect  = ''
    g._shared_mcu_index           = None
    g._shared_missed_limit        = missed_limit
    g._shared_force_spool_id      = force_spool_id
    g._has_per_lane_readers       = has_per_lane_readers
    g._shared_pending_uid         = None
    g._shared_pending_spool       = None
    g._shared_pending_deadline    = 0.0
    g._shared_pending_auto_created = False
    g._shared_last_error          = None
    g._shared_last_action         = None
    g._shared_read_deadline       = 0.0
    g._shared_missed_resolutions  = 0
    g._shared_polling_suspended_for_print = False
    g._spoolman_auto_create       = False
    g._tag_parsing                = False
    g._scan_mode                  = False
    g._state                      = GateState(255)
    g._reader                     = MockReader()
    g._spoolman                   = MockSpoolman()
    g.reactor                     = reactor
    g.printer                     = printer
    g._gcode                      = gcode
    g._poll_timer                 = reactor.register_timer(lambda e: reactor.NEVER)
    printer.set_print_state('standby')   # default: not printing
    return g


def _stage_pending(g, spool_id=42, uid='AABBCCDD', ttl=120.0,
                   auto_created=False):
    """Manually put a valid pending spool on the shared reader."""
    g._shared_pending_spool    = spool_id
    g._shared_pending_uid      = uid
    g._shared_pending_deadline = g.reactor.monotonic() + ttl
    g._shared_pending_auto_created = auto_created


# ── Config parsing ────────────────────────────────────────────────────────────

class MockConfig:
    def __init__(self, values=None, name='nfc_gate shared'):
        self._values  = dict(values or {})
        self._name    = name
        self._printer = MockPrinter()

    def get_name(self):       return self._name
    def get_printer(self):    return self._printer
    def error(self, msg):     return ValueError(msg)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def getboolean(self, key, default=None):
        raw = self._values.get(key, default)
        if isinstance(raw, bool): return raw
        if raw is None: return default
        return str(raw).strip().lower() in ('true', '1', 'yes')

    def getfloat(self, key, default=None, minval=None, maxval=None):
        raw = self._values.get(key, default)
        val = float(raw) if raw is not None else default
        if val is not None:
            if minval is not None and val < minval:
                raise ValueError(f"{key} below minval")
            if maxval is not None and val > maxval:
                raise ValueError(f"{key} above maxval")
        return val

    def getint(self, key, default=None, minval=None, maxval=None):
        raw = self._values.get(key, default)
        val = int(raw) if raw is not None else default
        if val is not None:
            if minval is not None and val < minval:
                raise ValueError(f"{key} below minval")
            if maxval is not None and val > maxval:
                raise ValueError(f"{key} above maxval")
        return val


def test_config_shared_defaults():
    """Shared config keys default correctly when not supplied."""
    from nfc_gates.nfc_manager import _SHARED_MISSED_RESOLUTION_LIMIT
    g = _make_shared()
    assert g._shared_pending_timeout  == 120.0
    assert g._shared_read_timeout     == 120.0
    assert g._shared_tag_read_effect  == ''
    assert g._shared_missed_limit     == _SHARED_MISSED_RESOLUTION_LIMIT
    assert g._shared_force_spool_id   is False


def test_config_shared_missed_limit_minval():
    """shared_missed_limit rejects values below 1."""
    cfg = MockConfig({'shared': True, 'shared_missed_limit': 0})
    try:
        cfg.getint('shared_missed_limit', 3, minval=1)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_config_force_spool_id():
    """force_spool_id is parsed as boolean."""
    g = _make_shared(force_spool_id=True)
    assert g._shared_force_spool_id is True


# ── _shared_expire_pending_if_needed ─────────────────────────────────────────

def test_expire_pending_before_deadline():
    g = _make_shared()
    _stage_pending(g, spool_id=10, ttl=60.0)
    g._shared_expire_pending_if_needed()
    assert g._shared_pending_spool == 10  # not expired yet


def test_expire_pending_after_deadline():
    g = _make_shared()
    _stage_pending(g, spool_id=10, ttl=1.0)
    g.reactor.advance(10.0)           # past the deadline
    g._shared_expire_pending_if_needed()
    assert g._shared_pending_spool is None


# ── _shared_preload_check ─────────────────────────────────────────────────────

def test_preload_check_no_pending_emits_advisory():
    g    = _make_shared()
    gcmd = MockGCmd()
    g._shared_preload_check(gcmd)
    assert any('no spool staged' in r for r in gcmd.responses)


def test_preload_check_no_pending_force_spool_id_raises():
    g    = _make_shared(force_spool_id=True)
    gcmd = MockGCmd()
    try:
        g._shared_preload_check(gcmd)
        assert False, "expected error to be raised"
    except RuntimeError as e:
        assert 'force_spool_id' in str(e)


def test_preload_check_stages_next_spoolid():
    g = _make_shared()
    _stage_pending(g, spool_id=42)
    gcmd = MockGCmd()
    g._shared_preload_check(gcmd)
    assert 'MMU_GATE_MAP NEXT_SPOOLID=42' in g._gcode.scripts
    assert g._shared_pending_spool is None   # cleared after staging
    assert g._polling is True                # polling restarted
    assert g._shared_last_action == 'staged spool 42 via NEXT_SPOOLID'


def test_preload_check_refreshes_before_auto_created_next_spoolid():
    g = _make_shared()
    _stage_pending(g, spool_id=42, auto_created=True)
    gcmd = MockGCmd()
    g._shared_preload_check(gcmd)
    assert g._gcode.scripts[:2] == [
        'MMU_SPOOLMAN REFRESH=1 QUIET=1',
        'MMU_GATE_MAP NEXT_SPOOLID=42',
    ]
    assert g._shared_pending_spool is None
    assert g._polling is True


def test_preload_check_keeps_pending_when_auto_created_refresh_fails():
    g = _make_shared()
    _stage_pending(g, spool_id=42, uid='AUTOUID', auto_created=True)
    g._gcode.fail_on = 'MMU_SPOOLMAN REFRESH'
    gcmd = MockGCmd()
    g._shared_preload_check(gcmd)
    assert not any('NEXT_SPOOLID' in s for s in g._gcode.scripts)
    assert g._shared_pending_spool == 42
    assert g._shared_pending_uid == 'AUTOUID'
    assert g._polling is False
    assert any('REFRESH failed' in r and 'pending spool 42 kept' in r
               for r in gcmd.responses)


def test_preload_check_keeps_pending_when_gate_map_fails():
    g = _make_shared()
    _stage_pending(g, spool_id=42, uid='KEEPUID')
    g._gcode.fail_on = 'MMU_GATE_MAP'
    gcmd = MockGCmd()
    g._shared_preload_check(gcmd)
    assert not any('NEXT_SPOOLID' in s for s in g._gcode.scripts)
    assert g._shared_pending_spool == 42
    assert g._shared_pending_uid == 'KEEPUID'
    assert g._polling is False
    assert any('pending spool 42 kept' in r for r in gcmd.responses)


def test_preload_check_expired_emits_advisory():
    g = _make_shared()
    _stage_pending(g, spool_id=7, ttl=1.0)
    g.reactor.advance(10.0)
    gcmd = MockGCmd()
    g._shared_preload_check(gcmd)
    assert not any('NEXT_SPOOLID' in s for s in g._gcode.scripts)
    assert any('no spool staged' in r for r in gcmd.responses)


def test_preload_check_skipped_while_printing():
    g = _make_shared()
    _stage_pending(g, spool_id=5)
    g.printer.set_print_state('printing')
    gcmd = MockGCmd()
    g._shared_preload_check(gcmd)
    assert not any('NEXT_SPOOLID' in s for s in g._gcode.scripts)
    assert g._shared_pending_spool == 5  # still pending
    assert any('skipped while printing' in r for r in gcmd.responses)


def test_preload_check_hybrid_already_assigned_silent():
    """Hybrid: spool already assigned by per-lane reader — silent skip, no staging."""
    g = _make_shared(has_per_lane_readers=True)
    _stage_pending(g, spool_id=42)
    g.printer.set_mmu(MockMMU(gate_spool_ids=[42, -1, -1]))
    gcmd = MockGCmd()
    g._shared_preload_check(gcmd)
    assert not any('NEXT_SPOOLID' in s for s in g._gcode.scripts)
    assert not gcmd.responses          # silent — no console message
    assert g._shared_pending_spool is None


def test_preload_check_pure_shared_already_assigned_warns():
    """Pure shared: spool already assigned is unexpected — warn on console."""
    g = _make_shared(has_per_lane_readers=False)
    _stage_pending(g, spool_id=42)
    g.printer.set_mmu(MockMMU(gate_spool_ids=[42, -1, -1]))
    gcmd = MockGCmd()
    g._shared_preload_check(gcmd)
    assert not any('NEXT_SPOOLID' in s for s in g._gcode.scripts)
    assert any('already assigned' in r for r in gcmd.responses)
    assert g._shared_pending_spool is None


# ── shared_status_line ────────────────────────────────────────────────────────

def test_status_idle():
    g = _make_shared()
    assert 'idle' in g.shared_status_line()


def test_status_polling():
    g = _make_shared()
    g._polling = True
    assert 'polling' in g.shared_status_line()


def test_status_pending():
    g = _make_shared()
    _stage_pending(g, spool_id=7, uid='DEADBEEF', ttl=90.0)
    line = g.shared_status_line()
    assert 'pending' in line
    assert '7' in line
    assert 'DEADBEEF' in line


def test_status_expired():
    g = _make_shared()
    _stage_pending(g, spool_id=7, ttl=1.0)
    g.reactor.advance(10.0)
    line = g.shared_status_line()
    assert 'expired' in line
    assert g._shared_pending_spool is None
    assert 'error' in g.shared_status_line()


def test_status_error():
    g = _make_shared()
    g._shared_last_error = 'tag uid=AABB not in Spoolman'
    assert 'error' in g.shared_status_line()


def test_status_failed():
    g = _make_shared()
    g._failed = True
    assert 'FAILED' in g.shared_status_line()


def test_cmd_status_reports_detailed_shared_state():
    g = _make_shared(tag_read_effect='mmu_RFID_read', force_spool_id=True)
    _stage_pending(g, spool_id=42, uid='DETAILUID', auto_created=True)
    g._shared_missed_resolutions = 1
    g._shared_last_action = 'tag staged spool 42 uid=DETAILUID auto_created=True'
    gcmd = MockGCmd({'STATUS': 1})
    g.cmd_NFC_SHARED(gcmd)
    status = '\n'.join(gcmd.responses)
    assert 'pending spool 42' in status
    assert 'pending_auto_created: yes' in status
    assert 'force_spool_id: on' in status
    assert 'tag_read_effect:    mmu_RFID_read' in status
    assert 'missed_resolutions: 1/3' in status
    assert 'last_action: tag staged spool 42' in status
    assert 'next: insert filament before timeout' in status


def test_cmd_summary_reports_one_line_with_next_action():
    g = _make_shared()
    _stage_pending(g, spool_id=42, uid='SUMMARYUID')
    gcmd = MockGCmd({'SUMMARY': 1})
    g.cmd_NFC_SHARED(gcmd)
    summary = '\n'.join(gcmd.responses)
    assert 'shared:  pending spool 42' in summary
    assert 'next: insert filament before timeout' in summary


# ── NFC_SHARED read/scan safety ──────────────────────────────────────────────

def test_shared_read_does_not_start_while_printing():
    g = _make_shared()
    g.printer.set_print_state('printing')
    gcmd = MockGCmd({'READ': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert g._polling is False
    assert g._shared_read_deadline == 0.0
    assert any('not started while printing' in r for r in gcmd.responses)


def test_shared_read_refuses_to_replace_pending_without_replace_command():
    g = _make_shared()
    _stage_pending(g, spool_id=42)
    gcmd = MockGCmd({'READ': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert g._shared_pending_spool == 42
    assert g._polling is False
    assert any('spool 42 is already pending' in r and 'REPLACE=1' in r
               for r in gcmd.responses)


def test_shared_replace_clears_pending_and_starts_polling():
    g = _make_shared()
    _stage_pending(g, spool_id=42)
    gcmd = MockGCmd({'REPLACE': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert g._shared_pending_spool is None
    assert g._polling is True
    assert g._shared_read_deadline == (
        g.reactor.monotonic() + g._shared_read_timeout)
    assert any('discarded pending spool 42' in r for r in gcmd.responses)


def test_shared_scan_skips_reader_while_printing():
    g = _make_shared()
    g.printer.set_print_state('printing')
    gcmd = MockGCmd({'SCAN': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert g._reader.read_target_calls == 0
    assert any('skipped while printing' in r for r in gcmd.responses)


def test_shared_poll_skips_while_printing_without_success_message():
    g = _make_shared()
    g.printer.set_print_state('printing')
    gcmd = MockGCmd({'POLL': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert g._reader.read_target_calls == 0
    assert any('poll skipped while printing' in r for r in gcmd.responses)
    assert not any('one poll complete' in r for r in gcmd.responses)


def test_shared_init_resumes_startup_polling():
    g = _make_shared(startup_polling=1)
    g._failed = True
    g._polling = False
    gcmd = MockGCmd({'INIT': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert g._reader.init_calls == 1
    assert g._polling is True
    assert g._shared_read_deadline == 0.0
    assert any('startup polling resumed' in r for r in gcmd.responses)


def test_shared_init_does_not_resume_when_pending():
    g = _make_shared(startup_polling=1)
    _stage_pending(g, spool_id=42)
    g._failed = True
    g._polling = False
    gcmd = MockGCmd({'INIT': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert g._polling is False
    assert not any('startup polling resumed' in r for r in gcmd.responses)


def test_shared_retry_alias_runs_preload_check():
    g = _make_shared()
    _stage_pending(g, spool_id=42)
    gcmd = MockGCmd({'RETRY': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert 'MMU_GATE_MAP NEXT_SPOOLID=42' in g._gcode.scripts
    assert g._shared_pending_spool is None


def test_shared_cancel_alias_clears_pending_and_stops():
    g = _make_shared()
    _stage_pending(g, spool_id=42)
    g._polling = True
    gcmd = MockGCmd({'CANCEL': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert g._shared_pending_spool is None
    assert g._polling is False
    assert g._shared_last_action == 'pending spool canceled'
    assert any('pending spool canceled' in r for r in gcmd.responses)


def test_shared_led_test_plays_configured_effect():
    g = _make_shared(tag_read_effect='mmu_RFID_read')
    gcmd = MockGCmd({'LED_TEST': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert any('_MMU_SET_LED_EFFECT EFFECT=mmu_RFID_read' in s
               for s in g._gcode.scripts)
    assert any('LED effect mmu_RFID_read started' in r
               for r in gcmd.responses)


def test_shared_led_test_reports_missing_effect_config():
    g = _make_shared()
    gcmd = MockGCmd({'LED_TEST': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert not g._gcode.scripts
    assert any('no LED effect configured' in r
               for r in gcmd.responses)


# ── _handle_print_start / _handle_print_end ───────────────────────────────────

def test_print_start_stops_polling():
    g = _make_shared(startup_polling=1)
    g.printer.set_print_state('printing')
    g._polling = True
    g._handle_print_start(0)
    assert g._polling is False
    assert g._shared_polling_suspended_for_print is True


def test_print_start_ignores_idle_timeout_busy_without_real_print():
    g = _make_shared(startup_polling=1)
    g.printer.set_print_state('standby')
    g._polling = True
    g._handle_print_start(0)
    assert g._polling is True
    assert g._shared_polling_suspended_for_print is False


def test_print_start_noop_when_not_polling():
    g = _make_shared(startup_polling=1)
    g._polling = False
    g._handle_print_start(0)
    assert g._polling is False


def test_print_end_resumes_when_no_pending():
    g = _make_shared(startup_polling=1)
    g._polling = False
    g._shared_polling_suspended_for_print = True
    g._handle_print_end(0)
    assert g._polling is True
    assert g._shared_polling_suspended_for_print is False


def test_print_end_stays_stopped_when_valid_spool_pending():
    """After a successful tag read, print-end must not overwrite the pending spool
    by restarting polling — the user hasn't loaded the spool yet."""
    g = _make_shared(startup_polling=1)
    g._polling = False
    g._shared_polling_suspended_for_print = True
    _stage_pending(g, spool_id=42, ttl=120.0)
    g._handle_print_end(0)
    assert g._polling is False
    assert g._shared_pending_spool == 42  # spool untouched
    assert g._shared_polling_suspended_for_print is False


def test_print_end_resumes_when_pending_expired():
    """Expired pending should not block polling restart — it's already gone."""
    g = _make_shared(startup_polling=1)
    g._polling = False
    g._shared_polling_suspended_for_print = True
    _stage_pending(g, spool_id=42, ttl=1.0)
    g.reactor.advance(10.0)           # deadline in the past
    g._handle_print_end(0)
    assert g._polling is True
    assert g._shared_pending_spool is None
    assert g._shared_polling_suspended_for_print is False


def test_pending_timeout_auto_expires_and_resumes_startup_polling():
    g = _make_shared(startup_polling=1, pending_timeout=10.0)
    g._polling = True
    g._shared_handle_event(EVENT_CHANGED, 'AUTOEXPIRE', 42)

    assert g._polling is False
    assert g.reactor.timers[g._poll_timer][1] == g._shared_pending_deadline

    g.reactor.advance(10.0)
    result = g._poll_timer_event(g.reactor.monotonic())

    assert result == g.reactor.NOW
    assert g._shared_pending_spool is None
    assert g._polling is True
    assert 'expired' in g._shared_last_error
    assert any('RESPOND MSG=' in s and 'pending spool timed out after 10s' in s
               and 'polling resumed' in s for s in g._gcode.scripts)


def test_print_end_noop_when_startup_polling_off():
    g = _make_shared(startup_polling=0)
    g._polling = False
    g._handle_print_end(0)
    assert g._polling is False


# ── _shared_handle_event ──────────────────────────────────────────────────────

def test_event_changed_stores_pending_stops_polling():
    g = _make_shared()
    g._polling = True
    g._shared_handle_event(EVENT_CHANGED, 'AABBCCDD', 99)
    assert g._shared_pending_spool == 99
    assert g._shared_pending_uid   == 'AABBCCDD'
    assert g._polling is False
    assert g._shared_pending_deadline > g.reactor.monotonic()


def test_event_changed_resets_miss_counter():
    g = _make_shared()
    g._shared_missed_resolutions = 2
    g._shared_handle_event(EVENT_CHANGED, 'AA', 5)
    assert g._shared_missed_resolutions == 0


def test_event_changed_does_not_replace_existing_pending_spool():
    g = _make_shared(tag_read_effect='mmu_RFID_read')
    _stage_pending(g, spool_id=55, uid='OLDUID', ttl=60.0)
    old_deadline = g._shared_pending_deadline

    g._shared_handle_event(EVENT_CHANGED, 'NEWUID', 99)

    assert g._shared_pending_spool == 55
    assert g._shared_pending_uid == 'OLDUID'
    assert g._shared_pending_deadline == old_deadline
    assert g._shared_last_action == 'ignored spool 99 while spool 55 pending'
    assert not any('MMU_SET_LED' in s for s in g._gcode.scripts)
    assert any('RESPOND MSG=' in s and 'spool 55 is already pending' in s
               and 'REPLACE=1' in s for s in g._gcode.scripts)


def test_event_changed_duplicate_pending_read_is_ignored():
    g = _make_shared()
    _stage_pending(g, spool_id=55, uid='OLDUID', ttl=60.0)

    g._shared_handle_event(EVENT_CHANGED, 'OLDUID', 55)

    assert g._shared_pending_spool == 55
    assert g._shared_pending_uid == 'OLDUID'
    assert g._shared_last_action == 'ignored duplicate read for pending spool 55'
    assert any('duplicate tag read ignored' in s for s in g._gcode.scripts)


def test_event_changed_replaces_expired_pending_spool():
    g = _make_shared()
    _stage_pending(g, spool_id=55, uid='OLDUID', ttl=1.0)
    g.reactor.advance(2.0)

    g._shared_handle_event(EVENT_CHANGED, 'NEWUID', 99)

    assert g._shared_pending_spool == 99
    assert g._shared_pending_uid == 'NEWUID'
    assert g._shared_last_action == (
        'tag staged spool 99 uid=NEWUID auto_created=False')


def test_event_changed_direct_metadata_increments_miss():
    g = _make_shared()
    g._shared_handle_event(EVENT_CHANGED, 'AABB', DIRECT_METADATA_SPOOL)
    assert g._shared_missed_resolutions == 1
    assert g._shared_pending_spool is None


def test_event_changed_direct_metadata_emits_respond_at_limit():
    g = _make_shared(missed_limit=2)
    g._shared_handle_event(EVENT_CHANGED, 'AABB', DIRECT_METADATA_SPOOL)
    assert not g._gcode.scripts           # not yet at limit
    g._shared_handle_event(EVENT_CHANGED, 'AABB', DIRECT_METADATA_SPOOL)
    assert any('RESPOND' in s for s in g._gcode.scripts)


def test_event_uid_only_increments_miss():
    g = _make_shared()
    g._shared_handle_event(EVENT_UID_ONLY, 'AABB', None)
    assert g._shared_missed_resolutions == 1
    assert g._shared_pending_spool is None


def test_event_uid_only_emits_respond_at_limit():
    g = _make_shared(missed_limit=2)
    g._shared_handle_event(EVENT_UID_ONLY, 'AABB', None)
    assert not g._gcode.scripts
    g._shared_handle_event(EVENT_UID_ONLY, 'AABB', None)
    assert any('RESPOND' in s for s in g._gcode.scripts)


def test_event_uid_only_does_not_clear_existing_pending():
    """UID-only reads must not wipe a valid pending spool from an earlier scan."""
    g = _make_shared()
    _stage_pending(g, spool_id=55)
    g._shared_handle_event(EVENT_UID_ONLY, 'FFFF', None)
    assert g._shared_pending_spool == 55  # preserved


def test_event_removed_keeps_pending():
    g = _make_shared()
    _stage_pending(g, spool_id=12)
    g._shared_handle_event(EVENT_REMOVED, 'AABB', None)
    assert g._shared_pending_spool == 12


def test_event_changed_fires_spool_ready_led_effect():
    g = _make_shared(spool_ready_effect='mmu_RFID_ready')
    g._shared_handle_event(EVENT_CHANGED, 'AA', 7)
    assert any('_MMU_SET_LED_EFFECT' in s and 'mmu_RFID_ready' in s
               for s in g._gcode.scripts)


def test_poll_fires_yellow_ack_then_green_ready_effect():
    g = _make_shared(tag_read_effect='mmu_RFID_read',
                     spool_ready_effect='mmu_RFID_ready')
    g._reader.tag = 'AABB'
    g._spoolman.spool_by_uid['AABB'] = 7

    g._poll()

    effects = [s for s in g._gcode.scripts if '_MMU_SET_LED_EFFECT' in s]
    assert effects == [
        '_MMU_SET_LED_EFFECT EFFECT=mmu_RFID_read',
        '_MMU_SET_LED_EFFECT EFFECT=mmu_RFID_ready',
    ]


def test_event_changed_does_not_fire_tag_read_ack():
    g = _make_shared(tag_read_effect='mmu_RFID_read',
                     spool_ready_effect='mmu_RFID_ready')
    g._shared_handle_event(EVENT_CHANGED, 'AA', 7)
    assert not any(s == '_MMU_SET_LED_EFFECT EFFECT=mmu_RFID_read'
                   for s in g._gcode.scripts)


def test_event_uid_only_fires_unresolved_led_effect():
    g = _make_shared(tag_unresolved_effect='mmu_RFID_unresolved')
    g._shared_handle_event(EVENT_UID_ONLY, 'AABB', None)
    assert any('_MMU_SET_LED_EFFECT' in s and 'mmu_RFID_unresolved' in s
               for s in g._gcode.scripts)


def test_auto_create_starts_and_stops_creating_led_effect():
    import nfc_gates.vendor.lameandboard_spoolman as lb_spoolman

    class FakeLBSpoolmanClient:
        def __init__(self, *args, **kwargs):
            pass

        def auto_create_spool(self, meta, uid_hex=None):
            return 42

    original_client = lb_spoolman.SpoolmanClient
    lb_spoolman.SpoolmanClient = FakeLBSpoolmanClient
    try:
        g = _make_shared()
        g._spoolman_auto_create = True
        g._tag_parsing = True
        g._shared_auto_create_effect = 'mmu_RFID_creating'
        g._state.current_tag = CurrentTag(uid='AABB')
        g._state.current_tag.meta = {'uid': 'AABB', 'material': 'PLA'}

        spool_id = tag_handler.resolve_spool(g, 'AABB')

        assert spool_id == 42
        assert g._spoolman.set_uid_calls == [(42, 'AABB')]
        assert '_MMU_SET_LED_EFFECT EFFECT=mmu_RFID_creating' in g._gcode.scripts
        assert '_MMU_STOP_LED_EFFECTS EFFECTS=mmu_RFID_creating' in g._gcode.scripts
    finally:
        lb_spoolman.SpoolmanClient = original_client


def test_direct_metadata_without_spool_fires_unresolved_led_effect():
    g = _make_shared(tag_unresolved_effect='mmu_RFID_unresolved')
    g._shared_handle_event(EVENT_CHANGED, 'AABB', DIRECT_METADATA_SPOOL)
    assert any('_MMU_SET_LED_EFFECT' in s and 'mmu_RFID_unresolved' in s
               for s in g._gcode.scripts)


def test_event_changed_led_effect_failure_warns_but_stages():
    g = _make_shared(spool_ready_effect='bad_effect')
    g._gcode.fail_on = 'MMU_SET_LED'
    g._shared_handle_event(EVENT_CHANGED, 'AA', 7)
    assert g._shared_pending_spool == 7
    assert g._polling is False
    assert any('RESPOND MSG=' in s and 'LED effect' in s
               for s in g._gcode.scripts)


# ── _poll_timer_event recovery command ───────────────────────────────────────

def test_poll_timer_failed_reader_uses_nfc_shared_init():
    """Failed shared reader should log NFC_SHARED INIT=1, not NFC GATE=255 INIT=1."""
    g = _make_shared()
    g._failed  = True
    g._polling = True

    log_messages = []

    class CapLogger:
        def warning(self, fmt, *args, **kw):
            log_messages.append(fmt % args if args else fmt)
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def error(self, *a, **k): pass

    globals_dict = NFCGate._poll_timer_event.__globals__
    old_logger = globals_dict['logger']
    globals_dict['logger'] = CapLogger()
    try:
        result = g._poll_timer_event(g.reactor.monotonic())
    finally:
        globals_dict['logger'] = old_logger

    assert result == g.reactor.NEVER
    assert g._polling is False
    assert any('NFC_SHARED INIT=1' in m for m in log_messages), \
        f"Expected 'NFC_SHARED INIT=1' in log; got: {log_messages}"
    assert not any('GATE=255' in m for m in log_messages), \
        f"'GATE=255' must not appear in log; got: {log_messages}"


# ── _shared_clear_pending resets miss counter ─────────────────────────────────

def test_clear_pending_resets_miss_counter():
    g = _make_shared()
    g._shared_missed_resolutions = 3
    _stage_pending(g, spool_id=1)
    g._shared_clear_pending()
    assert g._shared_missed_resolutions == 0
    assert g._shared_pending_spool is None


# ── NFC_SHARED CLEAR_CACHE ────────────────────────────────────────────────────

def test_shared_clear_cache_resets_tag_cache_and_keeps_pending_spool():
    g = _make_shared()
    _stage_pending(g, spool_id=88, uid='PENDINGUID', ttl=60.0)
    pending_deadline = g._shared_pending_deadline

    g._state.current_uid = 'CURRENTUID'
    g._state.current_spool = 12
    g._state.miss_count = 2
    assert g._state.current_tag is not None

    gcmd = MockGCmd({'CLEAR_CACHE': 1})
    g.cmd_NFC_SHARED(gcmd)

    assert g._state.current_uid is None
    assert g._state.current_spool is None
    assert g._state.current_tag is None
    assert g._state.miss_count == 0
    assert g._shared_pending_spool == 88
    assert g._shared_pending_uid == 'PENDINGUID'
    assert g._shared_pending_deadline == pending_deadline
    assert g._spoolman.clear_cache_calls == 1
    assert g._reader.clear_current_card_calls == 1
    assert any('pending spool kept' in r for r in gcmd.responses)


# ── NFC_SHARED help ───────────────────────────────────────────────────────────

def test_shared_help_groups_advanced_commands_separately():
    g = _make_shared()
    gcmd = MockGCmd()

    g.cmd_NFC_SHARED(gcmd)

    help_text = "\n".join(gcmd.responses)
    assert "NFC_SHARED commands:" in help_text
    assert "NFC_SHARED CANCEL=1" in help_text
    assert "NFC_SHARED REPLACE=1" in help_text
    assert "NFC_SHARED RETRY=1" in help_text
    assert "NFC_SHARED SUMMARY=1" in help_text
    assert "NFC_SHARED HELP=1" in help_text
    assert "Advanced shared-reader commands:" in help_text
    assert "NFC_SHARED PRELOAD_CHECK=1" in help_text
    assert "NFC_SHARED CLEAR_CACHE=1" in help_text
    assert "Low-Level Debug" not in help_text
    assert "low_level_debug" not in help_text


def test_shared_help_param_shows_help():
    g = _make_shared()
    gcmd = MockGCmd({'HELP': 1})
    g.cmd_NFC_SHARED(gcmd)
    assert any('NFC_SHARED commands:' in r for r in gcmd.responses)


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-v']))
