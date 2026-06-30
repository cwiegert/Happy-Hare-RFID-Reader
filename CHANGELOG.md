# Changelog

All notable changes to the EMU NFC Gate Reader are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased] - Continuous Scan - WoodWorker

### Spoolman-Disabled Support

- Added `spoolman_url: disabled` as a valid configuration option. When set, all
  Spoolman lookup paths short-circuit cleanly (`gate._spoolman is None` guards
  are already present throughout the Python code). Tag metadata or UID-only
  resolution continues to work; the gate map and Happy Hare filament data are
  still updated via `_NFC_SPOOL_CHANGED` when `tag_parsing: True` and the tag
  carries material/color fields.
- Added `BRAND`, `MIN_TEMP`, `DIAMETER`, and `WEIGHT` parameters to the
  `_NFC_SPOOL_CHANGED` macro dispatch for the no-Spoolman (metadata-only) path.
  All four are always sent; empty string is used when the tag does not provide a
  value. `klipper_interface.py` now reads `brand`/`vendor`, `min_temp`,
  `diameter_mm`, and `weight_g`/`spool_weight_g` from the tag metadata dict.
  The macro console message displays all fields; `MMU_GATE_MAP` continues to
  receive only the fields Happy Hare supports (NAME, MATERIAL, COLOR, TEMP).
- Added `disabled` as a third Spoolman option in the installer (lane and shared
  paths). When selected the installer sets `spoolman_url: disabled`, defaults the
  tag read mode prompt to `rich` (instead of `spoolman`), prints a note
  explaining that rich mode is required to pass filament data to Happy Hare
  without Spoolman, and skips the auto-create Spoolman spool question since there
  is no Spoolman instance to create records in.
- Fixed no-Spoolman continuous scan-jog resolution. When continuous mode finds a
  UID during motion and `tag_parsing: True`, it now runs the normal rich-tag poll
  immediately after the current move completes, before scheduling the
  overshoot-backup retry. This lets metadata-only tags resolve cleanly with
  `spoolman_url: disabled` instead of backing up first because the Spoolman UID
  lookup was unavailable.

### Reader Hardware

- Added RC522 as a supported SPI reader via `reader_type: rc522`.
  `reader_factory.py` now creates an `RC522Driver` through Klipper's SPI bus
  helper before the I2C reader path. The RC522 driver exposes the current
  reader API (`read_tag(timeout=...)`, `read_target(timeout=...)`, and target
  cleanup), and honors short continuous scan probe timeouts. RC522 uses its own
  optional `rc522_transceive_delay` setting (default `0.035`) instead of
  inheriting the slower PN532 passive-target delay.
- Added RC522 ISO14443A SELECT/cascade support and NTAG/Type-2 page reads.
  `read_target()` now returns selected target info with SAK/ATQA when SELECT
  succeeds, including 4-byte, 7-byte, and 10-byte UID cascade handling. The
  driver exposes `ntag_read_page()`, `ntag_read_user_memory()`, and
  `ntag_read_ndef_user_memory()` so `tag_handler.py` can run the existing
  rich-tag metadata resolution path for RC522 Type-2 tags. If SELECT fails
  after a valid anticollision UID, the reader falls back to the existing
  UID-only target shape so Spoolman UID lookup still works. ISO15693 remains
  unsupported on RC522.
- Added MIFARE Classic authentication and block reads to the RC522 driver.
  `mifare_authenticate()` uses the RC522 hardware `MFAuthent` (0x0E) command
  which handles the three-pass challenge/response exchange internally — no
  over-the-air auth frame is sent by the host like on PN532. After auth,
  `mifare_read_block()` uses the same `_transceive_crc` path as NTAG page
  reads; `RxCRCEn` in `ModeReg` strips the two CRC trailer bytes so the FIFO
  holds only the 16 data bytes. `mifare_read_authenticated_blocks()` matches
  the PN532 return shape (`{"uid_bytes": …, "blocks": {abs_block: bytes}}`)
  so `tag_handler.py` and the Bambu parser stay reader-agnostic. The RC522
  stops on the first auth or read failure (same policy as PN7160) because the
  hardware crypto state (`MFCrypto1On`) becomes unreliable after a rejected
  handshake; `_stop_crypto1()` clears `Status2Reg` bit 3 in the `finally`
  block so subsequent scans are never left in encrypted mode. Bambu refill
  RFID tags (MIFARE Classic 1K with HKDF-derived sector keys) are now fully
  supported on RC522.
