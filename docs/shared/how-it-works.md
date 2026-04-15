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
  └─ initialises PN532 reader
  └─ reads Happy Hare gate map  ← seeds local lane cache from HH state
  └─ starts background polling  (if startup_polling: 1)
```

### Why seed from Happy Hare?

After a Klipper restart the NFC lane cache is empty — but Happy Hare already knows which spool is in each gate from its own persisted state. Without seeding, the first poll would detect the tag, resolve it through Spoolman, and fire `_NFC_SPOOL_CHANGED` — redundantly telling Happy Hare something it already knows. With seeding, that first poll is absorbed silently.

If the physical spool was swapped while Klipper was down, the resolved spool_id won't match the seed, and `_NFC_SPOOL_CHANGED` dispatches normally.

### Startup console output

```
✅ NFC[lane0]: reader ready.  HH seed: spool_id=42  Startup polling is enabled; first poll in 0.0s.
✅ NFC[lane1]: reader ready.  HH reports gate empty  Run NFC_GATE GATE=1 READ=1 to start polling.
```

The seed is one-shot — it fires at most once per lane per boot, on the first `CHANGED` event. If Happy Hare wasn't ready when the NFC init ran, the seed step is skipped and a manual `NFC_HH_SYNC_CACHE` re-syncs all lanes.

---

## Per-Poll Flow

Tags are never written to. The NFC tag's factory UID is stored as a Spoolman extra field (`rfid_tag` by default). On every poll:

```
PN532 reads tag UID (or detects absence)
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

## System Layers

Each layer owns one responsibility and must not reach across the boundary.

| Layer | File | Owns | Does not own |
|---|---|---|---|
| **PN532Driver** | `pn532_driver.py` | PN532 wire protocol, I2C frames, UID extraction | Spoolman, gate policy, Happy Hare |
| **SpoolmanClient** | `spoolman_client.py` | UID → spool record lookup and TTL cache | Gate state, lane assignment, MMU commands |
| **NFC_Manager** | `NFC_manager.py` | Gate state machine, change/remove decisions, macro dispatch, HH seed | PN532 protocol details, Spoolman HTTP |
| **nfc_macros.cfg** | config file | Happy Hare-facing GCode calls | NFC reads, Spoolman lookups |

---

## Macro Dispatch Events

NFC_Manager fires exactly one of these on a state change. They live in `nfc_macros.cfg` and are the only place Happy Hare commands are called.

| Macro | When | Parameters |
|---|---|---|
| `_NFC_SPOOL_CHANGED` | New tag UID resolves to a Spoolman spool | `GATE`, `SPOOL_ID`, `UID` |
| `_NFC_SPOOL_REMOVED` | Tag absent for `absent_threshold` consecutive polls | `GATE` |
| `_NFC_TAG_NO_SPOOL` | Tag read but UID not registered in Spoolman | `GATE`, `UID` |

The default macro body for `_NFC_SPOOL_CHANGED`:
```gcode
MMU_GATE_MAP GATE={gate} SPOOLID={spool_id} AVAILABLE=1 SYNC=1 QUIET=1
MMU_GATE_MAP GATE={gate} APPLY=1
```

You can edit `nfc_macros.cfg` to match your Happy Hare version without touching any Python.
