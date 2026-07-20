# tests/python/test_klipper_interface.py
#
# KlipperInterface turns gate events into the exact G-code macro calls
# documented in nfc_macros.cfg (_NFC_SPOOL_CHANGED / _NFC_SPOOL_REMOVED /
# _NFC_TAG_NO_SPOOL). Getting these strings wrong is a silent regression —
# Klipper just runs whatever macro name/args come through — so these tests
# pin the exact script text for each event type and flag combination.
from nfc_gates.gate_state import EVENT_CHANGED, EVENT_REMOVED, EVENT_UID_ONLY
from nfc_gates.klipper_interface import KlipperInterface

from fakes import FakePrinter


def _dispatch(printer, event_type, gate, uid_hex, spool_id, **kwargs):
    ki = KlipperInterface(printer, printer.get_reactor(), debug=2, name="lane0")
    ki.dispatch(event_type, gate, uid_hex, spool_id, **kwargs)
    return printer.gcode.scripts


def test_changed_with_known_spool_dispatches_spool_id_script():
    printer = FakePrinter()
    scripts = _dispatch(printer, EVENT_CHANGED, 3, "04AABBCC", 7)
    assert scripts == [
        "_NFC_SPOOL_CHANGED GATE=3 READER=lane0 SPOOL_ID=7 UID=04AABBCC"
    ]


def test_changed_with_auto_created_and_scan_finish_flags():
    printer = FakePrinter()
    scripts = _dispatch(
        printer, EVENT_CHANGED, 3, "04AABBCC", 7,
        auto_created=True, scan_finish=True,
    )
    assert scripts == [
        "_NFC_SPOOL_CHANGED GATE=3 READER=lane0 SPOOL_ID=7 UID=04AABBCC"
        " AUTO_CREATED=1 SCAN_FINISH=1"
    ]


def test_changed_metadata_only_includes_all_fields():
    printer = FakePrinter()
    meta = {
        "material": "PLA",
        "color_hex": "FF0000",
        "brand": "Generic Brand",
        "min_temp": 190,
        "max_temp": 220,
        "diameter_mm": 1.75,
        "weight_g": 1000,
    }
    scripts = _dispatch(printer, EVENT_CHANGED, 0, "04AABBCC", None, meta=meta)
    assert len(scripts) == 1
    script = scripts[0]
    assert script.startswith("_NFC_SPOOL_CHANGED GATE=0 READER=lane0")
    assert "NAME=Generic_Brand_PLA" in script
    assert "MATERIAL=PLA" in script
    assert "COLOR=FF0000" in script
    assert "BRAND=Generic_Brand" in script
    assert "MIN_TEMP=190" in script
    assert "TEMP=220" in script
    assert "DIAMETER=1.75" in script
    assert "WEIGHT=1000" in script
    assert "UID=04AABBCC" in script


def test_changed_metadata_only_with_no_metadata_leaves_fields_blank():
    printer = FakePrinter()
    scripts = _dispatch(printer, EVENT_CHANGED, 0, "04AABBCC", None, meta=None)
    script = scripts[0]
    assert "NAME= MATERIAL= COLOR= BRAND=" in script
    assert "MIN_TEMP= TEMP= DIAMETER= WEIGHT=" in script


def test_uid_only_dispatches_no_spool_script():
    printer = FakePrinter()
    scripts = _dispatch(printer, EVENT_UID_ONLY, 2, "04AABBCC", None)
    assert scripts == ["_NFC_TAG_NO_SPOOL GATE=2 READER=lane0 UID=04AABBCC"]


def test_uid_only_flags_spoolman_disabled():
    printer = FakePrinter()
    ki = KlipperInterface(printer, printer.get_reactor(), name="lane0",
                          spoolman_enabled=False)
    ki.dispatch(EVENT_UID_ONLY, 2, "04AABBCC", None)
    assert printer.gcode.scripts == [
        "_NFC_TAG_NO_SPOOL GATE=2 READER=lane0 UID=04AABBCC SPOOLMAN_DISABLED=1"
    ]


def test_removed_dispatches_removed_script():
    printer = FakePrinter()
    scripts = _dispatch(printer, EVENT_REMOVED, 1, None, 7)
    assert scripts == ["_NFC_SPOOL_REMOVED GATE=1 READER=lane0"]


def test_unknown_event_type_dispatches_nothing():
    printer = FakePrinter()
    scripts = _dispatch(printer, "not_a_real_event", 0, "04AABBCC", None)
    assert scripts == []


def test_gcode_exception_is_swallowed_not_raised():
    """A macro that doesn't exist (e.g. missing nfc_macros.cfg include) must
    not take down the reactor callback — dispatch logs and moves on."""

    class ExplodingGCode:
        def run_script(self, script):
            raise Exception("Unknown command")

    printer = FakePrinter(gcode=ExplodingGCode())
    ki = KlipperInterface(printer, printer.get_reactor(), name="lane0")
    ki.dispatch(EVENT_CHANGED, 0, "04AABBCC", 7)  # must not raise


def test_macro_value_sanitizes_whitespace_and_special_characters():
    assert KlipperInterface._macro_value("PLA  Matte!") == "PLA_Matte"
    assert KlipperInterface._macro_value(None) == ""
    assert KlipperInterface._macro_value("") == ""


def test_metadata_name_prefixes_brand_when_not_already_present():
    printer = FakePrinter()
    ki = KlipperInterface(printer, printer.get_reactor(), name="lane0")
    name = ki._metadata_name({"material": "PLA", "brand": "Generic"})
    assert name == "Generic_PLA"


def test_metadata_name_does_not_duplicate_existing_prefix():
    printer = FakePrinter()
    ki = KlipperInterface(printer, printer.get_reactor(), name="lane0")
    name = ki._metadata_name({"material": "Generic PLA", "brand": "Generic"})
    assert name == "Generic_PLA"


def test_metadata_name_normalizes_bambu_lab_vendor_prefix():
    printer = FakePrinter()
    ki = KlipperInterface(printer, printer.get_reactor(), name="lane0")
    name = ki._metadata_name({"material_detail": "Basic PLA", "vendor": "bambu_lab"})
    assert name == "Bambu_Basic_PLA"
