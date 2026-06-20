# Shared Reader

[← Configuration](configuration.md) | [Commands](klipper-functions.md) | [Messages →](message_definition.md)

The shared reader is an optional single NFC reader mounted **inside the MMU body** - not tied to any EMU lane. It defaults to PN532 hardware, and can also use PN7160 by setting `reader_type: pn7160` in `[nfc_gate shared]`. You tap a tagged spool on it before loading; when Happy Hare starts the pregate preload NFC stages the spool ID automatically.

For the full console/log message reference, see [Message Definitions](message_definition.md).

---

## Rich tag compatibility

The shared reader can only stage a **Spoolman spool ID**. Happy Hare's
gate-map and Spoolman assignment commands require an integer spool ID; they
cannot accept raw filament metadata from a rich tag.

That means rich tags work only when NFC can turn the tag into a real Spoolman
spool first:

| Tag / resolution path | `spoolman_url` | `spoolman_auto_create` | Works with shared reader? |
|---|---|---|---|
| UID is already registered in Spoolman | `auto` / URL | either | Yes - existing spool ID is staged |
| Rich tag contains an embedded `spoolman_id` that exists in Spoolman | `auto` / URL | either | Yes - embedded spool ID is staged |
| Rich tag has metadata but no existing Spoolman spool | `auto` / URL | `true` | Yes - spool is auto-created, then staged |
| Rich tag has metadata but no existing Spoolman spool | `auto` / URL | `false` | No - tag can be read, but no spool ID exists to stage |
| Spoolman is disabled, unavailable, or cannot be discovered | empty / disabled / undiscovered `auto` | either | No - metadata-only dispatch is not usable for shared preload staging |

When a rich tag is readable but does not resolve to a Spoolman spool ID, the
shared reader treats it as unresolved, increments the miss counter, and after
`shared_missed_limit` attempts emits a console message advising `MMU_PRELOAD` or
enabling `spoolman_auto_create`.

---

## What it does

- Polls continuously for NFC tags while the printer is idle.
- When a tag is recognized, resolves it to a Spoolman spool ID, stages `MMU_GATE_MAP NEXT_SPOOLID=<id>` for Happy Hare, and holds the spool as **pending**.
- When Happy Hare begins a pregate preload it fires the `user_post_preload_extension` hook. `_NFC_SHARED_PRELOAD` validates that the same pending spool is still valid and commits it by calling `NFC_SHARED PRELOAD_CHECK=1` followed by `NFC_SHARED PRELOAD_COMMIT=1`.
- After staging, polling restarts automatically — no user action required between spools.

**No per-lane readers are required.** A shared-only installation needs only the base `[nfc_gate]` section (for Spoolman config) and `[nfc_gate shared]`.

---

## Normal load flow

1. **Shared reader is polling.** `startup_polling: 1` starts it at boot. It scans continuously, pausing automatically when printing starts and resuming when printing completes — no manual intervention required.

2. **Tap your spool tag on the shared reader.** NFC reads the UID and looks it up in Spoolman. Tag detection can flash yellow, auto-create can run a yellow chase while Spoolman creates a missing spool, and the ready-to-load confirmation is the green 2x blink. On success the spool ID is stored as pending, the `pending_spool_id_timeout` countdown starts (set in `mmu_parameters.cfg`), and polling stops.

3. **Drop the spool into an MMU lane** (physical action — NFC takes no action here).

4. **Push the filament tip into the pregate/buffer sensor.** Happy Hare detects filament at the sensor and begins a pregate load. HH's action transitions to `loading`.

5. **Happy Hare fires `user_post_preload_extension` -> `_NFC_SHARED_PRELOAD` macro.** This happens automatically - no user action required. The macro validates the pending shared-reader spool that was already staged as `NEXT_SPOOLID`.

6. **The macro commits the staged spool.** It calls `NFC_SHARED PRELOAD_CHECK=1 EXPECTED_SPOOL_ID=<spool_id>`, then `NFC_SHARED PRELOAD_COMMIT=1 SPOOL_ID=<spool_id>`. Happy Hare owns the gate assignment through its preload flow.

7. **Happy Hare completes the pregate load.** The spool is now registered in Happy Hare's gate map.

