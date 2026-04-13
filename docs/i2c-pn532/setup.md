# PN532 I2C Setup

[← Back to README](../../Readme.md) | [Wiring →](wiring.md)

This guide covers first-time setup of the supported path:

```
one PN532 per lane MCU / EBB42  →  Klipper I2C  →  NFC_Manager  →  Spoolman  →  Happy Hare
```

---

> [!CAUTION]
> ## 🔴 Flash Klipper Firmware on Every Lane MCU First
>
> Before you configure or test anything, update and flash Klipper firmware on every EBB42 / lane MCU that will carry a PN532. The PN532 driver depends on Klipper MCU I2C behaviour — not just the host-side Python. Stale MCU firmware produces failures that look like hardware problems:
>
> - ACK reads fail immediately after the ready byte succeeds
> - `Unable to obtain 'i2c_read_response' response` errors in Klipper logs
> - BME280 on the same bus starts timing out after PN532 is wired
> - Hardware I2C hangs while software I2C returns partial data
>
> **Build and flash each lane MCU before debugging PN532 behaviour.**

---

## Prerequisites

Before starting:

- [ ] Happy Hare is installed and `[mcu lane0]`, `[mcu lane1]` etc. exist in Klipper config.
- [ ] Each lane MCU is on the CAN bus and connecting cleanly on Klipper startup.
- [ ] PN532 modules are wired and set to I2C mode — see [Wiring](wiring.md).
- [ ] Lane MCU / EBB42 firmware has been rebuilt and flashed.
- [ ] A Spoolman instance is reachable from the Pi.

---

## Step 1 — Install

If you have not already installed, follow [Install & Uninstall](../shared/install-uninstall.md).

Quick reference:

```bash
cd ~
git clone --filter=blob:none --sparse git@github.com:<your-github-username>/NFC-Reader.git emu-nfc-reader
cd ~/emu-nfc-reader
git sparse-checkout set klippy config docs tools
bash install.sh
```

---

## Step 2 — Add Includes to `printer.cfg`

Add these three lines in this exact order:

```ini
[include NFC/nfc_vars.cfg]
[include NFC/nfc_macros.cfg]
[include NFC/pn532_i2C.cfg]
```

**Order is required.** `nfc_vars.cfg` defines the base `[nfc_gate]` section. Each `[nfc_gate laneN]` section in `pn532_i2C.cfg` inherits from it. Including the lane file before the base section is defined will cause a Klipper config error.

---

## Step 3 — Configure Spoolman

Edit `~/printer_data/config/NFC/nfc_vars.cfg`.

The minimum required settings:

```ini
[nfc_gate]
spoolman_url:      auto
spoolman_rfid_key: rfid_tag
```

| Setting | Value | Notes |
|---|---|---|
| `spoolman_url` | `auto` | Moonraker reads the Spoolman URL from its own config. Use this when Moonraker has a `[spoolman]` section. |
| `spoolman_url` | `http://host:7912` | Direct URL. Use when testing, or when Moonraker does not expose the Spoolman URL. |
| `spoolman_rfid_key` | `rfid_tag` | Must match the extra field name you created in Spoolman. |

See [Spoolman Integration](../shared/spoolman-integration.md) for how to create the extra field and register UIDs on spool records.

---

## Step 4 — Configure Lane Hardware

Edit `~/printer_data/config/NFC/pn532_i2C.cfg`.

Each lane needs one section. The default file includes four lanes — adjust or extend to match your printer:

```ini
[nfc_gate lane0]
mmu_gate:   0
i2c_mcu:    lane0
i2c_bus:    i2c3_PB3_PB4
```

| Key | Required | Value |
|---|---|---|
| `mmu_gate` | Yes | Happy Hare gate number (integer, 0-based) |
| `i2c_mcu` | Yes | Klipper MCU name — must match an `[mcu laneN]` section |
| `i2c_bus` | Yes | Hardware I2C bus name on that MCU. For PB3/PB4 use `i2c3_PB3_PB4` |

> [!NOTE]
> `i2c_mcu` must exactly match the MCU name as Klipper knows it. These names come from Happy Hare's `mmu_hardware.cfg`, typically `lane0`, `lane1`, etc. A mismatch produces a Klipper startup error.

All polling, Spoolman, timing, and logging settings are inherited from the base `[nfc_gate]` section in `nfc_vars.cfg`. Override any of them inside a specific lane section if needed:

```ini
[nfc_gate lane2]
mmu_gate:         2
i2c_mcu:          lane2
i2c_bus:          i2c3_PB3_PB4
debug:            2          ; verbose logging for this lane only
startup_polling:  1          ; optional: auto-start this lane after init
startup_poll_delay: 4.0      ; optional: stagger first startup poll
```

See [Configuration Reference](../shared/configuration.md) for a complete list of all available settings.

---

## Step 5 — Restart Klipper

```bash
sudo systemctl restart klipper
```

Watch the Klipper log (`~/printer_data/logs/klippy.log`) for NFC startup messages. Errors at this stage are almost always config typos or a missing lane MCU.

---

## Step 6 — Verify

### Check all gates

```gcode
NFC_GATE_STATUS
```

Expected output with no tags present:

```
NFC gate status  (4 gates configured):
  Gate 0  [lane0]:  empty
  Gate 1  [lane1]:  empty
  Gate 2  [lane2]:  empty
  Gate 3  [lane3]:  empty
```

### Initialise a specific lane

```gcode
NFC_GATE NAME=lane0 INIT=1
```

This runs the PN532 `GetFirmwareVersion` and `SAMConfiguration` sequence. If the reader is wired correctly and the MCU firmware is current, it completes without error.

### Hardware scan

```gcode
NFC_GATE NAME=lane0 SCAN=1
```

Reads the hardware once and reports the raw tag identity — no Spoolman lookup, no state machine. Hold an NFC tag above the reader and run this command. You should see the UID printed in the Klipper console.

### Full poll

```gcode
NFC_GATE NAME=lane0 POLL=1
```

Runs one complete NFC_Manager cycle: PN532 read → UID normalization → Spoolman lookup → state machine update → macro dispatch if the state changed. Use this to test the full pipeline end-to-end.

---

## Step 7 — Start Background Polling

Once you have confirmed at least one lane works end-to-end:

```gcode
NFC_GATE NAME=lane0 READ=1
```

To start all lanes, run `READ=1` for each. Background polling runs at `poll_interval` seconds (default 30).

By default, `startup_polling: -1` keeps polling manual-start only. Set `startup_polling: 1` in the base `[nfc_gate]` section or in a specific `[nfc_gate laneN]` section if you want polling to begin automatically after PN532 init succeeds.

When enabling startup polling on multiple lanes, stagger `startup_poll_delay` per lane so all readers do not enter their first poll cycle at the same time:

```ini
[nfc_gate lane0]
startup_polling:    1
startup_poll_delay: 0.0

[nfc_gate lane1]
startup_polling:    1
startup_poll_delay: 2.0

[nfc_gate lane2]
startup_polling:    1
startup_poll_delay: 4.0
```

---

## Updating

```bash
cd ~/emu-nfc-reader
git pull
bash install.sh
sudo systemctl restart klipper
```

> [!IMPORTANT]
> If the update touches Klipper MCU protocol (the Klipper changelog will say so), rebuild and flash every lane MCU firmware before restarting Klipper.

---

## SPI Status

SPI reader support is work in progress and is not part of this documented setup. Do not include any SPI config files alongside the I2C files above.
