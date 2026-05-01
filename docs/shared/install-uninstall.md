# Install & Uninstall

[← README](../../Readme.md) | [Wiring](../i2c-pn532/wiring.md) | [Setup →](../i2c-pn532/setup.md)

---

## How the Install Works

The installer creates **symlinks** — it does not copy Python files into the Klipper directory. This means:

- Updating is just `git pull` — the new code is live immediately, no file copy needed
- Config files in `~/printer_data/config/nfc/` are yours — the installer never overwrites sections you have already edited
- Running `bash install.sh` again after an update is always safe

**What gets created:**

```
~/klipper/klippy/extras/nfc_gate.py    →  symlink → repo/klippy/extras/nfc_gate.py
~/klipper/klippy/extras/nfc_gates/     →  symlink → repo/klippy/extras/nfc_gates/
~/printer_data/config/nfc/nfc_reader.cfg
~/printer_data/config/nfc/nfc_macros.cfg
~/printer_data/config/nfc/nfc_reader_hw.cfg
```

Config files use a non-destructive merge: if a section already exists in your file, it is left alone. Only missing sections are appended.

---

## Install

### Step 1 — Flash Lane MCU Firmware

> [!CAUTION]
> Do this before anything else. The PN532 driver communicates directly with the EBB42 MCU firmware over I2C. If the host Klipper and the MCU firmware are on different versions, I2C reads fail with errors that look like hardware faults.
>
> Build and flash Klipper firmware for every lane MCU / EBB42 that will carry a PN532 before you run the installer.

### Step 2 — Clone the Repository

SSH to the Pi:

```bash
cd ~
git clone --filter=blob:none --sparse git@github.com:<your-github-username>/NFC-Reader.git emu-nfc-reader
cd ~/emu-nfc-reader
git sparse-checkout set klippy config docs tools
```

The sparse checkout skips any large binary assets and keeps only what the Pi and Klipper need.

### Step 3 — Run the Installer

```bash
bash install.sh
```

The installer prints what it creates. If it cannot find the Klipper extras directory or the printer config directory, it exits with an error before making any changes.

### Step 4 — Add Includes to `printer.cfg`

Open `~/printer_data/config/printer.cfg` and add these three lines **in this exact order**:

```ini
[include nfc/nfc_reader.cfg]
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_hw.cfg]
```

Order is required. `nfc_reader.cfg` defines the base `[nfc_gate]` section that each lane section inherits from. Including the lane file first causes a Klipper startup error.

### Step 5 — Configure Spoolman

Edit `~/printer_data/config/nfc/nfc_reader.cfg`:

```ini
[nfc_gate]
spoolman_url:      auto
spoolman_rfid_key: rfid_tag
```

**`spoolman_url: auto`** works when Moonraker has a `[spoolman]` section configured. Use a direct URL if you need to point at a specific instance:

```ini
spoolman_url: http://192.168.1.50:7912
```

See [Spoolman Integration](spoolman-integration.md) for how to create the extra field and register UIDs.

### Step 6 — Configure Lane Hardware

Edit `~/printer_data/config/nfc/nfc_reader_hw.cfg`. The default file has four lanes — adjust the MCU names and gate numbers to match your Happy Hare setup:

```ini
[nfc_gate lane0]
mmu_gate:   0
i2c_mcu:    lane0
i2c_bus:    i2c3_PB3_PB4
```

`i2c_mcu` must exactly match the MCU name in your Happy Hare config (from `mmu_hardware.cfg`), typically `lane0`, `lane1`, etc.

> [!IMPORTANT]
> **Temperature sensor I2C bus must match.** If your lane MCU also has a thermistor or temperature sensor connected over I2C (e.g. an SHT3x), it must be configured on the **same hardware I2C bus** as the PN532. Set `i2c_bus` in your temperature sensor section to the same value as the `i2c_bus` in the matching `[nfc_gate laneN]` section (or the base `[nfc_gate]` if all lanes share one bus). Using a different bus, or using the Klipper software-emulated I2C bus (`i2c_software_*`), will cause collisions or read failures on both devices. Hardware I2C is required.

See [Configuration Reference](configuration.md) for all available settings.

### Step 7 — Restart and Verify

```bash
sudo systemctl restart klipper
```

```gcode
NFC_STATUS
```

Expected output with no tags loaded:

```
NFC gate status  (4 gates configured):
  Gate 0  [lane0]:  empty
  Gate 1  [lane1]:  empty
  Gate 2  [lane2]:  empty
  Gate 3  [lane3]:  empty
```

If you see errors, check [Troubleshooting](../i2c-pn532/troubleshooting.md).

---

## Moonraker Update Manager

Add this block to `~/printer_data/config/moonraker.conf` so Fluidd/Mainsail can update the NFC reader alongside Klipper:

```ini
[update_manager emu_nfc_reader]
type:             git_repo
path:             ~/emu-nfc-reader
origin:           git@github.com:<your-github-username>/NFC-Reader.git
primary_branch:   main
managed_services: klipper
install_script:   install.sh
```

Restart Moonraker:

```bash
sudo systemctl restart moonraker
```

> [!NOTE]
> The update manager runs `install.sh` automatically after pulling. Since the Python extras are symlinks, new code is live immediately after the pull. You still need to **manually rebuild and flash lane MCU firmware** when a Klipper MCU protocol change is included in an update — the update notes will say so.

---

## Updating

```bash
cd ~/emu-nfc-reader
git pull
bash install.sh
sudo systemctl restart klipper
```

If the update notes mention a Klipper MCU protocol change, rebuild and flash each lane MCU before restarting Klipper.

---

## Uninstall

```bash
cd ~/emu-nfc-reader
bash uninstall.sh
```

The uninstaller:

1. Removes the `nfc_gate.py` symlink from Klipper extras
2. Removes the `nfc_gates/` symlink from Klipper extras
3. Removes legacy `~/pn532_scan.py` if an older installer placed it there
4. Moves `~/printer_data/config/nfc/` to `nfc_removed_<timestamp>/` (your config is preserved, not deleted)
5. Restarts Klipper

**Manual steps after uninstalling** — the uninstaller cannot edit your config files:

Remove these three lines from `printer.cfg`:
```ini
[include nfc/nfc_reader.cfg]
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_hw.cfg]
```

Remove the update manager block from `moonraker.conf`, then restart Moonraker:
```bash
sudo systemctl restart moonraker
```

Delete the config backup when you no longer need it:
```bash
rm -rf ~/printer_data/config/nfc_removed_*
```

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
