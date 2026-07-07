# Happy Hare RFID/NFC Reader

NFC spool identification for Happy Hare. Use one NFC reader on each EMU lane, one shared reader inside the MMU body, or both. PN532 remains the default reader; PN7160 is also supported with `reader_type: pn7160`; RC522 is supported as an SPI reader with `reader_type: rc522`.

This is a system-level Klipper integration, not a plug-and-play appliance. It touches Klipper extras, Happy Hare macros, lane MCU firmware, I2C wiring, Spoolman, and optional LED effects. If you are not comfortable recovering from a Klipper config error, reflashing lane MCUs, and reading logs, expect a learning curve.

## Why This Project Is Different

Most DIY spool RFID/NFC systems put a small controller such as an ESP32 near the
reader and report tags over WiFi, MQTT, or another side channel. Others wire the
reader back to the host Raspberry Pi, which often means long SDA/SCL or USB runs
through the printer.

This project keeps the reader inside the MMU wiring domain. Each NFC reader is
connected directly to an existing Klipper MCU I2C bus, usually the MMU board or
the per-lane board inside the MMU. Klipper talks to that MCU, and the plugin
integrates the result with Happy Hare and Spoolman.

Practical hardware choices:

- **EMU / one board per lane:** per-lane readers are a natural fit. Each lane has
  its own MCU/I2C bus, so PN532 and PN7160 are both valid choices.
- **Other B-style multi-material systems with one MMU board:** PN7160 is usually
  the better choice for multiple readers on one bus because it has four
  selectable I2C addresses (`40-43` / `0x28-0x2B`). That matches many four-color
  MMU layouts.
- **Single shared reader:** PN532 or PN7160 can be mounted inside the MMU body
  and used as a tap-before-loading reader.

## Operating Modes

| Mode | Hardware | Best For | Flow |
|---|---|---|---|
| Per-lane readers | One NFC reader per EMU gate/lane MCU (default PN532, optional PN7160) | Automatic spool identification during lane preload | Load spool -> HH parks filament -> NFC scan-jog rotates spool -> Spoolman lookup -> HH gate map |
| Shared reader | One NFC reader mounted inside the MMU body (default PN532, optional PN7160) | Fewer readers, manual tap before loading | Tap tag -> spool staged -> load any gate -> HH pregate preload -> staged spool assigned |
| Hybrid | Per-lane readers plus shared reader | Normal lanes with per-lane readers, plus shared/bypass workflows | Lane readers handle scan-jog; shared reader stages tapped spools for preload |

The shared reader can stage only a real Spoolman spool ID. UID lookup, embedded spool IDs, and auto-created Spoolman records work. Metadata-only tags without a Spoolman spool ID cannot be staged for Happy Hare preload.

## Requirements

