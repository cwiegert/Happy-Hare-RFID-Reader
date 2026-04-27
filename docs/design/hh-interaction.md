# Design: Happy Hare ↔ NFC_Manager Interaction

> Engineering reference — not end-user documentation.

---

## Interaction Model: Unidirectional Push + Poll

NFC does not have a direct Python API to Happy Hare. HH is a separate Klipper extra with its own object namespace. NFC interacts with HH in two ways:

```
NFC → HH:   GCode macro dispatch  (_NFC_SPOOL_CHANGED, _NFC_SPOOL_REMOVED, _NFC_TAG_NO_SPOOL)
HH → NFC:   none — NFC polls HH status directly via mmu.get_status()
```

There is no callback registration, no event subscription, no shared queue. NFC pushes to HH by running GCode; NFC reads from HH by calling `mmu.get_status()` at the start of each poll cycle. HH never calls into NFC.

---

## HH Status Adapter: `hh_status.py`

All reads from the HH Python object are routed through `hh_status.read()`, which returns an `HHGateStatus` data object. This isolates all raw dict access and type coercion in one place.

```python
# hh_status.py
class HHGateStatus:
    present      # bool — True if 'mmu' object exists in Klipper
    gate         # int  — gate index requested
    spool        # int  — gate_spool_id[gate], or -1
    status       # int  — gate_status[gate], or 0
    action       # str  — mmu.action, lowercased
    active_gate  # int  — currently active gate index
    filament_pos # int  — filament position in loading sequence
    gate_count   # int  — len(gate_spool_id)

    @property assigned:   spool > 0
    @property available:  status >= 1
    @property idle:       action == 'idle'
```

Usage in `NFC_manager.py`:

```python
def _read_hh_status(self, eventtime=None):
    if eventtime is None:
        eventtime = self.reactor.monotonic()
    return hh_status.read(self.printer, self._gate, eventtime)
```

If `mmu` is not registered (HH not installed), `hh_status.read()` returns `HHGateStatus(present=False, ...)` and all properties degrade to safe defaults.

---

## Print Guard: Writes Suppressed During Printing

Spoolman location writes and HH GCode dispatch are both suppressed while `print_stats.state == 'printing'` — Klipper's exact state for an actively running print. A print job may run for hours; the guard covers the entire window.

**Paused prints are explicitly excluded.** When the user pauses (`state == 'paused'`), the guard clears immediately. A filament load, unload, or spool swap during a pause goes through the full write path normally — Spoolman is updated and HH dispatch fires. This is intentional: pausing to swap filament is exactly the moment the system should respond.

| `print_stats.state` | Writes suppressed |
|---|---|
| `printing` | Yes |
| `paused` | No |
| `standby`, `complete`, `cancelled`, `error` | No |

I2C reads, Spoolman UID lookups, and GateState updates continue in all states — the system always tracks what is on each gate internally, it just does not act on changes while the print is running.

This matches HH's own behaviour: HH does not permit gate map edits during an active (non-paused) print. Issuing `MMU_GATE_MAP` or a Spoolman PATCH while `state == 'printing'` could interfere with the in-progress job.

When the print ends or is cancelled, the next poll cycle re-reads the tag and dispatches any pending change automatically. No user action is required to re-sync.

The guard applies to all event types: `EVENT_CHANGED`, `EVENT_UID_ONLY`, and `EVENT_REMOVED`. Scan-jog mode has a separate guard that prevents the jog routine from starting while `state == 'printing'`; scan-jog is also permitted during a pause.

---

## NFC → HH: GCode Macro Dispatch

`KlipperInterface.dispatch()` schedules a GCode script via `reactor.register_callback()`. This only runs when the print guard above is not active:

```
EVENT_CHANGED   → "_NFC_SPOOL_CHANGED GATE={gate} SPOOL_ID={spool_id} UID={uid}"
EVENT_UID_ONLY  → "_NFC_TAG_NO_SPOOL GATE={gate} UID={uid}"
EVENT_REMOVED   → "_NFC_SPOOL_REMOVED GATE={gate}"
```

