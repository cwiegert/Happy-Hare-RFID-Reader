# Klipper Command Reference

[Back to README](../../Readme.md)

This page is the operator reference for every Klipper command and macro used by NFC Gate Reader.

## Command Groups

| Group | Who Calls It | Purpose |
|---|---|---|
| User commands | You, from Fluidd/Mainsail console | Check status, initialize readers, scan, poll, start/stop polling |
| Manager event macros | `NFC_Manager` Python code | Notify config macros that a spool changed, was removed, or was not found |
| Happy Hare commands | `nfc_macros.cfg` | Apply the resolved spool to Happy Hare |
| Expert low-level commands | You, only when `low_level_debug: True` | Manual PN532 I2C bus bring-up and debugging |

## User Commands

### Summary

| Command | Parameters | What It Does |
|---|---|---|
| `NFC_GATE_STATUS` | none | Shows last known state for every configured lane |
| `NFC_GATE NAME=<lane> STATUS=1` | `NAME`, `STATUS` | Shows last known state for one lane |
| `NFC_GATE NAME=<lane> INIT=1` | `NAME`, `INIT` | Runs PN532 initialization for one lane |
| `NFC_GATE NAME=<lane> SCAN=1` | `NAME`, `SCAN` | Reads hardware once, no Spoolman lookup, no state-machine dispatch |
| `NFC_GATE NAME=<lane> POLL=1` | `NAME`, `POLL` | Runs one full manager poll: read, lookup, state update, macro dispatch if changed |
| `NFC_GATE NAME=<lane> APPLY=1` | `NAME`, `APPLY` | Sends the lane's cached spool assignment to Happy Hare immediately |
| `NFC_GATE NAME=<lane> CLEAR_CACHE=1` | `NAME`, `CLEAR_CACHE` | Clears cached spool resolution without dispatching a Happy Hare change |
| `NFC_GATE NAME=<lane> READ=1` | `NAME`, `READ` | Starts reactor-timer polling for one lane |
| `NFC_GATE NAME=<lane> READ=0` | `NAME`, `READ` | Stops reactor-timer polling for one lane |
| `NFC_GATE NAME=<lane> HELP=1` | `NAME`, `HELP` | Shows normal commands; when low-level debug is enabled, also shows PN532 debug commands |

### User Parameter Values

| Parameter | Valid Values | Required With | Behavior |
|---|---|---|---|
| `NAME` | `lane0`, `lane1`, `lane2`, etc. | all `NFC_GATE` commands | Selects the configured `[nfc_gate laneN]` instance |
| `STATUS` | `1` | `NFC_GATE` | Print one lane's manager state |
| `INIT` | `1` | `NFC_GATE` | Re-run PN532 wake, firmware check, and SAM configuration |
| `SCAN` | `1` | `NFC_GATE` | Hardware read only; useful to get UID without triggering Happy Hare |
| `POLL` | `1` | `NFC_GATE` | Full pipeline once; can trigger `_NFC_*` macros if state changes |
| `APPLY` | `1` | `NFC_GATE` | Dispatches `_NFC_SPOOL_CHANGED` using the lane's cached `spool_id` and UID. Use after `POLL=1` when you need to force the Happy Hare handoff without changing the tag |
| `CLEAR_CACHE` | `1` | `NFC_GATE` | Clears the lane's cached `spool_id`, clears the Spoolman lookup cache, and forces the next tag read to resolve Spoolman again. Alias: `CLEAR=1` |
| `READ` | `0` or `1` | `NFC_GATE` | `1` starts timer polling, `0` stops it |
| `HELP` | `1` | `NFC_GATE` | Print command help |

### `NFC_GATE_STATUS`

```gcode
NFC_GATE_STATUS
```

Shows the NFC_Manager in-memory state. This is not a live I2C read at the moment the command is typed.

Example:

```text
NFC gate status  (5 gates configured):
  Gate 0  [lane0]:  empty
  Gate 4  [lane4]:  spool 43     UID 04456192D32A81
```

### `NFC_GATE NAME=<lane> STATUS=1`

```gcode
NFC_GATE NAME=lane4 STATUS=1
```

Shows one lane's state.

### `NFC_GATE NAME=<lane> INIT=1`

```gcode
NFC_GATE NAME=lane4 INIT=1
```