- Added RC522 diagnostic logging for the SPI path. Init failures now log a
  warning with SPI wiring/config hints before re-raising, health checks warn
  when the reader does not respond or antenna TX bits are off, and post-REQA
  anticollision/checksum failures produce warning-level summaries with raw
  detail retained behind `debug >= 4`. Manual `SCAN=1` output now handles
  UID-only targets with no SAK value.
- Added RC522-specific low-level debug commands guarded by `low_level_debug`.
  `INIT=1` and `SCAN=1` exercise the normal RC522 init and UID scan paths;
  `RC522_DUMP_REGS=1`, `RC522_REG_READ=<reg>`,
  `RC522_REG_WRITE=<reg> VALUE=<byte>`, `RC522_ANTENNA=0|1`,
  `RC522_REQA=1`/`RC522_WAKE=1`, and
  `RC522_TRANSCEIVE='<bytes>' BIT_FRAMING=<0-7>` provide register, antenna,
  FIFO transceive, and tag-wake diagnostics without reusing PN532 ACK/ready
  semantics.
- Added orchestration-facing RC522 SPI aliases for the same diagnostics:
  `RC522_REGISTER=<reg> [VALUE=<byte>]`, `RC522_ANTENNA_ENABLE=0|1`,
  `RC522_TAG_WAKE=1`, and
  `RC522_FIFO_TRANSCEIVE='<bytes>' BIT_FRAMING=<0-7>`. These run through the
  `NFC` / `NFC_SHARED` handlers, so shared-reader and per-lane RC522 debugging
  uses the same NFC Reader command surface as init and scan.

### Console Output and Logging

- Replaced all abbreviated `HH` references with `Happy Hare` across log strings, console messages, docstrings, and user-visible status text in `nfc_manager.py`, `scan_jog.py`, `shared_preload.py`, and `hh_status.py`. `HH_SYNC` macro/command names and the `HH:MM:SS` datetime format in `log.py` are unchanged.
- Changed `[SCAN]`, `[REWIND]`, and `[OK]` tagged scan-jog messages to appear at `console_log_level: 2`, matching the warning threshold. Previously all scan-jog messages required level 3. Verbose detail lines with no tag (move-queued timing, LED state changes) still require level 3.
- Gated retry-jog position detail lines (`decode retry move queued Xmm  scan position X.X / X.Xmm`) and overshoot backup position detail lines (`continuous overshoot backup queued Xmm  scan position X.X / X.Xmm`) behind `gate._debug >= 3`. The `[WARN]` tagged console messages for these same events are unaffected and remain visible at the default warning threshold.
- Changed continuous jog move logging so it reports the actual execution path:
  `Direct Move` for direct Happy Hare MMU-toolhead moves or `MMU_TEST_MOVE` for
  the G-code fallback. The user-visible `[SCAN]` move line now fires after the
  call so it accurately reflects what was executed.

### Macro Naming Consistency

- The configured lane name (e.g. `lane0` from `[nfc_gate lane0]` in `nfc_reader_hw.cfg`) is now forwarded to all three NFC event macros as a `READER=` parameter. `_NFC_SPOOL_CHANGED`, `_NFC_SPOOL_REMOVED`, and `_NFC_TAG_NO_SPOOL` use `params.READER | default('Lane' ~ gate)` so console output reads `NFC[lane0]:` matching the section name in hardware config rather than a hardcoded `NFC gate N:` format.
- Capitalized `Gate` (capital G) in the remaining user-visible macro output lines — `NFC_HH_SYNC_CACHE` sync progress and `_NFC_SCAN_UNRESOLVED` — to match the capitalization used by `NFC_STATUS`.

### Command Fixes

- Added `NFC GATE=<#> JOG_SCAN=1` to the `NFC_HELP` output. The command existed but was only documented in the per-gate `NFC GATE=<#> HELP=1` listing.
- Fixed `NFC GATE=<#> HELP=1` incorrectly triggering the low-level PN532 debug path. `low_level_debug_requested()` checks for a `HELP` parameter, so `HELP=1` was intercepted before the per-gate help handler ran, producing spurious `polling paused for low-level PN532 debug` and `low_level_debug is disabled in config` console messages. The HELP check now runs first in `cmd_NFC`.
- Corrected the help text entry from `NFC GATE=<#> HELP` to `NFC GATE=<#> HELP=1` to match the required Klipper parameter style.

