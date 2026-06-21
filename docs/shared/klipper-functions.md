# Commands & Macros

[← README](../../Readme.md) | [Configuration](configuration.md) | [Messages →](message_definition.md)

This is the day-to-day reference for operating the NFC gate reader from the Fluidd/Mainsail console.

For shared reader console and `nfc_reader.log` messages, see [Message Definitions](message_definition.md).

---

## Quick Reference

| Command | What it does |
|---|---|
| `NFC_HELP` | Show global NFC command help; add `ADVANCED=1 CALLBACKS=1 LOW_LEVEL=1` for the full command set |
| `NFC_STATUS` | Show current state of every configured gate (includes shared reader if configured) |
| `NFC_DOCTOR` | Check common config/setup problems: disabled lanes, Spoolman, shared-reader hook, and static warnings |
| `NFC_REGISTER UID=TAG_UID SPOOL_ID=SPOOL_ID` | Assign a known NFC UID to an existing Spoolman spool and clear NFC's lookup cache |
| `NFC_LED_TEST ALL=1` | Test lane tag-read LEDs across every enabled per-lane reader with a short chase delay |
| `NFC GATE=<#> STATUS` | Show one gate's state |
| `NFC GATE=<#> INIT=1` | Initialize (or re-initialize) the NFC reader |
| `NFC GATE=<#> SCAN=1` | One raw read — shows UID, no Spoolman lookup |
| `NFC GATE=<#> LED_TEST=1` | Test the configured lane tag-read LED effect on one gate |
| `NFC GATE=<#> JOG_SCAN=1` | Start scan-jog sequence (same as automatic pre-load trigger) |
| `NFC GATE=<#> POLL=1` | Full cycle: read → Spoolman → Happy Hare |
| `NFC GATE=<#> APPLY=1` | Force cached spool assignment to Happy Hare |
| `NFC GATE=<#> CLEAR_CACHE=1` | Clear cached spool, force fresh Spoolman lookup |
| `NFC GATE=<#> READ=1` | Start background polling |
| `NFC GATE=<#> READ=0` | Stop background polling |
| `NFC GATE=<#> HELP` | Show available commands for one per-lane reader |
| `NFC_HH_SYNC_CACHE` | Re-seed all lane caches from the current Happy Hare gate map |
| `NFC GATE=<#> HH_SYNC=1 SPOOL_ID=<n>` | Seed one lane's cache directly (called by `NFC_HH_SYNC_CACHE`) |
| `NFC_SHARED READ=1` | Start shared polling manually; rejected while printing |
| `NFC_SHARED READ=0` | Stop shared polling (keeps any pending spool) |
| `NFC_SHARED STATUS=1` | Show detailed shared reader state |
| `NFC_SHARED SUMMARY=1` | Show one-line shared reader state and next action |
| `NFC_SHARED HELP=1` | Show shared reader command help |
| `NFC_SHARED CANCEL=1` | Cancel a staged shared spool and stop polling |
| `NFC_SHARED REPLACE=1` | Discard a staged spool and scan another |
| `NFC_SHARED RESET=1` | Clear shared state, restore HH LED control, and restart polling |
| `NFC_SHARED LED_TEST=1` | Test configured shared tag-read LED effect |
| <span style="color:orange">━━━ **Advanced Shared Reader** — internal/recovery commands, not low-level PN532 debug ━━━</span> | |
| `NFC_SHARED CLEAR=1` | Clear pending spool, stop polling, reset shared state |
| `NFC_SHARED PRELOAD_CHECK=1` | Approve the pending shared-reader spool for the HH preload hook |
| `NFC_SHARED PRELOAD_COMMIT=1 SPOOL_ID=<id>` | Clear pending state after the hook bridge accepts the staged spool |
| `NFC_SHARED PRELOAD_CLEAR_ASSIGNED=1 SPOOL_ID=<id>` | Legacy/recovery command to clear shared pending state when HH already has this spool assigned |
| `NFC_SHARED POLL=1` | Force one full read/resolve cycle on the shared reader; skips while printing |
| `NFC_SHARED SCAN=1` | Raw hardware scan only — no Spoolman/HH dispatch; skips while printing |
| `NFC_SHARED INIT=1` | Re-run NFC reader init on the shared reader; resumes startup polling if enabled |
| `NFC_SHARED CLEAR_CACHE=1` | Clear tag cache on the shared reader (keeps pending spool) |
| <span style="color:red">━━━ **Low-Level Debug** — requires `low_level_debug: True` — bypasses normal state machine ━━━</span> | |
| `NFC GATE=<#> STEP=WAKEUP` | Write `0x00` to nudge PN532 out of power-down |
| `NFC GATE=<#> STEP=READY` | Read STATUS byte — expects `01` (ready) |
| `NFC GATE=<#> STEP=FIRMWARE_WRITE` | Send `GetFirmwareVersion` command frame |
| `NFC GATE=<#> STEP=FIRMWARE_ACK` | Read ACK — expects `01 00 00 FF 00 FF 00` |
| `NFC GATE=<#> STEP=FIRMWARE_READY` | Poll STATUS until `01` |
| `NFC GATE=<#> STEP=FIRMWARE_RESPONSE` | Read and parse firmware response |
| `NFC GATE=<#> STEP=FIRMWARE_ACK_DIRECT DELAY=0.050` | Write firmware command and read ACK with configurable delay (timing probe) |
| `NFC GATE=<#> STEP=SAM_WRITE` | Send `SAMConfiguration` command frame |
| `NFC GATE=<#> STEP=SAM_ACK` | Read ACK |
| `NFC GATE=<#> STEP=SAM_READY` | Poll STATUS until `01` |
| `NFC GATE=<#> STEP=SAM_RESPONSE` | Read and parse SAM response |
| `NFC GATE=<#> STEP=PASSIVE_WRITE` | Send `InListPassiveTarget` command frame |
| `NFC GATE=<#> STEP=PASSIVE_ACK` | Read ACK |
| `NFC GATE=<#> STEP=PASSIVE_READY` | Poll STATUS until `01` |
| `NFC GATE=<#> STEP=PASSIVE_RESPONSE LEN=30` | Read raw tag-detect response |
| `NFC GATE=<#> READY_READ=1` | Read PN532 STATUS byte (`01`=ready, `00`=busy) |
| `NFC GATE=<#> ACK_READ=1 LEN=7` | Read STATUS then ACK frame |
| `NFC GATE=<#> RAW_READ=1 LEN=<n>` | Read exactly N raw bytes |
| `NFC GATE=<#> RAW_WRITE=<hex>` | Write raw bytes (no framing added) |
| `NFC GATE=<#> RAW_CMD=<hex>` | Build and send a complete PN532 command frame |

