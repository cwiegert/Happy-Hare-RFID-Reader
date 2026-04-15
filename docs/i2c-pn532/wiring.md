# Wiring the PN532

[← README](../../Readme.md) | [Next: Install →](../shared/install-uninstall.md)

Each filament gate gets **one PN532 wired directly to that gate's EBB42**. There is no shared bus — each reader lives on its own lane MCU's I2C bus.

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

The PN532 defaults to address `0x24` (decimal `36`). Since each PN532 lives on its own dedicated I2C bus, all readers can stay at the default — no address conflict is possible.

If your breakout board has address selection pads (usually labeled A0/A1, separate from the mode selection jumpers):

| A1 | A0 | Address |
|:---:|:---:|:---:|
| 0 | 0 | `0x24` — default |
| 0 | 1 | `0x25` |
| 1 | 0 | `0x26` |
| 1 | 1 | `0x27` |

The address is set in `nfc_vars.cfg`:

```ini
[nfc_gate]
i2c_address: 36    ; decimal — 36 = 0x24
```

Only change this if you physically set the address pads.

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
7. Run `NFC_GATE GATE=0 INIT=1`
8. If INIT passes, run `NFC_GATE GATE=0 SCAN=1` with a tag nearby

If the BME280 breaks only after the PN532 is connected, the fault is physical — mode selection, swapped SDA/SCL, or pull-up interaction. It is not a Spoolman or Happy Hare issue.
