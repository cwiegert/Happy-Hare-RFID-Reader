# Troubleshooting

[← README](../../Readme.md) | [Setup](setup.md) | [Expert Debug →](../shared/expert-low-level-i2c-debugging.md)

---

## Start Here: MCU Firmware

> [!CAUTION]
> **The most common source of PN532 failures is stale lane MCU firmware.**
>
> If you updated the Klipper host but didn't rebuild and flash the EBB42 firmware, the MCU and host are running different protocol versions. I2C failures from this look exactly like hardware problems — bad wiring, wrong mode, broken module.
>
> **Rebuild and flash every lane MCU. Then try again.**

---

## Klipper Won't Start — Config Errors

### `Unknown config section 'nfc_gate'`

The NFC Python module didn't load. Check that the symlinks exist:

```bash
ls -la ~/klipper/klippy/extras/nfc_gate.py
ls -la ~/klipper/klippy/extras/nfc_gates/
```

If missing, run the installer again:
```bash
cd ~/emu-nfc-reader && bash install.sh
```

---

### `Section 'nfc_gate lane0' defined before 'nfc_gate' base section`

The includes in `printer.cfg` are in the wrong order. It must be:

```ini
[include nfc/nfc_reader.cfg]     ← first
[include nfc/nfc_macros.cfg]
[include nfc/nfc_reader_hw.cfg]    ← last
```

---

### `Option 'i2c_mcu' in section 'nfc_gate lane0' is not a valid config option`

Same cause as above — the base `[nfc_gate]` section wasn't loaded before the lane section. Fix the include order.

---

## PN532 Not Responding

### `PN532 did not respond`

The reader isn't answering on the I2C bus at all.

Check in this order:

1. **I2C mode** — SEL0=1, SEL1=0 on the PN532 DIP switches. This is the most common mistake.
2. **Voltage** — VCC is 3.3V (or 5V through onboard regulator if your board supports it, but SDA/SCL must be 3.3V)
3. **GND** — shared between PN532 and lane MCU
4. **SDA/SCL not swapped** — PB3=SCL, PB4=SDA on the EBB42
5. **`i2c_mcu` name** — matches exactly what Klipper uses (e.g. `lane0`, not `mcu_lane0`)
6. **`i2c_address`** — is `36` (hex `0x24`) unless you changed the address pads
7. **MCU firmware** — rebuilt and flashed to match the host Klipper version

Then try:
```gcode
NFC GATE=0 INIT=1
```

Still failing? Go to [Expert: Low-Level I2C Debugging](../shared/expert-low-level-i2c-debugging.md).

---

### `Unable to obtain 'i2c_read_response' response`

Klipper asked the MCU for an I2C read and got no reply.

Most common causes:

| Cause | Fix |
|---|---|
| Stale MCU firmware | Rebuild and flash |
| PN532 in wrong mode | Check DIP switches (SPI/UART mode accepts wiggled lines but won't produce I2C responses) |
| PN532 not ready yet | Increase `transceive_delay` to `0.500` |
| SDA/SCL swapped | Swap them |
| Another I2C device stuck | Check if BME280 is holding the bus |

**Quick test:** If `INIT=1` succeeds but polling produces this error, the issue is timing. Try:
```ini
[nfc_gate]
transceive_delay: 0.500
```

If `INIT=1` fails immediately, the bus is not functional — check wiring and mode selection first.

---

## BME280 Fails After PN532 Is Added

The BME280 (address `0x76`) and PN532 (address `0x24`) share the PB3/PB4 bus without conflict. If the BME280 worked before and breaks after the PN532 is connected:

| Symptom | Likely cause |
|---|---|
| BME280 fails on Klipper start | PN532 is in SPI or UART mode — it's disrupting the bus |
| BME280 works, fails after first INIT | PN532 power issue — voltage droop pulling SDA/SCL low |
| BME280 intermittent | Excessive pull-up current or marginal wiring |
| BME280 never works with PN532 connected | SDA/SCL short or wrong pin assignment |

**Quick check:** Disconnect the PN532 completely. If the BME280 recovers, the fault is physical — the PN532 is disrupting the bus. Check mode selection first.

---

## Tag Detected But No Spool Found

The hardware is working — the PN532 read the tag. The Spoolman lookup failed.

```gcode
NFC GATE=0 POLL=1
```

Check:

1. **`spoolman_url`** — visit it in a browser from the Pi: `curl http://yourhost:7912/api/v1/spool`
2. **`spoolman_rfid_key`** — must match the extra field name in Spoolman **exactly** (case-sensitive)
3. **UID is on the spool record**, not the filament record
4. **No typos in the UID** — copy-paste from the console, don't transcribe by hand

Enable verbose logging to see the full HTTP exchange:

```ini
[nfc_gate]
debug:             2
console_output:    True
console_log_level: info
```

Restart Klipper, run `POLL=1`, and read the log.

---

## False Spool Removals

Gate declares the spool removed while it's still physically there.

**Cause:** The reader is occasionally missing reads (tag angle, vibration, RF environment), and `absent_threshold` is too low.

Default settings require 3 consecutive misses (~90s) before removal:
```ini
poll_interval:    30
absent_threshold: 3
```

If you're still getting false removals, raise the threshold:
```ini
absent_threshold: 5    ; ~150s before removal at default interval
```

---

## Happy Hare Not Updating

NFC is detecting tags and looking up spools, but Happy Hare's gate map isn't changing.

**Test the macro directly** — no hardware needed:

```gcode
_NFC_SPOOL_CHANGED GATE=0 SPOOL_ID=42 UID=04AABBCCDD
```

- **If this updates Happy Hare:** the macro is fine. The problem is upstream — a polling or Spoolman issue.
- **If this doesn't update Happy Hare:** check `nfc_macros.cfg` and Happy Hare command compatibility.

Common macro issues:

- Wrong `MMU_GATE_MAP` parameter syntax for your Happy Hare version
- Macro was accidentally deleted or commented out
- Happy Hare is in a state that rejects `MMU_GATE_MAP` (e.g. mid-print with gate locks)
- `spoolman_support: push` is not set in Happy Hare, so `SYNC=1` does nothing

---

## Diagnostic Command Summary

```gcode
NFC_STATUS                    ; all gates — current state
NFC GATE=0 STATUS         ; one gate
NFC GATE=0 INIT=1         ; re-initialize the PN532
NFC GATE=0 SCAN=1         ; one raw read, no state machine or Spoolman
NFC GATE=0 POLL=1         ; one complete cycle, watch for console output
```

For deeper investigation, enable expert debug mode:

```ini
[nfc_gate]
low_level_debug:   True
console_output:    True
console_log_level: info
```

Then:
```gcode
NFC GATE=0 HELP      ; lists all available debug commands
```

See [Expert: Low-Level I2C Debugging](../shared/expert-low-level-i2c-debugging.md) for the complete step-by-step manual sequence.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
