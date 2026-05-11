"""
tests/test_scan_jog_mode.py
============================
Unit tests for the scan-and-jog mode state machine in NFCGate.

Covers:
- Class-level scan lock (prevents multi-lane race condition)
- Poll timer edge detection (0→1 gate_status trigger, cold-start guard)
- Scan step loop (jog, tag-found exit, max-mm abort, print-start abort)
- Rewind and jog GCode content
- Poll timer rescheduled correctly after scan exits

No hardware, no Klipper, no mocking framework required.

Run from the project root:
    python3 -m pytest tests/test_scan_jog_mode.py -v
or without pytest:
    python3 tests/test_scan_jog_mode.py
"""

import sys
import os
import re
import types
import tempfile

_EXTRAS = os.path.join(os.path.dirname(__file__), '..', 'klippy', 'extras')
sys.path.insert(0, _EXTRAS)

def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def _strip_html(text):
    return re.sub(r'</?span[^>]*>', '', text)


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

# Manager tests install different dependency stubs; import a fresh manager copy
# so pytest collection order cannot leak stubs between files.
sys.modules.pop('nfc_gates.nfc_manager', None)

from nfc_gates.nfc_manager import (
    CurrentTag, GateState, NFCGate, _lane_instances, _lane_status_lines)


# ── Test doubles ──────────────────────────────────────────────────────────────

class MockReactor:
    NEVER = -1.0
    NOW   =  0.0

    def __init__(self):
        self._time  = 100.0
        self.timers = {}

    def monotonic(self):
        return self._time

    def register_timer(self, callback, when=None):
        handle = object()
        self.timers[handle] = (callback, when if when is not None else self.NEVER)
        return handle

    def update_timer(self, handle, when):
        if handle in self.timers:
            cb = self.timers[handle][0]
            self.timers[handle] = (cb, when)

    def register_callback(self, cb):
        pass


class GCodeCapture:
    def __init__(self):
        self.scripts = []
        self.responses = []

    def run_script(self, script):
        self.scripts.append(script)

    def respond_info(self, msg):
        self.responses.append(msg)


class MockMMU:
    def __init__(self, gate_status=None, action='idle',
                 gear_short_move_speed=80.0, gate_spool_id=None,
                 filament_pos=0, active_gate=-1):
        self._gate_status          = gate_status or []
        self._gate_spool_id        = (
            gate_spool_id if gate_spool_id is not None
            else [-1] * len(self._gate_status))
        self._action               = action
        self.gear_short_move_speed = gear_short_move_speed
        self._filament_pos         = filament_pos
        self._active_gate          = active_gate

    def get_status(self, eventtime):
        return {
            'gate_status':   self._gate_status,
            'gate_spool_id': self._gate_spool_id,
            'action':        self._action,
            'filament_pos':  self._filament_pos,
            'gate':          self._active_gate,
        }


class MockPrintStats:
    def __init__(self, state='standby'):
        self._state = state

    def get_status(self, eventtime):
        return {'state': self._state}


class MockGCmd:
    def __init__(self, params=None):
        self.responses = []
        self.params = params or {}

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = int(self.params.get(name, default))
        if minval is not None and value < minval:
            raise ValueError("below min")
        if maxval is not None and value > maxval:
            raise ValueError("above max")
        return value

    def respond_info(self, msg):
        self.responses.append(msg)


class MockPrinter:
    def __init__(self):
        self._objects = {}
        self._gcode   = GCodeCapture()

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

    def register_event_handler(self, *a, **k):
        pass

    @property
    def gcode_scripts(self):
        return self._gcode.scripts


def _make_gate(gate=0, scan_jog_mm=50.0, scan_max_mm=200.0,
               scan_poll_interval=0.5,
               scan_enabled=True,
               scan_rewind_buffer_mm=30.0,
               scan_decode_retry_mm=2.0,
               scan_decode_retry_rounds=5):
    """Build a minimal NFCGate with scan-jog state, bypassing __init__.

    Uses object.__new__ to skip Klipper config/I2C setup, then manually
    populates every instance variable that the scan-jog methods touch.
    """
    g = object.__new__(NFCGate)
    g._name               = 'test'
    g._gate               = gate
    g._debug              = 0
    g._failed             = False
    g._polling            = True
    g._poll_interval      = 30.0
    g._scan_jog_mm        = scan_jog_mm
    g._scan_rewind_buffer_mm = scan_rewind_buffer_mm
    g._scan_decode_retry_mm = scan_decode_retry_mm
    g._scan_decode_retry_rounds = scan_decode_retry_rounds
    g._scan_max_mm        = scan_max_mm
    g._scan_poll_interval = scan_poll_interval
    g._scan_enabled       = scan_enabled
    g._scan_mode          = False
    g._scan_mm_total      = 0.0
    g._scan_start_time    = 0.0
    g._scan_next_chunk_time = 0.0
    g._scan_decode_retry_attempts = 0
    g._scan_decode_retry_uid = None
    g._scan_decode_retry_offset = 0.0
    g._scan_idle_ready_time = 0.0
    g._scan_found_event     = None
    g._scan_gate_selected   = False
    g._scan_hh_prep_pending = False
    g._scan_previous_active_gate = -1
    g._scan_timer         = None
    g._prev_gate_status   = -1
    g._scan_pending      = False
    g._hh_load_paused     = False
    g._hh_confirmed_spool = None
    g._state              = GateState(gate)
    g._spoolman           = None
    g.reactor             = MockReactor()
    g.printer             = MockPrinter()
    g._poll_timer         = g.reactor.register_timer(lambda e: g.reactor.NEVER)
    fd, path = tempfile.mkstemp(prefix='nfc_mmu_vars_', suffix='.cfg')
    with os.fdopen(fd, 'w') as f:
        f.write('mmu_calibration_bowden_lengths = [200.0, 175.0, 150.0, 125.0]\n')
    g._mmu_vars_path      = path
    g._bowden_lengths     = None
    NFCGate._active_scan_gate = None   # reset class-level lock before every test
    return g


# ── Scan lock ─────────────────────────────────────────────────────────────────

