# Scan-Jog WAIT=0 Motion Mode Design Note

This is a memory/design note for a future scan-jog mode that experiments with
Happy Hare `MMU_TEST_MOVE WAIT=0`. It is not implemented in current `main`.

## Goal

Current scan-jog uses stopped-position reads:

1. Queue `MMU_TEST_MOVE` with default `WAIT=1`.
2. Happy Hare blocks until the move completes.
3. NFC reads at the stopped spool position.
4. Repeat until tag found or max scan distance reached.

The proposed mode keeps the Klipper/Happy Hare integration inside Klipper, but
uses small non-blocking moves so NFC can poll between queued motion chunks:

1. Queue a small `MMU_TEST_MOVE ... WAIT=0`.
2. Poll the reader while that short move is completing or just after it.
3. If a tag is found, stop queuing more moves.
4. Let the current small move finish, then rewind/park through the existing
   scan-jog finish path.

This avoids a full external NFC daemon and keeps the blast radius small.

## Proposed Config

```ini
# Existing/default behavior.
scan_motion_mode: stopped

# Experimental behavior.
# scan_motion_mode: continuous

# Small non-blocking chunks used only in continuous mode.
scan_continuous_step_mm: 5.0

# Poll cadence while continuous mode is active.
scan_continuous_poll_interval: 0.05

# Optional explicit speed for continuous scan moves.
# If unset, Happy Hare's normal MMU_TEST_MOVE gear speed selection applies.
# scan_continuous_speed: 80.0
```

Keep `stopped` as the default until the continuous model is proven reliable.

## State Additions

Add these fields to `NFCGate` / scan-jog state:

```python
gate._scan_motion_mode = 'stopped'
gate._scan_continuous_step_mm = 5.0
gate._scan_continuous_poll_interval = 0.05
gate._scan_continuous_speed = None

gate._scan_continuous_move_inflight = False
gate._scan_continuous_move_complete_time = 0.0
gate._scan_continuous_last_move_mm = 0.0
```

`_scan_mm_total` remains the authoritative planned scan distance for rewind.

## Existing Stopped Mode

Leave the current `scan_jog.step_event()` path intact:

```python
if gate._scan_motion_mode == 'stopped':
    return stopped_step_event(gate, eventtime)
```

This preserves current behavior and makes continuous mode opt-in.

## Continuous Mode Pseudocode

```python
def step_event(gate, eventtime):
    if gate._scan_motion_mode != 'continuous':
        return stopped_step_event(gate, eventtime)
    return continuous_step_event(gate, eventtime)
```

```python
def continuous_step_event(gate, eventtime):
    if not gate._scan_mode:
        return gate.reactor.NEVER

    if is_printing(gate):
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    run_pending_hh_prep(gate)

    now = gate.reactor.monotonic()

    # First, poll NFC. This lets a tag hit stop future queued motion.
    try:
        tag_found = gate._poll()
    except Exception:
        logger.exception("[%s]: continuous scan poll error", gate._name)
        gate._console("[ERROR] NFC[%s]: scan poll failed" % gate._name)
        tag_found = False

    if tag_found:
        if handle_left_neighbor_interference(gate):
            if not gate._scan_mode:
                return gate.reactor.NEVER
            return now + gate._scan_continuous_poll_interval

        if current_tag_decode_incomplete(gate):
            # Prefer the existing decode retry machinery. The simplest first
            # implementation can temporarily fall back to stopped retry moves.
            if retry_incomplete_decode(gate, now):
                return now + gate._scan_continuous_poll_interval

        # Do not queue more motion. Let the current small move complete, then
        # use the existing finish path.
        if gate._scan_continuous_move_inflight and now < gate._scan_continuous_move_complete_time:
            return gate._scan_continuous_move_complete_time

        gate._finish_scan()
        return gate.reactor.NEVER

    if gate._scan_mm_total >= gate._scan_max_mm:
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    # If a previous non-blocking move is still expected to be in progress,
    # poll again soon instead of stacking another move.
    if gate._scan_continuous_move_inflight:
        if now < gate._scan_continuous_move_complete_time:
            return min(
                gate._scan_continuous_move_complete_time,
                now + gate._scan_continuous_poll_interval)
        gate._scan_continuous_move_inflight = False

    # Queue one small non-blocking movement chunk.
    remaining = gate._scan_max_mm - gate._scan_mm_total
    move = min(gate._scan_continuous_step_mm, remaining)
    if move <= 0.0:
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    queue_continuous_jog(gate, move)
    gate._scan_mm_total += move
    gate._scan_continuous_last_move_mm = move
    gate._scan_continuous_move_inflight = True
    gate._scan_continuous_move_complete_time = (
        now + chunk_interval(gate, move))

    return now + gate._scan_continuous_poll_interval
```

## Jog Command Pseudocode

```python
def queue_continuous_jog(gate, move):
    speed = getattr(gate, '_scan_continuous_speed', None)
    cmd = "MMU_TEST_MOVE MOTOR=gear MOVE=%.2f WAIT=0 QUIET=1" % move
    if speed is not None and speed > 0.0:
        cmd = "MMU_TEST_MOVE MOTOR=gear MOVE=%.2f SPEED=%.1f WAIT=0 QUIET=1" % (
            move, speed)
    gate.printer.lookup_object('gcode').run_script(cmd)
```

If Happy Hare ignores `QUIET=1` on `MMU_TEST_MOVE`, omit it.

## Safety Notes

- Do not queue another `WAIT=0` move until the estimated completion time for
  the prior small move has passed.
- Keep chunks small enough that tag-found overshoot is acceptable.
- Do not attempt true emergency mid-move stop in the first implementation.
- Let the current small move finish before `_finish_scan()` or rewind.
- Preserve the existing stopped-position decode retry path initially.
- Keep `scan_motion_mode: stopped` as the default.

## Initial Test Values

```ini
scan_motion_mode: continuous
scan_continuous_step_mm: 5.0
scan_continuous_poll_interval: 0.05
scan_continuous_speed: 80.0
```

Expected behavior:

- Scan should feel much smoother than stopped substeps.
- Worst-case tag overshoot should be roughly one small step plus scheduling
  latency.
- Rewind distance should still be based on `_scan_mm_total`, so the existing
  parking handoff remains usable.