### Scan-Jog Continuous Mode — Now Default

- Promoted continuous scan-jog to the default motion mode. `scan_motion_mode: continuous` is now the out-of-the-box setting in `nfc_reader.cfg` and the Python fallback default. `scan_motion_mode: stopped` remains fully supported for marginal reader or tag alignment where continuous polling misses the tag.
- Updated `docs/shared/configuration.md`, `docs/shared/klipper-functions.md`, and `docs/shared/how-it-works.md` to reflect continuous as the default and document stopped as the alternative.

- Added opt-in continuous scan-jog mode via `scan_motion_mode: continuous`.
  Continuous mode queues forward search chunks through Happy Hare's MMU toolhead
  and polls NFC while the chunk is estimated to be moving, while preserving
  existing tag-found actions, the 0.1 second read-light hold, rewind, and
  completion logic.
- Added continuous scan settings:
  `scan_continuous_step_mm`, `scan_continuous_speed`,
  `scan_continuous_accel`, and `scan_continuous_poll_interval`.
- Changed the default rich-tag decode retry spacing from 2 mm to 5 mm and added
  continuous-mode UID hit-window recentering before rich parsing/retries when a
  UID hit does not resolve through Spoolman.
- Changed continuous-mode rich tag retry jogs from a ±sweep to a two-phase
  backward/forward walk. After the initial overshoot backup, retries now:
  (1) step backward from the backup point in `scan_decode_retry_mm` increments
  (half of `scan_decode_retry_rounds` attempts, floor at 0); then
  (2) return to the backup point and step forward in the same increment toward
  the original UID detection position (ceiling = scan total before backup),
  using the remaining attempts. The backup point and UID detection position are
  recorded as `_scan_continuous_overshoot_start_mm` and
  `_scan_continuous_overshoot_origin_mm` at backup time. Both the
  `queue_continuous_overshoot_backup` and `retry_incomplete_decode` backup paths
  record these values. The `retry_incomplete_decode` function now redirects
  continuous+overshoot retries to `queue_decode_retry_move` (same path as
  no-tag-found retries) instead of falling through to the stopped-mode ±sweep.
- Fixed a race in the first continuous chunk where `mmu.select_gate()` blocks
  for the full gate-positioning time (~0.5 s) inside `run_direct_continuous_jog`.
  Because this blocking consumed more time than the expected move duration,
  `remaining_duration` was computed as zero and the code declared the first chunk
  complete before it entered the motion queue. The second chunk was then submitted
  immediately, creating a growing motion queue backlog that shifted all subsequent
  position estimates by one full chunk (75 mm). Tags were detected one chunk
  (75 mm × number of queued moves) later than their physical location. Fixed by
  detecting when gate selection ran during the call and using `expected_duration`
  rather than `max(0, expected_duration - command_elapsed)` for that call only.
- Changed in-flight continuous probe rate. `scan_continuous_poll_interval` now
  serves as the hardware probe timeout passed to `read_tag()` in addition to its
  existing role as the timer reschedule interval. The reactor now reschedules
  immediately after each in-flight probe (returning `now` rather than
  `now + poll_interval`) so the hardware timeout is the only pacing. This raises
  the probe count per chunk from ~3 to ~10–15 (depending on speed and step size),
  reducing the effective per-probe coverage from ~28 mm to ~5 mm and making
  near-edge misses much less likely.
- Added missing commands to the global `NFC_HELP` output. The following commands
  existed and worked but did not appear in the help listing:
    `NFC GATE=<#> INIT=1` — re-run reader hardware initialisation
    `NFC GATE=<#> APPLY=1` — send cached spool to Happy Hare immediately
    `NFC GATE=<#> CLEAR_CACHE=1` — clear cached spool/UID without dispatching
    `NFC GATE=<#> HH_SYNC=1 SPOOL_ID=<n>` — seed lane cache from Happy Hare gate map
- Changed continuous in-flight scanning to perform a UID-only probe while the
  chunk is moving, then defer Spoolman lookup and rich tag parsing until the
  current chunk has finished.
- Switched continuous scan behavior to try resolving a detected UID first;
  only if tag resolution fails is the reverse/overshoot backup jog queued.
