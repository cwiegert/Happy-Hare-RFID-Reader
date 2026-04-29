# Design: Configuration Architecture

> Engineering reference — not end-user documentation.

---

## File Structure

Three config files, included in this order from `printer.cfg`:

```
[include NFC/nfc_vars.cfg]    → NFCGateDefaults  — base [nfc_gate] section
[include NFC/nfc_macros.cfg]  → GCode macros only (no Python objects)
[include NFC/pn532_i2C.cfg]   → NFCGate × N     — one [nfc_gate laneN] per lane
```

Klipper processes `[include]` directives in order. By the time `pn532_i2C.cfg` is parsed, the `NFCGateDefaults` object for `[nfc_gate]` already exists and is retrievable via `printer.lookup_object('nfc_gate')`.

---

## Klipper Entry Point

The Klipper extras loader maps config section names to filenames in `klippy/extras/`. Section `[nfc_gate]` requires `klippy/extras/nfc_gate.py`. That file is the entry point; all implementation lives in `klippy/extras/nfc_gates/` (a package).

`nfc_gate.py` provides two functions Klipper calls during config load:

```python
def load_config(config):
    # Handles [nfc_gate] — the base defaults section
    global _current_printer
    _current_printer = config.get_printer()
    del _lane_instances[:]          # clear stale entries on Klipper RESTART
    return NFCGateDefaults(config)

def load_config_prefix(config):
    # Handles [nfc_gate lane0], [nfc_gate lane1], etc.
    global _current_printer
    printer = config.get_printer()
    if printer is not _current_printer:
        # No base [nfc_gate] section — first lane triggers the reset
        _current_printer = printer
        del _lane_instances[:]
    defaults = printer.lookup_object('nfc_gate', None)
    gate     = NFCGate(config, defaults)
    # Replace any existing entry for this lane name (guards against double-call on RESTART)
    name = config.get_name()
    for i, existing in enumerate(_lane_instances):
        if existing._name == gate._name:
            _lane_instances[i] = gate
            return gate
    _lane_instances.append(gate)
    return gate
```

`load_config` fires for exactly the bare `[nfc_gate]` section. `load_config_prefix` fires for every `[nfc_gate <anything>]` section. `_lane_instances` is a module-level list shared across all lanes; it powers `NFC_GATE_STATUS`.

`_current_printer` tracks which Printer object owns the current `_lane_instances` contents. A new Printer is created on every Klipper RESTART, so when `printer is not _current_printer`, stale entries from the previous session are cleared.

---

## Inheritance Model

Klipper config sections are independent key-value namespaces. There is no native Klipper inheritance. The three-tier lookup is implemented manually in Python.

**Tier 1 — lane config key**: if the key appears in the `[nfc_gate laneN]` section, that value wins.
**Tier 2 — defaults attribute**: if absent from the lane, the value read by `NFCGateDefaults` from the base `[nfc_gate]` section is used.
**Tier 3 — Python hardcoded fallback**: if `defaults` is None (no base section at all), the hardcoded default in `NFCGate.__init__` is used.

```python
# NFCGate.__init__ reads every parameter with this pattern:
self._poll_interval = config.getfloat(
    'poll_interval',
    d.poll_interval if d else 30.,   # d = defaults object, or None
    minval=1., maxval=3600.
)
```

A bare `[nfc_gate laneN]` section with no base `[nfc_gate]` section is valid — all parameters fall back to hardcoded Python defaults.

---

## Parameter Reference

All parameters are defined in `NFCGateDefaults` (from `[nfc_gate]`) and overridable per `[nfc_gate laneN]`.

### Spoolman

| Parameter | Python fallback | Shipped `nfc_vars.cfg` | Type | Bounds |
|---|---|---|---|---|
| `spoolman_url` | `''` | `auto` | str | — |
| `moonraker_url` | `http://127.0.0.1:7125` | _(not set)_ | str | — |
| `spoolman_rfid_key` | `rfid` | `rfid_tag` | str | — |
| `spoolman_timeout` | `5.0` | `5.0` | float | 0.5–30.0 |
| `spoolman_cache_ttl` | `300.0` | `300` | float | 0–3600 |

`spoolman_rfid_key`: The Python fallback is `'rfid'` but the shipped `nfc_vars.cfg` explicitly sets `rfid_tag`. The field name in Spoolman **must match exactly** — case-sensitive.

`spoolman_url: auto` causes `SpoolmanClient` to query Moonraker's `/server/config` endpoint the first time a tag lookup is needed. The discovered URL is cached after the first successful query.

If `spoolman_url` is left empty, `self._spoolman = None`. Tags are still read; every tag read fires `EVENT_UID_ONLY` → `_NFC_TAG_NO_SPOOL`, which logs the UID. HH is not updated with a spool assignment.

### Polling

