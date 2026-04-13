# Configuration Reference

[← Back to README](../../Readme.md)

---

## Config File Overview

NFC Gate Reader uses three config files, included in this order from `printer.cfg`:

```ini
[include NFC/nfc_vars.cfg]
[include NFC/nfc_macros.cfg]
[include NFC/pn532_i2C.cfg]
```

| File | Edit? | Purpose |
|---|:---:|---|
| `nfc_vars.cfg` | **Yes** | Base settings: Spoolman URL, polling timing, logging, debug flags. All lane sections inherit from this. |
| `nfc_macros.cfg` | Sometimes | Happy Hare handoff macros. Edit only to adjust Happy Hare command calls for your version. |
| `pn532_i2C.cfg` | **Yes** | One `[nfc_gate laneN]` section per physical gate, mapping the gate to its MCU and I2C bus. |

### How inheritance works

`nfc_vars.cfg` defines a base `[nfc_gate]` section with all defaults. Each `[nfc_gate laneN]` section in `pn532_i2C.cfg` automatically inherits every key from the base section. You only need to override a key inside a lane section if that lane needs a different value.

**Example:** Enable verbose logging on lane 2 only, while the other lanes stay at `debug: 1`:

```ini
[nfc_gate lane2]
mmu_gate:   2
i2c_mcu:    lane2
i2c_bus:    i2c3_PB3_PB4
debug:      2
```

---

## `nfc_vars.cfg` — Base Settings

### PN532 I2C Address

```ini
i2c_address: 36
```

The I2C address of the PN532 module, in decimal. The PN532 factory default is `0x24`, which is decimal `36`.

| Value | Hex | Notes |
|---|---|---|
| `36` | `0x24` | Default — shipped state for most PN532 modules |
| `37` | `0x25` | Only if A1=0, A0=1 address pads are set |
| `38` | `0x26` | Only if A1=1, A0=0 address pads are set |
| `39` | `0x27` | Only if A1=1, A0=1 address pads are set |

> [!NOTE]
> For the per-lane design, every PN532 lives on its own dedicated I2C bus (one per EBB42), so all readers can stay at the default address `36`. You only need to change this if your wiring shares a bus across multiple PN532s.

---

### Spoolman

```ini
spoolman_url:       auto
spoolman_rfid_key:  rfid_tag
spoolman_timeout:   5.0
spoolman_cache_ttl: 300
```

#### `spoolman_url`

| Value | Behaviour |
|---|---|
| `auto` | SpoolmanClient queries Moonraker for the Spoolman URL. Use this when Moonraker has a `[spoolman]` section configured. |
| `http://host:port` | Direct URL. Use for testing or when Moonraker cannot report the Spoolman URL. Example: `http://192.168.1.50:7912` |
| *(empty)* | Spoolman lookup is disabled. UID reads still happen; `_NFC_TAG_NO_SPOOL` fires for every tag. |

#### `spoolman_rfid_key`

The name of the extra field on Spoolman spool records that holds the NFC tag UID.

| Value | Behaviour |
|---|---|
| `rfid_tag` | Default documented field name — must be created in Spoolman Settings → Extra Fields |
| any string | Looks for that exact key in the spool `extra` data |

The value must match the field name in Spoolman exactly (case-sensitive). See [Spoolman Integration](spoolman-integration.md).

#### `spoolman_timeout`

HTTP request timeout in seconds for Spoolman API calls.

| Value | Behaviour |
|---|---|
| `5.0` | Default — covers most local Spoolman instances |
| `0.5` | Minimum accepted value |
| `30.0` | Upper end for remote or slow instances |

Increase this if you see Spoolman lookup timeouts in the log but Spoolman itself is healthy.

#### `spoolman_cache_ttl`

How long (in seconds) a successful UID → spool lookup result is cached.

| Value | Behaviour |
|---|---|
| `300` | Default — cache for 5 minutes |
| `0` | Disable cache entirely. Every poll re-queries Spoolman. |
| `1`–`3600` | Custom cache lifetime in seconds |

The cache prevents repeated Spoolman API calls when the same spool stays on a gate for an extended time.

---

### Polling

```ini
poll_interval:    30
absent_threshold: 3
```

#### `poll_interval`

How often (in seconds) each gate is scanned while reactor timer polling is active.

| Value | Behaviour |
|---|---|
| `30` | Default production value |
| `5` | Faster bench-testing cadence |
| `60` or higher | Lower bus/API traffic, slower spool-change detection |

Accepted range: `1` – `3600` seconds.

#### `absent_threshold`

How many consecutive missed reads must occur before the gate is declared empty and `_NFC_SPOOL_REMOVED` fires.

| Value | Behaviour |
|---|---|
| `3` | Default — a tag must be absent for ~90 s at default poll_interval |
| `1` | Immediate removal on first miss. Useful for bench testing, not production. |
| `5`+  | Tolerant of marginal tag placement or occasional missed reads |

Effective removal time (approximate):

```
poll_interval × absent_threshold  =  removal time in seconds
30 × 3  =  90 seconds
```

> [!TIP]
> For bench testing, use `poll_interval: 5` and `absent_threshold: 1` so removals fire quickly. Restore production values before long print runs.

---

### PN532 Timing

```ini
transceive_delay: 0.250
crc_delay:        0.050
```

These control how long the driver waits around the two PN532 passive-target commands. They are tuned for CAN bus round-trip latency on the EBB42. Leave them at defaults unless you are debugging timing-related failures.

#### `transceive_delay`

