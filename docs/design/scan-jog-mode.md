# Design: Scan-and-Jog Mode (Spool Pre-load NFC Identification)

> Engineering reference — not end-user documentation.
> Status: **Implemented** — `scan_jog.py` module, integrated into `nfc_manager.py`
> Source: `klippy/extras/nfc_gates/scan_jog.py`, `klippy/extras/nfc_gates/nfc_manager.py`

---

## Problem Statement

When a spool is manually loaded into a lane, Happy Hare parks the filament at the gate entrance (gate_status → 1, action → Idle). At that point the NFC tag is on the spool hub — potentially centimeters away from the PN532 antenna. The normal polling loop may not read the tag at all if the hub face isn't already aligned over the reader. The user needs a mode where, after filament is parked at the gate, the system automatically spins the spool in small increments to find the tag, identifies the spool, and then winds back to the parked position.

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

Driving the jog loop from GCode would require a `[delayed_gcode]` that reschedules itself, a cancel mechanism (no clean cancel exists), and scope crossing on every iteration. The Python-only approach eliminates all three problems. Reactor timers are first-class objects: they start, reschedule, and cancel entirely within `_scan_step_event` return values. The jog command (`MMU_TEST_MOVE`) is still issued via `gcode.run_script()`, which is safe from the reactor thread — the same pattern used by `KlipperInterface._run_gcode()`.

---

## Code Structure

All scan-jog logic lives in `klippy/extras/nfc_gates/scan_jog.py` as module-level functions. `NFCGate` in `nfc_manager.py` delegates to them via thin wrapper methods:

```python
# NFCGate wrappers in nfc_manager.py
def _start_scan_mode(self):   return scan_jog.start(self)
def _scan_step_event(self, t): return scan_jog.step_event(self, t)
def _finish_scan(self):        return scan_jog.finish(self)
def _rewind_and_exit_scan(self): return scan_jog.rewind_and_exit(self)
def _run_jog(self, mm):        return scan_jog.run_jog(self, mm)
def _run_rewind(self):         return scan_jog.run_rewind(self)
```

`gate` is passed as the first argument to every `scan_jog` function so it can read and write `gate._scan_mode`, `gate._scan_mm_total`, etc. directly.

---

## Trigger Detection

Trigger detection is folded into `_poll_timer_event`. The gate-status edge-detection path runs every tick when `scan_enabled` is True:

```
_poll_timer_event (every poll_interval)
  ├── read HH gate_status (Python dict — no I2C)
  │
  ├── curr == 0  → skip I2C entirely; also handles _hh_load_paused resume/suspend
  │
  ├── 0→1 edge  → set _scan_pending = True; reset _scan_idle_ready_time
  │
  ├── _scan_pending == True AND curr == 1 AND hh.idle AND not printing
  │     first fire:  _scan_idle_ready_time = now + 2.0  → return that time
  │     settled:     _scan_pending = False
  │                  if NFCGate._active_scan_gate is not None:
  │                      re-arm _scan_pending, retry in 3.0 s
  │                  else:
  │                      _start_scan_mode() → park poll timer, return NEVER
  │
  └── _scan_pending == True but conditions not met → return now + 1.0
```

**Key details:**
- `_prev_gate_status` initializes to `-1` on startup. The `-1 → 1` transition at cold start is ignored; only `0 → 1` triggers scan mode. This prevents a false trigger when HH already has `gate_status = 1` from a previous session.
- A 2-second idle-settle delay (`_scan_idle_ready_time`) is inserted after HH reports idle. This prevents premature scan entry while HH is still completing its park move.
- If another gate holds the scan lock, `_scan_pending` is re-armed and a 3-second retry is scheduled rather than silently dropping the trigger or spamming logs.

**Manual trigger:** `NFC GATE=N JOG_SCAN=1` calls `scan_jog.manual_jog_scan(gate, gcmd)` directly. It runs the same precondition checks (not printing, HH idle, no other gate scanning, reader healthy) and calls `start(gate)`. No edge detection is involved. `HH_SYNC=0` skips **both** the HH gate cache clear and the Spoolman sync — both use `gcode.run_script()` which deadlocks when called from inside an HH post-preload hook (HH already holds the GCode lock at that point).

