# Commands & Macros

[← README](../../Readme.md) | [Configuration](configuration.md) | [Messages →](message_definition.md)

This is the day-to-day reference for operating the NFC gate reader from the Fluidd/Mainsail console.

For shared reader console and `nfc_reader.log` messages, see [Message Definitions](message_definition.md).

---

## Quick Reference

| Command | What it does |
|---|---|
| `NFC_STATUS` | Show current state of every configured gate (includes shared reader if configured) |
| `NFC GATE=<n> STATUS=1` | Show one gate's state |
| `NFC GATE=<n> INIT=1` | Initialize (or re-initialize) the PN532 reader |
| `NFC GATE=<n> SCAN=1` | One raw read — shows UID, no Spoolman lookup |
| `NFC GATE=<n> JOG_SCAN=1` | Start scan-jog sequence (same as automatic pre-load trigger) |
| `NFC GATE=<n> POLL=1` | Full cycle: read → Spoolman → Happy Hare |
| `NFC GATE=<n> APPLY=1` | Force cached spool assignment to Happy Hare |
| `NFC GATE=<n> CLEAR_CACHE=1` | Clear cached spool, force fresh Spoolman lookup |
| `NFC GATE=<n> READ=1` | Start background polling |
| `NFC GATE=<n> READ=0` | Stop background polling |
| `NFC GATE=<n> HELP=1` | Show available commands |
| `NFC_HH_SYNC_CACHE` | Re-seed all lane caches from the current Happy Hare gate map |
| `NFC GATE=<n> HH_SYNC=1 SPOOL_ID=<n>` | Seed one lane's cache directly (called by `NFC_HH_SYNC_CACHE`) |
| `NFC_SHARED READ=1` | Start shared polling manually; rejected while printing |
| `NFC_SHARED READ=0` | Stop shared polling (keeps any pending spool) |
| `NFC_SHARED STATUS=1` | Show detailed shared reader state |
| `NFC_SHARED SUMMARY=1` | Show one-line shared reader state and next action |
| `NFC_SHARED HELP=1` | Show shared reader command help |
| `NFC_SHARED CANCEL=1` | Cancel a staged shared spool and stop polling |
| `NFC_SHARED REPLACE=1` | Discard a staged spool and scan another |
| `NFC_SHARED RETRY=1` | Retry shared preload staging after fixing HH/Spoolman issue |
| `NFC_SHARED LED_TEST=1` | Test configured shared tag-read LED effect |
| <span style="color:orange">━━━ **Advanced Shared Reader** — internal/recovery commands, not low-level PN532 debug ━━━</span> | |
| `NFC_SHARED CLEAR=1` | Clear pending spool, stop polling, reset shared state |
| `NFC_SHARED PRELOAD_CHECK=1` | Stage `NEXT_SPOOLID` if a valid pending spool exists (called from HH pre-load hook) |
| `NFC_SHARED POLL=1` | Force one full read/resolve cycle on the shared reader; skips while printing |
| `NFC_SHARED SCAN=1` | Raw hardware scan only — no Spoolman/HH dispatch; skips while printing |
| `NFC_SHARED INIT=1` | Re-run PN532 init on the shared reader; resumes startup polling if enabled |
| `NFC_SHARED CLEAR_CACHE=1` | Clear tag cache on the shared reader (keeps pending spool) |
| <span style="color:red">━━━ **Low-Level Debug** — requires `low_level_debug: True` — bypasses normal state machine ━━━</span> | |
| `NFC GATE=<n> STEP=WAKEUP` | Write `0x00` to nudge PN532 out of power-down |
| `NFC GATE=<n> STEP=READY` | Read STATUS byte — expects `01` (ready) |
| `NFC GATE=<n> STEP=FIRMWARE_WRITE` | Send `GetFirmwareVersion` command frame |
| `NFC GATE=<n> STEP=FIRMWARE_ACK` | Read ACK — expects `01 00 00 FF 00 FF 00` |
| `NFC GATE=<n> STEP=FIRMWARE_READY` | Poll STATUS until `01` |
| `NFC GATE=<n> STEP=FIRMWARE_RESPONSE` | Read and parse firmware response |
| `NFC GATE=<n> STEP=FIRMWARE_ACK_DIRECT DELAY=0.050` | Write firmware command and read ACK with configurable delay (timing probe) |
| `NFC GATE=<n> STEP=SAM_WRITE` | Send `SAMConfiguration` command frame |
| `NFC GATE=<n> STEP=SAM_ACK` | Read ACK |
| `NFC GATE=<n> STEP=SAM_READY` | Poll STATUS until `01` |
| `NFC GATE=<n> STEP=SAM_RESPONSE` | Read and parse SAM response |
| `NFC GATE=<n> STEP=PASSIVE_WRITE` | Send `InListPassiveTarget` command frame |
| `NFC GATE=<n> STEP=PASSIVE_ACK` | Read ACK |
| `NFC GATE=<n> STEP=PASSIVE_READY` | Poll STATUS until `01` |
| `NFC GATE=<n> STEP=PASSIVE_RESPONSE LEN=30` | Read raw tag-detect response |
| `NFC GATE=<n> READY_READ=1` | Read PN532 STATUS byte (`01`=ready, `00`=busy) |
| `NFC GATE=<n> ACK_READ=1 LEN=7` | Read STATUS then ACK frame |
| `NFC GATE=<n> RAW_READ=1 LEN=<n>` | Read exactly N raw bytes |
| `NFC GATE=<n> RAW_WRITE=<hex>` | Write raw bytes (no framing added) |
| `NFC GATE=<n> RAW_CMD=<hex>` | Build and send a complete PN532 command frame |

