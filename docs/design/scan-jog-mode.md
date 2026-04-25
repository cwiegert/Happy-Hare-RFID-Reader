# Design: Scan-and-Jog Mode (Spool Pre-load NFC Identification)

> Engineering reference — not end-user documentation.
> Status: **Implemented** — branch `scan-jog-mode`, not yet merged to `main`
> Source: `klippy/extras/nfc_gates/NFC_manager.py`

---

## Problem Statement

When a spool is manually loaded into a lane, Happy Hare parks the filament at the gate entrance (gate_status → 1, action → Idle). At that point the NFC tag is on the spool hub — potentially centimeters away from the PN532 antenna. The normal 30s polling loop may not read the tag at all if the hub face isn't already aligned over the reader. The user needs a mode where, after filament is parked at the gate, the system automatically spins the spool in small increments to find the tag, identifies the spool, and then winds back to the parked position.

---

## Constraints

- Must not run during an active print.
- Must not run while HH is executing any MMU operation (Loading, Unloading, Homing, etc.).
- Must not require a GCode macro to drive the loop — the control logic must stay in Python.
- Must rewind to the original parked position.
- Rewind must fire even if the search is aborted (max distance reached, print starts mid-scan).
- Must not use `UPDATE_DELAYED_GCODE` — timer lifecycle is owned entirely by the Klipper reactor.

---

## Why Not a GCode Macro Loop

Happy Hare's `_MMU_EVENT ACTION=gate_map_changed` is a GCode macro callback, not a Klipper event. Driving the jog loop from GCode would require:

1. A `[delayed_gcode]` that reschedules itself via `UPDATE_DELAYED_GCODE`.
2. A cancel mechanism (no clean cancel for `delayed_gcode` — you must schedule with DURATION=0 and check state inside).
3. Scope crossing on every iteration: GCode → Python (NFC read) → GCode (reschedule / jog).

The Python-only approach eliminates all three problems. Reactor timers are first-class objects: they start, reschedule, and cancel entirely within `_scan_step_event` return values. The jog command (`MMU_TEST_MOVE`) is still issued via `gcode.run_script()`, which is safe from the reactor thread — the same pattern used by `KlipperInterface._run_gcode()` today.

---

## Trigger Detection

Happy Hare does not fire a Klipper event when gate status changes. The `mmu:*` event namespace exists but does not include gate-status transitions. The only Python-readable signal is `mmu.get_status()['gate_status'][N]`.

Trigger detection is folded into the existing `_poll_event` tick. When the gate is empty (`gate_status == 0`) the poll tick skips the I2C read (no tag present) and instead checks for the load transition. It watches for all of:

- `gate_status[N]` was 0 (empty), is now 1 (available/parked)
- `mmu.action == "Idle"`
- Not currently printing (`print_stats.state != "printing"`)
- Not already in scan mode (`_scan_mode == False`)
- NFC reader is healthy (`_failed == False`)

Initialization value for `_prev_gate_status` is `-1` (unknown). This prevents a false trigger on cold start when HH already has status=1 from a previous session — the -1 → 1 transition is ignored; only 0 → 1 triggers scan mode.

When the gate is empty the poll tick checks only a Python dict — no I2C, no HTTP, no GCode. The existing poll interval (default 30 s) is adequate for detecting the load event; the user has already spent several seconds manually inserting filament before HH parks it.

---

## Gate Context and Scan Lock

`MMU_SELECT_GATE` sets a global active-gate context inside Happy Hare. If two
lanes both enter scan mode concurrently, their `MMU_SELECT_GATE` calls will
interleave and each `MMU_TEST_MOVE` may move the wrong lane's filament — a race
condition that cannot be caught at the GCode level.

Because all `nfc_gate` instances share the same Klipper printer object and run
on the same reactor thread, a **class-level lock** is sufficient. No threading
primitives are needed — the reactor is single-threaded, so reads and writes of a
class variable are atomic with respect to timer callbacks.

```python
# Class variable — shared across all nfc_gate instances
NfcGate._active_scan_gate = None   # set to self._gate while scan is running
```