Runs the PN532 init sequence manually. Use after wiring changes, failed delayed init, or MCU firmware updates.

Expected success:

```text
NFC_GATE[lane4]: reader OK
```

### `NFC_GATE NAME=<lane> SCAN=1`

```gcode
NFC_GATE NAME=lane4 SCAN=1
```

Runs a one-time hardware read and reports PN532 target fields. It does not call Spoolman and does not update Happy Hare.

Use it to answer: "Can this reader see this tag?"

### `NFC_GATE NAME=<lane> POLL=1`

```gcode
NFC_GATE NAME=lane4 POLL=1
```

Runs the complete manager path once:

1. PN532 reads UID.
2. NFC_Manager decides whether the UID is unchanged, changed, unknown, or absent.
3. If needed, SpoolmanClient resolves UID to spool ID.
4. GateState updates.
5. NFC_Manager dispatches `_NFC_SPOOL_CHANGED`, `_NFC_SPOOL_REMOVED`, or `_NFC_TAG_NO_SPOOL` only if state changed.

Use it to answer: "Does the complete NFC to Happy Hare pipeline work?"

### `NFC_GATE NAME=<lane> APPLY=1`

```gcode
NFC_GATE NAME=lane4 APPLY=1
```

Forces the current cached lane assignment through the Happy Hare macro boundary. It does not read the PN532 and does not call Spoolman. It simply dispatches:

```gcode
_NFC_SPOOL_CHANGED GATE=<gate> SPOOL_ID=<cached_spool_id> UID=<cached_uid>
```

Use this after `POLL=1` or active polling has already resolved a UID to a spool, but Happy Hare did not update. If this command prints "no cached spool_id", run `NFC_GATE NAME=<lane> POLL=1` first.

### `NFC_GATE NAME=<lane> CLEAR_CACHE=1`

```gcode
NFC_GATE NAME=lane4 CLEAR_CACHE=1
```

Clears cached spool resolution for the lane without dispatching `_NFC_SPOOL_CHANGED`, `_NFC_SPOOL_REMOVED`, or `_NFC_TAG_NO_SPOOL`.

This is for the case where the reader still remembers a UID, but you want the cached `spool_id` emptied so the next real tag read resolves through Spoolman again. It clears:

1. The lane's cached `spool_id`.
2. The SpoolmanClient UID lookup cache.
3. The PN532 driver's in-memory current-card cache.

It intentionally leaves the lane's UID baseline alone. That prevents the clear command itself from looking like a spool change. If the same UID is still present, the next lookup is treated as a quiet cache refresh and does not dispatch a Happy Hare macro. A different UID still follows the normal change path.

`CLEAR=1` is accepted as a short alias, but `CLEAR_CACHE=1` is the preferred documented command.

### `NFC_GATE NAME=<lane> READ=1`

```gcode
NFC_GATE NAME=lane4 READ=1
```

Starts reactor-timer polling for that lane. Polling happens on Klipper's reactor thread because Klipper MCU I2C uses reactor greenlets internally.

### `NFC_GATE NAME=<lane> READ=0`

```gcode
NFC_GATE NAME=lane4 READ=0
```

Stops reactor-timer polling for that lane.

## Manager Event Macros

These are called by `NFC_Manager`. They live in `nfc_macros.cfg`.

### Summary

| Macro | Called When | Parameters |
|---|---|---|
| `_NFC_SPOOL_CHANGED` | UID resolves to a spool and the lane state changed | `GATE`, `SPOOL_ID`, `UID` |
| `_NFC_SPOOL_REMOVED` | A previously present tag is absent for `absent_threshold` polls | `GATE` |
| `_NFC_TAG_NO_SPOOL` | UID was read but no Spoolman spool matched | `GATE`, `UID` |

### `_NFC_SPOOL_CHANGED`

```gcode
_NFC_SPOOL_CHANGED GATE=4 SPOOL_ID=43 UID=04456192D32A81
```

| Parameter | Valid Values | Meaning |
|---|---|---|
| `GATE` | integer, `0` or higher | Happy Hare gate number from `mmu_gate` |
| `SPOOL_ID` | integer, `1` or higher | Spoolman spool ID returned by lookup |
| `UID` | hex string | Normalized NFC tag UID |

Default body:

