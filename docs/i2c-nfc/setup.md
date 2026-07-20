# First-Time Setup

[← Install](../shared/install-uninstall.md) | [Spoolman Setup →](../shared/spoolman-integration.md)

This guide assumes you have:
- [Wired the NFC reader](wiring.md)
- [Installed the software](../shared/install-uninstall.md)
- Rebuilt and flashed Klipper firmware on every lane MCU

If you skipped any of those, do them first.

---

## Step 1 — Add Includes to `printer.cfg`

Add these three lines in this exact order:

```ini
[include nfc/nfc_reader.cfg]
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_hw.cfg]
```

`nfc_reader.cfg` must come first — it defines the base `[nfc_gate]` section that each `[nfc_gate laneN]` in `nfc_reader_hw.cfg` inherits from. Reversing the order causes a Klipper config error on startup.

---

## Step 2 — Configure Spoolman

Edit `~/printer_data/config/nfc/nfc_reader.cfg`:

```ini
[nfc_gate]
spoolman_url:      auto
spoolman_rfid_key: rfid_tag
```

| Setting | Value | When to use |
|---|---|---|
| `spoolman_url: auto` | Reads URL from Moonraker | Use this when `moonraker.conf` has a `[spoolman]` section |
| `spoolman_url: http://host:7912` | Direct URL | Use when testing, or if `auto` isn't working |
| `spoolman_rfid_key: rfid_tag` | Extra field name | Must match what you create in Spoolman Settings |

See [Spoolman Integration](../shared/spoolman-integration.md) — you need to create the extra field in Spoolman and register each tag UID before spool detection will work.

---

## Step 3 — Configure Lane Hardware

Edit `~/printer_data/config/nfc/nfc_reader_hw.cfg`. The default file has four lanes; adjust to match your printer:

```ini
[nfc_gate lane0]
enabled:    True
mmu_gate:   0
i2c_mcu:    mmu0

[nfc_gate lane1]
enabled:    True
mmu_gate:   1
i2c_mcu:    mmu1
```

| Key | Required | Value |
|---|:---:|---|
| `mmu_gate` | Yes | Happy Hare gate number (0-based integer) |
| `i2c_mcu` | Yes | Klipper MCU name — must match an `[mcu laneN]` in your config |
| `enabled` | No | Defaults to `True`. Set `False` to keep a future lane template without creating hardware. |
| `reader_type` | Inherited unless overridden | `pn532` by default. Set `pn7160`, `rc522`, or `pn5180` for the installed reader. |
| `i2c_address` | Inherited unless overridden | PN532 uses `36`; PN7160 must use `40-43`. Not used by RC522 or PN5180. |
| `i2c_bus` | Inherited unless overridden | I2C bus name on that MCU — use `i2c3_PB3_PB4` for PB3/PB4 on EBB42 or set it once in `[nfc_gate]`. Not used by RC522 or PN5180. |

| `spi_bus`, `cs_pin`, `spi_speed` | SPI readers | Required for RC522 and PN5180. Use `500000` for hardware SPI or `100000` for software SPI. |
| `reset_pin`, `busy_pin` | PN5180 | Required PN5180 RST and active-high BUSY GPIO. See [PN5180 wiring](pn5180-wiring.md). |

> [!NOTE]
> `i2c_mcu` must exactly match the MCU name Klipper uses. These names come from Happy Hare's `mmu_hardware.cfg`, typically `lane0`, `lane1`, etc. A mismatch causes a Klipper startup error.

> [!IMPORTANT]
> **Temperature sensor I2C bus must match.** If your lane MCU has a thermistor or temperature sensor on I2C (e.g. an SHT3x), configure it on the **same I2C bus** as the NFC reader. PN532 should use hardware I2C. PN7160 supports software I2C, but hardware I2C is recommended because software I2C increases MCU load.

PN7160 lane example:

```ini
[nfc_gate lane1]
enabled:     True
reader_type: pn7160
i2c_address: 40
mmu_gate:    1
i2c_mcu:     mmu1
```

All polling, timing, and logging settings are inherited from the base `[nfc_gate]` in `nfc_reader.cfg`. Override per-lane only if you need different behavior on a specific lane:

```ini
[nfc_gate lane2]
enabled:    True
mmu_gate:   2
i2c_mcu:    mmu2
debug:      2              ; verbose logging on this lane only
```

---

## Step 4 — Restart Klipper

```bash
sudo systemctl restart klipper
```

Watch the log for NFC startup messages:

```bash
tail -f ~/printer_data/logs/nfc_reader.log
```

Errors at this stage are almost always config typos or a missing/mismatched lane MCU name.

---

## Step 5 — Verify Each Reader

### 1. Check all gates

```gcode
NFC_STATUS
```

Expected with no tags loaded:
```
NFC gate status  (4 gates configured):
  Gate 0  [lane0]:  empty
  Gate 1  [lane1]:  empty
  Gate 2  [lane2]:  empty
  Gate 3  [lane3]:  empty
```

### 2. Watch the startup console output

When Klipper connects, each lane initialises automatically and reports to the console. Look for:

```
[OK] NFC[lane0]: ready.  HH seed: spool_id=42  Startup polling is enabled; first poll in 0.0s.
```

or, if the gate was empty in Happy Hare:

```
[OK] NFC[lane0]: ready.  HH reports gate empty  Run NFC GATE=0 READ=1 to start polling.
```

**The HH seed line is important.** It means NFC_Manager read Happy Hare's gate map on startup and pre-loaded the lane cache with the spool HH already knows about. The first poll will verify the physical tag matches that spool — if it does, no redundant dispatch is sent to Happy Hare. If the spool was swapped while Klipper was down, the mismatch is detected and `_NFC_SPOOL_CHANGED` fires normally.

If Happy Hare wasn't ready when the NFC init ran, the seed step is skipped. Run `NFC_HH_SYNC_CACHE` to manually re-seed all lanes from the current HH gate map.

### 3. Initialize a lane manually

```gcode
NFC GATE=0 INIT=1
```

This runs the configured NFC reader initialization. Expected output:
```
[OK] NFC[lane0]: reader OK
```

If it fails, check [Troubleshooting](troubleshooting.md).

### 4. Hardware scan

Hold an NFC tag near the reader, then:

```gcode
NFC GATE=0 SCAN=1
```

The UID prints to the console. This is a raw hardware read — no Spoolman lookup, no Happy Hare update.

### 5. Full pipeline test

With a registered tag (see [Spoolman Integration](../shared/spoolman-integration.md)):

```gcode
NFC GATE=0 POLL=1
```

Expected result:
```
NFC[lane0]: one poll complete; Gate 0:  spool 42  UID 04AABBCCDD ...
```

This runs the full chain: NFC reader read → Spoolman lookup → state update → Happy Hare macro. If this works, the pipeline is complete.

---

## Step 6 — Enable Background Polling

Once a lane works end-to-end, start automatic polling:

```gcode
NFC GATE=0 READ=1
```

To start all lanes, run `READ=1` for each. Polling runs at the `poll_interval` (default: 10 seconds).

**Optional: automatic polling on startup.**
To have lanes start polling automatically after Klipper boots, set `startup_polling: 1`. The shipped `nfc_reader_hw.cfg` staggers the per-lane startup delays by 0.5 seconds so all readers don't poll simultaneously:

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

## Next Steps

- [Spoolman Integration](../shared/spoolman-integration.md) — register your tag UIDs
- [Commands & Macros](../shared/klipper-functions.md) — full command reference
- [Configuration Reference](../shared/configuration.md) — tune polling, logging, and timing

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
