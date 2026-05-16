# Design: Error Handling and Logging

> Engineering reference — not end-user documentation.

---

## Log Architecture

The NFC system uses a dedicated logger named `nfc_gate` separate from the main Klipper logger. It writes to `nfc_reader.log` in the same directory as `klippy.log`.

```python
# log.py — module-level singleton created at import time
logger = _build_logger()
```

`_build_logger()` creates the logger, attaches `_DateRotatingFileHandler` and `_KlippyForwardHandler`, sets level to `DEBUG` (all messages pass through; the per-instance `debug` integer guards in application code control what gets emitted), and sets `logger.propagate = False`.

`propagate = False` prevents the standard Python propagation chain. Klippy routing is instead handled by `_KlippyForwardHandler`, a second handler attached to the `nfc_gate` logger that selectively forwards records to the root logger (klippy.log):

```python
class _KlippyForwardHandler(logging.Handler):
    def emit(self, record):
        if record.levelno < logging.WARNING:
            return          # INFO and DEBUG stay in nfc_reader.log only
        logging.getLogger().handle(record)   # WARNING+ → klippy.log
```

This means **any** `logger.warning()` or `logger.error()` call anywhere in the package automatically appears in both logs — no special wrapper function required. `logger.info()` and `logger.debug()` calls go to `nfc_reader.log` only.

```python
# Goes to nfc_reader.log ONLY (INFO level):
logger.info("nfc_gate: [%s] Spoolman→spool_id=%s", name, spool_id)

# Goes to nfc_reader.log AND klippy.log (WARNING level):
logger.warning("nfc_gate: [%s] Spoolman unreachable: %s", name, e)

# Goes to nfc_reader.log AND klippy.log (ERROR level):
logger.error("nfc_gate: [%s] PN532 did not respond — check wiring", name)
```

The module-level `info()`, `warning()`, `error()` wrapper functions in `log.py` are kept for call-site compatibility but are now thin aliases to `logger.*` — the handler does all routing.

The `configure()` function in `log.py` is called by `NFCGateDefaults.__init__` and optionally by `NFCGate.__init__`. It replaces the file handler with the configured path and sets up the optional console handler.

---

## Log File Rotation

The file handler is `_DateRotatingFileHandler`, not Python's standard `RotatingFileHandler` (which rotates by file size). This is a custom date-based rotation:

- **Rotates on day advance**: on each `emit()`, the current date is compared to the date of the first entry in the open log file. When the day has advanced, the file is closed, renamed to `nfc_reader.log.YYYY-MM-DD` (using the date from its first entry), and a fresh file is opened.
- **Startup rotation**: at handler construction time, if the existing `nfc_reader.log` contains entries from a prior day, it is immediately rotated before opening a new file. This handles Klipper restarts that happen after midnight.
- **Archive pruning**: after each rotation, old archives are deleted if there are more than 7 or if the archive is older than 7 days. The cutoff date is read from the archive filename, not filesystem mtime.
- **No size limit**: a single day's log file can grow arbitrarily large. Tune `debug` level if log volume is a concern.

Log line format (set by `_LOG_FORMATTER`):

```
2026-04-20 14:32:01 INFO     nfc_gate: [lane0] gate 0 — tag read uid=A3F200CC
2026-04-20 14:32:01 DEBUG    nfc_gate: [lane0] POLL  gate=0   tag=A3F200CC           →  quiet  (spool=42, uid unchanged)
2026-04-20 14:32:31 INFO     nfc_gate: [lane0] gate 0 — spool confirmed by NFC and HH; suspending scan
```

Format: `YYYY-MM-DD HH:MM:SS LEVEL    message`. The level field is left-padded to 8 characters.

---

## Debug Levels

