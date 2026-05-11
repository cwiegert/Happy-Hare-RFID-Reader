"""
tests/test_vendor_contract.py
=============================
Contract tests for vendored lameandboard/rfid files.

These tests verify that the API surface of the vendored files matches exactly
what our adapter code in nfc_manager.py expects. If any test fails after an
upstream sync PR is opened, the adapter code must be reviewed before merging.

No network calls are made. Method signatures are checked via inspect.
parse_tag is exercised with synthetic in-process inputs only.
"""
import inspect
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klippy', 'extras'))

from nfc_gates.vendor.rfid_tag_parser import parse_tag, is_parse_error
from nfc_gates.vendor.lameandboard_spoolman import SpoolmanClient


# ---------------------------------------------------------------------------
# rfid_tag_parser contract
# ---------------------------------------------------------------------------

class TestParseTagContract:

    def test_parse_tag_callable(self):
        assert callable(parse_tag)

    def test_is_parse_error_callable(self):
        assert callable(is_parse_error)

    def test_parse_tag_accepts_raw_and_uid_hex(self):
        sig = inspect.signature(parse_tag)
        params = sig.parameters
        # First positional param is raw data
        assert len(params) >= 1
        # uid_hex must be an accepted keyword
        assert 'uid_hex' in params
        # Optional trace callback allows integrations to capture parser flow.
        assert 'trace' in params

    def test_parse_tag_blank_returns_dict_or_none(self):
        # Blank NTAG user memory — all zeros
        result = parse_tag(bytes(64), uid_hex='04AABBCCDDEE')
        assert result is None or isinstance(result, dict)

    def test_parse_tag_uid_preserved_when_present(self):
        result = parse_tag(bytes(64), uid_hex='04AABBCCDDEE')
        if isinstance(result, dict):
            # Successful parser output should preserve uid when it carries one.
            # Parse-error dicts are adapted by nfc_manager before resolution.
            if not is_parse_error(result):
                assert result.get('uid') == '04AABBCCDDEE'

    def test_parse_tag_openspool_json_ndef(self):
        # Construct a minimal NDEF Text record containing OpenSpool JSON
        payload_json = json.dumps({
            "version": 1,
            "brand": "eSUN",
            "material": "PLA",
            "color_hex": "FF5500",
            "weight": 1000,
        }).encode('utf-8')
        lang = b'en'
        status = bytes([0x02 | len(lang)])        # UTF-8, lang length
        text_payload = status + lang + payload_json
        # NDEF record: MB=1 ME=1 SR=1 TNF=1 type_len=1 payload_len type='T'
        header = bytes([0xD1, 0x01, len(text_payload), 0x54])
        ndef_msg = header + text_payload
        # NTAG TLV wrapper
        tlv = bytes([0x03, len(ndef_msg)]) + ndef_msg + bytes([0xFE])
        raw = tlv + bytes(max(0, 64 - len(tlv)))

        result = parse_tag(raw, uid_hex='04AABBCCDDEE')
        if result is not None:
            # Must have at least one metadata field recognisable to our adapter
            assert any(k in result for k in (
                'material', 'vendor', 'brand', 'color_hex', 'uid'))

    def test_parse_tag_result_is_dict_or_none(self):
        result = parse_tag(bytes(64), uid_hex='04001122334455')
        assert result is None or isinstance(result, dict)

    def test_parse_tag_trace_callback_is_optional(self):
        events = []
        result = parse_tag(
            bytes(64),
            uid_hex='04001122334455',
            trace=lambda level, msg, *args: events.append((level, msg, args)))
        assert result is None or isinstance(result, dict)
        assert events

    def test_is_parse_error_accepts_none(self):
        # Our adapter calls is_parse_error(current_tag.meta) which may be None
        result = is_parse_error(None)
        assert isinstance(result, bool)

    def test_is_parse_error_accepts_dict(self):
        result = is_parse_error({'uid': '04AABBCCDDEE'})
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# lameandboard_spoolman SpoolmanClient contract
# ---------------------------------------------------------------------------

