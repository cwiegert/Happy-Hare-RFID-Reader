# Vendor Integration — Logic Model

**Branch:** `lameandboard---integration`  
**Implemented:** 2026-04-30  
**Related:** [tag-data-resolution.md](tag-data-resolution.md), [VENDORED.md](../../VENDORED.md)

This document describes how the `lameandboard/rfid` library is integrated — what it provides, how it is called, and how data flows from a physical NFC tag through to Happy Hare.

---

## What the vendor library provides

Two files are vendored verbatim from `lameandboard/rfid` into `klippy/extras/nfc_gates/vendor/`:

| File | Purpose |
|---|---|
| `rfid_tag_parser.py` | Parses raw NTAG/MIFARE bytes into a filament metadata dict. Supports 11+ tag formats (OpenSpool, Bambu, Elegoo, Creality, Anycubic, etc.) |
| `lameandboard_spoolman.py` | Spoolman CRUD client — `find_or_create_vendor()`, `find_or_create_filament()`, `create_spool()` |

Both files carry their original GPLv3 headers. Neither is modified. The source commit is pinned in `VENDORED.md`.

---

## How the library is loaded

Imports are lazy and wrapped in `try/except` inside `tag_handler.py`. The vendor package is on `sys.path` via a path fix applied at module import time so Klipper's sandboxed extras loader can find it.

```
klippy/extras/nfc_gates/
├── vendor/
│   ├── __init__.py
│   ├── rfid_tag_parser.py       ← vendored, parse_tag() entry point
│   └── lameandboard_spoolman.py ← vendored, Spoolman CRUD entry points
├── tag_handler.py               ← adapter layer: tag classification, hardware capture, resolution ladder
├── gate_state.py                ← per-gate debounce state machine, CurrentTag, DIRECT_METADATA_SPOOL
├── klipper_interface.py         ← reactor-thread GCode macro dispatcher
├── nfc_manager.py               ← config, polling lifecycle, get_status()
└── spoolman_client.py           ← our UID cache/circuit-breaker client
```

Import pattern in `tag_handler.py`:

```python
try:
    from .vendor.rfid_tag_parser import parse_tag, is_parse_error
    from .vendor.lameandboard_spoolman import SpoolmanClient as LBSpoolmanClient
    _VENDOR_AVAILABLE = True
except ImportError:
    _VENDOR_AVAILABLE = False
```

If the vendor files are absent, `tag_parsing` falls back to UID-only mode. Bambu MIFARE reads additionally require `pycryptodome` in the Klipper Python venv (installed by `install.sh`).

---

## Config flags

| Key | Default | Effect |
|---|---|---|
| `tag_parsing` | `False` | Enable full tag-content reads and metadata parsing |
| `bambu_reads` | `False` | Enable authenticated MIFARE reads for Bambu factory spools (requires `pycryptodome`) |
| `spoolman_auto_create` | `False` | Allow the plugin to create filament/spool records in Spoolman from tag metadata |

`tag_parsing: False` keeps the original UID-only behavior. All vendor calls are gated on this flag.

A warning is logged at init if `bambu_reads: True` with `tag_parsing: False` — the MIFARE path is never reached in that combination.

---

## Tag parsing pipeline

When `tag_parsing: True` and a tag is detected:

```
PN532 reads target identity
  → UID + ATQA + SAK + uid_length

NFCManager classifies target
  ├─ SAK & 0x08 → MIFARE Classic  (Bambu, Creality CFS, QIDI)
  ├─ SAK == 0x00 → NTAG / Type-2  (OpenSpool, Elegoo, Anycubic, generic)
  └─ unknown → UID-only fallback

NTAG/Type-2 path:
  PN532 reads user pages 4 → tag_max_pages (default 16, covers NTAG213/215/216)
  → raw bytes stored in current_tag.raw_tag_data

MIFARE Classic path (bambu_reads: True only):
  vendor key helper derives or selects Bambu sector keys
  PN532 authenticates sectors and reads blocks
  → block dict stored in current_tag.raw_tag_data
  auth/read failure → UID-only fallback

Both paths complete while tag is physically present.
Scan-jog must not move the spool between UID detection and raw read completion.

parse_tag(current_tag.raw_tag_data, uid_hex=uid)
  → metadata dict: { material, color_hex, min_temp, max_temp,
                     bed_temp, spoolman_id?, brand?, ... }
  stored in current_tag.meta

Parser runs from cached data — hardware no longer required.
```

