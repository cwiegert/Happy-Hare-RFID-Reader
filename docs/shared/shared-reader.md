# Shared Reader

[← Configuration](configuration.md) | [Commands](klipper-functions.md) | [Messages →](message_definition.md)

The shared reader is an optional single PN532 mounted **inside the MMU body** — not tied to any EMU lane. You tap a tagged spool on it before loading; when Happy Hare starts the pregate preload NFC stages the spool ID automatically.

For the full console/log message reference, see [Message Definitions](message_definition.md).

---

## Rich tag compatibility

The shared reader can only stage a **Spoolman spool ID**. Happy Hare's
`MMU_GATE_MAP NEXT_SPOOLID=<id>` requires an integer spool ID; it cannot accept
raw filament metadata from a rich tag.

That means rich tags work only when NFC can turn the tag into a real Spoolman
spool first:

| Tag / resolution path | `spoolman_url` | `spoolman_auto_create` | Works with shared reader? |
|---|---|---|---|
| UID is already registered in Spoolman | `auto` / URL | either | ✅ — existing spool ID is staged |
| Rich tag contains an embedded `spoolman_id` that exists in Spoolman | `auto` / URL | either | ✅ — embedded spool ID is staged |
| Rich tag has metadata but no existing Spoolman spool | `auto` / URL | `true` | ✅ — spool is auto-created, then staged |
| Rich tag has metadata but no existing Spoolman spool | `auto` / URL | `false` | ⚠️ — tag can be read, but no spool ID exists to stage |
| Spoolman is disabled, unavailable, or cannot be discovered | empty / disabled / undiscovered `auto` | either | ⚠️ — metadata-only dispatch is not usable for shared preload staging |

When a rich tag is readable but does not resolve to a Spoolman spool ID, the
shared reader treats it as unresolved, increments the miss counter, and after
`shared_missed_limit` attempts emits a console message advising `MMU_PRELOAD` or
enabling `spoolman_auto_create`.

---

## What it does

- Polls continuously for NFC tags while the printer is idle.
- When a tag is recognised, resolves it to a Spoolman spool ID and holds it as **pending**.
- When Happy Hare begins a pregate preload it fires the `user_pre_load_extension` hook → NFC calls `MMU_GATE_MAP NEXT_SPOOLID=<id>` → Happy Hare assigns that spool to the loaded gate.
- After staging, polling restarts automatically — no user action required between spools.

**No per-lane readers are required.** A shared-only installation needs only the base `[nfc_gate]` section (for Spoolman config) and `[nfc_gate shared]`.

---

## Normal load flow

1. **Shared reader is polling.** `startup_polling: 1` starts it at boot. It scans continuously, pausing automatically when printing starts and resuming when printing completes — no manual intervention required.

2. **Tap your spool tag on the shared reader.** NFC reads the UID and looks it up in Spoolman. Tag detection can flash yellow, auto-create can run a yellow chase while Spoolman creates a missing spool, and the ready-to-load confirmation is the green 2x blink. On success the spool ID is stored as pending, the `shared_pending_timeout` countdown starts, and polling stops.

3. **Drop the spool into an MMU lane** (physical action — NFC takes no action here).

4. **Push the filament tip into the pregate/buffer sensor.** Happy Hare detects filament at the sensor and begins a pregate load. HH's action transitions to `loading`.

5. **Happy Hare fires `user_pre_load_extension` → `_NFC_SHARED_PRELOAD` macro.** This happens automatically — no user action required. The macro reads Happy Hare's current gate map with normalized spool IDs before choosing a path.

6. **The behavior forks cleanly.** If Happy Hare already has the pending spool assigned to a gate, the macro calls `NFC_SHARED PRELOAD_CLEAR_ASSIGNED=1` and skips shared `NEXT_SPOOLID`. Otherwise it calls `NFC_SHARED PRELOAD_CHECK=1`, then runs the narrow Happy Hare bridge: optional `MMU_SPOOLMAN REFRESH=1 QUIET=1`, `MMU_GATE_MAP NEXT_SPOOLID=<spool_id>`, and `NFC_SHARED PRELOAD_COMMIT=1`. Pending state is cleared only by `PRELOAD_COMMIT` or `PRELOAD_CLEAR_ASSIGNED`, so if refresh or `MMU_GATE_MAP` fails, the pending spool is kept until timeout.

7. **Happy Hare completes the pregate load and assigns the staged spool ID** to the loaded gate. The spool is now registered in HH's gate map.

8. **Polling restarts automatically** (no read deadline). The shared reader is immediately ready for the next spool tap.

---

## What happens when no spool is staged

If `PRELOAD_CHECK` fires and no valid pending spool exists (none scanned, or the timeout expired), a console message appears:

```
    ⚠️ NFC[shared]: no spool staged — tap your spool tag on the shared reader first,
or use MMU_PRELOAD to load without spool assignment
```

The pregate load continues normally — Happy Hare loads the gate without a spool ID.

With `force_spool_id: true` the load is **blocked** instead: a gcode error stops the preload macro chain until the user taps a tag.

---

## What happens when a tag cannot be resolved

If the reader reads a UID that Spoolman does not recognise, the miss counter increments. After `shared_missed_limit` consecutive misses (default 3) a console message advises:

```
⚠️ NFC[shared]: tag uid=<uid> not found in Spoolman after 3 attempts —
use MMU_PRELOAD to load without spool assignment
```

The counter resets on a successful read, `NFC_SHARED CLEAR=1`,
`NFC_SHARED READ=1`, or `NFC_SHARED REPLACE=1`.

---

## Configuration

The shared reader config lives in its own file — `nfc_reader_shared.cfg` — separate from the lane hardware config. This allows it to be added to any install without touching existing config files.

**Pure shared install** — include this instead of `nfc_reader_hw.cfg`:
```ini
[include nfc/nfc_reader.cfg]
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_shared.cfg]
```

**Hybrid install** (per-lane readers + shared reader) — include both:
```ini
[include nfc/nfc_reader.cfg]
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_hw.cfg]
[include nfc/nfc_reader_shared.cfg]
```

In hybrid installs, per-lane readers take precedence. If a lane reader has
already assigned the shared reader's pending spool in Happy Hare, the shared
preload bridge skips `NEXT_SPOOLID`, clears the shared pending state, and lets
the Happy Hare preload continue.

Run `install.sh` to generate `nfc_reader_shared.cfg` automatically, or copy `config/nfc_reader_shared.cfg` from the repo and fill in `i2c_mcu`, `i2c_bus`, and `i2c_address`.

The `[nfc_gate shared]` section in `nfc_reader_shared.cfg`:

Minimal config:

```ini
[nfc_gate shared]
i2c_mcu:         mmu
i2c_bus:         i2c1
i2c_address:     0x24
shared:          true
startup_polling: 1
```

Full config with all optional keys:

```ini
[nfc_gate shared]
i2c_mcu:                mmu
i2c_bus:                i2c1
i2c_address:            0x24
shared:                 true
startup_polling:        1
poll_interval:          3.0
shared_pending_timeout: 120.0
shared_read_timeout:    120.0
shared_tag_read_effect: mmu_RFID_read
shared_spool_ready_effect: mmu_RFID_ready
shared_tag_unresolved_effect: mmu_RFID_unresolved
shared_missed_limit:    3
force_spool_id:         false
```

| Key | Default | Description |
|---|---|---|
| `shared` | `false` | Must be `true`. Enables shared dispatch mode. |
| `startup_polling` | `0` | Set to `1` to start polling at Klipper boot. |
| `poll_interval` | `3.0` | Seconds between tag reads while polling. |
| `shared_pending_timeout` | `120.0` | Seconds a resolved spool stays eligible for the next preload. |
| `shared_read_timeout` | `120.0` | Seconds polling may run after `NFC_SHARED READ=1` without resolving a tag before auto-stopping. Has no effect when started via `startup_polling` or after a successful `PRELOAD_CHECK`. |
| `shared_tag_read_effect` | `''` | Name of a `[mmu_led_effect]` to play as soon as the shared reader sees a tag. |
| `shared_spool_ready_effect` | `''` | Name of a `[mmu_led_effect]` to play when the tag resolves to a Spoolman spool and is ready to load. |
| `shared_tag_unresolved_effect` | `''` | Name of a `[mmu_led_effect]` to play when the tag UID does not resolve to a spool. |
| `shared_missed_limit` | `3` | Consecutive unresolvable reads before a console message advises `MMU_PRELOAD`. Minimum 1. |
| `force_spool_id` | `false` | Block pregate loads entirely when no spool is staged. |

`mmu_gate` and `scan_enabled` are set internally — do not add them. Only one shared reader may be configured. All Spoolman connection settings and logging settings are inherited from the base `[nfc_gate]` section.

---

## Happy Hare hook wiring

Add one user extension hook to `mmu_macro_vars.cfg`:

```ini
[gcode_macro _MMU_SEQUENCE_VARS]
; stage NEXT_SPOOLID before a pregate-triggered preload
variable_user_pre_load_extension: '_NFC_SHARED_PRELOAD'
```

`variable_user_pre_load_extension` fires at the start of every pregate load. `PRELOAD_CHECK` is safe to leave wired for all loads — it skips only while printing, and emits an advisory message (or blocks, with `force_spool_id`) when no spool is staged.

Shared polling pauses automatically when printing starts (`idle_timeout:printing`) and resumes when printing completes (`idle_timeout:ready`). No post-unload hook is needed.