---

## Help Commands

Use `NFC_HELP` the same way you use Happy Hare's `MMU_HELP`: run it by itself
for the everyday command list, or add flags to include less-common sections.

```gcode
NFC_HELP
NFC_HELP ADVANCED=1 CALLBACKS=1 LOW_LEVEL=1
```

`NFC_HELP` shows shared-reader commands only when `[nfc_gate shared]` is
configured. Per-reader help is still available for focused command lists:

```gcode
NFC GATE=0 HELP
NFC_SHARED HELP=1
```

Klipper requires `=1` on shared action flags, so use `NFC_SHARED CANCEL=1`,
not `NFC_SHARED CANCEL`.

---

## Startup — Happy Hare Cache Seed

When Klipper connects, NFC_Manager runs this sequence for each lane automatically:

```
klippy:connect  →  _handle_connect()  →  schedule _delayed_init() (2 s)

_delayed_init()
  1. Initialise NFC reader
  2. Read Happy Hare gate map  →  seed this lane's local cache
  3. Start background polling  (if startup_polling: 1)
```

**Step 2 is the key one.** NFC_Manager calls `mmu.get_status()` directly to read `gate_spool_id` for this gate. The result is stored as a one-shot seed. On the very first poll:

- Tag resolves to the **same spool** as the HH seed → cache updated silently, **no dispatch** (HH already knows)
- Tag resolves to a **different spool** → `_NFC_SPOOL_CHANGED` dispatched normally (spool was swapped while Klipper was down)
- No tag present → seed kept until the first successful tag read

The seed is always cleared after the first `CHANGED` event — it fires at most once per startup per lane.

**Console output at startup:**
```
[OK] NFC[lane0]: ready.  HH seed: spool_id=42  Startup polling is enabled; first poll in 0.0s.
[OK] NFC[lane1]: ready.  HH reports gate empty  Run NFC GATE=1 READ=1 to start polling.
```

**If Happy Hare wasn't ready** when the NFC init ran (rare — both init at `klippy:connect`), the seed step is skipped and all first-poll reads dispatch normally. Run `NFC_HH_SYNC_CACHE` to manually re-seed.

---

## `NFC_HH_SYNC_CACHE`

Re-seeds all NFC lane caches from the current Happy Hare gate map. Use this any time you want to re-align NFC state with HH without restarting Klipper.

```gcode
NFC_HH_SYNC_CACHE
```

The macro reads `printer.mmu.gate_spool_id` for each gate and calls `NFC GATE=N HH_SYNC=1 SPOOL_ID=<n>` per lane. The Python side receives the spool_id and sets the seed. On the next poll for each lane, if the physical tag matches the seed, the dispatch is suppressed.

**When to use:**
- Happy Hare wasn't fully initialised when the NFC reader init ran and the startup seed was skipped
- You manually changed the HH gate map and want NFC to treat the current state as baseline
- After loading filament manually (without NFC) and you don't want a spurious CHANGED on the next poll

---

## Normal Operation

### `NFC_STATUS`

Shows the NFC_Manager's last known state for every gate. This is an in-memory snapshot — it is not a live I2C read.

