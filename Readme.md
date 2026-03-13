# EMU NFC Gate Reader

Automatic spool detection for the Extended Multi-Material Unit (EMU).
NFC tags on filament spools are detected at each gate and matched to Spoolman
spool records via UID lookup.  Happy Hare's gate map is updated automatically
via `MMU_GATE_MAP` — no manual updates and no writing to tags needed.

**Last Updated:** March 2026

---

## Choose Your Hardware Path

Two hardware configurations are supported. **Pick one** based on what you have:

---

### Path A — SPI / RC522

**Use this if:** you have a dedicated **Raspberry Pi Pico** connected to the CAN bus,
with **RC522 NFC readers** wired to the Pico's SPI bus.

```
RC522 readers (SPI) → Pico (CAN) → klippy [nfc_gates] → Happy Hare
```

| | |
|---|---|
| Extra hardware | Raspberry Pi Pico + SN65HVD230 CAN transceiver |
| Readers | RC522 (one per gate, shared SPI bus, individual CS pins) |
| Klipper config | One `[nfc_gates]` section, `[mcu nfc_pico]` |
| Config files | `nfc_macros.cfg` + `nfc_vars.cfg` + `nfc_gates_spi_rc522.cfg` |

→ **[SPI / RC522 Setup Guide](docs/spi-rc522/setup.md)**

---

### Path B — I2C / PN532

**Use this if:** you have **EBB42 lane boards** already on the CAN bus and want to wire
a **PN532 NFC module** to each lane board's I2C bus. No separate Pico is needed.

```
PN532 (I2C on EBB42) → lane MCU (CAN) → klippy [nfc_gate laneN] → Happy Hare
```

| | |
|---|---|
| Extra hardware | PN532 module per gate (wired to EBB42 PB3/PB4) |
| Readers | PN532 (one per lane board, separate I2C bus per gate) |
| Klipper config | One `[nfc_gate laneN]` section per gate |
| Config files | `nfc_macros.cfg` + `nfc_vars.cfg` + `nfc_gate_i2c_pn532.cfg` |

→ **[I2C / PN532 Setup Guide](docs/i2c-pn532/setup.md)**

---

## One-Command Install

Both paths use the same install process — the script creates symlinks into Klipper so
updates require only a `git pull` and Klipper restart. The sparse clone below skips
the `tests/` directory (development only — not needed on the printer):

```bash
cd ~
git clone --filter=blob:none --sparse https://github.com/cwiegert/NFC-Reader.git emu-nfc-reader
cd ~/emu-nfc-reader
git sparse-checkout set klippy config docs
cd ~
bash ~/emu-nfc-reader/install.sh
```

Then follow the setup guide for your hardware path above.

---

## Keeping the Module Updated (Moonraker)

Add this to `~/printer_data/config/moonraker.conf` to get updates through the
Mainsail / Fluidd update panel alongside Klipper:

```ini
[update_manager emu_nfc_reader]
type: git_repo
path: ~/nfc-reader
origin: https://github.com/cwiegert/NFC-Reader.git
primary_branch: main
managed_services: klipper
install_script: install.sh
```

Restart Moonraker after adding the section:

```bash
sudo systemctl restart moonraker
```

When an update is available, Moonraker pulls the latest code, re-runs `install.sh`
to refresh the symlinks, and restarts Klipper.

---

## Repository Layout

