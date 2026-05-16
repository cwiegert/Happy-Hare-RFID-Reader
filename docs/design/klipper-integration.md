# Design: Klipper Integration

> Engineering reference — not end-user documentation.

---

## Threading Model

Klipper is single-threaded from the perspective of its extension system. All Python extras run on a single reactor greenlet. There is no conventional OS thread pool — concurrency is cooperative, managed by the reactor.

Consequences for NFC:
- `read_tag()` calls Klipper's MCU I2C helpers (`i2c_write`, `i2c_read`). These are synchronous calls that block the reactor greenlet while waiting for CAN round-trips from the EBB42. **They must be called from the reactor thread.** Moving them to a `threading.Thread` would deadlock or corrupt internal Klipper state.
- Reactor timer callbacks fire on the reactor thread. `_poll_timer_event` runs there — correct.
- GCode `run_script()` must also be called on the reactor thread. `KlipperInterface.dispatch()` uses `reactor.register_callback()` to ensure this.

---

## Timer Registration

```python
self._poll_timer = self.printer.get_reactor().register_timer(
    self._poll_timer_event
)
```

`register_timer` parks the timer at `reactor.NEVER`. It fires only after an explicit `update_timer`:

```python
# Arm — fire as soon as possible, then reschedule on each return value
reactor.update_timer(self._poll_timer, reactor.NOW)

# Park — no further callbacks until armed again
reactor.update_timer(self._poll_timer, reactor.NEVER)
```

Timer callback signature: `def _poll_timer_event(self, eventtime) → float`. The return value is the next absolute wake time. `reactor.monotonic() + N` means "N seconds from now". `reactor.NEVER` parks the timer indefinitely.

---

## Lifecycle: `klippy:connect` and `_delayed_init`

```python
self.printer.register_event_handler('klippy:connect', self._handle_connect)
self.printer.register_event_handler('klippy:disconnect', self._handle_disconnect)
```

### `_handle_connect`

Fires after all config sections are loaded and MCU connections are established. It does three things:

1. Registers GCode commands (mux command for `NFC`, fallback for `NFC_STATUS` if no base section)
2. Sends a console "connected" message: `📡 NFC Gate [laneN] connected`
3. Schedules `_delayed_init` as a one-shot timer 2 seconds later

The 2-second delay allows other I2C devices on the bus (e.g. BME280) to finish their own init sequences before the PN532 init runs.

### `_delayed_init`

Runs 2 seconds after `klippy:connect`. Does the actual hardware work:

1. Calls `self._reader.init()` → wakes the PN532 with `GetFirmwareVersion` (with retries), then sends `SAMConfiguration`
2. Sets `self._failed = True/False` based on whether the reader responds
3. Calls `_seed_cache_from_hh(eventtime)` — reads HH gate map and pre-populates the lane cache (see polling-state-machine.md)
4. Sends a ✅ or 💥 console status message with HH seed info
5. If `startup_polling == 1` and init succeeded: arms the poll timer with `startup_poll_delay` offset

None of the PN532 hardware initialization or HH seeding happens at `klippy:connect` time — all of it is in `_delayed_init`.

### `_handle_disconnect`

Parks the poll timer unconditionally. Prevents I2C traffic to a disconnected MCU. `self._polling = False` is also set so the timer will not re-arm itself if it fires during the shutdown race.

---

## GCode Command Registration: Mux Commands

Each `NFCGate` registers itself under the shared `NFC` command using Klipper's **mux command** API:

```python
self._gcode.register_mux_command(
    cmd='NFC',
    key='GATE',
    value=str(self._gate),
    func=self.cmd_NFC,
    desc="Control or test one configured NFC gate"
)
```

`register_mux_command` is a Klipper API that routes one GCode command to different handlers based on a parameter value. All lane instances register under the same `NFC` command name, but with different `GATE=` values. Klipper dispatches `NFC GATE=0 READ=1` to the lane-0 handler and `NFC GATE=2 SCAN=1` to the lane-2 handler automatically. This is different from `register_command`, which would register separate `NFC_LANE0`, etc. commands.

The `NFC_STATUS` command is registered as a plain `register_command` by `NFCGateDefaults` (the base `[nfc_gate]` section). If no base section exists, the first `NFCGate` registers it as a fallback.

Sub-commands within `cmd_NFC` are parsed by checking parameters in a fixed priority order: `READ`, `STATUS`, `INIT`, `SCAN`, `JOG_SCAN`, `CLEAR_CACHE`, `CLEAR`, `POLL`, `APPLY`, `HH_SYNC`. Low-level debug commands are checked first and take priority over all others.

