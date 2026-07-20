# Tests

Two independent suites, run together via `bash tests/run_tests.sh`:

- **`tests/bash/`** — unit tests for `install.sh` / `uninstall.sh` helper
  functions. No external dependencies (no bats) — a small harness in
  `tests/bash/lib/harness.sh` provides `assert_*` helpers and runs each
  `test_*` function in an isolated sandbox dir.

  `install.sh` is a top-to-bottom script with no `main` guard, so a test
  file extracts just the function(s) it needs via
  `tests/bash/lib/extract_functions.sh` rather than sourcing the whole
  script (which would run the interactive installer). If a function is
  renamed, extraction fails loudly instead of silently testing nothing.

  Run directly: `bash tests/bash/run_all.sh`
  Run one file: `bash tests/bash/test_install_managed_macros.sh`

- **`tests/python/`** — pytest unit tests for `klippy/extras/nfc_gates`.
  Modules that only need pure logic (e.g. `gate_state.py`) are tested with
  no mocking. Modules that talk to Klipper (e.g. `klipper_interface.py`)
  are tested against small fakes in `tests/python/fakes.py`
  (`FakePrinter`/`FakeReactor`/`FakeGCode`) rather than a real Klipper
  install.

  `tests/python/conftest.py` also works around an import-time side effect:
  `nfc_gates/log.py` builds its logger at import time by locating
  `klippy.log`'s directory through the root logger, falling back to
  `~/printer_data/logs` — which doesn't exist off-printer. conftest attaches
  a stub `FileHandler` pointing at a temp dir to the root logger *before*
  any `nfc_gates` module is imported, so that fallback never triggers.

  Setup: `pip install -r tests/python/requirements-dev.txt`
  Run: `pytest tests/python`

## What's covered so far

This is a scaffold, not full coverage. Included as worked examples of each
pattern:

- `install_managed_macros` (the read-only `nfc_macros.cfg` symlink logic)
  and `merge_config` (the non-destructive merge for user-owned config
  files) in `install.sh`.
- `GateState` (per-gate debounce state machine) and `KlipperInterface`
  (event → G-code macro dispatch) in `klippy/extras/nfc_gates/`.

Extending either suite means adding a new `test_*.sh` / `test_*.py` file
next to the existing ones — no registration step needed, both runners
discover files by naming convention.
