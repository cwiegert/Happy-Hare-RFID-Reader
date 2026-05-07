# EMU NFC Gate Reader

> Plug an NFC reader into each filament gate. Load a tagged spool. Happy Hare updates automatically.

Each filament gate on your EMU gets a PN532 NFC reader wired to its EBB42. When you load a spool, the system jogs the filament in small steps until the NFC tag rotates into read range, finds the matching entry in Spoolman, rewinds to the parked position, and tells Happy Hare which spool is in which gate — no console commands, no manual selection.

```
Load spool → HH parks filament → scan-jog rotates spool → tag in range → Spoolman lookup → Happy Hare gate map
```

---

## What You Need

- A Voron with an EMU running [Happy Hare](https://github.com/moggieuk/Happy-Hare)
- One mcu per filament lane (EBB36, EBB42, or SLB)
- One PN532 NFC reader module per gate (~$3–5 each) 
- M2 x 4 self-tapping screws to mount each PN532 to the bracket
- Spoolman running and accessible from the Pi
- NFC tags on your spools (NTAG213/215/216 or Mifare Classic)

---

## Documentation

| | Guide | What it covers |
|---|---|---|
| 1 | [Wiring](docs/i2c-pn532/wiring.md) | Pin connections, I2C mode selection, pull-ups |
| 2 | [Install](docs/shared/install-uninstall.md) | Clone, run installer, configure Moonraker updates |
| 3 | [Setup](docs/i2c-pn532/setup.md) | printer.cfg includes, lane config, first boot |
| 4 | [Spoolman Integration](docs/shared/spoolman-integration.md) | Create the extra field, register tag UIDs |
| 5 | [Commands & Macros](docs/shared/klipper-functions.md) | Every GCode command with examples |
| 6 | [Configuration Reference](docs/shared/configuration.md) | All settings with defaults |
| 7 | [Troubleshooting](docs/i2c-pn532/troubleshooting.md) | Failure patterns and fixes |
| 8 | [How It Works](docs/shared/how-it-works.md) | Boot sequence, poll flow, system layers, macro events |
| 9 | [Expert: Low-Level I2C Debug](docs/shared/expert-low-level-i2c-debugging.md) | Manual PN532 bus commands |

---

## Scan-and-Jog: How the Tag Gets Read

NFC tags sit on the spool hub. When Happy Hare parks filament at the gate the hub face may not be aligned over the antenna — the tag could be facing any direction. Scan-and-jog solves this automatically.

**Automatic path (normal operation):**
1. Happy Hare finishes loading filament and sets gate_status → 1 (parked)
2. NFC_Manager detects the 0→1 edge on the next poll tick
3. The scan-jog loop starts — filament advances in `scan_jog_mm` steps (default 75 mm), reading the NFC tag after each step
4. The moment the tag rotates into read range, the spool is identified through Spoolman and dispatched to Happy Hare
5. The filament rewinds to the parked position via `MMU_UNLOAD restore=0`

**Manual trigger:** If the automatic trigger didn't fire (or you want to retry), run:
```gcode
NFC GATE=0 JOG_SCAN=1
```
This runs the exact same sequence with the same precondition checks (HH idle, not printing, no other gate scanning)

**Configurable per lane** — see [Configuration Reference](docs/shared/configuration.md):

| Key | Default | Effect |
|---|---|---|
| `scan_enabled` | `True` | Master switch — set `False` to disable automatic scan-jog |
| `scan_jog_mm` | `75.0` | Filament advance per step (mm) |
| `scan_poll_interval` | `0.1` | Minimum seconds between NFC reads during scan |

---

## Quick Install

> [!IMPORTANT]
> Before installing, rebuild and flash Klipper firmware on every EBB42 / lane MCU. The NFC driver talks to the MCU directly over I2C — if MCU firmware is stale, failures look like hardware problems. See [the full warning below](#mcu-firmware-warning).

SSH to the Pi and clone the repo:

```bash
cd ~
git clone --filter=blob:none --sparse git@github.com:<your-github-username>/NFC-Reader.git emu-nfc-reader
cd ~/emu-nfc-reader
git sparse-checkout set klippy config
bash install.sh
```

Add to `printer.cfg` — **order matters**:

```ini
[include nfc/nfc_reader.cfg]
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_hw.cfg]
```

Set your Spoolman URL in `~/printer_data/config/nfc/nfc_reader.cfg`:

```ini
[nfc_gate]
spoolman_url:      auto
spoolman_rfid_key: rfid_tag
```

Add the Moonraker update block to `moonraker.conf`:

```ini
[update_manager emu_nfc_reader]
type:             git_repo
path:             ~/emu-nfc-reader
origin:           https://github.com/<your-github-username>/NFC-Reader.git
primary_branch:   main
managed_services: klipper
install_script:   install.sh
info_tags:        desc=EMU NFC Gate Reader for Happy Hare
```

Restart and verify:

```bash
sudo systemctl restart klipper moonraker
```

```gcode
NFC_STATUS
```

Expected (with no tags loaded):
```
NFC gate status  (4 gates configured):
  Gate 0  [lane0]:  empty
  Gate 1  [lane1]:  empty
  Gate 2  [lane2]:  empty
  Gate 3  [lane3]:  empty
```

See [Install & Uninstall](docs/shared/install-uninstall.md) for the complete first-boot checklist.

---

## Day-to-Day Commands

These are the commands you'll actually use at the Fluidd/Mainsail console:

```gcode
NFC_STATUS                    ; see all gates at a glance
NFC GATE=0 SCAN=1             ; read a tag and show its UID
NFC GATE=0 POLL=1             ; full cycle: read → Spoolman → Happy Hare
NFC GATE=0 JOG_SCAN=1         ; start scan-jog (same as automatic pre-load trigger)
NFC GATE=0 READ=1             ; start automatic background polling
NFC GATE=0 READ=0             ; stop polling
```

See [Commands & Macros](docs/shared/klipper-functions.md) for everything, including how to test the Happy Hare handoff without hardware.

---

See [How It Works](docs/shared/how-it-works.md) for the boot sequence, per-poll flow, system layers, and macro dispatch events.

---

<a name="mcu-firmware-warning"></a>

> [!CAUTION]
> ## 🔴⚡ Your Lane MCUs Should Run Firmware Built From Your Current Host firmware version
>
> This is the number one cause of mysterious NFC failures — and it looks nothing like a firmware problem. It looks like broken wiring, a dead PN532, a misconfigured I2C bus, or a ghost in the machine.
>
> **Here's what's actually happening:** The PN532 driver doesn't talk to Klipper software on the Pi. It talks directly to the firmware running on each EBB42 over I2C. When you run `git pull` on the Pi, the host updates — but every lane MCU is still running whatever firmware it had before. Now they speak different protocol versions, and I2C transactions start silently failing:
>
> - 🔇 ACK reads fail immediately after the ready byte succeeds
> - ⏱️ `i2c_read_response` timeouts appear out of nowhere
> - 🌡️ Your BME280 on the same bus starts misbehaving for no apparent reason
>
> **The fix is not in the wiring. It is not in the config. It is in the firmware.**
>
> Every time you update Klipper — before you touch NFC config, before you run `INIT`, before you blame the hardware — do this:
>
> ```
> 1. git pull                          ← update the Klipper host checkout
> 2. Build MCU firmware                ← compiled from THAT exact host version
> 3. Flash every lane MCU / EBB42      ← every one, not just lane0
> 4. sudo systemctl restart klipper
> 5. Confirm all lane MCUs reconnect   ← check Fluidd/Mainsail before testing NFC
> ```
>
> ✅ Host and MCU firmware versions match → NFC works reliably
> ❌ Host updated, MCUs not reflashed → NFC fails in ways that will waste hours

---

## License

Copyright (C) 2026 WoodWorker.
Licensed under [GNU General Public License v3.0 or later](https://www.gnu.org/licenses/gpl-3.0.html).
See [LICENSE](LICENSE) for the full terms.