These macros are defined in `nfc_macros.cfg` and are the only user-editable integration point. If a HH version uses different command syntax, only the macro bodies need updating — the NFC Python layer is unaffected.

### `_NFC_SPOOL_CHANGED`

```gcode
MMU_GATE_MAP GATE={gate} SPOOLID={spool_id} AVAILABLE=1 SYNC=1 QUIET=1
MMU_GATE_MAP GATE={gate} APPLY=1
```

`SYNC=1` tells HH to synchronize the assignment to Spoolman. `AVAILABLE=1` marks the gate as having filament loaded. `APPLY=1` pushes the updated map into the active print state. NFC also calls `_spoolman.update_spool_location(spool_id, gate)` directly before dispatching, setting the Spoolman `location` field to `MMU_GATE_<n>`. Both the Spoolman PATCH and this macro call are skipped when the print guard is active.

### `_NFC_SPOOL_REMOVED`

```gcode
{% set mmu_action = printer.mmu.action | default("") | lower %}
{% if "load" in mmu_action or "unload" in mmu_action or "homing" in mmu_action %}
    { action_respond_info("... ignoring removal.") }
{% else %}
    MMU_GATE_MAP GATE={gate} SPOOLID=-1 AVAILABLE=0 SYNC=1 QUIET=1
    MMU_GATE_MAP GATE={gate} APPLY=1
{% endif %}
```

**MMU action guard:** before clearing the gate, the macro reads `printer.mmu.action`. If the MMU is loading, unloading, or homing, the removal event is silently ignored. This prevents a tag momentarily leaving read range during filament movement from triggering a spurious gate clear. The removal will be retried on the next poll cycle if the tag is still absent.

NFC also calls `_spoolman.clear_spool_location(spool_id)` to clear the `location` field in Spoolman.

### `_NFC_TAG_NO_SPOOL`

```gcode
{ action_respond_info("NFC gate %d: tag UID %s is not registered in Spoolman.\n..." % ...) }
```

Logs the UID to the console and prompts the user to register it. Does not call `MMU_GATE_MAP`. No HH state changes.

---

## HH → NFC: Status Polling

NFC reads HH state by calling `hh_status.read()` which calls `mmu.get_status()` on the HH Python object. This is a synchronous in-process call — no GCode queue, no I2C.

Used in two places:

**`_hh_gate_matches_current_spool()`** — returns True when `hh.spool == nfc_spool` (exact match, not just `> 0`). Called at the top of every `_poll()` to decide whether to suspend scanning. Also called from `_poll_timer_event` on every tick when `gate_status == 0`.

**`_check_hh_cleared()`** — compares NFC's cached spool against HH's current value. Detects:
- HH cleared the gate: `not hh.assigned`
- HH has a different spool: `hh.spool != nfc_spool`

Only runs when `_hh_confirmed_spool == _state.current_spool` — prevents a loop where NFC dispatches a spool, HH hasn't processed it yet, the check sees the old HH state, and clears the cache before HH can acknowledge.

---

## Startup Seeding

On `klippy:connect`, `_delayed_init` runs 2 seconds later and calls `_seed_cache_from_hh()` — directly in Python, not via a macro.

```python
# Simplified from _seed_cache_from_hh()
hh = hh_status.read(self.printer, self._gate, eventtime)

if hh.assigned:
    self._hh_seed_spool_id  = hh.spool
    self._hh_seed_available = hh.available

    if hh.available and self._spoolman is not None:
        uid = self._spoolman.get_uid_for_spool(hh.spool)  # Spoolman reverse-lookup
        if uid:
            self._state.current_uid   = uid        # cache fully populated
            self._state.current_spool = hh.spool   # polling suspends immediately
            self._hh_confirmed_spool  = hh.spool
```

