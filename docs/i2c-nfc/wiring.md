# NFC Reader Wiring

[README](../../Readme.md) | [Next: Install](../shared/install-uninstall.md)

This project supports two I2C NFC reader families. RC522 is also supported as
a UID-only SPI reader with `reader_type: rc522`; it does not use the I2C wiring
or address settings below.

| Reader | `reader_type` | Address | I2C bus | Best use |
|---|---|---|---|---|
| PN532 | `pn532` | `36` (`0x24`) fixed | Hardware I2C | Default, simple UID/NTAG/Bambu reader path |
| PN7160 | `pn7160` | `40-43` (`0x28-0x2B`) selectable | Hardware I2C recommended; software I2C supported | Type5/ISO15693, OpenPrintTag SLIX2, PN7160 hardware |

Use the reader-specific wiring page for the electrical details:

- [PN532 wiring](pn532-wiring.md)
- [PN7160 wiring](pn7160-wiring.md)

## Per-Lane Readers

For per-lane EMU readers, each filament gate normally gets one NFC reader wired
to that lane MCU / EBB42. Each lane has its own I2C bus, so readers on different
lane MCUs may reuse the same I2C address.

```text
lane0 EBB42  -> I2C -> NFC reader (gate 0)
lane1 EBB42  -> I2C -> NFC reader (gate 1)
lane2 EBB42  -> I2C -> NFC reader (gate 2)
```

## Shared Reader

The optional shared reader is a single NFC reader mounted inside the MMU body
and configured in `nfc_reader_shared.cfg`. It can be PN532 or PN7160. It still
must be wired to a Klipper MCU that has suitable SDA/SCL pins, such as the MMU
MCU, a toolhead MCU, or another MCU declared with an `[mcu ...]` section.

See [Shared Reader](../shared/shared-reader.md) for the workflow and config.

## Bus Choice

PN532 should use hardware I2C.

PN7160 supports Klipper software I2C, but hardware I2C is recommended. Software
I2C bit-bangs SDA/SCL in MCU firmware, which increases MCU load. Use software
I2C only when the selected MCU has no suitable hardware I2C bus and after bench
testing the reader.

## Common Bring-Up Order

1. Confirm the MCU connects normally before adding the reader.
2. Wire VCC and GND.
3. Wire SDA and SCL.
4. Set the correct `reader_type` and `i2c_address`.
5. Restart Klipper.
6. Run `NFC GATE=0 INIT=1` or `NFC_SHARED INIT=1`.
7. Run `NFC GATE=0 SCAN=1` or `NFC_SHARED SCAN=1` with a tag nearby.

If basic init fails, use the reader-specific troubleshooting checks first.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) - see [LICENSE](../../LICENSE).*
