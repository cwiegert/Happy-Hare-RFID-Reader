# Design: Bypass Reader

> Status: Proposed
> Scope: Design only. No IRQ/data-ready pin support in this phase.

---

## Goal

Add one NFC reader inside the MMU body that is **not tied to an EMU lane**.
The reader acts as a staging point for the next manually loaded spool:

1. User taps a tag on the hidden MMU reader.
2. NFC resolves the tag to a Spoolman spool ID, creating the spool first if
   `spoolman_auto_create` is enabled and the tag carries enough metadata.
3. NFC gives user feedback through LEDs.
4. User loads a spool through the normal MMU/pre-gate path.
5. Happy Hare assigns the staged spool ID to whichever gate is preloaded next.

Happy Hare already has the desired primitive:

```gcode
MMU_GATE_MAP NEXT_SPOOLID=<spool_id>
```

If a gate is preloaded within Happy Hare's timeout window after that command,
HH assigns that spool ID to the loaded gate. The bypass reader should use that
mechanism instead of trying to determine the destination gate itself.

---

## Non-goals

- Do not make the bypass reader part of the EMU lane reader set.
- Do not add PN532 IRQ/data-ready support yet.
- Do not scan-jog from the bypass reader.
- Do not infer gate assignment by inspecting NFC gate polling state.
- Do not duplicate Spoolman create/lookup logic already used by lane readers.
- Do not duplicate the existing NFC reader timer, read, resolve, debounce, or
  debug command machinery.
- Do not call Happy Hare Python internals directly. Use HH's public GCode
  command surface.

---

## Key Decision

Use a normal `NFCGate` instance for the physical reader and put it in bypass
mode. The reader can be configured as `[nfc_gate bypass]` with `mmu_gate: 255`.
Gate 255 is a reserved sentinel for the single bypass reader; it is outside the
real Happy Hare gate map and is not treated as an EMU lane.

The bypass reader has no physical EMU lane, so its dispatch behavior differs
from a normal lane:

```text
lane reader:
  physical gate -> read tag -> resolve spool -> assign that same gate

bypass reader:
  existing NFCGate poll -> resolve spool -> remember pending spool
  preload hook -> stage NEXT_SPOOLID
```

That means the bypass reader only needs to know:

- the most recently scanned spool ID
- whether that staged spool is still within the timeout window

It does **not** need to know:

- which gate is currently loaded
- which gate is about to preload
- whether an EMU lane reader is paused
- whether a lane reader has cached NFC state

The preload hook is primarily a timing signal. NFC should wait until Happy Hare
reports that a pregate-triggered preload is about to start, then issue
`MMU_GATE_MAP NEXT_SPOOLID=<id>` if a valid pending spool exists. Happy Hare
still decides which gate receives that staged spool.

`bypass` is the NFC object name. `mmu_gate: 255` is the command-routing
sentinel. Scan-jog edge detection and HH gate-map matching should be disabled
for this reader.

---

## Proposed Config

The bypass reader is configured as a regular `[nfc_gate ...]` section with a
single bypass flag. This avoids a second config namespace and lets the
reader share the existing PN532 driver, Spoolman client, tag parser, logging,
status, debug, and polling controls.

Example:

```ini
[nfc_gate bypass]
mmu_gate: 255
i2c_mcu: mmu
i2c_bus: i2c1
i2c_address: 0x24

bypass: true
startup_polling: false
scan_enabled: false
poll_interval: 1.0
bypass_read_timeout: 120.0

bypass_pending_timeout: 120.0

bypass_tag_read_effect: mmu_RFID_read
```

Suggested keys:

| Key | Default | Meaning |
|---|---:|---|
| `bypass` | `false` | Enables bypass dispatch behavior for this `NFCGate`. |
| `mmu_gate` | required | Normal lane readers use their real Happy Hare gate number. The bypass reader must use reserved gate `255`. |
| `startup_polling` | `false` | Bypass readers should not poll at Klipper startup. |
| `scan_enabled` | `false` | Scan-jog must be disabled for the bypass reader. |
| `poll_interval` | `1.0` | Existing NFCGate poll interval while activated with `READ=1`. |
| `bypass_read_timeout` | `120.0` | Maximum seconds bypass polling may remain active after `READ=1` without a valid tag. |
| `bypass_pending_timeout` | required when `bypass: true` | Seconds a scanned spool remains eligible for the next preload. |
| `bypass_tag_read_effect` | required when `bypass: true` | Happy Hare LED effect name for a successful tag read. Intended visual response: blinking yellow. |

