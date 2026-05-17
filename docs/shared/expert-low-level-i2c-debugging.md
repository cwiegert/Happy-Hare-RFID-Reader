# Expert: Low-Level PN532 I2C Debugging

[← Back to README](../../Readme.md) | [Troubleshooting](../i2c-pn532/troubleshooting.md)

---

> [!WARNING]
> ## Expert Section — Read This First
>
> The commands in this section send raw bytes directly to the PN532 over I2C. They **bypass the normal state machine** entirely. Sending the wrong sequence or leaving a step half-finished can put the PN532 into a state where normal polling fails until the MCU power cycles or Klipper restarts.
>
> **Use these commands only:**
> - During initial bring-up of a new PN532 reader when normal `INIT=1` fails.
> - When you have exhausted normal troubleshooting and need to observe exactly what the bus is doing.
>
> **Do not leave `low_level_debug: True` enabled during printing.**

---

## Enabling Expert Mode

In `nfc_reader.cfg`:

```ini
[nfc_gate]
low_level_debug:   True
console_output:    True
console_log_level: info
```

Restart Klipper. When `low_level_debug` is enabled, Klipper starts cleanly but **the PN532 is not initialized**. The driver skips the wake sequence and SAMConfiguration entirely so no driver-initiated bus traffic interferes with your manual commands. The log will confirm this:

```
init: gate 0 (PN532) low_level_debug enabled — skipping wake and SAMConfiguration
```

You must run the manual init sequence (Phase 1 + Phase 2 below) before any tag read commands will work. Then ask for the command list:

```gcode
NFC GATE=0 HELP
```

This prints the full manual init sequence with the exact commands to paste, in order.

---

## What Expert Mode Exposes

Expert mode ports the bring-up workflow from the original `PN_CONSOLE` diagnostic tool into Klipper GCode. Each `STEP=` command executes one discrete PN532 bus transaction and reports exactly what was sent and what came back, before and after.

### Transparency output format

Every command prints:

```
NFC[lane0]: FIRMWARE WRITE before: 00 00 FF 02 FE D4 02 2A 00
NFC[lane0]: FIRMWARE WRITE after:  OK
NFC[lane0]: NEXT: NFC GATE=0 STEP=FIRMWARE_ACK
```

- **before**: the bytes that are about to be sent.
- **after**: the result (`OK`, the raw bytes read, or an error).
- **NEXT**: the next command to paste when this step succeeded.

This "paste the next command" pattern comes from the original console bring-up flow, where you run one command at a time and observe the result before proceeding.

---

## PN532 I2C Protocol Background

Understanding what you are looking at requires knowing the PN532 frame format.

### Frame structure

**Write frame (host → PN532):**

```
00 00 FF  LEN  LCS  TFI  CMD  [params]  DCS  00
```

**Read frame (PN532 → host):**

```
STATUS  00  00  FF  LEN  LCS  TFI  CMD  [data]  DCS  00
```

| Field | Meaning |
|---|---|
| `STATUS` | First byte of every I2C read. `0x01` = ready, `0x00` = busy (PN532 still processing). |
| `LEN` | Number of bytes in the data field (TFI + CMD + payload). |
| `LCS` | Length checksum: `(-LEN) & 0xFF`. LEN + LCS = 0 mod 256. |
| `TFI` | Transport Frame Identifier. `0xD4` = host→PN532, `0xD5` = PN532→host. |
| `CMD` | Command code. Response code is `CMD + 1`. |
| `DCS` | Data checksum: `(-sum(data_field)) & 0xFF`. |

### ACK frame

After every command write, the PN532 sends an ACK before it processes the command. The ACK frame is:

```
00 00 FF 00 FF 00
```

When read over I2C (including the status byte at the front):

```
01 00 00 FF 00 FF 00
```

The leading `01` is the STATUS byte (ready). The ACK itself is the remaining 6 bytes.

### PN532 command codes

| Command | Code | Purpose |
|---|---|---|
| `GetFirmwareVersion` | `0x02` | Verify PN532 is alive, get firmware version |
| `SAMConfiguration` | `0x14` | Configure Security Access Module (required before tag reads) |
| `InListPassiveTarget` | `0x4A` | Detect ISO14443A tag and return UID |
| `InRelease` | `0x52` | Release/deselect the current target |

---

## Manual Init Sequence

> [!IMPORTANT]
> **Phase 1 and Phase 2 are mandatory.** The PN532 is not initialized when Klipper starts in expert mode. You must complete both phases before tag detection (Phase 3) or any other read commands will work.

Run one command at a time. Wait for the response before running the next. If any step fails, do not proceed — diagnose the failure at that step.

### Phase 1: Wake and firmware check

```gcode
NFC GATE=0 STEP=WAKEUP
NFC GATE=0 STEP=READY
NFC GATE=0 STEP=FIRMWARE_WRITE
NFC GATE=0 STEP=FIRMWARE_ACK
NFC GATE=0 STEP=FIRMWARE_READY
NFC GATE=0 STEP=FIRMWARE_RESPONSE
```

**What each step does:**

| Step | Bus operation | Expected result |
|---|---|---|
| `WAKEUP` | Writes `0x00` to nudge the PN532 transport out of power-down mode | No response needed |
| `READY` | Reads 1 byte | `01` = ready |
| `FIRMWARE_WRITE` | Writes the `GetFirmwareVersion` command frame | `OK` |
| `FIRMWARE_ACK` | Reads 7 bytes | `01 00 00 FF 00 FF 00` |
| `FIRMWARE_READY` | Polls the STATUS byte until `01` | `01` |
| `FIRMWARE_RESPONSE` | Reads the full firmware response | `01 00 00 FF 06 FA D5 03 ...` containing IC, Ver, Rev, Support bytes |