The metadata dict field names are the vendor library's output names. The adapter in `tag_handler.py` reads them by key and does not transform them before storing on `current_tag.meta`.

---

## Resolution ladder

After parse, `NFCGate._resolve_spool(uid)` works through these steps in order:

```
Step 1 — Embedded Spoolman ID
  current_tag.meta has 'spoolman_id'?
  → GET /api/v1/spool/{spoolman_id}
  → found: use this spool_id  ✓
  → 404 or missing: continue

Step 2 — UID lookup (existing path, always runs)
  SpoolmanClient.lookup_spool_by_uid(uid)
  → found: use this spool_id  ✓
  → not found: continue

Step 3 — Auto-create (spoolman_auto_create: True only)
  tag has 'material' in meta?
  → LBSpoolmanClient.find_or_create_vendor(brand)
  → LBSpoolmanClient.find_or_create_filament(vendor_id, material, color, temp)
  → LBSpoolmanClient.create_spool(filament_id)
  → SpoolmanClient.set_spool_uid(new_spool_id, uid)   ← writes our rfid_tag key
  → SpoolmanClient.clear_cache()                       ← force re-poll on next scan
  → use new_spool_id  ✓
  → failed: continue

Step 4 — Spoolman disabled, metadata available  (DIRECT_METADATA_SPOOL sentinel)
  spoolman_url empty/disabled AND meta has material or color
  → return DIRECT_METADATA_SPOOL sentinel
  → KlipperInterface dispatches NAME/MATERIAL/COLOR/TEMP directly to macro
  → no spool_id assigned

Step 5 — UID-only fallback
  → _NFC_TAG_NO_SPOOL(GATE, UID)
```

`current_tag.resolution` records which step succeeded:

| Resolution | Meaning |
|---|---|
| `embedded_id` | Spoolman spool ID was in the tag payload |
| `uid_lookup` | Matched by UID in Spoolman extra field |
| `auto_create` | New spool created in Spoolman from tag metadata |
| `metadata_direct` | Spoolman disabled; metadata sent directly to HH |
| `unresolved` | UID not found in Spoolman, no auto-create |

---

## DIRECT_METADATA_SPOOL sentinel

When Spoolman is disabled and the tag carries metadata, there is no spool ID to assign. Rather than returning `None` (which means "no tag"), the resolver returns the module-level sentinel object `DIRECT_METADATA_SPOOL`.

`GateState` stores this on `current_spool`. All downstream code that checks `current_spool is not None` correctly sees it as "something is assigned". `get_status()` recognises the sentinel and returns `spool_id: -1` with `resolution: 'metadata_direct'`.

---

## KlipperInterface dispatch

`KlipperInterface.dispatch()` (adapter between the polling loop and Klipper GCode) calls `_run_gcode()` which builds the macro invocation string:

**Spoolman path** (`spool_id` is an integer):

```
_NFC_SPOOL_CHANGED GATE=0 SPOOL_ID=26 UID=04AABBCC
```

**Metadata-direct path** (`spool_id is None`, meta dict present):

```
_NFC_SPOOL_CHANGED GATE=0 NAME=eSUN_PLA MATERIAL=PLA COLOR=#FF5500 TEMP=220 UID=04AABBCC
```

