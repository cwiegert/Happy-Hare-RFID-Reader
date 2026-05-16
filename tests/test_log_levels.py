"""
tests/test_log_levels.py
========================
Unit tests for log._normalise_level — the function that maps
console_log_level config values to Python logging levels.

Run from the project root:
    python3 tests/test_log_levels.py
"""

import sys
import os
import logging
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                '..', 'klippy', 'extras', 'nfc_gates'))

# Patch logging.FileHandler with a real class (not a Mock) so that
# isinstance(handler, logging.FileHandler) keeps working when pytest
# installs its own root-logger handlers during collection.
class _FakeFileHandler(logging.Handler):
    def __init__(self, *a, **k): super().__init__()
    def emit(self, record): pass

with unittest.mock.patch('logging.FileHandler', _FakeFileHandler):
    import log as _log_module

_normalise_level = _log_module._normalise_level


def test_numeric_string_1_is_error():
    assert _normalise_level('1') == logging.ERROR

def test_numeric_string_2_is_warning():
    assert _normalise_level('2') == logging.WARNING

def test_numeric_string_3_is_info():
    assert _normalise_level('3') == logging.INFO

def test_int_1_is_error():
    assert _normalise_level(1) == logging.ERROR

def test_int_2_is_warning():
    assert _normalise_level(2) == logging.WARNING

def test_int_3_is_info():
    assert _normalise_level(3) == logging.INFO

def test_int_4_is_debug():
    assert _normalise_level(4) == logging.DEBUG

def test_string_error_is_error():
    assert _normalise_level('error') == logging.ERROR

def test_string_warning_is_warning():
    assert _normalise_level('warning') == logging.WARNING

def test_string_warn_is_warning():
    assert _normalise_level('warn') == logging.WARNING

def test_string_info_is_info():
    assert _normalise_level('info') == logging.INFO

def test_string_debug_is_debug():
    assert _normalise_level('debug') == logging.DEBUG

def test_uppercase_warning():
    assert _normalise_level('WARNING') == logging.WARNING

def test_mixed_case_error():
    assert _normalise_level('Error') == logging.ERROR

def test_unknown_string_returns_default():
    assert _normalise_level('bogus') == logging.WARNING

def test_unknown_string_custom_default():
    assert _normalise_level('bogus', logging.ERROR) == logging.ERROR

def test_unknown_int_returns_default():
    assert _normalise_level(99) == logging.WARNING

def test_level_ordering():
    assert _normalise_level(1) > _normalise_level(2) > _normalise_level(3)


if __name__ == '__main__':
    tests  = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL  {fn.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
