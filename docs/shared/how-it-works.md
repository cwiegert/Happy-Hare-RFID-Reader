# How It Works

[← README](../../Readme.md)

---

## Startup — Boot Sequence

When Klipper connects, each lane goes through this sequence before polling begins:

```
klippy:connect
  └─ NFC_Manager registers commands
  └─ schedules _delayed_init()  (2 s delay — lets I2C and HH settle)

_delayed_init()
  └─ initialises NFC reader
  └─ reads Happy Hare gate map  ← seeds local lane cache from HH state
  └─ starts background polling  (if startup_polling: 1)
```

### Why seed from Happy Hare?

After a Klipper restart the NFC lane cache is empty — but Happy Hare already knows which spool is in each gate from its own persisted state. Without seeding, the first poll would detect the tag, resolve it through Spoolman, and fire `_NFC_SPOOL_CHANGED` — redundantly telling Happy Hare something it already knows. With seeding, that first poll is absorbed silently.

If the physical spool was swapped while Klipper was down, the resolved spool_id won't match the seed, and `_NFC_SPOOL_CHANGED` dispatches normally.

### Startup console output

```
[OK] NFC[lane0]: ready.  HH seed: spool_id=42  Startup polling is enabled; first poll in 0.0s.
[OK] NFC[lane1]: ready.  HH reports gate empty  Run NFC GATE=1 READ=1 to start polling.
```

The seed is one-shot — it fires at most once per lane per boot, on the first `CHANGED` event. If Happy Hare wasn't ready when the NFC init ran, the seed step is skipped and a manual `NFC_HH_SYNC_CACHE` re-syncs all lanes.

---

## Per-Poll Flow

Tags are never written to. The NFC tag's factory UID is stored as a Spoolman extra field (`rfid_tag` by default). On every poll:

```
NFC reader reads tag UID (or detects absence)
        │
        ▼
SpoolmanClient resolves UID → spool_id
(in-memory cache if fresh · HTTP query if expired or cleared)
        │
        ▼
NFC_Manager compares (uid, spool_id) against the lane cache
Both must match to be considered unchanged
        │
        ▼
State changed?
  ├─ New / re-assigned spool  →  _NFC_SPOOL_CHANGED  GATE=n  SPOOL_ID=id  UID=uid
  ├─ Tag absent (threshold)   →  _NFC_SPOOL_REMOVED  GATE=n
  └─ UID not in Spoolman      →  _NFC_TAG_NO_SPOOL   GATE=n  UID=uid
        │
        ▼
nfc_macros.cfg calls MMU_GATE_MAP ... to Happy Hare
```

The `(uid, spool_id)` combination check means that if the same physical tag is re-registered to a different spool in Spoolman, the change is detected on the next poll after the cache expires (or after `CLEAR_CACHE=1`).

---

## Scan-and-Jog Flow

When a spool is loaded, the NFC tag is on the hub face — it may be pointing any direction. The scan-jog loop rotates the spool until the tag comes within read range of the NFC reader antenna.

During this scan, the current gate also checks for a narrow physical edge case:
if gate `N` reads a UID already cached on gate `N - 1`, the read is treated as
left-neighbor interference. NFC briefly shifts the left neighbor out of the
reader field, clears the false read, continues scanning gate `N`, and restores
the neighbor when the scan exits.

### Trigger

The poll tick runs the gate-status edge check on every cycle. When all conditions are met, scan mode starts and the poll timer parks itself at `NEVER` for the duration.

```
_poll_timer_event  (every poll_interval seconds)
  └─ reads HH gate_status[N]  (Python dict — no I2C)
  └─ gate_status was 0, is now 1?
       AND  HH action == idle?
       AND  not printing?
       AND  no other gate currently scanning?
         └─ YES → _start_scan_mode()  →  poll timer parks at NEVER
         └─ NO  → continue normal polling
```

Initialization sets `_prev_gate_status = -1` so a cold-start with status already at 1 (from a previous session) does not trigger scan mode — only a fresh 0→1 transition fires it.

### Virtual endstop

