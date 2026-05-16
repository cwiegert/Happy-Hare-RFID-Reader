# Design: Left-Neighbor Tag Interference During Scan-Jog

> Engineering reference. Not end-user documentation.
> Status: **Implemented** — `scan_jog.py` mitigation, `nfc_manager.py`
> lane lookup support, and scan-jog tests.

## Problem

Some tagged spools expose NFC/RFID tags on both sides of the spool. In an EMU
layout where each PN532 reader is mounted on the left side of its own spool, the
reader for gate `N` can occasionally see the tag on gate `N - 1` instead of the
spool currently being scan-jogged.

This is most likely during scan-jog:

- gate `N` is rotating to find its own tag
- gate `N - 1` is already loaded and parked
- one side tag on gate `N - 1` is sitting in gate `N` reader's field

The current NFC state machine assumes the UID returned by a gate reader belongs
to that gate. That assumption is still correct for normal operation, but it can
misassign a spool during this physical edge case.

## Scope

Recommended scope is intentionally narrow:

- only inspect the immediate left neighbor, `gate - 1`
- do nothing for gate `0`
- only apply mitigation while scan-jog is active
- only apply mitigation while Happy Hare is idle and lanes passed the existing
  parked-or-empty preflight
- do not add a configuration option
- always restore the left neighbor if it was moved
- do not add global cross-gate UID matching

This keeps the behavior matched to the physical mounting geometry instead of
turning the NFC manager into a global spool reconciliation system.

## Existing Safety Boundaries

The current implementation already provides useful protection:

- only one scan-jog may run at a time via `NFCGate._active_scan_gate`
- scan-jog is blocked unless Happy Hare reports all lanes parked or empty
- each gate owns its own reader
- scan-jog suppresses dispatch until after the rewind path
- incomplete structured reads can be retried nearby

The missing piece is identity disambiguation after a UID is read but before the
scan is accepted as successful.

## Insertion Point

The implementation lives in:

`klippy/extras/nfc_gates/scan_jog.py`

The best insertion point is inside `step_event()`, immediately after:

```python
tag_found = gate._poll()
```

and before the existing `if tag_found:` success path.

At this point:

- the PN532 read has happened
- `tag_handler.resolve_spool()` has already resolved UID to spool where possible
- `GateState.process_read()` has updated `gate._state`
- scan-jog dispatch is still deferred in `gate._scan_found_event`
- no Happy Hare assignment has been sent yet

That makes it possible to reject the read, clear the deferred event, move the
left neighbor out of the RF field, and continue scanning gate `N`.

Implemented call shape:

```python
tag_found = gate._poll()

if tag_found and handle_left_neighbor_interference(gate, now):
    return gate.reactor.monotonic() + gate._scan_poll_interval
```

## Detection Strategy

### Spoolman-enabled path

If Spoolman is enabled, the first identity test should still be UID-to-UID, not
UID-to-spool. The raw UID is the direct physical identity returned by the PN532.
If gate `N` reads the same UID already known for gate `N - 1`, the tag belongs
to the left neighbor and there is no reason to resolve the read UID to a
Spoolman spool before acting.

Implemented order:

1. Confirm a UID was read.
2. Find the known UID for gate `N - 1` from the left NFC gate object.
3. If the left gate has a known UID and it matches the read UID, treat the read
   as interference immediately.
4. Discard the read, jog gate `N - 1` out of the reader field, and continue
   scan-jog for gate `N`.
5. If the left UID is unavailable or does not match, fall out to the default
   scan-jog behavior.

This avoids unnecessary Spoolman resolution for the common strong-match case.
The UID comparison is both safer and more efficient: it answers "did this
reader see the left physical tag?" directly.

Helper logic:

```python
def read_uid_from_scan_event(gate):
    event = getattr(gate, '_scan_found_event', None)
    if event is not None and len(event) >= 3:
        return event[2]
    return gate._state.current_uid
```

Then:

```python
left_gate = gate._gate - 1
if left_gate < 0:
    return False

read_uid = read_uid_from_scan_event(gate)
if read_uid is None:
    return False

left_uid = known_uid_for_gate(gate, left_gate)
if left_uid is not None and left_uid == read_uid:
    return True
```

The `known_uid_for_gate()` helper is cheap and conservative. Preferred source:

1. The left `NFCGate` instance cache, if it has `current_uid`.