---

## Startup — Happy Hare Cache Seed

When Klipper connects, NFC_Manager runs this sequence for each lane automatically:

```
klippy:connect  →  _handle_connect()  →  schedule _delayed_init() (2 s)

_delayed_init()
  1. Initialise PN532 reader
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
[OK] NFC[lane0]: reader ready.  HH seed: spool_id=42  Startup polling is enabled; first poll in 0.0s.
[OK] NFC[lane1]: reader ready.  HH reports gate empty  Run NFC GATE=1 READ=1 to start polling.
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
- Happy Hare wasn't fully initialised when the PN532 init ran and the startup seed was skipped
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
  Gate 4:  spool 43     UID 04456192D32A81   [polling]  [HH: spool 43]
```

---

### `NFC GATE=<n> STATUS=1`

Same as `NFC_STATUS` but for a single lane.

```gcode
NFC GATE=4 STATUS=1
```

---

### `NFC GATE=<n> INIT=1`

Runs the PN532 initialization sequence: wakeup → `GetFirmwareVersion` → `SAMConfiguration`.

```gcode
NFC GATE=0 INIT=1
```

**When to use:** After first wiring, after a failed startup, or after flashing lane MCU firmware.

Expected success:
```
NFC[lane0]: reader OK
```

If this fails, see [Troubleshooting](../i2c-pn532/troubleshooting.md).

---

### `NFC GATE=<n> SCAN=1`

Reads the PN532 hardware once and prints the raw tag UID. Does not look up Spoolman and does not update Happy Hare.

```gcode
NFC GATE=0 SCAN=1
```

**When to use:**
- Getting a UID to register in Spoolman
- Confirming a reader can physically see a tag
- Checking whether a wiring or mode problem is fixed

---

### `NFC GATE=<n> JOG_SCAN=1`

Starts the scan-and-jog sequence on demand, identical to the automatic pre-load trigger that fires when Happy Hare parks filament at the gate.

```gcode
NFC GATE=0 JOG_SCAN=1
```