```
nfc-reader/
│
├── install.sh                        ← run once after cloning; re-run after manual git pull
│
├── klippy/
│   └── extras/
│       ├── nfc_gates/                ← Klipper extras package (shared library)
│       │   ├── __init__.py           ← [nfc_gates] handler — SPI / RC522 path
│       │   ├── rc522_driver.py       ← RC522 ISO14443A driver (SPI)
│       │   ├── pn532_driver.py       ← PN532 ISO14443A driver (I2C)
│       │   ├── spoolman_client.py    ← Spoolman REST API client (UID lookup)
│       │   ├── gate_state.py         ← debounce state machine (shared)
│       │   └── klipper_interface.py  ← GCode macro dispatch (shared)
│       └── nfc_gate.py               ← [nfc_gate laneN] handler — I2C / PN532 path
│
├── config/
│   ├── nfc_macros.cfg                ← Happy Hare macros — copy to printer_data/config/ (both paths)
│   ├── nfc_vars.cfg                  ← User settings (Spoolman URL, poll interval, debug) — edit this one
│   ├── nfc_gates_spi_rc522.cfg       ← Path A hardware config — copy to printer_data/config/
│   └── nfc_gate_i2c_pn532.cfg        ← Path B hardware config — copy to printer_data/config/
│
├── tests/                            ← Development utilities (not deployed to printer)
│   ├── simulate.py                   ← Interactive full-pipeline simulator (no hardware needed)
│   ├── lookup_uid.py                 ← Live Spoolman UID lookup test
│   ├── test_nfc_gate_config.py       ← Unit tests for NfcGateDefaults config handler
│   ├── test_pn532_driver.py          ← PN532 driver tests (mock I2C)
│   └── test_rc522_driver.py          ← RC522 driver tests (mock SPI)
│
└── docs/
    ├── spi-rc522/
    │   ├── setup.md                  ← Path A install walkthrough
    │   ├── wiring.md                 ← RC522 + Pico + CAN transceiver wiring
    │   └── troubleshooting.md
    ├── i2c-pn532/
    │   ├── setup.md                  ← Path B install walkthrough
    │   ├── wiring.md                 ← PN532 + EBB42 I2C wiring
    │   └── troubleshooting.md
    └── shared/
        ├── spoolman-integration.md   ← Spoolman setup, rfid field, UID registration
        ├── tag-writing.md            ← (redirects to spoolman-integration.md)
        └── debugging.md              ← klippy.log, NFC_GATE_STATUS, debug levels
```

---

## Quick Reference

### Check Gate Status

```
NFC_GATE_STATUS
```

### View Live Log

```bash
tail -f ~/printer_data/logs/klippy.log | grep nfc_gate
```

### Speed Up Testing

Edit `nfc_vars.cfg` and restart Klipper — restore production values when done:

```ini
# In nfc_vars.cfg — restore to production values when done
poll_interval:    5
absent_threshold: 1
```

### GCode Macros (same for both paths)

| Macro | Called when |
|---|---|
| `_NFC_SPOOL_CHANGED` | Tag placed — calls `MMU_GATE_MAP GATE=N SPOOLMAN_ID=X` |
| `_NFC_SPOOL_REMOVED` | Tag absent for `absent_threshold` polls — calls `MMU_GATE_MAP GATE=N SPOOLMAN_ID=-1` |
| `_NFC_TAG_NO_SPOOL` | Tag UID not registered in Spoolman — set the `rfid` extra field |

---

## Documentation Index

| Document | Contents |
|---|---|
| [SPI / RC522 Setup](docs/spi-rc522/setup.md) | Path A install: git clone, flash Pico, configure, Moonraker updater |
| [SPI / RC522 Wiring](docs/spi-rc522/wiring.md) | RC522 pinout, Pico GPIO table, CAN transceiver wiring |
| [SPI / RC522 Troubleshooting](docs/spi-rc522/troubleshooting.md) | Reader init failures, SPI errors, tag detection issues |
| [I2C / PN532 Setup](docs/i2c-pn532/setup.md) | Path B install: git clone, configure gate sections, Moonraker updater |
| [I2C / PN532 Wiring](docs/i2c-pn532/wiring.md) | PN532 pinout, EBB42 I2C pins, pull-up resistors |
| [I2C / PN532 Troubleshooting](docs/i2c-pn532/troubleshooting.md) | PN532 init failures, I2C conflicts, BME280 coexistence |
| [Spoolman Integration](docs/shared/spoolman-integration.md) | Add rfid extra field, read tag UIDs, register in Spoolman |
| [Debugging & Logs](docs/shared/debugging.md) | klippy.log filters, debug levels, NFC_GATE_STATUS output |