---

## Gate Context and Scan Lock

If two lanes entered scan mode concurrently, their `MMU_SELECT GATE=N` calls would interleave and `MMU_TEST_MOVE` would move the wrong lane's filament. Because all `nfc_gate` instances run on the same reactor thread, a **class-level lock** is sufficient:

```python
# Class variable — shared across all NFCGate instances
NFCGate._active_scan_gate = None   # gate number that currently holds the lock, or None
```

Rules:
- **Entry**: `scan_jog.start()` sets `NFCGate._active_scan_gate = gate._gate`.
- **Hold**: while scan is running, `_active_scan_gate` is non-None. Other gates re-arm `_scan_pending` and retry in 3 seconds.
- **Release**: both `finish()` and `rewind_and_exit()` set `NFCGate._active_scan_gate = None`.
- `_handle_disconnect` also clears the lock if this gate owns it.

Normal polling (I2C reads, no MMU moves) is not gated by this lock.

---

## State Machine

```
              klippy:
              connect ──► POLLING ──(0→1, HH idle, not printing)──► SCAN_JOG
                           ▲                                               │
                           │        tag found OR max_mm OR print starts   │
                           └───────────────────────────────────────────────┘
```

When scan mode starts, the poll timer is parked at `NEVER` and `_scan_timer` takes over. When scan mode ends, `_scan_timer` returns `NEVER` and the poll timer is resumed.

---

## Instance State Variables

```python
# Class variable — shared across all NFCGate instances
NFCGate._active_scan_gate = None   # gate number holding the scan lock, or None

# Timers
self._scan_timer           = None      # registered only during active scan

# Scan mode
self._scan_mode            = False
self._scan_mm_total        = 0.0       # mm jogged forward so far
self._scan_next_chunk_time = 0.0       # reactor timestamp when next jog chunk may fire
self._scan_found_event     = None      # cached event suppressed during jog; dispatched after rewind

# Trigger detection
self._prev_gate_status     = -1        # -1 = cold start (no 0→1 false trigger)
self._scan_pending         = False     # armed on 0→1; fires when HH confirms idle
self._scan_idle_ready_time = 0.0       # timestamp for 2s HH-idle settle delay
```

---

## Config Keys

All added to `[nfc_gate]` (overridable per `[nfc_gate laneN]`):

| Key | Python fallback | Shipped `nfc_reader.cfg` | Meaning |
|---|---|---|---|
| `scan_enabled` | `True` | `True` | Master switch — `False` disables scan mode entirely |
| `scan_jog_mm` | `50.0` | `25.0` | Filament advance per jog step (mm) |
| `scan_decode_retry_mm` | `5.0` | `5.0` | Distance between nearby retry positions |
| `scan_decode_retry_rounds` | `3` | `3` | Nearby retry rounds before accepting the current result |
| `scan_poll_interval` | `0.1` | `0.1` | Minimum seconds between NFC reads during scan |

`scan_jog_mm` of 25 mm gives a ~5 cm read window (25 mm on each side of center plus the antenna width) for finding tags that are slightly off-axis.
The maximum scan distance is read at scan start from Happy Hare's
`mmu_calibration_bowden_lengths` in `mmu_vars.cfg`; the current gate indexes
that list.

---

## Implementation: `scan_jog.py`

### `start(gate, max_mm, sync_hh)` — enter scan mode

```python
def start(gate, max_mm=None, sync_hh=True):
    gate.__class__._active_scan_gate = gate._gate
    gate._scan_mode = True
    gate._scan_mm_total = 0.0
    gate._scan_next_chunk_time = gate.reactor.monotonic()
    gate._hh_seed_spool_id = None
    gate._hh_seed_available = False
    gate._scan_found_event = None
    gate._scan_previous_uid = gate._state.current_uid
    gate._scan_previous_spool = gate._state.current_spool
    gate._scan_previous_active_gate = get_active_gate(gate)
    gate._state.reset()          # clears uid, spool, current_tag, miss_count
    gate._hh_load_paused = False
    gate._scan_gate_selected = False

    if sync_hh:
        clear_hh_gate_cache(gate)        # _NFC_GATE_CLEAR_CACHE GATE=N
        sync_spoolman_before_scan(gate)  # MMU_SPOOLMAN SYNC=1 QUIET=1

    gate._scan_timer = gate.reactor.register_timer(
        gate._scan_step_event,
        gate.reactor.monotonic())
```

