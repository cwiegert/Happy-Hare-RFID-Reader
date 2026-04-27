# Design: Polling State Machine

> Engineering reference — not end-user documentation.

---

## Overview

Each NFC gate runs an independent timer-driven polling loop on the Klipper reactor thread. The loop reads a physical tag, resolves a spool ID, feeds the result through a debounce state machine, and fires GCode macros only when state actually changes. The loop self-suspends when Happy Hare already has the spool assigned, then resumes when the gate is cleared.

---

## Timer Heartbeat

```python
self._poll_timer = self.reactor.register_timer(self._poll_timer_event)
```

`register_timer` parks the timer at `reactor.NEVER` — it fires only after an explicit `update_timer` call.

`_poll_timer_event` is the heartbeat. It has two distinct responsibilities:

1. **Scan-jog edge detection** (when `scan_enabled = True`): reads HH gate_status from a Python dict (no I2C), watches for the 0→1 load transition, and enters scan mode when conditions are met.
2. **Normal poll dispatch**: calls `_poll()` when the gate is loaded and scan mode is not active.

```python
def _poll_timer_event(self, eventtime):
    if not self._polling:
        return self.reactor.NEVER
    if self._failed:
        self._polling = False
        return self.reactor.NEVER

    if self._scan_enabled:
        hh = self._read_hh_status(eventtime)
        if hh.present and self._gate < hh.gate_count:
            curr = hh.status                  # gate_status for this gate
            prev = self._prev_gate_status
            self._prev_gate_status = curr

            if curr == 0:
                # Gate empty — handle _hh_load_paused resume/suspend; skip I2C
                ...
                return self.reactor.monotonic() + self._poll_interval

            # 0→1 edge: arm pending, wait for HH to settle
            if prev == 0 and curr == 1:
                self._scan_pending = True
                self._scan_idle_ready_time = 0.0

            # Pending: fire once HH is idle and settled
            if self._scan_pending and curr == 1 and hh.idle and not self._is_printing():
                now = self.reactor.monotonic()
                if self._scan_idle_ready_time <= 0.0:
                    self._scan_idle_ready_time = now + 2.0
                    return self._scan_idle_ready_time   # wait 2 s
                if now < self._scan_idle_ready_time:
                    return self._scan_idle_ready_time
                # 2 s elapsed — enter scan if lock is free
                self._scan_pending = False
                if NFCGate._active_scan_gate is not None:
                    self._scan_pending = True           # re-arm, retry in 1 s
                    self._scan_idle_ready_time = now + 1.0
                    return self._scan_idle_ready_time
                self._start_scan_mode()
                return self.reactor.NEVER               # poll resumes when scan exits

            if self._scan_pending:
                return self.reactor.monotonic() + 1.0

    # Normal poll path
    try:
        self._poll()
    except Exception:
        logger.exception("nfc_gate: [%s] poll error", self._name)
    return self.reactor.monotonic() + self._poll_interval
```

Key invariants:
- Returns `reactor.NEVER` to park the timer.
- Returns `reactor.monotonic() + poll_interval` to reschedule. The next firing is `poll_interval` seconds after the previous call **returns** — the poll duration is paid before the next interval starts.
- The bare `except Exception` is intentional: an unhandled exception from a reactor timer kills Klipper. Errors are logged and swallowed so polling continues.
- `_poll()` does not raise from I2C errors — `read_tag()` catches those internally and returns `None`. The outer `except` catches unexpected errors in Spoolman or GateState paths only.

Starting and stopping polling:

```python
# Start
self._polling = True
self.reactor.update_timer(self._poll_timer, self.reactor.NOW)

# Stop
self._polling = False
self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
```

---

## `_poll()` — Full Cycle Logic

`_poll()` is called by both `_poll_timer_event` (normal polling) and `scan_jog.step_event` (scan mode). It runs the full read-resolve-debounce-dispatch cycle.

