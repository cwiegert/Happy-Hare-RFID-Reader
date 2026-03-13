# I2C / PN532 — Setup & Deployment

[← Back to Index](../../Readme.md)

---

## Hardware Path: I2C / PN532

Use this path when you have **PN532 NFC modules wired to each EBB42 lane board's
I2C bus** (PB3/PB4). The lane MCUs are already defined by Happy Hare — no separate
Pico or CAN transceiver is needed.

**You do NOT need this path** if you are using a standalone Pico with RC522 readers —
use the [SPI / RC522 path](../spi-rc522/setup.md) instead.

---

## Prerequisites

- Klipper installed with Happy Hare configured and EBB42 lane MCUs (`lane0`, `lane1`, ...) already working
- PN532 modules wired to each EBB42 lane board per [wiring.md](wiring.md)
- PN532 modules set to **I2C mode** via the onboard DIP switch or solder jumper

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

## Step 2 — Configure printer.cfg

Copy all three config files to your Klipper config directory:

```bash
cp ~/emu-nfc-reader/config/nfc_macros.cfg          ~/printer_data/config/
cp ~/emu-nfc-reader/config/nfc_vars.cfg             ~/printer_data/config/
cp ~/emu-nfc-reader/config/nfc_gate_i2c_pn532.cfg  ~/printer_data/config/
```

Add all three includes to `printer.cfg` **in this order**:

```ini
[include nfc_vars.cfg]
[include nfc_macros.cfg]
[include nfc_gate_i2c_pn532.cfg]
```

- **`nfc_vars.cfg`** is the one file you edit for your installation — set your Spoolman URL, poll interval, and debug level here. It must be included before the hardware config.
- **`nfc_macros.cfg`** contains the Happy Hare integration macros (`_NFC_SPOOL_CHANGED` etc.) and is shared between the SPI and I2C paths.
- **`nfc_gate_i2c_pn532.cfg`** contains only hardware pin definitions — do not add user settings here.

> **Important:** Do **not** include `nfc_gates_spi_rc522.cfg` at the same time.
> The two hardware paths are mutually exclusive — use one or the other.

---

## Step 3 — Set Your Spoolman URL

Open `~/printer_data/config/nfc_vars.cfg` and set your Spoolman instance URL:

```ini
[nfc_gate]
spoolman_url: http://mainsailos.local:7912    # ← change this to your instance
```

All other settings in `nfc_vars.cfg` (poll interval, debug level, cache TTL, etc.)
have sensible defaults and can be left as-is initially. See the comments in that file
for a description of every option.

---

## Step 4 — Adjust Gate Count

Edit `~/printer_data/config/nfc_gate_i2c_pn532.cfg`.

By default the file has sections for `lane0` through `lane3` (4 gates), with `lane4`
commented out. Add or remove `[nfc_gate laneN]` sections to match your physical gate
count. Each section needs only the hardware-specific keys:

```ini
[nfc_gate lane2]
mmu_gate:                2
i2c_mcu:                 lane2           # must match an [mcu] name in mmu_hardware.cfg
i2c_software_scl_pin:    lane2:PB3
i2c_software_sda_pin:    lane2:PB4
```

All other settings (Spoolman URL, poll interval, debug level) are inherited from
`[nfc_gate]` in `nfc_vars.cfg`. You can override any key locally in a lane section
if you need per-gate behaviour.

---

## Step 5 — Set Up Moonraker Auto-Update

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

## Step 6 — Restart Klipper and Verify

```bash
sudo systemctl restart klipper
```

Check the log for each gate:

```bash
tail -f ~/printer_data/logs/klippy.log | grep nfc_gate
```

Expected output:

```
nfc_gate: [lane0] connected — gate=0, poll=30s, absent_threshold=3, debug=1
nfc_gate: [lane0] PN532 reader OK
nfc_gate: [lane0] polling thread started
nfc_gate: [lane1] connected — gate=1, poll=30s, absent_threshold=3, debug=1
nfc_gate: [lane1] PN532 reader OK
nfc_gate: [lane1] polling thread started
...
```

If a reader fails: `nfc_gate: [laneN] PN532 did not respond after init — check wiring and I2C address (default 0x24)`
— see [Troubleshooting](troubleshooting.md).

---

## Step 7 — Test Tag Detection

From the Klipper console (Mainsail / Fluidd terminal):

```
NFC_GATE_STATUS
```

All configured gates should show `empty`.

Place an NFC spool tag on Gate 0. Within one poll cycle (30 s default):

```
NFC gate status  (4 gates configured):
  Gate 0  [lane0]:  spool 1042   UID A3F200CC
  Gate 1  [lane1]:  empty
  Gate 2  [lane2]:  empty
  Gate 3  [lane3]:  empty
```

If you see `tag A3F200CC (no spool ID)`, the tag has not been written yet.
See [Writing Spool IDs to NFC Tags](../shared/tag-writing.md).

---

## Updating the Module

With Moonraker configured (Step 4), updates appear in the update panel automatically.

To update manually:

```bash
cd ~/emu-nfc-reader
git pull
bash install.sh
sudo systemctl restart klipper
```

---

**Next:** [Wiring Diagram →](wiring.md) | [Troubleshooting →](troubleshooting.md)
