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

For a shared-reader-only install, include `nfc_reader_shared.cfg` instead of
`nfc_reader_hw.cfg`. For a hybrid install, include both hardware files after
`nfc_macros.cfg`.

**How inheritance works:** `nfc_reader.cfg` defines the base `[nfc_gate]` section with all defaults. Each `[nfc_gate laneN]` in `nfc_reader_hw.cfg` inherits every key automatically. Override a key in a lane section only when that lane needs a different value.

This includes hardware keys. `i2c_address` and `i2c_bus` set in the base `[nfc_gate]` section are inherited by all lanes — you only need to specify them per lane if a particular reader uses different hardware.

`reader_type` is inherited the same way as other hardware keys. The shipped
default is `pn532`; set `reader_type` to `pn7160`, `rc522`, or `pn5180` when
that physical reader is installed. RC522 and PN5180 use SPI rather than I2C.

Example:

```ini
[nfc_gate]
reader_type:      pn532
i2c_address:      36
i2c_bus:          i2c3_PB3_PB4
scan_enabled:     False

[nfc_gate lane0]
enabled:          True
mmu_gate:         0
i2c_mcu:          lane0
# inherits i2c_address, i2c_bus, scan_enabled=False

[nfc_gate lane1]
mmu_gate:         1
i2c_mcu:          lane1
scan_enabled:     True
# overrides only scan_enabled for lane1
```

---

## `nfc_reader.cfg` — Base Settings

### Reader Hardware

```ini
[nfc_gate]
reader_type: pn532
i2c_address: 36
i2c_bus:     i2c3_PB3_PB4
i2c_speed:   100000
```

| Setting | Default | Description |
|---|---|---|
| `reader_type` | `pn532` | Reader driver to use. Supported values are `pn532`, `pn7160`, `rc522`, and `pn5180`. |
| `i2c_address` | `36` for PN532 | I2C address. PN532 uses fixed decimal `36` (`0x24`). PN7160 must use decimal `40-43` (`0x28-0x2B`). Not used by RC522 or PN5180. |
| `i2c_bus` | board-specific | I2C bus name on the selected MCU. PN532 should use hardware I2C. PN7160 supports software I2C, but hardware I2C is recommended because software I2C increases MCU load. Not used by RC522 or PN5180. |
| `i2c_speed` | `100000` | I2C speed in Hz. Keep `100000` for PN7160 and for conservative PN532 bring-up. |
| `i2c_mcu` | per section | Klipper MCU name that hosts the reader. Required in `[nfc_gate laneN]` and `[nfc_gate shared]`. |
| `cs_pin` | unset | Required for RC522 and PN5180 SPI readers. Use the Klipper pin connected to RC522 SDA/SS/CS or PN5180 NSS/CS. |
| `spi_bus` / software SPI pins | unset | Required for RC522 and PN5180. Use Klipper's normal SPI config keys for the selected MCU. |
| `spi_speed` | `500000` | Optional SPI clock in Hz for RC522 and PN5180. Use `500000` with hardware SPI; set `100000` for software SPI. |
| `rc522_transceive_delay` | `0.035` | Optional RC522 UID-read response wait in seconds. Leave at the default unless hardware testing shows the reader needs more time. |
| `reset_pin` | unset | Required for PN5180. Connect PN5180 RST to an MCU output so the driver can recover a communication lockup. |
| `busy_pin` | `mmu:PB0` | Required PN5180 active-high BUSY signal. The configuration key has this default, but the BUSY wire must be connected. |

Reader settings inherit from the base `[nfc_gate]` section. A lane with no
`reader_type` uses the base reader type. A lane with no `i2c_address` uses the
base address when its reader type matches the base reader type; otherwise it
uses that reader's default address.

PN7160 lane example:

```ini
[nfc_gate lane1]
enabled:     True
reader_type: pn7160
i2c_address: 40
mmu_gate:    1
i2c_mcu:     mmu1
```

PN7160 address rule: if multiple PN7160 readers share the same MCU/I2C bus,
give each reader a unique `i2c_address`. If each lane has its own MCU or its
own I2C bus, the same PN7160 address can be reused.

PN7160 optional hardware pins:

```ini
# ven_pin: PA8
# irq_pin: ^PC6
```