```
_poll()
  ├─ [A] suspend check  (_hh_gate_matches_current_spool AND current_spool set)
  │    └─ zero miss_count, return — PN532 antenna never pulsed
  ├─ [B] resume detection  (_hh_load_paused was True, now condition False)
  │    └─ clear GateState, clear _hh_confirmed_spool
  ├─ [C] _check_hh_cleared()  — detect external HH gate map changes
  ├─ [D] reader.read_tag()  — I2C → PN532 → UID hex string or None
  ├─ [E] spoolman.lookup_spool_by_uid()  — UID → spool_id (TTL cached)
  ├─ [F] GateState.process_read(uid_hex, spool_id, scan_mode)  → event or None
  └─ [G] print-guard / suppress / dispatch logic
       ├─ printing?  → log suppression, return — no writes, no HH dispatch
       ├─ startup seed match?  → skip HH dispatch only
       └─ normal path  → Spoolman location write + KlipperInterface.dispatch()
```

Steps [A] and [B] are the suspend/resume gate. Steps [D]–[G] only run when the gate is actively scanning.

Returns `True` if a tag was read (used by scan mode to detect success), `False` otherwise.

### [G] Print Guard

Before any write is issued, `_poll()` checks `_is_printing()`, which returns `True` only when `print_stats.state == 'printing'` (Klipper's exact string for an actively running print).

Klipper `print_stats.state` values:

| State | `_is_printing()` | Writes allowed |
|---|---|---|
| `printing` | True | No — suppressed |
| `paused` | False | **Yes** — filament swaps permitted |
| `standby`, `complete`, `cancelled`, `error` | False | Yes |

A print job can run for hours. During that entire window, with `state == 'printing'`, Spoolman and HH writes are suppressed. The moment the user pauses (`state → 'paused'`), the guard clears and a filament load, unload, or spool swap goes through the full write path normally — exactly as it would outside a print.

What the guard allows vs suppresses:

- **Allowed:** I2C tag read [D], Spoolman UID lookup [E], GateState update [F]. The tag is identified and state is tracked internally.
- **Suppressed:** Spoolman location PATCH (`update_spool_location`, `clear_spool_location`) and HH GCode dispatch (`KlipperInterface.dispatch`). No external writes happen.

At `debug >= 3`, a log line is written to `nfc_reader.log` noting the suppression:

```
nfc_gate: [laneN] gate N — EVENT_CHANGED detected during print; Spoolman and HH dispatch suppressed
```

When the print finishes (`state → 'complete'` or `'cancelled'`), the next normal poll tick re-reads the tag. If GateState still reflects a change, `process_read()` fires the event again and the full write path executes normally. No manual intervention is needed to sync state after a print.

**Why reads are still allowed during printing:** I2C traffic is harmless and maintaining an accurate internal picture of what is on each gate costs nothing. Suppressing writes only — not reads — means the system is always up to date and responds immediately when the print ends or is paused.

### [A] Suspend Check

```python
if (not self._scan_mode
        and self._hh_gate_matches_current_spool()
        and self._state.current_spool is not None):
    if not self._hh_load_paused:
        self._hh_load_paused = True
        logger.info("... suspending poll until ejected")
    self._state.miss_count = 0
    return
```

`_hh_gate_matches_current_spool()` returns True when:
- `mmu` object is present (`hh.present`)
- `hh.spool == nfc_spool` — HH's assigned spool ID **exactly matches** what NFC has cached

Both the HH match and `current_spool is not None` must be true to suspend. Requiring `current_spool` ensures the UID is populated in NFC status before scanning stops.

"Suspended" does not mean the timer stops. The timer fires every `poll_interval`. `_poll()` checks HH and returns early before `read_tag()`. No I2C traffic. `miss_count` is zeroed each cycle to prevent the debounce counter from accumulating while the tag is physically present.

### [B] Resume Detection

```python
if self._hh_load_paused:
    self._hh_load_paused    = False
    self._state.current_uid   = None
    self._state.current_spool = None
    self._state.miss_count    = 0
    self._hh_confirmed_spool  = None
    logger.info("... filament unloaded; resuming NFC scan")
```

When `_hh_gate_matches_current_spool()` returns False after the gate was suspended, the full GateState is reset. The next physical tag read fires a fresh `EVENT_CHANGED`.

---

## GateState — Debounce State Machine

`GateState` is the per-gate single source of truth for what the NFC reader currently sees.

```python
class GateState:
    gate              # lane gate number (read-only)
    current_uid       # last confirmed UID, or None if gate empty
    current_spool     # last resolved spool_id, or None
    miss_count        # consecutive missed reads since last tag
    absent_threshold  # misses required before REMOVED fires (from config)
```

### `process_read(uid_hex, spool_id, scan_mode=False) → event_tuple | None`

```
uid_hex present, same uid+spool as current  → None  (quiet — no GCode)
uid_hex present, uid or spool differs       → EVENT_CHANGED  (spool_id not None)
                                            → EVENT_UID_ONLY (spool_id is None)
uid_hex is None, scan_mode=True             → None  (scan rotation — not an absence)
uid_hex is None, miss_count < threshold     → None  (still counting)
uid_hex is None, miss_count >= threshold, current_uid was set → EVENT_REMOVED
uid_hex is None, current_uid was None       → None  (already empty)
```

When `scan_mode=True`, a no-read result does not increment `miss_count`. A missed NFC read during a deliberate spool rotation is not an absence event.

Event types returned as `(event_type, gate, uid_hex, spool_id)` tuples:

| Constant | uid/spool in tuple | Meaning |
|---|---|---|
| `EVENT_CHANGED` | uid=present, spool=int | Known spool confirmed on this gate |
| `EVENT_UID_ONLY` | uid=present, spool=None | Tag seen but UID not in Spoolman |
| `EVENT_REMOVED` | uid=None, spool=old_spool | Tag gone for `absent_threshold` polls |

`EVENT_UID_ONLY` fires when the Spoolman lookup returns no match — including when `_spoolman is None` (Spoolman not configured). In the no-Spoolman case, every tag read produces `EVENT_UID_ONLY` and calls `_NFC_TAG_NO_SPOOL`, which logs the UID. HH is not updated.

Removal debounce: a single RF miss does not trigger removal. The tag must be absent for `absent_threshold` consecutive polls. At the default 10 s interval and threshold of 3, removal fires after ~30 s of confirmed absence.

---

## `_check_hh_cleared()`

Called at step [C], runs every active (non-suspended) poll cycle. Detects two cases where HH's gate map diverges from NFC's cached state:

1. **HH cleared the gate** — `gate_spool_id[gate] < 0`
2. **HH has a different spool** — `gate_spool_id[gate] != current_spool`

Preconditions — both must be true before the check executes:
- `_state.current_spool is not None`
- `_hh_confirmed_spool == _state.current_spool` — HH previously acknowledged this exact spool

The `_hh_confirmed_spool` guard prevents a dispatch-clear-redispatch loop. When NFC dispatches `EVENT_CHANGED`, `_hh_confirmed_spool` is set immediately (optimistically). HH may not have processed the macro yet. Without the guard: `_check_hh_cleared()` would see HH still empty → clear cache → `process_read()` fires `EVENT_CHANGED` again → loop. The guard ensures the check only acts after HH has confirmed this spool at least once.

When a mismatch is detected, `current_uid`, `current_spool`, and `miss_count` are all cleared so the next tag read fires a fresh `EVENT_CHANGED`.

---

## Startup Seeding

On `klippy:connect`, `_handle_connect()` schedules a one-shot `_delayed_init` timer for 2 seconds later. `_delayed_init` runs PN532 init, then immediately calls `_seed_cache_from_hh()`.

`_seed_cache_from_hh()` reads HH's current gate map via `hh_status.read()` and seeds this gate's cache:

```python
hh = hh_status.read(self.printer, self._gate, eventtime)

if hh.assigned:
    self._hh_seed_spool_id  = hh.spool
    self._hh_seed_available = hh.available

    if hh.available and self._spoolman is not None:
        uid = self._spoolman.get_uid_for_spool(hh.spool)  # reverse lookup
        if uid:
            self._state.current_uid   = uid
            self._state.current_spool = hh.spool
            self._hh_confirmed_spool  = hh.spool
```

**Path 1: Spoolman configured and returns a UID** — `current_uid`, `current_spool`, and `_hh_confirmed_spool` are all pre-populated. On the first `_poll()`, both suspend conditions are immediately met. `NFC_GATE_STATUS` shows the correct UID before any physical scan.

**Path 2: Spoolman returns no UID, or Spoolman not configured** — only `_hh_seed_spool_id` and `_hh_seed_available` are set. Polling proceeds normally. On the first physical scan that resolves to the seeded spool:
- `_hh_seed_available == True` → suppress `EVENT_CHANGED` dispatch (HH already knows)
- `_hh_seed_available == False` → let dispatch through to set `AVAILABLE=1`

The seed (`_hh_seed_spool_id`) is always cleared after the first `EVENT_CHANGED` poll regardless of match — one-shot.

**`NFC_HH_SYNC_CACHE` macro** is a user-callable re-sync path. It issues `NFC_GATE GATE=n HH_SYNC=1 SPOOL_ID=<n>` for each lane, which sets `_hh_seed_spool_id` only. No Spoolman reverse-lookup, no state pre-population. Useful when the automatic seed failed because HH wasn't initialized yet at `_delayed_init` time.

---

## CLEAR_CACHE Behavior

`NFC_GATE GATE=n CLEAR_CACHE=1` clears the Spoolman TTL cache and resets `current_spool` to force a fresh lookup on the next poll.

What it does:
1. Sets `_state.current_spool = None` (keeps `current_uid`)
2. Calls `_spoolman.clear_cache()` — clears the in-memory UID→spool map
3. Calls `_reader._clear_current_card()` — resets cached tag state in the driver
4. Sets `_suppress_next_dispatch_uid` and `_suppress_next_dispatch_spool` (see note)

**Behavior on next poll:** `current_uid` is still set but `current_spool` is None. Spoolman is queried fresh. If it returns the same spool, `process_read()` sees the spool has changed (`current_spool` was None, now it's 42) and fires `EVENT_CHANGED`. This re-dispatches `_NFC_SPOOL_CHANGED` to HH even though the spool didn't actually change.

**Note on `_suppress_next_dispatch_uid` / `_suppress_next_dispatch_spool`:** These variables are set in `_clear_spool_cache()` but are not checked anywhere in `_poll()`. They have no effect on current dispatch behavior. They are reserved for a future suppress-on-same-result optimization.

---

## State Variable Reference

| Variable | Type | Meaning |
|---|---|---|
| `_polling` | bool | Timer is active; `_poll_timer_event` will reschedule |
| `_failed` | bool | Reader init failed; polling halted until `INIT=1` |
| `_hh_load_paused` | bool | Suspended: HH + NFC both have the same spool confirmed |
| `_hh_confirmed_spool` | int\|None | Last spool NFC dispatched; gates `_check_hh_cleared` |
| `_hh_seed_spool_id` | int\|None | Spool from HH map at startup; one-shot suppress on first matching scan |
| `_hh_seed_available` | bool | Whether HH had `gate_status >= 1` for the seeded spool |
| `_suppress_next_dispatch_uid` | str\|None | Set by CLEAR_CACHE; not currently checked in `_poll()` |
| `_suppress_next_dispatch_spool` | int\|None | Set by CLEAR_CACHE; not currently checked in `_poll()` |
| `_scan_pending` | bool | 0→1 edge seen; armed until scan starts or gate empties |
| `_scan_idle_ready_time` | float | Reactor timestamp for 2s HH-idle settle delay |
| `_scan_next_chunk_time` | float | Reactor timestamp when next scan chunk may fire |
| `GateState.current_uid` | str\|None | Last UID confirmed by `process_read()` |
| `GateState.current_spool` | int\|None | Last resolved spool_id confirmed by `process_read()` |
| `GateState.miss_count` | int | Consecutive polls since last tag; resets on any read |
| `GateState.absent_threshold` | int | Misses before `EVENT_REMOVED` fires (from config) |
