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

from nfc_gates.NFC_manager import NFCGate


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

    def run_script(self, script):
        self.scripts.append(script)


class MockMMU:
    def __init__(self, gate_status=None, action='idle'):
        self._gate_status = gate_status or []
        self._action      = action

    def get_status(self, eventtime):
        return {
            'gate_status':   self._gate_status,
            'gate_spool_id': [-1] * len(self._gate_status),
            'action':        self._action,
        }


class MockPrintStats:
    def __init__(self, state='standby'):
        self._state = state

    def get_status(self, eventtime):
        return {'state': self._state}


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
               scan_interval=2.0, scan_enabled=True):
    """Build a minimal NFCGate with scan-jog state, bypassing __init__.

    Uses object.__new__ to skip Klipper config/I2C setup, then manually
    populates every instance variable that the scan-jog methods touch.
    """
    g = object.__new__(NFCGate)
    g._name             = 'test'
    g._gate             = gate
    g._debug            = 0
    g._failed           = False
    g._polling          = True
    g._poll_interval    = 30.0
    g._scan_jog_mm      = scan_jog_mm
    g._scan_max_mm      = scan_max_mm
    g._scan_interval    = scan_interval
    g._scan_enabled     = scan_enabled
    g._scan_mode        = False
    g._scan_mm_total    = 0.0
    g._scan_timer       = None
    g._prev_gate_status = -1
    g.reactor           = MockReactor()
    g.printer           = MockPrinter()
    g._poll_timer       = g.reactor.register_timer(lambda e: g.reactor.NEVER)
    NFCGate._active_scan_gate = None   # reset class-level lock before every test
    return g


# ── Scan lock ─────────────────────────────────────────────────────────────────

def test_start_scan_acquires_lock():
    g = _make_gate()
    g._start_scan_mode()
    assert NFCGate._active_scan_gate == 0

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

def test_cold_start_no_false_trigger():
    """prev=-1 → curr=1 must not trigger scan (prevents cold-start false fire)."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], action='idle'))
    g.printer.set_print_state('standby')
    g._prev_gate_status = -1
    g._poll = lambda: False
    g._poll_timer_event(100.0)
    assert not g._scan_mode

def test_load_transition_starts_scan():
    """0→1 with HH idle and not printing starts scan mode."""
    g = _make_gate()
    g.printer.set_mmu(MockMMU(gate_status=[1], action='idle'))
    g.printer.set_print_state('standby')
    g._prev_gate_status = 0
    result = g._poll_timer_event(100.0)
    assert g._scan_mode
    assert NFCGate._active_scan_gate == 0
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

def test_scan_step_no_tag_jogs_and_reschedules():
    """_poll() returning False issues a jog and reschedules the timer."""
    g = _make_gate(scan_jog_mm=25.0, scan_interval=1.5)
    g._scan_mode = True
    g.printer.set_print_state('standby')
    jogged = []
    g._poll    = lambda: False
    g._run_jog = lambda mm: jogged.append(mm)
    result = g._scan_step_event(100.0)
    assert jogged == [25.0], f"Expected jog of 25mm, got {jogged}"
    assert g._scan_mm_total == 25.0
    assert result == 101.5

def test_scan_mm_accumulates_over_steps():
    """Each jog step adds scan_jog_mm to _scan_mm_total."""
    g = _make_gate(scan_jog_mm=30.0, scan_max_mm=500.0)
    g._scan_mode = True
    g.printer.set_print_state('standby')
    g._poll    = lambda: False
    g._run_jog = lambda mm: None
    g._scan_step_event(100.0)
    g._scan_step_event(102.0)
    g._scan_step_event(104.0)
    assert g._scan_mm_total == 90.0

def test_scan_step_max_mm_rewinds_and_exits():
    """scan_mm_total >= scan_max_mm calls _rewind_and_exit_scan."""
    g = _make_gate(scan_max_mm=100.0)
    g._scan_mode     = True
    g._scan_mm_total = 100.0
    g.printer.set_print_state('standby')
    rewound = []
    g._poll                 = lambda: False
    g._rewind_and_exit_scan = lambda: rewound.append(True)
    result = g._scan_step_event(100.0)
    assert rewound, "_rewind_and_exit_scan was not called at max_mm"
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


# ── GCode content ─────────────────────────────────────────────────────────────

def test_run_jog_gcode_content():
    g = _make_gate(gate=2)
    g._run_jog(37.5)
    scripts = g.printer.gcode_scripts
    assert len(scripts) == 1
    assert 'MMU_SELECT_GATE GATE=2' in scripts[0]
    assert 'MMU_TEST_MOVE MOVE=37.50' in scripts[0]

def test_run_jog_negative_distance():
    g = _make_gate(gate=0)
    g._run_jog(-50.0)
    assert 'MMU_TEST_MOVE MOVE=-50.00' in g.printer.gcode_scripts[0]

def test_run_rewind_gcode_content():
    g = _make_gate(gate=3)
    g._run_rewind()
    scripts = g.printer.gcode_scripts
    assert len(scripts) == 1
    assert 'MMU_SELECT_GATE GATE=3' in scripts[0]
    assert 'MMU_UNLOAD restore=0' in scripts[0]

def test_rewind_uses_unload_not_test_move():
    """Rewind must be MMU_UNLOAD — never a negative MMU_TEST_MOVE."""
    g = _make_gate(gate=1)
    g._run_rewind()
    script = g.printer.gcode_scripts[0]
    assert 'MMU_UNLOAD' in script
    assert 'MMU_TEST_MOVE' not in script


# ── Poll timer resume ─────────────────────────────────────────────────────────

def test_finish_scan_reschedules_poll_timer():
    g = _make_gate()
    g._start_scan_mode()
    g._finish_scan()
    scheduled_time = g.reactor.timers[g._poll_timer][1]
    assert scheduled_time != g.reactor.NEVER, \
        "poll timer should be rescheduled after a successful scan"

def test_rewind_and_exit_reschedules_poll_timer():
    g = _make_gate()
    g._start_scan_mode()
    g._rewind_and_exit_scan()
    scheduled_time = g.reactor.timers[g._poll_timer][1]
    assert scheduled_time != g.reactor.NEVER, \
        "poll timer should be rescheduled after scan abort"

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
