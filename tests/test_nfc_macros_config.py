"""
Static checks for shipped NFC Happy Hare macros.

These tests do not parse Klipper config fully; they catch contract-level drift
between Python gcode.run_script() calls and macros shipped in nfc_macros.cfg.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MACROS = ROOT / 'config' / 'nfc_macros.cfg'


def _macro_text():
    return MACROS.read_text()


def test_no_spool_macro_clears_visible_filament_fields():
    text = _macro_text()

    assert '[gcode_macro _NFC_TAG_NO_SPOOL]' in text
    assert (
        'MMU_GATE_MAP GATE={gate} SPOOLID=-1 NAME=Unknown '
        'MATERIAL=Unknown COLOR=FFFFFF TEMP=0 AVAILABLE=1 SYNC=1 QUIET=1'
    ) in text
    assert 'MMU_GATE_MAP GATE={gate} APPLY=1' in text


def test_scan_jog_clear_cache_macro_is_defined():
    text = _macro_text()

    assert '[gcode_macro _NFC_GATE_CLEAR_CACHE]' in text
    assert (
        'MMU_GATE_MAP GATE={gate} SPOOLID=-1 NAME=Unknown '
        'MATERIAL=Unknown COLOR=FFFFFF00 AVAILABLE=1 QUIET=1'
    ) in text


def test_core_nfc_event_macros_are_defined():
    text = _macro_text()

    for macro in (
            '_NFC_SPOOL_CHANGED',
            '_NFC_SPOOL_REMOVED',
            '_NFC_TAG_NO_SPOOL',
            '_NFC_GATE_CLEAR_CACHE'):
        assert '[gcode_macro %s]' % macro in text