---

## LED feedback

Define a named `[mmu_led_effect]` in your LED config:

```ini
[mmu_led_effect mmu_RFID_read]
define_on: gates
layers: strobe 1 2 top (1, 1, 0)

[mmu_led_effect mmu_RFID_ready]
define_on: gates
layers: strobe 1 2 top (0, 1, 0)

[mmu_led_effect mmu_RFID_unresolved]
define_on: gates
layers: strobe 1 5 top (1, 0, 0)
```

Set `shared_tag_read_effect: mmu_RFID_read`, `shared_spool_ready_effect: mmu_RFID_ready`, and `shared_tag_unresolved_effect: mmu_RFID_unresolved` in `[nfc_gate shared]`. Tag detection flashes bright yellow 2x; auto-create runs a yellow chase; a ready spool ID flashes bright green 2x; an unresolved UID flashes bright red 5x. If auto-create is enabled, keep the tag in front of the reader until the green ready blink appears.

---

## Commands

| Command | What it does |
|---|---|
| `NFC_SHARED READ=1` | Start polling. Refuses to overwrite a pending spool; use `REPLACE=1` or `CANCEL=1` first. Rejected while printing. |
| `NFC_SHARED READ=0` | Stop polling. Keeps any pending spool. |
| `NFC_SHARED STATUS=1` | Show detailed state — summary, polling flags, deadlines, pending spool, miss counter, LED effect, last action, next action, and last error. |
| `NFC_SHARED SUMMARY=1` | Show one compact state line and next suggested action. |
| `NFC_SHARED HELP=1` | Show shared reader command help. |
| `NFC_SHARED CANCEL=1` | Cancel a staged spool and stop polling. |
| `NFC_SHARED REPLACE=1` | Discard a staged spool and start scanning another. |
| `NFC_SHARED LED_TEST=1` | Test the configured shared tag-read LED effect. |

Advanced shared-reader commands:

| Command | What it does |
|---|---|
| `NFC_SHARED CLEAR=1` | Clear pending state, stop polling, reset the reader. |
| `NFC_SHARED PRELOAD_CHECK=1` | Approve `NEXT_SPOOLID` if a valid spool is pending. Called automatically by the HH hook before the macro bridge runs Happy Hare commands. |
| `NFC_SHARED PRELOAD_COMMIT=1 SPOOL_ID=<id>` | Clear pending state after the macro bridge successfully sends `NEXT_SPOOLID`. Called automatically by the HH hook. |
| `NFC_SHARED PRELOAD_CLEAR_ASSIGNED=1 SPOOL_ID=<id>` | Clear shared pending state when the macro sees Happy Hare already has this spool assigned, preserving per-lane precedence. Called automatically by the HH hook. |
| `NFC_SHARED POLL=1` | Force one full read/resolve cycle. Skips while printing. |
| `NFC_SHARED SCAN=1` | Raw hardware scan — shows UID only, no Spoolman lookup. Skips while printing. |
| `NFC_SHARED INIT=1` | Re-run PN532 initialisation. Resumes startup polling when enabled and safe. |
| `NFC_SHARED CLEAR_CACHE=1` | Clear the tag UID cache without clearing the pending spool. |

Full command reference: [Commands & Macros](klipper-functions.md#shared-reader).

If another valid tag is read while a spool is already pending, the shared
reader keeps the original pending spool. The new read is reported as ignored,
and the console points you to `NFC_SHARED REPLACE=1` if you meant to swap
spools.

---

## Troubleshooting

**Reader shows `READER FAILED` in `NFC_STATUS`.**
Run `NFC_SHARED INIT=1`. If it fails, check I2C wiring and confirm the MCU is flashed with the correct firmware.

**Tag scanned but spool not staged at preload.**
Check that `variable_user_pre_load_extension: '_NFC_SHARED_PRELOAD'` is set in `mmu_macro_vars.cfg` and that the printer was not actively printing when the preload fired.

If `MMU_SPOOLMAN REFRESH` or `MMU_GATE_MAP` fails, the macro aborts before
`NFC_SHARED PRELOAD_COMMIT=1`, so the pending spool is kept. Fix the
HH/Spoolman issue, then trigger the preload hook again before the pending
spool times out. If it expires, tap the tag again.

**`NFC_STATUS` shows `expired`.**
The `shared_pending_timeout` elapsed before the preload fired. The expired pending spool is cleared automatically; with `startup_polling: 1`, polling resumes. Tap the tag again. Increase `shared_pending_timeout` if you regularly take longer than 120 s between tapping and loading.

**Console shows "tag uid not found in Spoolman after N attempts".**
The tag is not registered in Spoolman. Either register the spool first or use `MMU_PRELOAD` to load without spool assignment.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
