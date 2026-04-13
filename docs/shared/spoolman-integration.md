# Spoolman Integration

[← Back to README](../../Readme.md)

---

## How It Works

NFC Gate Reader uses the tag's factory UID as the link between a physical spool and a Spoolman record. Tags are never written to. The lookup chain is:

```
PN532 reads tag UID
        │
        ▼
SpoolmanClient searches spool extra fields for matching UID
        │
        ▼
On match: returns spool_id to NFC_Manager
        │
        ▼
NFC_Manager dispatches _NFC_SPOOL_CHANGED  →  MMU_SPOOLMAN UPDATE=1
```

The UID is just a string. Spoolman stores it in a custom extra field on the spool record. When a new tag appears at a gate, SpoolmanClient queries the Spoolman API and scans all spool records for a matching extra field value.

---

## Step 1 — Create the Extra Field in Spoolman

Before any UID can be registered, Spoolman needs to know the extra field exists.

1. Open Spoolman in your browser.
2. Go to **Settings → Extra Fields**.
3. Click **Add extra field**.
4. Set **Entity** to `Spool`.
5. Set **Name** to `rfid_tag` (or whatever name you prefer — must match `spoolman_rfid_key` in config).
6. Set **Field type** to `Text`.
7. Save.

The field name must match your config exactly:

```ini
[nfc_gate]
spoolman_rfid_key: rfid_tag
```

---

## Step 2 — Get the Tag UID

You need the UID of each NFC tag before you can register it. Options:

**From the Klipper console (recommended):**

```gcode
NFC_GATE NAME=lane0 SCAN=1
```

Hold the tag near the lane 0 reader. The UID is printed in the console.

**From the status command (if already registered and polling):**

```gcode
NFC_GATE_STATUS
```

**From a phone app:**

Any NFC reader app on Android or iOS can read the UID of an NTAG/Mifare tag. The UID is the same value regardless of which reader reads it — it is the factory-programmed tag identifier.

**From the standalone scanner (Pi GPIO):**

```bash
python3 ~/pn532_scan.py
```

---

## Step 3 — Register the Tag in Spoolman

1. Open the spool record in Spoolman.
2. Find the `rfid_tag` extra field.
3. Paste the UID into the field.
4. Save.

**UID formatting is normalized.** Any of these are equivalent and will match correctly:

```
04AABBCCDD
04:AA:BB:CC:DD
04-AA-BB-CC-DD
04 AA BB CC DD
```

SpoolmanClient normalizes the stored UID and the read UID to the same uppercase hex string before comparing.

---

## Step 4 — Test the Lookup

With the spool loaded on a gate, run one full poll:

```gcode
NFC_GATE NAME=lane0 POLL=1
```

Expected console output:

```
NFC gate 0: spool 1042 detected (UID 04AABBCCDD)
```

If the tag is detected but the UID is not found in Spoolman:

```
NFC gate 0: tag UID 04AABBCCDD is not registered in Spoolman.
Open the spool record in Spoolman, set the 'rfid_tag' extra field to: 04AABBCCDD
```

---

## Lookup Behaviour

When NFC_Manager receives a UID from PN532Driver:

1. SpoolmanClient checks its in-memory cache for the UID.
2. **Cache hit:** Returns the cached spool record immediately. No HTTP request.
3. **Cache miss:** Queries `GET /api/v1/spool` with the Spoolman URL, searches all records for a matching `extra[spoolman_rfid_key]` value.
4. **Match found:** Returns the `spool_id`. NFC_Manager updates gate state and dispatches `_NFC_SPOOL_CHANGED`.
5. **No match:** Returns nothing. NFC_Manager dispatches `_NFC_TAG_NO_SPOOL`.

The cache TTL is controlled by `spoolman_cache_ttl` (default: 300 seconds). To disable caching:

```ini
spoolman_cache_ttl: 0
```

---

## Spoolman URL Configuration

### Automatic (preferred)

```ini
spoolman_url: auto
```

SpoolmanClient asks Moonraker for its configured Spoolman URL. This works when `moonraker.conf` has a `[spoolman]` section:

```ini
# in moonraker.conf
[spoolman]
server: http://192.168.1.50:7912
```

### Direct URL

```ini
spoolman_url: http://192.168.1.50:7912
```

Use this when:
- Moonraker is not configured with Spoolman.
- You are testing against a different Spoolman instance.
- `auto` is failing and you want to bypass Moonraker URL discovery.

---

## Ownership Boundary

SpoolmanClient is a **lookup and cache client only**.

- It resolves UID → spool record.
- It does not know which MMU gate the spool is physically on.
- It does not call `MMU_SPOOLMAN` or any Happy Hare command.
- It does not write to Spoolman.

Gate assignment belongs to NFC_Manager, which receives the spool_id from SpoolmanClient and decides whether the gate state changed. Happy Hare calls happen in `nfc_macros.cfg`.

This boundary is intentional. See [Architecture Decisions](architecture-decisions.md).
