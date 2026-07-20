# Troubleshooting

[← README](../../Readme.md) | [Setup](setup.md) | [Expert Debug →](../shared/expert-low-level-i2c-debugging.md)

---

## Quick Links

- [Start Here: MCU Firmware](#start-here-mcu-firmware)
- [Klipper Won't Start - Config Errors](#klipper-wont-start--config-errors)
- [PN532 Not Responding](#pn532-not-responding)
- [PN7160 Not Responding](#pn7160-not-responding)
  - [`connect_nci failed: I2C label=CORE_RESET status=START_NACK`](#connect_nci-failed-i2c-labelcore_reset-statusstart_nack)
  - [`Unable to obtain 'i2c_read_response' response`](#unable-to-obtain-i2c_read_response-response)
- [PN5180 Not Responding](#pn5180-not-responding)
- [BME280 Fails After PN532 Is Added](#bme280-fails-after-pn532-is-added)
- [Tag Detected But No Spool Found](#tag-detected-but-no-spool-found)
- [False Spool Removals](#false-spool-removals)
- [Happy Hare Not Updating](#happy-hare-not-updating)
- [Diagnostic Command Summary](#diagnostic-command-summary)

---

## Start Here: MCU Firmware

> [!CAUTION]
> **The most common source of NFC reader failures is stale lane MCU firmware.**
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
cd ~/rfid-reader && bash install.sh
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

## PN7160 Not Responding

PN7160 startup problems are usually address, bus, or reset related.

### `connect_nci failed: I2C label=CORE_RESET status=START_NACK`

The MCU could not start an I2C transaction with the PN7160 while sending the
NCI `CORE_RESET` command. In practice this means the PN7160 did not answer on
the configured I2C bus.

Common causes:

- SDA/SCL wiring is disconnected, swapped, or on the wrong MCU pins.
- `i2c_mcu`, `i2c_bus`, or `i2c_address` does not match the hardware.
- The PN7160 is not powered, does not share ground with the MCU, or is held in
  reset.
- The PN7160 is stuck after a failed debug run, forced Klipper stop, or bus
  error.

After checking wiring and config, run:

```gcode
NFC GATE=0 INIT=1
```

or for the shared reader:

```gcode
NFC_SHARED INIT=1
```

If wiring and config look correct but the PN7160 still cannot communicate, power
cycle the PN7160 module. This is especially important when `ven_pin` is not
wired, because Klipper cannot hard-reset the chip without removing power.

Check in this order:

1. **`reader_type`** - the lane/shared section must set `reader_type: pn7160`.
2. **`i2c_address`** - must match the hardware address selection and be one of `40`, `41`, `42`, or `43`.
3. **`i2c_mcu` name** - must match an existing Klipper `[mcu ...]` section.
4. **SDA/SCL not swapped** - verify the selected bus pins.
5. **Power and ground** - confirm the module is powered and shares ground with the MCU.
6. **Bus type** - hardware I2C is recommended. Software I2C is supported, but consumes more MCU time.
7. **VEN state** - if `ven_pin` is wired, confirm the pin is correct. If VEN is not wired and the chip appears stuck, power-cycle the module.

Then try:

```gcode
NFC GATE=0 INIT=1
```

or for the shared reader:

```gcode
NFC_SHARED INIT=1
```

If the PN7160 becomes unusually warm after a failed test or forced Klipper stop,
stop testing and power-cycle it. Wiring `ven_pin` gives Klipper a reliable
hardware reset path.

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

## PN5180 Not Responding

PN5180 failures are usually power, SPI wiring, BUSY, or RST related. The
documented SLB connection uses nine wires: 5V, PSF 3.3V, GND, SCK, MISO, MOSI,
NSS/CS, BUSY, and RST. See [PN5180 wiring](pn5180-wiring.md) for the table.

### `invalid PN5180 health registers`

The reader returned invalid register data during initialization. Check these in
order:

1. **Both power rails** - the module needs 5V and the PSF connector's 3.3V.
2. **Ground** - PN5180 and the SLB/MMU must share GND.
3. **SPI direction** - SCK=`PB13`, MISO=`PB14`, MOSI=`PB15`, and NSS=`PA8` for
   the documented SLB wiring.
4. **Configuration** - `spi_bus: spi2_PB14_PB15_PB13`, `cs_pin: mmu:PA8`, and
   `reader_type: pn5180` must match the physical wiring.
5. **RST** - must be connected to the configured MCU output (`mmu:PC6` in the
   SLB example), not tied permanently high.

### `timeout waiting for PN5180 BUSY low`

The BUSY input stayed high longer than `pn5180_busy_timeout` (default `0.100`).
The driver resets and reinitializes the reader after this condition, but a
repeated timeout is a wiring or power fault, not a normal no-tag result.

1. Confirm PN5180 BUSY is connected to the configured input (`mmu:PB0` in the
   SLB example) and is not inverted.
2. Confirm BUSY is not shorted to 3.3V or 5V.
3. Confirm both power rails and RST are connected.
4. Run `NFC_SHARED INIT=1` for a shared reader or `NFC GATE=<n> INIT=1` for a
   lane reader after correcting the wiring.

Do not use `pn5180_command_delay`; the driver synchronizes each command from
the actual BUSY signal. For a complete wire-by-wire check, use
[PN5180 wiring](pn5180-wiring.md).

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

The hardware is working — the NFC reader read the tag. The Spoolman lookup failed.

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
NFC GATE=0 INIT=1         ; re-initialize the NFC reader
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