```gcode
NFC_STATUS
```

Example output:
```
NFC gate status  (5 gates configured):
  Gate 0:  empty   [not polling]  [HH: empty]
  Gate 1:  empty   [polling]  [HH: empty]
  Gate 2:  tag 6317B1A1  metadata material=PLA color=FFFFFF spool_identity=bambu_BB9B88E7ED544C1B8FAA92972900E77B   [not polling]  [HH: found/no spool]
  Gate 3:  tag 04C19F92  metadata material=PLA color=FF5500 spool_identity=None   [not polling]  [HH: found/no spool]
  Gate 4:  spool 43     UID 04456192D32A81   [polling]  [HH: spool 43]
```

For metadata-direct tags, `spool_identity` is the parser's optional
spool-level identity. It is distinct from the physical NFC tag UID. Bambu
factory spools derive it from `tray_uid` as `bambu_<tray_uid>`, so the two
physical side tags on the same spool can have different UIDs while sharing the
same `spool_identity`. Tags without a parser-supplied spool identity display
`spool_identity=None`.

---

### `NFC_DOCTOR`

Runs a no-motion setup check. It does not scan tags or move filament.

```gcode
NFC_DOCTOR
```

It reports enabled/disabled lane readers, shared-reader state, Spoolman
availability, the shared-reader preload hook, and static config warnings such as
`bambu_reads: True` without `tag_parsing: True`.

---

### `NFC_REGISTER UID=TAG_UID SPOOL_ID=SPOOL_ID`

Writes a known tag UID to an existing Spoolman spool using the configured
`spoolman_rfid_key` field. This is for cases where you already know the UID
from a phone scan, a raw NFC scan, or an unresolved-tag console message and you
do not want to open the browser-based Spoolman UI.

```gcode
NFC_REGISTER UID=04A1B2C3D4 SPOOL_ID=123
```

The command validates that Spoolman is enabled, confirms the spool exists,
writes the UID to Spoolman, and clears NFC's UID lookup cache. It does not scan
every spool for duplicate UIDs and does not call `MMU_SPOOLMAN REFRESH` from
inside the command path. Happy Hare and Fluidd see the updated field through
their normal Spoolman refresh/polling behavior.

Expected success:
```
[OK] NFC: UID 04A1B2C3D4 assigned to Spoolman spool 123; NFC cache cleared. Happy Hare/Fluidd will refresh on their normal Spoolman polling cycle.
```

---

### `NFC GATE=<#> STATUS`

Same as `NFC_STATUS` but for a single lane.

```gcode
NFC GATE=4 STATUS
```

---

### `NFC GATE=<#> INIT=1`

Runs the configured NFC reader's initialization sequence.

```gcode
NFC GATE=0 INIT=1
```

**When to use:** After first wiring, after a failed startup, or after flashing lane MCU firmware.

Expected success:
```
NFC[lane0]: reader OK
```

If this fails, see [Troubleshooting](../i2c-nfc/troubleshooting.md).

---

### `NFC_LED_TEST ALL=1`

Tests the configured per-lane tag-read LED effect on every enabled lane reader,
with a short delay between gates so the effect chases across the MMU. This uses
`scan_tag_read_effect` from `nfc_reader.cfg` and calls Happy Hare with the
base effect plus `GATE=<gate>`. Each cycle starts the effect, waits about two
seconds, then starts the next cycle without restoring gate status in between.
After the final cycle, NFC asks Happy Hare to repaint the gate map so the
active test effects stop. If `CYCLES` is omitted, the default is `2`.

```gcode
NFC_LED_TEST ALL=1
```

The default chase delay is `0.20` seconds and the default cycle count is `2`.
Override either value if needed:

```gcode
NFC_LED_TEST ALL=1 DELAY=0.10 CYCLES=3
```

Expected success:
```
[OK] NFC: lane LED chase test scheduled for gates 0, 1, 2, 3, 4 (delay=0.20s cycles=2)
```

---

### `NFC GATE=<#> SCAN=1`

Reads the PN532 hardware once and prints the raw tag UID. Does not look up Spoolman and does not update Happy Hare.

```gcode
NFC GATE=0 SCAN=1
```

**When to use:**
- Getting a UID to register in Spoolman
- Confirming a reader can physically see a tag
- Checking whether a wiring or mode problem is fixed

---

### `NFC GATE=<#> LED_TEST=1`

Tests the configured tag-read LED effect for one lane reader. If `CYCLES` is
omitted, the default is `2`.

```gcode
NFC GATE=0 LED_TEST=1 CYCLES=2
```

With the default `scan_tag_read_effect: mmu_RFID_read`, NFC starts the effect
once per cycle, waits about two seconds, then starts the next cycle. After the
last cycle it asks Happy Hare to repaint the gate map so the active test effect
stops.

```gcode
MMU_SET_LED GATE=0 EXIT_EFFECT=mmu_RFID_read
MMU_GATE_MAP QUIET=1
```

