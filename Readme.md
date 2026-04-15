# EMU NFC Gate Reader

> Plug an NFC reader into each filament gate. Load a tagged spool. Happy Hare updates automatically.

Each filament gate on your EMU gets a PN532 NFC reader wired to its EBB42. When you drop in a spool that has an NFC tag, the reader sees it, finds the matching entry in Spoolman, and tells Happy Hare which spool is in which gate — no console commands, no manual selection.

```
Spool with NFC tag → PN532 on EBB42 → Klipper I2C → Spoolman lookup → Happy Hare gate map
```

---

## What You Need

- A Voron with an EMU running [Happy Hare](https://github.com/moggieuk/Happy-Hare)
- One EBB42 per filament lane (already required by Happy Hare on most EMU builds)
- One PN532 NFC reader module per gate (~$3–5 each)
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
| | [Configuration Reference](docs/shared/configuration.md) | All settings with defaults |
| | [Troubleshooting](docs/i2c-pn532/troubleshooting.md) | Failure patterns and fixes |
| | [How It Works](docs/shared/how-it-works.md) | Boot sequence, poll flow, system layers, macro events |
| | [Expert: Low-Level I2C Debug](docs/shared/expert-low-level-i2c-debugging.md) | Manual PN532 bus commands |

---

## Quick Install

> [!IMPORTANT]
> Before installing, rebuild and flash Klipper firmware on every EBB42 / lane MCU. The NFC driver talks to the MCU directly over I2C — if MCU firmware is stale, failures look like hardware problems. See [the full warning below](#mcu-firmware-warning).

SSH to the Pi and clone the repo:

```bash
cd ~
git clone --filter=blob:none --sparse git@github.com:<your-github-username>/NFC-Reader.git emu-nfc-reader
cd ~/emu-nfc-reader
git sparse-checkout set klippy config docs tools
bash install.sh
```

Add to `printer.cfg` — **order matters**:

```ini
[include NFC/nfc_vars.cfg]
[include NFC/nfc_macros.cfg]
[include NFC/pn532_i2C.cfg]
```

Set your Spoolman URL in `~/printer_data/config/NFC/nfc_vars.cfg`:

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
origin:           git@github.com:<your-github-username>/NFC-Reader.git
primary_branch:   main
managed_services: klipper
install_script:   install.sh
```

Restart and verify:

```bash
sudo systemctl restart klipper moonraker
```

```gcode
NFC_GATE_STATUS
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
NFC_GATE_STATUS                    ; see all gates at a glance
NFC_GATE GATE=0 SCAN=1         ; read a tag and show its UID
NFC_GATE GATE=0 POLL=1         ; full cycle: read → Spoolman → Happy Hare
NFC_GATE GATE=0 READ=1         ; start automatic background polling
NFC_GATE GATE=0 READ=0         ; stop polling
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

See [LICENSE](LICENSE).