When the Spoolman reverse-lookup succeeds: the gate goes directly to the "suspended" state on the first `_poll()` cycle. `NFC_GATE_STATUS` shows the correct UID immediately, before any physical scan.

When the Spoolman lookup fails (no UID in Spoolman, or Spoolman not configured): only `_hh_seed_spool_id` is set. Polling proceeds normally. On the first physical scan that resolves to the seeded spool, the dispatch is suppressed (HH already knows).

**`NFC_HH_SYNC_CACHE` macro** is a user-callable re-sync path — it calls `NFC_GATE GATE=n HH_SYNC=1 SPOOL_ID=<n>` for each lane, which sets `_hh_seed_spool_id` only (no Spoolman reverse-lookup, no state pre-population). Its comment says: "startup seeding happens automatically in Python (`_delayed_init`). This macro exists for user-triggered re-sync and for cases where the Python seed failed."

---

## Suspend/Resume Cycle: Full Trace

```
1. Gate 0 is empty. Polling runs every 10 s. read_tag() fires each cycle.
   GateState: uid=None spool=None misses=0

2. User loads a spool. On next poll, NFC reads the tag.
   read_tag() → uid_hex = "A3F200CC"

3. SpoolmanClient.lookup_spool_by_uid("A3F200CC") → spool_id = 42
   (HTTP request if cache cold, cache hit if warm)

4. GateState.process_read("A3F200CC", 42) → EVENT_CHANGED
   GateState: uid="A3F200CC" spool=42 misses=0

5. Not suppressed (no matching seed, no CLEAR_CACHE pending).
   _spoolman.update_spool_location(42, gate=0)   → Spoolman: location = "MMU_GATE_0"
   KlipperInterface.dispatch(EVENT_CHANGED, 0, "A3F200CC", 42)
   _hh_confirmed_spool = 42   (set optimistically when callback is SCHEDULED)

6. reactor.register_callback fires → gcode.run_script:
       _NFC_SPOOL_CHANGED GATE=0 SPOOL_ID=42 UID=A3F200CC
   Macro executes:
       MMU_GATE_MAP GATE=0 SPOOLID=42 AVAILABLE=1 SYNC=1 QUIET=1
       MMU_GATE_MAP GATE=0 APPLY=1
   HH now has gate_spool_id[0] = 42.

7. Next poll cycle (10 s later):
   hh = hh_status.read(...)  → hh.spool = 42
   _hh_gate_matches_current_spool() → True (hh.spool == nfc_spool == 42)
   current_spool = 42, not None → both suspend conditions met
   _hh_load_paused = True
   miss_count zeroed, return early. PN532 not pulsed.

   Note: _hh_confirmed_spool is set in step 5 when the callback is *scheduled*,
   before HH actually processes the macro. If a poll fires in the narrow window
   between scheduling and HH execution, hh.spool may still show -1. In that window
   _hh_gate_matches_current_spool() returns False and the poll reads the tag again —
   process_read() sees same uid+spool → None (quiet). No harm done.

8. Timer keeps firing every 10 s. Each cycle:
   _hh_gate_matches_current_spool() → True, current_spool set
   → miss_count zeroed, return. No I2C traffic.

9. User ejects spool. HH clears gate:
   gate_spool_id[0] = -1

10. Next poll cycle: _hh_gate_matches_current_spool() → False (hh.spool != 42).
    _hh_load_paused was True → resume path fires:
      _hh_load_paused    = False
      current_uid        = None
      current_spool      = None
      miss_count         = 0
      _hh_confirmed_spool = None

    _check_hh_cleared() runs — current_spool is now None, returns early.
    read_tag() fires. Gate is empty → uid_hex = None.
    GateState.process_read(None, None) → miss_count = 1 → None (still counting)

11. After absent_threshold polls (3 × 10 s = ~30 s) with no tag:
    GateState.process_read(None, None) → EVENT_REMOVED
    _NFC_SPOOL_REMOVED GATE=0 dispatched.
    Macro checks printer.mmu.action — if idle, clears HH gate map.
```

---

