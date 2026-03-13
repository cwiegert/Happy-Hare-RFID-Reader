# Spoolman Integration — UID Lookup Setup

[← Back to Index](../../Readme.md)

---

## How It Works

Tags are **never written to**.  The reader reads only the tag's factory UID — the
number burned into the chip at the factory, unique to every tag ever made.

```
blank tag  →  reader scans UID  →  Spoolman API lookup  →  spool_id  →  MMU_GATE_MAP
```

You paste the UID into Spoolman's database once.  After that, just stick the tag
on the spool — no NFC writing app, no NDEF records, no tag format to worry about.

---

## Step 1 — Add the RFID Extra Field in Spoolman

Spoolman stores arbitrary metadata on spool records in a JSON dict called **extra**.
You need to create a custom field (one-time setup, applies to all spools).
The field name must match `spoolman_rfid_key` in `nfc_vars.cfg` — the default is `rfid_tag`:

1. Open Spoolman in your browser.
2. Go to **Settings → Extra Fields → Spool**.
3. Click **Add field**.
4. Set:
   - **Field name:** `rfid_tag`  *(must match `spoolman_rfid_key` in `nfc_vars.cfg`)*
   - **Field type:** `Text`
5. Save.

> **Older Spoolman versions (pre-v0.14):** The extra-fields system was added in
> v0.14.  If you don't see it, upgrade Spoolman with:
> ```bash
> docker compose pull && docker compose up -d
> ```
> or however you installed it.  The `rfid` field approach works on all versions
> that have `/api/v1/spool` with an `extra` dict (v0.14+).

---

## Step 2 — Read Each Tag's UID

You need the UID **before** you put the tag on the spool — or you can put it on
the spool first and read it via Klipper (see Step 4).

### Option A — Phone app (recommended)

**NFC Tools** (free, iOS and Android):

1. Open NFC Tools.
2. Tap **Read**.
3. Hold the tag to the back of your phone.
4. Look at the **NFC** section — copy the **Serial number** (e.g. `04:A2:3B:C1:D4:5E:80`).

The UID may be shown with colons or without.  Spoolman accepts both —
`04:A2:3B:C1:D4:5E:80` and `04A23BC1D45E80` are treated as the same value.

### Option B — Read from Klipper

If the tag is already on a gate:

1. Place the tag on a gate.
2. Wait up to one poll cycle (30 s default).
3. Run `NFC_GATE_STATUS` from the Mainsail/Fluidd terminal.

**Output when UID is not yet registered:**
```
Gate 0: tag 04A23BC1D45E80 (UID not in Spoolman — set the 'rfid' field on the spool record)
```

Copy the UID from that line.

---

## Step 3 — Register the UID in Spoolman

For each spool:

1. Open the spool record in Spoolman.
2. Scroll to the **Extra** section (added in Step 1).
3. Set **rfid_tag** to the tag UID — e.g. `04A23BC1D45E80`.
4. Save.

> Separators are optional.  `04A23BC1D45E80`, `04:A2:3B:C1:D4:5E:80`, and
> `04-A2-3B-C1-D4-5E-80` all match the same tag.

---

## Step 4 — Verify

1. Make sure `spoolman_url` is set in `nfc_vars.cfg` to point at your Spoolman instance.
2. Restart Klipper.
3. Place the tag on a gate.
4. Wait one poll cycle (or set `poll_interval: 5` temporarily to speed up testing).
5. Run `NFC_GATE_STATUS`:

**Success:**
```
Gate 0: spool 42      UID 04A23BC1D45E80
```

**UID still not found:**
```
Gate 0: tag 04A23BC1D45E80 (UID not in Spoolman — set the 'rfid' field on the spool record)
```
→ Double-check the `rfid` field value on that spool record, and that `spoolman_url` is correct.

---

## Config Reference

All Spoolman settings go in the `[nfc_gate]` base section in **`nfc_vars.cfg`**.
Individual `[nfc_gate laneN]` or `[nfc_gates]` sections inherit these values and can
override them locally if needed.

| Key | Default | Description |
|---|---|---|
| `spoolman_url` | *(required)* | Root URL of your Spoolman instance, e.g. `http://192.168.1.50:7912` |
| `spoolman_rfid_key` | `rfid_tag` | Extra field name in Spoolman — must match what you created in Step 1 |
| `spoolman_timeout` | `5.0` | HTTP request timeout in seconds |
| `spoolman_cache_ttl` | `300` | Seconds to cache a successful UID→spool mapping (0 = no cache). Cached entries skip the Spoolman API call when the same tag is re-detected within the TTL. |

---

## Changing a Spool

When you load a new spool onto a gate:

- If you're **reusing the same tag**: update the `rfid` field on the new spool record,
  and clear it from the old spool record.
- If you're **using a new tag**: peel the old tag off, stick the new one on, then
  register the new UID in Spoolman.

There is no need to touch the tag itself — it stays blank.

---

## Supported Tag Types

Any 13.56 MHz ISO14443A tag works.  UID length varies by tag family:

| Tag family | UID length | Example products |
|---|---|---|
| NTAG213 / 215 / 216 | 7 bytes (14 hex chars) | 25 mm stickers, 50 mm discs |
| Mifare Classic 1K/4K | 4 bytes (8 hex chars) | White card blanks |
| Mifare Ultralight | 7 bytes (14 hex chars) | Thin sticker tags |

All of the above work.  The reader reports whichever UID length the chip provides.

> **Tip:** 50 mm disc tags are reliably detected at 1–3 cm.  Small 25 mm sticker tags
> may require sub-centimetre positioning.  For reliable gate detection without precise
> alignment, use 50 mm disc tags.