8. **Polling restarts automatically** (no read deadline). The shared reader is immediately ready for the next spool tap.

---

## What happens when no spool is staged

If `PRELOAD_CHECK` fires and no valid pending spool exists (none scanned, or the timeout expired), a console message appears:

```
[ERROR] NFC[shared]: no spool staged — tap your spool tag on the shared reader first,
or use MMU_PRELOAD to load without spool assignment
```

The pregate load continues normally — Happy Hare loads the gate without a spool ID.

With `force_spool_id: true`, the message specifically calls out `force_spool_id`
and tells the operator to tap a tag before loading or disable strict staging.

---

## What happens when a tag cannot be resolved

If the reader reads a UID that Spoolman does not recognise, the miss counter increments. After `shared_missed_limit` consecutive misses (default 3) a console error advises:

```
[ERROR] NFC[shared]: uid=<uid> not in Spoolman after 3 attempts
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

In hybrid installs, per-lane readers and the shared reader can coexist. The
shared reader stages a spool for the next Happy Hare pregate preload; per-lane
readers can still run scan-jog for lanes that have dedicated hardware.

Run `install.sh` to generate `nfc_reader_shared.cfg` automatically, or copy `config/nfc_reader_shared.cfg` from the repo and fill in `reader_type`, `i2c_mcu`, `i2c_bus`, and `i2c_address` as needed. If `reader_type` is omitted, the shared reader inherits the base `[nfc_gate]` default, which is `pn532` in the shipped config.

The `[nfc_gate shared]` section in `nfc_reader_shared.cfg`:

Minimal config:

```ini
[nfc_gate shared]
enabled:         True
i2c_mcu:         mmu
shared:          true
startup_polling: 1
```

PN7160 shared-reader example:

```ini
[nfc_gate shared]
enabled:         True
reader_type:     pn7160
i2c_address:     40
i2c_mcu:         mmu
shared:          true
startup_polling: 1
```

Full config with all optional keys:

```ini
[nfc_gate shared]
enabled:                True
reader_type:            pn532
i2c_mcu:                mmu
shared:                 true
startup_polling:        1
shared_read_timeout:    120.0
shared_tag_read_effect: mmu_RFID_read
read_effect_duration:   2.0
shared_bypass_tag_read_effect: mmu_RFID_bypass_read
bypass_read_effect_duration:   2.0
shared_spool_ready_effect: mmu_RFID_ready
shared_bypass_spool_ready_effect: mmu_RFID_bypass_ready
bypass_ready_effect_duration: 2.0
shared_tag_unresolved_effect: mmu_RFID_unresolved
unresolved_effect_duration: 2.0
shared_spool_warning_effect: mmu_RFID_warning
shared_auto_create_effect:   mmu_RFID_creating
shared_missed_limit:    3
force_spool_id:         true
```

| Key | Default | Description |
|---|---|---|
| `enabled` | `True` | Set `False` to keep the shared-reader config installed without initializing the reader. |
| `reader_type` | inherited from `[nfc_gate]` (`pn532` in the shipped config) | Reader driver to use. Supported values are `pn532` and `pn7160`. |
| `i2c_address` | reader-specific default | I2C address for the shared reader. PN532 defaults to `36`; PN7160 must be one of `40-43` (`0x28-0x2B`). |
| `shared` | `false` | Must be `true`. Enables shared dispatch mode. |
| `startup_polling` | `1` in the shipped template | Set to `1` to start polling at Klipper boot. |
| `scan_poll_interval` | inherited from `[nfc_gate]` | Seconds between shared-reader tag reads while polling. The shipped default is `0.25`. |
| `poll_interval` | inherited from `[nfc_gate]` | Ignored for shared-reader read cadence; lane readers still use it for normal background polling. |
| `pending_spool_id_timeout` | set in `mmu_parameters.cfg` | Seconds a resolved spool stays eligible for the next preload. Set in Happy Hare's `[mmu]` section (`~/printer_data/config/mmu/base/mmu_parameters.cfg`); NFC reads it automatically at connect time (falls back to 30 s). |
| `shared_read_timeout` | `120.0` | Seconds polling may run after `NFC_SHARED READ=1` without resolving a tag before auto-stopping. Has no effect when started via `startup_polling` or after a successful `PRELOAD_CHECK`. |
| `shared_tag_read_effect` | `''` | Name of a `[mmu_led_effect]` to play as soon as the shared reader sees a tag. |
| `read_effect_duration` | `2.0` | HH duration used by `NFC_SHARED LED_TEST=1`. Normal shared scans do not pass this duration to HH; NFC uses it only as a failsafe release window if no follow-up state replaces the read cue. |
| `shared_bypass_tag_read_effect` | `mmu_RFID_bypass_read` | Name of a `[mmu_led_effect]` to play when a tag is seen while bypass is selected. |
| `bypass_read_effect_duration` | `2.0` | Reserved for standalone bypass-read feedback. Normal bypass reads stay interruptible because bypass-ready feedback is expected to follow. |
| `shared_spool_ready_effect` | `''` | Name of a `[mmu_led_effect]` to play when the tag resolves to a Spoolman spool and is ready to load. Normal staged-spool ready feedback runs until preload commit, cancel, replace, or pending timeout; NFC then releases HH ownership with `MMU_GATE_MAP QUIET=1`. |
| `shared_bypass_spool_ready_effect` | `mmu_RFID_bypass_ready` | Name of a `[mmu_led_effect]` to play when a bypass spool resolves. |
| `bypass_ready_effect_duration` | `2.0` | Seconds before NFC stops `shared_bypass_spool_ready_effect`. |
| `shared_tag_unresolved_effect` | `''` | Name of a `[mmu_led_effect]` to play when the tag UID does not resolve to a spool. |
| `unresolved_effect_duration` | `2.0` | Seconds before NFC stops `shared_tag_unresolved_effect`. For example, `layers: strobe 2 2 ...` plus `unresolved_effect_duration: 1.0` plays two flashes and stops after 1 second. |
| `shared_spool_warning_effect` | `mmu_RFID_warning` | Name of a `[mmu_led_effect]` to play when the staged spool reaches 80% of its pending timeout. NFC does not pass HH `DURATION`; the effect must remain interruptible when preload starts. |
| `shared_auto_create_effect` | `mmu_RFID_creating` | Name of a `[mmu_led_effect]` to play while Spoolman auto-create is running. |
| `shared_missed_limit` | `3` | Consecutive unresolvable reads before a console error advises `MMU_PRELOAD`. Minimum 1. |
| `force_spool_id` | `true` | Show a blocking-style `[ERROR]` advisory when no spool is staged. |

`mmu_gate` and `scan_enabled` are set internally - do not add them. Only one enabled shared reader may be configured. All Spoolman connection settings and logging settings are inherited from the base `[nfc_gate]` section.

`MMU_SET_LED DURATION=` is used carefully. In Happy Hare, passing `DURATION` sets a per-unit pending-update flag; while that flag is active, later LED effect calls for the same unit are ignored until the timer restores the default LEDs. Because of that, normal shared read, staged-ready, and pending-warning feedback do **not** pass `DURATION`; they must remain interruptible by ready, unresolved, warning, preload start, loaded, cancel, replace, and timeout transitions. Duration is kept for standalone effects such as `NFC_SHARED LED_TEST=1`, unresolved feedback, and bypass-ready confirmation.

The normal shared read effect still has a failsafe. NFC arms a local timer using
`read_effect_duration`; if no ready, unresolved, warning, removal, stop, or
timeout path takes over, NFC releases the LEDs back to Happy Hare with
`MMU_GATE_MAP QUIET=1`.

---

## Happy Hare hook wiring

Add one user extension hook to `mmu_macro_vars.cfg`:

```ini
[gcode_macro _MMU_SEQUENCE_VARS]
; approve the staged shared-reader spool during pregate preload
variable_user_post_preload_extension: '_NFC_SHARED_PRELOAD'
```

`variable_user_post_preload_extension` fires at the start of every pregate load. `PRELOAD_CHECK` is safe to leave wired for all loads; it skips only while printing, and emits an advisory message when no spool is staged.

Do not leave this hook set to `NFC JOG_SCAN=1` when the shared reader is active. That is the per-lane scan-jog hook; Happy Hare may append the loaded gate number to it, which can produce `NFC GATE=<n>` errors and prevent `NFC_SHARED PRELOAD_COMMIT=1` from clearing the staged spool.

Shared polling pauses automatically when printing starts (`idle_timeout:printing`) and resumes when printing completes (`idle_timeout:ready`). No post-unload hook is needed.

---

## LED feedback

Define a named `[mmu_led_effect]` in your LED config:

```ini
[mmu_led_effect mmu_RFID_read]
define_on: gates
layers: strobe 1 3 top (0, 1, 0)