- Documented the tested continuous profile: 50 mm chunks at 150 mm/s with
  2000 mm/s^2 acceleration and a 0.05 s in-flight tag-check cadence.
- Optimized the baseline continuous scan configuration for optimal speed while
  minimizing overshoot, balancing chunk pacing and probe cadence to reduce
  missed tags without introducing extra backtracking.
- Added direct Happy Hare MMU-toolhead forward jog support for continuous scan,
  with `MMU_TEST_MOVE WAIT=0` retained as a compatibility fallback.
- Reduced repeated continuous-mode search LED calls by removing the top-of-loop
  LED reapply while keeping the post-move reassertion.
- Fixed scan-jog direct console messages so they respect `console_output` and
  `console_log_level` instead of always calling `respond_info`.

---

## [0.9.23] - 06/01/2026 - WoodWorker

### Installer Prompt Cleanup

- Removed the installer terminal theme/profile prompt and the `-p` profile color option.
- Simplified installer prompt emphasis to default terminal color with bold text only; no custom cyan/green/yellow/magenta highlight colors are used.
- Added a per-lane reader I2C bus prompt. The installer now writes the selected bus to the base `[nfc_gate]`, comments/uncomments the SLB and EBB preset lines, and uses a custom active bus line when the user enters a different hardware bus.
- Added a per-lane reader MCU prefix prompt. The installer defaults to `mmu` and writes lane hardware sections as `mmu0`, `mmu1`, etc.; entering `lane` writes `lane0`, `lane1`, etc.
- Added an installer warning when software-I2C sensor config is detected in printer config files, with the hardware `i2c_bus` line users should apply to sensor sections such as `emu_macros.cfg`.
- Added optional `scan_jog_max` for scan-jog. When set, NFC uses that fixed maximum travel distance instead of reading Happy Hare Bowden calibration. The installer now asks lane-reader users whether to set `scan_jog_max` with a default of `480.0mm` or keep using per-lane Bowden lengths.

### Install / Uninstall Cutover

- Changed the public install target to `Happy-Hare-RFID-Reader` cloned into `~/rfid-reader`.
- Updated Moonraker update-manager generation to use `[update_manager Happy-Hare-RFID-Reader]`, matching the public repo name exactly.
- Added cleanup for old beta Moonraker sections: `[update_manager emu_nfc_reader]`, `[update_manager happy_hare_rfid_reader]`, and `[update_manager Happy-Hare-rfid-reader]`.
- Added beta cutover handling for legacy `~/emu-nfc-reader` installs. The installer now prompts before cleanup, backs up `~/printer_data/config/nfc/`, backs up `moonraker.conf`, removes old Klipper symlinks, removes the legacy repo, and continues with a fresh `~/rfid-reader` install.
- Removed installer-driven update behavior for existing checkouts. Users update with `git pull` before rerunning `bash install.sh`; the installer no longer runs `git pull`, `git fetch`, or sparse-checkout updates during normal installs.
- Updated uninstall behavior to prompt for local repo checkout removal at the end of `bash uninstall.sh`. The default answer is yes; answering `n` keeps `~/rfid-reader`.
- Kept uninstall repo removal guarded so it only removes the expected `~/rfid-reader` git checkout.
- Added local simulation scripts for install and uninstall testing without touching the real repo or public GitHub: `tests/simulate_rfid_reader_install.sh` and `tests/simulate_rfid_reader_uninstall.sh`.
- Updated public README, private README, install/uninstall docs, design notes, and regression tests for the new install path, uninstall prompt, Moonraker section, and public sync target.

### Scan-Jog Safety

- Blocked manual `NFC GATE=<gate> JOG_SCAN=1` when Happy Hare reports the selected gate as empty (`gate_status=0`). NFC now returns a single console error that `jog_scan` is not enabled for an empty gate and does not start motion.
- Changed normal scan-jog rewind/no-tag status messages back to `[REWIND]`/`[OK]` severity so ordinary rewind flow is not reported as `[WARN]`.
- Changed `_NFC_TAG_NO_SPOOL` console guidance to use a red `[ERROR]` heading while keeping the message in the macro alongside the Happy Hare gate-map cleanup.

### Shared Reader Bypass

- Moved the bypass active-spool console confirmation out of `_NFC_SHARED_BYPASS_SPOOL_CHANGED` and into the Python shared-reader path next to the matching `nfc_reader.log` entry. The macro now only calls Moonraker's `spoolman_set_active_spool`.