Rules:
- **Entry**: `_start_scan_mode` only proceeds if `NfcGate._active_scan_gate is None`. If another gate holds the lock, the trigger is silently dropped; the gate's `_prev_gate_status` is still updated so it won't re-trigger on the same load event.
- **Hold**: while a scan is running, `_active_scan_gate == self._gate`. Other gates see a non-None value and skip scan entry.
- **Release**: both `_finish_scan` and `_rewind_and_exit_scan` clear `_active_scan_gate = None` before resuming the poll timer.

Normal polling (I2C reads only, no MMU moves) is **not** gated by this lock.
Gates that are not scanning continue to poll and identify tags independently.

---

## State Machine

Scan-and-jog adds a second timer (`_scan_timer`) to the existing `_poll_timer`:

```
                    ┌─────────────────────────────────────────────────┐
                    │                                                 │
       klippy:      ▼                                                 │
       connect ──► POLLING ──(0→1 gate_status, HH idle, not printing)──► SCAN_JOG
                    ▲                                                         │
                    │              tag found OR max_mm OR print starts        │
                    └─────────────────────────────────────────────────────────┘
```

`_poll_timer` runs continuously. When scan mode starts, the poll timer is parked at `NEVER` and `_scan_timer` takes over. When scan mode ends (success or abort), `_scan_timer` returns `NEVER` and the poll timer is resumed.

---

## New Instance State

```python
# Class variable — declared once at class body level, shared across all instances
NfcGate._active_scan_gate = None   # gate number that currently owns the MMU, or None

# Timer handles (instance)
self._scan_timer       = None    # registered only during active scan

# Scan mode tracking (instance)
self._scan_mode        = False
self._scan_mm_total    = 0.0     # mm jogged forward so far — logged on success, not used for rewind

# Previous HH gate_status value — for edge detection in poll tick (instance)
self._prev_gate_status = -1      # -1 = unknown (cold start)
```

---

## New Config Keys

All added to `[nfc_gate]` (and overridable per `[nfc_gate laneN]`):

| Key | Default | Meaning |
|---|---|---|
| `scan_jog_mm` | `50.0` | Filament advance per step (mm) |
| `scan_max_mm` | `600` | Maximum total advance before abort and rewind |
| `scan_interval` | `2.0` | Seconds between NFC poll attempts during scan |
| `scan_enabled` | `True` | Master switch — set False to disable scan mode entirely |

---

## Implementation Sketch

### Poll tick (enhanced with edge detection)

```python
def _poll_event(self, eventtime):
    if self._failed or self._scan_mode:
        return eventtime + self._poll_interval

    mmu = self.printer.lookup_object('mmu', None)
    if mmu is None:
        return eventtime + self._poll_interval

    try:
        status = mmu.get_status(eventtime)
        gate_statuses = status.get('gate_status', [])
        if self._gate >= len(gate_statuses):
            return eventtime + self._poll_interval
        curr = int(gate_statuses[self._gate] or 0)
        action = status.get('action', '').lower()
    except Exception:
        return eventtime + self._poll_interval

    prev = self._prev_gate_status
    self._prev_gate_status = curr

    # Gate is empty — check for load transition, skip I2C
    if curr == 0:
        return eventtime + self._poll_interval

    # Detect 0→1 load event and enter scan mode
    if (self._scan_enabled
            and prev == 0 and curr == 1
            and action == 'idle'
            and not self._is_printing()):
        if NfcGate._active_scan_gate is not None:
            if self._debug >= 3:
                logger.info(
                    "nfc_gate: [%s] gate %d — scan trigger deferred: "
                    "gate %d already scanning",
                    self._name, self._gate, NfcGate._active_scan_gate)
        else:
            self._start_scan_mode()
            return self.reactor.NEVER   # poll resumes when scan exits

    # Normal poll path — gate is loaded, read the tag
    self._poll()
    return eventtime + self._poll_interval
```

### Print guard

```python
def _is_printing(self):
    ps = self.printer.lookup_object('print_stats', None)
    if ps is None:
        return False
    return ps.get_status(0).get('state', '') == 'printing'
```

### Scan mode entry