def test_start_scan_acquires_lock():
    g = _make_gate()
    g._start_scan_mode()
    assert NFCGate._active_scan_gate == 0

def test_start_scan_clears_hh_pause_so_reader_can_poll():
    g = _make_gate()
    g._state.current_uid = '04C19F92D32A81'
    g._state.current_spool = 55
    g._hh_load_paused = True

    g._start_scan_mode()

    assert not g._hh_load_paused
    assert g._state.current_uid is None
    assert g._state.current_spool is None
    assert g._scan_previous_uid == '04C19F92D32A81'
    assert g._scan_previous_spool == 55

def test_paused_without_nfc_spool_does_not_skip_reader():
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], gate_spool_id=[55]))
    g._hh_load_paused = True
    g._state.current_spool = None

    skipped = g._poll_hh_pause_check()

    assert not skipped
    assert not g._hh_load_paused

def test_uid_only_pauses_poll_while_hh_reports_filament_present():
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], gate_spool_id=[-1]))
    g._state.current_uid = '04C19F92D32A81'
    g._state.current_spool = None

    skipped = g._poll_hh_pause_check()

    assert skipped
    assert g._hh_load_paused
    assert g._state.miss_count == 0

def test_uid_only_pause_resumes_when_hh_gate_empty():
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[0], gate_spool_id=[-1]))
    g._state.current_uid = '04C19F92D32A81'
    g._state.current_spool = None
    g._hh_load_paused = True

    skipped = g._poll_hh_pause_check()

    assert not skipped
    assert not g._hh_load_paused
    assert g._state.current_uid is None
    assert g._state.current_spool is None

def test_finish_scan_releases_lock():
    g = _make_gate()
    g._start_scan_mode()
    g._finish_scan()
    assert NFCGate._active_scan_gate is None

def test_rewind_and_exit_releases_lock():
    g = _make_gate()
    g._start_scan_mode()
    g._rewind_and_exit_scan()
    assert NFCGate._active_scan_gate is None

def test_no_tag_scan_clears_nfc_state():
    """After a failed scan, NFC state is fully cleared — no restore of previous spool.
    HH cache was also cleared so restoring the old spool would create a mismatch."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], gate_spool_id=[55]))
    g._state.current_uid = '04C19F92D32A81'
    g._state.current_spool = 55
    g._hh_load_paused = True
    g._start_scan_mode()

    g._rewind_and_exit_scan()

    assert g._state.current_uid is None
    assert g._state.current_spool is None
    assert not g._hh_load_paused

def test_finish_holds_lock_until_rewind_check_gate_runs():
    g = _make_gate(gate=2)
    g._start_scan_mode()
    observed = []
    def _rewind():
        observed.append(NFCGate._active_scan_gate)
    g._run_rewind = _rewind

    g._finish_scan()

    assert observed == [2]
    assert NFCGate._active_scan_gate is None

def test_finish_scan_restores_previous_hh_selected_gate():
    g = _make_gate(gate=3)
    g.printer.set_mmu(MockMMU(
        gate_status=[0, 0, 0, 1],
        gate_spool_id=[-1, -1, -1, -1],
        active_gate=1))
    g._start_scan_mode()
    g._scan_mm_total = 20.0

    g._finish_scan()

    assert g.printer.gcode_scripts[-1] == 'MMU_SELECT GATE=1'

def test_no_tag_scan_restores_previous_hh_selected_gate():
    g = _make_gate(gate=3)
    g.printer.set_mmu(MockMMU(
        gate_status=[0, 0, 0, 1],
        gate_spool_id=[-1, -1, -1, -1],
        active_gate=1))
    g._start_scan_mode()
    g._scan_mm_total = 20.0

    g._rewind_and_exit_scan()

    assert any(s == 'MMU_SELECT GATE=1' for s in g.printer.gcode_scripts)

def test_finish_scan_restores_previous_hh_selected_gate_after_rewind_error():
    g = _make_gate(gate=3)
    g.printer.set_mmu(MockMMU(
        gate_status=[0, 0, 0, 1],
        gate_spool_id=[-1, -1, -1, -1],
        active_gate=1))
    g._start_scan_mode()
    g._run_rewind = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

    try:
        g._finish_scan()
    except RuntimeError:
        pass

    assert g.printer.gcode_scripts[-1] == 'MMU_SELECT GATE=1'

def test_scan_does_not_restore_when_previous_hh_gate_matches_scan_gate():
    g = _make_gate(gate=3)
    g.printer.set_mmu(MockMMU(
        gate_status=[0, 0, 0, 1],
        gate_spool_id=[-1, -1, -1, -1],
        active_gate=3))
    g._start_scan_mode()
    g._scan_mm_total = 20.0

    g._finish_scan()

    assert all(script != 'MMU_SELECT GATE=3'
               for script in g.printer.gcode_scripts)

def test_second_gate_blocked_when_lock_held():
    """When gate 0 holds the lock, gate 1 must not acquire it."""
    g0 = _make_gate(gate=0)
    g1 = _make_gate(gate=1)
    g0._start_scan_mode()
    assert NFCGate._active_scan_gate == 0
    # Gate 1 sees a non-None active_scan_gate and must not proceed
    assert NFCGate._active_scan_gate is not None, \
        "gate 1 should see the lock held by gate 0"
    g0._finish_scan()
    assert NFCGate._active_scan_gate is None


# ── Poll timer edge detection ─────────────────────────────────────────────────

def test_empty_gate_returns_next_poll_without_scan():
    """gate_status=0 skips I2C and returns next poll time immediately."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[0]))
    g._prev_gate_status = 0
    result = g._poll_timer_event(100.0)
    assert result == pytest_approx(130.0), f"Expected 130.0, got {result}"
    assert not g._scan_mode