---

## [0.9.24] - 05/27/2026 - WoodWorker

### Shared Reader LED Target Guard

- Fixed shared-reader legacy LED gate targeting so an MCU-derived index outside Happy Hare's configured gate range no longer sends `MMU_SET_LED GATE=<invalid>`. NFC now falls back to whole-unit LED control for that effect, preventing HH errors such as `The value '4' is not valid for GATE`.
- Documented the fallback in `nfc_reader_shared.cfg` and the shared-reader design notes.
- Added stronger shared-reader hook diagnostics. `NFC_DOCTOR`, startup warnings, and shared pending timeouts now call out when `mmu_macro_vars.cfg` is still wired to the per-lane `NFC JOG_SCAN=1` hook instead of `_NFC_SHARED_PRELOAD`.
- Updated shared-reader docs and config comments so the Happy Hare hook is consistently documented as `variable_user_post_preload_extension: '_NFC_SHARED_PRELOAD'`.
- Restored the shared pending-spool 80% warning as an explicit Klipper console message while keeping the matching entry in `nfc_reader.log`.
- Made the shared 80% pending warning fire from the main poll timer as well as the warning timer, and changed the warning LED effect to avoid HH `DURATION` blocking. `_NFC_SHARED_PRELOAD` now releases warning LED feedback as soon as preload validation begins.

---

## [0.9.23] - 05/26/2026 - WoodWorker

### Shared Reader LED Duration Contract

- Changed normal shared-reader read and staged-ready LED feedback so NFC calls `MMU_SET_LED UNIT=0 EXIT_EFFECT=<effect>` without `DURATION`. These effects must remain interruptible by the next shared-reader state, such as ready, unresolved, warning, loaded, cancel, replace, or timeout.
- Added a local shared-read LED failsafe. If a normal shared read effect starts and no follow-up state takes LED ownership, NFC releases back to Happy Hare with `MMU_GATE_MAP QUIET=1` after `read_effect_duration`.
- Added HH-control restoration on shared read timeout, manual `NFC_SHARED READ=0`, reader failure, disconnect, and print-start polling suspension when no spool is pending.
- Added `NFC_SHARED RESET=1` as a recovery command that clears pending/read/preload shared state, restores HH LED ownership, and restarts shared polling.
- Kept HH `DURATION` only for standalone shared LED feedback: `NFC_SHARED LED_TEST=1`, unresolved feedback, and bypass-ready confirmation. Pending-warning feedback is interruptible so Happy Hare can take LEDs back when preload starts.
- Documented the Happy Hare `MMU_SET_LED DURATION=` behavior: when `DURATION` is passed, HH sets `pending_update[unit] = True`; while that timer is active, later LED effect requests for the same unit are ignored by `_set_led()`.
- Removed stale `ready_effect_duration` documentation for the normal staged-ready path. Staged-ready feedback now follows the shared-reader lifecycle and releases with `MMU_GATE_MAP QUIET=1` on preload commit, cancel, replace, or pending timeout.
- Updated shared-reader tests to assert the interruptible versus timeout-bound LED behavior.

### Repo Hygiene

- Stopped tracking `.claude/settings.json` and added it to `.gitignore` so local agent/editor settings do not appear in repo changes.

---

## [0.9.22] - 05/24/2026 - WoodWorker

### Spoolman UID Registration

- Added `NFC_Register UID=<uid> Spool_id=<id>` so a known NFC UID can be assigned to an existing Spoolman spool directly from the printer console.
- The command validates Spoolman availability, confirms the target spool exists, writes the configured `spoolman_rfid_key`, and clears NFC's UID lookup cache. It intentionally does not call `MMU_SPOOLMAN REFRESH` inline because that Happy Hare path can block Klipper after the Spoolman write succeeds.
- Avoided the full duplicate-UID scan from the Klipper command path so `NFC_REGISTER` stays light.
- Stopped reading the Spoolman PATCH response body after UID registration. Spoolman applies the RFID field update before the body is needed, and waiting for a slow/kept-open body can freeze Klipper's reactor after the UID has already been saved.
- Added command help, README, message documentation, and regression coverage for successful registration and missing-spool rejection.
- Fixed `NFC_REGISTER` help/error rendering so the command appears with the same uppercase styling as other commands, avoids console-swallowed angle-bracket placeholders, and emits command errors once while still writing them to `nfc_reader.log`.