**`GateState.reset()`** clears `_current_uid`, `_current_spool`, `current_tag`, and `miss_count` atomically (bypassing property setters). This forces `process_read` to fire a `changed` event on the first NFC read during scan, regardless of what was previously cached. The previous uid/spool are saved to `_scan_previous_uid`/`_scan_previous_spool` before the reset for reference during the abort path.

**Pre-scan clearing sequence:**

Both `clear_hh_gate_cache` and `sync_spoolman_before_scan` are guarded by `sync_hh`. When `sync_hh=False` (called from inside a Happy Hare post-preload hook), both are skipped entirely. Both use `gcode.run_script()` which acquires Klipper's GCode lock — calling either from inside an HH hook deadlocks because HH already holds that lock.

`clear_hh_gate_cache` issues `_NFC_GATE_CLEAR_CACHE GATE=N`, which calls `MMU_GATE_MAP GATE=N SPOOLID=-1 NAME=Unknown MATERIAL=Unknown COLOR=FFFFFF55 AVAILABLE=1`. This gives the Mainsail UI an "unknown filament" placeholder while the scan jog runs. `AVAILABLE=1` keeps the gate marked as loaded so HH does not treat it as empty.

`sync_spoolman_before_scan` pushes the cleared HH gate state to Spoolman via `MMU_SPOOLMAN SYNC=1`, vacating the spool's location field before the jog begins.

| Trigger | `sync_hh` | `clear_hh_gate_cache` | `sync_spoolman_before_scan` |
|---|---|---|---|
| Automatic `0→1` poll | `True` | ✅ runs | ✅ runs |
| Manual `NFC JOG_SCAN=1` | `True` | ✅ runs | ✅ runs |
| HH post-preload hook (`HH_SYNC=0`) | `False` | ❌ skipped | ❌ skipped |

When the scan succeeds, `_NFC_SPOOL_CHANGED` issues the next `MMU_SPOOLMAN SYNC=1` with the newly identified spool.

### `step_event(gate, eventtime)` — the loop body

```python
def step_event(gate, eventtime):
    if not gate._scan_mode:
        return gate.reactor.NEVER

    if is_printing(gate):
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    now = gate.reactor.monotonic()
    tag_found = gate._poll()

    if tag_found:
        if retry_incomplete_decode(gate, now):
            return gate.reactor.monotonic() + gate._scan_poll_interval
        gate._finish_scan()
        return gate.reactor.NEVER

    if gate._scan_mm_total >= gate._scan_max_mm:
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    # Jog only when the previous chunk is estimated complete; poll every tick.
    if now >= gate._scan_next_chunk_time:
        remaining = gate._scan_max_mm - gate._scan_mm_total
        chunk = min(gate._scan_jog_mm, remaining)
        gate._run_jog(chunk)
        gate._scan_mm_total += chunk
        gate._scan_next_chunk_time = now + chunk_interval(gate, chunk)

    return now + gate._scan_poll_interval
```

The timer always returns `now + scan_poll_interval` so NFC is polled continuously throughout the scan. Jog chunks are gated by `_scan_next_chunk_time`, which advances by `chunk_interval = abs(mm) / gear_short_move_speed` after each issue. This decouples read frequency from motor timing — the tag can be detected anywhere in the move, not only after the chunk completes.

### `finish(gate)` — tag found

When a UID is detected but the rich payload read is marked incomplete, scan-jog queues nearby retry jogs before accepting the current UID/metadata result. The retry decision is format-neutral: reader/parser code sets `CurrentTag.read_incomplete` and `read_retry_reason`; scan-jog only applies the configured `scan_decode_retry_mm` / `scan_decode_retry_rounds` policy. Each retry round probes both sides of the first UID hit position. The first implementation marks incomplete MIFARE reads when sector authentication or block reads fail, which covers spool-mounted Bambu tags at the edge of the reader field.