| Parameter | Python fallback | Shipped `nfc_vars.cfg` | Type | Bounds |
|---|---|---|---|---|
| `startup_polling` | `-1` | `1` | int | -1, 0, 1 |
| `startup_poll_delay` | `0.0` | `0.0` | float | 0–3600 |
| `poll_interval` | `30.0` | `10` | float | 1–3600 |
| `absent_threshold` | `3` | `3` | int | 1–255 |

`startup_polling = -1`: polling only starts when `NFC_GATE GATE=n READ=1` is issued manually.
`startup_polling = 1`: `_delayed_init` arms the poll timer after PN532 init succeeds, delayed by `startup_poll_delay`.
`startup_poll_delay`: stagger per-lane startup. With 4 lanes and delays of 0, 2, 4, 6 seconds, init and first polls spread across 6 seconds rather than all firing simultaneously.

`absent_threshold` × `poll_interval` = seconds before `EVENT_REMOVED` fires. Default: 3 × 10 = 30 seconds.

### Hardware Timing

| Parameter | Python fallback | Shipped default | Type | Bounds |
|---|---|---|---|---|
| `i2c_address` | `0x24` (36) | `36` | int | 0–127 |
| `transceive_delay` | `0.250` | `0.250` | float | 0.05–2.0 |
| `crc_delay` | `0.050` | `0.050` | float | 0.005–1.0 |

`transceive_delay`: passed as the timeout to `_transceive()` for `InListPassiveTarget`. The PN532 scans the RF field until a tag is found or this timeout expires. 250 ms covers the PN532's internal no-tag timeout safely.

`crc_delay`: used as the minimum timeout for the `InRelease` `_transceive()` call (clamped to ≥ 200 ms inside `_release_current_target`). In practice the PN532 responds to InRelease in a few milliseconds — 50 ms is conservative.

### Logging

| Parameter | Python fallback | Shipped default | Type | Bounds |
|---|---|---|---|---|
| `debug` | `2` | `2` | int | 0–4 |
| `log_file` | `''` | `nfc_reader.log` | str | — |
| `console_output` | `False` | `False` | bool | — |
| `console_log_level` | `warning` | `2` | str or int | error/warning/info/debug or 1–4 |
| `low_level_debug` | `False` | `False` | bool | — |

Both integer and string spellings are accepted for `console_log_level` (e.g. `console_log_level: 3` and `console_log_level: info` are equivalent). `debug` accepts integers only. See [error-logging.md](error-logging.md) for the full level mapping.

### Scan-and-Jog

| Parameter | Python fallback | Shipped `nfc_vars.cfg` | Type | Bounds |
|---|---|---|---|---|
| `scan_enabled` | `True` | `True` | bool | — |
| `scan_jog_mm` | `50.0` | `25.0` | float | 1.0–500.0 |
| `scan_poll_interval` | `0.1` | `0.1` | float | 0.1–5.0 |

The scan max distance is derived at scan start from Happy Hare's saved
`mmu_calibration_bowden_lengths` value in `mmu_vars.cfg`, indexed by MMU gate.

---

## Per-Lane Override Example

Any key from `nfc_vars.cfg` can be overridden in a lane section:

```ini
[nfc_gate lane2]
mmu_gate:           2
i2c_mcu:            lane2
i2c_bus:            i2c3_PB3_PB4
debug:              3             ; verbose logging on this lane only
startup_poll_delay: 4.0           ; 4 s after lane0's 0 s, lane1's 2 s
poll_interval:      10            ; faster for bench testing
```

Only keys explicitly listed in the lane section take effect. All others inherit from `[nfc_gate]`.

---

## SpoolmanClient Lifecycle

### Shared instance (base `[nfc_gate]` section present)

When `nfc_vars.cfg` includes a base `[nfc_gate]` section, `NFCGateDefaults.__init__` creates the single `SpoolmanClient` and stores it on `self._spoolman`. Each `NFCGate` then receives this shared instance:

```python
# NFCGate.__init__ when defaults is not None:
if d is not None:
    self._spoolman = d._spoolman   # shared — same object for all lanes
```

All lanes share:
- URL resolution state (`_base_url`)
- TTL cache (`_cache` dict, keyed by normalized UID)
- Circuit breaker state (`_cb_failures`, `_cb_backoff_until`)

Because all polling runs on the single reactor thread, concurrent cache access is not possible — no lock is needed.

### Per-lane fallback (no base `[nfc_gate]` section)

When no base section is present, each `NFCGate.__init__` creates its own `SpoolmanClient` as a fallback. Each lane then has an independent URL, cache, and circuit breaker.

### When `_spoolman is None`

If `spoolman_url` is empty, no `SpoolmanClient` is created. Tags are still read and `process_read(uid_hex, None)` is called, returning `EVENT_UID_ONLY` for every tag. `_NFC_TAG_NO_SPOOL` fires and logs the UID. HH `MMU_GATE_MAP` is not called.

When `_spoolman` is set but Spoolman is unreachable: the circuit breaker opens after 3 consecutive failures and backs off for 60 seconds. During backoff, `lookup_spool_by_uid()` returns `None` immediately — behavior is the same as `_spoolman is None`.
