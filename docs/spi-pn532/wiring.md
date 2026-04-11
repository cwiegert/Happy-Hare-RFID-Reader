# SPI / PN532 — Wiring Diagram & Pin Reference

[← Back to SPI/PN532 Setup](setup.md) | [← Back to Index](../../Readme.md)

---

## Overview

One **Raspberry Pi Pico** connects to the CAN bus and hosts up to 5 PN532 readers
on its hardware SPI1 bus. All readers share SCK, MOSI, and MISO.
Each reader has its own CS (chip-select) GPIO.

The PN532 uses **SPI mode 0** (CPOL=0, CPHA=0) with **LSB-first** byte order.
The driver handles bit-reversal in software — no special firmware configuration required.

---

## PN532 Mode Selection

Before wiring, set each PN532 module to **SPI mode**. Most breakout boards have a
DIP switch or solder jumpers to select the interface:

| SEL0 | SEL1 | Mode |
|---|---|---|
| L | L | **SPI** ← use this |
| H | L | I2C |
| L | H | HSU (UART) |

> Consult your module's datasheet if the switches are labelled differently.
> The SparkFun and Adafruit PN532 boards use the SEL0/SEL1 convention above.

---

## CAN Bus Transceiver

The Pico cannot drive CAN bus directly. A 3.3V CAN transceiver module
(e.g. SN65HVD230) is required between the Pico GPIO and the CAN bus differential pair.

> Use a **3.3V** transceiver. Do **not** use a 5V module (e.g. MCP2551) —
> the Pico's GPIO is not 5V tolerant.

> Tie the transceiver's RS / slope-control pin to GND for high-speed mode
> (required at 1 Mbit/s).

> The CAN bus must be terminated with 120 Ω between CANH and CANL at each physical
> end of the bus.

```
Raspberry Pi Pico          SN65HVD230
┌─────────────┐            ┌──────────────────┐
│  GP4 (TX) ──┼────────────┼─► TXD            │
│  GP5 (RX) ──┼────────────┼─◄ RXD            │
│  3V3 OUT  ──┼────────────┼─► VCC (3.3V)     │
│  GND      ──┼────────────┼─► GND            │
└─────────────┘            │   CANH ──────────┼──► CAN bus (CANH)
                           │   CANL ──────────┼──► CAN bus (CANL)
                           └──────────────────┘
```

---

## Full Wiring Diagram (5-gate example)

```
Raspberry Pi Pico
┌──────────────────────────────────────────┐
│                                          │
│  GP4 (CAN TX) ────────────────────────── TXD  SN65HVD230
│  GP5 (CAN RX) ────────────────────────── RXD  SN65HVD230
│                                          │
│  GP0  ──────────────────────────────── NSS  PN532 Gate 0
│  GP1  ──────────────────────────────── NSS  PN532 Gate 1
│  GP2  ──────────────────────────────── NSS  PN532 Gate 2
│  GP3  ──────────────────────────────── NSS  PN532 Gate 3
│  GP6  ──────────────────────────────── NSS  PN532 Gate 4
│                                          │
│  GP8  (SPI1 MISO) ─────────────┬──────── MISO PN532 Gate 0
│                                ├──────── MISO PN532 Gate 1
│                                ├──────── MISO PN532 Gate 2
│                                ├──────── MISO PN532 Gate 3
│                                └──────── MISO PN532 Gate 4
│                                          │
│  GP10 (SPI1 SCK)  ─────────────┬──────── SCK  PN532 Gate 0
│                                ├──────── SCK  PN532 Gate 1
│                                ├──────── SCK  PN532 Gate 2
│                                ├──────── SCK  PN532 Gate 3
│                                └──────── SCK  PN532 Gate 4
│                                          │
│  GP11 (SPI1 MOSI) ─────────────┬──────── MOSI PN532 Gate 0
│                                ├──────── MOSI PN532 Gate 1
│                                ├──────── MOSI PN532 Gate 2
│                                ├──────── MOSI PN532 Gate 3
│                                └──────── MOSI PN532 Gate 4
│                                          │
│  3V3 OUT ──────────────────────┬──────── VCC  PN532 Gate 0
│                       (also ──►│ VCC SN65HVD230)
│                                ├──────── VCC  PN532 Gate 1
│                                ├──────── VCC  PN532 Gate 2
│                                ├──────── VCC  PN532 Gate 3
│                                └──────── VCC  PN532 Gate 4
│                                          │
│  GND ──────────────────────────┬──────── GND  PN532 Gate 0
│                       (also ──►│ GND SN65HVD230)
│                                ├──────── GND  PN532 Gate 1
│                                ├──────── GND  PN532 Gate 2
│                                ├──────── GND  PN532 Gate 3
│                                └──────── GND  PN532 Gate 4
└──────────────────────────────────────────┘
```

---

## Pin Reference Table

| Pico GPIO | Function | Connected To | Notes |
|---|---|---|---|
| GP0 | PN532 CS (Gate 0) | PN532 Gate 0 NSS | Chip select — active low |
| GP1 | PN532 CS (Gate 1) | PN532 Gate 1 NSS | Chip select — active low |
| GP2 | PN532 CS (Gate 2) | PN532 Gate 2 NSS | Chip select — active low |
| GP3 | PN532 CS (Gate 3) | PN532 Gate 3 NSS | Chip select — active low |
| **GP4** | **CAN TX** | **SN65HVD230 TXD** | **CAN bus transmit** |
| **GP5** | **CAN RX** | **SN65HVD230 RXD** | **CAN bus receive** |
| GP6 | PN532 CS (Gate 4) | PN532 Gate 4 NSS | Chip select — active low |
| GP8 | SPI1 MISO | All PN532 MISO | Shared data in |
| GP10 | SPI1 SCK | All PN532 SCK | Shared clock — 1 MHz |
| GP11 | SPI1 MOSI | All PN532 MOSI | Shared data out |
| 3V3 OUT | — | PN532 VCC + transceiver VCC | 3.3V rail — do not use 5V |
| GND | — | PN532 GND + transceiver GND | Common ground |

---

## PN532 Module Pinout

| PN532 Pin | Function | Connect to |
|---|---|---|
| VCC | Power | Pico 3V3 OUT |
| GND | Ground | Pico GND |
| NSS | SPI Chip Select | Unique Pico GPIO per gate (GP0–GP6) |
| SCK | SPI Clock | Pico GP10 (shared) |
| MOSI | SPI data in to PN532 | Pico GP11 (shared) |
| MISO | SPI data out from PN532 | Pico GP8 (shared) |

> **Voltage:** PN532 modules run at 3.3V. The Pico's GPIO is 3.3V native — no level
> shifters required. Do **not** connect PN532 VCC to the Pico's VBUS (5V) pin.

> **No RST pin needed:** The PN532 is reset via the `GetFirmwareVersion` wake sequence
> in software. Leave the RST pin unconnected or tie it to 3.3V.

---

## Notes on Shared Lines

SCK, MOSI, MISO, VCC, and GND are shared across all PN532 modules.
Only the NSS (CS) line is unique per module — daisy-chain the shared lines across
all boards and run individual wires only for NSS.

**RF isolation:** All PN532 antenna coils are powered simultaneously. The CS pin
provides software isolation only — only one reader is selected on SPI at a time.
For physical RF isolation between adjacent gates, maintain at least **3 cm** separation
between antenna coils, or use ferrite backing behind each antenna.

**LSB-first note:** The PN532 SPI interface transfers data LSB first, which is the
opposite of most SPI devices. The driver handles this automatically via software
bit-reversal — no hardware or firmware configuration is required on the Pico.
