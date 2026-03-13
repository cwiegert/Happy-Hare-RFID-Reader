# Debugging & Log Reference

[← Back to Index](../../Readme.md)

---

## NFC_GATE_STATUS Command

Run from the Klipper console (Mainsail / Fluidd terminal) at any time:

```
NFC_GATE_STATUS
```

**SPI / RC522 output** (one `[nfc_gates]` section, all gates in one command):
```
NFC gate status — 5 gates, poll 30s, absent threshold 3:
  Gate 0: spool 1042    UID A3F200CC
  Gate 1: empty
  Gate 2: spool 207     UID B1D4A209
  Gate 3: empty
  Gate 4: READER FAILED (check wiring)
```

**I2C / PN532 output** (one `[nfc_gate laneN]` section per gate):
```
NFC gate status  (4 gates configured):
  Gate 0  [lane0]:  spool 1042    UID A3F200CC
  Gate 1  [lane1]:  empty
  Gate 2  [lane2]:  spool 207     UID B1D4A209
  Gate 3  [lane3]:  tag A3F200CC  (UID not in Spoolman — set the 'rfid_tag' field on the spool record)
```

This reflects the last poll result — it is not a live read. To force an immediate
read, reduce `poll_interval` to `5` in the config and restart Klipper.

---

## Klipper Log

All NFC events are written to `klippy.log`:

```bash
# Follow live
tail -f ~/printer_data/logs/klippy.log | grep nfc_gate

# Search for gate changes only
grep "state change\|spool.*detected\|spool removed" ~/printer_data/logs/klippy.log

# Show the last 100 NFC lines
grep nfc_gate ~/printer_data/logs/klippy.log | tail -100
```

---

## Debug Verbosity

Set `debug:` in your config section to control log detail.

| Value | Output |
|---|---|
| `0` | Errors and warnings only |
| `1` | Major events: poll cycles, tag detected/removed, state changes **(default)** |
| `2` | Full trace: every register read/write (SPI) or I2C frame (PN532), FIFO contents, CRC values |

**SPI / RC522** — set inside `[nfc_gates]`:
```ini
[nfc_gates]
debug: 2
```

**I2C / PN532** — set in the `[nfc_gate]` base section in `nfc_vars.cfg` to apply to all lanes, or override per-lane in `nfc_gate_i2c_pn532.cfg`:
```ini
# nfc_vars.cfg — applies to all lanes
[nfc_gate]
debug: 2
```

Restart Klipper after changing. Revert to `debug: 1` after debugging (debug=2 is very chatty at a 30 s poll interval with 5 gates).

---

## Expected Log Output at debug: 1

### SPI / RC522 — successful startup and poll cycle

```
nfc_gates: connected to MCU 'nfc_pico', initialising 5 gates (poll=30s, absent_threshold=3, debug=1)
nfc_gates: gate 0 init — soft-resetting RC522
nfc_gates: gate 0 RC522 init OK (TxControl=0x83)
nfc_gates: gate 1 RC522 init OK (TxControl=0x83)
...
nfc_gates: 5/5 readers initialised
nfc_gates: polling thread started
nfc_gates: poll cycle — checking 5 gate(s)
nfc_gates: gate 0 — no tag (miss_count=1)
nfc_gates: gate 0 read_tag — tag detected UID=A3F200CC
nfc_gates: gate 0 — state change: CHANGED (uid=A3F200CC spool=1042)
```

### I2C / PN532 — successful startup

```
nfc_gate: [lane0] connected — gate=0, poll=30s, absent_threshold=3, debug=1
nfc_gate: [lane0] PN532 reader OK
nfc_gate: [lane0] polling thread started
nfc_gate: [lane0] gate 0 — no tag (miss 1)
nfc_gate: [lane0] gate 0 — CHANGED uid=A3F200CC spool=1042
```

---

## Reducing Poll Interval for Testing

Change `poll_interval` and `absent_threshold` for faster feedback during setup.
Do this in the config file and restart Klipper — no hardware changes needed.

```ini
# Fast testing settings — restore to production values when done
poll_interval:    5    # check every 5 seconds instead of 30
absent_threshold: 1    # report removal after first missed poll
```

Production values: `poll_interval: 30` / `absent_threshold: 3`
(3 missed polls = ~90 seconds absence before REMOVED fires — prevents false removals from RF glitches.)

---

## Simulating Events Without Hardware

Use the Klipper console to test your Happy Hare macro integration directly,
without waiting for a physical tag:

```
# Simulate spool placed on gate 0
_NFC_SPOOL_CHANGED GATE=0 SPOOL_ID=1042 UID=A3F200CC

# Simulate spool removed from gate 0
_NFC_SPOOL_REMOVED GATE=0

# Simulate unwritten tag
_NFC_TAG_NO_SPOOL GATE=2 UID=DEADBEEF
```

Verify that `MMU_GATE_MAP` updates correctly in Happy Hare before deploying hardware.

---

## Testing Utilities (Development)

The `tests/` directory contains standalone scripts for testing without a live printer.
These are **not deployed** to the printer — run them from your development machine.

### simulate.py — Full pipeline simulator

Runs a full gate polling loop with a fake GCode dispatcher. No Klipper, no hardware.

```bash
# From the project root
python3 tests/simulate.py
```

Available commands at the `sim>` prompt:

| Command | Description |
|---|---|
| `place <gate> <spool_id>` | Simulate placing spool on a gate |
| `place <gate> uid <uid_hex>` | Simulate a tag with no matching spool |
| `place <gate> lookup <uid_hex>` | Look up UID in Spoolman and place with resolved spool |
| `remove <gate>` | Simulate removing a tag (debounce auto-applied) |
| `status` | Show current simulated gate state |
| `poll` | Manually trigger one poll cycle |
| `set poll_interval <s>` | Change poll interval |
| `set absent_threshold <n>` | Change debounce threshold |
| `set spoolman_url <url>` | Change Spoolman URL |
| `set spoolman_rfid_key <key>` | Change RFID extra-field name |

### lookup_uid.py — Live Spoolman lookup

Queries a real Spoolman instance to verify a UID is registered correctly.

```bash
# From the project root
python3 tests/lookup_uid.py <uid> [spoolman_url] [rfid_field_name]

# Examples
python3 tests/lookup_uid.py A3F200CC
python3 tests/lookup_uid.py A3F200CC http://192.168.1.50:7912
python3 tests/lookup_uid.py A3F200CC http://192.168.1.50:7912 rfid_tag
```

If omitted, `spoolman_url` defaults to `http://mainsailos.local:7912` and
`rfid_field_name` defaults to `rfid_tag`. The UID is normalised automatically —
colons, hyphens, and lowercase are all accepted.