Delay (seconds) after issuing `InListPassiveTarget` before reading the response. The PN532 scans for tags during this window — if no tag is present, it times out internally and returns a no-target response.

| Value | Behaviour |
|---|---|
| `0.250` | Default — conservative, covers the PN532's internal no-tag timeout plus CAN latency |
| `0.050` | Minimum accepted value |
| `1.000`–`2.000` | Slow debug values for timing-marginal situations |

#### `crc_delay`

Delay (seconds) after `InRelease` (target deselect) before the next command.

| Value | Behaviour |
|---|---|
| `0.050` | Default |
| `0.005` | Minimum accepted value |
| `1.000` | Maximum accepted value |

---

### Logging

```ini
log_file:          nfc_reader.log
debug:             1
console_output:    False
console_log_level: warning
```

#### `log_file`

NFC-specific log file. Relative names resolve under Klipper's log directory (`~/printer_data/logs/`).

| Value | Behaviour |
|---|---|
| `nfc_reader.log` | Default — creates `~/printer_data/logs/nfc_reader.log` |
| `/absolute/path/to/file.log` | Write to an explicit path |
| *(empty)* | Use normal Klipper logger output without a dedicated file |

#### `debug`

Log verbosity level.

| Value | Behaviour |
|---|---|
| `0` | Warnings and errors only |
| `1` | Default — operational events: startup, reader health, state changes, Spoolman lookup results |
| `2` | Full trace — PN532 frames, I2C transaction details, UID parsing, cache hits/misses. Intentionally noisy. |

Use `debug: 2` only while diagnosing a specific problem.

#### `console_output`

Whether to send log messages to the Fluidd / Mainsail console.

| Value | Behaviour |
|---|---|
| `False` | Default — no NFC messages in the console during normal operation |
| `True` | Messages at or above `console_log_level` appear in the console |

> [!NOTE]
> **Errors are always sent to the console**, regardless of this setting, once the NFC module has loaded.

#### `console_log_level`

Controls which messages appear in the console when `console_output: True`. Higher number = more output.

| Value | Alias | Console shows |
|---|---|---|
| `1` | `error` | Errors only — quietest |
| `2` | `warning` | Warnings and errors — recommended for normal use |
| `3` | `info` | Info, warnings, and errors — real-time debugging |

Recommended for normal printing:
```ini
console_output:    False
console_log_level: 2
```

Recommended during setup or real-time debugging:
```ini
console_output:    True
console_log_level: 3
```

---

### Expert Debug Flag

```ini
low_level_debug: False
```

When `True`, the `NFC_GATE` command exposes a full set of manual PN532 bus commands for step-by-step I2C debugging. These commands bypass the normal state machine.

| Value | Behaviour |
|---|---|
| `False` | Default — raw PN532 bus commands are not available |
| `True` | Enables `HELP`, `STEP`, `RAW_READ`, `RAW_WRITE`, `RAW_CMD`, `READY_READ`, `ACK_READ` on `NFC_GATE` |

> [!WARNING]
> Low-level debug commands can disturb the PN532 state machine. Sending the wrong sequence can leave the PN532 in a state where normal polling fails until it is restarted. Use only during manual bring-up. Set back to `False` before printing.

See [Expert: Low-Level I2C Debugging](expert-low-level-i2c-debugging.md).

---

## `pn532_i2C.cfg` — Lane Hardware

### `[nfc_gate laneN]` sections

Each physical gate needs one section.

```ini
[nfc_gate lane0]
mmu_gate:   0
i2c_mcu:    lane0
i2c_bus:    i2c3_PB3_PB4
```

| Key | Required | Value |
|---|:---:|---|
| `mmu_gate` | Yes | Happy Hare gate number (integer). Gate 0 maps to the first MMU gate. |
| `i2c_mcu` | Yes | Klipper MCU name that owns this I2C bus. Must exactly match an `[mcu laneN]` section. |
| `i2c_bus` | Yes | Hardware I2C bus identifier on that MCU. For PB3/PB4 on EBB42 use `i2c3_PB3_PB4`. |

Any key from the base `[nfc_gate]` section can be overridden per lane. Overrides apply only to that lane.

---

## `nfc_macros.cfg` — Happy Hare Handoff

These macros are called by NFC_Manager when gate state changes. You should rarely need to edit them, but they are the correct place to adjust Happy Hare command calls for your version.

### `_NFC_SPOOL_CHANGED`

Called when a new UID is resolved to a Spoolman spool.

**Parameters:** `GATE` (int), `SPOOL_ID` (int), `UID` (string)

Default body:
```gcode
MMU_GATE_MAP GATE={gate} SPOOLMAN_ID={spool_id}
```

### `_NFC_SPOOL_REMOVED`

Called after `absent_threshold` consecutive missed polls.

**Parameters:** `GATE` (int)

Default body:
```gcode
MMU_GATE_MAP GATE={gate} SPOOLMAN_ID=-1
```

### `_NFC_TAG_NO_SPOOL`

Called when a tag UID is detected but no matching Spoolman spool is found.

**Parameters:** `GATE` (int), `UID` (string)

Default body reports the unknown UID to the console so you know which UID to register in Spoolman.

> [!IMPORTANT]
> Do not put `MMU_GATE_MAP`, `MMU_SPOOLMAN`, or any other Happy Hare commands inside `PN532Driver` or `SpoolmanClient`. All Happy Hare calls must live in these macros or in `NFC_Manager` as an explicit orchestration decision. This keeps Happy Hare-facing behaviour visible and editable in config.

See [Klipper Commands & Macros](klipper-functions.md) for full documentation of all commands.
