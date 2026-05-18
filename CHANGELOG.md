# Changelog

All notable changes to the EMU NFC Gate Reader are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.9.0-beta.1] — 2026-05-17 — Public Beta

### Added

**Shared Reader**
- Single PN532 mounted inside the MMU body for spool staging before loading
- Tap a tagged spool; NFC stages the spool ID for the next pregate preload automatically
- Separate `nfc_reader_shared.cfg` hardware config — can coexist with per-lane readers
- `_NFC_SHARED_PRELOAD` Happy Hare post-preload hook
- LED feedback states: tag read, spool ready, unresolved tag, auto-create in progress

**Rich Tag Metadata**
- OpenSpool and OpenPrintTag NTAG/Type-2 metadata reads
- Bambu factory-tagged spool support (MIFARE Classic with HKDF authentication via `pycryptodome`)
- Auto-create Spoolman spools from rich tag data (material, color, temperature)
- Optional `cbor2` library for full OpenPrintTag CBOR payload decoding; built-in minimal fallback active without it

**Scan-Jog Mode**
- Incremental jog/read cycles to bring a tag into read range without a full load
- Left-lane interference detection using spool identity — distinguishes neighbor reads from target reads
- 3-attempt interference retry with automatic left-lane repark on failure

**Happy Hare Integration**
- `MMU_GATE_MAP` updates with material, color, and temperature from tag metadata
- `NFC_GATE_STATUS` cross-references Klipper MMU lane MCUs for live status
- HH external gate-clear detection — re-dispatches `_NFC_SPOOL_CHANGED` on next poll
- Spoolman sync (`MMU_SPOOLMAN REFRESH=1`) triggered automatically for auto-created spools

**Installer**
- Interactive terminal color theme selection (20 profiles, or pass `-p <profile>` to skip)
- Always installs both `nfc_reader_hw.cfg` and `nfc_reader_shared.cfg`; non-destructive merge on re-runs adds only missing sections
- Sparse checkout configured automatically — docs, bracket files, and CI scripts excluded from the Pi working tree
- Moonraker `[update_manager emu_nfc_reader]` block appended automatically
- Klipper Python venv auto-detection across standard install paths and non-standard usernames

### Hardware Support

| Reader | Mode | Status |
|---|---|---|
| PN532 I2C (one per EBB42 lane) | per-lane | stable |
| PN532 I2C (single, any Klipper MCU) | shared | beta |
| RC522 | — | not yet supported |

### Known Limitations
- RC522 driver is not yet available
- Bambu MIFARE reads require `pycryptodome` in the Klipper Python environment — the installer checks and warns if absent
- Shared reader is tested on single-MCU setups; hybrid (per-lane + shared on separate MCUs) configurations are community-tested only
