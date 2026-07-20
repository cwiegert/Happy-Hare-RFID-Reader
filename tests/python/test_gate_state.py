# tests/python/test_gate_state.py
#
# GateState is the per-gate debounce state machine that turns raw poll
# results (uid_hex, spool_id) into the three events klipper_interface.py
# dispatches as G-code: EVENT_CHANGED, EVENT_UID_ONLY, EVENT_REMOVED. No
# Klipper objects involved — pure logic, so no fakes needed here.
from nfc_gates.gate_state import (
    DIRECT_METADATA_SPOOL,
    EVENT_CHANGED,
    EVENT_REMOVED,
    EVENT_UID_ONLY,
    GateState,
)


def test_new_tag_with_known_spool_emits_changed():
    gs = GateState(gate=0, absent_threshold=3)
    event = gs.process_read("04AABBCC", 7)
    assert event == (EVENT_CHANGED, 0, "04AABBCC", 7)


def test_new_tag_without_spool_emits_uid_only():
    gs = GateState(gate=1, absent_threshold=3)
    event = gs.process_read("04AABBCC", None)
    assert event == (EVENT_UID_ONLY, 1, "04AABBCC", None)


def test_same_tag_seen_again_emits_nothing():
    gs = GateState(gate=0, absent_threshold=3)
    gs.process_read("04AABBCC", 7)
    event = gs.process_read("04AABBCC", 7)
    assert event is None


def test_spool_id_resolving_later_emits_changed():
    """A tag read as uid-only, then later resolved to a spool ID, is a real
    state change and must re-fire even though the UID hasn't changed."""
    gs = GateState(gate=0, absent_threshold=3)
    gs.process_read("04AABBCC", None)
    event = gs.process_read("04AABBCC", 7)
    assert event == (EVENT_CHANGED, 0, "04AABBCC", 7)


def test_tag_swap_emits_changed_for_new_uid():
    gs = GateState(gate=0, absent_threshold=3)
    gs.process_read("04AABBCC", 7)
    event = gs.process_read("04DDEEFF", 9)
    assert event == (EVENT_CHANGED, 0, "04DDEEFF", 9)


def test_misses_below_threshold_emit_nothing():
    gs = GateState(gate=0, absent_threshold=3)
    gs.process_read("04AABBCC", 7)
    assert gs.process_read(None, None) is None
    assert gs.process_read(None, None) is None
    assert gs.miss_count == 2


def test_misses_reaching_threshold_emit_removed_with_last_spool():
    gs = GateState(gate=2, absent_threshold=3)
    gs.process_read("04AABBCC", 7)
    gs.process_read(None, None)
    gs.process_read(None, None)
    event = gs.process_read(None, None)
    assert event == (EVENT_REMOVED, 2, None, 7)


def test_removed_resets_state_so_next_tag_is_a_fresh_changed():
    gs = GateState(gate=0, absent_threshold=1)
    gs.process_read("04AABBCC", 7)
    gs.process_read(None, None)  # threshold=1 -> fires EVENT_REMOVED
    event = gs.process_read("04AABBCC", 7)
    assert event == (EVENT_CHANGED, 0, "04AABBCC", 7)


def test_misses_with_no_prior_tag_never_fire_removed():
    """miss_count still increments with no tag present (there's nothing
    tracking whether a gate has ever seen a tag), but EVENT_REMOVED only
    fires when there was a current_uid to remove."""
    gs = GateState(gate=0, absent_threshold=1)
    assert gs.process_read(None, None) is None
    assert gs.miss_count == 1


def test_scan_mode_suppresses_miss_counting():
    """scan_mode is used during active scan-jog passes, where a momentary
    gap must not be treated as the tag having left the gate."""
    gs = GateState(gate=0, absent_threshold=1)
    gs.process_read("04AABBCC", 7)
    event = gs.process_read(None, None, scan_mode=True)
    assert event is None
    assert gs.miss_count == 0


def test_direct_metadata_spool_emits_changed_once():
    gs = GateState(gate=0, absent_threshold=3)
    event = gs.process_read("04AABBCC", DIRECT_METADATA_SPOOL)
    assert event == (EVENT_CHANGED, 0, "04AABBCC", None)
    assert gs.current_tag.spool_id is DIRECT_METADATA_SPOOL


def test_direct_metadata_spool_repeat_emits_nothing():
    gs = GateState(gate=0, absent_threshold=3)
    gs.process_read("04AABBCC", DIRECT_METADATA_SPOOL)
    event = gs.process_read("04AABBCC", DIRECT_METADATA_SPOOL)
    assert event is None


def test_reset_clears_all_state():
    gs = GateState(gate=0, absent_threshold=3)
    gs.process_read("04AABBCC", 7)
    gs.miss_count = 2
    gs.reset()
    assert gs.current_uid is None
    assert gs.current_spool is None
    assert gs.current_tag is None
    assert gs.miss_count == 0
