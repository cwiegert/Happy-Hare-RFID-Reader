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
    assert 'MMU_GATE_MAP GATE={gate} APPLY=1 QUIET=1' in text


def test_scan_jog_clear_cache_macro_is_defined():
    text = _macro_text()

    assert '[gcode_macro _NFC_GATE_CLEAR_CACHE]' in text
    assert 'MMU_GATE_MAP GATE={gate} SPOOLID=-1 AVAILABLE=1 QUIET=1' in text


def test_core_nfc_event_macros_are_defined():
    text = _macro_text()

    for macro in (
            '_NFC_SPOOL_CHANGED',
            '_NFC_SPOOL_REMOVED',
            '_NFC_TAG_NO_SPOOL',
            '_NFC_GATE_CLEAR_CACHE',
            '_NFC_SCAN_UNRESOLVED'):
        assert '[gcode_macro %s]' % macro in text


def test_state_changing_mmu_gate_map_calls_are_quiet():
    for line in _macro_text().splitlines():
        stripped = line.strip()
        if stripped.startswith('MMU_GATE_MAP '):
            assert 'QUIET=1' in stripped


def test_scan_finish_does_not_dump_hh_gate_map():
    text = _macro_text()

    assert '_NFC_SCAN_FINISH' not in text
    assert 'DWELL_MS' not in text
    assert 'G4 P{dwell_ms}' not in text
    assert 'scan-jog finished for gate {gate}; Happy Hare gate map:' not in text
    assert '\n    MMU_GATE_MAP\n' not in text


def test_scan_unresolved_macro_clears_stale_filament_fields():
    text = _macro_text()

    assert '[gcode_macro _NFC_SCAN_UNRESOLVED]' in text
    assert (
        'MMU_GATE_MAP GATE={gate} SPOOLID=-1 NAME=Unknown '
        'MATERIAL=Unknown COLOR=FFFFFF TEMP=0 AVAILABLE=1 SYNC=1 QUIET=1'
    ) in text
    assert 'MMU_GATE_MAP GATE={gate} APPLY=1 QUIET=1' in text


def test_ok_macro_messages_remain_plain_gcode_safe_text():
    text = _macro_text()

    assert '[OK] NFC gate %d: spool %d detected' in text
    assert '[OK] NFC gate %d: tag metadata detected' in text
    assert '<span' not in text