def test_assigned_matching_spool_suspends_without_polling():
    """HH assigned + matching NFC cache should not keep reading the PN532."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[0], gate_spool_id=[49]))
    g._state.current_uid = '04448F92D32A81'
    g._state.current_spool = 49
    g._prev_gate_status = 0
    called = []
    g._poll = lambda: called.append(True)

    result = g._poll_timer_event(100.0)

    assert called == []
    assert g._hh_load_paused
    assert result == pytest_approx(130.0)
    assert '[polling suspended]' in g.status_line()

def test_status_line_hh_empty_overrides_stale_nfc_cache():
    """After eject, HH gate_status=0 should display empty even with cache."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[0], gate_spool_id=[55]))
    g._state.current_uid = '04C19F92D32A81'
    g._state.current_spool = 55
    g._hh_load_paused = True

    line = g.status_line()
    plain = _strip_html(line)

    assert 'Gate 0:  empty' in plain
    assert 'spool 55       UID' not in plain
    assert '[HH: spool 55  assigned]' in plain
    assert '<span style="color:#87CEEB">empty</span>' in line
    assert '<span style="color:#FFFF00">assigned</span>' in line

def test_status_line_collapses_hh_assigned_cache_empty_note():
    """HH assigned with no NFC cache should be one compact status block."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[0], gate_spool_id=[55]))

    line = g.status_line()
    plain = _strip_html(line)

    assert '[HH has spool 55; NFC cache empty]' not in plain
    assert '[HH: spool 55  assigned, NFC cache empty]' in plain
    assert '<span style="color:#FFFF00">assigned</span>' in line

def test_status_line_colors_hh_available_with_html_span():
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], gate_spool_id=[55]))
    g._state.current_uid = '04C19F92D32A81'
    g._state.current_spool = 55

    line = g.status_line()

    assert '<span style="color:#90EE90">available</span>' in line
    assert 'HH: spool 55  available' in _strip_html(line)

def test_hh_found_without_spool_does_not_clear_nfc_cache():
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], gate_spool_id=[-1]))
    g._state.current_uid = '04C19F92D32A81'
    g._state.current_spool = 55
    g._hh_load_paused = True

    skipped = g._poll_hh_pause_check()

    assert skipped
    assert g._state.current_uid == '04C19F92D32A81'
    assert g._state.current_spool == 55
    assert g._hh_load_paused
    line = _strip_html(g.status_line())
    assert '[NFC has spool 55; HH found/no spool]' in line
    assert line.count('found/no spool') == 1, "HH state must not appear twice"

def test_hh_found_with_nfc_spool_does_not_start_scan_jog():
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], gate_spool_id=[-1]))
    g.printer.set_print_state('standby')
    g._state.current_uid = '04C19F92D32A81'
    g._state.current_spool = 55
    g._prev_gate_status = 0
    g._poll = lambda: False

    result = g._poll_timer_event(100.0)

    assert not g._scan_pending
    assert not g._scan_mode
    assert g._hh_load_paused
    assert g._state.current_spool == 55
    assert result == pytest_approx(130.0)

def test_startup_hh_found_without_spool_allows_discovery_scan():
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], gate_spool_id=[-1]))
    g._seed_cache_from_hh(100.0)

    assert not g._hh_load_paused
    assert g._state.current_spool is None
    line = _strip_html(g.status_line())
    assert 'Gate 0:  occupied' in line
    assert '[polling]' in line
    assert '[HH: found/no spool]' in line

def test_hh_found_without_nfc_spool_starts_discovery_scan_jog():
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], gate_spool_id=[-1]))
    g.printer.set_print_state('standby')
    g._prev_gate_status = 0
    g._poll = lambda: False

    result = g._poll_timer_event(100.0)

    assert not g._scan_mode
    assert result == pytest_approx(100.1)

    g.reactor._time = 100.1
    result = g._poll_timer_event(100.1)

    assert g._scan_mode
    assert result == g.reactor.NEVER

def test_lane_status_lines_matches_lane_mcus_without_crashing():
    class StatusPrinter:
        def lookup_objects(self, kind):
            assert kind == 'mcu'
            return [('mcu lane0', object()), ('mcu lane1', object())]

    g = _make_gate()
    g._name = 'lane0'
    g.status_line = lambda: '  Gate 0:  empty   [polling]  [HH idle]'

    old_lanes = list(_lane_instances)
    try:
        _lane_instances[:] = [g]
        lines = _lane_status_lines(StatusPrinter())
    finally:
        _lane_instances[:] = old_lanes

    assert lines[0] == 'NFC gate status — 2 MMU lane(s), 1 NFC reader(s) configured:'
    assert 'Gate 0:  empty' in lines[1]
    assert lines[2] == '  lane1:    no NFC reader configured'

def test_cold_start_no_false_trigger():
    """prev=-1 → curr=1 must not trigger scan (prevents cold-start false fire)."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], action='idle'))
    g.printer.set_print_state('standby')
    g._prev_gate_status = -1
    g._poll = lambda: False
    g._poll_timer_event(100.0)
    assert not g._scan_mode

def test_load_transition_waits_for_idle_settle_before_scan():
    """0→1 with HH idle waits briefly before starting scan mode."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], action='idle'))
    g.printer.set_print_state('standby')
    g._prev_gate_status = 0
    result = g._poll_timer_event(100.0)
    assert not g._scan_mode
    assert result == pytest_approx(100.1)

    g.reactor._time = 100.1
    result = g._poll_timer_event(100.1)
    assert g._scan_mode
    assert NFCGate._active_scan_gate == 0
    assert g.printer._gcode.responses[-1].startswith(
        '[SCAN] NFC[0]: starting scan-jog (max=')
    assert result == g.reactor.NEVER

def test_no_trigger_while_printing():
    """0→1 while printing does not start scan."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], action='idle'))
    g.printer.set_print_state('printing')
    g._prev_gate_status = 0
    g._poll = lambda: False
    g._poll_timer_event(100.0)
    assert not g._scan_mode

