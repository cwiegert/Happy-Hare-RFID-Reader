# QIDI Box RFID Reference

Source: <https://wiki.qidi3d.com/en/QIDIBOX/RFID>

This note captures the QIDI Box tag details that matter when debugging QIDI
rich-tag reads.

## Tag Hardware

- Chip: FM11RF08S, MIFARE Classic 1K compatible
- RF protocol: ISO/IEC 14443-A
- Frequency: 13.56 MHz
- EEPROM layout: 16 sectors, 4 blocks per sector, 16 bytes per block

## QIDI Data Location

QIDI stores the filament payload in sector 1, block 0. This is absolute block
number 4.

The first three bytes of block 4 are:

| Byte | Meaning | Range / Notes |
| --- | --- | --- |
| 0 | Material code | 1-50 |
| 1 | Color code | 1-24 |
| 2 | Manufacturer code | Default value is 1 |

The current parser reads this as offsets `raw[64]`, `raw[65]`, and `raw[66]`
because absolute block 4 starts at byte offset `4 * 16 = 64` in a flat MIFARE
block dump.

## Auth Notes

If QIDI tags fail to rich-read, validate the sector 1 Key A path:

- Primary Key A reported for QIDI: `D3 F7 D3 F7 D3 F7`
- Fallback/factory Key A: `FF FF FF FF FF FF`

QIDI data is only in sector 1, so a QIDI-specific retry should read sector 1
only. This avoids reader-specific behavior where a failed sector 0 auth can
prevent PN7160/RC522 from reaching sector 1.

## Manufacturer Code

The tag stores a numeric manufacturer code, not a manufacturer name string.
QIDI's wiki documents the default manufacturer value as `1`. BoxRFID-Touch
maps manufacturer IDs through its active manufacturer database, where `0` is
Generic and `1` is QIDI by default.