---

### `NFC GATE=<#> JOG_SCAN=1`

Starts the scan-and-jog sequence on demand, identical to the automatic pre-load trigger that fires when Happy Hare parks filament at the gate.

```gcode
NFC GATE=0 JOG_SCAN=1
```

**What it does:** Selects the gate, then jogs the filament forward until the NFC tag is read or the scan limit is reached. When the tag is found it rewinds toward the parked position, leaves `scan_rewind_buffer_mm` for Happy Hare's final gate parking step, and runs `_MMU_STEP_UNLOAD_GATE`. If `scan_jog_max` is set, that distance is the scan limit; otherwise the lane's Happy Hare Bowden calibration length is used. When the limit is reached without a read, NFC follows the same rewind-and-park path and exits scan mode.

Scan-jog supports two motion modes:

| Mode | Config | Behavior |
|---|---|---|
| Continuous | `scan_motion_mode: continuous` | **Default.** Queues each forward search chunk through Happy Hare's MMU toolhead and polls NFC every `scan_continuous_poll_interval` while that chunk is estimated to be moving. If a tag is found during motion, the current chunk is allowed to finish before the existing 0.1 second read-light hold, rewind, and completion logic run. |
| Stopped | `scan_motion_mode: stopped` | Divides each `scan_jog_mm` chunk into three blocking `MMU_TEST_MOVE` substeps, then reads at stopped spool positions. `scan_reads_per_position` and `scan_poll_interval` control the stopped-position reads. More reliable for marginal reader or tag alignment at the cost of scan speed. |

Default continuous scan settings:

```ini
[nfc_gate]
scan_motion_mode: continuous
scan_continuous_step_mm: 50.0
scan_continuous_speed: 200.0
scan_continuous_accel: 2000.0
scan_continuous_poll_interval: 0.05
#scan_continuous_overshoot_backup_mm: 25.0
```

With those values, a 50 mm forward chunk takes about `0.35s`. NFC polls every
`0.05s` during that estimated motion window, then queues the next chunk if no
tag has been found. Effective scan advance is roughly `143mm/s` before NFC read
time is included.

If a continuous UID hit occurs during motion, NFC waits for the current chunk to
finish and checks Spoolman first. If the UID resolves, scan-jog finishes without
a rich read. If the UID does not resolve and rich parsing is enabled, NFC backs
up by `scan_continuous_overshoot_backup_mm` before running rich tag parsing and
the normal `scan_decode_retry_mm` left/right retry sweep. By default, the backup
is 50% of `scan_continuous_step_mm`.

Scan-jog always clears the Happy Hare gate cache and runs the pre-scan
`MMU_SPOOLMAN SYNC=1` before moving filament. When launched from a Happy Hare
hook, those prep calls are deferred to the scan timer so the hook can return
before NFC calls back into Happy Hare.


**Happy Hare post-preload hook setup:**

