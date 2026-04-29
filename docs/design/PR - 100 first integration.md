# PR - 100 first integration

## Overview

This document captures the first integration plan for NFC scan-jog coordination with Happy Hare MMU lane state and Bowden calibration data.

Scope:

- require all lanes to be safely parked or empty before any scan-jog begins
- apply that rule to both manual jog and automatic jog-on-load
- derive per-lane scan max distance from Happy Hare's saved Bowden calibration
- remove the separate NFC `scan_max_mm` configuration knob

This is a requirements, design, and pseudocode document only. It does not implement code changes.

## Requirements

1. Scan-jog must not start unless every MMU lane is in a safe pre-scan state.
2. A safe pre-scan state is:
   - lane is empty
   - or lane is parked at the gate
3. If any lane is not clearly empty or parked, scan-jog must be blocked.
4. This blocking behavior must apply to:
   - manual jog via `NFC_GATE ... JOG_SCAN=1`
   - automatic jog triggered after Happy Hare reports a lane load
5. The scan max distance must come from Happy Hare's saved Bowden calibration for the current lane.
6. The NFC-side `scan_max_mm` parameter must no longer be required in `config/nfc_vars.cfg`.
7. If the MMU vars file, Bowden-length variable, or current lane index cannot be resolved, scan-jog must fail closed and report a clear reason.

## Current system

### Current scan-jog entry points

- Manual scan-jog enters through `klippy/extras/nfc_gates/scan_jog.py` in `manual_jog_scan()`.
- Automatic scan-jog enters through `klippy/extras/nfc_gates/NFC_manager.py` in `_poll_timer_event()` when Happy Hare reports a `0 -> 1` gate-state transition and the MMU is idle.

### Current NFC config

`config/nfc_vars.cfg` currently defines:

- `scan_jog_mm`
- `scan_max_mm`
- `scan_poll_interval`
- `scan_poll_interval`

The implemented design keeps `scan_jog_mm` and `scan_poll_interval`, but removes
`scan_max_mm` and `scan_settle_time`.

### Current Happy Hare config path discovery

The live include chain resolves as follows:

- `printer.cfg` includes `mmu/base/*.cfg`
- `mmu/base/.../mmu_macro_vars.cfg` defines `[save_variables]`
- `[save_variables] filename` points to `~/printer_data/config/mmu/mmu_vars.cfg`

The resolved MMU variables file is:

`~/printer_data/config/mmu/mmu_vars.cfg`

That file contains:

`mmu_calibration_bowden_lengths = [ ... ]`

Example:

```ini
mmu_calibration_bowden_lengths = [1750.8, 1640.5, 1544.8, 1585.2, 1680.6]
```

The lane index must match `NFCGate._gate`.

## Happy Hare state semantics used by this design

Relevant Happy Hare state values observed in `mmu.py`:

- `GATE_EMPTY = 0`
- `GATE_AVAILABLE = 1`
- `GATE_AVAILABLE_FROM_BUFFER = 2`
- `FILAMENT_POS_UNLOADED = 0` meaning parked in gate

Safe interpretation for this integration:

- global filament position must be `FILAMENT_POS_UNLOADED`
- each lane's `gate_status` must be either:
  - `GATE_EMPTY`
  - or `GATE_AVAILABLE`

Blocked states:

- `GATE_UNKNOWN = -1`
- `GATE_AVAILABLE_FROM_BUFFER = 2`
- any other unexpected value
- any global `filament_pos` other than `FILAMENT_POS_UNLOADED`

## Design

### 1. Add an all-lanes preflight guard

Add a helper on the NFC side that inspects Happy Hare status across all lanes and returns:

- `ok = True` when all lanes are empty or parked
- `ok = False` plus a human-readable reason when any lane is not safe

This should be a logical check only. It should not actively move filament.

### 2. Apply the guard to both scan-jog paths

#### Manual jog

Before stopping the poll timer or starting scan mode, call the all-lanes guard. If it fails, respond to the console and return without jogging.

#### Automatic jog

Keep the existing trigger logic based on Happy Hare gate status and idle state, but insert the all-lanes guard before `_start_scan_mode()`.

If the guard fails:

- do not start scan mode
- log the reason
- either clear or defer the pending scan trigger according to the final retry policy

Recommended behavior for first integration:

- fail the pending attempt for that load event
- require a fresh safe state or a new load event before scan-jog starts

This is simpler and avoids retry loops while another lane remains unsafe.

### 3. Read per-lane Bowden length from `mmu_vars.cfg`

Replace fixed `scan_max_mm` with a per-lane max distance read from:

- resolved MMU vars file path
- parsed `mmu_calibration_bowden_lengths`
- indexed by `NFCGate._gate`

Use `ast.literal_eval` to parse the bracketed list safely.

### 4. Cache resolved MMU calibration data

To avoid repeated filesystem work:

- resolve the MMU vars path once per gate instance and cache it
- cache parsed Bowden lengths
- refresh the cache when a new scan-jog is about to start

This keeps scan polling lightweight while still allowing updated calibration data to be picked up between scans.

### 5. Remove NFC `scan_max_mm` config

The NFC side should no longer accept or advertise a separate max scan distance in config or defaults.

That means removing:

- config parsing for `scan_max_mm`
- help text and comments describing it
- tests that assert the old default/override behavior

## Where the code changes would be made

### `klippy/extras/nfc_gates/hh_status.py`

Add helper support for whole-MMU state inspection.

Planned additions:

- a fuller status snapshot that includes:
  - full `gate_status` list
  - global `filament_pos`
- helper methods/functions:
  - `lane_is_parked_or_empty(...)`
  - `all_lanes_parked_or_empty(...)`

Purpose:

- keep Happy Hare status parsing in one place
- avoid duplicating state interpretation inside `scan_jog.py` and `NFC_manager.py`

### `klippy/extras/nfc_gates/NFC_manager.py`

Update configuration and gate object state.

Planned changes:

- remove `scan_max_mm` from `NFCGateDefaults`
- remove `self._scan_max_mm` from `NFCGate`
- add cached fields for:
  - resolved MMU vars file path
  - parsed Bowden-length list
  - current scan max distance for the active lane
- add instance helpers:
  - resolve MMU vars path from config include chain
  - read and parse `mmu_calibration_bowden_lengths`
  - get current lane scan max distance
  - evaluate all-lanes parked/empty preflight

Automatic trigger path to update:

- `_poll_timer_event()`

Specifically:

- keep existing `0 -> 1`, HH idle, not printing logic
- before `_start_scan_mode()`, run:
  - all-lanes safety preflight
  - lane Bowden-length resolution

### `klippy/extras/nfc_gates/scan_jog.py`

Update scan-jog start and runtime behavior.

Planned changes:

- in `manual_jog_scan()`:
  - call all-lanes safety preflight
  - resolve current lane scan max distance
  - block with console message if either fails
- in `start()`:
  - initialize scan mode using the resolved lane max distance
- in `step_event()`:
  - stop using fixed `_scan_max_mm`
  - use current lane derived max distance for:
    - limit checks
    - remaining distance
    - console and log messages

### `config/nfc_vars.cfg`

Remove:

- `scan_max_mm`
- its surrounding comments

Keep:

- `scan_jog_mm`
- `scan_poll_interval`
- `scan_poll_interval`
- `scan_enabled`

### Tests

#### `tests/test_nfc_gate_config.py`

Update config expectations:

- remove `scan_max_mm` default and override tests
- add tests for the new behavior if config validation is added around MMU path discovery

#### `tests/test_scan_jog_mode.py`

Add coverage for:

- manual jog blocked when another lane is not parked/empty
- automatic jog blocked by unsafe lane state
- manual jog blocked when Bowden list is missing
- manual jog blocked when lane index is out of range
- derived lane max controls total scan distance
- derived lane max controls final chunk size near the limit

## Pseudocode

### All-lanes guard

```text
def _all_lanes_parked_or_empty(self):
    status = read_full_hh_status(self.printer)
    if not status.present:
        return False, "Happy Hare status unavailable"

    if status.filament_pos != FILAMENT_POS_UNLOADED:
        return False, "filament is not parked"

    for lane, gate_state in enumerate(status.gate_statuses):
        if gate_state not in (GATE_EMPTY, GATE_AVAILABLE):
            return False, "lane %d is not parked or empty (status=%d)" % (
                lane, gate_state)

    return True, None
```

### Manual jog path

```text
def manual_jog_scan(gate, gcmd):
    reject if failed
    reject if printing
    reject if Happy Hare busy
    reject if another scan is active
    reject if this gate is already scanning

    ok, reason = gate._all_lanes_parked_or_empty()
    if not ok:
        gcmd.respond_info("scan-jog blocked: %s" % reason)
        return

    max_mm = gate._get_lane_scan_max_mm()
    if max_mm is None:
        gcmd.respond_info("scan-jog blocked: missing bowden length for gate %d" % gate._gate)
        return

    gate.reactor.update_timer(gate._poll_timer, gate.reactor.NEVER)
    gate._start_scan_mode(max_mm=max_mm)
```

### Automatic jog path

```text
def _poll_timer_event(self, eventtime):
    if scan_enabled:
        hh = self._read_hh_status(eventtime)
        detect 0 -> 1 transition
        wait for HH idle settle

        if pending and curr == 1 and hh.idle and not printing:
            ok, reason = self._all_lanes_parked_or_empty()
            if not ok:
                log "scan blocked: %s" % reason
                clear or defer pending
                return next_poll

            max_mm = self._get_lane_scan_max_mm()
            if max_mm is None:
                log "scan blocked: no bowden calibration for gate %d" % self._gate
                clear pending
                return next_poll

            self._start_scan_mode(max_mm=max_mm)
            return NEVER
```

### MMU vars resolution

```text
def _resolve_mmu_vars_path(self):
    locate printer.cfg directory
    parse printer.cfg
    find include matching mmu/base/*.cfg
    inspect matching files for [save_variables]
    extract filename value
    expand ~
    return absolute path
```

### Bowden list parsing

```text
def _load_bowden_lengths(self):
    path = self._resolve_mmu_vars_path()
    if not path or file missing:
        return None

    read file line by line
    find "mmu_calibration_bowden_lengths = ..."
    parse value with ast.literal_eval
    validate it is a list of numbers
    return list of floats
```

### Lane-specific max distance

```text
def _get_lane_scan_max_mm(self):
    lengths = self._load_bowden_lengths()
    if lengths is None:
        return None
    if self._gate < 0 or self._gate >= len(lengths):
        return None
    return float(lengths[self._gate])
```

## Non-goals

- no automatic corrective movement to park unsafe lanes
- no use of `MMU_CHECK_GATE` as part of scan-jog preflight
- no changes to Happy Hare behavior itself for this first integration

## Design note: why not call `MMU_CHECK_GATE`

`MMU_CHECK_GATE` is intentionally active and invasive. It can unload the current tool, check gates by loading/unloading them, and modify MMU state while doing so.

That is useful for maintenance, but not appropriate as a scan-jog preflight check. For this integration, the NFC side should only inspect current Happy Hare state and decide whether to proceed.

## Summary

This first integration should:

- block scan-jog unless all lanes are safe
- derive scan max from Happy Hare's calibrated Bowden lengths
- remove redundant NFC max-distance config
- keep all preflight logic read-only and local to the NFC side