- Voron/EMU setup running the [igiannakas IG-dev branch of Happy Hare](https://github.com/igiannakas/Happy-Hare/tree/IG-dev), which provides `variable_user_post_preload_extension`
- One Klipper MCU per filament lane for per-lane reader installs, or one MCU hosting the shared NFC reader
- Supported NFC reader hardware configured for its bus
- Hardware I2C or SPI bus on the MCU hosting the NFC reader
- Klipper version requirements:
  - Strongly recommended: use the latest Klipper on the host and every MCU
    hosting an NFC reader. The NFC drivers use Klipper's newer low-level
    `i2c_transfer` / `i2c_bus_status` path so bus errors such as `START_NACK`
    can be handled on the host side instead of turning reader faults into
    Klipper shutdowns.
  - If checking an older install, it should be newer than upstream commit
    `6bbc9069` (`bus: Note mcu code deprecation if missing i2c_transfer`,
    Feb 24, 2026 on GitHub).
  - After updating Klipper, rebuild and flash every MCU hosting an NFC reader.
- Spoolman reachable from the Pi
- NFC tags on spools: NTAG213/215/216, MIFARE Classic, or supported rich-tag formats
- Lane MCU firmware rebuilt from the same Klipper checkout as the host

## Happy Hare V4 Compatibility

Happy Hare v4 can run the post-preload hook while the MMU reports
`action=checking`. For automatic gate-status polling, NFC treats `checking` as
scan-safe only when the detected Happy Hare major version is 4 or newer. For
the post-preload hook, `_NFC_SCAN_JOG_PRELOAD` sends
`NFC GATE=<n> JOG_SCAN=1 SOURCE=AUTO`; that trusted hook path uses the same
version-aware scan-safe check: Happy Hare v4 accepts `action=idle` or
`action=checking`; Happy Hare v3/pre-v4 and unknown versions accept only
`action=idle`. Manual or console `JOG_SCAN=1` commands without `SOURCE=AUTO`
stay conservative and always require `action=idle`.

Supported readers:

| Reader | `reader_type` | Bus / address | Notes |
|---|---|---|---|
| PN532 | `pn532` | `36` (`0x24`) | Default reader and documented PN532 wiring path |
| PN7160 | `pn7160` | `40-43` (`0x28-0x2B`) | Supports NTAG/Type2, ISO15693/Type5, and authenticated Bambu/MIFARE reads |
| RC522 | `rc522` | SPI (`cs_pin` + `spi_bus`/software SPI pins) | UID lookup, NTAG/Type-2 rich reads, and authenticated MIFARE/Bambu reads; no ISO15693 |

Supported tag formats:

| Tag / data path | PN532 | PN7160 | RC522 | Notes |
|---|---:|---:|---:|---|
| Spoolman UID lookup | Yes | Yes | Yes | Default path. The tag only needs a readable factory UID registered in Spoolman's extra field. |
| NTAG / NFC Type 2 rich tags | Yes | Yes | Yes | NDEF text/URI/MIME/JSON payloads, OpenSpool, OpenTag3D, TigerTag, OpenPrintTag text-compatible payloads, and several manufacturer binary tags. |
| MIFARE Classic rich tags | Yes | Yes | Yes | Bambu factory tags and other MIFARE Classic formats require `tag_parsing: True`; Bambu authenticated reads also require `bambu_reads: True` and `pycryptodome`. |
| ISO15693 / NFC Type 5 rich tags | No | Yes | No | Used by SLIX2 / OpenPrintTag Type-5 tags. Official OpenPrintTag antenna size is best suited to a shared reader; per-lane use needs hardware testing. |

The vendored parser currently recognizes Bambu Lab, ELEGOO, Anycubic ACE,
TigerTag, Creality CFS/K1/K2, QIDI Box, SimplyPrint/QIDI URL tags, OpenTag3D,
OpenSpool, OpenPrintTag, and generic NDEF JSON filament records. See
[Spoolman Integration](docs/shared/spoolman-integration.md) for format details
and auto-create behavior.

## Documentation

| Guide | Purpose |
|---|---|
| [NFC Reader Wiring](docs/i2c-nfc/wiring.md) | PN532 and PN7160 wiring, I2C address, and bus notes |
| [Install & Uninstall](docs/shared/install-uninstall.md) | Installer behavior, includes, updates, removal |
| [First-Time Setup](docs/i2c-nfc/setup.md) | Configure Spoolman, lane hardware, first verification |
| [Shared Reader](docs/shared/shared-reader.md) | Shared-reader workflow, hook wiring, commands, LED behavior |
| [Configuration Reference](docs/shared/configuration.md) | Every config key, defaults, inheritance rules |
| [Commands & Macros](docs/shared/klipper-functions.md) | User commands, shared commands, callback macros |
| [Message Definitions](docs/shared/message_definition.md) | Console output and matching `nfc_reader.log` entries |
| [Spoolman Integration](docs/shared/spoolman-integration.md) | Extra field setup, UID registration, lookup behavior |
| [Troubleshooting](docs/i2c-nfc/troubleshooting.md) | Startup errors, reader failures, tag lookup issues |
| [How It Works](docs/shared/how-it-works.md) | Boot sequence, poll flow, scan-jog, dispatch layers |
| [Expert I2C Debugging](docs/shared/expert-low-level-i2c-debugging.md) | Low-level PN532 probe commands |

## Quick Install

Before installing, rebuild and flash Klipper firmware on every MCU that will host an NFC reader. Host and MCU firmware version mismatches produce I2C failures that look like wiring or reader problems.

```bash
cd ~
git clone https://github.com/cwiegert/Happy-Hare-RFID-Reader.git rfid-reader
cd ~/rfid-reader
bash install.sh
```

Beta cutover note: if an old `~/emu-nfc-reader` install exists, the installer prompts before backing it up/removing it and continuing with a fresh `~/rfid-reader` install.

Add the includes to `printer.cfg` in this order.

Per-lane readers:

```ini
[include nfc/nfc_reader.cfg]
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_hw.cfg]
```

Shared-reader-only:

```ini
[include nfc/nfc_reader.cfg]
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_shared.cfg]
```

Hybrid:

```ini
[include nfc/nfc_reader.cfg]
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_hw.cfg]
[include nfc/nfc_reader_shared.cfg]
```

Set Spoolman in `~/printer_data/config/nfc/nfc_reader.cfg`:

```ini
[nfc_gate]
spoolman_url:      auto
spoolman_rfid_key: rfid_tag
```

For shared-reader installs, wire Happy Hare's post-preload hook:

```ini
[gcode_macro _MMU_SEQUENCE_VARS]
variable_user_post_preload_extension: '_NFC_SHARED_PRELOAD'
```

Restart Klipper and run the doctor:

```bash
sudo systemctl restart klipper
```

```gcode
NFC_DOCTOR
NFC_STATUS
```

## Common Commands

```gcode
NFC_HELP                                      ; show normal command help
NFC_HELP ADVANCED=1 CALLBACKS=1 LOW_LEVEL=1  ; show the full command set
NFC_DOCTOR                                   ; check common config/setup problems
NFC_STATUS                                   ; show every configured lane/shared reader
NFC_REGISTER UID=04A1B2C3D4 SPOOL_ID=123     ; link a known UID to an existing Spoolman spool
NFC_LED_TEST ALL=1 CYCLES=2                  ; chase-test lane tag-read LEDs on all enabled lanes
NFC GATE=0 INIT=1                            ; re-run reader init on one lane reader
NFC GATE=0 SCAN=1                            ; raw UID scan, no Spoolman/HH dispatch
NFC GATE=0 LED_TEST=1 CYCLES=2               ; test lane tag-read LED on one gate
NFC GATE=0 POLL=1                            ; read, resolve, and dispatch once
NFC GATE=0 JOG_SCAN=1                        ; run scan-jog manually; blocked if HH gate is empty
NFC GATE=0 READ=1                            ; start lane background polling
NFC GATE=0 READ=0                            ; stop lane background polling
NFC_SHARED STATUS=1                          ; show shared-reader state
NFC_SHARED READ=1                            ; start shared-reader polling
NFC_SHARED REPLACE=1                         ; discard staged shared spool and scan another
NFC_SHARED CANCEL=1                          ; cancel staged shared spool
```

## Key Defaults

These are the defaults shipped in `config/nfc_reader.cfg`:

| Setting | Default | Notes |
|---|---:|---|
| `startup_polling` | `1` | Start polling after reader init succeeds |
| `poll_interval` | `10` | Per-lane background poll interval in seconds |
| `absent_threshold` | `3` | Missed polls before a removal event |
| `scan_enabled` | `False` | Disables automatic gate-status scan-jog trigger; manual/hook `JOG_SCAN` still works |
| `scan_jog_mm` | `150.0` | Logical scan chunk divided into three stopped-position substeps |
| `scan_jog_max` | unset | Optional fixed scan-jog travel limit; leave unset to use the lane Bowden length |
| `scan_reads_per_position` | `1` | Reads per stopped scan position |
| `scan_poll_interval` | `0.25` | Read spacing during scan-jog and shared-reader polling cadence |
| `scan_motion_mode` | `continuous` | `continuous` queues direct Happy Hare gear moves and polls while each chunk is moving; `stopped` keeps blocking substep reads |
| `scan_continuous_step_mm` | `150.0` | Continuous-mode forward chunk size. Rich reads use the observed UID hit-window center, so this no longer needs to be kept small for rich-tag positioning |
| `scan_continuous_speed` | `200.0` | Continuous-mode gear move speed |
| `scan_continuous_accel` | `2000.0` | Continuous-mode gear move acceleration |
| `scan_continuous_poll_interval` | `0.05` | In-flight NFC read cadence while a continuous chunk is estimated to be moving |
| `debug` | `2` | Warnings and errors in `nfc_reader.log` |
| `console_output` | `False` | Keep routine NFC logs out of the console |

To try continuous scan-jog:

```ini
[nfc_gate]
scan_motion_mode: continuous
scan_continuous_step_mm: 150.0
scan_continuous_speed: 200.0
scan_continuous_accel: 2000.0
scan_continuous_poll_interval: 0.05
```

Continuous mode preserves the existing tag-found flow: the read-light effect
plays for about 0.1 second, then NFC rewinds and dispatches the cached tag/spool
action just like stopped mode.
If a UID is found during motion, continuous mode waits for the current chunk to
finish and checks Spoolman first. If rich tag parsing is still needed, NFC
recenters to the observed UID hit-window center before running rich parsing and
the normal rich-read retry sweep.

Set `enabled: False` on a `[nfc_gate laneN]` or `[nfc_gate shared]` section to keep the config block in place without initializing hardware or registering commands for that reader.

## MCU Firmware Warning

The NFC reader driver talks to firmware running on the MCU, not only to Klipper on the Pi. After updating Klipper, rebuild and flash every lane/shared MCU that hosts an NFC reader before debugging NFC.

Recommended update order:

```text
1. Update the Klipper host checkout.
2. Build MCU firmware from that same checkout.
3. Flash every MCU that hosts an NFC reader.
4. Restart Klipper.
5. Confirm all MCUs reconnect before testing NFC.
```

When host and MCU firmware are out of sync, common symptoms include reader init failures, `i2c_read_response` timeouts, and other devices on the same I2C bus failing unexpectedly.

## License

Copyright (C) 2026 WoodWorker.
Licensed under [GNU General Public License v3.0 or later](https://www.gnu.org/licenses/gpl-3.0.html).
See [LICENSE](LICENSE) for the full terms.
