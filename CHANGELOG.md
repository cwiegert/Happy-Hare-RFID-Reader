# Changelog

All notable changes to the EMU NFC Gate Reader are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [05/20/2026] - WoodWorker

### Shared Reader LEDs

- Fixed bypass unresolved-tag feedback replaying across the shared reader's missed-resolution retries. The unresolved red LED effect now starts only once for an unresolved tag sequence, so bypass mode no longer appears to flash 6 times from three repeated attempts.
- Changed `mmu_RFID_unresolved` to exactly 2 red flashes (`strobe 2 2`) and updated shared-reader config comments, installer text, and docs to match.
- Kept bypass-ready confirmation bounded at 2 seconds and ensured scheduled LED timers stop the exact effect that was started.

### Tests

- Added regression coverage proving repeated unresolved shared-reader events only start the red unresolved LED effect once.

---

## [0.9.15] - 05/19/2026 - WoodWorker

### Pending Spool Timeout Now Sourced from Happy Hare

- Removed `shared_pending_timeout` as a configurable key in `[nfc_gate shared]`. The pending window is now read automatically from Happy Hare's `mmu_parameters.cfg` (`[mmu] → pending_spool_id_timeout`) at Klipper connect time via the Klipper `configfile` object. Falls back to 30 s if the value cannot be read.
- `nfc_reader_shared.cfg` and the installer-generated shared config now carry a comment directing users to set `pending_spool_id_timeout` in `mmu_parameters.cfg` instead of a local override.
- Documentation updated across `docs/shared/configuration.md`, `docs/shared/shared-reader.md`, `docs/shared/klipper-functions.md`, and the design docs to reference the HH parameter.

---

## [05/19/2026] - WoodWorker

### Shared Reader Preload — Spool ID Timing Fix

- `MMU_GATE_MAP NEXT_SPOOLID` is now staged in Python immediately when the NFC tag resolves to a spool, rather than inside the `_NFC_SHARED_PRELOAD` GCode macro. Previously the macro ran AFTER Happy Hare had already finalized the gate map entry for the loaded gate, so the spool ID was never applied to the active gate and it displayed `Id: n/a`.
- For auto-created spools, `MMU_SPOOLMAN REFRESH=1` is also dispatched at resolution time (before `NEXT_SPOOLID`) so Happy Hare knows about the new spool before the hint is staged.
- Removed the now-redundant `MMU_GATE_MAP NEXT_SPOOLID` and `MMU_SPOOLMAN REFRESH` lines from `_NFC_SHARED_PRELOAD`; the macro now only handles validation (`PRELOAD_CHECK`) and commit (`PRELOAD_COMMIT`).
- If Happy Hare already shows the staged shared-reader spool assigned to a loaded gate, `_NFC_SHARED_PRELOAD` now treats that as success and commits the pending NFC read without clearing the Happy Hare gate map. This fixes same-lane eject/read/reinsert flows where the spool briefly loaded and then gate 4 was reset to `Empty` / `Id: n/a`.
- When no spool is pending at preload time (either nothing was tapped or the UID was unresolved), `_NFC_SHARED_PRELOAD` now clears the target gate's map to `SPOOLID=-1 COLOR=FFFFFF55` before firing the `force_spool_id` advisory. Previously the gate retained the previous spool's color and ID so the EMU showed the wrong spool instead of the shadowed unknown state.

### LED Effects

- `mmu_RFID_read` changed from 3 flashes to 1 flash on tag scan, differentiating it clearly from `mmu_RFID_bypass_ready` (3 flashes = bypass spool confirmed).
- Bypass-ready/shared-ready LED effects are now scheduled to stop after 4 seconds when used for immediate bypass spool confirmation, so the confirmation flash does not run indefinitely.
- The shared-reader 80% pending-timeout warning now defaults to `mmu_RFID_warning`, switches from the ready LED to the warning LED at the warning point, and re-arms itself for the actual timeout so expiry cleanup cannot be missed.
- When a shared-reader pending spool expires, NFC now stops the warning LED, issues `MMU_GATE_MAP QUIET=1` to restore Happy Hare steady-state LEDs, and restarts shared polling so another spool can be scanned immediately.

### Shared Reader Installer

