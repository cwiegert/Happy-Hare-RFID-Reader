# Wiring the PN5180

[NFC Reader Wiring](wiring.md) | [Configuration](../shared/configuration.md) | [Setup](setup.md)

PN5180 is an SPI NFC controller. In this project it supports ISO14443A tags
(including NTAG and authenticated MIFARE/Bambu reads) and SLIX2
(ISO15693/Type-5) tags.
It can be configured as a shared reader or as a per-lane reader with
`reader_type: pn5180`.

SLIX2 support is not limited to OpenPrintTag. After the Type-5 user memory is
read, the reader path removes the Type-5 Capability Container when present and
passes the remaining bytes to the normal tag parser. The payload is therefore
handled according to its actual supported format, not a reader-specific format.

This page uses the Happy Hare SLB/MMU board as a concrete wiring example. Pin
names and power connectors vary on other boards; use their equivalent MCU GPIO
and power connections instead.

## Required Connections

The PN5180 setup requires all nine connections below. `BUSY` and `RST` are not
optional: the driver waits for the active-high BUSY signal around every command
and uses RST to recover a reader that has stopped responding.

| PN5180 module pin | SLB/MMU connection | Config setting | Required purpose |
|---|---|---|---|
| `SCK` | `PB13` | `spi_bus: spi2_PB14_PB15_PB13` | SPI clock |
| `MISO` | `PB14` | `spi_bus: spi2_PB14_PB15_PB13` | SPI data from reader |
| `MOSI` | `PB15` | `spi_bus: spi2_PB14_PB15_PB13` | SPI data to reader |
| `NSS` / `CS` | `PA8` | `cs_pin: mmu:PA8` | SPI chip select |
| `BUSY` | `PB0` | `busy_pin: mmu:PB0` | Active-high command-complete signal |
| `RST` | `PC6` | `reset_pin: mmu:PC6` | MCU-controlled hardware reset |
| `5V` | SLB/MMU 5V supply | - | RF/front-end power |
| `3.3V` | **PSF connector 3.3V** | - | Logic/SPI power; must be connected |
| `GND` | SLB/MMU GND | - | Common ground |

> [!IMPORTANT]
> The PN5180 breakout used for this wiring needs both `5V` and `3.3V`. Do not
> omit the 3.3V wire or replace it with 5V. Connect its 3.3V pin to the PSF
> connector's 3.3V supply, and ensure every signal shares the same ground.

The module's `IRQ`, `GPIO`, and `AUX` pins are not used by the current driver.
It polls the PN5180 internal IRQ status through SPI, so leave those pins
unconnected unless another circuit requires them.

## SPI Speed

Both PN5180 and RC522 default to `spi_speed: 500000` with hardware SPI. This is
a conservative starting point for MMU wiring. Software SPI can also be used
when the MCU has no suitable hardware bus, but set `spi_speed: 100000` because
bit-banged SPI is less tolerant of high rates.

## Shared Reader Configuration

Put this in `[nfc_gate shared]` in `nfc_reader_shared.cfg` for the SLB wiring
above. Keep the existing `i2c_mcu: mmu` line in the section; it identifies the
reader's Klipper MCU even though the PN5180 itself uses SPI.

```ini
[nfc_gate shared]
enabled:                True
reader_type:            pn5180
i2c_mcu:                mmu
spi_bus:                spi2_PB14_PB15_PB13
cs_pin:                 mmu:PA8
reset_pin:              mmu:PC6
busy_pin:               mmu:PB0
spi_speed:              500000
shared:                 true
startup_polling:        1
```

`reset_pin` has no implicit fallback and must be present in the configuration.
`busy_pin` defaults to `mmu:PB0`, but explicitly setting it is recommended so
the configured wire is clear. The BUSY wire itself is mandatory in either case.

## Per-Lane Configuration

Use the same reader-specific settings in the matching lane section. Replace the
MCU prefix and SPI bus with the actual lane MCU wiring.

```ini
[nfc_gate lane0]
enabled:                True
reader_type:            pn5180
mmu_gate:               0
i2c_mcu:                mmu
spi_bus:                spi2_PB14_PB15_PB13
cs_pin:                 mmu:PA8
reset_pin:              mmu:PC6
busy_pin:               mmu:PB0
spi_speed:              500000
```

For software SPI, replace `spi_bus` with Klipper's
`spi_software_sclk_pin`, `spi_software_mosi_pin`, and
`spi_software_miso_pin` settings, retain `cs_pin`, `busy_pin`, and `reset_pin`,
and set `spi_speed: 100000`.

## Bring-Up Checks

1. Confirm all nine connections, especially both power rails and the common
   ground.
2. Confirm `BUSY` reaches the configured GPIO and is not inverted. It is
   active high.
3. Confirm `RST` reaches the configured GPIO; do not tie RST permanently high.
4. Confirm the selected SPI bus matches `PB13`/`PB14`/`PB15` on the SLB.
5. Restart Klipper and run `NFC_SHARED INIT=1` or `NFC GATE=<n> INIT=1`.
6. Run `NFC_SHARED SCAN=1` or `NFC GATE=<n> SCAN=1` with a tag nearby.

If the reader reports a BUSY timeout or invalid health registers, first check
the 3.3V wire, BUSY, RST, MISO, chip select, and common ground. A working reset
wire lets the driver recover from a communication lockup without unplugging the
module.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) - see [LICENSE](../../LICENSE).*
