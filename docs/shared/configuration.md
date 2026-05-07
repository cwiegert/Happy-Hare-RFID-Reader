# Configuration Reference

[← README](../../Readme.md) | [Commands & Macros →](klipper-functions.md)

---

## Config File Overview

Three files, included in this order from `printer.cfg`:

```ini
[include nfc/nfc_reader.cfg]    ; base settings — edit this
[include nfc/nfc_macros.cfg]    ; Happy Hare event macros — edit only if needed
[include nfc/nfc_reader_hw.cfg] ; one section per physical gate — edit this
```

**How inheritance works:** `nfc_reader.cfg` defines the base `[nfc_gate]` section with all defaults. Each `[nfc_gate laneN]` in `nfc_reader_hw.cfg` inherits every key automatically. Override a key in a lane section only when that lane needs a different value.

This includes hardware keys. `i2c_address` and `i2c_bus` set in the base `[nfc_gate]` section are inherited by all lanes — you only need to specify them per lane if a particular reader uses different hardware.

---

## `nfc_reader.cfg` — Base Settings

### Spoolman

```ini
[nfc_gate]
spoolman_url:       auto
spoolman_rfid_key:  rfid_tag
spoolman_timeout:   5.0
spoolman_cache_ttl: 300
```

| Setting | Default | Description |
|---|---|---|
| `spoolman_url` | `auto` | `auto` reads the URL from Moonraker's `[spoolman]` config. Set to `http://host:port` to use a direct URL. Leave empty to disable Spoolman lookup. |
| `spoolman_rfid_key` | `rfid_tag` | Name of the extra field on Spoolman spool records that holds the tag UID. Must match exactly — case-sensitive. |
| `spoolman_timeout` | `5.0` | HTTP request timeout in seconds. Increase if you see timeouts on a slow network. |
| `spoolman_cache_ttl` | `300` | How long (seconds) a UID→spool lookup is cached. `0` disables caching. |

---

### Tag Data Parsing

```ini
[nfc_gate]
tag_parsing:          False
bambu_reads:          False
spoolman_auto_create: False
#tag_max_pages:       16
```

| Setting | Default | Description |
|---|---|---|
| `tag_parsing` | `False` | `False` = UID-only (default — no tag content reads). `True` = read NTAG user pages or MIFARE authenticated blocks and parse filament metadata from the tag payload. |
| `bambu_reads` | `False` | Allow authenticated MIFARE reads for Bambu factory spools when `tag_parsing: True`. Requires `pycryptodome` in the Klipper Python venv. Leave `False` unless pycryptodome is installed. |
| `spoolman_auto_create` | `False` | When `tag_parsing: True` and no existing Spoolman spool matches the tag, automatically create a new vendor/filament/spool record from tag metadata. Only activates when the tag carries at least a material type. |
| `tag_max_pages` | `16` | Fallback NTAG user-page window for non-NDEF/binary tags. NDEF text/JSON tags read the NDEF TLV length dynamically, so large OpenSpool/OpenPrintTag payloads do not need this increased. |

> [!NOTE]
> `bambu_reads: True` with `tag_parsing: False` logs a warning at startup and has no effect — the MIFARE path is never reached when tag parsing is disabled.

---

### Polling

```ini
[nfc_gate]
startup_polling:    1
startup_poll_delay: 0.0
poll_interval:      10
absent_threshold:   3
```

| Setting | Default | Description |
|---|---|---|
| `startup_polling` | `-1` | `-1` = manual start only. `1` = start polling automatically after PN532 init. `0` = explicitly disabled (useful as a lane override). |
| `startup_poll_delay` | `0.0` | Seconds to wait before the first automatic poll. The shipped hardware config staggers this by 0.5 seconds per lane. |
| `poll_interval` | `10` | Seconds between polls while background polling is active. |
| `absent_threshold` | `3` | Consecutive missed reads before `_NFC_SPOOL_REMOVED` fires. At 10s interval, default = ~30s before removal. |

> [!TIP]
> For bench testing, use `poll_interval: 5` and `absent_threshold: 1` so state changes fire quickly. Restore production values before a real print run.

**Effective removal time:**
```
poll_interval × absent_threshold = seconds before removal fires
10 × 3 = 30 seconds  (default)
```

---

### PN532 I2C Hardware

```ini
[nfc_gate]
i2c_address: 36
i2c_bus:     i2c3_PB3_PB4
startup_poll_delay: 0.5
```

These keys in the base `[nfc_gate]` section are inherited by every `[nfc_gate laneN]`. Set them once here; lane sections only need to specify them if a particular reader differs from the rest.

| Setting | Default | Description |
|---|---|---|
| `i2c_address` | `36` (`0x24`) | PN532 I2C address as a decimal integer. The PN532 I2C address is fixed at `0x24` (36) by the chip — leave this at the default. |
| `i2c_bus` | _(none)_ | Hardware I2C bus identifier on the lane MCU. Must be set in the base section or overridden per lane. |

**Common bus names:**

| Board / wiring | `i2c_bus` value |
|---|---|
| EBB42 v1.x (PB3/PB4) | `i2c3_PB3_PB4` |
| SLB (PB10/PB11) | `i2c2_PB10_PB11` |

> [!NOTE]
> The PN532 I2C address is hardwired to `0x24` (decimal `36`). The two pads/jumpers on the breakout board (SEL0/SEL1, sometimes labeled A0/A1) select the **communication protocol** (I2C, SPI, or HSU), not the address. For I2C: SEL0=1, SEL1=0. See the [wiring guide](../i2c-pn532/wiring.md) for the mode selection table.

---

### PN532 Timing