**What it does:** Selects the gate, then jogs the filament forward in `scan_jog_mm` increments, reading the NFC tag after each step. When the tag is found it rewinds toward the parked position, leaves `scan_rewind_buffer_mm` for Happy Hare's final gate parking step, and runs `_MMU_STEP_UNLOAD_GATE`. If the lane's Happy Hare Bowden calibration length is reached without a read, it follows the same rewind-and-park path and exits scan mode.

`HH_SYNC=0` skips the pre-scan `MMU_SPOOLMAN SYNC=1` call. Use it when scan-jog is launched from inside a Happy Hare hook, where calling back into HH via Spoolman sync would cause reentrancy problems.

```gcode
NFC GATE=0 JOG_SCAN=1 HH_SYNC=0
```

**Happy Hare post-preload hook setup:**

The [jacksky6 JK-dev branch](https://github.com/jacksky6/Happy-Hare/tree/JK-dev) of Happy Hare adds `variable_user_post_preload_extension` in `config/base/mmu_macro_vars.cfg`. Set it to drive NFC scan-jog automatically after each `MMU_PRELOAD`:

```ini
[gcode_macro _MMU_SEQUENCE_VARS]
description: Happy Hare sequence macro configuration variables
gcode: # Leave empty
variable_user_post_preload_extension: 'NFC JOG_SCAN=1 HH_SYNC=0'
```

Happy Hare appends `GATE=<n>` automatically after a successful preload, so the final command becomes:

```gcode
NFC JOG_SCAN=1 HH_SYNC=0 GATE=<n>
```

Recommended NFC config when using the hook — disables gate-status polling so HH is the sole trigger:

```ini
[nfc_gate]
startup_polling: 0
scan_enabled:    False
```

**Preconditions** (same as the automatic path — the command checks all of these and reports a plain-language error if any fail):

| Check | What it guards |
|---|---|
| PN532 not in failed state | Reader must have initialised successfully |
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

If a precondition fails:
```
NFC[lane0]: Happy Hare is busy (action=loading) — wait for idle before starting scan-jog
```

---

### `NFC GATE=<n> POLL=1`

Runs one complete cycle of the NFC manager pipeline:

1. PN532 reads the UID
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
NFC gate 0: tag UID 04AABBCCDD is not registered in Spoolman.
Open the spool record in Spoolman, set the 'rfid_tag' extra field to: 04AABBCCDD
```

---

### `NFC GATE=<n> APPLY=1`

Forces the lane's cached spool assignment through to Happy Hare immediately. Does not read the PN532, does not query Spoolman — it just dispatches:

```gcode
_NFC_SPOOL_CHANGED GATE=<gate> SPOOL_ID=<cached_id> UID=<cached_uid>
```

```gcode
NFC GATE=0 APPLY=1
```

**When to use:** When a poll or polling cycle already resolved a spool, but Happy Hare didn't update (e.g. it was in a locked state during the scan). If you get "no cached spool_id", run `POLL=1` first.

---

### `NFC GATE=<n> CLEAR_CACHE=1`

Clears the lane's cached spool ID and forces a fresh Spoolman lookup on the next tag read.

```gcode
NFC GATE=0 CLEAR_CACHE=1
```

Clears:
1. The lane's cached spool ID
2. The SpoolmanClient UID cache
3. The PN532 driver's in-memory current-card cache

On the next poll, the full `(uid, spool_id)` combination is re-evaluated:

- **Same UID, same spool in Spoolman** → cache refreshed silently, no dispatch
- **Same UID, different spool in Spoolman** → `_NFC_SPOOL_CHANGED` dispatched with the new spool

This is the correct way to force a re-read after editing a spool's UID registration in Spoolman.

`CLEAR=1` is accepted as a shorthand.

---

### `NFC GATE=<n> HH_SYNC=1 SPOOL_ID=<n>`

Seeds this lane's cache with a spool_id from Happy Hare's gate map. On the next poll, if the physical tag resolves to that spool_id, the dispatch is suppressed (HH already knows).

```gcode
NFC GATE=0 HH_SYNC=1 SPOOL_ID=42
```

This command is normally called automatically by the `NFC_HH_SYNC_CACHE` macro — you won't need it directly in normal operation. Use `NFC_HH_SYNC_CACHE` to sync all lanes at once.

---

### `NFC GATE=<n> READ=1` / `READ=0`

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
_NFC_SPOOL_CHANGED GATE=<gate> SPOOL_ID=<id> UID=<uid> [AUTO_CREATED=1]
```

**Metadata path** — tag carries embedded filament data (Spoolman disabled or no match):
```gcode
_NFC_SPOOL_CHANGED GATE=<gate> UID=<uid> [MATERIAL=<str>] [COLOR=<hex>] [TEMP=<int>]
```

Parameters:
- `GATE` — Happy Hare gate number (integer, matches `mmu_gate` in config)
- `SPOOL_ID` — Spoolman spool ID (integer); present on Spoolman path only
- `UID` — NFC tag UID (hex string); always present
- `AUTO_CREATED` — `1` when `spoolman_auto_create` just created the spool record; absent otherwise
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
    MMU_GATE_MAP GATE={gate} [MATERIAL=..] [COLOR=..] [TEMP=..] AVAILABLE=1 QUIET=1
{% endif %}
MMU_GATE_MAP GATE={gate} APPLY=1
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
MMU_GATE_MAP GATE={gate} SPOOLID=-1 AVAILABLE=1 SYNC=1 QUIET=1
MMU_GATE_MAP GATE={gate} APPLY=1
```

Clears only the Spoolman ID in Happy Hare's gate map. `AVAILABLE=1` keeps the gate marked as occupied/available, and `APPLY=1` applies the change immediately.

The macro also checks `printer.mmu.action` — if the MMU is mid-load, unload, or homing, the removal is silently ignored to avoid clearing the gate while filament is actively moving.

---

### `_NFC_TAG_NO_SPOOL`

Fires when a tag UID is detected but no matching spool is found in Spoolman.

```gcode
_NFC_TAG_NO_SPOOL GATE=<gate> UID=<uid>
```

Parameters:
- `GATE` — Happy Hare gate number
- `UID` — the unrecognized tag UID

Default behavior: prints a message to the console with the UID and instructions to register it, then leaves the Happy Hare gate available with no Spoolman ID.

**Optional:** If you want unregistered tags to clear the Happy Hare gate instead, change the macro body to:
```gcode
MMU_GATE_MAP GATE={gate} SPOOLID=-1 AVAILABLE=0 SYNC=1 QUIET=1
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
The shared reader is the exception: `NFC_SHARED PRELOAD_CHECK=1` calls Happy
Hare's public GCode commands directly from Python because it must stage
`NEXT_SPOOLID` at the exact moment the pre-load hook fires.

### Happy Hare commands used by the defaults

| Command | Effect |
|---|---|
| `MMU_GATE_MAP GATE=<n> SPOOLID=<id> AVAILABLE=1 SYNC=1 QUIET=1` | Assign a spool to a gate, mark it available, and sync to Spoolman |
| `MMU_GATE_MAP GATE=<n> APPLY=1` | Apply the current gate map immediately |
| `MMU_GATE_MAP GATE=<n> SPOOLID=-1 AVAILABLE=0 SYNC=1 QUIET=1` | Clear a gate, mark it empty, and sync to Spoolman |
| `MMU_GATE_MAP GATE=<n> [MATERIAL=..] [COLOR=..] [TEMP=..] AVAILABLE=1 QUIET=1` | Assign metadata (no Spoolman spool ID) — metadata path only |
| `MMU_GATE_MAP NEXT_SPOOLID=<id>` | Stage the next shared-reader spool for the gate Happy Hare is about to preload |
| `MMU_SPOOLMAN REFRESH=1 QUIET=1` | Force Happy Hare to re-sync its Spoolman cache — called before gate assignment when a new spool was auto-created |

The default lane macros are designed for Happy Hare with `spoolman_support:
push`. `SYNC=1` tells Happy Hare to push the local gate map change to Spoolman.
If your Happy Hare version uses different lane command names or parameters,
update the macro body. Shared-reader `NEXT_SPOOLID` staging is implemented in
Python and uses Happy Hare's public GCode command surface.

---

## Shared Reader

The shared reader is a single PN532 mounted inside the MMU body. Tap a spool tag on it before loading; NFC stages the spool ID for the next pregate preload automatically. See [Shared Reader](shared-reader.md) for the full setup and workflow guide.

### Normal flow

1. Shared reader is polling. With `startup_polling: 1` it starts at boot and pauses automatically when printing starts, resuming when printing completes.
2. Tap your spool tag on the shared reader — NFC resolves the spool in Spoolman and stores it as pending. LED effect fires if configured.
3. Drop the spool into an MMU lane and push the filament tip into the pregate sensor.
4. Happy Hare detects the pregate load and fires `variable_user_pre_load_extension` → `NFC_SHARED PRELOAD_CHECK=1`.
5. NFC issues `MMU_GATE_MAP NEXT_SPOOLID=<id>` — Happy Hare assigns the spool to the loaded gate.
6. Pending state is cleared only after Happy Hare accepts the command. For auto-created Spoolman spools, NFC refreshes HH's Spoolman cache first. Polling restarts automatically for the next spool.

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

**`NFC_SHARED RETRY=1`** — Alias for retrying `PRELOAD_CHECK=1` after fixing an HH/Spoolman issue.

**`NFC_SHARED LED_TEST=1`** — Play the configured `shared_tag_read_effect` without scanning a tag. Use this during setup to confirm the HH LED effect exists and works.

### Advanced Shared Reader Commands

These are shared-reader control and recovery commands. They are not low-level
PN532 debug commands, and they do not require `low_level_debug: True`.

**`NFC_SHARED CLEAR=1`** — Clear pending state, stop polling, reset the reader. Use this to cancel a staged spool before the preload fires.

**`NFC_SHARED PRELOAD_CHECK=1`** — Called automatically by `variable_user_pre_load_extension`. Issues `MMU_GATE_MAP NEXT_SPOOLID=<id>` if a valid pending spool exists. Skips only while printing. If no spool is staged, a console message advises tapping a tag first or using `MMU_PRELOAD`. With `force_spool_id: true` the load is blocked entirely until a spool is staged.

If HH/Spoolman staging fails, the pending spool is kept and the console tells
you to fix the issue and retry with `NFC_SHARED PRELOAD_CHECK=1`.

**`NFC_SHARED POLL=1`** — Force one full read/resolve cycle. Skips while printing and reports that no poll was run.

**`NFC_SHARED SCAN=1`** — Raw hardware scan only — shows UID, no Spoolman lookup or dispatch. Skips while printing.

**`NFC_SHARED INIT=1`** — Re-run PN532 initialisation. Use after a wiring fault or reader failure. If `startup_polling: 1` is set, the printer is not printing, and no spool is pending, polling resumes automatically after a successful init.

**`NFC_SHARED CLEAR_CACHE=1`** — Clear the tag UID cache without clearing any pending spool state.

### Pending timeout

`shared_pending_timeout` starts when a tag resolves. If no preload fires within
that window, the pending spool expires automatically. With `startup_polling: 1`,
polling resumes after the expired spool is cleared. Tap again to queue a new
spool.

### Unresolvable tags

If the reader sees a UID that Spoolman cannot resolve, it increments a miss counter. After `shared_missed_limit` consecutive misses (default 3) a console message appears advising the user to use `MMU_PRELOAD` to load without spool assignment. The counter resets on a successful read, `CLEAR=1`, `READ=1`, or `REPLACE=1`.

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
