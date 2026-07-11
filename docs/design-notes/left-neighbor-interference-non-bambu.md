# Design Note: Left-Neighbor Interference Detection for Non-Bambu Rich Tags

Status: **Design only — not yet implemented.**

---

## Background

`scan_jog.py`'s left-neighbor interference mitigation currently only fires
for Bambu-format tags. `is_left_neighbor_spool_identity_match()`
(`scan_jog.py:1545`) compares `current_tag.spool_identity` between the
scanning gate and its immediate left neighbor's cached tag — and
`spool_identity` is only ever set by the Bambu block parser
(`vendor/rfid_tag_parser.py:846`, `"bambu_%s" % tray_uid`). No other format
in the vendored parser sets it, and `_spool_identity_from_meta()`
(`tag_handler.py:232`) has no fallback field.

This wasn't an oversight — commit `3484a95` ("Use spool identity for
left-neighbor interference", jacksky6) deliberately replaced an earlier
UID-based check specifically because Bambu spools carry **two physical NFC
tags, one per side, each with a different chip UID** for the same spool. A
straight UID comparison couldn't recognize those as the same spool, so the
fix switched to the tray-embedded `spool_identity`, which is identical on
both side-tags. The commit is explicit that this was a deliberate,
Bambu-scoped decision: *"There is intentionally no UID, Spoolman, material,
color, or Happy Hare metadata fallback."*

The gap: every other supported format — ELEGOO, Anycubic ACE, TigerTag,
Creality CFS/K1/K2, QIDI Box, OpenTag3D, OpenSpool, OpenPrintTag, generic
NDEF JSON — gets **no** interference protection at all today.

## What the research shows about other formats

Checked against both the vendored parser's actual field output and the
public specs for the open standards this project supports:

| Format | Physical tags per spool | Fields the parser extracts | Per-spool instance ID in the payload? |
|---|---|---|---|
| **Bambu** | 2 (different chip UIDs) | material, brand, color, temps, `tray_uid` | **Yes** — `tray_uid` (hence `spool_identity`) |
| ELEGOO | 1 | material, brand, color_hex, diameter_mm, weight_g | No |
| Anycubic ACE | 1 | brand, material, sku, color_hex, temps, diameter_mm | No — `sku` is a catalog/product code, identical across every spool of that product |
| Creality CFS/K1/K2 | 1 | material, brand, color_hex | No |
| QIDI Box | 1 | material, brand, color_hex, diameter_mm | No |
| TigerTag | 1 | material, brand, temps, fabrication timestamp | No — the tag's own NFC chip UID *is* presented as "the unique ID" per TigerTag's own docs |
| OpenSpool | 1 | type (material), color_hex, brand, temps | No |
| OpenTag3D | 1 | manufacturer, material, color, print settings | No (spec still finalizing, no per-unit ID field documented) |
| OpenPrintTag | 1 | material, color, weight, temps, density, diameter | No — per OpenPrintTag's own materials: *"the tag itself stores only a UID, which serves as the unique identifier for each tag"* |

Two findings that matter for this design:

1. **Bambu's dual-tag-per-spool design looks like the outlier, not the norm.**
   Every other researched format is one physical NFC chip per spool. For
   those, the chip's own factory UID reliably and persistently identifies
   that specific physical spool — which is exactly the property `spool_identity`
   was invented to provide for Bambu's two-tags-one-spool case.
2. **None of the researched metadata fields are per-instance.** Material,
   color, brand, and even SKU are all *product-catalog* attributes — the
   same for every spool of that product, not a serial number for one
   physical unit. Two different spools of, say, the same Anycubic PLA Black
   SKU would produce **identical** metadata.

## Implication for the original ask

The original ask was: read the rich tag on a non-Bambu format, compare its
parsed metadata against what's cached for the left neighbor, and treat a
match as proof of interference. Given finding (2) above, that check alone
has a real false-positive mode: two genuinely different spools of the same
product in adjacent gates (a common real scenario — buying more than one
spool of a popular color) would match on metadata despite not being the
same physical spool at all, and `shift_left_neighbor()` would then jog the
left gate's own, correctly-loaded spool out of position for no reason.

Given finding (1), there's a lower-risk option available for every
single-tag format: fall back to comparing the raw NFC UID itself, restoring
(in scoped form) the mechanism `3484a95` removed wholesale. For a
single-tag-per-spool format, gate N genuinely reading gate N-1's antenna
field means gate N's reader is picking up **the exact same physical chip**
— so its UID will exactly match gate N-1's cached UID. This has no
metadata-collision failure mode, costs nothing extra to compute (the UID is
already read every poll), and directly mirrors why `spool_identity` works
for Bambu — both are "the identifier that's actually invariant for this
physical spool," just sourced differently per tag design.

## Proposed design: tiered identity check

Generalize `current_spool_identity()` into something that returns a
`(tier, value)` pair instead of a single Bambu-only string, and only ever
compare values within the same tier (never let a UID-tier value match
against a metadata-tier value, and never let either match against a
`spool_identity`-tier value):

```
Tier 1 — spool_identity   (existing, Bambu only, unchanged)
Tier 2 — raw NFC UID       (new: any format, when spool_identity is absent)
Tier 3 — metadata fingerprint (new: opt-in, only when even the UID
                                comparison isn't decisive — see caveat below)
```