`ven_pin` allows a hardware reset / hard power-down. It is optional, but strongly
recommended for PN7160. Without VEN, abnormal Klipper termination can leave the
chip in a state that software cannot fully reset. `irq_pin` is also optional;
when omitted, the PN7160 driver uses timing-based polling.

RC522 lane example:

```ini
[nfc_gate lane1]
enabled:     True
reader_type: rc522
mmu_gate:    1
i2c_mcu:     mmu1
cs_pin:      mmu1:PA4
spi_bus:     spi1
# spi_speed: 500000  # Hardware SPI default; use 100000 for software SPI
# rc522_transceive_delay: 0.035
```

RC522 can resolve tags registered in Spoolman's RFID extra field and can read
NTAG/Type-2 rich metadata through the normal NFC Reader pipeline. It also
supports authenticated MIFARE Classic reads (Bambu, QIDI Box, Creality CFS)
when `tag_parsing: True` and `bambu_reads: True` are set; Bambu's own reads
and Creality's UID-derived Key B both require `pycryptodome`, QIDI's
default-key fallback does not (see [Tag Data Parsing](#tag-data-parsing)).
It does not support ISO15693 rich tag metadata.

PN5180 lane example (SLB wiring):

```ini
[nfc_gate lane0]
enabled:     True
reader_type: pn5180
mmu_gate:    0
i2c_mcu:     mmu
spi_bus:     spi2_PB14_PB15_PB13
cs_pin:      mmu:PA8
reset_pin:   mmu:PC6
busy_pin:    mmu:PB0
spi_speed:   500000
```

PN5180 supports ISO14443A/NTAG, authenticated MIFARE/Bambu, and SLIX2
(ISO15693/Type-5) reads. It requires nine physical connections on the documented
SLB setup, including both 5V and PSF 3.3V power rails, BUSY, and RST. See
[PN5180 wiring](../i2c-nfc/pn5180-wiring.md) before wiring or configuring it.

---

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
| `spoolman_url` | `auto` | `auto` reads the URL from Moonraker's `[spoolman]` config. Set to `http://host:port` to use a direct URL. Set to `disabled` or leave empty to skip Spoolman lookup. |
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
| `bambu_reads` | `False` | Allow authenticated MIFARE Classic reads when `tag_parsing: True`. Gates three attempts, tried in order, each only firing when the previous one didn't authenticate any sector: Bambu's own per-tag HKDF-derived Key A (sectors 0-4, requires `pycryptodome`), the plain MIFARE Classic factory default Key A (sectors 0-4, QIDI Box), then Creality's UID-derived Key B (sector 1 only, requires `pycryptodome`). Despite the name, this flag also covers the QIDI/Creality fallbacks. |
| `spoolman_auto_create` | `False` | When `tag_parsing: True` and no existing Spoolman spool matches the tag, automatically create a new vendor/filament/spool record from tag metadata. Only activates when the tag carries at least a material type. |
| `tag_max_pages` | `16` | Fallback NTAG user-page window for non-NDEF/binary tags. NDEF text/JSON tags read the NDEF TLV length dynamically, so large OpenSpool/OpenPrintTag payloads do not need this increased. |

> [!NOTE]
> `bambu_reads: True` with `tag_parsing: False`, or `spoolman_auto_create: True` without tag parsing and a usable Spoolman URL, logs a startup warning and has no effect. Run `NFC_DOCTOR` after restart to see these warnings again.

> [!NOTE]
> The default-key fallback authenticates with the plain MIFARE Classic
> factory default Key A, `FF FF FF FF FF FF` — not a secret, not derived,
> the same key every unprotected MIFARE Classic card ships with. It is
> **confirmed** correct for QIDI Box tags (sourced from the community
> `BoxRFID-Touch` project, which authenticates block 4 with this exact key
> before reading/writing material/color/manufacturer codes).
> If QIDI tags fail to rich-read, see the
> [QIDI Box RFID reference](qidi-rfid-reference.md): QIDI's own tag guide
> places the payload in sector 1/block 0 (absolute block 4) and notes a
> QIDI-specific sector 1 Key A in addition to the factory fallback key.
>
> Creality CFS/K1/K2 does not use the default key at all: its sector 1 is
> protected by a Key B derived per-tag from the UID
> (`AES-128-ECB(AES_KEY_GEN, uid×4)[:6]`), and the stored payload is itself
> AES-128-ECB-encrypted with a second static key. Both keys' hex/ASCII
> values are published in the [Creality key material
> table](spoolman-integration.md#supported-rich-manufacturer-tags);
> community-sourced from a Creality RFID encryption helper script mirroring
> the JavaScript implementation used by Creality's own tag-writer tooling.
> When the default-key attempt authenticates nothing, `bambu_reads: True`
> also tries this Creality Key B against sector 1 only — if that also fails
> (or `pycryptodome` isn't installed), the read falls through to UID-only.
> Creality rich reads have been tested against real Creality spool tags. The
> parser builds a same-spool `spool_identity` from the decoded payload fields,
> not from the hardware UID, so two tags on the same spool resolve to the same
> identity for left-neighbor interference handling.

---

### Polling

```ini
[nfc_gate]
startup_polling:    -1
startup_poll_delay: 0.0
poll_interval:      10
absent_threshold:   3
```

| Setting | Default | Description |
|---|---|---|
| `startup_polling` | `-1` | `-1` = manual start only, the default for hook-driven lane scans. `1` = start optional background lane polling automatically after reader init. `0` = explicitly disabled. The shared-reader section overrides this with `1`. |
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

### Scan-and-Jog

```ini
[nfc_gate]
scan_enabled:          False
scan_jog_mm:           150.0
#scan_jog_max:         480.0
scan_reads_per_position: 1
scan_rewind_buffer_mm: 30.0
scan_decode_retry_mm:     5.0
scan_decode_retry_rounds: 5
scan_poll_interval:    0.25
scan_motion_mode: continuous
scan_continuous_step_mm: 150.0
scan_continuous_speed: 200.0
scan_continuous_accel: 2000.0
scan_continuous_poll_interval: 0.05
```

| Setting | Default | Description |
|---|---|---|
| `scan_enabled` | `False` | Controls the automatic Happy Hare gate-status edge trigger. `False` disables automatic 0→1 scan-jog, but manual `NFC JOG_SCAN=1` or Happy Hare hook-triggered `_NFC_SCAN_JOG_PRELOAD` still works. |
| `scan_jog_mm` | `150.0` | Logical filament advance per scan chunk (mm). NFC divides this into three blocking MMU_TEST_MOVE substeps so it can read at stopped spool positions. For rich tags such as Bambu/MIFARE, a smaller value like `75.0` can improve payload-read reliability. |
| `scan_jog_max` | unset | Optional maximum scan-jog travel distance. When set, NFC uses this value and does not read Happy Hare Bowden calibration. `480.0` is roughly one full spool rotation. Leave unset/commented to keep scanning until the active lane's Bowden calibration length is reached. |
| `scan_reads_per_position` | `1` | Number of NFC read attempts at each stopped spool position before moving the next substep. Reads are spaced by `scan_poll_interval`. Increase for marginal tag alignment at the cost of scan time. |
| `scan_rewind_buffer_mm` | `30.0` | Distance reserved for Happy Hare's final gate-parking step (`_MMU_STEP_UNLOAD_GATE`). After a tag is found, NFC fast-rewinds to within this buffer and then hands off to HH for sensor/encoder-based final parking. If the scan moved less than this value, the fast rewind is skipped. |
| `scan_decode_retry_mm` | `5.0` | Distance between nearby retry positions after a UID is found but the rich tag payload is marked incomplete. |
| `scan_decode_retry_rounds` | `5` | Nearby retry rounds before accepting the current UID/metadata result. Each round probes both sides of the first UID hit. |
| `scan_poll_interval` | `0.25` | Seconds between stopped-position NFC read attempts during scan-jog. The shared reader also uses this value as its active polling cadence. Since Happy Hare `MMU_TEST_MOVE` blocks by default, this is not a read-while-moving interval. |
| `scan_motion_mode` | `continuous` | `continuous` (default) uses the lane NFC virtual endstop for the full remaining forward search and records UID hits while the homing move is active. `stopped` uses blocking MMU_TEST_MOVE substeps with reads at each stopped spool position — use this for marginal reader or tag alignment. |
| `scan_continuous_step_mm` | `150.0` | Continuous-mode forward search chunk size. Rich tag reads use the observed UID hit-window center, so this value no longer needs to be kept small just to avoid overshooting the ideal metadata-read position. |
| `scan_continuous_speed` | `200.0` | Continuous-mode gear move speed in mm/s. |
| `scan_continuous_accel` | `2000.0` | Continuous-mode gear move acceleration in mm/s^2. At `150mm`, `200mm/s`, `2000mm/s^2`, each move takes about `0.85s` before NFC read time is included. |
| `scan_continuous_poll_interval` | `0.05` | NFC read cadence while a continuous chunk is estimated to be in flight. When the chunk completes with no tag, NFC queues the next chunk. |

There is no user setting for left-neighbor interference. During scan-jog, gate
`N` first trusts a Spoolman UID hit when it resolves to a different spool than
gate `N - 1`. For rich manufacturer tags that expose a same-spool
`spool_identity` (Bambu `tray_uid`, TigerTag Twin Tag ID, Creality decoded
payload hash), NFC also compares that identity against the cached identity on
gate `N - 1`. If it matches, NFC moves the left neighbor 75 mm out of range,
clears the false read, restarts a full scan on the current lane, and restores
the neighbor on scan exit.

Continuous scan mode preserves the existing tag-found path: tag actions are
cached until after rewind, the 0.1 second read-light hold still plays before the
rewind effect, and `_scan_mm_total` still drives the final rewind distance.
If a continuous scan sees a UID during motion, NFC waits for the chunk to
finish, checks whether Spoolman already knows the UID, and stops the scan if it
does not need a richer identity check. If the UID does not resolve through
Spoolman, or if the resolved spool matches the left neighbor and rich tag
parsing is needed to confirm interference, NFC backs up to the observed UID
hit-window center before rich parsing and the normal `scan_decode_retry_mm`
retry sweep.

When a virtual NFC homing move consumes the full configured scan distance
without caching a UID, NFC rewinds directly. It does not start one extra
endpoint PN532 probe, because the final virtual-endstop discovery may still be
busy. A fixed internal `0.1mm` end tolerance also prevents floating-point
residue from generating a `MOVE=0.00` homing command.

**Happy Hare post-preload hook (alternative to automatic polling):**

The [igiannakas IG-dev branch](https://github.com/igiannakas/Happy-Hare/tree/IG-dev) of Happy Hare adds `variable_user_post_preload_extension` in `config/base/mmu_macro_vars.cfg`. Set it to trigger NFC scan-jog after each successful `MMU_PRELOAD`:

```ini
[gcode_macro _MMU_SEQUENCE_VARS]
description: Happy Hare sequence macro configuration variables
gcode: # Leave empty
variable_user_post_preload_extension: '_NFC_SCAN_JOG_PRELOAD'
```

Happy Hare V3 appends `GATE=<n>` automatically. V4 invokes the hook without
that parameter, so `_NFC_SCAN_JOG_PRELOAD` falls back to `printer.mmu.gate`.
It then calls `NFC GATE=<n> JOG_SCAN=1 SOURCE=AUTO`; the marker permits V4's
trusted post-preload `action=checking` state while manual scan commands remain
restricted to `action=idle`. NFC starts the configured scan-jog LED effect from
the Python scan timer before motion begins. NFC always clears the Happy Hare
gate cache, explicitly unsets the old Spoolman gate assignment with
`MMU_SPOOLMAN GATE=<n>`, and runs the pre-scan `MMU_SPOOLMAN SYNC=1`; when
launched from this hook, those calls are deferred to the scan timer so the hook
can return first.

Recommended NFC config when using the hook:

```ini
[nfc_gate]
startup_polling: 0
scan_enabled:    False
```

With this setup NFC does not poll gate-status at all — Happy Hare calls NFC only after the relevant gate completes preload. The gate-status 0→1 edge trigger is disabled.

---

### PN532 I2C Timing

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
> The PN532 I2C address is hardwired to `0x24` (decimal `36`). The two pads/jumpers on the breakout board (SEL0/SEL1, sometimes labeled A0/A1) select the **communication protocol** (I2C, SPI, or HSU), not the address. For I2C: SEL0=1, SEL1=0. See the [PN532 wiring guide](../i2c-nfc/pn532-wiring.md) for the mode selection table.

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
console_log_level: 2
```

| Setting | Default | Description |
|---|---|---|
| `log_file` | `nfc_reader.log` | Log filename. Relative paths resolve to `~/printer_data/logs/`. Set to an absolute path to write elsewhere. Leave empty to use the main Klipper log only. |
| `debug` | `2` | `0` (or `off`) = no logging. `1` (or `error`) = errors only. `2` (or `warning`) = warnings and errors. `3` (or `info`) = state changes, Spoolman lookups, HH handoff. `4` (or `debug`) = full I2C protocol trace. |
| `console_output` | `False` | Send NFC log messages to the Fluidd/Mainsail console. Errors always appear in the console regardless of this setting. |
| `console_log_level` | `2` | Minimum level to show in console when `console_output: True`. Accepts string (`error`, `warning`, `info`, `debug`) or numeric (`1`-`4`). |

Shared reader console messages and their matching `nfc_reader.log` entries are
defined in [Message Definitions](message_definition.md).

**Recommended for normal printing:**
```ini
console_output:    False
console_log_level: 2
```

**Recommended during setup or debugging:**
```ini
console_output:    True
console_log_level: 3
debug:             3
```

---

### Expert Debug Flag

```ini
[nfc_gate]
low_level_debug: False
```

When `True`, exposes manual low-level reader commands for step-by-step bring-up
debugging. PN532 readers expose I2C `STEP`, `RAW_READ`, `RAW_WRITE`, and ACK
helpers. RC522 readers expose SPI register, antenna, FIFO transceive, and REQA
tag-wake helpers on `NFC` / `NFC_SHARED`.

> [!WARNING]
> These commands bypass the normal state machine. Set back to `False` before printing.

See [Commands & Macros](klipper-functions.md#expert-low-level-debug-commands) and [Expert: Low-Level I2C Debugging](expert-low-level-i2c-debugging.md).

---

## `nfc_reader_hw.cfg` — Lane Hardware

One `[nfc_gate laneN]` section per physical gate. Most lanes only need two lines:

```ini
[nfc_gate lane0]
enabled:   True
mmu_gate:  0
i2c_mcu:   lane0
```

`i2c_address` and `i2c_bus` are inherited from the base `[nfc_gate]` section in `nfc_reader.cfg` and do not need to be repeated here unless a specific lane uses different hardware.

| Key | Required | Description |
|---|:---:|---|
| `enabled` | — | `True` by default. Set `False` to leave a lane template in place without creating I2C hardware, registering `NFC GATE=<n>`, or running reader init. Disabled lanes still appear in `NFC_STATUS` and `NFC_DOCTOR`. |
| `mmu_gate` | Yes | Happy Hare gate number (0-based integer). Gate 0 = first MMU gate. |
| `i2c_mcu` | Yes | Klipper MCU name. Must exactly match an `[mcu laneN]` section in your config. |
| `i2c_bus` | — | Override the base `[nfc_gate]` bus for this lane only. Omit when all readers share the same bus pin. |
| `i2c_address` | — | Override the base address for this lane only. Omit when all readers are at the default `0x24`. |

Any other `nfc_reader.cfg` key can also be overridden per lane. Example — delayed startup and extra logging on one lane only:

```ini
[nfc_gate lane2]
enabled:           True
mmu_gate:           2
i2c_mcu:            lane2
startup_poll_delay: 1.0
debug:              3
```

To keep a future lane in the file without requiring its MCU or reader to exist:

```ini
[nfc_gate lane4]
enabled: False
mmu_gate: 4
i2c_mcu:  mmu4
```

### `[mmu_nfc_endstop laneN]`

`install.sh` writes one of these alongside every enabled `[nfc_gate laneN]`
section. It registers that lane's reader with Happy Hare as a gear-rail
homing endstop (`ENDSTOP=nfc_lane<N>`) — no new hardware, it borrows the same
reader the matching `[nfc_gate laneN]` section owns. This is what lets
scan-jog's forward search stop the instant the tag is detected instead of
jogging a fixed distance and polling afterward. See
[Virtual Endstop](klipper-functions.md#virtual-endstop) for how scan-jog uses it.

V3 registers the endstop on Happy Hare's shared gear rail. V4 registers it on
the drive selected by the reader's global `mmu_gate`. The section name, reader
name, and `endstop_name` may therefore use any naming convention, including
multi-unit layouts; `nfc_gate` and `mmu_gate` define the actual association.

```ini
[mmu_nfc_endstop lane0]
nfc_gate:                lane0
endstop_name:            nfc_lane0
poll_interval:           0.05
register_sensor:         True
```

| Key | Required | Description |
|---|:---:|---|
| `nfc_gate` | Yes | Name of the matching `[nfc_gate laneN]` section this endstop borrows a reader from. |
| `endstop_name` | — | Endstop name Happy Hare homing moves reference as `ENDSTOP=<name>`. Defaults to the section name (`lane0`, `lane1`, ...); the installer writes `nfc_lane0`, `nfc_lane1`, etc. explicitly. |
| `poll_interval` | — | Seconds between NFC reads while a homing move against this endstop is in progress. Default `0.05`. |
| `register_sensor` | — | `True` by default. Also registers this endstop as a `filament_switch_sensor`-style presence sensor so Happy Hare's runout tooling can see it. |

`[mmu_nfc_endstop laneN]` has no `enabled` flag of its own, and it requires
its matching `[nfc_gate laneN]` to be enabled with a working reader — Klipper
raises a config error at startup (`mmu_nfc_endstop <name> references disabled
[nfc_gate <name>]`) if the lane is disabled while its endstop section is
still present. Comment out (or remove) `[mmu_nfc_endstop laneN]` too when
setting `enabled: False` on a lane.

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
GATE  UID  [NAME=...]  [MATERIAL=...]  [COLOR=...]  [TEMP=...]
```

Default:
```gcode
{% if params.SPOOL_ID is defined %}
    {% if auto_created %}
    MMU_SPOOLMAN REFRESH=1 QUIET=1
    {% endif %}
    MMU_GATE_MAP GATE={gate} SPOOLID={spool_id} AVAILABLE=1 SYNC=1 QUIET=1
{% else %}
    MMU_GATE_MAP GATE={gate} [NAME=..] [MATERIAL=..] [COLOR=..] [TEMP=..] AVAILABLE=1 QUIET=1
{% endif %}
MMU_GATE_MAP GATE={gate} APPLY=1 QUIET=1
```

`AUTO_CREATED=1` is set when the spool record was just created by `spoolman_auto_create`. The macro runs `MMU_SPOOLMAN REFRESH=1 QUIET=1` first so Happy Hare's Spoolman cache includes the new spool before the gate assignment is sent.

### `_NFC_SPOOL_REMOVED`

Called after `absent_threshold` consecutive missed polls. Parameter: `GATE`.

Default:
```gcode
MMU_GATE_MAP GATE={gate} SPOOLID=-1 AVAILABLE=0 SYNC=1 QUIET=1
MMU_GATE_MAP GATE={gate} APPLY=1 QUIET=1
```

### `_NFC_TAG_NO_SPOOL`

Called when a tag is detected but cannot be resolved to a spool. Parameters:
`GATE`, `UID`, and optional `SPOOLMAN_DISABLED`.

Default: with Spoolman enabled, prints the unknown UID with instructions to
register it. With Spoolman disabled, prints a warning that the tag was read but
no rich metadata or spool assignment was available.

---

---

## Shared Reader

The shared reader is an optional single NFC reader mounted inside the MMU body — not tied to any EMU lane. It defaults to PN532 hardware and also supports PN7160, RC522, and PN5180. Tap a tagged spool on it before loading; when Happy Hare starts the pregate preload NFC stages the spool ID automatically.

**No per-lane readers are required.** A shared-only installation needs only the base `[nfc_gate]` section (for Spoolman config) and the `[nfc_gate shared]` section. No `[nfc_gate lane0]` or similar sections are needed.

The shared reader lives in its own file — `nfc_reader_shared.cfg` — so it can be added to any install without editing the lane hardware config. For a **pure shared install**, include it instead of `nfc_reader_hw.cfg`. For a **hybrid install** (per-lane readers plus a shared reader), include both.

Run `install.sh` to generate `nfc_reader_shared.cfg` with your hardware values, or copy the template from `config/nfc_reader_shared.cfg` in the repo and edit the I2C or SPI settings required by the selected `reader_type`.

### Config

The `[nfc_gate shared]` section lives in `nfc_reader_shared.cfg`:

```ini
[nfc_gate shared]
enabled:                True
i2c_mcu:                mmu
shared:                 true
startup_polling:        1
```

Full config with all optional keys shown:

```ini
[nfc_gate shared]
enabled:                True
i2c_mcu:                mmu
shared:                 true
startup_polling:        1
shared_read_timeout:    120.0
shared_tag_read_effect: mmu_RFID_read
read_effect_duration:   2.0
shared_bypass_tag_read_effect: mmu_RFID_bypass_read
bypass_read_effect_duration:   2.0
shared_spool_ready_effect: mmu_RFID_ready
shared_bypass_spool_ready_effect: mmu_RFID_bypass_ready
bypass_ready_effect_duration: 2.0
shared_tag_unresolved_effect: mmu_RFID_unresolved
unresolved_effect_duration: 2.0
shared_spool_warning_effect: mmu_RFID_warning
shared_auto_create_effect:   mmu_RFID_creating
shared_missed_limit:    3
force_spool_id:         true
```

| Setting | Default | Description |
|---|---|---|
| `enabled` | `True` | Set `False` to keep the shared-reader template installed without initializing hardware or registering `NFC_SHARED`. |
| `shared` | `false` | Enable shared dispatch for this reader. Must be `true`. |
| `startup_polling` | `1` in the shipped template | Set to `1` to poll at Klipper boot. Set to `0` or `-1` if you want to start it manually with `NFC_SHARED READ=1`. |
| `scan_poll_interval` | inherited from `[nfc_gate]` | Seconds between shared-reader tag reads while polling. The shipped default is `0.25`. |
| `poll_interval` | inherited from `[nfc_gate]` | Ignored for shared-reader read cadence; lane readers still use it for normal background polling. |
| `pending_spool_id_timeout` | Happy Hare `[mmu]` value or `60s` | Seconds a scanned spool remains eligible for the next preload. NFC uses Happy Hare's active `[mmu]` value when it is exposed through Klipper; otherwise it uses 60 s. |
| `shared_read_timeout` | `120.0` | Seconds polling may run without resolving a valid tag before auto-stopping. No effect when started via `startup_polling` or PRELOAD_CHECK auto-restart. |
| `shared_tag_read_effect` | `''` | Name of a `[mmu_led_effect]` to play as soon as the shared reader sees a tag. Leave empty to skip tag-detected LED feedback. |
| `read_effect_duration` | `2.0` | HH duration used by `NFC_SHARED LED_TEST=1`. Normal shared scans do not pass this duration to HH; NFC uses it only as a failsafe release window if no follow-up state replaces the read cue. |
| `shared_bypass_tag_read_effect` | `mmu_RFID_bypass_read` | Name of a `[mmu_led_effect]` to play when a tag is seen while Happy Hare bypass is selected. |
| `bypass_read_effect_duration` | `2.0` | Reserved for standalone bypass-read feedback. Normal bypass reads stay interruptible because bypass-ready feedback is expected to follow. |
| `shared_spool_ready_effect` | `''` | Name of a `[mmu_led_effect]` to play when the tag resolves to a Spoolman spool and is ready to load. Normal staged-spool ready feedback runs until preload commit, cancel, replace, or pending timeout; NFC then releases HH ownership with `MMU_GATE_MAP QUIET=1`. |
| `shared_bypass_spool_ready_effect` | `mmu_RFID_bypass_ready` | Name of a `[mmu_led_effect]` to play when a bypass spool resolves. |
| `bypass_ready_effect_duration` | `2.0` | Seconds before NFC stops `shared_bypass_spool_ready_effect`. |
| `shared_tag_unresolved_effect` | `''` | Name of a `[mmu_led_effect]` to play when the tag UID does not resolve to a spool. Leave empty to skip unresolved LED feedback. |
| `unresolved_effect_duration` | `2.0` | Seconds before NFC stops `shared_tag_unresolved_effect`. For example, `layers: strobe 2 2 ...` plus `unresolved_effect_duration: 1.0` plays two flashes and stops after 1 second. |
| `shared_spool_warning_effect` | `mmu_RFID_warning` | Name of a `[mmu_led_effect]` to play when the staged spool reaches 80% of its pending timeout. NFC does not pass HH `DURATION`; the effect must remain interruptible when preload starts. |
| `shared_auto_create_effect` | `mmu_RFID_creating` | Name of a `[mmu_led_effect]` to play while Spoolman auto-create is running. |
| `shared_missed_limit` | `3` | Consecutive unresolvable UID reads before a console error advises the user to use `MMU_PRELOAD`. Minimum 1. |
| `force_spool_id` | `true` | When `true`, `PRELOAD_CHECK` emits a `[ERROR]` advisory if no spool is staged, telling the user to scan a tag before loading. |

`mmu_gate` and `scan_enabled` are not user-configurable — both are set internally by `shared: true`. Only one enabled shared reader may be configured. The reader inherits `spoolman_url`, `spoolman_rfid_key`, `tag_parsing`, `spoolman_auto_create`, and all logging settings from the base `[nfc_gate]` section. Set `enabled: False` to keep the shared-reader template installed without initializing hardware.

`MMU_SET_LED DURATION=` is intentionally limited to standalone or timeout-bound LED feedback. Happy Hare sets a per-unit pending-update flag while a duration timer is active, and later LED effect calls for that unit are ignored until the timer expires. Normal shared read and staged-ready effects do not pass `DURATION` so follow-up shared-reader states can replace them immediately.

For normal shared reads, `read_effect_duration` still provides a local failsafe:
if the read LED starts and no follow-up state takes ownership, NFC releases the
LEDs back to Happy Hare with `MMU_GATE_MAP QUIET=1`.

**Rich tags** work with the shared reader only when they resolve to a real
Spoolman spool ID. That can happen through an existing UID lookup, an embedded
`spoolman_id`, or `spoolman_auto_create: true`. Metadata-only rich tags are not
enough for shared preload staging because `MMU_GATE_MAP NEXT_SPOOLID` requires
an integer spool ID. See [Shared Reader — Rich tag compatibility](shared-reader.md#rich-tag-compatibility).

### Happy Hare hook wiring

Add one user extension hook to `mmu_macro_vars.cfg`:

```ini
[gcode_macro _MMU_SEQUENCE_VARS]
; stage NEXT_SPOOLID before a pregate-triggered automatic preload
variable_user_post_preload_extension: '_NFC_SHARED_PRELOAD'
```

Use `_NFC_SHARED_PRELOAD` only for a shared-reader-only installation. When
per-lane readers and a shared reader coexist, configure:

```ini
variable_user_post_preload_extension: '_NFC_HYBRID_PRELOAD'
```

The hybrid hook starts the configured lane reader first. If its scan reaches a
final no-tag result, staged shared-reader data is assigned to that same gate.
Gates without a lane reader use the shared-reader transaction directly.

`variable_user_post_preload_extension` fires at the start of every pregate load. `PRELOAD_CHECK` skips only while printing — it is safe to leave wired for all loads. If no spool is staged a console message advises the user; with `force_spool_id: true` that advisory uses the `[ERROR]` prefix.

Shared polling pauses automatically when printing starts and resumes when printing completes via Klipper's `idle_timeout` events — no post-unload hook is needed.

The post-preload hook points to a macro shipped in `nfc_macros.cfg`. Override it in your own cfg to add logic around the NFC check without changing the HH variable.

### LED effect

Define a named `[mmu_led_effect]` in your LED config (same style as `emu_macros.cfg`):

```ini
[mmu_led_effect mmu_RFID_read]
define_on: gates
layers: strobe 1 2 top (1, 1, 0)

[mmu_led_effect mmu_RFID_ready]
define_on: gates
layers: strobe 1 2 top (0, 1, 0)

[mmu_led_effect mmu_RFID_unresolved]
define_on: gates
layers: strobe 2 2 top (1, 0, 0)
```

The effect names must match `shared_tag_read_effect`, `shared_spool_ready_effect`, and `shared_tag_unresolved_effect` in the gate config.

`shared_auto_create_effect: mmu_RFID_creating` runs a bright yellow chase while Spoolman creates a missing spool, then stops before the green ready blink.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
