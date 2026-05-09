## Summary

This change tightens scan-jog state handling so NFC and Happy Hare stay in sync
across scan start, successful scan completion, and no-tag abort paths.

### Gate state reset

Added a dedicated `GateState.reset()` helper to clear cached NFC state in one
place. This resets the UID, spool, current tag object, and miss counter together.

```python
def reset(self):
    self._current_uid = None
    self._current_spool = None
    self.current_tag = None
    self.miss_count = 0
```

Scan-jog now saves the previous state for reference, then resets the active gate
state before scan polling begins. This forces the first tag read during scan mode
to re-resolve through Spoolman and emit the expected changed event, even if the
same tag was previously cached.

```python
gate._scan_previous_uid = gate._state.current_uid
gate._scan_previous_spool = gate._state.current_spool
gate._scan_previous_active_gate = get_active_gate(gate)
gate._state.reset()
```

### No-tag scan cleanup

Changed the no-tag scan abort path so it leaves both NFC and HH as a clean slate
after rewind. Instead of restoring the previous NFC spool, the abort path now
clears the HH gate cache and resets NFC state.

```python
try:
    gate._run_rewind()
finally:
    restore_active_gate(gate)

clear_hh_gate_cache(gate)
gate._state.reset()
gate._hh_load_paused = False
```

This avoids a mismatch where HH has been cleared to unknown/empty state while NFC
restores the old spool, which could cause immediate re-clear or re-dispatch
behavior on the next poll.

### HH hook safety

Updated `HH_SYNC=0` behavior so hook-triggered scans skip both HH gate cache
clearing and Spoolman sync. Both operations use `gcode.run_script()`, so skipping
both avoids GCode lock deadlocks when scan-jog is triggered from inside a Happy
Hare post-preload hook.

### Poll optimization

Added a fast path in `_poll()` to skip Spoolman lookups when the currently read
UID already matches the cached NFC UID.

```python
uid_hex = self._read_current_tag()
if uid_hex is not None and uid_hex == self._state.current_uid:
    self._state.miss_count = 0
    return True

spool_id = self._resolve_spool(uid_hex)
```

This keeps repeated reads of the same tag cheap during normal polling and
scan-jog dwell periods, while still forcing a Spoolman lookup after
`GateState.reset()` clears the cached UID.

### Status and docs

- Cleaned up scan-jog status formatting so HH state is not duplicated when an
  NFC/HH sync note is shown.
- Expanded scan-jog design docs to describe reset behavior, no-tag cleanup, HH
  sync skipping, poll short-circuiting, and related debug messages.
- Added tests covering `GateState.reset()`, scan start reset behavior, no-tag
  rewind cleanup, HH cache clearing, and status output formatting.

## Testing

```shell
python3 -m pytest tests/test_scan_jog_mode.py
```

Result:

```text
74 passed
```