```python
def finish(gate):
    gate._scan_mode = False
    gate.__class__._active_scan_gate = None
    gate._state.miss_count = 0
    gate._run_rewind()
    # Dispatch the spool event that was suppressed while the filament was moving.
    if gate._scan_found_event is not None:
        event_type, g, uid, spool = gate._scan_found_event
        gate._scan_found_event = None
        gate._klipper.dispatch(event_type, g, uid, spool)
    gate._resume_poll_after_rewind()
```

### `rewind_and_exit(gate)` — abort path

```python
def rewind_and_exit(gate):
    gate._scan_mode = False
    gate._state.miss_count = 0
    try:
        gate._run_rewind()
    finally:
        restore_active_gate(gate)
    gate.__class__._active_scan_gate = None
    clear_hh_gate_cache(gate)   # _NFC_GATE_CLEAR_CACHE GATE=N — safe, timer thread
    gate._state.reset()         # clean slate: uid, spool, tag, miss_count all None/0
    gate._hh_load_paused = False
    gate._scan_previous_uid = None
    gate._scan_previous_spool = None
    gate._resume_poll_after_rewind()
```

**After a no-tag scan, both sides are left as clean slate:**

`clear_hh_gate_cache` is safe here because `rewind_and_exit` is always called from the reactor timer thread — not from inside an HH hook — so `gcode.run_script()` does not deadlock.

`gate._state.reset()` clears NFC uid, spool, current_tag, and miss_count. The previous spool is **not** restored. Since HH's gate map was just cleared, restoring the old NFC spool would create a mismatch (`_check_hh_cleared` would see NFC=54, HH=Unknown and immediately re-clear NFC, creating a re-dispatch loop on the next poll).

After the rewind, `_hh_load_paused = False` and both states are blank. When the next normal poll fires and the tag is under the reader, NFC re-reads it, re-looks up the spool in Spoolman, and re-dispatches to HH in one clean cycle.

`resume_poll_after_rewind` restarts the poll timer with an extra delay equal to the rewind move duration (`scan_mm_total / speed`) so the first scheduled poll fires after the rewind is complete.

### `run_jog(gate, mm)` — jog primitive

```python
def run_jog(gate, mm):
    gcode = gate.printer.lookup_object('gcode')
    gcode.run_script("MMU_TEST_MOVE MOVE=%.2f QUIET=1" % mm)
```

`MMU_SELECT GATE=N` is issued once in `start()` before the scan timer fires. `run_jog` issues only `MMU_TEST_MOVE` since the gate context is already set. `QUIET=1` suppresses HH console output.

### `run_rewind(gate)` — rewind primitive

```python
def run_rewind(gate):
    if gate._scan_mm_total <= 0.0:
        return
    gcode = gate.printer.lookup_object('gcode')
    gcode.run_script("MMU_TEST_MOVE MOVE=%.2f QUIET=1\nM400"
                     % -gate._scan_mm_total)
```

Rewind is dead-reckoning: a negative `MMU_TEST_MOVE` of exactly `scan_mm_total`. `M400` (wait for moves) is appended so the reactor timer knows the rewind is physically complete before the next poll fires.

---

## Timer Lifecycle

| Timer | Created | Destroyed | Interval |
|---|---|---|---|
| `_poll_timer` | `__init__` (parked at NEVER) | `_handle_disconnect` | `poll_interval` (default 10 s) |
| `_scan_timer` | `start()` | `step_event()` returns NEVER | `scan_poll_interval` |

The scan timer is created anew on each scan entry. Returning `reactor.NEVER` from `step_event` parks it permanently. `_scan_mode = False` is the canonical in-flight abort flag.

---

## GateState Interaction

`step_event` calls `gate._poll()` directly — the same method the poll timer fires. `_poll()` runs the full state machine including Spoolman lookup and `GateState.process_read`. When `_poll()` returns `True` (tag found), `step_event` calls `finish()` immediately.

**`_poll()` UID short-circuit.** Before calling `_resolve_spool()` (Spoolman round trip), `_poll()` checks:

```python
if uid_hex is not None and uid_hex == self._state.current_uid:
    self._state.miss_count = 0
    return True   # same tag already tracked — skip Spoolman
```