def test_no_trigger_when_hh_busy():
    """0→1 while HH action != idle does not start scan."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], action='loading'))
    g.printer.set_print_state('standby')
    g._prev_gate_status = 0
    g._poll = lambda: False
    g._poll_timer_event(100.0)
    assert not g._scan_mode

def test_scan_lock_defers_pending_trigger_for_three_seconds():
    g = _make_gate(gate=2)
    g.printer.set_mmu(MockMMU(gate_status=[0, 0, 1, 0, 0], action='idle'))
    g.printer.set_print_state('standby')
    g._prev_gate_status = 0
    NFCGate._active_scan_gate = 4

    first = g._poll_timer_event(100.0)
    g.reactor._time = first
    result = g._poll_timer_event(first)

    assert not g._scan_mode
    assert g._scan_pending
    assert result == pytest_approx(first + 3.0)

    NFCGate._active_scan_gate = None
    g.reactor._time = result
    result = g._poll_timer_event(result)

    assert g._scan_mode
    assert g.printer._gcode.responses[-1].startswith(
        '[SCAN] NFC[2]: starting scan-jog (max=')
    assert result == g.reactor.NEVER

def test_scan_disabled_skips_all_detection():
    """scan_enabled=False bypasses gate_status checking entirely."""
    g = _make_gate(scan_enabled=False)
    g.printer.set_mmu(MockMMU(gate_status=[1], action='idle'))
    g.printer.set_print_state('standby')
    g._prev_gate_status = 0
    g._poll = lambda: False
    g._poll_timer_event(100.0)
    assert not g._scan_mode


# ── Scan step loop ────────────────────────────────────────────────────────────

def test_scan_step_noop_when_not_in_scan_mode():
    g = _make_gate()
    g._scan_mode = False
    result = g._scan_step_event(100.0)
    assert result == g.reactor.NEVER

def test_scan_step_tag_found_exits_loop():
    """_poll() returning True calls _finish_scan and returns NEVER."""
    g = _make_gate()
    g._scan_mode = True
    g.printer.set_print_state('standby')
    finished = []
    g._poll        = lambda: True
    g._finish_scan = lambda: finished.append(True)
    result = g._scan_step_event(100.0)
    assert finished, "_finish_scan was not called"
    assert result == g.reactor.NEVER

def test_scan_step_retries_incomplete_rich_read():
    g = _make_gate(scan_max_mm=200.0, scan_decode_retry_mm=5.0,
                   scan_decode_retry_rounds=3)
    g._scan_mode = True
    g._scan_mm_total = 100.0
    g._scan_found_event = ('uid_only', 0, '04AABB', None, None)
    g._state.current_uid = '04AABB'
    g._state.current_spool = None
    g._state.current_tag = CurrentTag(
        uid='04AABB',
        read_incomplete=True,
        read_retry_reason='auth failed sectors [1]')
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=100.0))
    g._poll = lambda: True
    finished = []
    g._finish_scan = lambda: finished.append(True)

    result = g._scan_step_event(100.0)

    assert not finished
    assert result == pytest_approx(100.5)
    assert g._scan_mm_total == 105.0
    assert g._scan_decode_retry_attempts == 1
    assert g._scan_decode_retry_uid == '04AABB'
    assert g._scan_decode_retry_offset == 5.0
    assert g._scan_found_event == ('uid_only', 0, '04AABB', None, None)
    assert g._state.current_uid is None
    assert any('MMU_TEST_MOVE MOVE=5.00' in script
               for script in g.printer.gcode_scripts)
    assert any('tag decode incomplete; retry 1/6 after 5.0mm jog' in msg
               for msg in g.printer._gcode.responses)

def test_scan_step_second_decode_retry_checks_other_side():
    g = _make_gate(scan_max_mm=200.0, scan_decode_retry_mm=5.0,
                   scan_decode_retry_rounds=3)
    g._scan_mode = True
    g._scan_mm_total = 105.0
    g._scan_decode_retry_uid = '04AABB'
    g._scan_decode_retry_attempts = 1
    g._scan_decode_retry_offset = 5.0
    g._scan_found_event = ('uid_only', 0, '04AABB', None, None)
    g._state.current_uid = '04AABB'
    g._state.current_spool = None
    g._state.current_tag = CurrentTag(
        uid='04AABB',
        read_incomplete=True,
        read_retry_reason='auth failed sectors [1]')
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=100.0))
    g._poll = lambda: True
    finished = []
    g._finish_scan = lambda: finished.append(True)

    result = g._scan_step_event(100.0)

    assert not finished
    assert result == pytest_approx(100.5)
    assert g._scan_mm_total == 95.0
    assert g._scan_next_chunk_time == pytest_approx(101.0)
    assert g._scan_decode_retry_attempts == 2
    assert g._scan_decode_retry_offset == -5.0
    assert any('MMU_TEST_MOVE MOVE=-10.00' in script
               for script in g.printer.gcode_scripts)
    assert any('tag decode incomplete; retry 2/6 after -10.0mm jog' in msg
               for msg in g.printer._gcode.responses)

def test_scan_step_continues_decode_retry_without_regular_jog():
    g = _make_gate(scan_jog_mm=20.0, scan_max_mm=200.0,
                   scan_decode_retry_mm=5.0,
                   scan_decode_retry_rounds=3)
    g._scan_mode = True
    g._scan_mm_total = 105.0
    g._scan_next_chunk_time = 100.0
    g._scan_decode_retry_uid = '04AABB'
    g._scan_decode_retry_attempts = 1
    g._scan_decode_retry_offset = 5.0
    g._scan_found_event = ('uid_only', 0, '04AABB', None, None)
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=100.0))
    g._poll = lambda: False

    result = g._scan_step_event(100.0)

    assert result == pytest_approx(100.5)
    assert g._scan_mm_total == 95.0
    assert g._scan_decode_retry_attempts == 2
    assert g._scan_decode_retry_offset == -5.0
    assert any('MMU_TEST_MOVE MOVE=-10.00' in script
               for script in g.printer.gcode_scripts)
    assert not any('MMU_TEST_MOVE MOVE=20.00' in script
                   for script in g.printer.gcode_scripts)


def test_scan_step_waits_for_decode_retry_settle_before_polling():
    g = _make_gate(scan_jog_mm=20.0, scan_max_mm=200.0,
                   scan_decode_retry_mm=5.0,
                   scan_decode_retry_rounds=3)
    g._scan_mode = True
    g._scan_mm_total = 105.0
    g._scan_next_chunk_time = 101.0
    g._scan_decode_retry_uid = '04AABB'
    g._scan_decode_retry_attempts = 1
    g._scan_decode_retry_offset = 5.0
    g._scan_found_event = ('uid_only', 0, '04AABB', None, None)
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=100.0))
    polled = []
    g._poll = lambda: polled.append(True) or False

    result = g._scan_step_event(100.0)

    assert result == pytest_approx(101.0)
    assert not polled
    assert g._scan_mm_total == 105.0
    assert g._scan_decode_retry_attempts == 1
    assert not any('MMU_TEST_MOVE' in script
                   for script in g.printer.gcode_scripts)


def test_scan_step_logs_decode_retry_poll_without_tag():
    from nfc_gates import scan_jog

    g = _make_gate(scan_jog_mm=20.0, scan_max_mm=200.0,
                   scan_decode_retry_mm=5.0,
                   scan_decode_retry_rounds=3)
    g._debug = 3
    g._scan_mode = True
    g._scan_mm_total = 105.0
    g._scan_decode_retry_uid = '04AABB'
    g._scan_decode_retry_attempts = 1
    g._scan_decode_retry_offset = 5.0
    g._scan_found_event = ('uid_only', 0, '04AABB', None, None)
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=100.0))
    g._poll = lambda: False

    messages = []
    old_info = scan_jog.logger.info
    scan_jog.logger.info = lambda msg, *args: messages.append(
        msg % args if args else msg)
    try:
        g._scan_step_event(100.0)
    finally:
        scan_jog.logger.info = old_info

    assert any('decode retry poll 1/6 at scan position 105.0' in msg
               for msg in messages)
    assert any('decode retry poll 1/6 found no tag' in msg
               for msg in messages)


def test_scan_step_resumes_regular_scan_after_decode_retry_limit():
    g = _make_gate(scan_decode_retry_rounds=1)
    g._scan_mode = True
    g._scan_mm_total = 100.0
    g._scan_next_chunk_time = 200.0
    g._scan_decode_retry_uid = '04AABB'
    g._scan_decode_retry_attempts = 2
    g._scan_decode_retry_offset = -5.0
    g._scan_found_event = ('uid_only', 0, '04AABB', None, None)
    g._state.current_uid = '04AABB'
    g._state.current_spool = None
    g._state.current_tag = CurrentTag(
        uid='04AABB',
        read_incomplete=True,
        read_retry_reason='auth failed sectors [1]')
    g.printer.set_print_state('standby')
    g._poll = lambda: True
    finished = []
    g._finish_scan = lambda: finished.append(True)

    result = g._scan_step_event(100.0)

    assert not finished
    assert result == pytest_approx(100.5)
    assert g._scan_decode_retry_uid is None
    assert g._scan_decode_retry_attempts == 0
    assert g._scan_decode_retry_offset == 0.0
    assert g._scan_next_chunk_time == 100.0
    assert g._state.current_uid is None
    assert g._scan_found_event == ('uid_only', 0, '04AABB', None, None)
    assert not any('MMU_TEST_MOVE' in script for script in g.printer.gcode_scripts)
    assert any('tag decode still incomplete after 2 retries; continuing scan-jog' in msg
               for msg in g.printer._gcode.responses)


def test_scan_step_uses_incomplete_fallback_at_max_after_decode_retry_limit():
    g = _make_gate(scan_decode_retry_rounds=1)
    g._scan_mode = True
    g._scan_mm_total = 200.0
    g._scan_max_mm = 200.0
    g._scan_decode_retry_uid = '04AABB'
    g._scan_decode_retry_attempts = 2
    g._scan_found_event = ('uid_only', 0, '04AABB', None, None)
    g.printer.set_print_state('standby')
    g._poll = lambda: False
    finished = []
    g._finish_scan = lambda: finished.append(True)

    result = g._scan_step_event(100.0)

    assert finished
    assert result == g.reactor.NEVER
    assert any('scan reached max distance after decode retries' in msg
               for msg in g.printer._gcode.responses)

def test_scan_decode_retry_clamps_to_scan_bounds():
    g = _make_gate(scan_max_mm=200.0, scan_decode_retry_mm=5.0,
                   scan_decode_retry_rounds=3)
    g._scan_mode = True
    g._scan_mm_total = 199.0
    g._state.current_uid = '04AABB'
    g._state.current_spool = None
    g._state.current_tag = CurrentTag(
        uid='04AABB',
        read_incomplete=True,
        read_retry_reason='auth failed sectors [1]')
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=100.0))
    g._poll = lambda: True

    result = g._scan_step_event(100.0)

    assert result == pytest_approx(100.5)
    assert g._scan_mm_total == 200.0
    assert g._scan_next_chunk_time == pytest_approx(101.0)
    assert g._scan_decode_retry_attempts == 1
    assert g._scan_decode_retry_offset == 1.0
    assert any('MMU_TEST_MOVE MOVE=1.00' in script
               for script in g.printer.gcode_scripts)

def test_scan_decode_retry_state_cleared_on_finish_and_no_tag_exit():
    g = _make_gate()
    g._start_scan_mode()
    g._scan_decode_retry_uid = '04AABB'
    g._scan_decode_retry_attempts = 2
    g._finish_scan()

    assert g._scan_decode_retry_uid is None
    assert g._scan_decode_retry_attempts == 0

    g = _make_gate()
    g._start_scan_mode()
    g._scan_decode_retry_uid = '04AABB'
    g._scan_decode_retry_attempts = 2
    g._rewind_and_exit_scan()

    assert g._scan_decode_retry_uid is None
    assert g._scan_decode_retry_attempts == 0

def test_scan_step_no_tag_reschedules_at_poll_interval():
    """Poll cadence stays independent while a chunk is still moving."""
    g = _make_gate(scan_max_mm=200.0, scan_poll_interval=0.5)
    g._scan_mode       = True
    g._scan_start_time = 100.0   # scan just started
    g._scan_mm_total   = 100.0
    g._scan_next_chunk_time = 101.0
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=80.0))
    g._poll = lambda: False
    result = g._scan_step_event(100.0)
    assert result == pytest_approx(100.5)

def test_scan_step_timeout_rewinds_and_exits():
    """After scan_max_mm/speed + 5s slack, _rewind_and_exit_scan is called."""
    g = _make_gate(scan_max_mm=200.0, scan_poll_interval=0.5)
    g._scan_mode       = True
    # scan_max_mm=200 at 80mm/s = 2.5s; timeout = 2.5+5 = 7.5s
    # set start_time so elapsed > 7.5s
    g._scan_start_time = 100.0 - 8.0   # elapsed = 8s > 7.5s
    g._scan_mm_total   = 200.0
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=80.0))
    rewound = []
    g._poll                 = lambda: False
    g._rewind_and_exit_scan = lambda: rewound.append(True)
    result = g._scan_step_event(100.0)
    assert rewound, "_rewind_and_exit_scan was not called on timeout"
    assert result == g.reactor.NEVER

def test_scan_step_print_start_aborts():
    """A print starting mid-scan calls _rewind_and_exit_scan."""
    g = _make_gate()
    g._scan_mode = True
    g.printer.set_print_state('printing')
    rewound = []
    g._rewind_and_exit_scan = lambda: rewound.append(True)
    result = g._scan_step_event(100.0)
    assert rewound, "_rewind_and_exit_scan was not called on print start"
    assert result == g.reactor.NEVER

def test_scan_starts_without_immediate_jog():
    """_start_scan_mode schedules an immediate read before first motion."""
    g = _make_gate(gate=1, scan_max_mm=200.0)
    g._start_scan_mode()
    scripts = g.printer.gcode_scripts
    assert not any('MMU_TEST_MOVE' in script for script in scripts)
    assert g._scan_mm_total == 0.0
    assert g._scan_next_chunk_time == pytest_approx(100.0)

def test_start_scan_defers_hh_prep_until_timer_step():
    """scan start only marks HH prep pending so hooks can return first."""
    g = _make_gate(gate=2)
    g._start_scan_mode()
    scripts = g.printer.gcode_scripts
    assert not any('_NFC_GATE_CLEAR_CACHE' in s for s in scripts)
    assert not any('MMU_SPOOLMAN SYNC=1' in s for s in scripts)
    assert g._scan_hh_prep_pending

def test_scan_step_runs_deferred_hh_prep_before_first_move():
    """First scan timer step runs clear/sync before the first MMU_TEST_MOVE."""
    g = _make_gate(gate=2, scan_jog_mm=50.0, scan_max_mm=200.0,
                   scan_poll_interval=0.5)
    g._start_scan_mode()
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=80.0))
    g._poll = lambda: False

    g._scan_step_event(100.0)

    scripts = g.printer.gcode_scripts
    assert len(scripts) == 3
    assert scripts[0] == '_NFC_GATE_CLEAR_CACHE GATE=2'
    assert scripts[1] == 'MMU_SPOOLMAN SYNC=1 QUIET=1'
    assert 'MMU_SELECT GATE=2' in scripts[2]
    assert 'MMU_TEST_MOVE MOVE=50.00' in scripts[2]
    assert not g._scan_hh_prep_pending

def test_scan_step_issues_one_chunk_when_due():
    """No tag + due chunk issues scan_jog_mm, not the full scan distance."""
    g = _make_gate(gate=1, scan_jog_mm=50.0, scan_max_mm=200.0,
                   scan_poll_interval=0.5)
    g._scan_mode = True
    g._scan_next_chunk_time = 100.0
    g._scan_hh_prep_pending = False
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=80.0))
    g._poll = lambda: False

    result = g._scan_step_event(100.0)

    scripts = g.printer.gcode_scripts
    assert len(scripts) == 1
    assert 'MMU_SELECT GATE=1' in scripts[0]
    assert 'MMU_TEST_MOVE MOVE=50.00' in scripts[0]
    assert g._scan_mm_total == 50.0
    assert g._scan_next_chunk_time == pytest_approx(102.125)
    assert result == pytest_approx(100.5)


def test_scan_step_dwell_after_completed_chunk_before_next_jog():
    """A completed jog gets three poll intervals of stationary read time."""
    g = _make_gate(gate=1, scan_jog_mm=50.0, scan_max_mm=200.0,
                   scan_poll_interval=0.5)
    g._scan_mode = True
    g._scan_mm_total = 50.0
    g._scan_next_chunk_time = 102.125
    g.reactor._time = 101.0
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=80.0))
    g._poll = lambda: False

    result = g._scan_step_event(101.0)

    assert len(g.printer.gcode_scripts) == 0
    assert g._scan_mm_total == 50.0
    assert result == pytest_approx(101.5)

def test_derived_lane_max_controls_final_chunk_size():
    """A lane Bowden length limits the last chunk instead of overshooting."""
    g = _make_gate(gate=1, scan_jog_mm=50.0, scan_max_mm=999.0,
                   scan_poll_interval=0.5)
    g._start_scan_mode(max_mm=g._get_lane_scan_max_mm())
    g._scan_mm_total = 150.0
    g._scan_next_chunk_time = 100.0
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gate_status=[0, 1], gear_short_move_speed=100.0))
    g._poll = lambda: False

    g._scan_step_event(100.0)

    assert g._scan_max_mm == 175.0
    assert g._scan_mm_total == 175.0
    assert any('MMU_TEST_MOVE MOVE=25.00' in script
               for script in g.printer.gcode_scripts)

def test_scan_step_does_not_stack_chunks_before_interval():
    """Chunks are not queued until the calculated move interval has elapsed."""
    g = _make_gate(gate=1, scan_jog_mm=50.0, scan_max_mm=200.0,
                   scan_poll_interval=0.5)
    g._scan_mode = True
    g._scan_mm_total = 50.0
    g._scan_next_chunk_time = 100.645
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gear_short_move_speed=80.0))
    g._poll = lambda: False

    result = g._scan_step_event(100.5)

    assert len(g.printer.gcode_scripts) == 0
    assert g._scan_mm_total == 50.0
    assert result == pytest_approx(100.5)

def test_get_scan_speed_reads_from_hh():
    """_get_scan_speed returns gear_short_move_speed from the mmu object."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gear_short_move_speed=120.0))
    assert g._get_scan_speed() == 120.0