```python
def _start_scan_mode(self):
    NfcGate._active_scan_gate = self._gate
    self._scan_mode = True
    self._scan_mm_total = 0.0
    # Schedule first NFC read after a short settle
    self._scan_timer = self.reactor.register_timer(
        self._scan_step_event,
        self.reactor.monotonic() + 0.5
    )
    if self._debug >= 3:
        logger.info(
            "nfc_gate: [%s] gate %d scan mode started — "
            "step=%.1fmm max=%.1fmm interval=%.1fs",
            self._name, self._gate, self._scan_jog_mm, self._scan_max_mm, self._scan_interval)
```

### Scan step (the loop body)

```python
def _scan_step_event(self, eventtime):
    if not self._scan_mode:
        return self.reactor.NEVER

    # Abort if a print starts mid-scan
    if self._is_printing():
        logger.warning("nfc_gate: [%s] scan mode: print started — aborting", self._name)
        self._rewind_and_exit_scan()
        return self.reactor.NEVER

    tag_found = self._poll()

    if tag_found:
        self._finish_scan()
        return self.reactor.NEVER           # ← terminates the loop

    if self._scan_mm_total >= self._scan_max_mm:
        logger.warning(
            "nfc_gate: [%s] scan mode: no tag after %.1fmm — rewinding",
            self._name, self._scan_mm_total)
        self._rewind_and_exit_scan()
        return self.reactor.NEVER           # ← terminates the loop

    self._run_jog(self._scan_jog_mm)
    self._scan_mm_total += self._scan_jog_mm
    if self._debug >= 4:
        logger.info(
            "nfc_gate: [%s] scan mode: no tag — jogged %.1fmm (total %.1fmm)",
            self._name, self._scan_jog_mm, self._scan_mm_total)
    return eventtime + self._scan_interval  # ← reschedules next attempt
```

### Tag found

```python
def _finish_scan(self):
    self._scan_mode = False
    NfcGate._active_scan_gate = None
    # Rewind so filament is at parked position — _poll() already identified the tag
    self._run_rewind()
    if self._debug >= 3:
        logger.info(
            "nfc_gate: [%s] gate %d scan mode: tag identified after %.1fmm — rewound",
            self._name, self._gate, self._scan_mm_total)
    # Resume normal polling
    self.reactor.update_timer(
        self._poll_timer,
        self.reactor.monotonic() + self._poll_interval)
```

### Abort path

```python
def _rewind_and_exit_scan(self):
    self._scan_mode = False
    NfcGate._active_scan_gate = None
    self._run_rewind()
    self.reactor.update_timer(
        self._poll_timer,
        self.reactor.monotonic() + self._poll_interval)
```

### Jog primitive

```python
def _run_jog(self, mm):
    """Select this gate then advance filament via MMU_TEST_MOVE.

    Safe to call from reactor thread — gcode.run_script() is the same
    mechanism used by KlipperInterface._run_gcode().
    Positive mm = advance toward MMU; negative mm = retract toward spool.
    Speed and accel are intentionally omitted — MMU_TEST_MOVE defaults to
    the values defined for the motor/homing combination, avoiding stepper
    skips or filament grinding from an instantaneous speed change.
    """
    gcode = self._printer.lookup_object('gcode')
    gcode.run_script(
        "MMU_SELECT_GATE GATE=%d\nMMU_TEST_MOVE MOVE=%.2f"
        % (self._gate, mm))
```

### Rewind primitive

```python
def _run_rewind(self):
    """Unload the active gate back to the parked position via MMU_UNLOAD.

    MMU_UNLOAD restore=0 retracts to the gate entrance (parked) without
    restoring the pre-load extruder position. This is the correct rewind
    regardless of how far the spool was jogged — HH drives the move to the
    known parked position rather than dead-reckoning a negative distance.
    Safe to call from reactor thread via gcode.run_script().
    """
    gcode = self._printer.lookup_object('gcode')
    gcode.run_script(
        "MMU_SELECT_GATE GATE=%d\nMMU_UNLOAD restore=0"
        % self._gate)
```

---

## Timer Lifecycle

| Timer | Created | Destroyed | Interval |
|---|---|---|---|
| `_poll_timer` | `__init__` (parked at NEVER) | `_handle_disconnect` | `poll_interval` (30 s default) |
| `_scan_timer` | `_start_scan_mode` | `_scan_step_event` returns NEVER | `scan_interval` (2 s default) |