| Level | Value | What is logged | nfc_reader.log | klippy.log |
|---|---|---|---|---|
| None | `0` | Nothing | — | — |
| Errors | `1` | Init failures, hardware errors, I2C errors | ✅ | ✅ |
| Warnings | `2` | Unexpected conditions, config problems, lifecycle warnings **(default)** | ✅ | ✅ |
| Integration | `3` | Spoolman lookups, HH gate sync, dispatch decisions, cache hits, scan mode start/success/deferred | ✅ | ❌ |
| Trace | `4` | Full I2C frame trace, every byte, every poll event, per-jog-step scan progress | ✅ | ❌ |

`debug` accepts **integers only** (0–4). It is read with `config.getint()` so string values like `debug: info` are rejected. The `console_log_level` parameter accepts both integers and strings (`error`, `warning`, `info`, `debug`).

The `debug` integer is an application-layer gate — code uses guards like `if self._debug >= 3: logger.info(...)`. The Python logger itself is always set to `DEBUG` (passes everything). The `_KlippyForwardHandler` does the sink routing: `WARNING+` records reach klippy.log, `INFO` and `DEBUG` records stay in nfc_reader.log only.

**For normal printing:** `debug: 2`. Warnings and errors only. Minimal file volume.
**During setup / testing scan-jog:** `debug: 3`. Spoolman lookups, HH sync events, scan start/success visible without per-poll noise.
**Diagnosing I2C problems or scan-jog step detail:** `debug: 4`. Every I2C byte logged; each jog step distance logged.

---

## Console Output

When `console_output: True`, NFC log messages at or above `console_log_level` are sent to the Fluidd/Mainsail GCode console.

**Errors are always sent to the console** regardless of `console_output` setting, once the `gcode` object is available. This is unconditional in `_respond_to_console()`:

```python
if record.levelno < logging.ERROR:
    if not _console_enabled or record.levelno < _console_level:
        return  # filtered
# ERROR-level and above: always fall through
```

Console output uses `reactor.register_callback()` to send the message on the reactor thread, avoiding thread-safety issues with `respond_info()`.

The console handler is a `_GCodeConsoleHandler` attached to the `nfc_gate` logger. Its `emit()` delegates to `_respond_to_console()`. The handler is added once and never removed — `configure_console()` checks for its presence before adding.

---

## Error States

### `_failed` Flag

`NFCGate._failed` is set to `True` when the PN532 does not respond during `_delayed_init`:

```python
def _delayed_init(self, eventtime):
    try:
        self._reader.init()         # GetFirmwareVersion with retries, then SAMConfig
        if self._reader.is_alive():
            self._failed = False
            logger.info("nfc_gate: [%s] PN532 reader OK", self._name)
        else:
            self._failed = True
            logger.error("nfc_gate: [%s] PN532 did not respond — check wiring", ...)
    except Exception as e:
        self._failed = True
        logger.error("nfc_gate: [%s] init error: %s", self._name, e)
```

When `_failed` is True:
- `_poll_timer_event` sets `_polling = False` and returns `NEVER` — timer parks, polling stops
- `_set_reading(enabled=True)` refuses to start and prints a console message
- `NFC_STATUS` shows `READER FAILED (check wiring, address 0x24)`
- `_seed_cache_from_hh()` is skipped (only called if `not self._failed`)

Recovery: `NFC GATE=n INIT=1` calls `_manual_init()`, which clears `_failed` and re-runs `reader.init()` + `reader.is_alive()`. If the reader responds, the gate is back to normal and `READ=1` can start polling.

`_failed` is not set by poll errors — only by explicit `init()` failure. A failed init indicates the hardware is absent or mis-wired (persistent). Poll errors may be transient.

### Poll Error Handling

All exceptions that escape `_poll()` are caught in the timer callback:

```python
def _poll_timer_event(self, eventtime):
    ...
    try:
        self._poll()
    except Exception:
        logger.exception("nfc_gate: [%s] poll error", self._name)
    return self.reactor.monotonic() + self._poll_interval
```

`logger.exception()` logs the full Python traceback automatically. Polling continues on schedule — the timer reschedules normally. Note that this handler is a safety net for the Spoolman and state-machine code paths. It does not fire for PN532 I2C errors because `read_tag()` catches those internally (see below).