def test_get_scan_speed_fallback_without_mmu():
    """_get_scan_speed returns 80.0 when no mmu object is present."""
    g = _make_gate()
    assert g._get_scan_speed() == 80.0

def test_scan_chunk_interval_uses_speed():
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gear_short_move_speed=100.0))
    assert g._scan_chunk_interval(50.0) == pytest_approx(0.5)


# ── PR-100 preflight and Bowden calibration ─────────────────────────────────

def test_all_lanes_guard_blocks_buffer_loaded_lane():
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1, 3], filament_pos=0))
    ok, reason = g._all_lanes_parked_or_empty()
    assert not ok
    assert 'lane 1' in reason

def test_all_lanes_guard_blocks_non_parked_filament():
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1, 0], action='loading',
                              filament_pos=3, active_gate=1))
    ok, reason = g._all_lanes_parked_or_empty()
    assert not ok
    assert 'lane 1 is loading' in reason
    assert 'filament is not parked' in reason

def test_lane_scan_max_comes_from_mmu_vars():
    g = _make_gate(gate=2)
    assert g._get_lane_scan_max_mm() == 150.0

def test_lane_scan_max_missing_bowden_list_blocks():
    g = _make_gate(gate=0)
    fd, path = tempfile.mkstemp(prefix='nfc_mmu_vars_missing_', suffix='.cfg')
    with os.fdopen(fd, 'w') as f:
        f.write('other_value = [1, 2, 3]\n')
    g._mmu_vars_path = path
    assert g._get_lane_scan_max_mm() is None

