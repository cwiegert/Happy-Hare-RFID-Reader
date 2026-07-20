# Spoolman Integration

[← README](../../Readme.md) | [Commands & Macros →](klipper-functions.md)

The NFC reader links a physical spool to a Spoolman record using the tag's factory UID — a unique identifier that every NFC tag ships with. Tags are never written to.

The link is:
```
NFC tag UID  ←→  rfid_tag field on the Spoolman spool record
```

When a reader sees a tag, it sends the UID to Spoolman's API, finds the matching spool record, and passes the spool ID to Happy Hare.

---

## Step 1 — Create the Extra Field in Spoolman

Before registering any tags, Spoolman needs to know the `rfid_tag` field exists.

1. Open Spoolman in your browser
2. Go to **Settings → Extra Fields**
3. Click **Add extra field**
4. Fill in:
   - **Entity:** `Spool`
   - **Name:** `rfid_tag`
   - **Field type:** `Text`
5. Save

> [!IMPORTANT]
> The field name must exactly match `spoolman_rfid_key` in `nfc_reader.cfg`. Both default to `rfid_tag`. If you use a different name, change both places.

---

## Step 2 — Get a Tag's UID

You need the UID of each NFC tag before you can register it. There are a few ways to get it:

**From the Klipper console (easiest):**

Hold the tag near the reader and run:
```gcode
NFC GATE=0 SCAN=1
```
The UID prints in the console.

**From a phone:**
Any NFC reader app on Android or iOS can read the UID. The UID is factory-programmed and identical regardless of which reader reads it.   NFCTools is a good option

---

## Step 3 — Register the Tag in Spoolman

1. Open the spool record in Spoolman
2. Find the `rfid_tag` extra field
3. Paste in the UID
4. Save

**UID format doesn't matter.** These all register as the same UID:
```
04AABBCCDD
04:AA:BB:CC:DD
04-AA-BB-CC-DD
04 AA BB CC DD
```

The system normalizes everything to uppercase hex before comparing.

---

## Step 4 — Test the Lookup

With the registered spool loaded on a gate, run a full poll:

```gcode
NFC GATE=0 POLL=1
```

**Success:**
```
NFC gate 0: spool 42 detected (UID 04AABBCCDD). Sending to Happy Hare.
```

**Tag found, but not registered:**
```
[ERROR] NFC gate 0: tag UID 04AABBCCDD is not registered in Spoolman.
Open the spool record in Spoolman, set the 'rfid_tag' extra field to: 04AABBCCDD
```

Copy the UID directly from the console message and paste it into Spoolman — this avoids transcription errors.

---

## How Lookup Works

Each time a new UID appears at a gate:

1. SpoolmanClient checks its in-memory cache for that UID
2. **Cache hit** → returns the cached spool ID, no HTTP request
3. **Cache miss** → queries `GET /api/v1/spool` and scans all records for a matching `rfid_tag` value
4. **Match found** → spool ID goes to NFC_Manager, which updates gate state and fires `_NFC_SPOOL_CHANGED`
5. **No match** → fires `_NFC_TAG_NO_SPOOL` with the unrecognized UID

Cache lifetime is controlled by `spoolman_cache_ttl` (default: 300 seconds). Set to `0` to disable caching and re-query Spoolman on every poll.

After a UID resolves, NFC dispatches `_NFC_SPOOL_CHANGED`; the default macro updates Happy Hare with `MMU_GATE_MAP ... SYNC=1` and then calls `MMU_SPOOLMAN SYNC=1 QUIET=1`. Happy Hare owns the Spoolman location synchronization.

---

## Dropout & Build-up logic (UID → Spool → Filament → Vendor)

This section describes the cascade the software follows when a UID is read and how missing entities are handled.

Dropout logic (what happens when something is missing)