Happy Hare may be used as a safety check that the left gate is still physically
loaded or available, but it should not be used to resolve the UID that gate `N`
just read. Likewise, the first implementation should not fall back from
`read_uid` to `spool_id` comparison. If the left NFC gate cache has no UID,
there is no positive interference proof.

If no left UID can be established, or if the known left UID does not match the
read UID, the mitigation should return `False` and let normal scan-jog continue.
The resolved `spool_id` is useful for the normal assignment flow, but it is not
the primary decision point for left-neighbor interference.

### Spoolman-disabled path

When Spoolman is disabled, the same UID-first rule still applies. Do not invent
a Happy Hare metadata convention just to smuggle the UID through display names.
The NFC layer already owns per-gate state, and `NFC_STATUS` already shows each
gate's cached UID from the `NFCGate` object.

Recommended order:

1. Confirm a UID was read by gate `N`.
2. Resolve the left NFC gate object for `N - 1`.
3. Read `left_gate._state.current_uid`.
4. If the left NFC gate has a cached UID and it matches the read UID, treat the
   read as interference immediately.
5. If the left NFC gate has no cached UID, or the UID does not match, return
   `False` and let normal scan-jog continue.

This keeps the no-Spoolman path a corner case with the same core rule:

```python
left_nfc = nfc_gate_for_gate_number(gate._gate - 1)
if left_nfc is None:
    return False

left_uid = left_nfc._state.current_uid
return left_uid is not None and left_uid == read_uid
```

Happy Hare can still be used as a sanity check that the left lane is physically
loaded or available, but HH does not need to carry the UID. The UID comparison
should come from the NFC gate cache, not from gate names or display metadata.

## Mitigation Strategy

If the current scan reads the left neighbor:

1. Log the decision, including current gate, left gate, UID, and spool ID when
   available.
2. Select the left gate.
3. Jog the left gate in the positive `MMU_TEST_MOVE` direction by a fixed
   distance. This is the "push filament out" direction for the parked left
   neighbor.
4. Wait for the move to complete.
5. Re-select the current scan gate.
6. Clear the false scan result.
7. Continue the scan-jog loop.

Up to three clearance moves may be attempted per scan. After each move, the
false scan result is cleared and the current lane reads again before any normal
current-lane jog is queued. If the same scan still reads the left neighbor
after the third clearance move, treat that as a larger mechanical/RF fault.
Log and console an `[ERROR]`, abort scan-jog for the current gate, restore the
left neighbor by the accumulated clearance distance, and rewind the current
gate through the normal scan exit path.

Fixed distance:

```python
LEFT_NEIGHBOR_CLEARANCE_MM = 75.0
```

No config option is recommended. A fixed value keeps the feature simple and
avoids adding a tuning knob for a rare hardware geometry problem.

Clearance GCode sequence:

```gcode
MMU_SELECT GATE=<left>
MMU_TEST_MOVE MOVE=75.00 QUIET=1
M400
MMU_SELECT GATE=<current>
```

After shifting the neighbor, clear the false read:

```python
gate._scan_found_event = None
gate._state.current_uid = None
gate._state.current_spool = None
gate._state.miss_count = 0
```

If a structured tag object is present, clear it as well:

```python
gate._state.current_tag = None
```

## Restore Strategy

If the left neighbor was moved, it must be restored from every scan exit path:

- successful scan
- no tag found
- max distance reached
- print-start abort
- scan poll exception path that exits scan-jog

State on `NFCGate`:

```python
gate._scan_left_neighbor_gate = -1
gate._scan_left_neighbor_shift_mm = 0.0
gate._scan_left_neighbor_shifted = False
gate._scan_left_neighbor_uid = None
gate._scan_left_neighbor_attempts = 0
```

Initialize these in `scan_jog.start()`.

Restore helper:

```python
def restore_left_neighbor(gate):
    if not getattr(gate, '_scan_left_neighbor_shifted', False):
        return
    left = gate._scan_left_neighbor_gate
    mm = gate._scan_left_neighbor_shift_mm
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_uid = None

    gcode = gate.printer.lookup_object('gcode')
    gcode.run_script(
        "MMU_SELECT GATE=%d\n"
        "MMU_TEST_MOVE MOVE=%.2f QUIET=1\n"
        "M400\n"
        "MMU_SELECT GATE=%d" % (left, -mm, gate._gate))
```

