# CW-Development PR Summary

## 🚀 What This Branch Builds

This branch turns scan-jog from a slow stop-and-read workflow into a faster continuous scanning system, while preserving the existing Happy Hare + Spoolman tag resolution behavior.

The big idea: NFC can now move the spool forward in continuous chunks, poll the reader while motion is estimated to be in progress, then use the same proven tag-found, read-light, rewind, and dispatch flow once a tag is detected.

## ✨ Highlights

- 🌀 Added **continuous scan-jog mode** and made it the default.
- ⚙️ Added a **Direct Move** path that queues forward scan motion through Happy Hare's MMU toolhead instead of relying on the `MMU_TEST_MOVE` G-code wrapper.
- 🧭 Kept `MMU_TEST_MOVE WAIT=0` as a compatibility fallback when the direct Happy Hare motion path is not available.
- 🧠 Split continuous scan reads into a fast UID probe during motion and full Spoolman/rich-tag resolution after the current chunk finishes.
- 🏷️ Added continuous overshoot recovery for rich tag parsing and Spoolman auto-create workflows.
- 🔎 Preserved the existing scan finish behavior: tag found, read-light flash, rewind, and cached tag/spool dispatch.
- 💡 Reduced repeated LED calls during continuous scan while keeping the searching/read visual feedback.
- 🧼 Cleaned up console output, help text, macro naming, and scan-jog logging so the messages match the actual behavior.
- 📚 Updated user documentation for configuration, command behavior, internal flow, and message definitions.

## 🌀 Continuous Scan-Jog

`scan_motion_mode: continuous` is now the default scan-jog behavior.

Continuous mode changes only the forward search jog. Everything after a tag is found still follows the existing scan-jog path:

1. NFC polls for the tag.
2. The tag/spool result is cached.
3. The read LED effect plays briefly.
4. NFC rewinds toward the parked position.
5. The existing Happy Hare / Spoolman completion macros run.

The stopped scan model is still available with:

```ini
scan_motion_mode: stopped
```

That mode keeps the older blocking `MMU_TEST_MOVE` substep behavior and reads at stopped spool positions.

## ⚙️ New Configuration

Continuous scan adds these config values:

```ini
scan_motion_mode: continuous
scan_continuous_step_mm: 75.0
scan_continuous_speed: 250.0
scan_continuous_accel: 2000.0
scan_continuous_poll_interval: 0.03
#scan_continuous_overshoot_backup_mm: 37.5
scan_decode_retry_mm: 5.0
scan_decode_retry_rounds: 5
```

What they control:

| Setting | Purpose |
|---|---|
| `scan_motion_mode` | Chooses `continuous` or `stopped` scan-jog behavior. |
| `scan_continuous_step_mm` | Forward chunk size during continuous scan. This is also the expected maximum overrun after a tag is detected. |
| `scan_continuous_speed` | Gear move speed for the continuous scan chunk. |
| `scan_continuous_accel` | Gear move acceleration for the continuous scan chunk. |
| `scan_continuous_poll_interval` | How often NFC polls while the current chunk is estimated to be moving. |
| `scan_continuous_overshoot_backup_mm` | One-time backtrack before rich tag parsing/retries when a continuous UID hit does not resolve through Spoolman. Defaults to 50% of `scan_continuous_step_mm`. |
| `scan_decode_retry_mm` | Rich-tag retry spacing after the overshoot backup. Default changed to `5.0mm`. |
| `scan_decode_retry_rounds` | Number of left/right rich-tag retry rounds. |

## 🧠 Direct Move Path

Continuous scan first tries to use Happy Hare's Python motion layer directly:

- Selects the active gate.
- Syncs the MMU toolhead to `GEAR_ONLY`.
- Applies Happy Hare gear current restoration when available.
- Applies per-gate speed override when Happy Hare exposes it.
- Moves the gear rail through `mmu_toolhead.move(...)`.
- Flushes step generation after queueing motion.

User-facing logs call this path:

```text
Direct Move
```

If the installed Happy Hare version does not expose the needed internals, the code falls back to:

```text
MMU_TEST_MOVE
```

The fallback command uses:

```gcode
MMU_TEST_MOVE MOVE=<mm> SPEED=<speed> ACCEL=<accel> WAIT=0 QUIET=1
```

## 🧪 Scan Behavior

Continuous scan runs as a reactor-timer loop:

```text
start scan-jog
  └─ prepare Happy Hare / Spoolman state
  └─ queue forward Direct Move chunk
  └─ UID-only probe every scan_continuous_poll_interval while chunk is estimated in flight
      └─ UID found during chunk? stop queueing forward chunks
      └─ wait for current chunk to finish
      └─ resolve UID through Spoolman
          └─ known UID? finish scan using existing logic
          └─ unknown UID? optionally back up and run rich tag parse/retry
  └─ no tag? queue next chunk
  └─ scan limit reached? rewind and exit
```

Continuous motion uses the fast UID probe only while the spool is moving. Spoolman lookup, rich tag parsing, and auto-create work happen after the current chunk finishes, so the moving scan loop does not perform heavier HTTP or rich-read work while motion timing is active.

## 🏷️ Rich Tag Read and Overshoot Recovery

Continuous scan can overshoot the reader because the current chunk is allowed to finish after the UID is detected. This branch adds a specific recovery path for rich tag workflows:

```text
UID detected during continuous motion
  └─ finish current chunk
  └─ check UID against Spoolman
      └─ UID exists? cache spool result and finish normally
      └─ UID unknown?
          └─ back up scan_continuous_overshoot_backup_mm
          └─ run full rich tag read
          └─ if incomplete, probe left/right using scan_decode_retry_mm
          └─ if metadata is valid, auto-create or resolve spool
```

