# Continuous Scan Changelog

Branch: `CW-Development`

## Added

- Added opt-in `scan_motion_mode: continuous` for scan-jog.
- Added continuous scan config:
  - `scan_continuous_step_mm: 50.0`
  - `scan_continuous_speed: 150.0`
  - `scan_continuous_accel: 2000.0`
  - `scan_continuous_poll_interval: 0.05`
- Added `MMU_TEST_MOVE WAIT=0` forward jog support for continuous scan.
- Added trapezoid timing estimation for continuous scan chunks so the next
  read/check is scheduled after the estimated move completion plus the
  configured check gap.
- Added adaptive continuous timing: NFC measures how long `MMU_TEST_MOVE WAIT=0`
  actually takes to return and subtracts that elapsed time from the estimated
  move duration before scheduling the next read. This avoids double-waiting on
  systems where Happy Hare returns after most or all of the move has completed.
- Reduced repeated continuous-mode search LED calls by removing the top-of-loop
  LED reapply while keeping the post-move reassertion that restores scan LED
  ownership after Happy Hare motion updates.
- Fixed scan-jog direct console messages so they respect `console_output` and
  `console_log_level`.

## Behavior

- Default scan behavior remains `stopped`.
- Continuous scan only changes the forward search jog pacing.
- Happy Hare's default `MMU_TEST_MOVE` motor remains `gear`; continuous scan
  does not need to pass `MOTOR=gear`.
- Tag-found handling still uses the existing scan completion path:
  - stop queueing forward moves
  - preserve the 0.1 second read-light hold
  - rewind using the existing rewind path
  - dispatch the cached tag/spool action after rewind
- Decode retry moves remain on the existing stopped/blocking retry path.

## Motion Profile

With the default continuous values:

- 50 mm move
- 150 mm/s speed
- 2000 mm/s^2 acceleration
- 0.05 s post-move tag-check gap

Estimated motion:

- Accel time: about 0.075 s
- Cruise distance: about 38.75 mm
- Move duration: about 0.408 s
- Effective scan advance including the 0.05 s check gap: about 109 mm/s
- If Happy Hare blocks inside `MMU_TEST_MOVE WAIT=0`, NFC subtracts that command
  time from the estimated move duration before applying the 0.05 s check gap.

## Files Changed

- `klippy/extras/nfc_gates/scan_jog.py`
- `klippy/extras/nfc_gates/nfc_manager.py`
- `config/nfc_reader.cfg`
- `docs/shared/configuration.md`
- `docs/shared/scan-jog-wait0-design-note.md`