Call this from both `finish()` and `rewind_and_exit()`. The restore should run
after the current gate's rewind is queued, and before the scan state is fully
cleared. The final `MMU_SELECT GATE=<current>` leaves Happy Hare selected on the
gate whose scan just completed.

## State Reset and Scan Continuation

Handling interference should not count as a successful scan. It should behave
like "that UID was not ours; try again after moving the neighbor".

Recommended behavior after the left gate jog:

- keep `gate._scan_mode = True`
- keep `NFCGate._active_scan_gate` held by the current gate
- keep `gate._scan_mm_total` unchanged
- clear current NFC state and deferred event
- set `gate._scan_next_chunk_time` to a short future time so the field can
  settle before polling again

Example:

```python
gate._scan_next_chunk_time = (
    gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY)
```

Using the existing decode retry settle constant is acceptable. A separate
constant can be introduced if hardware testing shows the neighbor shift needs a
different dwell.

## Implementation Notes

This section describes the implemented first pass.

### Identity Rule

Use exactly one positive interference rule:

```text
gate N read UID X
left NFC gate cache has current_uid Y
X == Y  -> left-neighbor interference
X != Y  -> normal scan-jog behavior
Y empty -> normal scan-jog behavior
```

Do not resolve the read UID to a Spoolman spool ID for interference detection.
Do not compare the read spool ID to Happy Hare's left-gate spool ID. Do not
inspect Happy Hare display names or metadata for UID information.

### Data Sources

The current read UID comes from the current gate's scan state, in this order:

1. `gate._scan_found_event[2]`, if a deferred scan event exists
2. `gate._state.current_uid`

The left UID comes from the left NFC gate object only:

```python
left_nfc = nfc_gate_for_gate_number(gate._gate - 1)
left_uid = left_nfc._state.current_uid if left_nfc is not None else None
```

Happy Hare may be consulted only to reject stale left-cache data:

```python
left_hh = hh_status.read(gate.printer, gate._gate - 1)
if left_hh.present and not left_hh.available:
    return None
```

If Happy Hare is unavailable, the implementation may still use the left NFC
cache because scan-jog already passed its preflight before starting. The
important part is that HH is not used to translate the current read into an
identity.

### Helper Contracts

Add a lookup helper near the existing `_lane_instances` registry in
`nfc_manager.py`:

```python
def nfc_gate_for_gate_number(gate_number):
    for candidate in _lane_instances:
        if candidate._gate == gate_number:
            return candidate
    return None
```

Expose it to `scan_jog.py` through an `NFCGate` wrapper to avoid importing
`nfc_manager.py` from `scan_jog.py`:

```python
def _nfc_gate_for_gate_number(self, gate_number):
    return nfc_gate_for_gate_number(gate_number)
```

Then `scan_jog.py` can call:

```python
left_nfc = gate._nfc_gate_for_gate_number(gate._gate - 1)
```

### Scan Loop Placement

In `scan_jog.step_event()`, call the interference handler immediately after
`gate._poll()` and before decode retry or finish handling:

```python
tag_found = gate._poll()

if tag_found and handle_left_neighbor_interference(gate, now):
    return gate.reactor.monotonic() + gate._scan_poll_interval
```

This placement matters. `_poll()` has already captured the UID and deferred the
event, but scan-jog has not accepted the read, started decode retry, dispatched
to Happy Hare, or finished.

### Interference Handler

The handler should:

1. Read the current scan UID.
2. Return `False` for gate `0`.
3. Return `False` if the current UID is empty.
4. Read the left NFC gate object and its `current_uid`.
5. Return `False` if the left UID is empty or does not match.
6. If the left neighbor was already shifted during this scan and the same left
   gate/UID is still being read, log a fault, abort scan-jog, and return `True`.
7. Log the interference decision.
8. Move the left gate out of range. If the shift command fails, log the
   failure, clear no scan state, return `False`, and let normal scan-jog
   behavior continue.
9. Clear the current gate's false read state.
10. Keep scan-jog active and return `True`.

The handler should not inspect or compare spool IDs.

### Left-Gate Movement

Use a single fixed displacement:

```python
LEFT_NEIGHBOR_CLEARANCE_MM = 75.0
```

The shift GCode should explicitly select the left gate, move it in the positive
`MMU_TEST_MOVE` direction to push filament out, wait, then select the current
gate:

```gcode
MMU_SELECT GATE=<left>
MMU_TEST_MOVE MOVE=75.00 QUIET=1
M400
MMU_SELECT GATE=<current>
```

Set restore state only after the shift command is successfully queued:

```python
gate._scan_left_neighbor_gate = left
gate._scan_left_neighbor_shift_mm = 75.0
gate._scan_left_neighbor_shifted = True
gate._scan_left_neighbor_uid = uid
```

If the shift command raises, log a warning, clear no state, and return `False`
so normal scan-jog behavior continues.

Retry clearance moves conservatively. If `gate._scan_left_neighbor_shifted` is
already `True` for the same left gate and UID, the implementation may call
`MMU_TEST_MOVE MOVE=75.00` again until three total clearance moves have been
queued. Each retry clears the false scan result and reads again before normal
current-lane scan-jog movement resumes.

If the same UID is still visible after the third clearance move and follow-up
read, report an error and exit scan-jog:

```text
[ERROR] NFC[<current>]: left lane gate <left> is interfering with the current lane read after 3 clearance moves (<mm>mm); check reader position, tag placement, or lane spacing
```

That exit should use the normal rewind/restore path so the neighbor is returned
with `MMU_TEST_MOVE MOVE=-<total clearance>` and the current gate is rewound.

### False Read Cleanup

After a confirmed left-neighbor UID match and successful left-gate shift, clear:

```python
gate._scan_found_event = None
gate._state.current_uid = None
gate._state.current_spool = None
gate._state.current_tag = None
gate._state.miss_count = 0
```

Also clear decode-retry state if it had been initialized from the false UID:

```python
gate._scan_decode_retry_attempts = 0
gate._scan_decode_retry_uid = None
gate._scan_decode_retry_offset = 0.0
```

Do not change `gate._scan_mm_total`; the current gate has not moved.

### Restore Ordering

Call `restore_left_neighbor(gate)` from both scan exit paths:

- `finish(gate)`
- `rewind_and_exit(gate)`

The restore should run after the current gate's rewind is queued and before the
scan state is fully discarded. This preserves the current gate's normal rewind
behavior and still guarantees the neighbor is put back.

Restore GCode:

```gcode
MMU_SELECT GATE=<left>
MMU_TEST_MOVE MOVE=-75.00 QUIET=1
M400
MMU_SELECT GATE=<current>
```

Clear restore state before running the restore GCode so a restore failure cannot
cause repeated negative moves on later cleanup attempts. Log failures.

## Module Changes

### `scan_jog.py`

Owns:

- interference detection wrapper
- left-neighbor jog GCode
- restore-on-exit state
- clearing false scan results

Recommended additions:

- `LEFT_NEIGHBOR_CLEARANCE_MM`
- `handle_left_neighbor_interference(gate, now)`
- `is_left_neighbor_interference(gate, uid, spool)`
- `shift_left_neighbor(gate)`
- `restore_left_neighbor(gate)`
- `clear_false_scan_result(gate)`

### `nfc_manager.py`

Keep changes minimal.

Add scan state fields in `NFCGate.__init__` so tests and status are explicit:

```python
self._scan_left_neighbor_gate = -1
self._scan_left_neighbor_shift_mm = 0.0
self._scan_left_neighbor_shifted = False
self._scan_left_neighbor_uid = None
```

Expose or reuse a small lookup helper for the existing NFC gate registry:

```python
def nfc_gate_for_gate_number(gate_number):
    for candidate in _lane_instances:
        if candidate._gate == gate_number:
            return candidate
    return None
```

This helper lets scan-jog read the left gate's cached `current_uid` directly
from the NFC gate object. That is the right source for UID identity; Happy Hare
does not need a UID field for this mitigation.

No Happy Hare movement commands should be added here. Movement remains in
`scan_jog.py`.

### `hh_status.py`

No display-name or metadata changes are required for this design.

`hh_status.py` may still be used to verify the left lane is physically loaded or
available before trusting a cached UID, but it should not be extended to carry
UIDs through Happy Hare gate metadata.

### `klipper_interface.py`

No changes are required.

Do not append UID suffixes to metadata names for this mitigation. Display names
should remain display names; UID identity stays in the NFC gate cache.

## Pseudocode

