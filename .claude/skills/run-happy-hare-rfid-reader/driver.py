#!/usr/bin/env python3
# Runtime smoke driver for the EMU NFC Gate Reader Klipper plugin.
#
# This project is a Klipper "extras" plugin: on a real printer it runs inside
# Klipper on a Pi, polling physical NFC readers (PN532/PN7160/RC522) and
# handing tag reads to Happy Hare as G-code macro calls. None of that hardware
# exists in a dev container, and the hardware-facing modules (nfc_manager,
# reader_factory) need Klipper's own `bus` module to import at all.
#
# But the layer that PRs actually change — the decision + dispatch pipeline —
# is pure Python and imports cleanly off-printer:
#
#     raw poll (uid, spool_id)
#       -> GateState        (per-gate debounce: is this a real change?)
#         -> event tuple    (EVENT_CHANGED / EVENT_UID_ONLY / EVENT_REMOVED)
#           -> KlipperInterface  (event -> exact _NFC_* G-code macro string)
#             -> gcode.run_script(...)   <- what Happy Hare receives
#
# This driver wires the REAL GateState and REAL KlipperInterface together
# (only the Klipper printer/reactor/gcode objects are faked) and runs a few
# realistic filament-scan scenarios, printing the actual G-code each one emits.
# It is the fast way to see a change to the event/dispatch logic actually work
# without a printer. Run it with:  python3 <this file>
#
# Exit status is nonzero if any scenario's emitted G-code doesn't match what is
# expected, so it doubles as a CI smoke check.

import logging
import os
import sys
import tempfile

# --- Make the plugin importable the way Klipper's module loader sees it ------
# Klipper puts klippy/extras on the import path and imports nfc_gates as a
# package. We do the same. log.py builds a module-level logger AT IMPORT TIME
# by locating klippy.log's directory via the root logger, falling back to
# ~/printer_data/logs (absent off-printer -> import crash). Attaching a stub
# FileHandler to the root logger first makes it resolve into a temp dir.
_LOG_DIR = tempfile.mkdtemp(prefix="nfc_driver_logs_")
logging.getLogger().addHandler(logging.FileHandler(os.path.join(_LOG_DIR, "klippy.log")))

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(_REPO, "klippy", "extras"))

from nfc_gates.gate_state import (  # noqa: E402
    DIRECT_METADATA_SPOOL, EVENT_CHANGED, EVENT_REMOVED, EVENT_UID_ONLY,
    GateState,
)
from nfc_gates.klipper_interface import KlipperInterface  # noqa: E402


# --- Minimal Klipper stand-ins (the only things faked) -----------------------
class FakeGCode:
    def __init__(self):
        self.scripts = []

    def run_script(self, script):
        self.scripts.append(script)


class FakeReactor:
    """Runs registered callbacks synchronously so we can observe output."""
    def register_callback(self, cb):
        cb(None)


class FakePrinter:
    def __init__(self, gcode):
        self._objects = {"gcode": gcode}

    def lookup_object(self, name, default=None):
        return self._objects.get(name, default)

    def get_reactor(self):
        return FakeReactor()


# --- Driver ------------------------------------------------------------------
EVENT_NAMES = {
    EVENT_CHANGED: "CHANGED", EVENT_UID_ONLY: "UID_ONLY", EVENT_REMOVED: "REMOVED",
}


def run_scenario(title, reads, absent_threshold=3, name="lane0",
                 spoolman_enabled=True):
    """Feed a sequence of (uid, spool_id[, meta]) reads through the real
    GateState + KlipperInterface and return the list of G-code strings emitted.
    A uid of None models a poll where no tag was seen."""
    print(f"\n=== {title} ===")
    gcode = FakeGCode()
    printer = FakePrinter(gcode)
    ki = KlipperInterface(printer, printer.get_reactor(), debug=2, name=name,
                          spoolman_enabled=spoolman_enabled)
    gs = GateState(gate=0, absent_threshold=absent_threshold)

    for read in reads:
        uid, spool_id = read[0], read[1]
        meta = read[2] if len(read) > 2 else None
        event = gs.process_read(uid, spool_id)
        shown_uid = uid if uid is not None else "<no tag>"
        if event is None:
            print(f"  poll uid={shown_uid:<12} spool={spool_id!r:<16} -> (no state change)")
            continue
        etype, gate, ev_uid, ev_spool = event
        ki.dispatch(etype, gate, ev_uid, ev_spool, meta=meta)
        print(f"  poll uid={shown_uid:<12} spool={spool_id!r:<16} -> {EVENT_NAMES[etype]}")
        print(f"       G-code: {gcode.scripts[-1]}")
    return gcode.scripts


def main():
    failures = []

    def expect(label, got, needle):
        if needle not in got:
            failures.append(f"{label}: expected substring {needle!r} in {got!r}")

    # 1. A spool tracked in Spoolman is inserted, sits, then is pulled out.
    scripts = run_scenario(
        "Known Spoolman spool: insert, hold, remove",
        [("04A1B2C3", 42), ("04A1B2C3", 42), (None, None), (None, None), (None, None)],
    )
    expect("known-spool insert", scripts[0], "_NFC_SPOOL_CHANGED")
    expect("known-spool insert", scripts[0], "SPOOL_ID=42")
    expect("known-spool remove", scripts[-1], "_NFC_SPOOL_REMOVED")

    # 2. A rich/Bambu tag with embedded metadata but no Spoolman spool id.
    scripts = run_scenario(
        "Metadata-only tag (no Spoolman id): dispatch filament attributes",
        [("04DDEEFF", DIRECT_METADATA_SPOOL,
          {"material": "PLA", "color_hex": "FF8800", "brand": "Bambu",
           "min_temp": 190, "max_temp": 220})],
    )
    expect("metadata dispatch", scripts[0], "_NFC_SPOOL_CHANGED")
    expect("metadata dispatch", scripts[0], "MATERIAL=PLA")
    expect("metadata dispatch", scripts[0], "COLOR=FF8800")

    # 3. A tag whose UID isn't known to Spoolman -> _NFC_TAG_NO_SPOOL.
    scripts = run_scenario(
        "Unknown tag (UID not in Spoolman)",
        [("0499AABB", None)],
    )
    expect("unknown tag", scripts[0], "_NFC_TAG_NO_SPOOL")

    # 4. Debounce: a single dropped poll must NOT emit a removal (threshold 3).
    scripts = run_scenario(
        "Debounce: one missed poll does not trigger removal",
        [("04A1B2C3", 42), (None, None)],  # only 1 miss, threshold 3
    )
    if len(scripts) != 1:  # only the initial insert should have dispatched
        failures.append(f"debounce: expected 1 emission, got {len(scripts)}: {scripts}")

    print("\n" + "=" * 60)
    if failures:
        print(f"DRIVER RESULT: FAIL ({len(failures)} check(s))")
        for f in failures:
            print("  - " + f)
        return 1
    print("DRIVER RESULT: PASS — event pipeline emitted expected Happy Hare G-code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