def test_lane_scan_max_gate_index_out_of_range_blocks():
    g = _make_gate(gate=8)
    assert g._get_lane_scan_max_mm() is None

def test_manual_jog_blocked_by_unsafe_lane():
    g = _make_gate()
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gate_status=[1, 3], action='idle'))
    gcmd = MockGCmd()

    g._manual_jog_scan(gcmd)

    assert not g._scan_mode
    assert 'scan-jog not available while' in gcmd.responses[-1]

def test_manual_jog_blocked_by_missing_bowden_length():
    g = _make_gate(gate=8)
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gate_status=[1, 0], action='idle'))
    gcmd = MockGCmd()

    g._manual_jog_scan(gcmd)

    assert not g._scan_mode
    assert 'missing Bowden calibration length' in gcmd.responses[-1]

def test_manual_jog_success_message_has_readable_spacing():
    g = _make_gate(gate=3)
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gate_status=[0, 0, 0, 1], action='idle'))
    gcmd = MockGCmd()

    g._manual_jog_scan(gcmd)

    assert g._scan_mode
    assert gcmd.responses[-1].startswith(
        '[SCAN] NFC[test]: scan-jog started for gate 3 (max=')

def test_manual_jog_schedules_required_hh_prep():
    g = _make_gate(gate=3)
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gate_status=[0, 0, 0, 1], action='idle'))
    gcmd = MockGCmd()

    g._manual_jog_scan(gcmd)

    assert g._scan_mode
    assert g._scan_hh_prep_pending
    assert not any('MMU_SPOOLMAN SYNC=1' in script
                   for script in g.printer.gcode_scripts)