### PN532 Hardware Disconnect Mid-Session

`read_tag()` catches all exceptions internally and returns `None`:

```python
def read_tag(self):
    try:
        target_info = self.read_target()
        ...
        return uid_hex
    except Exception as e:
        if self._debug >= 4:
            logger.debug("read_tag: gate %d error (tag removed mid-scan?): %s", ...)
        return None
```

A PN532 hardware disconnect causes `read_tag()` to return `None` — exactly the same as a missed read. `_poll()` receives `None`, calls `GateState.process_read(None, None)`, and `miss_count` increments. After `absent_threshold` consecutive `None` returns, `EVENT_REMOVED` fires normally. Polling continues throughout, with the disconnect details logged only at `debug >= 4`.

The error is therefore transparent to the state machine: a disconnected PN532 looks identical to a removed spool. The difference is visible in the log — `read_tag` errors appear alongside the miss-count increments.

---

## SpoolmanClient Circuit Breaker

SpoolmanClient prevents a slow or dead Spoolman from blocking the reactor thread on every poll. Thresholds are hardcoded in `SpoolmanClient.__init__`:
- `_CB_THRESHOLD = 3` — consecutive failures before circuit opens
- `_CB_BACKOFF = 60.0` — seconds to back off before retrying

State variables:
- `_cb_failures` — consecutive failure count; resets to 0 on any success
- `_cb_backoff_until` — `time.monotonic()` timestamp when backoff expires

Logic in `_fetch_spools()`:

```
if _cb_failures >= _CB_THRESHOLD:
    if now < _cb_backoff_until:
        → return None immediately (circuit open, no HTTP request)
    else:
        → allow one probe through (backoff elapsed)

if request succeeds:
    → reset _cb_failures = 0 (circuit closed, log "connection restored")

if request fails:
    → _cb_failures += 1
    → _cb_backoff_until = now + 60.0
    → if _cb_failures >= 3: log warning "circuit open, backing off 60s"
```

When the circuit is open, `lookup_spool_by_uid()` returns `None`. `GateState.process_read()` receives `(uid_hex, None)` — tag present, spool unknown. This fires `EVENT_UID_ONLY` → `_NFC_TAG_NO_SPOOL`. The user sees the UID logged but HH is not updated.

This is intentional: a Spoolman outage does not affect HH or printing. Gates that were already matched when Spoolman went down retain their spool assignments — the NFC cache holds the last good value and polling suspends normally. Only new assignments (new tag reads on previously empty gates) are blocked.

---

## What Gets Logged Where: Summary

| Message type | `nfc_reader.log` | `klippy.log` | Console |
|---|---|---|---|
| Per-poll / movement trace (`debug ≥ 4`, `logger.debug()` or trace console message) | ✅ | ❌ | if `console_output: True` and `console_log_level ≤ 4` |
| Integration — Spoolman / HH sync / scan state (`debug ≥ 3`, `logger.info()` or `[OK]`/`[SCAN]`/`[REWIND]`) | ✅ | ❌ | if `console_output: True` and `console_log_level ≤ 3` |
| Warnings — config issues, failure transitions (`debug ≥ 2`, `logger.warning()` or `[WARN]`) | ✅ | ✅ | if `console_output: True` and `console_log_level ≤ 2` |
| Errors — init fail, I2C error (`debug ≥ 1`, `logger.error()`) | ✅ | ✅ | ✅ always |
| PN532 init result OK (`logger.info()`) | ✅ | ❌ | ✅ via `respond_info` |
| PN532 init result failed (`logger.error()`) | ✅ | ✅ | ✅ via `respond_info` |
| Startup seed result — unconditional `logger.info()` | ✅ | ❌ | ✅ via `respond_info` |

`klippy.log` receives all `WARNING` and `ERROR` level records automatically via `_KlippyForwardHandler`. No explicit `log_both()` call is required. `INFO` and `DEBUG` records go to `nfc_reader.log` only regardless of which call site emits them.