If `FIRMWARE_WRITE` succeeds but `FIRMWARE_ACK` returns wrong bytes or errors, the problem is in the write→ACK round trip: timing, MCU firmware version, or I2C bus quality.

### Phase 2: SAM configuration

```gcode
NFC GATE=0 STEP=SAM_WRITE
NFC GATE=0 STEP=SAM_ACK
NFC GATE=0 STEP=SAM_READY
NFC GATE=0 STEP=SAM_RESPONSE
```

`SAMConfiguration` sets the PN532 to normal mode (no SAM involvement). It must succeed before `InListPassiveTarget` will work.

### Phase 3: Tag detect (optional — requires Phase 1 + 2 complete)

With the PN532 initialized, you can test tag detection:

```gcode
NFC GATE=0 STEP=PASSIVE_WRITE
NFC GATE=0 STEP=PASSIVE_ACK
NFC GATE=0 STEP=PASSIVE_READY
NFC GATE=0 STEP=PASSIVE_RESPONSE LEN=30
```

Hold a tag near the reader before running `PASSIVE_WRITE`. The `PASSIVE_RESPONSE` bytes will contain the UID if a tag was found.

---

## Step Reference

| Step | PN532 operation | What it sends / reads |
|---|---|---|
| `WAKEUP` | Nudge transport | Writes `0x00` |
| `READY` | Read STATUS byte | Reads 1 byte. Expects `01`. |
| `FIRMWARE_WRITE` | `GetFirmwareVersion` command | Writes full PN532 frame for command `0x02` |
| `FIRMWARE_ACK` | Read ACK for firmware command | Reads 7 bytes. Expects `01 00 00 FF 00 FF 00`. |
| `FIRMWARE_READY` | Wait for firmware response readiness | Polls STATUS until `01` |
| `FIRMWARE_RESPONSE` | Read and parse firmware response | Reads full response frame, prints IC/Ver/Rev |
| `SAM_WRITE` | `SAMConfiguration` command | Writes frame for command `0x14` |
| `SAM_ACK` | Read ACK for SAM command | Reads 7 bytes. Expects ACK pattern. |
| `SAM_READY` | Wait for SAM response readiness | Polls STATUS until `01` |
| `SAM_RESPONSE` | Read and parse SAM response | Reads response, confirms success |
| `PASSIVE_WRITE` | `InListPassiveTarget` command | Writes frame for command `0x4A` |
| `PASSIVE_ACK` | Read ACK for passive target command | Reads 7 bytes. Expects ACK pattern. |
| `PASSIVE_READY` | Wait for tag-detect response readiness | Polls STATUS until `01` (waits up to `transceive_delay`) |
| `PASSIVE_RESPONSE` | Read tag-detect response | Reads `LEN` bytes. Contains UID if tag found. |

---

## Direct ACK Timing Probe

`FIRMWARE_ACK_DIRECT` writes `GetFirmwareVersion` and immediately reads the ACK frame with a configurable delay between write and read:

```gcode
NFC GATE=0 STEP=FIRMWARE_ACK_DIRECT DELAY=0.050
```

Use this to find the minimum delay that still produces a valid ACK. Start at 50 ms and increase:

```gcode
NFC GATE=0 STEP=FIRMWARE_ACK_DIRECT DELAY=0.100
NFC GATE=0 STEP=FIRMWARE_ACK_DIRECT DELAY=0.200
```

If you find that ACK reads fail below a certain delay, that is a signal about your bus latency (CAN round-trip + PN532 processing). Increase `transceive_delay` to match.

---

## Raw Tools

These bypass the `STEP=` sequence entirely and give direct bus access.

### `READY_READ` — Read the PN532 status byte

```gcode
NFC GATE=0 READY_READ=1
```

Reads 1 byte from the PN532 I2C address.

| Result | Meaning |
|---|---|
| `01` | PN532 is ready — a response is waiting in its buffer |
| `00` | PN532 is busy — still processing the last command |

Use this to check whether the PN532 is alive and whether a previous command completed.

---

### `ACK_READ` — Read the ACK frame

```gcode
NFC GATE=0 ACK_READ=1 LEN=7
```

First reads the STATUS byte. If STATUS is `01` (ready), reads `LEN` more bytes.

**Expected good ACK:**

```
01 00 00 FF 00 FF 00
```

The `01` is STATUS (ready). The remaining 6 bytes are the PN532 ACK frame. If you see anything else, the PN532 is either not ready, or the previous write did not produce a standard ACK.

---

### `RAW_READ` — Read raw bytes

```gcode
NFC GATE=0 RAW_READ=1 LEN=1
```

Reads exactly `LEN` bytes from the PN532 I2C address without any interpretation. The raw bytes are printed to the console.

---

### `RAW_WRITE` — Write raw bytes

```gcode
NFC GATE=0 RAW_WRITE=00
NFC GATE=0 RAW_WRITE=00,00,FF,02,FE,D4,02,2A,00
```

Writes the specified bytes exactly as given. Accepts space, comma, colon, or dash-separated hex values. No framing is added.

---

### `RAW_CMD` — Write a framed PN532 command

```gcode
NFC GATE=0 RAW_CMD=02
```

Builds and sends a complete PN532 command frame for the given command byte. For command `0x02` (`GetFirmwareVersion`) with no additional parameters, the full frame is:

```
00 00 FF 02 FE D4 02 2A 00
```

The driver calculates `LEN`, `LCS`, `TFI`, `DCS` and the preamble/postamble automatically.

---

## Disabling Expert Mode

When bring-up is complete:

```ini
[nfc_gate]
low_level_debug:   False
console_output:    False
```

Restart Klipper.

Normal polling (`READ=1`) will now function without interference from manual bus commands.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