Config validation requirement:

```text
if bypass == true:
    only one configured nfc_gate may have bypass == true
    mmu_gate must be 255
    bypass_read_timeout is optional; 120.0 seconds if not set
    bypass_pending_timeout must be defined
    bypass_tag_read_effect must be defined
```

This keeps bypass mode explicit: v1 supports exactly one bypass reader, and
that reader must define both how long a scanned spool remains pending and what
visual feedback confirms that a tag was read.

The reader should inherit global tag-resolution settings where possible:

- `spoolman_url`
- `spoolman_rfid_key`
- `tag_parsing`
- `spoolman_auto_create`
- metadata read limits and parser settings
- logging/debug settings

---

## State Model

The bypass reader should keep using `GateState` for the current physical tag.
`GateState.process_read()` already handles changed tags, UID-only tags, repeated
reads, metadata-only reads, and absent debounce.

Bypass mode only adds small pending-spool fields to the `NFCGate` instance:

```python
self._bypass_pending_uid = None
self._bypass_pending_spool = None
self._bypass_pending_deadline = 0.0
self._bypass_pending_auto_created = False
self._bypass_last_error = None
self._bypass_read_deadline = 0.0   # set when READ=1 starts; enforces bypass_read_timeout
```

State meanings:

| State | Meaning |
|---|---|
| Inactive | Existing NFC polling is stopped. This is the default after startup. |
| Active | Existing NFC polling is running because `READ=1` was issued manually or from a gate-ejected hook. |
| Idle | Active, but no valid pending spool. |
| Pending | A tag resolved to a spool and is waiting for the next preload hook. |
| Expired | The pending timeout elapsed before preload. Pending state clears. |
| Error | Tag read succeeded but did not resolve, parse failed, or Spoolman was unavailable. |

The state should not track a gate number. Happy Hare owns the destination gate.

---

## Existing Poll Loop Reuse

For v1, the bypass reader uses the existing `NFCGate` timer and `_poll()` flow.
The PN532 data-ready/IRQ pin is intentionally left out.

Activation uses the existing `NFC` GCode command on the bypass gate. There are
two activation modes:

1. Manual start, when the user wants continuous scanning before loading.
2. Automatic start from a Happy Hare hook when an exit/eject is recognized.

```gcode
NFC GATE=255 READ=1   ; start polling the bypass reader
NFC GATE=255 READ=0   ; stop polling the bypass reader
NFC GATE=255 POLL=1   ; force one full read/resolve cycle
NFC GATE=255 SCAN=1   ; low-level hardware scan only
```

The hook path should use a small user macro. Happy Hare calls the user macro;
the user macro calls NFC:

```gcode
[gcode_macro NFC_BYPASS_GATE_EJECTED]
description: Called by Happy Hare after MMU_UNLOAD/MMU_EJECT to start bypass RFID polling
gcode:
    NFC GATE=255 READ=1
```

Then configure the Happy Hare extension variables:

```ini
; start bypass polling after a gate is ejected/unloaded
variable_user_post_unload_extension: 'NFC_BYPASS_GATE_EJECTED'

; stage NEXT_SPOOLID before a pregate-triggered automatic preload
variable_user_pre_load_extension: 'NFC_BYPASS PRELOAD_CHECK'
```

