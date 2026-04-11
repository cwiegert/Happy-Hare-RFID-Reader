# SPI / PN532 — Setup & Deployment

[← Back to Index](../../Readme.md)

---

## Hardware Path: SPI / PN532

Use this path when you have a **dedicated Raspberry Pi Pico** connected to the CAN bus
with PN532 NFC readers wired to its SPI1 bus.

The PN532 handles the full ISO14443A stack in hardware — one `InListPassiveTarget`
command returns the tag UID directly with no manual REQA/ANTICOLL/SELECT sequence
required. This keeps CAN bus traffic lower than the RC522 path.

---

## Prerequisites

- Klipper installed on a Raspberry Pi (klippy running)
- CAN bus working (e.g. with an EBB36/42 toolhead board already on the bus)
- Raspberry Pi Pico + SN65HVD230 CAN transceiver, wired per [wiring.md](wiring.md)
- PN532 modules wired to Pico SPI1 per [wiring.md](wiring.md)
- Each PN532 set to **SPI mode** via its onboard DIP switch or solder jumper

---

## Step 1 — Install from Git

Clone the repository using sparse checkout so the `tests/` directory (development
only) is not downloaded to the printer. Run the install script, which creates symlinks
from the repo into Klipper's extras directory — future `git pull` updates take effect
after a Klipper restart, with no re-install needed.

```bash
cd ~
git clone --filter=blob:none --sparse YOUR_REPO_URL_HERE emu-nfc-reader
cd ~/emu-nfc-reader
git sparse-checkout set klippy config docs
cd ~
bash ~/emu-nfc-reader/install.sh
```

Verify the symlinks were created:

```bash
ls -la ~/klipper/klippy/extras/nfc_gates
ls -la ~/klipper/klippy/extras/nfc_gate.py
```

Both should point back into `~/emu-nfc-reader/`.

---

## Step 2 — Build Klipper MCU Firmware for the Pico

```bash
cd ~/klipper
make menuconfig
```

Select these options:

| Setting | Value |
|---|---|
| Micro-controller | Raspberry Pi RP2040 |
| Communication interface | CAN bus |
| CAN TX GPIO | 4 |
| CAN RX GPIO | 5 |
| CAN bus speed | 1000000 (1 Mbit/s) |

Then build:

```bash
make clean && make
```

Output firmware: `out/klipper.uf2`

> SPI pin assignments are **not** set in `make menuconfig`. They are configured at
> runtime by Klipper's `MCU_SPI` interface using the `cs_pin` and `spi_bus` keys
> in `pn532_spi.cfg`.

---

## Step 3 — Flash the Pico

Hold **BOOTSEL** and connect the Pico to the Pi via USB:

```bash
cp out/klipper.uf2 /media/$USER/RPI-RP2/
```

The Pico reboots automatically into Klipper MCU firmware and appears on the CAN bus.

If the Pico is already on CAN running an older Klipper build:

```bash
python3 ~/klipper/scripts/flash_can.py -u <current_uuid> -f out/klipper.uf2
```

---

## Step 4 — Find the Pico CAN UUID

```bash
~/klippy-env/bin/python ~/klipper/scripts/canbus_query.py can0
```

Expected output:

```
Found canbus_uuid=aabbccddeeff, Application: Klipper
```

Note the UUID — you will paste it into the config in the next step.

---

## Step 5 — Configure printer.cfg

`install.sh` already copied all config files to `~/printer_data/config/NFC/`.
Add these includes to `printer.cfg` **in this order**:

```ini
[include NFC/nfc_vars.cfg]
[include NFC/nfc_macros.cfg]
[include NFC/pn532_spi.cfg]
```

- **`NFC/nfc_vars.cfg`** is the one file you edit — set your Spoolman URL, poll interval, and debug level here. It must be included before the hardware config.
- **`NFC/nfc_macros.cfg`** contains the Happy Hare integration macros and is shared between all hardware paths.
- **`NFC/pn532_spi.cfg`** contains only hardware-specific settings.

Edit these files:

| File | Key | What to do |
|---|---|---|
| `NFC/nfc_vars.cfg` | `spoolman_url` | Set to your Spoolman instance URL |
| `NFC/pn532_spi.cfg` | `canbus_uuid` | Replace `YOUR_UUID_HERE` with the UUID from Step 4 |
| `NFC/pn532_spi.cfg` | `extra_cs_pins` | Remove entries for gates you don't have wired |

---

## Step 6 — Set Up Moonraker Auto-Update

Add this section to `~/printer_data/config/moonraker.conf`:

```ini
[update_manager emu_nfc_reader]
type: git_repo
path: ~/emu-nfc-reader
origin: YOUR_REPO_URL_HERE
primary_branch: main
managed_services: klipper
install_script: install.sh
```

Restart Moonraker:

```bash
sudo systemctl restart moonraker
```

Updates will now appear in the Mainsail / Fluidd update panel alongside Klipper itself.

---

## Step 7 — Restart Klipper and Verify

```bash
sudo systemctl restart klipper
```

Check the NFC log for successful initialisation:

```bash
tail -f ~/printer_data/logs/nfc_reader.log
```

Expected output:

```
nfc_gates: connected to MCU 'nfc_pico', initialising 5 gates (poll=30s, absent_threshold=3)
init: gate 0 (PN532) SAMConfiguration OK
init: gate 1 (PN532) SAMConfiguration OK
init: gate 2 (PN532) SAMConfiguration OK
init: gate 3 (PN532) SAMConfiguration OK
init: gate 4 (PN532) SAMConfiguration OK
nfc_gates: 5/5 readers initialised
nfc_gates: polling thread started
```

If any reader fails — see [Troubleshooting](troubleshooting.md).

---

## Step 8 — Test Tag Detection

From the Klipper console (Mainsail / Fluidd terminal):

```
NFC_GATE_STATUS
```

All gates should show `empty` before any tags are placed.

Place an NFC spool tag on Gate 0. Within one poll cycle (30 s default):

```
NFC gate 0: spool 1042 detected (UID A3F200CC)
```

---

## Updating the Module

With Moonraker configured (Step 6), updates appear in the update panel automatically.

To update manually:

```bash
cd ~/emu-nfc-reader
git pull
bash install.sh
sudo systemctl restart klipper
```

---

## Uninstalling

### 1 — Remove the Klipper extra symlinks

```bash
rm ~/klipper/klippy/extras/nfc_gate.py
rm -rf ~/klipper/klippy/extras/nfc_gates
```

### 2 — Remove the printer config files

```bash
rm -rf ~/printer_data/config/NFC
```

Remove the three `[include NFC/...]` lines from `printer.cfg`.

### 3 — Remove the Moonraker update manager entry

Delete the `[update_manager emu_nfc_reader]` block from `moonraker.conf`, then restart Moonraker:

```bash
sudo systemctl restart moonraker
```

### 4 — Remove the repo clone

```bash
rm -rf ~/emu-nfc-reader
```

### 5 — Restart Klipper

```bash
sudo systemctl restart klipper
```

---

**Next:** [Wiring Diagram →](wiring.md)