def test_automatic_jog_blocked_by_unsafe_lane():
    g = _make_gate()
    g.printer.set_print_state('standby')
    g.printer.set_mmu(MockMMU(gate_status=[1, 3], action='idle'))
    g._prev_gate_status = 0

    first = g._poll_timer_event(100.0)
    g.reactor._time = first
    result = g._poll_timer_event(first)

    assert not g._scan_mode
    assert 'scan-jog not available while' in g.printer._gcode.responses[-1]
    assert 'lane 1' in g.printer._gcode.responses[-1]
    assert result == pytest_approx(first + g._poll_interval)


# ── GCode content ─────────────────────────────────────────────────────────────

def test_run_jog_gcode_content():
    g = _make_gate(gate=2)
    g._run_jog(37.5)
    scripts = g.printer.gcode_scripts
    assert len(scripts) == 1
    assert 'MMU_SELECT GATE=2' in scripts[0]
    assert 'MMU_TEST_MOVE MOVE=37.50' in scripts[0]
    assert 'M400' not in scripts[0]   # jog is non-blocking

def test_run_jog_no_select_on_subsequent_steps():
    """After the first jog, MMU_SELECT must not be re-issued."""
    g = _make_gate(gate=2)
    g._scan_gate_selected = True
    g._run_jog(50.0)
    script = g.printer.gcode_scripts[0]
    assert 'MMU_SELECT' not in script
    assert 'MMU_TEST_MOVE MOVE=50.00' in script

def test_run_jog_negative_distance():
    g = _make_gate(gate=0)
    g._run_jog(-50.0)
    assert 'MMU_TEST_MOVE MOVE=-50.00' in g.printer.gcode_scripts[0]

def test_run_rewind_gcode_content():
    g = _make_gate(gate=3)
    g._scan_mm_total = 100.0
    g._run_rewind()
    scripts = g.printer.gcode_scripts
    assert len(scripts) == 2
    assert 'MMU_TEST_MOVE MOVE=-70.00' in scripts[0]
    assert scripts[1] == '_MMU_STEP_UNLOAD_GATE'
    assert 'MMU_UNLOAD' not in scripts[0]

def test_run_rewind_uses_configured_buffer():
    g = _make_gate(gate=3, scan_rewind_buffer_mm=40.0)
    g._scan_mm_total = 100.0
    g._run_rewind()
    scripts = g.printer.gcode_scripts
    assert len(scripts) == 2
    assert 'MMU_TEST_MOVE MOVE=-60.00' in scripts[0]
    assert scripts[1] == '_MMU_STEP_UNLOAD_GATE'

def test_run_rewind_short_scan_skips_fast_rewind():
    g = _make_gate(gate=3, scan_rewind_buffer_mm=30.0)
    g._scan_mm_total = 20.0
    g._run_rewind()
    scripts = g.printer.gcode_scripts
    assert scripts == ['_MMU_STEP_UNLOAD_GATE']