```ini
[gcode_macro _NFC_SPOOL_CHANGED]
gcode:
    {% set gate     = params.GATE     | int %}
    {% set spool_id = params.SPOOL_ID | int %}
    {% set uid      = params.UID %}
    { action_respond_info("😊 NFC gate %d: spool %d detected (UID %s). Sending to Happy Hare." % (gate, spool_id, uid)) }
    MMU_SPOOLMAN UPDATE=1 GATE={gate} SPOOLID={spool_id}
```

### `_NFC_SPOOL_REMOVED`

```gcode
_NFC_SPOOL_REMOVED GATE=4
```

| Parameter | Valid Values | Meaning |
|---|---|---|
| `GATE` | integer, `0` or higher | Happy Hare gate number from `mmu_gate` |

Default body:

```ini
[gcode_macro _NFC_SPOOL_REMOVED]
gcode:
    {% set gate = params.GATE | int %}
    { action_respond_info("🧹 NFC gate %d: spool removed. Clearing Happy Hare Spoolman gate." % gate) }
    MMU_SPOOLMAN UPDATE=1 GATE={gate} SPOOLID=-1
```

### `_NFC_TAG_NO_SPOOL`

```gcode
_NFC_TAG_NO_SPOOL GATE=4 UID=04456192D32A81
```

| Parameter | Valid Values | Meaning |
|---|---|---|
| `GATE` | integer, `0` or higher | Happy Hare gate number from `mmu_gate` |
| `UID` | hex string | UID that was read but not found in Spoolman |

Default body is informational only:

```ini
[gcode_macro _NFC_TAG_NO_SPOOL]
gcode:
    {% set gate = params.GATE | int %}
    {% set uid  = params.UID %}
    { action_respond_info(
        "NFC gate %d: tag UID %s is not registered in Spoolman.\n"
        "Open the spool record in Spoolman, set the 'rfid_tag' extra field to: %s" %
        (gate, uid, uid)) }
```

If you want unknown tags to clear the Happy Hare gate, add:

```gcode
MMU_SPOOLMAN UPDATE=1 GATE={gate} SPOOLID=-1
```

## Happy Hare Commands Used By The Default Macros

| Command | Parameters | Meaning |
|---|---|---|
| `MMU_SPOOLMAN UPDATE=1 GATE=<gate> SPOOLID=<id>` | `UPDATE`, `GATE`, `SPOOLID` | Writes the resolved spool ID to Happy Hare's Spoolman-backed gate map |
| `MMU_SPOOLMAN UPDATE=1 GATE=<gate> SPOOLID=-1` | `UPDATE`, `GATE`, `SPOOLID=-1` | Clears the Spoolman-backed gate assignment |

The default command uses `MMU_SPOOLMAN UPDATE=1` because Happy Hare owns the Spoolman-backed gate mapping and cache:

```gcode
MMU_SPOOLMAN UPDATE=1 GATE=<gate> SPOOLID=<spool_id>
```

Older or different Happy Hare flows may show commands such as:

```gcode
MMU_GATE_MAP NEXT_SPOOLID=<ID>
MMU_GATE_MAP GATE=<gate> SPOOLMAN_ID=<spool_id>
```

Those are not the default here. `NEXT_SPOOLID` does not identify which physical gate was read, and `MMU_GATE_MAP` alone does not write the Spoolman DB gate mapping. Keep Happy Hare command differences inside `nfc_macros.cfg`. Do not put Happy Hare commands in `PN532Driver` or `SpoolmanClient`.

## Test Macro Boundary Without Hardware

Run these from Fluidd/Mainsail:

```gcode
_NFC_SPOOL_CHANGED GATE=4 SPOOL_ID=43 UID=04456192D32A81
_NFC_SPOOL_REMOVED GATE=4
_NFC_TAG_NO_SPOOL GATE=4 UID=04456192D32A81
```

If `_NFC_SPOOL_CHANGED` does not update Happy Hare, the problem is the macro body or Happy Hare command syntax, not the PN532 reader or Spoolman lookup.

## Expert Low-Level PN532 Commands

These are hidden unless:

```ini
[nfc_gate]
low_level_debug: True
```

When any low-level command is used while polling is active, NFC_Manager pauses polling first.

### Summary