```python
def current_spool_signature(gate):
    tag = gate._state.current_tag
    if tag is None:
        return None
    if getattr(tag, 'spool_identity', None):
        return ('identity', tag.spool_identity)          # Tier 1
    if getattr(tag, 'uid', None):
        return ('uid', tag.uid)                           # Tier 2
    return None
```

`spool_identity_for_gate()`'s left-neighbor lookup generalizes the same
way, reading the left gate's cached `current_tag` and producing the same
kind of `(tier, value)` pair. `is_left_neighbor_spool_identity_match()`
becomes a same-tier comparison: `mine == left and mine[0] == left[0]`.

**Tier 2 (UID) is the recommended addition.** It closes the real gap (every
non-Bambu format currently gets zero protection) without introducing the
false-positive mode metadata matching has. It's also nearly free to add: the
existing `read_uid_from_scan_event()`/`GateState.current_tag` plumbing
already carries the UID everywhere `spool_identity` is read from today.

**Tier 3 (metadata fingerprint) is the part of the original ask this design
flags rather than just building as specified.** Given Tier 2 already covers
the true RF-crosstalk case for every single-tag format (gate N reading gate
N-1's actual chip means an exact UID match, full stop), Tier 3 adds
detection power only for a narrower, harder-to-justify case — e.g., a
physical tag that was replaced/rewritten on the same spool, so the UID
changed but the printed contents (material/color/brand) didn't. If it's
still wanted despite the collision risk:

- Build the fingerprint from the *most* discriminating fields available,
  not material+color alone — `sku`/`material_id`/`material_variant_id` when
  present, combined with material+color+brand+diameter+weight when they
  aren't, all case-normalized and joined into one string.
- Require an exact match on the *entire* available field set, not a partial
  overlap — partial matches only make the collision problem worse.
- Consider making a Tier 3 match **log a warning and stop the scan for
  manual confirmation** rather than automatically triggering
  `shift_left_neighbor()`'s physical jog, since a wrong automatic shift
  moves a correctly-loaded neighbor spool out of position. Tier 1 and 2
  matches are exact-identity proofs and are safe to act on automatically;
  Tier 3 is a heuristic and arguably shouldn't drive an unattended physical
  move by itself.

This is the one part of this design that's a genuine open question rather
than a settled recommendation — worth your call before it's built.

## Integration points (unchanged from the existing mechanism)

No new hooks needed — `handle_left_neighbor_interference()`
(`scan_jog.py:1650`), `shift_left_neighbor()`, and `restore_left_neighbor()`
keep their current structure and call sites. Only the identity computation
and comparison (`current_spool_identity()` /
`is_left_neighbor_spool_identity_match()`) change shape, from a single
Bambu-only string to the tiered `(tier, value)` check above. The retry/abort
state tracking (`_scan_left_neighbor_identity`, capped at
`LEFT_NEIGHBOR_CLEARANCE_RETRIES` attempts) stays as-is, just now populated
from whichever tier produced the match.

## Suggested implementation sequence

1. Generalize `current_spool_identity()` → `current_spool_signature()`
   returning `(tier, value)`, adding the Tier 2 UID fallback.
2. Generalize `spool_identity_for_gate()` the same way for the left
   neighbor's cached tag.
3. Update `is_left_neighbor_spool_identity_match()` to compare same-tier
   pairs only.
4. Decide on Tier 3 (build it with the log-and-stop behavior above, or skip
   it and rely on Tier 1 + Tier 2) — this needs a decision before
   implementation, not just a default choice.
5. Update `docs/shared/architecture-decisions.md` / test coverage
   (`tests/test_scan_jog_mode.py` per `3484a95`'s precedent) to cover the
   new UID-tier case explicitly, including a same-product-different-spool
   test proving Tier 3 (if built) doesn't fire on metadata alone without an
   identity or UID match.

## Sources

- [TigerTag-Project/TigerTag-RFID-Guide](https://github.com/TigerTag-Project/TigerTag-RFID-Guide) — TigerTag data structure; unique ID is the tag's own serial
- [TigerTag.io](https://tigertag.io/) — "Every NFC tag has a unique ID (serial number)... Each spool carries a unique identifier" (chip-level, not a separate payload field)
- [OpenSpool (spuder/OpenSpool)](https://github.com/spuder/OpenSpool) — JSON payload fields: protocol, version, type, color_hex, brand, min_temp, max_temp
- [OpenTag3D specification](https://opentag3d.info/spec.html) — NTAG213/215/216, manufacturer/material/color/print-settings fields, spec still finalizing (v0.020 at time of writing)
- [OpenPrintTag.org](https://openprinttag.org/) — CBOR payload on ISO15693 tags; "the tag itself stores only a UID, which serves as the unique identifier for each tag"
- [SimplyPrint: All about NFC/RFID for filament spools](https://help.simplyprint.io/en/article/all-about-nfc-rfid-for-filament-spools-bambu-openprinttag-creality-qidi-anycubic-more-19luyni/) — general survey confirming per-format data varies but per-spool distinction commonly relies on the chip's own unique ID