This means Spoolman is only called when the UID changes (new tag, or after `GateState.reset()` cleared `current_uid`). Repeated reads of the same tag — the common case during scan-jog dwell periods and normal polling — cost only an I2C read with no network round trip. `reset()` sets `current_uid = None`, so the first read after any scan entry or abort always goes to Spoolman once to re-establish the spool mapping.

**Event dispatch is deferred during scan.** If a spool-changed event fires while `_scan_mode` is True (filament is still moving), `nfc_manager` caches it in `_scan_found_event` instead of dispatching immediately. `finish()` dispatches the cached event after `run_rewind()` returns, so HH and Spoolman receive the notification only after the filament is back at the parked position.

**`GateState.reset()`** clears `_current_uid`, `_current_spool`, `current_tag`, and `miss_count` atomically. It is called in two places:
- `start()` — before the scan timer is registered, so the first scan poll fires a `changed` event even if the same tag was previously known.
- `rewind_and_exit()` — after the no-tag rewind, so NFC and HH both start from a clean slate for the next poll cycle.

`GateState.miss_count` does **not** increment during scan ticks. `process_read()` receives `scan_mode=True` when called from within scan mode — the miss path is skipped for no-read results. A missed NFC read during a deliberate spool rotation is not an absence event.

---

## Interaction with `_hh_load_paused`

When `_poll()` identifies a tag during a scan step, `GateState.process_read` sets `current_uid` and `current_spool` but `KlipperInterface.dispatch` is suppressed — the event is cached in `_scan_found_event`. After the rewind completes, `finish()` dispatches `_NFC_SPOOL_CHANGED`; HH sets `gate_spool_id[N] > 0`. When the poll timer resumes, the first tick sees `_hh_gate_matches_current_spool()` returning True and enters the normal suspended state.

---

## Logging

Scan-jog messages follow the standard debug level conventions:

| Message | Level | `nfc_reader.log` | `klippy.log` |
|---|---|---|---|
| `scan mode started — chunk=Xmm max=Xmm speed=Xmm/s` | `debug >= 3` | ✅ | ❌ |
| `gate state reset (uid=X spool=X -> None/None)` | `debug >= 3` | ✅ | ❌ |
| `clearing HH gate cache before scan-jog` | `debug >= 3` | ✅ | ❌ |
| `syncing HH Spoolman state before scan-jog` | `debug >= 3` | ✅ | ❌ |
| `gate loaded; waiting for HH idle before scan` | `debug >= 3` | ✅ | ❌ |
| `HH idle; waiting 0.1s before scan-jog` | `debug >= 3` | ✅ | ❌ |
| `scan preflight — lane N gate_status=X safe/not safe` | `debug >= 3` | ✅ | ❌ |
| `scan trigger deferred: gate N already scanning` | `debug >= 3` | ✅ | ❌ |
| `starting scan-jog (max=Xmm poll=Ys)` | `debug >= 3`; console always | ✅ | ❌ |
| `scan-jog not available while reason` | warning (always) | ✅ | ✅ |
| `tag identified — rewinding Xmm` | `info` (always) | ✅ | ❌ |
| `no tag — jogged Xmm / Xmm` | `info` (always at each step) | ✅ | ❌ |
| `print started — aborting` | warning (always) | ✅ | ✅ |
| `no tag after Xmm — rewinding` | warning (always) | ✅ | ✅ |
| `no tag found, NFC state and HH gate cache cleared after rewind` | `debug >= 3` | ✅ | ❌ |

Set `debug: 3` to observe scan start and success. Set `debug: 4` for full poll detail during scans.

---

## Happy Hare Compatibility Notes

- `MMU_SELECT GATE=N` and `MMU_TEST_MOVE MOVE=mm QUIET=1` are standard Happy Hare v2.x commands.
- `get_speed()` reads `mmu.gear_short_move_speed` from the HH Python object to compute chunk timing. Falls back to 80 mm/s if the attribute is absent.
- `mmu.get_status()['gate_status']` values: `0` = empty, `1` = available/parked, `2` = available from buffer. Scan mode triggers only on `0 → 1`. Buffer-loaded filament (`0 → 2`) does not trigger.
- `mmu.get_status()['action']` is lowercased and compared `== 'idle'` (exact). If HH changes its action string, the guard silently prevents scan mode from starting (safe-fail direction).