`variable_user_post_unload_extension` fires after both `MMU_UNLOAD` and
`MMU_EJECT`. Happy Hare's `mmu_macro_vars.cfg` groups these under the "unload"
operation (comment at the `variable_user_post_unload_extension` line: "unload —
individual MMU_UNLOAD/MMU_EJECT operation"). This hook is confirmed correct for
starting bypass polling.

`variable_user_pre_load_extension` fires before a pregate-triggered automatic
preload starts. This is the preload hook that calls `NFC_BYPASS PRELOAD_CHECK`
to stage `NEXT_SPOOLID` if a valid pending spool exists. This resolves Open
Question #2.

This macro is the bridge from the Happy Hare macro layer into NFC Python. Happy
Hare does not call an NFC Python method directly. It runs a user GCode macro,
which runs the registered `NFC` GCode command. The existing
`NFCGate.cmd_NFC()` handler then calls the Python method for the requested
operation. For `READ=1`, that means `_set_reading(..., True)` starts the
existing poll timer. `POLL=1` remains useful for debugging or a forced immediate
single read.

### Scan Requirements

Bypass RFID reads must use the same safety posture as the main NFC path:

1. Do not read while `print_stats.state == 'printing'`. For normal lanes this
   check lives in the scan-jog block, which bypass never enters. For bypass,
   `_is_printing()` must be checked at the top of the bypass `_poll()` branch
   before any I2C read is attempted.
2. Do not start scan-jog; `scan_enabled: false` is required.
3. Do not use HH gate ownership, spool assignment, or lane cache state to decide
   what the bypass reader means.
4. When started by the eject hook, only start polling after Happy Hare has
   completed the unload/eject sequence.
5. If HH reports an active load/unload/homing action when bypass polling fires,
   skip that poll and try again on the next normal poll interval.
6. Once a valid tag resolves to a pending spool, stop bypass polling using the
   same internal path as `NFC GATE=255 READ=0`.
7. `POLL=1` follows the same bypass scan requirements as timer polling. It must
   not bypass print/action checks or read RFID during unsafe states.
8. If `READ=1` is active for `bypass_read_timeout` seconds without resolving a
   valid tag, stop polling using the same internal path as `NFC GATE=255 READ=0`.
9. If a preload hook fires while bypass polling is still active (tag seen but
   Spoolman resolution not yet complete), `PRELOAD_CHECK` will find no pending
   spool and will do nothing — the preload proceeds without bypass staging. The
   user can re-scan after the load. This window is bounded by `poll_interval`
   plus Spoolman response time and is not treated as an error.

`NFC_BYPASS PRELOAD_CHECK` uses the existing mainline safety precheck style, but
it must not read RFID. It checks only whether printing or active MMU
load/unload/homing should prevent staging. If safe, it inspects the already
pending spool and, if valid, stages it with `MMU_GATE_MAP NEXT_SPOOLID=<id>`.

The reused flow is:

```text
NFCGate._poll_timer_event()
  -> NFCGate._poll()
  -> _read_current_tag()
  -> _resolve_spool()
  -> GateState.process_read()
  -> _poll_dispatch_event()
```

Only the last step changes in bypass mode. Instead of dispatching a normal
gate assignment event, an `EVENT_CHANGED` records a pending bypass spool and
emits LED feedback:

```python
def _poll_dispatch_event(event):
    if self._bypass:
        return self._bypass_handle_event(event)
    return self._normal_gate_dispatch(event)
```

Bypass-specific behavior:

- `EVENT_CHANGED` with a real spool stores pending UID/spool/deadline.
- `EVENT_CHANGED` from auto-create records that the spool was newly created so
  the preload path can assess whether Happy Hare needs a Spoolman refresh before
  `NEXT_SPOOLID`.
- After a valid pending spool is stored, bypass mode stops polling
  automatically. The user has received the tag-read confirmation, and the
  pending spool should now survive the tag being removed from the reader.
- `EVENT_UID_ONLY` records an error only when there is no valid pending spool.
  It must not clear an already pending spool.
- `EVENT_REMOVED` must not clear an already pending spool. Tag removal after a
  successful scan is expected.
- Repeated reads of the same tag stay quiet because `GateState` already returns
  no event for unchanged UID/spool.
- Bypass `_poll()` should skip HH lane-specific logic:
  `_poll_hh_pause_check()`, `_check_hh_cleared()`, startup seeding from HH, and
  any HH gate-map matching.
- `startup_polling` remains false, so no scan happens at Klipper startup.
- `scan_enabled` remains false, so scan-jog never runs for `bypass`.

This keeps the bypass path on the same reader lifecycle and error handling as
normal lanes, while changing only what happens after a tag resolves.

---

## Tag Resolution

The bypass reader should use the same resolution path as lane readers:

```text
PN532 read
  -> tag_handler / parser
  -> Spoolman UID lookup
  -> optional auto-create
  -> spool_id or UID-only result
```

If `spoolman_auto_create` creates a new spool, the bypass path should remember
that fact. Because the bypass assignment uses `MMU_GATE_MAP NEXT_SPOOLID=<id>`,
it may not need the same HH Spoolman refresh path as a direct gate assignment.
This should be assessed during implementation instead of run automatically.

```gcode
MMU_GATE_MAP NEXT_SPOOLID=<spool_id>
```

If testing shows HH needs to refresh its Spoolman cache before accepting an
auto-created spool ID as `NEXT_SPOOLID`, the refresh should happen at preload
check time. It should not run automatically just because a tag was scanned.

If the tag cannot resolve:

- keep any previously pending valid spool
- do not make a pending `NEXT_SPOOLID` available
- allow later preloads to continue normally without a bypass spool assignment

Pending state is cleared only by:

- timeout
- successful `NFC_BYPASS PRELOAD_CHECK`
- explicit `NFC_BYPASS CLEAR`
- a new valid tag replacing the previous pending spool

---

## Happy Hare Integration

Stage the bypass spool directly from Python at preload-check time by issuing
Happy Hare's public GCode command:

```text
NFC bypass reader remembers pending spool
HH pregate preload hook fires
  -> NFC_BYPASS PRELOAD_CHECK
  -> gcode.run_script("MMU_GATE_MAP NEXT_SPOOLID=<id>")
```

There is no need for an intermediate `_NFC_BYPASS_SPOOL_READY` macro whose only
job is to call `MMU_GATE_MAP`.

Default preload-check shape:

```python
def _bypass_preload_check(self):
    if self._is_printing():
        return True
    if self._hh_action_is_loading_unloading_or_homing():
        return True
    self._bypass_expire_pending_if_needed()
    if not self._bypass_pending_spool_is_valid():
        return True

    gcode.run_script(
        "MMU_GATE_MAP NEXT_SPOOLID=%d" % self._bypass_pending_spool)
    self._bypass_clear_pending()
    return True
```

`MMU_SPOOLMAN REFRESH=1` is intentionally not part of the default path. The
implementation should assess whether it is needed for auto-created spools when
using `NEXT_SPOOLID`.

The preload hook should be explicit. Happy Hare should notify NFC when a
pregate-triggered automatic preload is about to start. That hook is likely only
needed to tell NFC when to issue `MMU_GATE_MAP NEXT_SPOOLID=<id>`.

Conceptual hook:

```text
HH pregate preload detected
  -> NFC BYPASS_PRELOAD_CHECK
  -> if pending spool exists: issue MMU_GATE_MAP NEXT_SPOOLID=<id>
  -> if no pending spool: do nothing and allow preload to continue
```

The exact HH hook shape may be macro-based, but the responsibility split should
stay the same:

| Owner | Responsibility |
|---|---|
| NFC bypass reader | Detect tag, resolve spool, maintain pending timeout, stage `NEXT_SPOOLID` when HH preload hook fires and a pending spool exists. |
| Happy Hare | Detect that pregate preload is starting and apply `NEXT_SPOOLID` to the loaded gate. |
| User macro layer | Calls `NFC_BYPASS PRELOAD_CHECK` from the appropriate HH hook. |

---

## No-Pending Behavior

When no valid bypass tag is pending:

- pregate-triggered preload proceeds normally
- no `NEXT_SPOOLID` is staged by NFC
- Happy Hare remains fully usable for manual spool assignment and normal loading

The bypass reader must never block filament loading. A missing scan only means
there is no bypass spool to stage.

---

## Timeout Behavior

`pending_timeout` starts when a tag resolves. `NEXT_SPOOLID` is not issued until
the preload hook fires.

If a gate is preloaded before the timeout:

```text
scan tag -> pending spool 42
HH preload hook/event occurs
NFC issues MMU_GATE_MAP NEXT_SPOOLID=42
HH applies NEXT_SPOOLID=42 to the gate being loaded
NFC clears pending state
LED success/ready feedback ends
```

After `scan tag -> pending spool 42`, bypass polling should already be stopped.
Removing the tag from the reader must not clear the pending spool.

If the timeout expires first:

```text
scan tag -> pending spool 42
no preload before pending_deadline
NFC clears pending state
LED returns to idle/default
```

After timeout:

- preloading works normally
- no spool assignment happens from the bypass reader until a new tag is scanned
- Happy Hare remains available for manual spool assignment

Scanning is activation-based, not startup-based. A gate-ejected hook is the
normal automatic activation path, because ejecting a gate is the moment the user
is likely preparing the next spool. Manual activation remains available with
`NFC GATE=255 READ=1`.

Polling can be stopped with `NFC GATE=255 READ=0`. If no valid tag is found
after `bypass_read_timeout` seconds, bypass mode stops polling automatically.
The default timeout is 120 seconds.

---

## LED Feedback

LED feedback should be minimal. The only required bypass LED indication is:

```text
tag read successfully -> blinking yellow LED response
```

This should follow the same model as the EMU LED configuration: define a named
`[mmu_led_effect ...]`, then reference that effect by name from the NFC config.
NFC should not need a dedicated LED macro.

Example NFC config:

```ini
bypass_tag_read_effect: mmu_RFID_read
```

Example effect definition, in the same style as `emu_macros.cfg`:

```ini
[mmu_led_effect mmu_RFID_read]
define_on: gates,exit
layers: strobe 1 0 top (1.0, 0.75, 0.0)
```

When a tag resolves and becomes the pending bypass spool, NFC issues:

```gcode
MMU_SET_LED EXIT_EFFECT=<bypass_tag_read_effect> DURATION=3
```

`MMU_SET_LED` is the public HH LED command (`mmu_led_manager.py`). Passing
`EXIT_EFFECT=<name>` with a base effect name causes HH to internally invoke
`_MMU_SET_LED_EFFECT EFFECT=unit0_<name>_exit`, which plays the
`[mmu_led_effect]` defined effect on all exit LEDs. `DURATION` limits how long
the effect runs before HH returns exit LEDs to their default state.

The effect name in config (`bypass_tag_read_effect`) is the bare `[mmu_led_effect]`
section name without the `unit0_` prefix and segment suffix that HH appends
internally. NFC passes the name exactly as configured; HH handles the rest.

Example full invocation from `_bypass_handle_event`:
```python
gcode.run_script(
    "MMU_SET_LED EXIT_EFFECT=%s DURATION=3" % self._bypass_tag_read_effect)
```

If `MMU_SET_LED` is not available (no `[mmu_leds]` configured), the
`run_script` call will fail silently with a GCode error logged to the console.
NFC should not treat LED failure as a fatal error — the bypass spool is already
staged at this point and the preload will proceed regardless.

---

## Commands

Proposed user/debug commands:

```gcode
NFC_BYPASS STATUS
NFC_BYPASS CLEAR
NFC_BYPASS PRELOAD_CHECK
```

Meanings:

| Command | Purpose |
|---|---|
| `STATUS` | Report pending UID/spool, timeout remaining, and last error. |
| `CLEAR` | Clear pending state and return LEDs to idle. |
| `PRELOAD_CHECK` | Hook command used by HH/user macros before automatic preload. |

`NFC_BYPASS` operates on the single configured bypass reader (`mmu_gate: 255`).
Because v1 permits only one bypass reader, the command does not need a reader
parameter.

`CLEAR` clears both the bypass pending state (`_bypass_pending_*` fields) and
the `GateState` tag cache, then returns LEDs to idle. It is the correct command
to fully reset bypass state.

`NFC GATE=255 CLEAR_CACHE=1` clears only the `GateState` tag cache (UID and
spool). It does **not** clear `_bypass_pending_*` fields. Use it when you want
to force a fresh hardware read on the next poll without discarding a pending
spool assignment.

`PRELOAD_CHECK` should be intentionally small. It should only answer:

- is a valid pending spool available?
- has it expired?
- is the printer/MMU in a safe state to stage `NEXT_SPOOLID`?
- should `MMU_GATE_MAP NEXT_SPOOLID=<id>` be issued?

It should not inspect lane NFC state, and it must not read RFID.

Status output should include bypass state. `NFC_BYPASS STATUS` should report one
of:

```text
bypass: idle
bypass: polling, no tag pending
bypass: pending spool 42 uid=ABCDEF expires in 87s
bypass: expired spool 42 uid=ABCDEF
bypass: error <last_error>
```

`NFC_STATUS` should include the bypass reader after the numbered EMU lanes.
`_lane_status_lines()` currently iterates `_lane_instances` matched against
`lane<N>` MCU names. A bypass reader (`_name == 'bypass'`) will never match a
lane MCU and would be silently omitted from the primary path. The fix: after
the lane MCU loop, scan `_lane_instances` for any gate where `_bypass is True`
and append its status line. No separate section header is needed — one
appended line using the same `NFC_BYPASS STATUS` output format is sufficient.

General reader activation and debug should use the existing `NFC` command on
the configured bypass gate:

```gcode
NFC GATE=255 READ=1
NFC GATE=255 READ=0
NFC GATE=255 POLL=1
NFC GATE=255 STATUS=1
NFC GATE=255 CLEAR_CACHE=1
```

---

## Minimal Implementation Shape

Prefer a small extension of `NFCGate` rather than a new reader module.

Reuse:

- `NFCGate._poll_timer_event()`
- `NFCGate._poll()`
- `GateState`
- `pn532_driver.py` / `rc522_driver.py`
- `tag_handler.py`
- `spoolman_client.py`
- `klipper_interface.py` dispatch style
- existing logging setup
- existing tag parser and auto-create behavior

The bypass reader is an NFC gate object with bypass dispatch enabled. It is not
a Happy Hare gate.

New code should be mostly:

- config keys on `[nfc_gate bypass]`: `bypass`, `bypass_pending_timeout`,
  `bypass_read_timeout`, and `bypass_tag_read_effect`
- pending-spool fields on `NFCGate`
- a bypass branch in `_poll_dispatch_event()`
- a small `_bypass_handle_event()` helper
- `NFC_BYPASS STATUS/CLEAR/PRELOAD_CHECK` command handling
- a helper to run `MMU_GATE_MAP NEXT_SPOOLID=<id>` from `PRELOAD_CHECK`
- reserved gate `255` routing for the single bypass reader, e.g.
  `NFC GATE=255 READ=1`
- bypass status output in both `NFC_BYPASS STATUS` and `NFC_STATUS`

---

## Open Questions

1. ~~Confirm on hardware that `variable_user_post_unload_extension` fires after
   `MMU_EJECT`.~~ **Resolved.** HH `mmu_macro_vars.cfg` explicitly groups
   `MMU_UNLOAD`/`MMU_EJECT` under the same "unload" operation; this hook covers
   both. Confirm timing on hardware (gate empty before first poll tick) during
   integration testing.
2. ~~What exact Happy Hare hook fires before pregate-triggered automatic
   preload?~~ **Resolved.** `variable_user_pre_load_extension` fires before a
   pregate-triggered preload. Set it to `'NFC_BYPASS PRELOAD_CHECK'`.
3. Does `MMU_GATE_MAP NEXT_SPOOLID=<id>` accept a newly auto-created spool ID
   without `MMU_SPOOLMAN REFRESH=1`, or is a refresh required at preload-check
   time?
4. ~~What is the exact HH LED command that plays a named `mmu_led_effect`?~~
   **Resolved.** `MMU_SET_LED EXIT_EFFECT=<name> DURATION=<seconds>`. HH's
   `mmu_led_manager.py` maps the base effect name to the internal
   `unit0_<name>_exit` effect and invokes `_MMU_SET_LED_EFFECT`. See LED
   Feedback section for the full invocation.
