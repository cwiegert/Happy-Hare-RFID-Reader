# Tag Data Resolution and Spoolman Auto-Create

**Status:** Point-in-time design snapshot  
**Branch:** woodworker-debug  
**Related:** lameandboard/rfid tag_parser reference implementation

> **Decision recorded 2026-04-29:** After completing this design, we reviewed lameandboard/rfid's `spoolman_client.py` in detail and found it already implements `auto_create_spool()`, `find_or_create_vendor()`, `find_or_create_filament()`, `build_openspool_payload()`, and SpoolmanDB density/Bambu metadata integration. Rather than porting individual methods, we decided to embed their library directly. This was enabled by relicensing the project from CC BY-NC-SA 4.0 to **GPL-3.0-or-later**, which is compatible with their GPLv3 code and consistent with the rest of the Klipper ecosystem.
>
> The design below remains valid as a specification of intended behavior and the resolution flow. Implementation will use lameandboard's client for all Spoolman CRUD operations, adapting it to our logging/debug system and retaining our `rfid_tag` UID field convention (not their `rfid_uid_N` multi-slot pattern).

---

## Requirements

1. **Feature flag required.** Full tag-data reads, payload parsing, hardware-family classification, and Spoolman auto-create must run only when explicitly enabled. The default upgrade path remains UID-only.
2. **UID-only compatibility.** When `tag_parsing: False`, the PN532 driver and manager behavior must remain compatible with the current flow: read UID, resolve UID through Spoolman, and dispatch the existing Happy Hare macros.
3. **PN532 integration support.** The PN532 driver will be extended to return target identity fields needed by the manager: UID, UID bytes, target number, ATQA/SENS_RES, SAK, and UID length.
4. **Manager chooses read strategy.** `NFCManager` must choose the tag-data read strategy from the target identity returned by the UID/anticollision read. The parser detects payload format after bytes/blocks are available; it does not choose the hardware read strategy.
5. **Read while present.** When tag parsing is enabled and a tag is detected, all configured page/block reads must happen immediately while the tag is still in front of the reader. Scan-jog, rewind, park, or later manager logic must not assume it can return to the hardware for metadata.
6. **Cache complete tag observation.** The manager must cache the full observation from the detection window: UID, target identity, raw pages or authenticated blocks when available, parser result, parse errors, and final resolution result.
7. **Conservative fallback.** If target identity is unknown, the reader does not support the needed strategy, authentication fails, parsing fails, or the tag is blank, the system falls back to UID-only resolution without disrupting the existing Spoolman path.
8. **No tag writes in this feature.** This design is read-only. Any future tag-write behavior, including writing resolved Spoolman IDs back to tags, is a separate explicitly gated feature. A future bidirectional-sync phase is an opportunity to leverage `lameandboard/rfid`'s existing write capabilities so resolved Spoolman spool IDs can be written back to writable tags. Before enabling that mode, the design should define how Spoolman and the physical tag payload stay synchronized when either side contains newer or conflicting metadata.
9. **Spoolman auto-create remains opt-in.** Parsed metadata may be used for lookup and direct Happy Hare mapping when enabled, but creating Spoolman records requires `spoolman_auto_create: True`.
10. **Keep our UID field convention.** All Spoolman UID matching and new spool creation continue to use the configured `spoolman_rfid_key`, defaulting to `rfid_tag`.

---

## Opportunity

Today the system reads only the factory UID from each NFC tag. The UID is a lookup key into Spoolman via the `rfid_tag` extra field. This is a reliable baseline, and the integration can now improve on it by using the richer metadata already present on many filament tags.

This creates two useful expansion opportunities:

1. **Tags that carry filament data** (OpenSpool NDEF JSON, Bambu MIFARE blocks, Elegoo, Creality, etc.) can provide material, color, temperature, weight, and sometimes embedded Spoolman IDs. Leveraging that data can reduce or remove manual UID registration.
2. **Spoolman-free setups** can still benefit from tag metadata. If `spoolman_url` is empty or Spoolman is unavailable, parsed filament data can be handed directly to Happy Hare.