## Happy Hare Compatibility Contract

### `mmu.get_status()` — Required Keys

Called via `hh_status.read()` which accesses these keys with `.get(key, default)`:

| Key | Type | Used in | Purpose |
|---|---|---|---|
| `gate_spool_id` | `list[int]` | `_seed_cache_from_hh`, `_check_hh_cleared`, `_hh_gate_matches_current_spool`, `_hh_filament_label` | Spool ID per gate; `-1` = empty/unknown |
| `gate_status` | `list[int]` | `_seed_cache_from_hh`, `_hh_filament_label`, scan-jog edge detection | Availability flag per gate; `0` = unavailable, `≥1` = available |
| `action` | `str` | all `_read_hh_status()` callers | MMU action string, lowercased; `'idle'` checked in `HHGateStatus.idle` |
| `gate` | `int` | `_hh_filament_label` | Index of currently active gate |
| `filament_pos` | `int` | `_hh_filament_label` | Current filament position; `0` = at/before gate |

Missing keys degrade to safe defaults (`HHGateStatus` fields initialize to `-1` / `0` / `''`).

### `printer.mmu.*` — Jinja2 Template Variables

Accessed in `nfc_macros.cfg` via the Klipper template engine:

| Variable | Type | Used in | Purpose |
|---|---|---|---|
| `printer.mmu.action` | `str` | `_NFC_SPOOL_REMOVED` | MMU action string. Removal suppressed when containing `"load"`, `"unload"`, or `"homing"`. |
| `printer.mmu.num_gates` | `int` | `NFC_HH_SYNC_CACHE` | Total configured MMU gates; used to iterate the gate map. |
| `printer.mmu.gate_spool_id` | `list[int]` | `NFC_HH_SYNC_CACHE` | Same as `gate_spool_id` above. |
| `printer.mmu.gate_status` | `list[int]` | `NFC_HH_SYNC_CACHE` | Same as `gate_status` above. |

### GCode Commands Issued

All GCode is dispatched from `nfc_macros.cfg`. The NFC Python layer never calls HH commands directly.

| Command | Parameters | Issued from | Purpose |
|---|---|---|---|
| `MMU_GATE_MAP` | `GATE=N SPOOLID=N AVAILABLE=1 SYNC=1 QUIET=1` | `_NFC_SPOOL_CHANGED` | Assign spool to gate and mark available |
| `MMU_GATE_MAP` | `GATE=N APPLY=1` | `_NFC_SPOOL_CHANGED`, `_NFC_SPOOL_REMOVED` | Push updated map into active print state |
| `MMU_GATE_MAP` | `GATE=N SPOOLID=-1 AVAILABLE=0 SYNC=1 QUIET=1` | `_NFC_SPOOL_REMOVED` | Clear gate assignment |
| `MMU_SELECT` | `GATE=N` | scan-jog first jog only | Set active gate for `MMU_TEST_MOVE` |
| `MMU_TEST_MOVE` | `MOVE=mm QUIET=1` | scan-jog each jog step and rewind | Drive gear stepper |

`SYNC=1` tells HH to synchronise the assignment to Spoolman. `QUIET=1` suppresses HH console output.

### Version Requirements

These parameters (`SYNC`, `QUIET`, `APPLY`) were introduced in **Happy Hare v2.x**. The NFC macros are not compatible with Happy Hare v1.x. No minimum patch version within v2 has been formally validated.

---

## Design Invariants

- NFC does not read or write HH's internal Python objects directly — only via `hh_status.read()` (read) and GCode macros (write).
- NFC does not listen for HH events (`printer.register_event_handler('mmu:...')` is not used).
- NFC does not call `MMU_GATE_MAP` directly — only the user-editable macros in `nfc_macros.cfg` do.
- NFC does not disable itself when HH is absent. With no `mmu` object: scanning never suspends, `_check_hh_cleared()` is a no-op, and GCode dispatches still fire — but HH macros will fail with "Unknown command" if HH isn't installed.
