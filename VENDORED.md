# Vendored Files

Files copied verbatim from upstream repos. GPLv3 headers are preserved in each file.

## lameandboard/rfid

| Source | Destination | Used for |
|---|---|---|
| `extras/rfid_tag_parser.py` | `klippy/extras/nfc_gates/vendor/rfid_tag_parser.py` | Tag payload parsing (all formats) |
| `extras/spoolman_client.py` | `klippy/extras/nfc_gates/vendor/lameandboard_spoolman.py` | Vendor/filament/spool CRUD building blocks |

```
upstream_repo:   https://github.com/lameandboard/rfid.git
upstream_branch: main
upstream_commit: c1aadffa8d58abc92eaa674e66a57b2998a44386
synced_date:     2026-04-30
```

### Usage notes

`rfid_tag_parser.py` is used as-is. Entry point: `parse_tag(raw_bytes_or_blocks, uid_hex)`.

`lameandboard_spoolman.py` is used for `find_or_create_vendor()`, `find_or_create_filament()`,
and `create_spool()` only. `auto_create_spool()` is present in the file but **not called** —
it encodes the `rfid_uid_N` multi-slot UID convention which this project does not use.

### Updating

A GitHub Action runs weekly and opens a PR automatically if upstream changes either file.
To update manually:

```bash
git fetch lameandboard
git show lameandboard/main:extras/rfid_tag_parser.py > klippy/extras/nfc_gates/vendor/rfid_tag_parser.py
git show lameandboard/main:extras/spoolman_client.py > klippy/extras/nfc_gates/vendor/lameandboard_spoolman.py
```

Review the diff against `nfc_manager.py` adapter code before committing, then update
`upstream_commit` and `synced_date` above.