---

## Goals

1. Parse tag payload pages or authenticated blocks to extract filament metadata and any embedded Spoolman spool ID.
2. Use embedded spool ID (when present) as the primary Spoolman key — no manual registration required for tags that carry it.
3. Fall back to existing UID-lookup path when no embedded ID is found.
4. When Spoolman is enabled but the spool is not yet there, optionally auto-create the filament and/or spool from tag metadata.
5. When Spoolman is disabled entirely, hand the parsed filament metadata directly to Happy Hare.

---

## Current State

```
tag detected
  └─ read UID only (pn532_driver)
       └─ SpoolmanClient.lookup_spool_by_uid(uid)
            ├─ match → _NFC_SPOOL_CHANGED(GATE, SPOOL_ID)
            └─ no match → _NFC_TAG_NO_SPOOL(GATE, UID)
```

Happy Hare always receives `SPOOL_ID`. Material/color is pulled from Spoolman's spool record by Happy Hare itself — we never touch it.

---

## Proposed Flow

```
tag detected
  └─ pn532_driver reads target identity
       → uid, uid_bytes, target, ATQA/SENS_RES, SAK, uid_length

       ├─ tag_parsing: False
       │    └─ UID-only resolution (current behavior)
       │
       └─ tag_parsing: True
            └─ NFCManager classifies target identity
                 ├─ NTAG / Type 2-compatible
                 │    └─ PN532 driver reads configured user pages immediately
                 ├─ MIFARE Classic-compatible
                 │    └─ lameandboard/rfid derives/selects auth keys
                 │         └─ PN532 driver authenticates sectors and reads blocks
                 └─ unknown / unsupported
                      └─ no metadata read; UID-only fallback

            └─ lameandboard/rfid parse_tag(raw_or_blocks, uid)
                 → tag metadata { uid, raw_format?, spoolman_id?,
                                  vendor?, material?, color_hex?,
                                  nozzle_temp?, bed_temp?, parse_error?, ... }

            └─ cache full observation
                 uid + target identity + raw data + parsed metadata

            ┌─────────────────────────────────────────────────┐
            │  Is Spoolman enabled?                           │
            │  (spoolman_url is non-empty and reachable)      │
            └──────────┬──────────────────────────────────────┘
                       │ Yes                       No
                       ▼                           ▼
        ┌──────────────────────────┐   ┌──────────────────────────────┐
        │ Step 1: embedded ID?     │   │ HH direct path               │
        │                          │   │ MMU_GATE_MAP with tag metadata│
        │ tag has spoolman_id?     │   │ (no Spoolman call)           │
        └──────┬───────────────────┘   └──────────────────────────────┘
               │ Yes                      No
               ▼                          ▼
  lookup by ID             Step 2: UID lookup (existing)
  GET /spool/{id}          SpoolmanClient.lookup_spool_by_uid(uid)
       │                          │
       │ found                    │ found
       └──────────┬───────────────┘
                  ▼
     _NFC_SPOOL_CHANGED(GATE, SPOOL_ID)   ← today's success path

       No match from either lookup
               ▼
        ┌──────────────────────────────┐
        │ Step 3: Auto-create enabled? │
        │ (spoolman_auto_create: True) │
        └──────┬───────────────────────┘
               │ Yes                 No
               ▼                     ▼
  Step 4: create path         _NFC_TAG_NO_SPOOL(GATE, UID)
  find or create filament          (today's no-match path)
  create spool
  → _NFC_SPOOL_CHANGED(GATE, new_spool_id)
```

When tag parsing is enabled, metadata reads are part of the tag-detection window. Resolution logic must use cached data only. It must not attempt a later page read after scan-jog rewinds or parks the spool.

---

## Read Strategy Responsibilities

The integration has three distinct responsibilities that must not be blurred:

1. **PN532 driver reads hardware.** It owns UID detection, target identity reads, NTAG/Type 2 page reads, MIFARE Classic sector authentication, and MIFARE block reads. It does not interpret filament metadata, create Spoolman records, or issue Happy Hare commands.
2. **NFCManager chooses the strategy.** It looks at the target identity returned by the UID read and chooses `ntag_type2`, `mifare_classic`, or `uid_only`. It also controls the feature flags and decides whether enhanced metadata reads are allowed.
3. **`lameandboard/rfid` supplies domain logic.** It provides parser support, Bambu/MIFARE key derivation or key-selection helpers, tag-format knowledge, and Spoolman auto-create logic. It should not directly perform Klipper I2C/SPI hardware transactions in this integration.

The required order when `tag_parsing: True` is intentionally split into two phases: the hardware-critical capture phase, then the cached-data resolution phase.

```
CAPTURE PHASE — tag must still be in front of the reader

PN532 read_target()
  → UID + ATQA/SAK/target identity
  → create/update GateState.current_tag with UID + target_info

NFCManager classify target
  → NTAG/Type2, MIFARE Classic, or UID-only fallback

If NTAG/Type2:
  PN532 read user pages immediately

If MIFARE Classic:
  adapter calls lameandboard/rfid key helper
    → sector keys or no-key reason
  PN532 authenticates sectors with those keys
  PN532 reads blocks immediately
  if key/auth/read fails → UID-only fallback

GateState.current_tag stores raw pages/blocks or fallback reason

scan-jog may continue, rewind, or park


RESOLUTION PHASE — hardware is no longer required

adapter calls lameandboard/rfid parse_tag(current_tag.raw_tag_data, uid)
  → current_tag.meta / current_tag.parse_error

resolver uses current_tag.uid + current_tag.meta
  → embedded spool_id lookup
  → UID lookup
  → optional auto-create via lameandboard/rfid Spoolman client
  → direct HH metadata path when Spoolman is disabled

current_tag.resolution records the chosen path/result

resolution uses only cached GateState.current_tag data
```

The scan-jog loop must not move the spool away from the reader between UID detection and the configured metadata read attempt. Once raw metadata has either been captured or safely fallen back to UID-only, scan-jog may continue. Parser work, Spoolman lookup, and auto-create can run from cached `current_tag` data while the gate is rewinding or after it parks, because those steps no longer need the physical tag in front of the reader.

---

## Step Details

### Step 1 — Embedded Spoolman ID

Some tag formats encode a direct Spoolman spool ID. The most common is OpenSpool JSON with a `spoolman_id` field:

```json
{"version":1,"brand":"eSUN","material":"PLA","color":"#FF5500","spoolman_id":42}
```

When the embedded parser finds this field in the NDEF payload, the adapter maps it to `current_tag.meta["spoolman_id"]`. The resolution path then calls:

```
GET /api/v1/spool/{spoolman_id}
```

A 200 response with a matching record resolves immediately — no UID scan of the spool list required. This is the fast path for pre-labeled spools.

**A missing or 404 response does not stop resolution** — fall through to Step 2.

---

### Step 2 — UID Lookup (Existing Path)

Unchanged from today. `SpoolmanClient.lookup_spool_by_uid(uid)` scans all spool records for a matching `rfid_tag` extra field value. Cache behavior is unchanged.

This step runs whenever Step 1 did not produce a confirmed match.

**Future writeback opportunity:** If UID lookup resolves a spool but the physical tag does not contain an embedded `spoolman_id`, a future bidirectional-sync feature could write the resolved spool ID back to writable tags. That would make the next read eligible for the faster embedded-ID path in Step 1 and would leverage `lameandboard/rfid`'s existing tag-write support. This is not part of the read-only integration phase and must remain behind a separate explicit writeback feature flag.

---

### Step 3 — Auto-Create Gate

Only runs when:
- `spoolman_auto_create: True` (new config key, default `False`)
- Tag data has at minimum `material` (vendor optional but improves deduplication)
- Both Steps 1 and 2 returned no match