Rules applied to the metadata path:
- `NAME` uses `material_detail` or `material`, prefixed with brand/vendor/tag format when present
- `MATERIAL` and `COLOR` are only included when non-empty (omitting an empty param avoids broken GCode)
- `TEMP` uses `meta['min_temp']` (lower bound of the print window = recommended extruder temp), only included when present
- `VENDOR` is not passed — HH has no `VENDOR` param; vendor/brand display comes from HH's own Spoolman enrichment when a spool ID is set
- Special characters in string values are sanitised via `_macro_value()`: spaces → underscores, non-alphanumeric/`#`/`.`/`_` stripped

---

## Happy Hare macro

`_NFC_SPOOL_CHANGED` in `nfc_macros.cfg` branches on whether `SPOOL_ID` was passed:

```jinja
{% if params.SPOOL_ID is defined %}
    MMU_GATE_MAP GATE={gate} SPOOLID={spool_id} AVAILABLE=1 SYNC=1 QUIET=1
{% else %}
    MMU_GATE_MAP GATE={gate}
      {% if name     %} NAME={name}{% endif %}
      {% if material %} MATERIAL={material}{% endif %}
      {% if color    %} COLOR={color}{% endif %}
      {% if temp     %} TEMP={temp | int}{% endif %}
      AVAILABLE=1 QUIET=1
{% endif %}
MMU_GATE_MAP GATE={gate} APPLY=1 QUIET=1
```

- `SYNC=1` on the Spoolman path pushes the spool assignment back to Spoolman so the spool's location field stays current
- `APPLY=1` on a second call activates the gate (HH loads the filament profile)
- NAME/MATERIAL/COLOR/TEMP are each omitted from the MMU_GATE_MAP call when not present in params

---

## Spoolman clients — two clients, one URL

There are two Spoolman HTTP clients in play:

| Client | File | Owns |
|---|---|---|
| Our client | `spoolman_client.py` | URL auto-discovery (Moonraker), circuit breaker, UID cache, `lookup_spool_by_uid()`, `lookup_spool_by_id()`, `set_spool_uid()` |
| Vendor client | `lameandboard_spoolman.py` | `find_or_create_vendor()`, `find_or_create_filament()`, `create_spool()` |

The vendor client is instantiated only during auto-create, receiving the URL that our client already resolved. Both clients therefore use the same Moonraker-discovered URL. Our client does not gain vendor/filament/spool creation logic; the vendor client does not know about our UID cache or circuit breaker.

---

## get_status() fields

`NFCGate.get_status()` exposes gate state to Moonraker/Fluidd/macros:

| Field | Type | Description |
|---|---|---|
| `gate` | int | Gate index |
| `tag_present` | bool | True when a tag UID is currently on the gate |
| `spool_id` | int | Resolved Spoolman spool ID, or -1 when no spool assigned |
| `uid` | str | Current tag UID hex string, or '' |
| `failed` | bool | True when the PN532 hardware has entered a failed state |
| `resolution` | str | Internal path taken (debug/diagnostic use; consumers should use `tag_present` and `spool_id`) |

The three meaningful states a consumer should branch on:

| `tag_present` | `spool_id` | State |
|---|---|---|
| `False` | `-1` | Empty gate — no tag |
| `True` | `-1` | Tag present, no Spoolman spool (metadata-direct or uid-only) |
| `True` | `N` | Tag resolved to spool N |

---

## pycryptodome dependency

Bambu MIFARE reads use HKDF key derivation, which requires `pycryptodome`. `install.sh` targets the Klipper Python venv (`~/klippy-env/` or `/home/*/klippy-env/`) so the library is available in the same environment where `tag_handler.py` runs. `uninstall.sh` removes it from the same venv.

`bambu_reads: False` (default) skips all MIFARE authentication. `pycryptodome` is not required when Bambu reads are disabled.

---

## Updating the vendored library

See `VENDORED.md` for the pinned source commit and manual update instructions. A GitHub Action runs weekly and opens a PR automatically when upstream changes either vendored file. Review any diff against `tag_handler.py` adapter code before merging — the adapter keys into specific field names from the parser output.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html).*