`mmu_nfc_endstop.py` wraps a lane's existing `[nfc_gate laneN]` reader as a
Happy Hare gear-rail endstop (`ENDSTOP=nfc_lane<N>`) — no extra hardware. While
Happy Hare is homing against it, a reactor timer polls the lane's NFC reader
every `poll_interval` (default `0.05s`) and reports the endstop triggered the
instant a tag UID is read. Both scan-jog motion modes use this: the forward
search is a real Klipper homing move for the full remaining scan distance
(`_MMU_STEP_HOMING_MOVE ENDSTOP=nfc_lane<N> STOP_ON_ENDSTOP=1 MOTOR=gear`, or
the direct `mmu.move_filament(homing_move=1, endstop_name=...)` equivalent
when available), which physically stops the instant the tag is detected —
scan-jog gets the tag's real position from the homing result, not from an
estimate after a fixed-length jog.

### Scan loop

```
_scan_step_event  (stopped mode)
  └─ print started?  →  rewind and exit
  └─ homing move for the remaining scan distance, ENDSTOP=nfc_lane<N>
       (stops early the instant the virtual endstop trips)
  └─ _poll()
       └─ tag found?  →  _finish_scan()
            └─ dispatch spool to Happy Hare (already done inside _poll)
            └─ rewind to parked position, hand final parking to Happy Hare
            └─ resume poll timer
  └─ scan_mm_total >= scan_jog_max or lane Bowden length?  →  rewind and exit (no tag found)
```

`_poll()` during a scan step is identical to a normal poll — I2C read, Spoolman lookup, `GateState.process_read`, macro dispatch. The only difference is that `GateState.miss_count` does not increment on a no-read during scan (a blank read while the spool rotates is not an absence event).

`scan_motion_mode: continuous` is the default. It changes only how NFC reacts
while the homing move is in flight — tag-found actions, the 0.1 second
read-light hold, rewind, and completion logic are identical in both modes.

`scan_motion_mode: stopped` is the alternative: it waits for the homing move
to finish (or fail to trigger, at which point NFC rewinds with no tag found),
then reads once. Use this for marginal reader or tag alignment where the
in-flight continuous probing below misses the tag.

Continuous mode additionally polls the reader *while* the homing move is
still moving, building a UID hit-window (the mm range where the UID was
observed) used later to recenter before rich tag parsing:

```
_scan_step_event  (continuous mode)
  └─ print started? → rewind and exit
  └─ homing move for the remaining scan distance, ENDSTOP=nfc_lane<N> (in flight)
       └─ UID found while still moving? → keep probing, record hit-window, wait for move to finish
  └─ move complete →
       └─ Spoolman UID lookup succeeds? → existing _finish_scan()
       └─ UID unresolved and rich parsing enabled? → back up to UID hit-window center
       └─ rich payload incomplete? → retry around backed-up position
       └─ tag found → existing _finish_scan()
            └─ 0.1 second read-light hold
            └─ rewind
            └─ dispatch cached tag/spool event
  └─ scan limit reached with no tag? → rewind and exit
```

The shipped continuous config probes at 200 mm/s and 2000 mm/s^2 with a
0.05 s in-flight read cadence. During in-flight motion NFC uses a UID-only
probe and avoids rich tag parsing. Once the homing move completes, Spoolman
UID lookup runs first; if it resolves, scan-jog can finish without any rich
read. If the UID does not resolve and rich parsing is enabled, NFC backs up
to the observed UID hit-window center before rich parsing. After that
one-time recenter move, decode retry moves for incomplete rich tag reads stay
on the existing stopped/blocking retry path. Continuous mode's rewind leg
still uses a queued Happy Hare MMU-toolhead move (bypassing the public
`MMU_TEST_MOVE` G-code wrapper) rather than a homing move, since there is no
tag to home against on the way back.

### Class-level scan lock