This matters for workflows where the UID is not already registered in Spoolman and the tag payload is needed to create or identify the spool.

The backup move updates the internal scan distance before rewind. For example:

```text
continuous chunk moved: 75.0mm
overshoot backup:      -37.5mm
rewind basis:           37.5mm
```

That keeps the final rewind from overshooting by the amount already backed up.

After the backup, the decode retry sweep is recentered around the backed-up position. With the new default:

```ini
scan_decode_retry_mm: 5.0
scan_decode_retry_rounds: 5
```

retry offsets are:

```text
+5, -5, +10, -10, +15, -15, +20, -20, +25, -25mm
```

## 💡 LED Behavior

This branch keeps the visual scan feedback but avoids repeatedly restarting the scan LED effect on every poll tick.

Changes include:

- The searching effect is applied after move submission.
- A delayed LED reassert remains to recover from Happy Hare LED updates.
- The read-light hold was shortened from `1.0s` to `0.1s` before rewind.
- The read-light delay is now named in code as `TAG_READ_HOLD_DELAY`.

## 🧼 Console and Logging Cleanup

Scan-jog messages now better reflect what the system is actually doing.

Notable cleanup:

- Scan start messages now distinguish:
  - `continuous scan-jog started`
  - `stopped scan-jog started`
- Detailed scan settings are no longer stuffed into the first console message.
- Detailed settings are logged only at higher debug verbosity.
- Continuous move logs identify the actual motion source:
  - `Direct Move`
  - `MMU_TEST_MOVE`
- Scan-jog console output now respects:
  - `console_output`
  - `console_log_level`
- User-visible `HH` wording was expanded to `Happy Hare` for clarity.
- Macro output now uses reader labels like `NFC[lane0]` instead of hardcoded gate text where possible.

## 🛠 Command and Help Fixes

This branch also improves NFC command discoverability:

- Added `NFC GATE=<#> JOG_SCAN=1` to `NFC_HELP`.
- Corrected per-gate help text to use `HELP=1`.
- Fixed `NFC GATE=<#> HELP=1` so it no longer gets intercepted by low-level PN532 debug handling.
- Cleaned up help text wording from `HH` to `Happy Hare`.

## 🧩 Macro Interface Improvements

The NFC event macros now receive the reader name:

```gcode
READER=<lane-name>
```

Updated macro calls include:

- `_NFC_SPOOL_CHANGED`
- `_NFC_SPOOL_REMOVED`
- `_NFC_TAG_NO_SPOOL`

This makes console output line up with the configured reader/lane name instead of only showing numeric gate IDs.

Example:

```text
NFC[lane0]: spool 52 detected (UID 04BBD592D32A81). Sending to Happy Hare.
```

## 📚 Documentation Added / Updated

Documentation was updated across:

- `CHANGELOG.md`
- `Readme.md`
- `docs/shared/configuration.md`
- `docs/shared/how-it-works.md`
- `docs/shared/klipper-functions.md`
- `docs/shared/message_definition.md`

The docs now describe:

- Continuous scan mode.
- Stopped scan mode.
- Continuous scan config options.
- Direct Move vs `MMU_TEST_MOVE` logging.
- Updated command examples.
- Updated expected console/log messages.

## 🎨 Installer Prompt Polish

The installer prompt styling was adjusted so the entire prompt is not bolded. This keeps interactive installer screens easier to scan, with only the intended/default selection emphasized.

## ✅ Compatibility Notes

- `scan_motion_mode: stopped` remains supported for installations where continuous movement misses tags due to reader alignment, tag placement, or acceleration/speed tuning.
- `MMU_TEST_MOVE WAIT=0` remains available as the fallback path when the Direct Move path cannot be used.
- Existing tag-found actions, Spoolman lookup behavior, Happy Hare dispatch, rich-tag retry handling, and rewind logic are preserved.

## 🧾 Files Touched

Core behavior:

- `klippy/extras/nfc_gates/scan_jog.py`
- `klippy/extras/nfc_gates/nfc_manager.py`
- `klippy/extras/nfc_gates/klipper_interface.py`
- `klippy/extras/nfc_gates/hh_status.py`
- `klippy/extras/nfc_gates/shared_preload.py`

Config and macros:

- `config/nfc_reader.cfg`
- `config/nfc_macros.cfg`

Docs and installer:

- `CHANGELOG.md`
- `Readme.md`
- `docs/shared/configuration.md`
- `docs/shared/how-it-works.md`
- `docs/shared/klipper-functions.md`
- `docs/shared/message_definition.md`
- `install.sh`

## 🧭 Suggested PR Description

This PR adds continuous scan-jog support for NFC spool tag detection.

Continuous scan-jog queues forward spool movement through Happy Hare's MMU toolhead and uses a lightweight UID-only probe while the move is estimated to be in flight. Once a UID is found, NFC stops queueing forward chunks, waits for the current chunk to finish, then resolves the UID through Spoolman.

If the UID is already known, scan-jog finishes through the existing read-light, rewind, and Happy Hare / Spoolman dispatch behavior. If the UID is unknown and rich tag parsing is enabled, NFC backs up toward the reader field before attempting the full rich tag read and auto-create path. This keeps continuous scan fast for known spools while preserving rich tag creation workflows for new spools.

The PR also improves operator-facing logging and console output so messages clearly show whether scan-jog is using `Direct Move` or the `MMU_TEST_MOVE` fallback, adds documentation for the new scan settings, fixes NFC help output, and cleans up macro labels so reader names like `lane0` are shown consistently.
