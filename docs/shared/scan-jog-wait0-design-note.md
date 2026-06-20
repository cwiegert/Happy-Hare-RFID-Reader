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
scan_continuous_step_mm: 50.0

# Delay between one estimated move completion and queueing the next move.
# During this gap, poll the reader once for a tag.
scan_continuous_poll_interval: 0.05

# Explicit continuous scan move settings.
scan_continuous_speed: 150.0
scan_continuous_accel: 2000.0
```

Keep `stopped` as the default until the continuous model is proven reliable.

`MMU_TEST_MOVE` defaults to the `gear` motor in Happy Hare, so continuous mode
does not need to pass `MOTOR=gear` unless we want the generated command to be
extra explicit. The important behavior change is `WAIT=0`.

## Motion Model

Initial continuous values:

- Move length: `50.0` mm
- Move speed: `150.0` mm/s
- Move accel: `2000.0` mm/s^2
- Inter-move delay / tag-check window: `0.05` s

For a 50 mm move at 150 mm/s with 2000 mm/s^2 acceleration:

- Accel time to 150 mm/s: `150 / 2000 = 0.075` s
- Accel distance: `150^2 / (2 * 2000) = 5.625` mm
- Decel distance: `5.625` mm
- Cruise distance: `50 - 5.625 - 5.625 = 38.75` mm
- Cruise time: `38.75 / 150 = 0.258` s
- Total motion time per 50 mm chunk: about `0.408` s
- Average speed during the moving part: about `122` mm/s
- Average scan advance including the 0.05 s gap: about `109` mm/s

So the spool advances in 50 mm non-blocking chunks, with a short stationary-ish
gap between chunks. The first implementation should check for a tag during that
0.05 s gap after each chunk completes, then either queue the next chunk or stop
the jog flow and let the existing tag handling finish the scan.

The existing `finish()` path intentionally pauses for about 1.0 second after a
tag is found so the `scan_tag_read_effect` / read-light effect is visible before
the rewind effect starts. Continuous mode should keep that behavior. The motion
change is only the forward jog pacing.

## State Additions

Add these fields to `NFCGate` / scan-jog state:

```python
gate._scan_motion_mode = 'stopped'
gate._scan_continuous_step_mm = 50.0
gate._scan_continuous_poll_interval = 0.05
gate._scan_continuous_speed = 150.0
gate._scan_continuous_accel = 2000.0

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

    # First, poll NFC. This happens after the previous small move has completed
    # or before the first move is queued. A tag hit stops future queued motion.
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

        # Do not queue more motion. Let the current small move complete if the
        # estimate says it is still active, then use the existing finish path.
        if gate._scan_continuous_move_inflight and now < gate._scan_continuous_move_complete_time:
            return gate._scan_continuous_move_complete_time

        # Use the same tag read actions, read-light hold, rewind, and scan
        # completion logic as stopped mode. The only behavior being changed is
        # how the forward jog motion is paced.
        gate._finish_scan()
        return gate.reactor.NEVER

    if gate._scan_mm_total >= gate._scan_max_mm:
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    # If a previous non-blocking move is still expected to be in progress,
    # poll again soon instead of stacking another move.
    if gate._scan_continuous_move_inflight:
        if now < gate._scan_continuous_move_complete_time:
            return gate._scan_continuous_move_complete_time
        gate._scan_continuous_move_inflight = False

        # After each completed chunk, wait 0.05 s, poll once, and only then
        # queue the next chunk if no tag was found.
        return now + gate._scan_continuous_poll_interval

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
    speed = getattr(gate, '_scan_continuous_speed', 150.0)
    accel = getattr(gate, '_scan_continuous_accel', 2000.0)
    cmd = (
        "MMU_TEST_MOVE MOVE=%.2f SPEED=%.1f ACCEL=%.1f WAIT=0 QUIET=1"
        % (move, speed, accel)
    )
    gate.printer.lookup_object('gcode').run_script(cmd)
```

If Happy Hare ignores `QUIET=1` on `MMU_TEST_MOVE`, omit it.

## Safety Notes

- Do not queue another `WAIT=0` move until the estimated completion time for
  the prior small move has passed.
- Use the existing tag read actions and existing scan completion logic after a
  tag is found. Continuous mode only changes how jog moves are queued.
- Preserve the existing 1.0 s read-light hold before rewind.
- With 50 mm chunks, worst-case tag overshoot is roughly one chunk plus the
  0.05 s check gap and scheduler latency.
- Do not attempt true emergency mid-move stop in the first implementation.
- Let the current small move finish before `_finish_scan()` or rewind.
- Preserve the existing stopped-position decode retry path initially.
- Keep `scan_motion_mode: stopped` as the default.

## Initial Test Values

```ini
scan_motion_mode: continuous
scan_continuous_step_mm: 50.0
scan_continuous_poll_interval: 0.05
scan_continuous_speed: 150.0
scan_continuous_accel: 2000.0
```

Expected behavior:

- Scan should move in 50 mm non-blocking gear chunks.
- Each chunk should take about 0.408 s, then pause/check for about 0.05 s before
  the next chunk is queued.
- Effective scan advance should be roughly 109 mm/s with these defaults.
- If a tag is read during the check window, no more motion is queued and the
  existing tag read actions, 1.0 s read-light hold, rewind, and completion logic
  run.
- Rewind distance should still be based on `_scan_mm_total`, so the existing
  parking handoff remains usable.