### Lane LED Test Commands

- Added `NFC GATE=<n> LED_TEST=1` to test the configured per-lane tag-read effect on one gate.
- Added `NFC_LED_TEST ALL=1` to test the configured per-lane tag-read effect on every enabled lane reader, with a default 0.20 s chase delay between gates.
- Added `LED_effect_mgr.py` as the NFC/Happy Hare LED contract boundary. Lane scan-jog, shared-reader feedback, and LED test commands now route LED effect naming, public `MMU_SET_LED` effect calls, and HH release requests through the shared manager.
- Completed the LED contract layer with semantic event helpers (`scan_start`, `tag_read`, `rewind`, `spool_ready`, `unresolved`, `auto_create`, `warning`, and `led_test`) and moved lane LED test cycle scheduling into `LED_effect_mgr.py`.
- Fixed `NFC GATE=<n> LED_TEST=1` so the lane LED test dispatches through Klipper's async callback path instead of running nested GCode from inside the command handler.
- Added `CYCLES=<count>` to per-gate and all-lane LED tests. The default is `2`, and each cycle starts the configured base effect with `MMU_SET_LED GATE=<n> EXIT_EFFECT=<effect>`; NFC no longer sends a default-effect restore between cycles and ends the sequence with `MMU_GATE_MAP QUIET=1`.
- Updated command help, README, message docs, and tests for the new LED test commands.

---

## [0.9.21] - 05/23/2026 - WoodWorker

### Scan-Jog LED Reliability

- Added `_NFC_SCAN_JOG_PRELOAD`, a per-lane Happy Hare post-preload hook macro that starts `mmu_clockwise_slow_exit_<gate>` immediately before launching `NFC GATE=<gate> JOG_SCAN=1`.
- Added a delayed scan LED reassert timer so the per-gate clockwise search effect is reapplied after Happy Hare's own LED refreshes from `MMU_GATE_MAP`, `MMU_SELECT`, and `MMU_TEST_MOVE`.
- Reasserted the searching effect after scan prep, each scan step, each jog move, and decode-retry moves so the NFC scan state keeps visible control of the active gate while scan-jog is running.
- Cancelled pending LED reassert timers when scan-jog exits, rewinds, disconnects, or intentionally hands LED control back to Happy Hare.
- Added a short visible tag-read hold before rewind so `mmu_RFID_read_exit_N` can play before the rewind effect starts.
- Kept rewind LED feedback active through the rewind move and released back to Happy Hare only after parking/polling cleanup completes.
- Updated per-lane hook documentation to wire `variable_user_post_preload_extension: '_NFC_SCAN_JOG_PRELOAD'` instead of calling `NFC JOG_SCAN=1` directly.
- Added regression coverage for the hook wrapper ordering, delayed search-effect reassert, and updated scan-jog timing expectations around the tag-read hold.

### Config Defaults

- Fixed default inheritance for the per-lane LED effect names so base `[nfc_gate]` values are available to lane instances unless explicitly overridden.

### Installer

- Stopped installing `nfc_reader_shared.cfg` during lane-reader installs. Shared-reader config is now written only when the user selects the shared-reader install path.
- Reader-type detection now defaults to `shared` only when `printer.cfg` actively includes a shared-reader config, or when a shared-reader config exists without a lane hardware config. Old shared-reader template files no longer force a lane reinstall to default to shared.
- Made interactive choice highlighting consistent: the default selection is highlighted in both the description text and bracket prompt; non-default choices remain unhighlighted.

---

## [0.9.20] - 05/23/2026 - WoodWorker

### Per-Gate LED Feedback — Scan-Jog and Spool Resolution

Added per-gate LED effects for per-lane readers across three events. All effects target only the active gate using HH's `_exit_N` naming convention (`_MMU_SET_LED_EFFECT EFFECT={name}_exit_{gate} REPLACE=1`).

**Scan-jog state machine** (`scan_jog.py`):

| Phase | Effect fired |
|---|---|
| Searching (first scan step, deferred from `start()`) | `mmu_clockwise_slow_exit_N` |
| Tag read confirmed | `mmu_RFID_read_exit_N` |
| Rewind started (tag found or no-tag abort) | `mmu_anticlock_fast_exit_N` |
| Park complete / polling resumed | `MMU_GATE_MAP QUIET=1` → returns LED control to Happy Hare |

