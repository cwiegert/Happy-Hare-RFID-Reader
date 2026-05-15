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
NFC gate 0: tag UID 04AABBCCDD is not registered in Spoolman.
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

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