All lane instances share one class variable, `NFCGate._active_scan_gate`. Because Klipper's reactor is single-threaded, reads and writes are atomic with respect to timer callbacks — no mutex needed. Only one gate may scan at a time; a second gate that detects a 0→1 edge while the lock is held re-arms its pending flag and retries on the next poll tick. The lock is held until a scan's rewind, Happy Hare dispatch, poll resume, and LED release have all completed — releasing it any earlier let a second `JOG_SCAN=1` start while the previous session's own Happy Hare interaction was still in flight.   If you try to start a jog_scan while another is in flight, and that attempt is blocked/queued for next tick, you may see a spool start jogging wihtout an interactive initiation.   This behavior is expected, as it's the queued scan executing once the block is lifted.   The behavinor allows the user to add spools to the mmu without having to wait for a previous jog_scan to complete.  as each laned completes, the next lane will initiate the jog_scan.

---

## System Layers

Each layer owns one responsibility and must not reach across the boundary.

| Layer | File | Owns | Does not own |
|---|---|---|---|
| **ReaderFactory** | `reader_factory.py` | Selects `PN532Driver`, `PN7160Driver`, or `RC522Driver` from `reader_type`, validates reader-specific bus defaults | Tag parsing, gate policy, Happy Hare |
| **PN532Driver** | `pn532_driver.py` | PN532 wire protocol, I2C frames, UID/page/block reads | Spoolman, gate policy, Happy Hare |
| **PN7160Driver** | `pn7160_driver.py` | PN7160/NCI protocol, Type2/Type5/MIFARE reads, RF discovery lifecycle | Spoolman, gate policy, Happy Hare |
| **RC522Driver** | `rc522_driver.py` | RC522 SPI register protocol, ISO14443A select/cascade, UID reads, NTAG/Type-2 page reads, and MIFARE Classic auth/block reads | Rich tag parsing, Spoolman, gate policy, Happy Hare |
| **SpoolmanClient** | `spoolman_client.py` | UID → spool record lookup, TTL cache, URL discovery | Gate state, lane assignment, MMU commands |
| **TagHandler** | `tag_handler.py` | Tag classification, NTAG/MIFARE capture, metadata parsing, spool resolution ladder | Gate lifecycle, polling timers, GCode dispatch |
| **GateState** | `gate_state.py` | Per-gate debounce state machine, event generation, `CurrentTag` observation | Hardware reads, Spoolman, GCode |
| **KlipperInterface** | `klipper_interface.py` | GCode macro dispatch (reactor-thread safe), macro string building | Gate state, hardware, Spoolman |
| **NFCGate / NFCGateDefaults** | `nfc_manager.py` | Config, polling lifecycle, HH seed, scan-jog coordination | Reader wire protocol, Spoolman HTTP |
| **nfc_macros.cfg** | config file | Happy Hare-facing GCode calls | NFC reads, Spoolman lookups |

---

## Macro Dispatch Events

NFC_Manager fires exactly one of these on a state change. They live in `nfc_macros.cfg` and are the only place Happy Hare commands are called.

| Macro | When | Parameters |
|---|---|---|
| `_NFC_SPOOL_CHANGED` | Tag resolved to a spool (Spoolman or metadata-direct) | `GATE`, `UID`; plus `SPOOL_ID` (Spoolman path) or `NAME`/`MATERIAL`/`COLOR`/`TEMP` (metadata path, each optional); `AUTO_CREATED=1` when spool was just created |
| `_NFC_SPOOL_REMOVED` | Tag absent for `absent_threshold` consecutive polls | `GATE` |
| `_NFC_TAG_NO_SPOOL` | Tag read but UID not registered in Spoolman | `GATE`, `UID` |

The default macro body for `_NFC_SPOOL_CHANGED` handles both paths:
```gcode
{% if params.SPOOL_ID is defined %}
    {% if params.AUTO_CREATED is defined %}
    MMU_SPOOLMAN REFRESH=1 QUIET=1
    {% endif %}
    MMU_GATE_MAP GATE={gate} SPOOLID={spool_id} AVAILABLE=1 SYNC=1 QUIET=1
{% else %}
    MMU_GATE_MAP GATE={gate} [NAME=..] [MATERIAL=..] [COLOR=..] [TEMP=..] AVAILABLE=1 QUIET=1
{% endif %}
MMU_GATE_MAP GATE={gate} APPLY=1 QUIET=1
```

You can edit `nfc_macros.cfg` to match your Happy Hare version without touching any Python.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