class TestSpoolmanClientContract:

    def test_class_importable(self):
        assert SpoolmanClient is not None

    def test_constructor_accepts_base_url_and_timeout(self):
        # Our adapter calls: LBSpoolmanClient(base_url=url, timeout=self._timeout)
        sig = inspect.signature(SpoolmanClient.__init__)
        params = sig.parameters
        assert 'base_url' in params
        assert 'timeout' in params
        assert 'trace' in params

    def test_find_or_create_vendor_exists(self):
        assert callable(getattr(SpoolmanClient, 'find_or_create_vendor', None))

    def test_find_or_create_vendor_accepts_name(self):
        # Our adapter calls: lb.find_or_create_vendor(vendor_name)
        sig = inspect.signature(SpoolmanClient.find_or_create_vendor)
        params = list(sig.parameters.keys())
        # Expect self + name (positional)
        assert len(params) >= 2

    def test_find_or_create_filament_exists(self):
        assert callable(getattr(SpoolmanClient, 'find_or_create_filament', None))

    def test_find_or_create_filament_accepts_required_params(self):
        # Our adapter calls: lb.find_or_create_filament(name=material, vendor_id=vendor_id, ...)
        sig = inspect.signature(SpoolmanClient.find_or_create_filament)
        params = sig.parameters
        assert 'name' in params
        assert 'vendor_id' in params
        assert 'material' in params

    def test_create_spool_exists(self):
        assert callable(getattr(SpoolmanClient, 'create_spool', None))

    def test_create_spool_accepts_required_params(self):
        # Our adapter calls: lb.create_spool(filament_id=..., remaining_weight=..., extra={rfid_key: uid})
        sig = inspect.signature(SpoolmanClient.create_spool)
        params = sig.parameters
        assert 'filament_id' in params
        assert 'remaining_weight' in params
        assert 'extra' in params

    def test_auto_create_spool_still_present(self):
        # Our adapter calls this with uid_hex=None, then patches its configured
        # single UID field separately instead of using rfid_uid_N slots.
        assert callable(getattr(SpoolmanClient, 'auto_create_spool', None))

    def test_auto_create_spool_uses_max_temp_as_recommended(self, monkeypatch):
        requests = []

        def fake_fetch_spoolmandb_bambu():
            return []

        def fake_fetch_spoolmandb_materials():
            return {}

        monkeypatch.setattr(
            'nfc_gates.vendor.lameandboard_spoolman._fetch_spoolmandb_bambu',
            fake_fetch_spoolmandb_bambu)
        monkeypatch.setattr(
            'nfc_gates.vendor.lameandboard_spoolman._fetch_spoolmandb_materials',
            fake_fetch_spoolmandb_materials)

        client = SpoolmanClient('http://spoolman')

        def fake_req(method, path, body=None):
            requests.append((method, path, body))
            if method == 'GET' and path.startswith('/api/v1/vendor?'):
                return [{'id': 3, 'name': 'Bambu Lab'}]
            if method == 'GET' and path.startswith('/api/v1/filament?'):
                return []
            if method == 'POST' and path == '/api/v1/filament':
                return {'id': 10}
            if method == 'POST' and path == '/api/v1/spool':
                return {'id': 14}
            raise AssertionError((method, path, body))

        client._req = fake_req
        spool_id = client.auto_create_spool({
            'tag_format': 'bambu',
            'brand': 'Bambu Lab',
            'material': 'PLA',
            'material_detail': 'PLA Basic',
            'color_hex': '0086D6',
            'min_temp': 190,
            'max_temp': 230,
            'weight_g': 1000,
        }, uid_hex=None)

        assert spool_id == 14
        filament_posts = [
            body for method, path, body in requests
            if method == 'POST' and path == '/api/v1/filament'
        ]
        assert len(filament_posts) == 1
        assert filament_posts[0]['settings_extruder_temp'] == 230
        assert filament_posts[0]['settings_extruder_temp_max'] == 230