**Spool resolution** (`tag_handler.py`):

| Event | Effect fired |
|---|---|
| Spoolman auto-create in progress | `mmu_RFID_creating_exit_N` (stopped when API call returns) |
| Tag found but cannot be resolved to a spool | `mmu_RFID_unresolved_exit_N` (self-terminates) |

- The shared reader already handled `mmu_RFID_creating` and `mmu_RFID_unresolved` via its own effect scheduler. Per-lane effects now use the same `tag_handler.py` call sites, guarded by `not getattr(gate, '_shared', False)`.
- Effect names are configurable in `nfc_reader.cfg` via five new keys in the base `[nfc_gate]` section: `scan_searching_effect`, `scan_tag_read_effect`, `scan_rewind_effect`, `lane_auto_create_effect`, `lane_unresolved_effect`. Per-lane sections inherit these from the base or can override individually. Set any key to empty to disable that effect.
- Module constants `LED_SEARCHING`, `LED_TAG_READ`, `LED_REWINDING` in `scan_jog.py` and `LED_AUTO_CREATING`, `LED_UNRESOLVED` in `tag_handler.py` serve as code-level fallbacks only; the live effect name is always read from `gate._xxx_effect` at runtime.
- The searching effect is deferred to the first reactor timer step (`run_pending_hh_prep`) so it fires from timer context where `gcode.run_script()` is safe.
- Required: all five base effects must be defined with `define_on: gates` (or `define_on: gates, exit`) in the HH LED config so Klipper generates per-gate `_exit_N` variants automatically. `mmu_RFID_read`, `mmu_RFID_creating`, and `mmu_RFID_unresolved` are already defined this way in `nfc_macros.cfg`.

---

## [0.9.19] - 05/22/2026 - WoodWorker

### Reader Diagnostics

- Added `NFC_DOCTOR`, a no-motion setup check that reports configured lane readers, the shared reader, disabled readers, Spoolman availability, shared-reader Happy Hare hook wiring, and static configuration warnings.
- Added startup warnings for configuration combinations that are accepted by Klipper but unlikely to work as intended, including rich-tag reads with tag parsing disabled, auto-create with tag parsing disabled, and auto-create without a usable Spoolman URL.

### Config Compatibility

- Added `enabled: False` support for per-lane and shared `[nfc_gate ...]` sections. Disabled readers keep their config block in place but skip PN532/I2C initialization, command registration, polling, and scan setup while still appearing in status and doctor output.
- Updated the hardware and shared-reader config templates, installer-generated configs, and configuration docs to show the new `enabled` option.

### Documentation and Tests

- Added `NFC_DOCTOR` to the README command list and the Klipper command reference.
- Added regression coverage for doctor command registration, disabled-lane reporting, and static warning generation. Full test suite passes with `376` tests.

---

## [0.9.18] - 05/20/2026 - WoodWorker

### Test Suite — Macro Tests Updated

- Rewrote 4 tests across `test_nfc_macros_config.py` and `test_shared_reader.py` that were asserting old `_NFC_SHARED_PRELOAD` behavior (`MMU_SELECT`, `MMU_GATE_MAP`, `MMU_SPOOLMAN`, `auto_created`). Tests now verify the validate-and-commit-only pattern and assert that gate map calls are absent from the macro.

### Documentation — Config Inheritance Clarified

- Added a `docs/shared/configuration.md` example showing top-level `[nfc_gate]` defaults and per-lane `[nfc_gate laneN]` overrides for values like `i2c_address`, `i2c_bus`, and `scan_enabled`.
- Removed obsolete LED holder files from `NFC Mounting Bracket`, preserving only `LED_holder_with_NFC_Bambu_height v3.step`.

---

## [0.9.17] - 05/20/2026 - WoodWorker

### Shared Preload Macro — Root Cause Fix