[mmu_led_effect mmu_RFID_ready]
define_on: gates
layers: strobe 1 2 top (0, 1, 0)

[mmu_led_effect mmu_RFID_unresolved]
define_on: gates
layers: strobe 2 2 top (1, 0, 0)
```

Set `shared_tag_read_effect: mmu_RFID_read`, `shared_spool_ready_effect: mmu_RFID_ready`, and `shared_tag_unresolved_effect: mmu_RFID_unresolved` in `[nfc_gate shared]`. Tag detection flashes bright green 3x; auto-create runs a yellow chase; a ready spool ID flashes bright green 2x; an unresolved UID flashes bright red 2x. If auto-create is enabled, keep the tag in front of the reader until the green ready blink appears.

---

## Commands

| Command | What it does |
|---|---|
| `NFC_SHARED READ=1` | Start polling. Refuses to overwrite a pending spool; use `NFC_SHARED REPLACE=1` or `NFC_SHARED CANCEL=1` first. Rejected while printing. |
| `NFC_SHARED READ=0` | Stop polling. Keeps any pending spool. |
| `NFC_SHARED STATUS=1` | Show detailed state — summary, polling flags, deadlines, pending spool, miss counter, LED effect, last action, next action, and last error. |
| `NFC_SHARED SUMMARY=1` | Show one compact state line and next suggested action. |
| `NFC_SHARED HELP=1` | Show shared reader command help. |
| `NFC_SHARED CANCEL=1` | Cancel a staged spool and stop polling. |
| `NFC_SHARED REPLACE=1` | Discard a staged spool and start scanning another. |
| `NFC_SHARED RESET=1` | Clear shared state, restore HH LED control, and restart polling. |
| `NFC_SHARED LED_TEST=1` | Test the configured shared tag-read LED effect. |

Klipper requires `=1` on shared action flags, so commands like
`NFC_SHARED CANCEL` are not valid console syntax.

Advanced shared-reader commands:

| Command | What it does |
|---|---|
| `NFC_SHARED CLEAR=1` | Clear pending state, stop polling, reset the reader. |
| `NFC_SHARED PRELOAD_CHECK=1` | Approve the pending shared-reader spool for the HH preload hook. |
| `NFC_SHARED PRELOAD_COMMIT=1 SPOOL_ID=<id>` | Clear pending state after the hook bridge accepts the staged spool. |
| `NFC_SHARED PRELOAD_CLEAR_ASSIGNED=1 SPOOL_ID=<id>` | Legacy/recovery command to clear shared pending state when HH already has this spool assigned. |
| `NFC_SHARED POLL=1` | Force one full read/resolve cycle. Skips while printing. |
| `NFC_SHARED SCAN=1` | Raw hardware scan — shows UID only, no Spoolman lookup. Skips while printing. |
| `NFC_SHARED INIT=1` | Re-run NFC reader initialisation. Resumes startup polling when enabled and safe. |
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
Check that `variable_user_post_preload_extension: '_NFC_SHARED_PRELOAD'` is set in `mmu_macro_vars.cfg` and that the printer was not actively printing when the preload fired.

Run `NFC_DOCTOR` and confirm it reports the shared preload hook as present. If
the pending spool expired before preload, tap the tag again.

**`NFC_STATUS` shows `expired`.**
The pending timeout elapsed before the preload fired. The expired pending spool is cleared automatically; with `startup_polling: 1`, polling resumes. Tap the tag again. Increase `pending_spool_id_timeout` in `mmu_parameters.cfg` if you regularly take longer than the configured window between tapping and loading.

**Console shows "uid=<uid> not in Spoolman after N attempts".**
The tag is not registered in Spoolman. Either register the spool first or use `MMU_PRELOAD` to load without spool assignment.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
