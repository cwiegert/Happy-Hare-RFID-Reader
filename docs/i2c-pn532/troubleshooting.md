# Troubleshooting

[← Back to README](../../Readme.md) | [Setup](setup.md) | [Expert Debugging →](../shared/expert-low-level-i2c-debugging.md)

---

## Before Anything Else: Check MCU Firmware

> [!CAUTION]
> **The single most common source of PN532 failures is stale lane MCU firmware.**
>
> If you updated the Klipper host but did not rebuild and flash the EBB42 / lane MCU firmware, the MCU and host are running different protocol versions. This causes I2C transaction failures that look exactly like hardware problems.
>
> **Rebuild and flash every lane MCU. Then try again.**

---

## Failure: `PN532 did not respond`

The PN532 is not answering on the I2C bus at all.

Check in this order:

| # | Check |
|---|---|
| 1 | PN532 is set to I2C mode (SEL0=1, SEL1=0). Check DIP switches or solder jumpers. |
| 2 | VCC is 3.3 V (or 5 V if the board has an onboard regulator — but SDA/SCL must be 3.3 V). |
| 3 | GND is shared between the PN532 and the lane MCU. |
| 4 | SCL connects to `laneN:PB3`. SDA connects to `laneN:PB4`. They are not swapped. |
| 5 | `i2c_mcu` in config exactly matches the MCU name Klipper uses (e.g. `lane0`). |
| 6 | `i2c_address` is `36` (hex `0x24`) unless you changed the PN532 address pads. |
| 7 | Lane MCU firmware has been rebuilt and flashed to match the host Klipper version. |

After checking all of the above, run:

```gcode
NFC_GATE NAME=lane0 INIT=1
```

If INIT still fails, proceed to the [Expert: Low-Level I2C Debugging](../shared/expert-low-level-i2c-debugging.md) guide.

---

## Failure: `Unable to obtain 'i2c_read_response' response`

Klipper asked the MCU for an I2C read and got no response back.

This error almost always means one of:

1. **Stale MCU firmware** — rebuild and flash.
2. **PN532 not ready** — the PN532 accepted the write but is still processing. Try increasing `transceive_delay`.
3. **Wrong PN532 mode** — SPI or UART mode will accept the SCL/SDA lines being wiggled but won't produce a valid I2C response.
4. **SDA/SCL swapped** — especially easy to get wrong on PN532 modules with non-standard silkscreen labelling.
5. **Bus held by another device** — if the BME280 or another I2C device is stuck in a transaction, it can hold SDA low.
6. **Pullup resistance problem** — too weak (bus doesn't pull up fast enough) or too strong (overdrive).

**Systematic check:**

```gcode
NFC_GATE NAME=lane0 INIT=1
```

If INIT succeeds but normal polling produces this error, the issue is in the read timing. Try:

```ini
transceive_delay: 0.500
```

If INIT fails immediately, the bus is not functional. Check wiring and mode selection first.

---

## BME280 Fails After PN532 Is Added

The BME280 and PN532 can share the PB3/PB4 bus because their addresses differ. If the BME280 worked before and fails only after the PN532 is connected:

| Symptom | Likely cause |
|---|---|
| BME280 fails immediately on Klipper start | PN532 is in SPI or UART mode — it is disrupting the I2C bus |
| BME280 works, then fails after first INIT | PN532 power issue — voltage droop pulling SDA/SCL low |
| BME280 works intermittently | Excessive pull-up current, SDA/SCL marginal wiring |
| BME280 never works with PN532 connected | SDA/SCL short or wrong pin assignment |

**First fix to try:** Disconnect the PN532 completely. If the BME280 recovers, the problem is physical — the PN532 is disturbing the bus. Check mode selection above everything else.

---

## Tag Detected But No Spool Found

The hardware is working — the PN532 read the tag. The Spoolman lookup did not find a match.

Check:

| # | Check |
|---|---|
| 1 | `spoolman_url` points at the correct Spoolman instance. Test by visiting the URL in a browser. |
| 2 | `spoolman_rfid_key` matches the extra field name in Spoolman **exactly** (case-sensitive). |
| 3 | The UID is stored on the **spool** record, not the filament record. |
| 4 | UID formatting matches after normalization. `04AABBCC`, `04:AA:BB:CC`, `04-AA-BB-CC` all normalize to the same value — but check for typos. |

Run a full poll and watch the console:

```gcode
NFC_GATE NAME=lane0 POLL=1
```

If the console shows `tag UID ... is not registered in Spoolman`, copy the UID exactly and paste it into the spool's extra field.

Enable `debug: 2` and `console_output: True` to see the full Spoolman HTTP exchange in the log.

---

## False Spool Removals

The gate is declaring the spool removed when it is still physically there.

Cause: the PN532 is missing occasional reads due to tag angle, distance, vibration, or RF environment, and `absent_threshold` is too low.

**Fix:**

```ini
[nfc_gate]
poll_interval:    30
absent_threshold: 3
```

At these defaults a tag must be unreadable for approximately 90 seconds before removal fires. If you are still getting false removals, increase `absent_threshold`:

```ini
absent_threshold: 5
```

For bench testing where you want quick responses:

```ini
poll_interval:    5
absent_threshold: 1
```

Restore production values before a real print run.

---

## Klipper Startup Error: Config Section Not Found

```
Option 'i2c_mcu' in section 'nfc_gate lane0' is not a valid config option
```

or

```
Unknown config section 'nfc_gate'
```

Both mean the NFC Python module did not load. Check:

1. `nfc_gate.py` symlink exists in `~/klipper/klippy/extras/`.
2. `nfc_gates/` symlink exists in `~/klipper/klippy/extras/`.
3. Run `bash install.sh` again if either is missing.

---

## Klipper Startup Error: Include Order

```
Section 'nfc_gate lane0' defined before 'nfc_gate' base section
```

The includes in `printer.cfg` are in the wrong order. Fix:

```ini
# Must be in this order:
[include NFC/nfc_vars.cfg]
[include NFC/nfc_macros.cfg]
[include NFC/pn532_i2C.cfg]
```

`nfc_vars.cfg` (which defines `[nfc_gate]`) must come before `pn532_i2C.cfg` (which defines `[nfc_gate lane0]` etc.).

---

## Polling Is Running But Happy Hare Is Not Updating

The NFC reader is detecting tags and looking up spools, but Happy Hare is not being updated.

Check `nfc_macros.cfg`. The default `_NFC_SPOOL_CHANGED` calls:

```gcode
MMU_SPOOLMAN UPDATE=1 GATE=<gate> SPOOLID=<spool_id>
```

Possible causes:

- The macro was accidentally deleted or commented out.
- Your installed `~/printer_data/config/NFC/nfc_macros.cfg` still has an older body such as `MMU_GATE_MAP NEXT_SPOOLID=...` or `MMU_GATE_MAP GATE=...`.
- Moonraker does not have Spoolman configured, so Happy Hare cannot write the gate mapping to the Spoolman DB.
- Happy Hare is in a state where it rejects `MMU_SPOOLMAN UPDATE=1` (e.g. a print is active with gate locks on).

Test the macro boundary directly, without hardware:

```gcode
_NFC_SPOOL_CHANGED GATE=0 SPOOL_ID=1042 UID=04AABBCCDD
```

If this works, the macro is fine and the problem is upstream. If it fails, the macro or Happy Hare is the issue.

---

## Useful Diagnostic Commands

```gcode
NFC_GATE_STATUS                    ; all gates, current state
NFC_GATE NAME=lane0 STATUS=1       ; one gate
NFC_GATE NAME=lane0 INIT=1         ; re-init the PN532 reader
NFC_GATE NAME=lane0 SCAN=1         ; one raw read, no state machine
NFC_GATE NAME=lane0 POLL=1         ; one full pipeline cycle
```

When normal commands are insufficient, enable expert debug mode:

```ini
[nfc_gate]
low_level_debug:   True
console_output:    True
console_log_level: info
```

Restart Klipper, then:

```gcode
NFC_GATE NAME=lane0 HELP=1
```

See [Expert: Low-Level I2C Debugging](../shared/expert-low-level-i2c-debugging.md) for the complete manual step-through sequence.
