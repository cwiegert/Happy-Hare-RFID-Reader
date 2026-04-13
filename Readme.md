# EMU NFC Gate Reader

> Automatic spool detection for Happy Hare / EMU gates using PN532 NFC readers on Klipper lane MCUs.

Each filament gate gets a dedicated PN532 NFC reader wired directly to the lane MCU's I2C bus. When a spool carrying an NFC tag is loaded, the reader detects it, looks the UID up in Spoolman, and updates the Happy Hare gate map automatically — no manual spool selection needed.

```
PN532 on EBB42 → Klipper I2C → NFC_Manager → Spoolman lookup → MMU_SPOOLMAN
```

---

> [!CAUTION]
> ## 🔴 Update Klipper Firmware on Every Lane MCU
>
> Updating the Klipper host checkout is **not enough**.
>
> The PN532 driver talks directly to Klipper MCU firmware over I2C. If the lane MCU / EBB42 is still running older firmware while the host has been updated, I2C transactions can fail in ways that look like hardware problems: ACK reads fail, `i2c_read_response` timeouts appear, or the BME280 on the same bus starts misbehaving.
>
> **Every time you update Klipper, rebuild and flash each lane MCU.**
>
> ```
> 1. git pull  (update the Klipper host checkout)
> 2. Build MCU firmware for each EBB42 / lane board
> 3. Flash each lane MCU
> 4. sudo systemctl restart klipper
> 5. Confirm lane MCUs reconnect before testing NFC
> ```

---

## Documentation

| Guide | What it covers |
|---|---|
| [Install & Uninstall](docs/shared/install-uninstall.md) | Clone, install, Moonraker update manager, uninstall |
| [Wiring](docs/i2c-pn532/wiring.md) | PN532 to EBB42 pin connections, mode selection, pullups |
| [Setup](docs/i2c-pn532/setup.md) | printer.cfg includes, lane configuration, first boot |
| [Configuration Reference](docs/shared/configuration.md) | Every setting documented with values and behaviour |
| [Klipper Commands & Macros](docs/shared/klipper-functions.md) | All GCode commands and the Happy Hare macro boundary |
| [Spoolman Integration](docs/shared/spoolman-integration.md) | Extra field setup, UID registration, lookup behaviour |
| [Troubleshooting](docs/i2c-pn532/troubleshooting.md) | Failure patterns, diagnostics, systematic checks |
| [Expert: Low-Level I2C Debugging](docs/shared/expert-low-level-i2c-debugging.md) | Manual PN532 bus commands for bring-up |
| [Architecture Decisions](docs/shared/architecture-decisions.md) | Why the system is designed the way it is |

---

## Quick Install

Clone on the Klipper host (Pi):

```bash
cd ~
git clone --filter=blob:none --sparse git@github.com:<your-github-username>/NFC-Reader.git emu-nfc-reader
cd ~/emu-nfc-reader
git sparse-checkout set klippy config docs tools
bash install.sh
```

Add to `printer.cfg`:

```ini
[include NFC/nfc_vars.cfg]
[include NFC/nfc_macros.cfg]
[include NFC/pn532_i2C.cfg]
```

Configure Spoolman in `~/printer_data/config/NFC/nfc_vars.cfg`:

```ini
[nfc_gate]
spoolman_url:      auto
spoolman_rfid_key: rfid_tag
```

Add the Moonraker update manager block to `~/printer_data/config/moonraker.conf`:

```ini
[update_manager emu_nfc_reader]
type:             git_repo
path:             ~/emu-nfc-reader
origin:           git@github.com:<your-github-username>/NFC-Reader.git
primary_branch:   main
managed_services: klipper
install_script:   install.sh
```

Restart Klipper and Moonraker:

```bash
sudo systemctl restart klipper moonraker
```

Verify:

```gcode
NFC_GATE_STATUS
```

See [Install & Uninstall](docs/shared/install-uninstall.md) for the full first-boot checklist and uninstall steps.

---

## Architecture

The system is layered. Each layer owns exactly one responsibility and must not reach across the boundary.

| Layer | File | Owns | Does not own |
|---|---|---|---|
| **PN532Driver** | `pn532_driver.py` | PN532 wire protocol, I2C frames, UID extraction | Spoolman, gate policy, Happy Hare commands |
| **SpoolmanClient** | `spoolman_client.py` | UID → spool record lookup and cache | Gate state, lane assignment, MMU commands |
| **NFC_Manager** | `NFC_manager.py` | Gate state machine, changed/removed decisions, macro dispatch | PN532 protocol details |
| **nfc_macros.cfg** | `nfc_macros.cfg` | Happy Hare-facing GCode calls | NFC bus reads, Spoolman HTTP lookups |

Tags are identified by factory UID only. Tags are never written to. The UID is stored as a Spoolman spool extra field. NFC_Manager fires one of three macros when gate state changes:

```
_NFC_SPOOL_CHANGED  GATE=<n>  SPOOL_ID=<id>  UID=<uid>
_NFC_SPOOL_REMOVED  GATE=<n>
_NFC_TAG_NO_SPOOL   GATE=<n>  UID=<uid>
```

The macros in `nfc_macros.cfg` translate these events into `MMU_SPOOLMAN UPDATE=1` calls. You can edit the macros to match your Happy Hare version without touching Python.

---

## Runtime Flow

```
Tag detected by PN532
        │
        ▼
PN532Driver extracts UID, passes it to NFC_Manager
        │
        ▼
NFC_Manager checks gate state — is this a new UID, the same UID, or absence?
        │
        ▼
If new UID: SpoolmanClient resolves UID → spool_id (cached)
        │
        ▼
NFC_Manager updates gate state machine
        │
        ▼
On state change: dispatch _NFC_SPOOL_CHANGED / _NFC_SPOOL_REMOVED / _NFC_TAG_NO_SPOOL
        │
        ▼
nfc_macros.cfg calls MMU_SPOOLMAN UPDATE=1 on Happy Hare
```

---

## Commands

### Status

```gcode
NFC_GATE_STATUS                   ; show all gates
NFC_GATE NAME=lane0 STATUS=1      ; show one gate
```

### Lane operations

```gcode
NFC_GATE NAME=lane0 INIT=1        ; initialise PN532
NFC_GATE NAME=lane0 SCAN=1        ; one hardware read, no state machine
NFC_GATE NAME=lane0 POLL=1        ; one full poll including Spoolman lookup
NFC_GATE NAME=lane0 APPLY=1       ; force cached spool to Happy Hare
NFC_GATE NAME=lane0 CLEAR_CACHE=1 ; clear cached spool_id, no HH dispatch
NFC_GATE NAME=lane0 READ=1        ; start reactor timer polling
NFC_GATE NAME=lane0 READ=0        ; stop reactor timer polling
```

### Expert low-level debug

Enable in `nfc_vars.cfg`:

```ini
[nfc_gate]
low_level_debug: True
```

```gcode
NFC_GATE NAME=lane0 HELP=1        ; list all available debug steps
NFC_GATE NAME=lane0 STEP=WAKEUP   ; manual PN532 init sequence
```

See [Expert: Low-Level I2C Debugging](docs/shared/expert-low-level-i2c-debugging.md).

---

## SPI Support

SPI reader support is work in progress. SPI config files may be present in the repo but they are not part of the documented or supported install path. Do not include SPI config files alongside the I2C config files.

---

## License

See [LICENSE](LICENSE).
