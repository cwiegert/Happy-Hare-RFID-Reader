---
name: Bug report
about: Report a problem with the EMU NFC Gate Reader
title: '[BUG] '
labels: bug
assignees: ''
---

## Reader Setup
- **Reader type:** lane / shared  *(delete one)*
- **Lane count:** 
- **MCU type:** EBB42 / SHT / other:
- **Klipper version:** *(run `klipper --version` or check Mainsail/Fluidd)*
- **Happy Hare version:**
- **NFC Reader commit:** *(run `git -C ~/rfid-reader rev-parse --short HEAD`)*

## Spoolman
- **Spoolman version:**
- **Tag mode:** spoolman (UID-only) / rich  *(delete one)*
- **Tag type:** NTAG213 / NTAG215 / NTAG216 / Bambu / other:

## Description
*A clear description of what went wrong.*

## Steps to Reproduce
1. 
2. 
3. 

## Expected Behavior
*What you expected to happen.*

## Actual Behavior
*What actually happened.*

## Logs
*Paste relevant output from `~/printer_data/logs/nfc_reader.log` and `~/printer_data/logs/klippy.log`.*
*Tip: set `log_level: 2` in `nfc_reader.cfg` before reproducing the issue for more detail.*

<details>
<summary>nfc_reader.log</summary>

```
paste here
```

</details>

<details>
<summary>klippy.log (relevant section)</summary>

```
paste here
```

</details>
