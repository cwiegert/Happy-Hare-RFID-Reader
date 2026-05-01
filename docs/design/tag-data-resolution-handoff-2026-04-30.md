# Tag Data Resolution Handoff — 2026-04-30

## Current State

The implementation now has the manager-owned deep-read orchestration path:

- `tag_parsing: False` keeps the UID-only path and calls `read_tag()`.
- `tag_parsing: True` calls `read_target()`, creates `CurrentTag`, stores `target_info`, classifies the target, and only deep-reads when the target is currently classified as `ntag_type2`.
- NTAG/Type-2 metadata is read before scan-jog rewind because scan-jog calls `_poll()`, and `_poll()` now completes `_read_current_tag()` before returning `tag_found=True`.
- `_resolve_spool()` walks the resolution ladder:
  - embedded `spoolman_id`
  - UID lookup
  - opt-in auto-create
  - metadata-direct fallback when Spoolman is disabled or unavailable
- Metadata-only scan-jog dispatch preserves metadata through rewind.

## Bambu / MIFARE Status

Bambu-style tags are safely stubbed off for this phase:

- Conservative classification treats `SAK & 0x08` as `mifare_classic`.
- MIFARE Classic does not attempt NTAG page reads.
- The tag observation records `parse_error = "mifare_classic metadata read not implemented"`.
- The system releases the target and falls back to UID-only Spoolman resolution.

This means Bambu tags can still work by UID registration, but Bambu metadata parsing is not implemented yet.

## What Is Still Missing

1. Bambu/MIFARE authenticated block reads:
   - key derivation / key selection
   - PN532 authentication commands
   - authenticated block reads
   - passing a block dict to `rfid_tag_parser.parse_tag()`

2. More precise target classification:
   - current table is conservative, not exhaustive
   - unknown targets intentionally fall back UID-only

3. Hardware validation:
   - UID-only with `tag_parsing: False`
   - NTAG/OpenSpool deep read with `tag_parsing: True`
   - blank NTAG fallback
   - MIFARE/Bambu fallback without NTAG read
   - scan-jog waits for deep read before rewind

4. Optional richer Happy Hare metadata forwarding:
   - current direct metadata path forwards material/color/vendor/uid
   - temps/weight are not forwarded to HH macros yet

## Tomorrow Starting Point

Start by checking the implementation against `docs/design/tag-data-resolution.md`, especially:

- Requirements 1, 2, 5, 6, 7, 9, 10
- "Read Strategy Responsibilities"
- "When parsing runs"
- "Resolution Result Summary"

Then decide whether we are ready for live NTAG hardware testing or whether to add more ugly-case tests first.

Recommended first command:

```bash
python3 -m pytest -q
```

Current local result at handoff:

```text
128 passed
```

## Agent Pickup Prompts

Use one of these prompts when restarting work so the next agent anchors on the design and current implementation instead of guessing from isolated files.

### Prompt for Codex

```text
Read docs/design/tag-data-resolution.md and docs/design/tag-data-resolution-handoff-2026-04-30.md first. Then inspect klippy/extras/nfc_gates/nfc_manager.py, klippy/extras/nfc_gates/pn532_driver.py, klippy/extras/nfc_gates/scan_jog.py, klippy/extras/nfc_gates/spoolman_client.py, and tests/test_gate_state.py.

Goal: continue the NFC tag-data integration from the handoff. Do not assume Bambu/MIFARE metadata is implemented. Confirm that tag_parsing False remains UID-only, tag_parsing True uses the manager-owned _read_current_tag() path, NTAG/Type-2 deep reads happen before scan-jog rewind, and MIFARE/unknown targets fall back UID-only.

Before changing code, state whether the next best step is ugly-case tests, live NTAG hardware validation support, or MIFARE/Bambu implementation. Preserve the original design requirements, especially feature gating, UID-only compatibility, conservative fallback, no tag writes, and our configured spoolman_rfid_key convention.
```

### Prompt for Claude Code

```text
You are picking up the NFC tag-data resolution implementation. Start by reading:
- docs/design/tag-data-resolution.md
- docs/design/tag-data-resolution-handoff-2026-04-30.md

Then inspect:
- klippy/extras/nfc_gates/nfc_manager.py
- klippy/extras/nfc_gates/pn532_driver.py
- klippy/extras/nfc_gates/scan_jog.py
- klippy/extras/nfc_gates/spoolman_client.py
- tests/test_gate_state.py
- tests/test_vendor_contract.py

Current intended state:
- tag_parsing False: UID-only via read_tag(), no metadata read.
- tag_parsing True: manager calls read_target(), stores CurrentTag.target_info, classifies, reads NTAG memory only for ntag_type2, parses cached bytes, then resolves.
- MIFARE/Bambu: explicitly stubbed to UID-only fallback with parse_error "mifare_classic metadata read not implemented"; do not attempt NTAG reads.
- Unknown targets: UID-only fallback.
- _resolve_spool() ladder: embedded spoolman_id, UID lookup, optional auto-create, metadata-direct fallback.
- scan-jog must not rewind until _poll() completes the deep-read attempt.

Run python3 -m pytest -q before and after changes. Current known local result at handoff was 128 passed. If tests differ, investigate runner/environment before making broad assumptions.

Recommended next work: add ugly-case tests for read_target None/missing UID, failed NTAG read, empty NTAG read, parser exception, parser None/error dict, and scan-jog metadata preservation; or prepare a live NTAG hardware validation checklist. Keep code changes small and design-driven.
```

## Suggested Next Work

If staying in non-Bambu scope tomorrow:

1. Add ugly-case tests for failed/empty NTAG reads and parser exceptions.
2. Add live-reader debug logging checklist for NTAG validation.
3. Test actual OpenSpool/metadata NTAG through scan-jog.

If moving into Bambu/MIFARE scope tomorrow:

1. Define exact PN532 auth/read API surface.
2. Add MIFARE target classification fixtures.
3. Wire key derivation separately from PN532 hardware transactions.
4. Keep all failures UID-only until authenticated block reads are proven.
