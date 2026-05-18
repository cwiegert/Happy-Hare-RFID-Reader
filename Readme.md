# EMU NFC Gate Reader

> NFC spool identification for Happy Hare: use one reader per EMU lane, or one shared reader inside the MMU body.

This project supports two operating models:

- **Per-lane readers:** each EMU gate gets its own PN532 reader. When you load a spool, NFC can scan-jog the filament until the tag rotates into read range, resolve the spool in Spoolman, and update Happy Hare's gate map automatically.
- **Shared reader:** one PN532 is mounted inside the MMU body. Tap the spool tag before loading, then insert filament into any gate. When Happy Hare starts pregate preload, NFC stages that spool ID for the gate being loaded.

Per-lane flow:

```
Load spool → HH parks filament → scan-jog rotates spool → tag in range → Spoolman lookup → Happy Hare gate map
```

Shared-reader flow:

```
Tap tag → spool pending → load any MMU gate → HH pregate preload → spool assigned
```

---

## What You Need

- A Voron with an EMU running ~~[Happy Hare](https://github.com/moggieuk/Happy-Hare)~~
- The [igiannakas IG-dev branch](https://github.com/igiannakas/Happy-Hare/tree/IG-dev) of Happy Hare.
- One mcu per filament lane (EBB36, EBB42, or SLB)
- One PN532 NFC reader module per gate (~$3–5 each)    the holder is designed for [35mm PCB](https://www.amazon.com/dp/B0DDKX2JCD)
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
| 7 | [Message Definitions](docs/shared/message_definition.md) | Console messages and `nfc_reader.log` entries for per-lane and shared readers |
| 8 | [Troubleshooting](docs/i2c-pn532/troubleshooting.md) | Failure patterns and fixes |
| 9 | [How It Works](docs/shared/how-it-works.md) | Boot sequence, poll flow, system layers, macro events |
| 10 | [Expert: Low-Level I2C Debug](docs/shared/expert-low-level-i2c-debugging.md) | Manual PN532 bus commands |

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
This runs the exact same sequence with the same precondition checks (HH idle, not printing, no other gate scanning).

**Happy Hare post-preload hook (alternative to automatic polling):** The [igiannakas IG-dev branch](https://github.com/igiannakas/Happy-Hare/tree/IG-dev) of Happy Hare adds a `variable_user_post_preload_extension` hook. Configure it to trigger NFC scan-jog automatically after each `MMU_PRELOAD`:

```ini
[gcode_macro _MMU_SEQUENCE_VARS]
variable_user_post_preload_extension: 'NFC JOG_SCAN=1'
```

Happy Hare appends `GATE=<n>` automatically, giving `NFC JOG_SCAN=1 GATE=<n>`. Use this recommended config with the hook:

```ini
startup_polling: 0
scan_enabled:    False
```

This disables gate-status polling entirely — Happy Hare calls NFC only after the relevant gate completes preload. See [Configuration Reference](docs/shared/configuration.md) for full details.

**Tag-not-in-Spoolman behavior:** If the tag UID is detected but not registered in Spoolman, the gate is kept `AVAILABLE=1` with `SPOOLID=-1` so Happy Hare still treats the lane as loaded. The console prompts the user to add the UID to Spoolman.

**Configurable per lane** — see [Configuration Reference](docs/shared/configuration.md):

| Key | Default | Effect |
|---|---|---|
| `scan_enabled` | `False` | Automatic gate-status trigger; keep `False` when using the Happy Hare post-preload hook |
| `scan_jog_mm` | `75.0` | Filament advance per step (mm) |
| `scan_reads_per_position` | `3` | NFC read attempts at each stopped scan position before the next substep |
| `scan_rewind_buffer_mm` | `30.0` | Distance left for Happy Hare's final gate parking step |
| `scan_decode_retry_mm` | `2.0` | Distance between nearby retry positions after an incomplete rich tag read |
| `scan_decode_retry_rounds` | `5` | Nearby retry rounds; each round probes both sides of the first UID hit |
| `scan_poll_interval` | `0.1` | Seconds between stopped-position NFC reads during scan |

During scan-jog, gate `N` also guards against the rare case where its reader
sees the parked spool on gate `N - 1`. If the read UID exactly matches the left
neighbor's cached UID, NFC shifts that neighbor 75 mm out of range, continues
scanning the current gate, and restores the neighbor on scan exit.

---

## Shared Reader

An optional single PN532 mounted inside the MMU body — not tied to any EMU lane. Tap a tagged spool on it before loading; Happy Hare automatically assigns that spool to whichever gate gets the next pregate preload.

```
tap spool on shared reader → blinking LED → spool pending
insert filament into MMU gate → pregate preload → HH assigns spool ID
```

Happy Hare already has the mechanism:

```gcode
MMU_GATE_MAP NEXT_SPOOLID=<spool_id>
```

The shared reader issues this command at `variable_user_post_preload_extension` time — just after preload starts. Happy Hare assigns it to the loaded gate; NFC clears the pending state.

**Setup:** include `nfc_reader_shared.cfg` and wire the Happy Hare post-preload hook to `_NFC_SHARED_PRELOAD`. See [Shared Reader](docs/shared/shared-reader.md) for the full workflow, [Configuration Reference](docs/shared/configuration.md#shared-reader) for the config block, and [Commands & Macros](docs/shared/klipper-functions.md#shared-reader) for operation.

---

## Quick Install

> [!IMPORTANT]
> Before installing, rebuild and flash Klipper firmware on every EBB42 / lane MCU. The NFC driver talks to the MCU directly over I2C — if MCU firmware is stale, failures look like hardware problems. See [the full warning below](#mcu-firmware-warning).

SSH to the Pi and clone the repo:

```bash
cd ~
git clone https://github.com/cwiegert/HH-RFID-Reader.git emu-nfc-reader
cd ~/emu-nfc-reader
bash install.sh
```

Add the matching includes to `printer.cfg` — **order matters**.

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

Hybrid install with per-lane readers and a shared reader:

```ini
[include nfc/nfc_reader.cfg]
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_hw.cfg]
[include nfc/nfc_reader_shared.cfg]
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
origin:           https://github.com/cwiegert/HH-RFID-Reader.git
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
NFC_HELP                      ; show everyday command help
NFC_HELP ADVANCED=1 CALLBACKS=1 LOW_LEVEL=1  ; show the full command set
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