```python
def handle_left_neighbor_interference(gate, now):
    if not gate._scan_mode:
        return False
    if gate._gate <= 0:
        return False

    event = getattr(gate, '_scan_found_event', None)
    uid = None
    spool = gate._state.current_spool
    if event is not None and len(event) >= 4:
        uid = event[2]
        spool = event[3]
    if uid is None:
        uid = gate._state.current_uid
    if uid is None:
        return False

    if not is_left_neighbor_interference(gate, uid, spool):
        return False

    left_gate = gate._gate - 1
    if left_neighbor_already_shifted(gate, left_gate, uid) and attempts >= 3:
        msg = (
            "[ERROR] NFC[%d]: left lane gate %d is interfering with the "
            "current lane read after %d clearance moves"
            % (gate._gate, left_gate, attempts))
        logger.error("%s", msg)
        gate._console(msg)
        clear_false_scan_result(gate)
        gate._rewind_and_exit_scan()
        return True

    logger.warning(
        "nfc_gate: [%s] gate %d scan mode - uid=%s spool=%s belongs "
        "to left neighbor gate %d; moving neighbor out of reader field",
        gate._name, gate._gate, uid, spool, left_gate)

    if not shift_left_neighbor(gate):
        return False
    clear_false_scan_result(gate)
    gate._scan_next_chunk_time = (
        gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY)
    return True


def left_neighbor_already_shifted(gate, left_gate, uid):
    return (
        gate._scan_left_neighbor_shifted
        and gate._scan_left_neighbor_gate == left_gate
        and gate._scan_left_neighbor_uid == uid)


def is_left_neighbor_interference(gate, uid, spool):
    if gate._gate <= 0:
        return False
    if uid is None:
        return False

    left_gate = gate._gate - 1
    left_uid = known_uid_for_gate(gate, left_gate)
    if left_uid is None:
        return False

    return left_uid == uid


def known_uid_for_gate(gate, target_gate):
    left_nfc = gate._nfc_gate_for_gate_number(target_gate)
    if left_nfc is None or not left_nfc._state.current_uid:
        return None

    # Optional stale-cache guard. HH is not used to resolve identity; it can
    # only veto a cached left UID when it clearly says the left gate is empty.
    left = hh_status.read(gate.printer, target_gate)
    if left.present and not left.available:
        return None

    return left_nfc._state.current_uid
```

## Tests

Add tests to:

`tests/test_scan_jog_mode.py`

Recommended cases:

- gate `0` never checks a left neighbor
- Spoolman-enabled read matching the known left UID shifts left and does not finish
  scan
- Spoolman-enabled read not matching the known left UID finishes normally
- Spoolman-enabled read with no known left UID falls through to normal scan-jog
  behavior
- left gate assigned but not available does not trigger mitigation
- after interference, false `_scan_found_event` and `GateState` values are
  cleared
- `finish()` restores the left gate if shifted
- `rewind_and_exit()` restores the left gate if shifted
- repeated left-neighbor hits after the first clearance do not issue another
  `MOVE=75.00`; they warn, abort scan-jog, restore the neighbor, and rewind the
  current gate
- Spoolman-disabled read matching the left NFC gate object's cached UID shifts
  left and does not finish scan
- Spoolman-disabled read with no cached left UID falls through to normal
  scan-jog behavior

The tests should assert emitted GCode order, especially:

```text
MMU_SELECT GATE=<left>
MMU_TEST_MOVE MOVE=75.00 QUIET=1
M400
MMU_SELECT GATE=<current>
```

and restore:

```text
MMU_SELECT GATE=<left>
MMU_TEST_MOVE MOVE=-75.00 QUIET=1
M400
MMU_SELECT GATE=<current>
```

## Risks and Notes

- The no-Spoolman path depends on the left NFC gate object having a current UID
  in its cache. If it does not, the mitigation should degrade gracefully by
  returning `False` and letting normal scan-jog continue.
- Moving the left neighbor assumes all lanes are parked or empty. The existing
  scan-jog preflight is therefore part of this feature's safety case.
- If the reader still sees the same left-neighbor UID after a 75mm clearance
  move, the problem is likely mechanical placement, reader field overlap, tag
  placement, or lane spacing. The implementation should surface that condition
  clearly and stop the scan instead of pushing the neighbor farther out.
- The mitigation should be conservative about what counts as interference.
  False positives move a neighboring parked spool unnecessarily. False
  negatives preserve current behavior.
- The feature should log clearly but avoid console spam. A single warning per
  interference event is enough.
