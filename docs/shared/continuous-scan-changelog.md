# Continuous Scan Changelog

Branch: `CW-Development`

## Added

- Added opt-in `scan_motion_mode: continuous` for scan-jog.
- Added continuous scan config:
  - `scan_continuous_step_mm: 50.0`
  - `scan_continuous_speed: 150.0`
  - `scan_continuous_accel: 2000.0`
  - `scan_continuous_poll_interval: 0.05`
- Added direct Happy Hare MMU-toolhead forward jog support for continuous scan.
- Added trapezoid timing estimation for continuous scan chunks so NFC can poll
  while a chunk is estimated to be moving.
- Added in-flight NFC polling every `scan_continuous_poll_interval` during the
  continuous search chunk. If a tag is found during motion, NFC lets the current
  chunk finish before running the existing finish/rewind path.
- Reduced repeated continuous-mode search LED calls by removing the top-of-loop
  LED reapply while keeping the post-move reassertion that restores scan LED
  ownership after Happy Hare motion updates.
- Fixed scan-jog direct console messages so they respect `console_output` and
  `console_log_level`.

## Behavior

- Default scan behavior remains `stopped`.
- Continuous scan only changes the forward search jog pacing.
- Continuous forward search moves bypass the public `MMU_TEST_MOVE` G-code
  wrapper and use Happy Hare's MMU toolhead directly. If that direct path is not
  available, NFC falls back to `MMU_TEST_MOVE WAIT=0`.
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
- 0.05 s in-flight NFC poll cadence

Estimated motion:

- Accel time: about 0.075 s
- Cruise distance: about 38.75 mm
- Move duration: about 0.408 s
- Effective scan advance before NFC read time is included: about 122 mm/s
- If a tag is found during the in-flight polling window, NFC lets the current
  chunk finish before running the existing finish/rewind path.

## Files Changed

- `klippy/extras/nfc_gates/scan_jog.py`
- `klippy/extras/nfc_gates/nfc_manager.py`
- `config/nfc_reader.cfg`
- `docs/shared/configuration.md`
- `docs/shared/scan-jog-wait0-design-note.md`
