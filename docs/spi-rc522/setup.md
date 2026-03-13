# SPI / RC522 — Setup & Deployment

[← Back to Index](../../Readme.md)

---

## Hardware Path: SPI / RC522

Use this path when you have a **dedicated Raspberry Pi Pico** connected to the CAN bus
with RC522 NFC readers wired to its SPI1 bus.

**You do NOT need this path** if your EMU gates are wired to EBB42 lane boards with PN532
readers — use the [I2C / PN532 path](../i2c-pn532/setup.md) instead.

---

## Prerequisites

- Klipper installed on a Raspberry Pi (klippy running)
- CAN bus working (e.g. with an EBB36/42 toolhead board already on the bus)
- Raspberry Pi Pico + SN65HVD230 CAN transceiver, wired per [wiring.md](wiring.md)
- RC522 modules wired to Pico SPI1 per [wiring.md](wiring.md)

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
> in `nfc_gates_spi_rc522.cfg`.

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

Copy all three config files to your Klipper config directory:

```bash
cp ~/emu-nfc-reader/config/nfc_macros.cfg           ~/printer_data/config/
cp ~/emu-nfc-reader/config/nfc_vars.cfg              ~/printer_data/config/
cp ~/emu-nfc-reader/config/nfc_gates_spi_rc522.cfg  ~/printer_data/config/
```

Add all three includes to `printer.cfg` **in this order**:

```ini
[include nfc_vars.cfg]
[include nfc_macros.cfg]
[include nfc_gates_spi_rc522.cfg]
```

- **`nfc_vars.cfg`** is the one file you edit for your installation — set your Spoolman URL, poll interval, and debug level here. It must be included before the hardware config.
- **`nfc_macros.cfg`** contains the Happy Hare integration macros and is shared between both hardware paths.
- **`nfc_gates_spi_rc522.cfg`** contains only hardware-specific settings.

Edit `~/printer_data/config/nfc_vars.cfg` and `~/printer_data/config/nfc_gates_spi_rc522.cfg`:

| File | Key | What to do |
|---|---|---|
| `nfc_vars.cfg` | `spoolman_url` | Set to your Spoolman instance URL |
| `nfc_gates_spi_rc522.cfg` | `canbus_uuid` | Replace `YOUR_UUID_HERE` with the UUID from Step 4 |
| `nfc_gates_spi_rc522.cfg` | `extra_cs_pins` | Remove entries for gates you don't have wired |

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
Moonraker runs `install.sh` after each update to refresh the symlinks, then restarts Klipper.

---

## Step 7 — Restart Klipper and Verify

```bash
sudo systemctl restart klipper
```

Check the log for successful initialisation:

```bash
tail -f ~/printer_data/logs/klippy.log | grep nfc_gates
```

Expected output:

```
nfc_gates: connected to MCU 'nfc_pico', initialising 5 gates (poll=30s, absent_threshold=3)
nfc_gates: gate 0 RC522 init OK (TxControl=0x83)
nfc_gates: gate 1 RC522 init OK (TxControl=0x83)
nfc_gates: gate 2 RC522 init OK (TxControl=0x83)
nfc_gates: gate 3 RC522 init OK (TxControl=0x83)
nfc_gates: gate 4 RC522 init OK (TxControl=0x83)
nfc_gates: 5/5 readers initialised
nfc_gates: polling thread started
```

If any reader fails: `nfc_gates: gate N reader did not respond after init (check wiring)`
— see [Troubleshooting](troubleshooting.md).

---

## Step 8 — Test Tag Detection

From the Klipper console (Mainsail / Fluidd terminal):

```
NFC_GATE_STATUS
```

All gates should show `empty` before any tags are placed.

Place an NFC spool tag on Gate 0. Within one poll cycle (30 s, or 5 s if you reduced it):

```
NFC gate 0: spool 1042 detected (UID A3F200CC)
```

Remove the tag. After 3 missed polls (90 s at the default 30 s interval):

```
NFC gate 0: spool removed
```

If you see `has no spool ID`, the tag has not been written yet.
See [Writing Spool IDs to NFC Tags](../shared/tag-writing.md).

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

**Next:** [Wiring Diagram →](wiring.md) | [Troubleshooting →](troubleshooting.md)