- UID present but no Spoolman match
  - If `spoolman_auto_create: False` → the system falls back to the UID-only path and fires `_NFC_TAG_NO_SPOOL`. The gate remains `AVAILABLE=1` and Happy Hare will treat the lane as loaded (no `SPOOL_ID` set).
  - If `spoolman_auto_create: True` and `tag_parsing: True` but the tag metadata is insufficient (no `material`) → auto-create is skipped and the read is unresolved.
- Spool created but UID write fails
  - After auto-creation, the adapter attempts to write the UID into the configured Spoolman extra field (`spoolman_rfid_key`). If that HTTP patch fails, the read is treated as unresolved to avoid losing the authoritative UID→spool link.
- Auto-create failure
  - If the tag metadata is incomplete, or if Spoolman cannot accept the new spool record, auto-create aborts and the read is treated as unresolved.

Build-up logic (auto-create flow when metadata is present)

- Preconditions:
  - `tag_parsing: True`
  - `spoolman_auto_create: True`
  - Tag parser returned a `meta` dict containing at minimum `material` (e.g. "PLA"). Additional helpful fields: `brand`/`vendor`, `color_hex`, `min_temp`, `max_temp`, `bed_temp`, `diameter`.

- Decision/build tree:
  1. If the tag carries an embedded `spoolman_id`, that ID is checked first.
     - If the spool exists, it is used directly.
     - If the spool ID is invalid, the flow continues to UID lookup and then auto-create.
  2. If no spool ID is embedded, the system looks up the UID in `rfid_tag`.
     - If the UID is already attached to a spool, that spool is used.
     - If the UID is not found, auto-create begins.
  3. During auto-create, the system determines whether a matching vendor and filament/profile already exist:
     - If `brand`/`vendor` is present, the existing vendor is used when possible; otherwise a new vendor entry may be created.
     - If a filament/profile matching the parsed metadata exists for that vendor, it is reused; otherwise a new filament/profile is created and the vendor is assigned on that filament.
     - Finally, the spool record is created for that filament/profile if needed.
  4. After the spool record exists, the UID is written into the configured `spoolman_rfid_key` extra field on that spool.

- Practical result:
  - Existing spool by UID → no create needed.
  - Existing spool by embedded ID → no create needed.
  - Missing spool but enough metadata → new spool record is created.
  - Missing filament/profile metadata → auto-create is skipped and the read is unresolved.

- On success: the new or existing spool ID is used and `_NFC_SPOOL_CHANGED` is fired with `SPOOL_ID=<new_id>`.
- On any failure: auto-create aborts, the read is treated as unresolved, and the console/logs show the error. Inspect verbose logs to see the exact HTTP response.

Notes:
- Auto-create uses the same Spoolman URL resolved by your configuration.
- The system always writes the UID to the configured `spoolman_rfid_key` field after creating the spool so later reads resolve by UID.

---

## Spoolman URL Configuration

**Automatic (recommended):**
```ini
spoolman_url: auto
```
Reads the Spoolman URL from Moonraker. Works when `moonraker.conf` has a `[spoolman]` section:
```ini
# in moonraker.conf
[spoolman]
server: http://192.168.1.50:7912
```

**Direct URL:**
```ini
spoolman_url: http://192.168.1.50:7912
```
Use this when Moonraker doesn't have Spoolman configured, or when `auto` is failing.

---

## Troubleshooting Lookup Failures

| Symptom | Check |
|---|---|
| `tag UID ... is not registered in Spoolman` | UID is not in the `rfid_tag` extra field on the spool record |
| `Connection refused` or timeout | `spoolman_url` is wrong or Spoolman isn't reachable from the Pi |
| Tag found but wrong spool ID | UID registered on the wrong spool record |
| Working yesterday, failing today | Spoolman URL changed, or `auto` lost the Moonraker URL |

Enable verbose logging to see the full Spoolman HTTP exchange:

```ini
[nfc_gate]
debug:             3
console_output:    True
console_log_level: info
```

Restart Klipper, then run `NFC GATE=0 POLL=1`.

---

## Creating a rich tag (examples and required fields)

