---
name: run-happy-hare-rfid-reader
description: Run, drive, smoke-test, or verify the EMU NFC Gate Reader Klipper plugin and its installer without a printer. Use when asked to run the NFC reader, drive the gate/event pipeline, test install.sh (fresh/repair/reconfigure), or check that a change to nfc_gates emits the right Happy Hare G-code.
---

# Run the EMU NFC Gate Reader

This repo is a **Klipper `extras` plugin**, not a standalone app. On a real
printer it runs inside Klipper on a Raspberry Pi, polls physical NFC readers
(PN532 / PN7160 / RC522 over I2C or SPI), and hands each filament-tag read to
Happy Hare as a `_NFC_*` G-code macro call. There is **no GUI, no server, and
no printer here** — the MCU, the NFC hardware, and Happy Hare don't exist in a
dev environment, and the two hardware-facing modules (`nfc_manager`,
`reader_factory`) can't even import without Klipper's own `bus` module.

So "running it" means driving the two layers that PRs actually change, both of
which work off-printer:

1. **The plugin's decision + dispatch pipeline** — `GateState` (per-gate
   debounce) → `KlipperInterface` (event → exact G-code string). Driven by
   `.claude/skills/run-happy-hare-rfid-reader/driver.py`, which wires the
   **real** modules together with fake Klipper objects and prints the G-code
   each scenario emits to Happy Hare.
2. **The installer** — `install.sh` (fresh / `--repair` / `--reconfigure`),
   driven end-to-end against a throwaway `$HOME` by
   `tests/test_installer_repair.sh`.

All paths below are relative to the repo root. Everything runs on stock
`python3` (3.9+) and `bash`; no pip installs, no external services.

## Prerequisites

Nothing to install for the core paths — `python3` and `bash` only. The driver
uses the standard library exclusively.

```bash
python3 --version   # 3.9+ (uses stdlib only)
bash -n install.sh  # sanity: installer parses
```

## Run (agent path) — plugin event pipeline

The primary driver. Runs the real `GateState` + `KlipperInterface` through four
realistic scenarios (known Spoolman spool insert/hold/remove, a metadata-only
Bambu-style tag, an unknown tag, and debounce), printing the actual G-code sent
to Happy Hare. Exits nonzero if any emission is wrong, so it doubles as a smoke
test.

```bash
python3 .claude/skills/run-happy-hare-rfid-reader/driver.py
```

Expected tail:

```
DRIVER RESULT: PASS — event pipeline emitted expected Happy Hare G-code
```

Sample of what it prints (this is the real dispatch, not a mock of it):

```
  poll uid=04A1B2C3     spool=42               -> CHANGED
       G-code: _NFC_SPOOL_CHANGED GATE=0 READER=lane0 SPOOL_ID=42 UID=04A1B2C3
  poll uid=<no tag>     spool=None             -> REMOVED
       G-code: _NFC_SPOOL_REMOVED GATE=0 READER=lane0
```

To exercise a change to the event logic, edit
`klippy/extras/nfc_gates/gate_state.py` or `klipper_interface.py`, add or adjust
a scenario in `driver.py`, and rerun. To add a new expected-G-code case, append
to the `reads` list in a `run_scenario(...)` call and add an `expect(...)`
check in `main()`.

## Direct invocation — one module, no driver

For a change to a single function, import the real module and call it. This is
the fastest inner loop. The `FileHandler` line is required: `nfc_gates/log.py`
builds its logger at import time and, off-printer, would otherwise crash trying
to open `~/printer_data/logs/nfc_reader.log` (see Gotchas).

```bash
python3 - <<'PY'
import logging, os, sys, tempfile
logging.getLogger().addHandler(logging.FileHandler(os.path.join(tempfile.mkdtemp(), "klippy.log")))
sys.path.insert(0, "klippy/extras")
from nfc_gates.gate_state import GateState
gs = GateState(gate=3, absent_threshold=3)
print("first read of a new tag ->", gs.process_read("04AABBCC", 7))
print("same tag again          ->", gs.process_read("04AABBCC", 7))
PY
```

Prints:

```
first read of a new tag -> ('changed', 3, '04AABBCC', 7)
same tag again          -> None
```

Modules that import cleanly off-printer: `gate_state`, `klipper_interface`,
`log`, `tag_handler`, `spoolman_client`, `happy_hare_compat`, `hh_status`,
`shared_preload`, `pn532_driver`, `pn7160_driver`, `rc522_driver`, `scan_jog`,
`LED_effect_mgr`. **`nfc_manager` and `reader_factory` do not** — they
`import bus` from Klipper. Don't try to drive those two here; drive the pieces
they orchestrate instead.

## Run (agent path) — the installer

`install.sh` never needs a real printer: `HOME`, `RFID_READER_PRINTER_CONFIG`,
and `RFID_READER_KLIPPER_EXTRAS` redirect every target into a sandbox, and
`RFID_READER_ALLOW_DEV_PATH=yes` lets it run from a dev checkout. The committed
end-to-end test builds a fake `printer_data`, runs fresh install + `--repair` +
`--reconfigure`, and asserts the symlinks, backups, printer.cfg include order,
Moonraker updater rewrite, and `.install-state` are all correct:

```bash
bash tests/test_installer_repair.sh
```

Expected last line:

```
installer repair test: PASS
```

To watch one installer mode directly (repair, against your own sandbox):

```bash
SANDBOX="$(mktemp -d)"
CFG="$SANDBOX/printer_data/config"; EXTRAS="$SANDBOX/klipper/klippy/extras"
mkdir -p "$CFG/nfc" "$EXTRAS"
printf '[include mainsail.cfg]\n[include nfc/nfc_reader_hw.cfg]\n' > "$CFG/printer.cfg"
printf '[server]\nhost: 0.0.0.0\n' > "$CFG/moonraker.conf"
printf '[nfc_gate]\nspoolman_url: http://x:7912\n' > "$CFG/nfc/nfc_reader.cfg"
printf '[nfc_gate lane0]\ni2c_mcu: my_mcu\n' > "$CFG/nfc/nfc_reader_hw.cfg"
printf '# my macros\n' > "$CFG/nfc/nfc_macros.cfg"

HOME="$SANDBOX" RFID_READER_ALLOW_DEV_PATH=yes \
  RFID_READER_PRINTER_CONFIG="$CFG" RFID_READER_KLIPPER_EXTRAS="$EXTRAS" \
  bash install.sh -r
ls -l "$CFG/nfc/nfc_macros.cfg"   # -> symlink into repo config/ (read-only)
rm -rf "$SANDBOX"
```

Modes: no flag = interactive first-time wizard (prompts on stdin; the test
pipes blank lines to accept defaults); `-r` / `--repair` = re-link
installer-owned files, never touch user settings; `-c` / `--reconfigure` = back
up NFC config and rerun the wizard. `bash install.sh -h` prints the summary.

## Run (human path) — a real printer

Not reproducible here. On a Pi with Klipper + Happy Hare + wired NFC readers:
`bash install.sh` (interactive), add the `[include nfc/...]` lines it reports to
`printer.cfg`, restart Klipper, then present a tag to a reader and watch the
`_NFC_*` macros fire. `NFC_DOCTOR` (a plugin G-code command) reports live gate
and virtual-endstop status. None of that is exercisable without hardware — use
the driver and installer paths above instead.

## Gotchas

- **Importing any `nfc_gates` module crashes off-printer unless you pre-seed a
  log dir.** `log.py` runs `logger = _build_logger()` at import time; with no
  Klipper root-logger `FileHandler` present it falls back to
  `~/printer_data/logs/nfc_reader.log`, which doesn't exist and raises
  `FileNotFoundError` *during import*. Attaching any `FileHandler` to the root
  logger before the first `import nfc_gates.*` makes it resolve the log into
  that handler's directory instead. Both the driver and the one-liner above do
  this on their first two lines — keep it.
- **`nfc_manager` and `reader_factory` need Klipper's `bus` module** and will
  `ModuleNotFoundError: No module named 'bus'` off-printer. That's expected —
  they're the hardware-facing layer. Drive `GateState` / `KlipperInterface` /
  `tag_handler` directly instead of going through the manager.
- **The installer refuses to run from a dev checkout unless you say so.**
  Without `RFID_READER_ALLOW_DEV_PATH=yes` it aborts because the checkout isn't
  the expected `~/rfid-reader` install dir. The env var is how the test and the
  sandbox recipe above get past it.
- **`install.sh` with no flag is interactive** — it blocks on `read` prompts. To
  drive it non-interactively, pipe blank lines (`printf '\n%.0s' {1..20} | ...`)
  to accept defaults, as `tests/test_installer_repair.sh` does; a plain
  `bash install.sh` will just hang waiting for input.
- **`nfc_macros.cfg` is installed as a read-only symlink into the repo's
  `config/`**, not a copy. On repair, any pre-existing regular file is moved to
  `nfc_macros.cfg.pre-read-only-<timestamp>` first. If you're asserting on it,
  test with `-L` / `readlink`, not file contents.
- **`DIRECT_METADATA_SPOOL` is a sentinel `object()`, not an int.** A rich tag
  that carries filament metadata but has no Spoolman id resolves to this
  sentinel; `GateState` treats it as a real spool for change detection, and
  `KlipperInterface` emits the `MATERIAL=/COLOR=/BRAND=` form of
  `_NFC_SPOOL_CHANGED` instead of `SPOOL_ID=`. Passing a plain `None` instead
  gives you `_NFC_TAG_NO_SPOOL` — a different path.

## Troubleshooting

- `FileNotFoundError: .../printer_data/logs/nfc_reader.log` on import → you
  skipped the root-logger `FileHandler` seed. Add the two-line stub (see Direct
  invocation) before importing anything from `nfc_gates`.
- `ModuleNotFoundError: No module named 'bus'` → you imported `nfc_manager` or
  `reader_factory`. Those only load inside Klipper; drive the sub-components.
- `ModuleNotFoundError: No module named 'nfc_gates'` → `sys.path` is missing
  `klippy/extras`, or you're not running from the repo root. `cd` to the repo
  root first (the driver derives the path from its own location, so it works
  from anywhere; the one-liner assumes cwd = repo root).
- Installer aborts with a message about the install directory → add
  `RFID_READER_ALLOW_DEV_PATH=yes`.
- `install.sh` seems to hang → it's waiting on an interactive prompt; use repair
  mode (`-r`) or pipe blank lines for the wizard.
