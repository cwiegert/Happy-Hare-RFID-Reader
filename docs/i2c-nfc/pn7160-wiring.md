# Wiring the PN7160

[NFC Reader Wiring](wiring.md) | [Setup](setup.md)

PN7160 is an I2C NFC controller. In this project it can be used as a per-lane
reader or as the shared reader by setting `reader_type: pn7160`.

## Basic Wiring

The normal PN7160 connection uses four wires:

```text
MCU / EBB42       PN7160 module
--------------------------------
SCL          ->   SCL
SDA          ->   SDA
3V3          ->   VCC
GND          ->   GND
```

Use 3.3V logic on SDA/SCL. If your PN7160 board has its own power regulator,
follow the module vendor's power input notes, but keep the I2C signal voltage
compatible with the MCU.

## Optional Pins

PN7160 boards may expose extra control pins:

```ini
# ven_pin: PA8
# irq_pin: ^PC6
```

`ven_pin` controls PN7160 VEN. It is optional, but recommended when possible
because it lets Klipper hard-reset / hard-power-down the chip. Without VEN,
normal reads still work, but an abnormal Klipper stop or failed debug session
can leave the chip in a state that software cannot fully reset.

`irq_pin` is optional. When omitted, the driver uses timing-based polling.
Use IRQ only when you have a spare input pin and want to experiment with lower
latency / less polling.

## I2C Address

PN7160 supports four I2C addresses selected by hardware address pins or DIP
switches:

| Decimal | Hex |
|---:|---:|
| `40` | `0x28` |
| `41` | `0x29` |
| `42` | `0x2A` |
| `43` | `0x2B` |

The configured address must match the module's hardware address selection.
Klipper will raise a config error if `reader_type: pn7160` uses any other
address.

If each lane has its own MCU or its own I2C bus, multiple PN7160 readers can
reuse the same address. If multiple PN7160 readers share one MCU/I2C bus, each
reader must use a unique address.

## Hardware I2C vs Software I2C

Hardware I2C is recommended for PN7160.

PN7160 can run on Klipper software I2C, but software I2C consumes more MCU time
because the firmware bit-bangs SDA and SCL. That extra load matters on lane MCUs
that also handle steppers, heaters, fans, endstops, or other sensors. Use
software I2C only when the hardware layout requires it.

## Per-Lane Example

```ini
[nfc_gate lane1]
enabled:                True
reader_type:            pn7160
i2c_address:            40
mmu_gate:               1
i2c_mcu:                mmu1
i2c_bus:                i2c3_PB3_PB4
startup_poll_delay:     0.5
```

## Shared Reader Example

```ini
[nfc_gate shared]
enabled:                True
reader_type:            pn7160
i2c_address:            40
i2c_mcu:                mmu
i2c_bus:                i2c3_PB3_PB4
shared:                 true
startup_polling:        1
```

## Bring-Up Checks

1. Confirm the module address switch/pins match `i2c_address`.
2. Confirm `reader_type: pn7160` is set on that lane/shared section.
3. Confirm `i2c_mcu` matches an existing Klipper `[mcu ...]`.
4. Start with hardware I2C if possible.
5. Restart Klipper and run `NFC GATE=<n> INIT=1` or `NFC_SHARED INIT=1`.
6. Run a raw scan with a tag nearby.

If the chip gets stuck or becomes unusually warm, stop testing and power-cycle
the module. If `ven_pin` is wired, a hardware reset can recover it without
removing power.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) - see [LICENSE](../../LICENSE).*