A "rich" tag carries structured metadata the vendored parser understands (OpenSpool/OpenPrintTag/TigerTag/Bambu/Elegoo/etc.). To have Spoolman auto-create records from a tag, the tag should include at least the `material` field. Recommended additional fields:
- `brand` or `vendor` (string)
- `material` (string) — required for auto-create
- `color_hex` (string, `#RRGGBB`) — optional but recommended
- `min_temp` / `max_temp` (integers) — recommended for sensible temps
- `bed_temp` (integer) — optional
- `diameter` (float, e.g. `1.75`) — optional
- `spoolman_id` (integer) — optional embedded Spoolman ID; if present the resolver will try this first

### Supported Rich Manufacturer Tags

The bundled rich-tag parser currently recognizes these manufacturer spool tag formats:

| Manufacturer / ecosystem | Tag format |
|---|---|
| Bambu Lab | Factory MIFARE Classic 1K spool tags; requires `bambu_reads: True` and `pycryptodome`. Per-tag HKDF-derived keys. |
| ELEGOO | EPC-256 binary tags on NTAG213 |
| Anycubic | ACE binary tags on NTAG213/215 |
| TigerTag | TigerTag / TigerTag+ binary tags on NTAG213/215/216; local core metadata only, no cloud lookup or signature verification. Also extracts the Twin Tag ID (same-spool pairing key for TigerTag's two-tags-per-spool design) as `spool_identity`, used for left-neighbor interference detection the same way Bambu's `tray_uid` is. |
| Creality | CFS / K1 / K2 MIFARE Classic tags; requires `bambu_reads: True` and `pycryptodome`. Sector 1 uses a UID-derived Key B plus an AES-128-ECB-encrypted payload (both **confirmed on real Creality spool tags**, community-sourced) — not the plain default key. The decoded payload also creates a Bambu-style `spool_identity` for same-spool / left-neighbor interference handling. |
| QIDI | QIDI Box MIFARE Classic tags; requires `bambu_reads: True` (gates the default-key fallback attempt — no `pycryptodome` needed). Authenticates with the plain MIFARE Classic factory default key, `FF FF FF FF FF FF` — **confirmed**, sourced from the community `BoxRFID-Touch` project. If reads fail on real QIDI tags, see the [QIDI Box RFID reference](qidi-rfid-reference.md) for the official sector/block layout and QIDI-specific Key A note. |
| SimplyPrint / QIDI standard URL | NDEF URI/Text tags with supported filament query fields |

It also recognizes open rich-tag formats: OpenTag3D, OpenSpool, OpenPrintTag, and generic NDEF JSON filament records.

### Same-Spool `spool_identity`

Some manufacturer tags expose a stable spool identity that is separate from the
chip UID. NFC uses this only for scan-jog left-neighbor interference detection:
if gate `N` reads a tag whose `spool_identity` matches gate `N - 1`, NFC treats
the read as the left spool bleeding into the current reader field and keeps
scanning the current lane.

| Format | `spool_identity` | Source fields | Notes |
|---|---|---|---|
| Bambu Lab | `bambu_<tray_uid>` | Bambu `tray_uid` | Models the same spool even when a spool has more than one readable chip/side. |
| TigerTag / TigerTag+ | `tigertag_<twin_tag_id>` | Twin Tag ID | The same Twin Tag ID is written to both chips on a two-tag spool. |
| Creality CFS/K1/K2 | `creality_<numeric_hash>` | `vendor_id`, `date_code`, `batch`, `filament_id`, `color`, `length`, `serial` | The hardware UID is intentionally not part of the identity, so two tags carrying the same Creality spool payload produce the same identity. |
| QIDI Box | none currently | material, color, manufacturer code only | QIDI parsing resolves filament metadata, but the known three-byte payload is not enough to prove same physical spool. |

`spool_identity` is not a replacement for Spoolman's UID registration. Spoolman
still resolves the actual spool ID from the tag UID first. The identity is a
secondary same-spool signal used before metadata-based auto-create and during
scan-jog interference handling.

> [!NOTE]
> **Creality key material.** Neither key is a secret held back from this
> repo — both are published here exactly as used in `vendor/rfid_tag_parser.py`
> (`_CREALITY_AES_KEY_GEN` / `_CREALITY_AES_KEY_CIPHER`), community-sourced
> from a Creality RFID encryption helper script that mirrors the JavaScript
> implementation in Creality's own tag-writer tooling.
>
> | Key | Hex | ASCII | Used for |
> |---|---|---|---|
> | `AES_KEY_GEN` | `71 33 62 75 5E 74 31 6E 71 66 5A 28 70 66 24 31` | `q3bu^t1nqfZ(pf$1` | AES-128-ECB-encrypting the tag UID (repeated to 16 bytes) to derive the sector-1 MIFARE Key B — first 6 bytes of the ciphertext are the key. |
> | `AES_KEY_CIPHER` | `48 40 43 46 6B 52 6E 7A 40 4B 41 74 42 4A 70 32` | `H@CFkRnz@KAtBJp2` | AES-128-ECB-decrypting the ASCII payload stored across sector 1 blocks 4-6 (date code, vendor ID, batch, filament ID/material code, color, length code, serial, reserve/trailing data). |
>
> Both are static, UID-independent AES-128-ECB keys (no IV, no per-tag salt
> beyond the UID feeding `AES_KEY_GEN`). Reading a Creality tag therefore
> needs `pycryptodome` for both key derivation and payload decryption, unlike
> QIDI's plain default-key fallback.

Reader compatibility:

| Tag capability | PN532 | PN7160 | RC522 | PN5180 | Notes |
|---|---:|---:|---:|---:|---|
| UID lookup through Spoolman | Yes | Yes | Yes | Yes | Factory UID matching works on all supported readers. |
| NTAG / Type-2 rich tags | Yes | Yes | Yes | Yes | Used by common NDEF text, JSON, OpenSpool, TigerTag, and several manufacturer tags. |
| MIFARE Classic rich reads (Bambu, QIDI, Creality) | Yes | Yes | Yes | Yes | Requires `tag_parsing: True` and `bambu_reads: True` on all four. Bambu and Creality additionally require `pycryptodome`; QIDI uses a default-key fallback that needs no extra dependency. |
| SLIX2 / ISO15693 Type-5 rich tags | No | Yes | No | Yes | Current Type-5 rich-read path is for SLIX2 tags. Read bytes enter the normal parser and are not limited to OpenPrintTag. |

Example OpenSpool-like JSON payload (MIME `application/vnd.openspool` or generic NDEF JSON):

```json
{
  "brand": "eSun",
  "material": "PLA",
  "color_hex": "#FF5500",
  "min_temp": 200,
  "max_temp": 220,
  "bed_temp": 60,
  "diameter": 1.75
}
```

If you are using the NFC Tools app, write the payload as a plain text record. Do not use a rich-text editor or any tool that formats or escapes quotes automatically.

Example NFC Tools write steps:
1. Open NFC Tools and go to the **Write** tab.
2. Tap **Add a record**.
3. Choose **Text**.
4. Paste the exact JSON payload below into the text field, including all braces, quotes, commas, and colons:

```json
{"brand":"Sunlu","material":"ASA","color_hex":"#FF5500","min_temp":200,"max_temp":220,"bed_temp":60,"diameter":1.75}
```

5. Tap **Write**, then place the tag on the reader.

The text field on the tag must contain the raw JSON object exactly as shown; the parser reads that text as metadata. The `material` field is required for Spoolman auto-create.

If your writer supports custom MIME records, use MIME type `application/vnd.openspool` and the same JSON payload exactly.

For Bambu factory/tagged MIFARE spools, use the appropriate authenticated writer; plain text writes only apply to generic rich tags.

When testing: enable `tag_parsing: True` and `spoolman_auto_create: True` in your `nfc_reader.cfg` so the auto-create path runs on read. Watch the Klipper console logs for `auto_create` resolution and any HTTP error messages.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