`JOG_SCAN=1` triggers the scan-and-jog sequence on demand, bypassing the automatic 0→1 edge-detection trigger. Calls `scan_jog.manual_jog_scan(self, gcmd)` after verifying the reader is healthy, HH is idle, and no other gate is scanning.

---

## I2C Bus Access

Each `NFCGate` owns one `MCU_I2C` object:

```python
i2c = bus_module.MCU_I2C_from_config(
    config,
    default_addr=0x24,   # PN532 default I2C address
    default_speed=100000  # 100 kHz
)
```

This binds the I2C bus to a specific MCU (the EBB42 for this lane) and a specific hardware I2C peripheral (`i2c3_PB3_PB4` on PB3/PB4 of the STM32G0B1). The MCU name comes from `i2c_mcu` in the lane config — it must exactly match an `[mcu laneN]` section.

### I2C Transaction Chain per `read_tag()` call

```
read_tag()
  └─ read_target()
       └─ _transceive([InListPassiveTarget, 0x01, 0x00], expected_resp=0x4B)
            ├─ _send([InListPassiveTarget, ...])
            │    └─ i2c_write(frame_bytes)         ← 1 write transaction
            │
            ├─ _read_ack()  [polls for PN532 ACK]
            │    ├─ loop: i2c_read([], 1)           ← 1-byte reads at 5ms intervals
            │    │        until STATUS=0x01 (ready)
            │    └─ i2c_read([], 7)                 ← read 7-byte ACK frame
            │
            └─ _recv()  [polls for response ready]
                 ├─ loop: i2c_read([], 1)            ← 1-byte reads at 5ms intervals
                 │        until STATUS=0x01 (ready)
                 └─ i2c_read([], 32)                 ← read full response (32 bytes max)

  └─ _release_current_target()
       └─ _transceive([InRelease, Tg], expected_resp=0x53)
            ├─ _send([InRelease, Tg])                ← 1 write transaction
            ├─ _read_ack()                           ← as above
            └─ _recv()                               ← as above (12-byte read)
```

Two `_transceive` calls per `read_tag()`. Each `_transceive` involves: one write, a polling loop reading 1-byte status until ready (typically 1–3 polls at 5 ms each), and one full read. Total I2C transactions per `read_tag()`: roughly 6–10 depending on how quickly the PN532 becomes ready.

The `transceive_delay` (250 ms default) is the timeout passed to the `_transceive` call for `InListPassiveTarget`. The PN532 scans until a tag is found or its internal timer expires within this window. Increasing it gives the PN532 more time to find a tag; decreasing it risks missing tags that are slightly far from the antenna.

`read_tag()` catches all exceptions internally and returns `None` on error. I2C errors from a disconnected PN532 surface as a `None` return, which is treated as a missed read by `GateState.process_read()`. The outer poll timer exception handler only catches unexpected errors from Spoolman or state-machine code.

---

## GCode Dispatch

NFC dispatches state changes to HH by running GCode macros. The dispatch chain:

```python
# In _poll() — on the reactor thread
self._klipper.dispatch(event_type, gate, uid_hex, spool_id)

# KlipperInterface.dispatch() — schedules callback
self._reactor.register_callback(
    lambda e, et=event_type, g=gate, u=uid_hex, s=spool_id:
        self._run_gcode(et, g, u, s)
)

# KlipperInterface._run_gcode() — executes on next reactor iteration
gcode = self._printer.lookup_object('gcode')
gcode.run_script("_NFC_SPOOL_CHANGED GATE=0 SPOOL_ID=42 UID=A3F200CC")
```

`reactor.register_callback()` is used even though `_poll()` already runs on the reactor thread. This defers GCode execution to the next reactor iteration, ensuring the current poll cycle fully completes (including setting `_hh_confirmed_spool`) before GCode processing begins. This matters because HH status (`gate_spool_id`) will still reflect the old state until HH processes the macro — the next poll cycle may fire before that happens. See hh-interaction.md for the timing discussion.

`run_script()` queues the macro in the GCode command queue. It executes when the reactor processes it — which can be delayed if the printer is busy with another GCode command.

---

## MCU Firmware Version Matching

The PN532 driver issues raw I2C transactions directly to the MCU firmware. The Klipper MCU I2C protocol is version-locked between the host Python code and the compiled MCU firmware. If the host is updated (`git pull`) without reflashing MCU firmware:

- `_read_ack` reads fail immediately after the command write succeeds (protocol divergence)
- `i2c_read_response` timeouts appear with no obvious cause
- Other I2C devices on the same bus (e.g., BME280 temperature sensor on EBB42) may start misbehaving

This is not a software bug in the NFC system. Fix: build MCU firmware from the same Klipper host checkout and flash every lane MCU before testing NFC.