- Removed all `MMU_GATE_MAP`, `MMU_SELECT`, and `MMU_SPOOLMAN` calls from `_NFC_SHARED_PRELOAD`. Python stages `MMU_GATE_MAP NEXT_SPOOLID` at tag-read time; Happy Hare processes it during the pregate load and owns the gate map entry by the time `user_post_preload_extension` fires. The macro re-assigning the gate map inside the hook was interfering with HH's finalized state, causing Spoolman ID to remain `-1` in the gate editor even though filament metadata was correct. The macro is now validate-and-commit only: `NFC_SHARED PRELOAD_CHECK=1` and `NFC_SHARED PRELOAD_COMMIT=1`.
- Removed `MMU_SELECT GATE={target_gate}` — HH has already selected the gate before the post-preload hook fires; re-selecting inside the hook can reset gate state and halt macro execution before `PRELOAD_CHECK` runs.

### Shared Preload Macro Cleanup (earlier in session)

- Removed the `auto_created` guard and redundant `MMU_SPOOLMAN REFRESH=1 QUIET=1` — Python already issues the refresh at tag-resolution time before staging `NEXT_SPOOLID`.
- Replaced `MMU_SPOOLMAN SPOOLID={spool_id} GATE={target_gate} QUIET=1` with `MMU_SPOOLMAN SYNC=1 QUIET=1` to match the pattern used by `_NFC_SPOOL_CHANGED`.

### Documentation — First-Install Gaps Fixed

- Added "Shared Reader — Additional Required Steps" section to `install-uninstall.md` covering the two things that silently break a first-time shared-reader install: wiring `variable_user_post_preload_extension: '_NFC_SHARED_PRELOAD'` in `mmu_macro_vars.cfg`, and setting `pending_spool_id_timeout` in `mmu_parameters.cfg` (the 30 s fallback is too short for normal use).
- Fixed stale description in `shared-reader.md` ("What it does" and step 5 of the load flow) that still described the old macro behaviour — selecting the gate and issuing `MMU_GATE_MAP`/`MMU_SPOOLMAN` directly. Both now accurately describe the validate-and-commit-only macro.

---

## [05/20/2026] - WoodWorker

### Shared Reader LEDs

- Changed shared-reader ready, bypass-ready, and warning LED defaults from strobes to breathing-style effects so staged/ready feedback is calmer while the EMU waits.
- Updated `_NFC_SHARED_PRELOAD` to follow the per-lane assignment pattern more closely: select the hook gate, validate/commit the pending spool, apply the local Happy Hare gate map, and directly set the Spoolman gate assignment with `MMU_SPOOLMAN SPOOLID=<n> GATE=<n> QUIET=1`.
- Shared preload now mirrors the per-lane auto-created spool path by running `MMU_SPOOLMAN REFRESH=1 QUIET=1` only when the pending shared-reader spool was newly created, before applying the gate map assignment.
- Removed the obsolete duplicated shared-reader "already assigned" macro branch and the broad gate-map refresh from the shared preload success path.
- `_NFC_SHARED_PRELOAD` now prefers Happy Hare's hook-provided `GATE=<n>` and selects that gate before preload validation, preventing a previously selected lane from receiving the shared reader's resolved spool.
- Fixed bypass unresolved-tag feedback replaying across the shared reader's missed-resolution retries. The unresolved red LED effect now starts only once for an unresolved tag sequence, so bypass mode no longer appears to flash 6 times from three repeated attempts.
- Changed `mmu_RFID_unresolved` to exactly 2 red flashes (`strobe 2 2`) and updated shared-reader config comments, installer text, and docs to match.
- Kept bypass-ready confirmation bounded at 2 seconds and ensured scheduled LED timers stop the exact effect that was started.
- Added configurable shared-reader LED stop durations (`read_effect_duration`, `bypass_read_effect_duration`, `ready_effect_duration`, `bypass_ready_effect_duration`, and `unresolved_effect_duration`) next to their related effect names in `nfc_reader_shared.cfg`.
- Expanded `nfc_reader.cfg` scan-jog comments to document polling lane readers, `NFC JOG_SCAN=1` hook-triggered lane readers, shared-reader preload mode, and the hybrid lane-reader plus shared-reader bypass setup.
- Updated scan-jog's `_NFC_GATE_CLEAR_CACHE` preflight to call `MMU_SPOOLMAN GATE=<n> QUIET=1` before clearing the local HH gate spool ID, so stale Spoolman edit-dialog gate/location assignments are removed before NFC applies the newly read spool.

### Tests

- Added regression coverage proving repeated unresolved shared-reader events only start the red unresolved LED effect once.
- Added regression coverage for custom shared-reader LED effect durations.
- Added static coverage for the scan-jog preflight Spoolman gate unset.

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