- Shared-reader installs no longer inject `shared_led_segment` into generated config; users can own that setting directly in `nfc_reader_shared.cfg`.
- Re-running the installer in shared-reader mode now skips `nfc_reader_hw.cfg` lane-section merging so pure shared installs do not unexpectedly append lane sections.
- Shared-reader installs now update only the selected hardware/startup keys in `[nfc_gate shared]`, preserving user-edited LED settings and comments.
- The installer no longer regenerates the full shared-reader config on every shared install. It now uses targeted `set_config_value` updates for `i2c_mcu`, `i2c_bus`, `shared`, and `startup_polling`.
- Added `detect_mmu_led_unit()` to the installer. It reads `[mmu_leds <name>]` from `mmu_hardware.cfg` and exposes the real unit name so the post-install summary shows the correct whole-chain effect name (e.g., `unit0_mmu_RFID_read_exit`) instead of a hardcoded placeholder.

### Shared Reader LEDs

- `shared_led_segment: exit` keeps the whole-segment LED behavior (`unit0_mmu_RFID_read_exit`).
- `shared_led_segment: gate` restores the legacy single-lane LED behavior (`mmu_RFID_read_exit_N`).
- `shared_led_segment` is normalized to lowercase at load time, so values like `Gate` and `EXIT` resolve consistently.
- `nfc_reader_shared.cfg` now documents `shared_led_segment` as an LED target selector: `exit`/`entry`/`status` target a whole segment, while `gate` targets the legacy single lane.

### Happy Hare Bypass

- Shared-reader spool resolution now detects Happy Hare bypass mode (`printer.mmu.tool == -2`) and immediately sets Moonraker's active Spoolman spool through `_NFC_SHARED_BYPASS_SPOOL_CHANGED`.
- When bypass is active, the shared reader does not stage `NEXT_SPOOLID` or wait for the Happy Hare preload hook; normal shared preload behavior is unchanged for MMU gates.

### Config Compatibility

- Replaced raw CSS hex colors in `_NFC_SPOOL_CHANGED` console messages with named colors so Klipper's config/template parser does not treat `#` as a comment marker and halt during startup.
- Unknown/no-spool NFC metadata now uses `COLOR=FFFFFF55` consistently, matching the scan-unresolved placeholder color.
- Shared-reader preload transaction warnings now use the standard NFC console color tags instead of raising Klipper command errors, avoiding red `!! Error running _NFC_SHARED_PRELOAD` wrappers for nonfatal bridge warnings.
- NFC logger console output now avoids Klipper `RESPOND TYPE=error` / `TYPE=command` coloring and uses the NFC HTML tag colors for `[OK]`, `[WARN]`, and `[ERROR]` consistently.

### Log Rotation and Pruning

- Fixed archive accumulation: `_prune_old_archives` now runs at every Klipper startup, not only when the midnight-crossing `_rotate` path fires. Previously, Klipper restarts before midnight would rename the old log file but never prune, so archives built up indefinitely.
- Retention remains 7 days / 7 archives; the bug was the pruning never ran, not the threshold.

### Console Output and Logging Consistency

- All log message prefixes standardized to `NFC[name]: ` across every module (`nfc_manager.py`, `shared_preload.py`, `tag_handler.py`, `scan_jog.py`). Previously the format varied between `nfc_gate: [name] `, `nfc_gate: `, and bare messages — making it hard to filter logs for a specific gate.
- Switched logger console dispatch away from generated `RESPOND` scripts and onto direct `gcode.respond_info()` calls. This prevents Fluidd/Mainsail from showing `echo:` or Klipper-added prefixes on NFC status lines, while still allowing the NFC logger to color `[OK]`, `[WARN]`, and `[ERROR]` consistently.
- Shared-reader pending warnings and timeout messages now use the same themed prefix format as the rest of the console output: `[WARN] NFC[shared]: ...` and `[ERROR] NFC[shared]: ...`. This fixes the old `NFC [WARN] [shared]` ordering and removes leftover white `[shared]` lane styling.
- Scan-jog and per-lane command replies now use the same themed `NFC[lane]: ...` console prefix, including READ/POLL replies and rewind messages. Logger console output also normalizes older internal `[lane]: ...` messages before they reach the UI.
- `shared_preload.py` fully converted from `gcmd.respond_info()` to `logger.info/warning/error()`. All `PRELOAD_CHECK`, `PRELOAD_COMMIT`, and `PRELOAD_CLEAR_ASSIGNED` feedback now routes through the shared logger so the three output destinations (nfc_reader.log, klippy.log forwarding, and Klipper console) are always in sync.

### Tests

- Added regression coverage for shared LED target naming, including whole-segment and legacy single-gate modes.
- Added regression coverage for shared-reader bypass detection and immediate active-spool assignment.
- Added installer checks to keep `shared_led_segment` out of generated shared config and prevent shared-only installs from merging lane hardware sections.
- Added a macro config guard so `action_respond_info` lines do not use raw CSS hex color literals.
- Added logger regression coverage for direct console dispatch, NFC-themed warning ordering, green `[OK]`, yellow `[WARN]`, red `[ERROR]`, and removal of the old white lane-name styling.
- Added regression coverage for scan-jog rewind and per-lane READ/POLL console prefixes.