Auto-create is intentionally opt-in. A misconfigured reader that sees random tags should not silently pollute Spoolman.

---

### Step 4 — Find or Create Filament, Then Create Spool

From `NFCManager` this should be a single adapter call into the embedded `lameandboard/rfid` Spoolman client:

```python
spool = resolver.auto_create_spool(uid=uid, tag_meta=current_tag.meta)
```

Internally, the embedded client performs the Spoolman CRUD sequence:

```
GET /api/v1/filament?vendor={vendor}&name={material}
  ├─ match found → use existing filament_id
  └─ no match → POST /api/v1/filament  (create filament)
                  → filament_id

POST /api/v1/spool
  body: { filament_id, extra: { rfid_tag: uid } }
  → new spool_id

→ _NFC_SPOOL_CHANGED(GATE, new_spool_id)
```

The NFC integration should not duplicate this create/find logic. Our adapter is responsible for translating `current_tag.meta` into the library's expected input, enforcing our configured `spoolman_rfid_key`/`rfid_tag` convention, and translating errors into our logging and console-message style.

The newly created spool gets the tag UID written into its `rfid_tag` extra field so that Step 2 resolves immediately on the next scan (no re-create).

Color, nozzle temp, bed temp, and weight are written to the filament record when present in tag data and when the filament was just created (not when reusing an existing one — we don't overwrite filament records the user may have customized).

Duplicate vendor/filament guarding is delegated to `lameandboard/rfid`'s `find_or_create_vendor()` and `find_or_create_filament()` logic. Our adapter should not add a second competing duplicate-prevention layer unless testing the vendored code exposes a specific gap.

---

### HH Direct Path (Spoolman Disabled)

When `spoolman_url` is empty, `spoolman_url: disabled`, or `spoolman_url: auto` and Moonraker reports no Spoolman URL, the system skips all Spoolman calls entirely. Tag metadata from `current_tag.meta` is passed directly into the `_NFC_SPOOL_CHANGED` macro:

```gcode
_NFC_SPOOL_CHANGED GATE=0 MATERIAL=PLA COLOR=FF5500 VENDOR=eSUN
```

The default macro sets the Happy Hare gate map directly from these values:

```gcode
MMU_GATE_MAP GATE={gate} MATERIAL={material} COLOR={color} AVAILABLE=1 QUIET=1
MMU_GATE_MAP GATE={gate} APPLY=1
```

The macro must handle both the Spoolman path (where `SPOOL_ID` is present, `MATERIAL`/`COLOR` may not be) and the direct path (where `MATERIAL`/`COLOR` are present, `SPOOL_ID` is not). Klipper GCode macros will error if a referenced variable is not passed, so the default macro uses conditional blocks:

```gcode
{% if spool_id is defined %}
    MMU_GATE_MAP GATE={gate} SPOOLID={spool_id} AVAILABLE=1 SYNC=1 QUIET=1
{% else %}
    MMU_GATE_MAP GATE={gate} MATERIAL={material|default('')} COLOR={color|default('')} AVAILABLE=1 QUIET=1
{% endif %}
MMU_GATE_MAP GATE={gate} APPLY=1
```

---

## Embedded Library — What We Use and How

We embed two files from `lameandboard/rfid` verbatim (GPL-3.0-or-later, compatible with our license):

| Source file | Destination | Used for |
|---|---|---|
| `extras/rfid_tag_parser.py` | `klippy/extras/nfc_gates/vendor/rfid_tag_parser.py` | Tag payload parsing, all 11 formats |
| `extras/spoolman_client.py` | `klippy/extras/nfc_gates/vendor/lameandboard_spoolman.py` | Vendor/filament/spool CRUD building blocks |

### `rfid_tag_parser.py` — drop-in, no adaptation needed

Standard library only (+ optional `pycryptodome` for Bambu, `cbor2` for OpenPrintTag). No Klipper dependencies. Entry point:

```python
from .vendor.rfid_tag_parser import parse_tag, is_parse_error

info = parse_tag(raw_bytes_or_blocks, uid_hex=uid)
```

`raw_bytes_or_blocks` is either raw NTAG user-memory bytes or an authenticated MIFARE Classic block dict. Returns a metadata dict or `None`. Logger name `rfid.rfid_tag_parser` routes through standard Python logging — no change needed.

### `lameandboard_spoolman.py` — use building blocks, not the top-level `auto_create_spool()`

Their `auto_create_spool()` hard-codes the `rfid_uid_N` multi-slot UID convention. We do not use it. Instead, our adapter calls their lower-level methods directly and controls the UID field name:

```python
from .vendor.lameandboard_spoolman import SpoolmanClient as LBSpoolmanClient

lb = LBSpoolmanClient(base_url=our_resolved_url, timeout=self._timeout)

vendor_id   = lb.find_or_create_vendor(vendor_name)
filament_id = lb.find_or_create_filament(name=material, vendor_id=vendor_id, ...)
spool       = lb.create_spool(
    filament_id=filament_id,
    remaining_weight=weight_g,
    extra={self._rfid_key: uid}   # our rfid_tag convention, not their rfid_uid_N
)
```

`our_resolved_url` comes from our existing `SpoolmanClient._resolve_base_url()` so Moonraker auto-discovery and URL normalization stay in one place.

Their `build_openspool_payload(spool_dict)` is available for future tag write-back but is not called in this read-only phase.

### What stays in our `spoolman_client.py`

Our client keeps everything it already has:
- Moonraker URL auto-discovery
- Circuit breaker
- In-memory UID cache with TTL
- `lookup_spool_by_uid()` — UID-to-spool resolution
- `lookup_spool_by_id()` — direct spool ID fetch (expose existing `_fetch_spool_detail` as public)
- `update_spool_location()` / `clear_spool_location()`

It does not gain vendor/filament/spool creation — those come from the embedded client.

### Adapter responsibilities

Our adapter code (in `nfc_manager.py` or a thin `tag_resolver.py`) owns NFC-Gates-specific behavior:

- translating `parse_tag()` output into `current_tag.meta`
- passing `our_resolved_url` to the embedded client so both clients share one URL-resolution result
- enforcing `spoolman_rfid_key` / `rfid_tag` on every Spoolman write
- translating embedded client errors into our debug/logging system
- keeping all of this behind `tag_parsing` and `spoolman_auto_create` flags

### GateState Refactor

Do not introduce a competing tag model unless implementation proves it is needed. Today, `GateState` already tracks the live tag identity and owns the debounce/event logic:

```python
current_uid
current_spool
process_read(uid_hex, spool_id, scan_mode=False)
```

The integration can refactor `GateState` before parser support is enabled, as long as the existing behavior and tests remain unchanged. `GateState` should remain the gate debounce/event state machine, and a new `current_tag` field should hold the richer tag observation used by future metadata reads:

```python
current_uid              # compatibility alias / shortcut for current_tag.uid
current_spool            # compatibility alias / shortcut for current_tag.spool_id
current_tag              # rich tag observation, or None
```

`current_tag` should be populated from the first UID read today, and later enriched by the second metadata read when `tag_parsing: True`:

```python
current_tag.uid              # factory UID
current_tag.spool_id         # resolved Spoolman ID, or None
current_tag.target_info      # PN532 target identity: ATQA/SAK/UID length/etc.
current_tag.raw_tag_data     # raw pages or authenticated blocks when captured
current_tag.meta             # adapted lameandboard/rfid metadata dict
current_tag.parse_error      # parser/auth/read error summary when applicable
current_tag.resolution       # final resolution path/result for debugging
```

With `tag_parsing: False`, `current_tag` only needs UID and resolved spool ID, matching today's behavior. Future metadata fields are additive; existing UID-only logic can ignore them.

`GateState` should remain a normal behavior-owning class, not a dataclass. It owns `process_read()`, removal debounce, event generation, and state transitions.

`current_tag` should be implemented as a dataclass-style data holder, for example `CurrentTag` or `TagObservation`. It should contain structured tag data only and no gate debounce behavior.

During the transition, keep `current_uid` and `current_spool` as compatibility fields or compatibility properties so existing code can continue to read and write them safely. `process_read()` should keep those fields and `current_tag` synchronized. Existing tests must continue to pass before any parsing capability is enabled.

### Parser Adapter

The adapter calls `lameandboard/rfid`'s parser directly:

```python
info = lameandboard_rfid.parse_tag(raw_or_blocks, uid_hex=uid)
tag_meta = adapt_lameandboard_info(uid, info)
```

`raw_or_blocks` is either raw NTAG/Type 2 user memory bytes, or an authenticated MIFARE Classic block dictionary compatible with `lameandboard/rfid`'s `parse_tag()` input. The adapter returns a metadata dict that can be stored on `current_tag.meta`.

**Format detection order:**

| Priority | Format | Detection |
|---|---|---|
| 1 | Authenticated MIFARE Classic blocks | Bambu, Creality CFS, QIDI, or other block-based formats when authenticated data is available |
| 2 | Binary NTAG/Type 2 formats | ELEGOO, Anycubic ACE, and similar raw user-memory layouts |
| 3 | NDEF MIME formats | OpenTag3D, OpenPrintTag, OpenSpool JSON |
| 4 | NDEF text/URI formats | SimplyPrint/QIDI URL, generic JSON, query params |
| 5 | Blank / unwritten tag | Empty data, all zeros, or empty NDEF wrapper returns metadata with only `uid` populated |

The embedded parser identifies the payload format after the reader has already supplied bytes or authenticated blocks. It does not decide whether the physical tag should be read as NTAG pages or MIFARE Classic blocks; that decision belongs to `NFCManager` and the reader driver.

**Blank tag behavior:** A factory-fresh NTAG tag has no user data — pages 4–N are 0x00 or an empty NDEF message (`0x03 0x00 0xFE`). Blank or unrecognized parser output is adapted into metadata with only `uid` populated. Resolution then proceeds on UID alone, which is identical to today's UID-only path:

- UID registered in Spoolman → resolves normally
- UID not registered, auto-create off → `_NFC_TAG_NO_SPOOL` (same message as today)
- UID not registered, auto-create on → no material data available, cannot create; falls through to `_NFC_TAG_NO_SPOOL`

The only overhead compared to today is reading empty pages before concluding there is nothing on them. This is unavoidable — the system cannot know a tag is blank without reading it.

### When parsing runs

Parsing is enabled when `tag_parsing: True` (default `False` initially). When disabled the driver returns UID only and the existing flow is unchanged.

When `tag_parsing: True`, the detection pass collects UID and target identity first, then immediately performs the selected metadata read while the same tag is still present. The manager caches the complete observation before scan-jog moves the spool.

- **Known UID** — metadata is still captured and cached when tag parsing is enabled, so the manager has a complete tag observation for debugging and future decision logic. Existing UID resolution may still win.
- **Unknown UID** — metadata is parsed and used for embedded ID lookup, direct Happy Hare metadata, or optional auto-create.
- **Unsupported tag type** — no metadata read is attempted; UID-only resolution continues.

The page/block read cost is paid only when `tag_parsing: True`. This is intentional: once scan-jog advances, the tag may no longer be in front of the reader, so metadata cannot be deferred safely.

---

## Configuration Changes

### `nfc_reader.cfg` additions

```ini
[nfc_gate]
# Tag data parsing
tag_parsing:          False   ; True = read tag metadata pages/blocks and parse
                              ; False = UID only (current behavior, default)

# Spoolman auto-create (only active when tag_parsing: True and Spoolman enabled)
spoolman_auto_create: False   ; True = create filament/spool in Spoolman when
                              ; no existing match is found for a tag
```

`tag_parsing` and `spoolman_auto_create` can be overridden per lane in `nfc_reader_hw.cfg`.

---

## Changes to Existing Code

### `pn532_driver.py`

- Keep `read_tag()` as the UID-only compatibility method.
- Expose `read_target()` to the manager as the enhanced detection method when `tag_parsing: True`. It returns UID, UID bytes, target number, ATQA/SENS_RES, SAK, and UID length.
- Add or harden raw NTAG/Type 2 user-memory reads, using sequential `READ` commands up to `tag_max_pages`.
- Add MIFARE Classic sector authentication and block-read support as a separate reader capability. The driver receives keys from the manager/adapter and uses those keys for `InDataExchange` auth/read commands; it does not derive the keys itself. If unavailable, MIFARE Classic tags fall back to UID-only resolution with a clear debug/warning message.
- Pages/blocks and UID must be collected during the same tag presence. No later hardware read is part of the resolution ladder.

### Logging conventions

All new code in this integration must follow the existing debug level conventions:

| Level | When to use |
|---|---|
| `logger.error(...)` | Unexpected exceptions — parse_tag raised, vendor import failed |
| `logger.warning(...)` | Recoverable failures — NTAG read failed, empty read, auth failed, fallback taken |
| `logger.info(...)` at `debug >= 3` | State changes visible to the user — UID read, parse result summary, Spoolman lookup result, resolution path taken |
| `logger.debug(...)` at `debug >= 4` | Full detail — raw metadata dict, page-level read trace, every resolution step |

Console output (`self._console(...)`) is reserved for user-facing events (spool found, tag not registered). It must not be used for internal resolution steps.

---

### `nfc_manager.py`

- `NFCGate._poll_tag()` keeps the current UID-only call when `tag_parsing: False`.
- When `tag_parsing: True`, `NFCGate._poll_tag()` calls `read_target()` and passes target identity to the manager's classifier.
- New helper `_classify_tag_target(target_info)` maps ATQA/SAK/UID length to a conservative strategy: `ntag_type2`, `mifare_classic`, or `uid_only`.
- New helper `_resolve_auth_keys(current_tag)` asks the adapter/library for required MIFARE keys when the selected strategy needs authentication. This is a pre-read call to `lameandboard/rfid`. Failure to resolve keys is not fatal; it records the reason and falls back to UID-only resolution.
- The selected strategy performs the metadata read immediately and updates `GateState.current_tag` with UID, target identity, raw pages or authenticated blocks, and any read/auth failure reason.
- After the raw capture phase, a second adapter call invokes `lameandboard/rfid.parse_tag()` using cached `current_tag.raw_tag_data`; this fills `current_tag.meta` or `current_tag.parse_error`.
- Resolution runs from cached `GateState.current_tag`; it must not ask the reader for metadata later.
- New helper `_spoolman_enabled()` — returns `True` when `spoolman_url` is non-empty and not `disabled`.
- `_dispatch_spool_changed()` accepts optional tag metadata so metadata params can be forwarded to the macro when Spoolman is disabled.

### `spoolman_client.py`

- No new CRUD methods added here. Creation is delegated to the embedded `lameandboard_spoolman.py`.
- Expose `_fetch_spool_detail` as public `lookup_spool_by_id(spool_id)` — needed for Step 1 (embedded ID resolution).
- Continue to own URL resolution, circuit breaker, cache, and UID lookup. The resolved base URL is shared with the embedded client at call time so both use the same discovered URL.

### `vendor/lameandboard_spoolman.py` (vendored verbatim)

- Copied from `lameandboard/rfid extras/spoolman_client.py` at a pinned source commit into `klippy/extras/nfc_gates/vendor/`.
- Used only for `find_or_create_vendor()`, `find_or_create_filament()`, and `create_spool()`.
- `auto_create_spool()` is present in the file but **not called** — it encodes the `rfid_uid_N` convention we don't use.
- Constructor receives `base_url` from our client's `_resolve_base_url()`.

### `vendor/rfid_tag_parser.py` (vendored verbatim)

- Copied from `lameandboard/rfid extras/rfid_tag_parser.py` at a pinned source commit into `klippy/extras/nfc_gates/vendor/`.
- No changes needed. `parse_tag(raw, uid_hex)` is called directly from the manager adapter.

---

## Resolution Result Summary

| Scenario | Spoolman call | HH receives |
|---|---|---|
| Spoolman disabled, tag has metadata | None | `SPOOL_ID=-1 MATERIAL=X COLOR=Y` |
| Spoolman disabled, UID only | None | nothing (existing `_NFC_TAG_NO_SPOOL`) |
| Spoolman enabled, embedded ID resolves | GET /spool/{id} | `SPOOL_ID=N` |
| Spoolman enabled, UID resolves | GET /spool list | `SPOOL_ID=N` |
| Spoolman enabled, no match, auto-create off | None | `_NFC_TAG_NO_SPOOL` |
| Spoolman enabled, no match, auto-create on, has metadata | POST filament + spool | `SPOOL_ID=new_N` |
| Spoolman enabled, no match, auto-create on, UID only | None | `_NFC_TAG_NO_SPOOL` |

---

## Open Questions

1. **Target classification table** — define the exact conservative ATQA/SAK/UID-length table used to select `ntag_type2`, `mifare_classic`, or `uid_only`. Unknown combinations must fall back to UID-only. **Not yet implemented** — `_classify_tag_target()` currently returns `ntag_type2` for all tags. Bambu MIFARE Classic support is gated on this table plus key derivation via `lameandboard/rfid`. Search `TODO: MIFARE Classic classification not implemented` in `nfc_manager.py`.

---

## Implementation Order

1. Copy `rfid_tag_parser.py` and `spoolman_client.py` from `lameandboard/rfid` into `klippy/extras/nfc_gates/vendor/` as `rfid_tag_parser.py` and `lameandboard_spoolman.py`. Add an empty `vendor/__init__.py`. Record the source commit SHA in a `VENDORED.md` at repo root. Both files carry their original GPLv3 headers.
2. Extend `GateState` with `current_tag` and add the parser adapter around `lameandboard/rfid.parse_tag()`.
3. Extend `pn532_driver.py` so the manager can request target identity and raw NTAG/Type 2 memory reads behind `tag_parsing`.
4. Add conservative target classification in `nfc_manager.py`: NTAG/Type 2, MIFARE Classic, or UID-only fallback.
5. Add PN532 MIFARE Classic authenticated block-read support as a gated capability. If this misses the first implementation pass, MIFARE tags remain UID-only.
6. Embed/wrap `lameandboard/rfid` Spoolman client for direct ID lookup and auto-create while preserving `rfid_tag`.
7. Implement NFCManager's spool resolution process using cached `current_tag` data only. The manager should try the resolution paths in this order:
   - If `current_tag.meta` contains an embedded Spoolman spool ID, look up that spool ID first.
   - If no spool was resolved by embedded ID, look up the tag UID using the existing `rfid_tag`/`spoolman_rfid_key` path.
   - If Spoolman is enabled, no existing spool was found, `spoolman_auto_create` is enabled, and the tag metadata has enough filament information, call the `lameandboard/rfid` auto-create path.
   - If Spoolman is disabled but tag metadata has material/color/vendor information, dispatch that metadata directly to Happy Hare.
   - If none of the above resolves a spool or usable metadata, fall back to the existing UID-only no-spool behavior.
8. Add config keys: `tag_parsing`, `tag_max_pages`, and `spoolman_auto_create`.
9. Update macros and docs for direct metadata dispatch when Spoolman is disabled.
10. Add tests for UID-only compatibility, NTAG page reads, parser adapter, target classification, auto-create gating, and UID-only fallback.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
