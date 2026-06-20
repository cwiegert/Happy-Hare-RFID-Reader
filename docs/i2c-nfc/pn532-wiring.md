# Wiring the PN532

[NFC Reader Wiring](wiring.md) | [Setup](setup.md)

For per-lane EMU readers, each filament gate gets **one PN532 wired directly to
that gate's EBB42**. There is no shared bus across the per-lane readers — each
reader lives on its own lane MCU's I2C bus.

The optional shared reader is the exception: it is a single PN532 mounted inside
the MMU body and configured separately in `nfc_reader_shared.cfg`. It still
must be wired to **some Klipper MCU's available I2C bus** — for example the MMU
MCU, a toolhead MCU, or another controller already declared with an `[mcu ...]`
section. The installer can write `i2c_mcu` / `i2c_bus` into
`nfc_reader_shared.cfg`, but it does not create or validate that physical bus;
you must pick real SDA/SCL pins supported by that MCU and wire the PN532 there.
See [Shared Reader](../shared/shared-reader.md) for that wiring/config flow.

```
lane0 EBB42  ←I2C→  PN532 (gate 0)
lane1 EBB42  ←I2C→  PN532 (gate 1)
lane2 EBB42  ←I2C→  PN532 (gate 2)
  ...
```

---

## Before You Wire Anything

> [!IMPORTANT]
> **Set the PN532 to I2C mode first, before connecting any wires.** The mode is selected by two jumpers or DIP switches on the PN532 board (labeled SEL0/SEL1, or sometimes A0/A1). If the mode is wrong, the reader will not respond on the I2C bus — and the symptoms look exactly like a wiring fault.

| Mode | SEL0 | SEL1 |
|---|:---:|:---:|
| SPI | 0 | 0 |
| **I2C ← set this** | **1** | **0** |
| UART / HSU | 0 | 1 |

Set **SEL0 = 1, SEL1 = 0**. Check your board's silkscreen — some boards number the switches in reverse, or label them differently. When in doubt, check the NXP PN532 datasheet (UM10232), Table 3.

---

## Soldering the PN532

The PN532 antenna pads are fragile — the copper ring on the top (antenna) side will lift off the board if overheated. Always solder from the back.

**Method:**

1. Insert the wire from the **back** of the board so it pokes through the hole toward the antenna side.
2. Apply the iron to the back of the board. Let solder pool up on the back pad.
3. Feed enough solder that the pool flows **through the hole** and wets the antenna-side ring — you should see a small fillet appear on the top.
4. Remove heat immediately once the solder flows through. Lingering heat is what lifts the antenna-side ring.

> [!WARNING]
> **Never apply the iron directly to the antenna-side pad.** The copper ring on the top of the board is bonded to a thin substrate and will delaminate with sustained heat. If the ring lifts, the mechanical connection is gone even if the hole still looks filled.

A proper joint: solder pool visible on the back, small filled fillet on the antenna side, wire cannot be pulled out by hand.

---

## Pin Connections

Connect each PN532 to its lane's EBB42 using these four wires:

```
EBB42 Pin       →    PN532 Module
──────────────────────────────────
PB3  (I2C SCL)  →    SCL
PB4  (I2C SDA)  →    SDA
3V3             →    VCC  (or the 3.3V pad if your board has both)
GND             →    GND
```

> [!WARNING]
> **3.3V logic only on SDA and SCL.** Some PN532 breakout boards accept 5V on the VCC pin through an onboard regulator, but the I2C signal lines must be 3.3V. The EBB42 is a 3.3V device. Never connect 5V to PB3 or PB4.

The matching Klipper config for these pins:

```ini
[nfc_gate lane0]
i2c_mcu: lane0
i2c_bus: i2c3_PB3_PB4
```

---

## I2C Address

The PN532 I2C address is **fixed at `0x24` (decimal `36`)** by the chip — it cannot be changed. The two pads/jumpers on the breakout board (SEL0/SEL1, sometimes labeled A0/A1) select the **communication protocol**, not the address. For I2C you must have SEL0=1, SEL1=0 (see the mode table above).

The address is set in `nfc_reader.cfg`:

```ini
[nfc_gate]
i2c_address: 36    ; fixed — 36 = 0x24, do not change
```

---

## Sharing the Bus with a BME280

The EBB42 typically has a BME280 temperature/humidity sensor on the same PB3/PB4 bus. This is fine — the PN532 (`0x24`) and BME280 (`0x76`) have different addresses and coexist without conflict.

> [!NOTE]
> If the BME280 worked before you added the PN532 and now fails, it is almost always a physical issue — the PN532 is in SPI or UART mode and disrupting the bus, or there is a wiring problem. See [Troubleshooting: BME280 fails after PN532 is added](troubleshooting.md#bme280-fails-after-pn532-is-added).

---

## Pull-up Resistors

I2C needs pull-up resistors on SDA and SCL. Most PN532 breakout boards include 10kΩ pull-ups, and the EBB42 also has its own. These typically work fine together.

If you add more boards (each with their own pull-ups), the parallel resistance drops and can cause marginal signaling:

- 2× 10kΩ boards in parallel → 5kΩ — fine
- 3× boards → ~3.3kΩ — usually fine
- 6+ boards → ~1.7kΩ — may be too strong

If you suspect pull-up issues: remove or bridge (disable) the PN532 board's onboard resistors and rely on the MCU's pull-ups alone.

---

## Wire Length

Keep wires short for initial bring-up — under 20cm if possible. Longer cables add capacitance that rounds signal edges, which makes marginal timing worse.

Once confirmed working, cables up to ~50cm are usually fine at 400kHz. If you see intermittent failures with longer cables, drop the I2C speed:

```ini
[nfc_gate lane0]
i2c_speed: 100000    ; 100kHz instead of 400kHz
```

---

## Bring-Up Order

Follow this sequence to avoid chasing phantom failures:

1. Confirm Happy Hare sees the lane MCU normally (no PN532 connected yet)
2. If a BME280 is fitted, confirm it reads correctly before touching anything
3. Set the PN532 DIP switches to I2C mode (SEL0=1, SEL1=0)
4. Connect VCC and GND
5. Connect SDA and SCL
6. Restart Klipper
7. Run `NFC GATE=0 INIT=1`
8. If INIT passes, run `NFC GATE=0 SCAN=1` with a tag nearby

If the BME280 breaks only after the PN532 is connected, the fault is physical — mode selection, swapped SDA/SCL, or pull-up interaction. It is not a Spoolman or Happy Hare issue.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