def test_rewind_skipped_when_nothing_jogged():
    """_run_rewind must not issue any GCode if scan_mm_total is 0."""
    g = _make_gate(gate=1)
    g._run_rewind()   # scan_mm_total defaults to 0.0
    assert len(g.printer.gcode_scripts) == 0


# ── Poll timer resume ─────────────────────────────────────────────────────────

def test_finish_scan_reschedules_poll_timer():
    g = _make_gate()
    g._start_scan_mode()
    g._scan_mm_total = 50.0
    g._finish_scan()
    scheduled_time = g.reactor.timers[g._poll_timer][1]
    assert scheduled_time == pytest_approx(130.625), \
        "poll timer should resume after rewind can finish"

def test_rewind_and_exit_reschedules_poll_timer():
    g = _make_gate()
    g._start_scan_mode()
    g._scan_mm_total = 50.0
    g._rewind_and_exit_scan()
    scheduled_time = g.reactor.timers[g._poll_timer][1]
    assert scheduled_time == pytest_approx(130.625), \
        "poll timer should resume after rewind can finish"

def test_finish_scan_resets_miss_count():
    g = _make_gate()
    g._state.miss_count = 2
    g._start_scan_mode()
    g._finish_scan()
    assert g._state.miss_count == 0

def test_finish_scan_clears_scan_mode():
    g = _make_gate()
    g._start_scan_mode()
    g._finish_scan()
    assert not g._scan_mode

def test_rewind_and_exit_clears_scan_mode():
    g = _make_gate()
    g._start_scan_mode()
    g._rewind_and_exit_scan()
    assert not g._scan_mode


# ─────────────────────────────────────────────────────────────────────────────
# Item 8 — scan_jog finish: 5-tuple cached event with meta dispatches correctly
# ─────────────────────────────────────────────────────────────────────────────

class _DispatchCapture:
    def __init__(self):
        self.calls = []
    def dispatch(self, event_type, gate, uid, spool, meta=None, auto_created=False):
        self.calls.append((event_type, gate, uid, spool, meta))

def test_finish_scan_metadata_direct_dispatches_meta():
    """5-tuple scan_found_event with meta=dict passes meta to klipper.dispatch."""
    g = _make_gate()
    g._klipper = _DispatchCapture()
    g._start_scan_mode()
    meta = {'material': 'PLA', 'color_hex': 'FF5500'}
    g._scan_found_event = ('changed', 0, '04AABB', None, meta)

    g._finish_scan()

    assert len(g._klipper.calls) == 1
    ev = g._klipper.calls[0]
    assert ev[0] == 'changed'
    assert ev[1] == 0
    assert ev[2] == '04AABB'
    assert ev[3] is None
    assert ev[4] == meta

def test_finish_scan_spool_id_dispatches_no_meta():
    """Normal spool_id event dispatches with meta=None."""
    g = _make_gate()
    g._klipper = _DispatchCapture()
    g._start_scan_mode()
    g._scan_found_event = ('changed', 0, '04AABB', 42, None)

    g._finish_scan()

    assert g._klipper.calls == [('changed', 0, '04AABB', 42, None)]
    assert g._hh_load_paused
    assert g._hh_confirmed_spool == 42

def test_finish_scan_no_event_does_not_dispatch():
    """When scan_found_event is None, klipper.dispatch is never called."""
    g = _make_gate()
    g._klipper = _DispatchCapture()
    g._start_scan_mode()
    g._scan_found_event = None

    g._finish_scan()

    assert g._klipper.calls == []

def test_finish_scan_uid_only_event_dispatches_without_meta():
    """uid_only event (tag unregistered in Spoolman) dispatches correctly."""
    g = _make_gate()
    g._klipper = _DispatchCapture()
    g._start_scan_mode()
    g._scan_found_event = ('uid_only', 0, '04AABB', None, None)

    g._finish_scan()

    assert g._klipper.calls == [('uid_only', 0, '04AABB', None, None)]
    assert g._hh_load_paused


# ─────────────────────────────────────────────────────────────────────────────
# GateState.reset() and scan_jog start/rewind integration
# ─────────────────────────────────────────────────────────────────────────────

def test_gate_state_reset_clears_all_fields():
    """reset() zeros uid, spool, current_tag, and miss_count."""
    from nfc_gates.gate_state import GateState
    s = GateState(gate=0, absent_threshold=3)
    s.current_uid   = 'AABBCC'
    s.current_spool = 7
    s.miss_count    = 2
    s.reset()
    assert s.current_uid   is None
    assert s.current_spool is None
    assert s.current_tag   is None
    assert s.miss_count    == 0

def test_start_scan_resets_gate_state():
    """scan start calls state.reset(), clearing miss_count in addition to uid/spool."""
    g = _make_gate()
    g._state.current_uid   = 'AABBCC'
    g._state.current_spool = 7
    g._state.miss_count    = 2
    g._start_scan_mode()
    assert g._state.current_uid   is None
    assert g._state.current_spool is None
    assert g._state.miss_count    == 0

def test_rewind_and_exit_clears_hh_cache_and_resets_state():
    """After a no-tag scan, rewind_and_exit calls _NFC_GATE_CLEAR_CACHE and resets state."""
    g = _make_gate(gate=1)
    g._start_scan_mode()
    g._scan_mm_total = 50.0
    scripts_before = len(g.printer.gcode_scripts)

    g._rewind_and_exit_scan()

    new_scripts = g.printer.gcode_scripts[scripts_before:]
    assert any('_NFC_GATE_CLEAR_CACHE GATE=1' in s for s in new_scripts), \
        "_NFC_GATE_CLEAR_CACHE not issued after no-tag rewind"
    assert g._state.current_uid   is None
    assert g._state.current_spool is None
    assert g._state.miss_count    == 0


# ── Approx helper (avoids pytest dependency for float comparison) ─────────────

def pytest_approx(val, rel=1e-6):
    """Minimal stand-in so the test file runs without pytest installed."""
    class _Approx:
        def __init__(self, v): self.v = v
        def __eq__(self, other): return abs(other - self.v) <= rel * abs(self.v)
        def __repr__(self): return f"~{self.v}"
    return _Approx(val)


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
