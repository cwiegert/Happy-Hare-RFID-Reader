# Install & Uninstall

[← README](../../Readme.md) | [Wiring](../i2c-nfc/wiring.md) | [Setup →](../i2c-nfc/setup.md)

---

## How the Install Works

The installer creates **symlinks** — it does not copy Python files into the Klipper directory. This means:

- Updating is just `git pull` — the new code is live immediately, no file copy needed
- Config files in `~/printer_data/config/nfc/` are yours — the installer never overwrites sections you have already edited
- Running `bash install.sh` again after an update is always safe

**What gets created:**

```
~/klipper/klippy/extras/nfc_gate.py         →  symlink → repo/klippy/extras/nfc_gate.py
~/klipper/klippy/extras/nfc_gates/          →  symlink → repo/klippy/extras/nfc_gates/
~/klipper/klippy/extras/mmu_nfc_endstop.py  →  symlink → repo/klippy/extras/mmu_nfc_endstop.py
~/printer_data/config/nfc/nfc_reader.cfg
~/printer_data/config/nfc/nfc_macros.cfg
~/printer_data/config/nfc/nfc_reader_hw.cfg
~/printer_data/config/nfc/nfc_reader_shared.cfg
```

`mmu_nfc_endstop.py` registers each enabled lane's NFC reader as a Happy Hare
gear-rail homing endstop. `nfc_reader_hw.cfg` gets one `[mmu_nfc_endstop
laneN]` section per enabled lane automatically, alongside the matching
`[nfc_gate laneN]` — see [Virtual Endstop](../shared/klipper-functions.md#virtual-endstop).

Config files use a non-destructive merge: if a section already exists in your file, it is left alone. Only missing sections are appended.

---

## Install

### Step 1 — Flash Lane MCU Firmware

> [!CAUTION]
> Do this before anything else. The NFC reader driver communicates directly with the MCU firmware over I2C. If the host Klipper and the MCU firmware are on different versions, I2C reads fail with errors that look like hardware faults.
>
> Build and flash Klipper firmware for every MCU that will carry an NFC reader before you run the installer.

### Step 2 — Clone the Repository

SSH to the Pi:

```bash
cd ~
git clone https://github.com/cwiegert/Happy-Hare-RFID-Reader.git rfid-reader
cd ~/rfid-reader
```

The installer configures a sparse checkout automatically — documentation and other non-runtime files are excluded from the Pi.

> [!IMPORTANT]
> Beta cutover: old `~/emu-nfc-reader` installs are not migrated in place. Clone this repo into `~/rfid-reader` and run `bash install.sh`. If the installer finds `~/emu-nfc-reader`, it prompts before backing up `~/printer_data/config/nfc/`, backing up `moonraker.conf`, removing the old Moonraker update-manager block, removing the old clone, and continuing with a fresh install.

### Step 3 — Run the Installer

```bash
bash install.sh
```

The installer prints what it creates. If it cannot find the Klipper extras directory or the printer config directory, it exits with an error before making any changes.

For per-lane readers, the installer asks for the Happy Hare version. Select
`v3` for the conventional `lane0` / `mmu0` layout. Select `v4` for Happy Hare
V4's default single-unit layout; it creates matching `unit0_laneN`,
`unit0_gateN`, and `nfc_unit0_laneN` sections. The V4 option deliberately
uses only the standard `unit0` layout. Multi-unit or custom gate mappings must
be edited in `nfc_reader_hw.cfg` after installation.

### Step 4 — Add Includes to `printer.cfg`

Open `~/printer_data/config/printer.cfg` and add the matching includes in this order.

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
enabled: True
mmu_gate: 0
i2c_mcu:  mmu0
```

`i2c_mcu` must exactly match the MCU name in your Happy Hare config (from `mmu_hardware.cfg`), typically `mmu0`, `mmu1`, etc. `i2c_bus` can be set once in the base `[nfc_gate]` section or overridden per lane. RC522 and PN5180 use SPI settings instead; PN5180 additionally requires wired `reset_pin` and active-high `busy_pin`. See [PN5180 wiring](../i2c-nfc/pn5180-wiring.md) for the SLB nine-wire example.

> [!IMPORTANT]
> **Temperature sensor I2C bus must match.** If your lane MCU also has a thermistor or temperature sensor connected over I2C (e.g. an SHT3x), configure it on the same I2C bus as the NFC reader. PN532 should use hardware I2C. PN7160 supports software I2C, but hardware I2C is recommended because software I2C increases MCU load.

See [Configuration Reference](configuration.md) for all available settings.

### Step 7 — Restart and Verify

```bash
sudo systemctl restart klipper
```

```gcode
NFC_STATUS
NFC_DOCTOR
```

Expected output with no tags loaded:

```
NFC gate status  (4 gates configured):
  Gate 0  [lane0]:  empty
  Gate 1  [lane1]:  empty
  Gate 2  [lane2]:  empty
  Gate 3  [lane3]:  empty
```

If you see errors, check [Troubleshooting](../i2c-nfc/troubleshooting.md).

---

### Per-Lane Readers — Required Hook

Wire the Happy Hare post-preload hook so scan-jog triggers automatically after each preload. This requires the [igiannakas IG-dev branch](https://github.com/igiannakas/Happy-Hare/tree/IG-dev) of Happy Hare, which adds `variable_user_post_preload_extension` to `mmu_macro_vars.cfg`.

**Wire the scan-jog hook.** Open `~/printer_data/config/mmu/base/mmu_macro_vars.cfg` and add:

```ini
[gcode_macro _MMU_SEQUENCE_VARS]
variable_user_post_preload_extension: '_NFC_SCAN_JOG_PRELOAD'
```

Happy Hare appends `GATE=<n>` automatically. `_NFC_SCAN_JOG_PRELOAD` calls `NFC GATE=<n> JOG_SCAN=1 SOURCE=AUTO`; NFC starts the configured scan-jog LED effect from the Python scan timer before motion begins. With this wired, set `scan_enabled: False` so Happy Hare is the sole scan-jog trigger:

```ini
[nfc_gate]
scan_enabled: False
```

`SOURCE=AUTO` identifies this as Happy Hare's own hook call. Happy Hare v4 can
run the hook while its action is still `checking`, before it unwinds back to
`idle`, so NFC runs hook calls through the version-aware scan-safe check.
Manual or console `JOG_SCAN=1` commands without `SOURCE=AUTO` still require
strict `action=idle`.

Without this hook wired, scan-jog falls back to triggering on gate status change (`scan_enabled: True`), which is less reliable than the preload hook.

---

### Shared Reader — Additional Required Steps

If you are using the shared reader (`[nfc_gate shared]`), two more things must be configured before it will work. Both require the [igiannakas IG-dev branch](https://github.com/igiannakas/Happy-Hare/tree/IG-dev) of Happy Hare, which adds `variable_user_post_preload_extension` to `mmu_macro_vars.cfg`.

**Wire the Happy Hare preload hook.** Open `~/printer_data/config/mmu/base/mmu_macro_vars.cfg` and add:

```ini
[gcode_macro _MMU_SEQUENCE_VARS]
variable_user_post_preload_extension: '_NFC_SHARED_PRELOAD'
```

For a hybrid installation that also has per-lane readers, use this instead:

```ini
variable_user_post_preload_extension: '_NFC_HYBRID_PRELOAD'
```

`_NFC_HYBRID_PRELOAD` starts a configured lane reader first. If the lane scan
cannot read a tag, it applies staged shared-reader data to the loaded gate.
It uses the shared reader directly for a gate without a lane reader.

Without this, NFC never sees the preload event and the spool ID is never applied to the gate.

If this value is still set to `NFC JOG_SCAN=1`, the printer is using the per-lane reader hook. Shared-reader loads will stage a spool, but the post-preload commit will not run; the pending spool will eventually time out and you may see `NFC GATE=<n>` errors.

**Pending timeout.** NFC uses Happy Hare's active `[mmu]`
`pending_spool_id_timeout` when Klipper exposes that value. Otherwise it uses
its 60 s default. No additional Happy Hare configuration is required for the
default behavior.

---

## Moonraker Update Manager

The installer adds this block to `~/printer_data/config/moonraker.conf` automatically. If it was not found or you need to add it manually:

```ini
[update_manager Happy-Hare-RFID-Reader]
type:             git_repo
path:             ~/rfid-reader
origin:           https://github.com/cwiegert/Happy-Hare-RFID-Reader.git
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
cd ~/rfid-reader
git pull
bash install.sh
sudo systemctl restart klipper
```

If the update notes mention a Klipper MCU protocol change, rebuild and flash each lane MCU before restarting Klipper.

---

## Uninstall

```bash
cd ~/rfid-reader
bash uninstall.sh
```

The uninstaller:

1. Removes the `nfc_gate.py` symlink from Klipper extras
2. Removes the `nfc_gates/` symlink from Klipper extras
3. Removes legacy `~/pn532_scan.py` if an older installer placed it there
4. Removes the `mmu_nfc_endstop.py` symlink from Klipper extras
5. Moves `~/printer_data/config/nfc/` to `nfc_removed_<timestamp>/` (your config is preserved, not deleted)
6. Restarts Klipper

At the end, it prompts whether to remove the local repo checkout at
`~/rfid-reader`. The default answer is yes. Answer `n` to keep the checkout.

**Manual steps after uninstalling** — the uninstaller cannot edit your config files:

Remove these three lines from `printer.cfg`:
```ini
[include nfc/nfc_reader.cfg]
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_hw.cfg]
```

Remove the update manager block from `moonraker.conf`, then restart Moonraker:

```ini
[update_manager Happy-Hare-RFID-Reader]
```

If you are cleaning up an earlier beta, also remove either old block if present:

```ini
[update_manager emu_nfc_reader]
[update_manager happy_hare_rfid_reader]
[update_manager Happy-Hare-rfid-reader]
```

```bash
sudo systemctl restart moonraker
```

Delete the config backup when you no longer need it:
```bash
rm -rf ~/printer_data/config/nfc_removed_*
```

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