```ini
[nfc_gate]
transceive_delay: 0.250
crc_delay:        0.050
```

These are tuned for CAN bus round-trip latency on the EBB42. Leave them at defaults unless you're debugging timing-related failures.

| Setting | Default | Description |
|---|---|---|
| `transceive_delay` | `0.250` | Seconds to wait after `InListPassiveTarget` before reading the response. The PN532 scans for tags during this window. Increase if you see spurious `i2c_read_response` timeouts. |
| `crc_delay` | `0.050` | Seconds after `InRelease` before the next command. |

---

### Logging

```ini
[nfc_gate]
log_file:          nfc_reader.log
debug:             2
console_output:    False
console_log_level: warning
```

| Setting | Default | Description |
|---|---|---|
| `log_file` | `nfc_reader.log` | Log filename. Relative paths resolve to `~/printer_data/logs/`. Set to an absolute path to write elsewhere. Leave empty to use the main Klipper log only. |
| `debug` | `2` | `0` (or `off`) = no logging. `1` (or `error`) = errors only. `2` (or `warning`) = warnings and errors. `3` (or `info`) = state changes, Spoolman lookups, HH handoff. `4` (or `debug`) = full I2C protocol trace. |
| `console_output` | `False` | Send NFC log messages to the Fluidd/Mainsail console. Errors always appear in the console regardless of this setting. |
| `console_log_level` | `warning` | Minimum level to show in console when `console_output: True`. Accepts string (`error`, `warning`, `info`, `debug`) or numeric (`1`–`4`). |

**Recommended for normal printing:**
```ini
console_output:    False
console_log_level: warning
```

**Recommended during setup or debugging:**
```ini
console_output:    True
console_log_level: info
debug:             3
```

---

### Expert Debug Flag

```ini
[nfc_gate]
low_level_debug: False
```

When `True`, exposes manual PN532 I2C bus commands (`STEP`, `RAW_READ`, `RAW_WRITE`, etc.) on the `NFC` command for step-by-step bring-up debugging.

> [!WARNING]
> These commands bypass the normal state machine. Set back to `False` before printing.

See [Commands & Macros](klipper-functions.md#expert-low-level-debug-commands) and [Expert: Low-Level I2C Debugging](expert-low-level-i2c-debugging.md).

---

## `nfc_reader_hw.cfg` — Lane Hardware

One `[nfc_gate laneN]` section per physical gate. Most lanes only need two lines:

```ini
[nfc_gate lane0]
mmu_gate:  0
i2c_mcu:   lane0
```

`i2c_address` and `i2c_bus` are inherited from the base `[nfc_gate]` section in `nfc_reader.cfg` and do not need to be repeated here unless a specific lane uses different hardware.

| Key | Required | Description |
|---|:---:|---|
| `mmu_gate` | Yes | Happy Hare gate number (0-based integer). Gate 0 = first MMU gate. |
| `i2c_mcu` | Yes | Klipper MCU name. Must exactly match an `[mcu laneN]` section in your config. |
| `i2c_bus` | — | Override the base `[nfc_gate]` bus for this lane only. Omit when all readers share the same bus pin. |
| `i2c_address` | — | Override the base address for this lane only. Omit when all readers are at the default `0x24`. |

Any other `nfc_reader.cfg` key can also be overridden per lane. Example — delayed startup and extra logging on one lane only:

```ini
[nfc_gate lane2]
mmu_gate:           2
i2c_mcu:            lane2
startup_poll_delay: 1.0
debug:              3
```

---

## `nfc_macros.cfg` — Event Macros

These macros are called by NFC_Manager when gate state changes. Edit them to adjust Happy Hare command calls for your Happy Hare version. Do not put Happy Hare commands anywhere else.

### `_NFC_SPOOL_CHANGED`

Called when a tag resolves to a spool (either via Spoolman or from tag metadata directly).

Two dispatch paths depending on tag type and Spoolman availability:

**Spoolman path** — tag UID matched a Spoolman record:
```
GATE  SPOOL_ID  UID  [AUTO_CREATED=1]
```

**Metadata path** — tag carries embedded filament data, Spoolman disabled or no match:
```
GATE  UID  [MATERIAL=...]  [COLOR=...]  [TEMP=...]
```

Default:
```gcode
{% if params.SPOOL_ID is defined %}
    {% if auto_created %}
    MMU_SPOOLMAN REFRESH=1 QUIET=1
    {% endif %}
    MMU_GATE_MAP GATE={gate} SPOOLID={spool_id} AVAILABLE=1 SYNC=1 QUIET=1
{% else %}
    MMU_GATE_MAP GATE={gate} [MATERIAL=..] [COLOR=..] [TEMP=..] AVAILABLE=1 QUIET=1
{% endif %}
MMU_GATE_MAP GATE={gate} APPLY=1
```

`AUTO_CREATED=1` is set when the spool record was just created by `spoolman_auto_create`. The macro runs `MMU_SPOOLMAN REFRESH=1 QUIET=1` first so Happy Hare's Spoolman cache includes the new spool before the gate assignment is sent.

### `_NFC_SPOOL_REMOVED`

Called after `absent_threshold` consecutive missed polls. Parameter: `GATE`.

Default:
```gcode
MMU_GATE_MAP GATE={gate} SPOOLID=-1 AVAILABLE=0 SYNC=1 QUIET=1
MMU_GATE_MAP GATE={gate} APPLY=1
```

### `_NFC_TAG_NO_SPOOL`

Called when a tag is detected but no matching spool is found in Spoolman. Parameters: `GATE`, `UID`.

Default: prints the unknown UID to the console with instructions to register it.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