---

## [05/18/2026]

### Shared Reader Improvements

- Added full shared-reader command coverage through `NFC_SHARED`, including help, status, summary, cancel, replace, LED test, cache sync, polling, scan, and raw poll actions.
- `NFC_SHARED HELP=1` now documents the required `=1` action flag style so commands match Klipper's parser and avoid malformed bare commands.
- Shared-reader installs now include the base `nfc_reader.cfg` along with `nfc_reader_shared.cfg`, so the standard NFC commands and shared commands are both available after install.
- Shared-reader polling uses the scan-jog reader interval, not the slower base polling interval. The config comments now call this out so tuning the shared reader is less mysterious.
- The shared reader now prints a green `[OK]` line as soon as a tag is successfully read and staged, before the later Happy Hare preload messages.
- `nfc_reader_shared.cfg` now defaults `i2c_mcu: mmu` so the most common wiring works without editing the hardware config.

### LED Behavior

- Shared reader events now flash **all MMU gate exit LEDs simultaneously** instead of a single per-gate LED. All five `mmu_RFID_*` effects now use `define_on: gates, exit`, which creates both per-gate effects (used by per-lane readers) and a whole-chain effect that targets every gate at once (used by the shared reader).
- Fixed indefinitely looping unresolved-tag strobe — it now plays a fixed number of flashes and stops cleanly.
- `mmu_RFID_ready` (green strobe) now persists continuously while a spool is staged and waiting to load; previously it blinked twice and stopped.
- Added `mmu_RFID_warning` amber strobe: plays when the pending timeout is 80% elapsed, giving the user a visible countdown before the spool is dropped.
- Amber warning strobe stops exactly when the pending timeout expires — no overshoot.
- After all NFC effects finish, HH gate LEDs are restored via `MMU_GATE_MAP QUIET=1` only when no spool is pending, preventing HH's gate repaint from killing the active ready effect mid-wait.
- On pending timeout, polling always restarts automatically (equivalent to issuing `NFC_SHARED REPLACE=1`).

### Klipper Deadlock Fix

- Eliminated a class of Klipper deadlocks caused by calling `run_script()` from inside GCode command handlers. All LED, respond, and HH gate-map calls that originate from GCode handlers are now deferred via `register_async_callback`. Affected commands: `NFC_SHARED LED_TEST`, `REPLACE`, `CANCEL`, `CLEAR`, `PRELOAD_CLEAR_ASSIGNED`.

### Happy Hare Preload Behavior

- Fixed the pure shared-reader stale assignment path. If Happy Hare still has a spool assigned to a gate when the shared reader stages that same spool, NFC now clears the stale Happy Hare gate assignment and gets ready for the next tag.
- Hybrid installs are still protected: when per-lane readers are present, the shared reader does not overwrite or clear legitimate per-lane assignments.
- `NFC_SHARED PRELOAD_CLEAR_ASSIGNED=1` now receives the assigned gate number so the automatic cleanup can target the correct Happy Hare gate.
- `force_spool_id` no longer throws a Klipper command error when no spool is staged. It now shows one red `[ERROR]` console advisory instead of duplicated `!!` error lines.
- `_NFC_SHARED_PRELOAD` macro cross-checks `gate_status` when evaluating already-assigned spools — a gate with status 0 (filament absent) is no longer treated as occupied, fixing the case where a spool ejected from a gate with no per-lane reader left a stale HH assignment that blocked future loads of the same spool.

### Console Output

- All NFC console messages now route through the logger rather than inline `RESPOND` GCode calls, eliminating `echo:` format output and removing a second source of deadlocks.
- Standardized console tag colors:
  - `[OK]` renders green.
  - `[WARN]` renders yellow.
  - `[ERROR]` renders red text without forcing a Klipper error unless the command truly fails.
- Reduced duplicate shared-reader warnings by keeping recovered stale-assignment details in the internal log and showing only one concise console warning.
- Replaced stop-sign precondition glyphs with `[ERROR]` for clearer, consistent console messages.

### Documentation

- README and quick-install guide updated with the correct Happy Hare hook parameter: `variable_user_post_preload_extension: '_NFC_SHARED_PRELOAD'` in `mmu_macro_vars.cfg`.
- Install instructions now list the required config includes for shared-reader installs.
- Command reference updated to cover `NFC_SHARED` commands users are expected to run day-to-day.