The scan timer is created anew on each scan entry. There is no need to deregister it explicitly — returning `reactor.NEVER` from `_scan_step_event` parks it permanently. `_scan_mode = False` is the canonical in-flight abort flag; any path that sets it to False before the next tick will cause the next `_scan_step_event` call to immediately return `NEVER`.

---

## GateState Interaction

Scan mode is normal polling at a shorter interval, with a jog between each attempt. `_scan_step_event` calls `_poll()` directly on every tick — the same method the poll timer fires. `_poll()` does the I2C read and runs the full state machine (Spoolman lookup, `GateState.process_read`, both suppression checks, Spoolman location update, `KlipperInterface.dispatch`, `_hh_confirmed_spool`). When `_poll()` identifies a tag it returns `True`; `_scan_step_event` then calls `_finish_scan()` which rewinds and resumes the normal poll timer. No second read, no special logic in `_finish_scan`.

This means:
- `GateState.miss_count` does **not** increment during scan ticks. `_poll()` checks `self._scan_mode` and skips the miss path when True — a no-read during a deliberate spool rotation is not an absence event.
- When a tag is found, `_poll()` has already processed it through the full state machine. `_finish_scan` only needs to rewind and hand control back to the poll timer.
- After scan mode completes, the first scheduled poll timer tick will fire into a gate that is already fully populated — `process_read` returns `None` (quiet).

---

## Interaction with `_hh_load_paused` (Normal Suspend Logic)

When `_poll()` identifies a tag during a scan step, the normal path runs immediately: `GateState.process_read` sets `current_uid` and `current_spool`; `_hh_confirmed_spool` is set; `KlipperInterface.dispatch` issues the GCode so HH processes `_NFC_SPOOL_CHANGED` and sets `gate_spool_id[N] > 0`. `_finish_scan` then rewinds and resumes the poll timer. On the next scheduled poll timer tick, `_hh_gate_is_loaded()` returns True and the gate enters the normal suspended state exactly as it would after any other tag read.

---

## Logging

Scan-jog messages follow the standard debug level conventions defined in [Error Handling and Logging](error-logging.md):

| Message | Level gate | `nfc_reader.log` | `klippy.log` |
|---|---|---|---|
| `scan mode started` | `debug >= 3` | ✅ | ❌ |
| `scan trigger deferred: gate N already scanning` | `debug >= 3` | ✅ | ❌ |
| `tag identified after Xmm — rewound` | `debug >= 3` | ✅ | ❌ |
| `no tag — jogged Xmm (total Xmm)` | `debug >= 4` | ✅ | ❌ |
| `print started — aborting` | warning (always) | ✅ | ✅ |
| `no tag after Xmm — rewinding` | warning (always) | ✅ | ✅ |

Set `debug: 3` on the test lane to observe scan start and success. Set `debug: 4` to also see each individual jog step.

---

## Happy Hare Compatibility Notes

- `MMU_SELECT_GATE` and `MMU_TEST_MOVE` are standard Happy Hare v2.x commands. `MMU_SELECT_GATE GATE=N` selects the lane before any move; `MMU_TEST_MOVE MOVE=mm` drives the gear stepper at its configured default speed and accel.
- `mmu.get_status()['gate_status']` values: `0` = empty, `1` = available/parked, `2` = available from buffer. Scan mode triggers only on `0 → 1`. Buffer-loaded filament (`0 → 2`) does not trigger — the NFC tag will be on the spool hub on the lane, not yet near the gate.
- `mmu.get_status()['action']` comparison is lowercased and checked for `== 'idle'` (exact). If HH changes its action string in a future release, this guard silently prevents scan mode from starting (safe-fail direction).

---

## Open Questions (Needs Hardware Verification)

1. **`MMU_TEST_MOVE` direction convention** — confirm that positive `MOVE` values advance filament toward the MMU (into the gate) on all HH versions. Negative values retract toward the spool.
2. **`scan_jog_mm` default** — 50 mm per step is a starting point; tune after observing how many steps are typically needed to rotate the hub over the antenna.