The [igiannakas IG-dev branch](https://github.com/igiannakas/Happy-Hare/tree/IG-dev) of Happy Hare adds `variable_user_post_preload_extension` in `config/base/mmu_macro_vars.cfg`. Set it to drive NFC scan-jog automatically after each `MMU_PRELOAD`:

```ini
[gcode_macro _MMU_SEQUENCE_VARS]
description: Happy Hare sequence macro configuration variables
gcode: # Leave empty
variable_user_post_preload_extension: '_NFC_SCAN_JOG_PRELOAD'
```

Happy Hare appends `GATE=<n>` automatically after a successful preload. `_NFC_SCAN_JOG_PRELOAD` starts scan-jog with:

```gcode
NFC GATE=<n> JOG_SCAN=1
```

NFC starts the configured scan-jog LED effect from the Python scan timer before motion begins.

Recommended NFC config when using the hook — disables gate-status polling so HH is the sole trigger:

```ini
[nfc_gate]
startup_polling: 0
scan_enabled:    False
```

**Preconditions** (same as the automatic path — the command checks all of these and reports a plain-language error if any fail):

| Check | What it guards |
|---|---|
| Reader not in failed state | Reader must have initialised successfully |
| No active print | Scan cannot move filament during a print |
| Happy Hare `action == idle` | HH must not be loading, unloading, or homing |
| No other gate currently scanning | Only one gate may hold the MMU at a time |

**When to use:**
- Filament was loaded manually and the automatic trigger didn't fire (e.g. `scan_enabled: False`, or the 0→1 edge was missed)
- Retrying a scan after a failed automatic attempt
- Testing scan-jog behaviour without physically reloading filament

Expected success output:
```
NFC[lane0]: scan-jog started for gate 0 (step=50mm  max=600mm  interval=2.0s)
NFC[0] - moved 50.0mm  total 50.0mm / 600.0mm
NFC[0]: rewinding 50.0mm
```

Continuous-mode success output uses the same start/rewind messages but includes
continuous move messages while searching:

```
NFC[lane0]: continuous scan-jog started for gate 0
NFC[Lane0]: continuous Direct Move 50.0mm  scan position 50.0 / 600.0mm
NFC[Lane0]: tag found
NFC[Lane0]: rewinding 20.0mm (scan=50.0mm buffer=30.0mm)
```

If a precondition fails:
```
NFC[lane0]: Happy Hare is busy (action=loading) — wait for idle before starting scan-jog
```

---

### `NFC GATE=<#> POLL=1`

Runs one complete cycle of the NFC manager pipeline:

1. NFC reader reads the UID
2. NFC_Manager checks if the UID is new, the same, or absent
3. If new: SpoolmanClient looks up the spool ID
4. Gate state updates
5. If state changed: dispatches `_NFC_SPOOL_CHANGED`, `_NFC_SPOOL_REMOVED`, or `_NFC_TAG_NO_SPOOL`

```gcode
NFC GATE=0 POLL=1
```

**When to use:** Testing the complete pipeline end-to-end, or verifying a specific tag is registered correctly.

Expected output (registered tag):
```
NFC gate 0: spool 42 detected (UID 04AABBCCDD). Sending to Happy Hare.
```

Expected output (unregistered tag):
```
[ERROR] NFC gate 0: tag UID 04AABBCCDD is not registered in Spoolman.
Open the spool record in Spoolman, set the 'rfid_tag' extra field to: 04AABBCCDD
```

---

### `NFC GATE=<#> APPLY=1`

Forces the lane's cached spool assignment through to Happy Hare immediately. Does not read the NFC reader, does not query Spoolman — it just dispatches:

```gcode
_NFC_SPOOL_CHANGED GATE=<gate> SPOOL_ID=<cached_id> UID=<cached_uid>
```

```gcode
NFC GATE=0 APPLY=1
```

**When to use:** When a poll or polling cycle already resolved a spool, but Happy Hare didn't update (e.g. it was in a locked state during the scan). If you get "no cached spool_id", run `POLL=1` first.

---

### `NFC GATE=<#> CLEAR_CACHE=1`

Clears the lane's cached spool ID and forces a fresh Spoolman lookup on the next tag read.

```gcode
NFC GATE=0 CLEAR_CACHE=1
```

Clears:
1. The lane's cached spool ID
2. The SpoolmanClient UID cache
3. The reader driver's in-memory current-card cache

On the next poll, the full `(uid, spool_id)` combination is re-evaluated:

- **Same UID, same spool in Spoolman** → cache refreshed silently, no dispatch
- **Same UID, different spool in Spoolman** → `_NFC_SPOOL_CHANGED` dispatched with the new spool

This is the correct way to force a re-read after editing a spool's UID registration in Spoolman.

`CLEAR=1` is accepted as a shorthand.

---

### `NFC GATE=<#> HH_SYNC=1 SPOOL_ID=<n>`

Seeds this lane's cache with a spool_id from Happy Hare's gate map. On the next poll, if the physical tag resolves to that spool_id, the dispatch is suppressed (HH already knows).

```gcode
NFC GATE=0 HH_SYNC=1 SPOOL_ID=42
```

This command is normally called automatically by the `NFC_HH_SYNC_CACHE` macro — you won't need it directly in normal operation. Use `NFC_HH_SYNC_CACHE` to sync all lanes at once.

---

### `NFC GATE=<#> READ=1` / `READ=0`

Starts or stops background timer polling on one lane.

```gcode
NFC GATE=0 READ=1    ; start polling
NFC GATE=0 READ=0    ; stop polling
```

While polling is running, the lane runs `POLL=1` automatically every `poll_interval` seconds (default: 30). Macro dispatches happen automatically when gate state changes.

---

## Background Polling Setup

For production use, you want all lanes polling automatically. There are two ways to start polling:

**Manually after boot** (default — useful during setup):
```gcode
NFC GATE=0 READ=1
NFC GATE=1 READ=1
NFC GATE=2 READ=1
NFC GATE=3 READ=1
```

**Automatically on boot** (for set-and-forget operation): Add `startup_polling: 1` to each lane in `nfc_reader_hw.cfg`. The shipped hardware config staggers startup delays by 0.5 seconds so all readers don't poll at the same moment:

```ini
[nfc_gate lane0]
startup_polling:    1
startup_poll_delay: 0.0

[nfc_gate lane1]
startup_polling:    1
startup_poll_delay: 0.5

[nfc_gate lane2]
startup_polling:    1
startup_poll_delay: 1.0

[nfc_gate lane3]
startup_polling:    1
startup_poll_delay: 1.5
```

---

## Event Macros

These macros live in `nfc_macros.cfg` and are called automatically by NFC_Manager when gate state changes. You don't call these manually during normal operation, but you can call them directly to test the Happy Hare handoff without hardware.

### `_NFC_SPOOL_CHANGED`

Fires when a tag resolves to a spool and the gate state changed. Two dispatch paths depending on tag type and Spoolman availability.

**Spoolman path** — tag UID matched a Spoolman record:
```gcode
_NFC_SPOOL_CHANGED GATE=<gate> SPOOL_ID=<id> UID=<uid> [AUTO_CREATED=1] [SCAN_FINISH=1]
```

**Metadata path** — tag carries embedded filament data (Spoolman disabled or no match):
```gcode
_NFC_SPOOL_CHANGED GATE=<gate> UID=<uid> [NAME=<str>] [MATERIAL=<str>] [COLOR=<hex>] [TEMP=<int>] [SCAN_FINISH=1]
```

Parameters:
- `GATE` — Happy Hare gate number (integer, matches `mmu_gate` in config)
- `SPOOL_ID` — Spoolman spool ID (integer); present on Spoolman path only
- `UID` — NFC tag UID (hex string); always present
- `AUTO_CREATED` — `1` when `spoolman_auto_create` just created the spool record; absent otherwise
- `SCAN_FINISH` — `1` when the event came from scan-jog after rewind; accepted as a compatibility marker by the default macros
- `NAME` — display name from `material_detail` or `material`, prefixed with brand/vendor/tag format when present; metadata path only
- `MATERIAL` — filament material string from tag metadata (e.g. `PLA`, `ABS`); metadata path only
- `COLOR` — color hex string from tag metadata (e.g. `FF0000`); metadata path only
- `TEMP` — recommended extruder temperature (integer °C) from tag `min_temp` field; metadata path only

Default behavior:
```gcode
{% if params.SPOOL_ID is defined %}
    {% if auto_created %}
    MMU_SPOOLMAN REFRESH=1 QUIET=1
    {% endif %}
    MMU_GATE_MAP GATE={gate} SPOOLID={spool_id} AVAILABLE=1 SYNC=1 QUIET=1
{% else %}
    MMU_GATE_MAP GATE={gate} [NAME=..] [MATERIAL=..] [COLOR=..] [TEMP=..] AVAILABLE=1 QUIET=1
{% endif %}
MMU_GATE_MAP GATE={gate} APPLY=1 QUIET=1
```

On the Spoolman path, `AVAILABLE=1` marks the gate as loaded and `SYNC=1` lets Happy Hare push the update to Spoolman. When `AUTO_CREATED=1`, `MMU_SPOOLMAN REFRESH=1 QUIET=1` runs first so Happy Hare's Spoolman cache includes the new spool before the gate assignment is sent. On the metadata path, whatever fields are present on the tag are forwarded to `MMU_GATE_MAP`. `APPLY=1` applies the updated map immediately on both paths.

---

### `_NFC_SPOOL_REMOVED`

Fires after a previously-detected spool is absent for `absent_threshold` consecutive polls.

```gcode
_NFC_SPOOL_REMOVED GATE=<gate>
```

Parameters:
- `GATE` — Happy Hare gate number

Default behavior:
```gcode
MMU_GATE_MAP GATE={gate} SPOOLID=-1 AVAILABLE=0 SYNC=1 QUIET=1
MMU_GATE_MAP GATE={gate} APPLY=1 QUIET=1
```

Clears the gate in Happy Hare's gate map. `AVAILABLE=0` marks the gate as empty. `APPLY=1` applies the change immediately.

The macro also checks `printer.mmu.action` — if the MMU is mid-load, unload, or homing, the removal is silently ignored to avoid clearing the gate while filament is actively moving.

---

### `_NFC_TAG_NO_SPOOL`

Fires when a tag UID is detected but no matching spool is found in Spoolman.

```gcode
_NFC_TAG_NO_SPOOL GATE=<gate> UID=<uid> [SCAN_FINISH=1]
```

Parameters:
- `GATE` — Happy Hare gate number
- `UID` — the unrecognized tag UID
- `SCAN_FINISH` — `1` when the event came from scan-jog after rewind; accepted as a compatibility marker by the default macro

Default behavior: prints a message to the console with the UID and instructions to register it, clears stale visible filament fields, and keeps the gate loaded/available.

```gcode
MMU_GATE_MAP GATE={gate} SPOOLID=-1 NAME=Unknown MATERIAL=Unknown COLOR=FFFFFF TEMP=0 AVAILABLE=1 SYNC=1 QUIET=1
MMU_GATE_MAP GATE={gate} APPLY=1 QUIET=1
```

---

### `_NFC_SCAN_UNRESOLVED`

Fires after scan-jog rewinds when no tag/spool could be resolved. It clears stale visible Happy Hare filament metadata while keeping the gate loaded/available.

```gcode
_NFC_SCAN_UNRESOLVED GATE=<gate>
```

Default behavior:
```gcode
MMU_GATE_MAP GATE={gate} SPOOLID=-1 NAME=Unknown MATERIAL=Unknown COLOR=FFFFFF TEMP=0 AVAILABLE=1 SYNC=1 QUIET=1
MMU_GATE_MAP GATE={gate} APPLY=1 QUIET=1
```

---

## Testing the Happy Hare Handoff Without Hardware

You can test whether the macro-to-Happy-Hare pipeline works by calling the event macros directly:

```gcode
; Spoolman path
_NFC_SPOOL_CHANGED GATE=0 SPOOL_ID=42 UID=04AABBCCDD

; Metadata path
_NFC_SPOOL_CHANGED GATE=0 UID=04AABBCCDD MATERIAL=PLA COLOR=FF0000 TEMP=215

_NFC_SPOOL_REMOVED GATE=0
_NFC_TAG_NO_SPOOL GATE=0 UID=04AABBCCDD
_NFC_SCAN_UNRESOLVED GATE=0
```

If Happy Hare updates correctly, the pipeline from macro inward is working. If it doesn't, check:
- The macro body in `nfc_macros.cfg`
- Whether `MMU_GATE_MAP GATE=... SPOOLID=... AVAILABLE=1 SYNC=1 QUIET=1` is the right syntax for your Happy Hare version
- Whether Happy Hare is in a state that accepts gate map changes (e.g. not mid-print with locks active)

---

## Customizing the Macros

The event macros are in `~/printer_data/config/nfc/nfc_macros.cfg`. Edit them to match your Happy Hare version.

For lane readers, the Happy Hare-facing gate assignment commands live in
`nfc_macros.cfg` so they remain visible and editable without touching Python.
The shared reader stages `MMU_GATE_MAP NEXT_SPOOLID=<id>` in Python when the
tag resolves. The hook macro is deliberately narrow: Python validates pending
state with `NFC_SHARED PRELOAD_CHECK=1`, and `NFC_SHARED PRELOAD_COMMIT=1`
clears pending state only after the same spool ID is approved.

### Happy Hare commands used by the defaults

| Command | Effect |
|---|---|
| `MMU_GATE_MAP GATE=<n> SPOOLID=<id> AVAILABLE=1 SYNC=1 QUIET=1` | Assign a spool to a gate, mark it available, and sync to Spoolman |
| `MMU_GATE_MAP GATE=<n> APPLY=1 QUIET=1` | Apply the current gate map immediately |
| `MMU_GATE_MAP GATE=<n> SPOOLID=-1 AVAILABLE=0 SYNC=1 QUIET=1` | Clear a gate, mark it empty, and sync to Spoolman |
| `MMU_GATE_MAP GATE=<n> [NAME=..] [MATERIAL=..] [COLOR=..] [TEMP=..] AVAILABLE=1 QUIET=1` | Assign metadata (no Spoolman spool ID) — metadata path only |
| `MMU_SPOOLMAN SPOOLID=<id> GATE=<n> QUIET=1` | Directly set a Spoolman spool's printer/gate fields for the edit-spool dialog |
| `MMU_SPOOLMAN REFRESH=1 QUIET=1` | Force Happy Hare to re-sync its Spoolman cache — called before gate assignment when a new spool was auto-created |

The default lane macros are designed for Happy Hare with `spoolman_support:
push`. `SYNC=1` tells Happy Hare to push the local gate map change to Spoolman.
If your Happy Hare version uses different lane command names or parameters,
update the macro body. Shared-reader preloading uses Happy Hare's public GCode
command surface.

---

## Shared Reader

The shared reader is a single NFC reader mounted inside the MMU body. It defaults to PN532 and can use PN7160 with `reader_type: pn7160`. Tap a spool tag on it before loading; NFC stages the spool ID for the next pregate preload automatically. See [Shared Reader](shared-reader.md) for the full setup and workflow guide.

### Normal flow

1. Shared reader is polling. With `startup_polling: 1` it starts at boot, reads at `scan_poll_interval`, and pauses automatically when printing starts, resuming when printing completes.
2. Tap your spool tag on the shared reader — NFC resolves the spool in Spoolman and stores it as pending. LED effect fires if configured.
3. Drop the spool into an MMU lane and push the filament tip into the pregate sensor.
4. Happy Hare detects the pregate load and fires `variable_user_post_preload_extension` → `_NFC_SHARED_PRELOAD`.
5. The macro reads the pending spool from `printer['nfc_gate shared']`, runs `NFC_SHARED PRELOAD_CHECK=1 EXPECTED_SPOOL_ID=<id>`, then `NFC_SHARED PRELOAD_COMMIT=1 SPOOL_ID=<id>`.
6. Pending state is cleared only after the commit command matches the approved spool. Polling restarts automatically for the next spool.

### Commands

**`NFC_SHARED STATUS=1`** — Show detailed shared reader state: summary, polling
flags, read deadline, pending spool/UID, auto-created flag, miss counter,
strict mode, LED effect, print-safety block, last action, next action, and
last error.

```
shared: idle
shared: polling, no tag pending
shared: pending spool 42  uid=ABCDEF  expires in 87s
shared: expired  spool 42  uid=ABCDEF
shared: error  tag uid=ABCDEF not in Spoolman
```

**`NFC_SHARED READ=1`** — Start shared polling. Rejected while printing. If a pending spool already exists, it refuses to overwrite it and tells you to use `NFC_SHARED REPLACE=1` or `NFC_SHARED CANCEL=1`. Polling auto-stops after `shared_read_timeout` seconds if no tag resolves. Not needed when `startup_polling: 1`.

**`NFC_SHARED READ=0`** — Stop shared polling without clearing any pending spool.

**`NFC_SHARED SUMMARY=1`** — Show one compact line with current shared state and the next suggested user action.

**`NFC_SHARED HELP=1`** — Show the shared reader command list.

**`NFC_SHARED CANCEL=1`** — User-friendly cancel command. Clears any staged spool and stops polling.

**`NFC_SHARED REPLACE=1`** — Discard the staged spool and start a new timed scan. Use this when you tapped the wrong spool tag.

**`NFC_SHARED RESET=1`** — Recovery command that clears pending/read/preload state, restores HH LED control with `MMU_GATE_MAP QUIET=1`, and restarts shared polling. Rejected while printing or while the reader is failed.

**`NFC_SHARED LED_TEST=1`** — Play the configured `shared_tag_read_effect` without scanning a tag, using `read_effect_duration` as the HH duration. Use this during setup to confirm the HH LED effect exists and works.

### Advanced Shared Reader Commands

These are shared-reader control and recovery commands. They are not low-level
PN532 debug commands, and they do not require `low_level_debug: True`.

**`NFC_SHARED CLEAR=1`** — Clear pending state, stop polling, reset the reader. Use this to cancel a staged spool before the preload fires.

**`NFC_SHARED PRELOAD_CHECK=1`** — Called automatically by `variable_user_post_preload_extension`. Approves the shared-reader preload if a valid pending spool exists. Skips only while printing. If no spool is staged, a console message advises tapping a tag first or using `MMU_PRELOAD`. With `force_spool_id: true`, the advisory uses the `[ERROR]` prefix without raising a Klipper command error.

The default macro reads the pending spool from `printer['nfc_gate shared']`,
runs `PRELOAD_CHECK` with `EXPECTED_SPOOL_ID=<id>`, and commits the same spool
ID only after Python approves it. The Python side clears pending state only at
commit.

**`NFC_SHARED PRELOAD_CLEAR_ASSIGNED=1 SPOOL_ID=<id> GATE=<gate>`** — Recovery
command that clears shared pending state when Happy Hare already reports the
pending spool in its gate map.

**`NFC_SHARED POLL=1`** — Force one full read/resolve cycle. Skips while printing and reports that no poll was run.

**`NFC_SHARED SCAN=1`** — Raw hardware scan only — shows UID, no Spoolman lookup or dispatch. Skips while printing.

**`NFC_SHARED INIT=1`** — Re-run NFC reader initialisation. Use after a wiring fault or reader failure. If `startup_polling: 1` is set, the printer is not printing, and no spool is pending, polling resumes automatically after a successful init.

**`NFC_SHARED CLEAR_CACHE=1`** — Clear the tag UID cache without clearing any pending spool state.

### Pending timeout

The pending timeout (`pending_spool_id_timeout` in Happy Hare's `mmu_parameters.cfg`)
starts when a tag resolves. If no preload fires within that window, the pending
spool expires automatically. With `startup_polling: 1`, polling resumes after
the expired spool is cleared. Tap again to queue a new spool.

### Unresolvable tags

If the reader sees a UID that Spoolman cannot resolve, it increments a miss counter. After `shared_missed_limit` consecutive misses (default 3) a console error appears advising the user to use `MMU_PRELOAD` to load without spool assignment, and LED feedback stops so Happy Hare can resume normal LED control. The counter resets on a successful read, `CLEAR=1`, `READ=1`, or `REPLACE=1`.

### Re-scanning

Once a tag resolves, polling stops. To scan a different spool issue
`NFC_SHARED REPLACE=1`. Plain `READ=1` will refuse to overwrite a pending spool
and will tell you to use `REPLACE=1` or `CANCEL=1`.

If an advanced/manual read path sees another valid tag while a spool is already
pending, NFC keeps the original pending spool, reports the newly read
UID/spool as ignored, and tells the user to run `NFC_SHARED REPLACE=1` if
replacement was intentional.

---

## Expert: Low-Level Debug Commands

These commands expose raw PN532 I2C bus access for bring-up debugging. They are hidden by default — enable with `low_level_debug: True` in `nfc_reader.cfg`.

> [!WARNING]
> Low-level commands bypass the normal state machine. Sending the wrong sequence can leave the PN532 in a state where normal polling fails until it is restarted. Use only during manual bring-up. Set `low_level_debug: False` before printing.

See [Expert: Low-Level I2C Debugging](expert-low-level-i2c-debugging.md) for the complete step-by-step bring-up sequence.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