| Command | Parameters | What It Does |
|---|---|---|
| `NFC_GATE NAME=<lane> HELP=1` | `NAME`, `HELP` | Shows normal and low-level command help |
| `NFC_GATE NAME=<lane> STEP=WAKEUP` | `NAME`, `STEP` | Writes PN532 wake byte |
| `NFC_GATE NAME=<lane> STEP=READY` | `NAME`, `STEP` | Reads PN532 ready byte |
| `NFC_GATE NAME=<lane> STEP=FIRMWARE_WRITE` | `NAME`, `STEP` | Writes `GetFirmwareVersion` frame |
| `NFC_GATE NAME=<lane> STEP=FIRMWARE_ACK` | `NAME`, `STEP`, optional `LEN` | Reads ACK for firmware command |
| `NFC_GATE NAME=<lane> STEP=FIRMWARE_READY` | `NAME`, `STEP` | Reads ready before firmware response |
| `NFC_GATE NAME=<lane> STEP=FIRMWARE_RESPONSE` | `NAME`, `STEP`, optional `LEN` | Reads and parses firmware response |
| `NFC_GATE NAME=<lane> STEP=FIRMWARE_ACK_DIRECT` | `NAME`, `STEP`, optional `DELAY`, optional `LEN` | Writes firmware command, waits, then reads ACK directly |
| `NFC_GATE NAME=<lane> STEP=SAM_WRITE` | `NAME`, `STEP` | Writes `SAMConfiguration` |
| `NFC_GATE NAME=<lane> STEP=SAM_ACK` | `NAME`, `STEP`, optional `LEN` | Reads ACK for SAM command |
| `NFC_GATE NAME=<lane> STEP=SAM_READY` | `NAME`, `STEP` | Reads ready before SAM response |
| `NFC_GATE NAME=<lane> STEP=SAM_RESPONSE` | `NAME`, `STEP`, optional `LEN` | Reads and parses SAM response |
| `NFC_GATE NAME=<lane> STEP=PASSIVE_WRITE` | `NAME`, `STEP` | Writes `InListPassiveTarget` |
| `NFC_GATE NAME=<lane> STEP=PASSIVE_ACK` | `NAME`, `STEP`, optional `LEN` | Reads ACK for passive target command |
| `NFC_GATE NAME=<lane> STEP=PASSIVE_READY` | `NAME`, `STEP` | Reads ready before tag response |
| `NFC_GATE NAME=<lane> STEP=PASSIVE_RESPONSE` | `NAME`, `STEP`, optional `LEN` | Reads raw tag-detect response |
| `NFC_GATE NAME=<lane> RAW_READ=1 LEN=<n>` | `NAME`, `RAW_READ`, `LEN` | Raw PN532 transport read |
| `NFC_GATE NAME=<lane> RAW_WRITE=<hex>` | `NAME`, `RAW_WRITE` | Raw PN532 transport write |
| `NFC_GATE NAME=<lane> RAW_CMD=<hex>` | `NAME`, `RAW_CMD` | Build and write a PN532 command frame |
| `NFC_GATE NAME=<lane> READY_READ=1` | `NAME`, `READY_READ` | Raw ready-byte read |
| `NFC_GATE NAME=<lane> ACK_READ=1 LEN=<n>` | `NAME`, `ACK_READ`, `LEN` | Ready read, then ACK read |

### Expert Parameter Values

| Parameter | Valid Values | Default | Notes |
|---|---|---:|---|
| `STEP` | listed step names above | `HELP` | Case-insensitive |
| `LEN` | integer `1` to `64` | command-specific | ACK defaults to `7`; passive response defaults to `30` |
| `DELAY` | float `0.0` to `2.0` | `0.050` | Used by `FIRMWARE_ACK_DIRECT` |
| `RAW_WRITE` | hex bytes, e.g. `00` or `00 00 FF` | none | Separators may be spaces, commas, colons, or hyphens |
| `RAW_CMD` | PN532 command bytes, e.g. `02` | none | Driver builds the full PN532 frame |
| `RAW_READ` | `1` | none | Requires `LEN` for more than one byte |
| `READY_READ` | `1` | none | Reads one byte |
| `ACK_READ` | `1` | none | Uses `LEN`, normally `7` for I2C ACK with status byte |

See [Expert: Low-Level PN532 I2C Debugging](expert-low-level-i2c-debugging.md) for the step-by-step bring-up flow.
