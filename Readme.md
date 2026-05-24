# EMU NFC Gate Reader

NFC spool identification for Happy Hare. Use one PN532 reader on each EMU lane, one shared reader inside the MMU body, or both.

This is a system-level Klipper integration, not a plug-and-play appliance. It touches Klipper extras, Happy Hare macros, lane MCU firmware, I2C wiring, Spoolman, and optional LED effects. If you are not comfortable recovering from a Klipper config error, reflashing lane MCUs, and reading logs, expect a learning curve.

## Operating Modes

| Mode | Hardware | Best For | Flow |
|---|---|---|---|
| Per-lane readers | One PN532 per EMU gate/lane MCU | Automatic spool identification during lane preload | Load spool -> HH parks filament -> NFC scan-jog rotates spool -> Spoolman lookup -> HH gate map |
| Shared reader | One PN532 mounted inside the MMU body | Fewer readers, manual tap before loading | Tap tag -> spool staged -> load any gate -> HH pregate preload -> staged spool assigned |
| Hybrid | Per-lane readers plus shared reader | Normal lanes with per-lane readers, plus shared/bypass workflows | Lane readers handle scan-jog; shared reader stages tapped spools for preload |

The shared reader can stage only a real Spoolman spool ID. UID lookup, embedded spool IDs, and auto-created Spoolman records work. Metadata-only tags without a Spoolman spool ID cannot be staged for Happy Hare preload.

## Requirements

- Voron/EMU setup running the [igiannakas IG-dev branch of Happy Hare](https://github.com/igiannakas/Happy-Hare/tree/IG-dev), which provides `variable_user_post_preload_extension`
- One Klipper MCU per filament lane for per-lane reader installs, or one MCU hosting the shared PN532
- PN532 NFC reader modules configured for I2C mode
- Hardware I2C bus on the MCU; software I2C is not supported
- Spoolman reachable from the Pi
- NFC tags on spools: NTAG213/215/216, MIFARE Classic, or supported rich-tag formats
- Lane MCU firmware rebuilt from the same Klipper checkout as the host

## Documentation

| Guide | Purpose |
|---|---|
| [Wiring](docs/i2c-pn532/wiring.md) | PN532 I2C mode, pin connections, pull-ups, bus notes |
| [Install & Uninstall](docs/shared/install-uninstall.md) | Installer behavior, includes, updates, removal |
| [First-Time Setup](docs/i2c-pn532/setup.md) | Configure Spoolman, lane hardware, first verification |
| [Shared Reader](docs/shared/shared-reader.md) | Shared-reader workflow, hook wiring, commands, LED behavior |
| [Configuration Reference](docs/shared/configuration.md) | Every config key, defaults, inheritance rules |
| [Commands & Macros](docs/shared/klipper-functions.md) | User commands, shared commands, callback macros |
| [Message Definitions](docs/shared/message_definition.md) | Console output and matching `nfc_reader.log` entries |
| [Spoolman Integration](docs/shared/spoolman-integration.md) | Extra field setup, UID registration, lookup behavior |
| [Troubleshooting](docs/i2c-pn532/troubleshooting.md) | Startup errors, PN532 failures, tag lookup issues |
| [How It Works](docs/shared/how-it-works.md) | Boot sequence, poll flow, scan-jog, dispatch layers |
| [Expert I2C Debugging](docs/shared/expert-low-level-i2c-debugging.md) | Low-level PN532 probe commands |

## Quick Install

Before installing, rebuild and flash Klipper firmware on every MCU that will host a PN532. Host and MCU firmware version mismatches produce I2C failures that look like wiring or PN532 problems.

```bash
cd ~
git clone https://github.com/cwiegert/HH-RFID-Reader.git emu-nfc-reader
cd ~/emu-nfc-reader
bash install.sh
```

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
NFC GATE=0 INIT=1                            ; re-run PN532 init on one lane reader
NFC GATE=0 SCAN=1                            ; raw UID scan, no Spoolman/HH dispatch
NFC GATE=0 POLL=1                            ; read, resolve, and dispatch once
NFC GATE=0 JOG_SCAN=1                        ; run scan-jog manually
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
| `startup_polling` | `1` | Start polling after PN532 init succeeds |
| `poll_interval` | `10` | Per-lane background poll interval in seconds |
| `absent_threshold` | `3` | Missed polls before a removal event |
| `scan_enabled` | `False` | Disables automatic gate-status scan-jog trigger; manual/hook `JOG_SCAN` still works |
| `scan_jog_mm` | `150.0` | Logical scan chunk divided into three stopped-position substeps |
| `scan_reads_per_position` | `1` | Reads per stopped scan position |
| `scan_poll_interval` | `0.25` | Read spacing during scan-jog and shared-reader polling cadence |
| `debug` | `2` | Warnings and errors in `nfc_reader.log` |
| `console_output` | `False` | Keep routine NFC logs out of the console |

Set `enabled: False` on a `[nfc_gate laneN]` or `[nfc_gate shared]` section to keep the config block in place without initializing hardware or registering commands for that reader.

## MCU Firmware Warning

The PN532 driver talks to firmware running on the MCU, not only to Klipper on the Pi. After updating Klipper, rebuild and flash every lane/shared MCU that hosts a PN532 before debugging NFC.

Recommended update order:

```text
1. Update the Klipper host checkout.
2. Build MCU firmware from that same checkout.
3. Flash every MCU that hosts a PN532.
4. Restart Klipper.
5. Confirm all MCUs reconnect before testing NFC.
```

When host and MCU firmware are out of sync, common symptoms include PN532 ACK failures, `i2c_read_response` timeouts, and other devices on the same I2C bus failing unexpectedly.

## License

Copyright (C) 2026 WoodWorker.
Licensed under [GNU General Public License v3.0 or later](https://www.gnu.org/licenses/gpl-3.0.html).
See [LICENSE](LICENSE) for the full terms.
