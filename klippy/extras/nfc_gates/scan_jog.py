# klippy/extras/nfc_gates/scan_jog.py
#
# Scan-and-jog mode helpers for NFCGate.

from . import hh_status
from .gate_state import DIRECT_METADATA_SPOOL
from .log import info_both, logger


DECODE_RETRY_SETTLE_DELAY = 1.0
SCAN_JOG_SUBSTEPS = 3
LEFT_NEIGHBOR_CLEARANCE_MM = 75.0


def _color_tags(text):
    """Wrap console bracket tags with HTML color spans for the Klipper UI."""
    text = text.replace('[WARN]',  '<span style="color:#FFFF00">[WARN]</span>')
    text = text.replace('[OK]',    '<span style="color:#90EE90">[OK]</span>')
    text = text.replace('[REWIND]', '<span style="color:#90EE90">[REWIND]</span>')
    text = text.replace('[ERROR]', '<span style="color:#FF6060">[ERROR]</span>')
    return text


def manual_jog_scan(gate, gcmd):
    """Start scan-and-jog on demand, matching the automatic trigger path."""
    if gate._failed:
        msg = ("[ERROR] NFC[%s]: reader failed - "
               "run NFC GATE=%d INIT=1 first"
               % (gate._name, gate._gate))
        logger.error(msg)
        gcmd.respond_info(_color_tags(msg))
        return
    if is_printing(gate):
        msg = ("[WARN] NFC[%s]: print is active - "
               "cannot start scan-jog while printing" % gate._name)
        logger.warning(msg)
        gcmd.respond_info(_color_tags(msg))
        return
    hh = gate._read_hh_status()
    if hh.present and not hh.idle:
        msg = ("[WARN] NFC[%s]: Happy Hare is busy (action=%s) — "
               "wait for idle before starting scan-jog"
               % (gate._name, hh.action))
        logger.warning(msg)
        gcmd.respond_info(_color_tags(msg))
        return
    if gate.__class__._active_scan_gate is not None:
        msg = ("[WARN] NFC[%s]: gate %d is already scanning — "
               "only one gate may scan at a time"
               % (gate._name, gate.__class__._active_scan_gate))
        logger.warning(msg)
        gcmd.respond_info(_color_tags(msg))
        return
    if gate._scan_mode:
        msg = "[WARN] NFC[%s]: scan-jog already in progress for this gate" % gate._name
        logger.warning(msg)
        gcmd.respond_info(_color_tags(msg))
        return
    ok, reason, max_mm = gate._prepare_scan_jog()
    if not ok:
        msg = "[WARN] NFC[%s]: scan-jog not available while %s" % (
            gate._name, reason)
        logger.warning(msg)
        gcmd.respond_info(_color_tags(msg))
        return

    gate.reactor.update_timer(gate._poll_timer, gate.reactor.NEVER)
    start(gate, max_mm=max_mm)
    gcmd.respond_info(
        "[SCAN] NFC[%s]: scan-jog started for gate %d"
        " (max=%.0fmm  poll=%.2fs)"
        % (gate._name, gate._gate,
           gate._scan_max_mm, gate._scan_poll_interval))


def is_printing(gate):
    ps = gate.printer.lookup_object('print_stats', None)
    if ps is None:
        return False
    return ps.get_status(0).get('state', '') == 'printing'


def get_speed(gate):
    """Return gear_short_move_speed from Happy Hare, or 80 mm/s as fallback."""
    mmu = gate.printer.lookup_object('mmu', None)
    if mmu is not None:
        speed = getattr(mmu, 'gear_short_move_speed', None)
        if speed is not None:
            try:
                speed = float(speed)
                if speed > 0.0:
                    return speed
            except (TypeError, ValueError):
                pass
    return 80.0


def chunk_interval(gate, mm):
    """Return the time to wait before issuing the next scan chunk."""
    return abs(mm) / get_speed(gate)


def chunk_dwell(gate):
    """Return the stationary read window after each scan substep."""
    reads = max(1, int(getattr(gate, '_scan_reads_per_position', 3)))
    return reads * gate._scan_poll_interval


def substep_distance(gate):
    """Return the scan-jog submove distance.

    MMU_TEST_MOVE defaults to WAIT=1 in Happy Hare, so reads happen after the
    move returns, not while the spool is moving.  Substeps create physical read
    positions inside each configured scan_jog_mm chunk.
    """
    return gate._scan_jog_mm / float(SCAN_JOG_SUBSTEPS)


def next_event_time(gate, mm):
    """Return when it is safe to read after a queued scan chunk."""
    return gate.reactor.monotonic() + max(
        chunk_interval(gate, mm) + chunk_dwell(gate),
        gate._scan_poll_interval)


def sync_spoolman_before_scan(gate):
    """Ask Happy Hare to sync Spoolman before scan-jog changes the lane."""
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return
    try:
        if gate._debug >= 3:
            logger.info(
                "nfc_gate: [%s] gate %d scan mode — syncing HH Spoolman "
                "state before scan-jog",
                gate._name, gate._gate)
        gcode.run_script("MMU_SPOOLMAN SYNC=1 QUIET=1")
    except Exception as e:
        logger.warning(
            "nfc_gate: [%s] gate %d scan mode — MMU_SPOOLMAN SYNC failed: %s",
            gate._name, gate._gate, e)


def clear_hh_gate_cache(gate):
    """Mark the gate loaded but unknown before scan-jog resolves the spool."""
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return
    try:
        if gate._debug >= 3:
            logger.info(
                "nfc_gate: [%s] gate %d scan mode — clearing HH gate cache "
                "before scan-jog",
                gate._name, gate._gate)
        gcode.run_script("_NFC_GATE_CLEAR_CACHE GATE=%d" % gate._gate)
    except Exception as e:
        logger.warning(
            "nfc_gate: [%s] gate %d scan mode — _NFC_GATE_CLEAR_CACHE failed: %s",
            gate._name, gate._gate, e)


def run_pending_hh_prep(gate):
    """Run HH prep once from the scan timer, outside the hook call stack."""
    if not getattr(gate, '_scan_hh_prep_pending', False):
        return
    gate._scan_hh_prep_pending = False
    clear_hh_gate_cache(gate)
    sync_spoolman_before_scan(gate)


def get_active_gate(gate):
    """Return Happy Hare's currently selected gate, or -1 if unavailable."""
    hh = gate._read_hh_status()
    if not hh.present:
        return -1
    return hh.active_gate


def restore_active_gate(gate):
    """Restore the Happy Hare gate that was selected before scan-jog."""
    previous_gate = getattr(gate, '_scan_previous_active_gate', -1)
    gate._scan_previous_active_gate = -1
    if previous_gate < 0 or previous_gate == gate._gate:
        return
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return
    try:
        gcode.run_script("MMU_SELECT GATE=%d" % previous_gate)
    except Exception as e:
        logger.warning(
            "nfc_gate: [%s] gate %d scan mode — failed to restore "
            "Happy Hare selected gate %d: %s",
            gate._name, gate._gate, previous_gate, e)


def resume_poll_after_rewind(gate):
    """Restart regular polling after the queued rewind move can finish."""
    delay = gate._poll_interval
    if gate._scan_mm_total > 0.0:
        delay += chunk_interval(gate, gate._scan_mm_total)
    gate.reactor.update_timer(
        gate._poll_timer,
        gate.reactor.monotonic() + delay)


def start(gate, max_mm=None):
    if max_mm is not None:
        gate._scan_max_mm = float(max_mm)
    gate.__class__._active_scan_gate = gate._gate
    gate._scan_mode = True
    gate._scan_mm_total = 0.0
    gate._scan_next_chunk_time = gate.reactor.monotonic()
    gate._scan_position_reads_done = 0
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_uid = None
    gate._hh_seed_spool_id = None
    gate._hh_seed_available = False
    gate._scan_found_event = None
    gate._scan_previous_uid = gate._state.current_uid
    gate._scan_previous_spool = gate._state.current_spool
    gate._state.current_uid   = None  # force changed event on first read
    gate._state.current_spool = None
    gate._hh_load_paused = False
    gate._scan_gate_selected = False  # deferred to first jog (must run from timer, not GCode handler)
    gate._scan_hh_prep_pending = True

    gate._scan_timer = gate.reactor.register_timer(
        gate._scan_step_event,
        gate.reactor.monotonic())
    if gate._debug >= 3:
        logger.info(
            "nfc_gate: [%s] gate %d scan mode started — "
            "chunk=%.1fmm substep=%.1fmm max=%.1fmm speed=%.1fmm/s "
            "reads_per_position=%d poll=%.2fs",
            gate._name, gate._gate,
            gate._scan_jog_mm, substep_distance(gate), gate._scan_max_mm,
            get_speed(gate),
            max(1, int(getattr(gate, '_scan_reads_per_position', 3))),
            gate._scan_poll_interval)


def step_event(gate, eventtime):
    if not gate._scan_mode:
        return gate.reactor.NEVER

    if is_printing(gate):
        logger.warning(
            "nfc_gate: [%s] scan mode: print started — aborting",
            gate._name)
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    now = gate.reactor.monotonic()

    retry_poll = decode_retry_in_progress(gate)
    if retry_poll and now < gate._scan_next_chunk_time:
        if gate._debug >= 3:
            logger.info(
                "nfc_gate: [%s] gate %d decode retry waiting %.2fs "
                "before polling at scan position %.1f / %.1fmm",
                gate._name, gate._gate,
                gate._scan_next_chunk_time - now,
                gate._scan_mm_total, gate._scan_max_mm)
        return gate._scan_next_chunk_time
    if retry_poll:
        log_decode_retry_poll_start(gate)
    elif not decode_retry_exhausted(gate) and now < gate._scan_next_chunk_time:
        if gate._debug >= 3:
            logger.info(
                "nfc_gate: [%s] gate %d waiting %.2fs before next "
                "stopped-position read at scan position %.1f / %.1fmm",
                gate._name, gate._gate,
                gate._scan_next_chunk_time - now,
                gate._scan_mm_total, gate._scan_max_mm)
        return gate._scan_next_chunk_time

    run_pending_hh_prep(gate)

    try:
        tag_found = gate._poll()
    except Exception:
        logger.exception("nfc_gate: [%s] scan step poll error", gate._name)
        msg = "[ERROR] NFC[%d]: scan poll failed" % gate._gate
        logger.error(msg)
        gate._console(msg)
        tag_found = False

    if tag_found and handle_left_neighbor_interference(gate):
        if not gate._scan_mode:
            return gate.reactor.NEVER
        return gate.reactor.monotonic() + gate._scan_poll_interval

    if retry_poll:
        log_decode_retry_poll_result(gate, tag_found)
    elif not tag_found:
        gate._scan_position_reads_done += 1

    if tag_found:
        if current_tag_decode_incomplete(gate):
            if retry_incomplete_decode(gate, now):
                return gate.reactor.monotonic() + gate._scan_poll_interval
            if decode_retry_exhausted(gate):
                resume_scan_after_decode_retry(gate, now)
                if gate._scan_mm_total < gate._scan_max_mm:
                    return now + gate._scan_poll_interval
            else:
                gate._finish_scan()
                return gate.reactor.NEVER
        else:
            gate._finish_scan()
            return gate.reactor.NEVER

    if decode_retry_in_progress(gate):
        if continue_decode_retry(gate, now):
            return gate.reactor.monotonic() + gate._scan_poll_interval
        resume_scan_after_decode_retry(gate, now)
        if gate._scan_mm_total < gate._scan_max_mm:
            return now + gate._scan_poll_interval

    reads_per_position = max(
        1, int(getattr(gate, '_scan_reads_per_position', 3)))
    if (not decode_retry_in_progress(gate)
            and gate._scan_position_reads_done < reads_per_position):
        if gate._debug >= 3:
            logger.info(
                "nfc_gate: [%s] gate %d stopped-position read %d/%d "
                "found no tag at scan position %.1f / %.1fmm",
                gate._name, gate._gate,
                gate._scan_position_reads_done, reads_per_position,
                gate._scan_mm_total, gate._scan_max_mm)
        return gate.reactor.monotonic() + gate._scan_poll_interval

    if gate._scan_mm_total >= gate._scan_max_mm:
        if gate._scan_found_event is not None:
            msg = ("NFC[%d]: scan reached max distance after decode retries; "
                   "using best incomplete result" % gate._gate)
            logger.warning(msg)
            gate._console("[WARN] " + msg)
            gate._finish_scan()
            return gate.reactor.NEVER
        logger.warning(
            "nfc_gate: [%s] scan mode: no tag after %.1fmm — rewinding",
            gate._name, gate._scan_mm_total)
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    # MMU_TEST_MOVE blocks until the move completes, so reads are placed at
    # stopped positions.  Queue one physical substep after the configured
    # number of reads at the current position has been exhausted.
    if now >= gate._scan_next_chunk_time:
        remaining = gate._scan_max_mm - gate._scan_mm_total
        chunk = min(substep_distance(gate), remaining)
        next_position = gate._scan_mm_total + chunk
        msg = ("NFC[%d]: moving %.1fmm  scan position %.1f / %.1fmm "
               "(substep of %.1fmm)"
               % (gate._gate, chunk, next_position, gate._scan_max_mm,
                  gate._scan_jog_mm))
        logger.info(msg)
        gate._console(msg)
        if gate._debug >= 4:
            logger.debug("NFC[%d]: run_script MMU_TEST_MOVE MOVE=%.2f QUIET=1",
                         gate._gate, chunk)
        gate._run_jog(chunk)
        gate._scan_mm_total += chunk
        gate._scan_position_reads_done = 0
        gate._scan_next_chunk_time = (
            gate.reactor.monotonic() + gate._scan_poll_interval)
        logger.info(
            "NFC[%d]: move queued %.1fmm  scan position %.1f / %.1fmm",
            gate._gate, chunk, gate._scan_mm_total, gate._scan_max_mm)

    return gate._scan_next_chunk_time


def current_tag_decode_incomplete(gate):
    tag = gate._state.current_tag
    if tag is None:
        return False
    if (gate._state.current_spool is not None
            and gate._state.current_spool is not DIRECT_METADATA_SPOOL):
        return False
    return bool(getattr(tag, 'read_incomplete', False))


def read_uid_from_scan_event(gate):
    event = getattr(gate, '_scan_found_event', None)
    if event is not None and len(event) >= 3:
        return event[2]
    return gate._state.current_uid


def read_spool_from_scan_event(gate):
    event = getattr(gate, '_scan_found_event', None)
    if event is not None and len(event) >= 4:
        return event[3]
    return gate._state.current_spool


def known_uid_for_gate(gate, target_gate):
    left_nfc = gate._nfc_gate_for_gate_number(target_gate)
    if left_nfc is None:
        if gate._debug >= 4:
            logger.debug("NFC[%d]: left gate %d has no NFC instance",
                         gate._gate, target_gate)
        return None
    left_uid = getattr(left_nfc._state, 'current_uid', None)
    if not left_uid:
        if gate._debug >= 4:
            logger.debug("NFC[%d]: left gate %d NFC cache has no uid",
                         gate._gate, target_gate)
        return None

    left_hh = hh_status.read(gate.printer, target_gate)
    if left_hh.present and not left_hh.available:
        if gate._debug >= 3:
            logger.info(
                "nfc_gate: [%s] gate %d — left gate %d uid=%s suppressed: "
                "HH reports gate empty (status=%d)",
                gate._name, gate._gate, target_gate, left_uid, left_hh.status)
        return None
    if gate._debug >= 4:
        logger.debug(
            "NFC[%d]: left gate %d uid=%s hh_available=%s",
            gate._gate, target_gate, left_uid,
            left_hh.available if left_hh.present else "hh-absent")
    return left_uid


def is_left_neighbor_interference(gate, uid):
    if gate._gate <= 0 or not uid:
        return False
    left_uid = known_uid_for_gate(gate, gate._gate - 1)
    result = left_uid is not None and left_uid == uid
    if gate._debug >= 4:
        logger.debug(
            "NFC[%d]: interference check uid=%s left_uid=%s → %s",
            gate._gate, uid, left_uid, "match" if result else "no match")
    return result


def left_neighbor_already_shifted(gate, left_gate, uid):
    return (
        getattr(gate, '_scan_left_neighbor_shifted', False)
        and getattr(gate, '_scan_left_neighbor_gate', -1) == left_gate
        and getattr(gate, '_scan_left_neighbor_uid', None) == uid)


def clear_false_scan_result(gate):
    gate._scan_found_event = None
    gate._state.current_uid = None
    gate._state.current_spool = None
    gate._state.current_tag = None
    gate._state.miss_count = 0
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0


def shift_left_neighbor(gate, left_gate, uid):
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return False
    try:
        gcode.run_script(
            "MMU_SELECT GATE=%d\n"
            "MMU_TEST_MOVE MOVE=%.2f QUIET=1\n"
            "M400\n"
            "MMU_SELECT GATE=%d"
            % (left_gate, LEFT_NEIGHBOR_CLEARANCE_MM, gate._gate))
    except Exception as e:
        logger.warning(
            "nfc_gate: [%s] gate %d scan mode — failed to clear left "
            "neighbor gate %d from reader field: %s",
            gate._name, gate._gate, left_gate, e)
        return False
    gate._scan_left_neighbor_gate = left_gate
    gate._scan_left_neighbor_shift_mm = LEFT_NEIGHBOR_CLEARANCE_MM
    gate._scan_left_neighbor_shifted = True
    gate._scan_left_neighbor_uid = uid
    if gate._debug >= 3:
        logger.info(
            "nfc_gate: [%s] gate %d scan mode — left neighbor gate %d "
            "shifted %.1fmm to clear reader field",
            gate._name, gate._gate, left_gate, LEFT_NEIGHBOR_CLEARANCE_MM)
    return True


def restore_left_neighbor(gate):
    if not getattr(gate, '_scan_left_neighbor_shifted', False):
        return
    left_gate = getattr(gate, '_scan_left_neighbor_gate', -1)
    shift_mm = getattr(gate, '_scan_left_neighbor_shift_mm', 0.0)
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_uid = None
    if left_gate < 0 or shift_mm <= 0.0:
        return
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return
    try:
        gcode.run_script(
            "MMU_SELECT GATE=%d\n"
            "MMU_TEST_MOVE MOVE=%.2f QUIET=1\n"
            "M400\n"
            "MMU_SELECT GATE=%d"
            % (left_gate, -shift_mm, gate._gate))
    except Exception as e:
        logger.warning(
            "nfc_gate: [%s] gate %d scan mode — failed to restore left "
            "neighbor gate %d by %.1fmm: %s",
            gate._name, gate._gate, left_gate, shift_mm, e)
        gate._console(
            "[WARN] NFC[%d]: failed to restore left neighbor gate %d — "
            "move it back manually" % (gate._gate, left_gate))
        return
    if gate._debug >= 3:
        logger.info(
            "nfc_gate: [%s] gate %d scan mode — left neighbor gate %d "
            "restored by %.1fmm",
            gate._name, gate._gate, left_gate, shift_mm)


def handle_left_neighbor_interference(gate):
    if not getattr(gate, '_scan_mode', False) or gate._gate <= 0:
        return False

    uid = read_uid_from_scan_event(gate)
    if not uid or not is_left_neighbor_interference(gate, uid):
        if gate._debug >= 3 and uid:
            logger.info(
                "nfc_gate: [%s] gate %d scan mode — read uid=%s, "
                "no left-neighbor interference",
                gate._name, gate._gate, uid)
        return False

    left_gate = gate._gate - 1
    spool = read_spool_from_scan_event(gate)
    if left_neighbor_already_shifted(gate, left_gate, uid):
        msg = (
            "NFC[%d]: still reading left neighbor gate %d after %.0fmm "
            "clearance; check reader position, tag placement, or lane spacing"
            % (gate._gate, left_gate,
               getattr(gate, '_scan_left_neighbor_shift_mm',
                       LEFT_NEIGHBOR_CLEARANCE_MM)))
        logger.warning("%s", msg)
        gate._console("[WARN] " + msg)
        clear_false_scan_result(gate)
        gate._rewind_and_exit_scan()
        return True

    logger.warning(
        "nfc_gate: [%s] gate %d scan mode — uid=%s spool=%s belongs to "
        "left neighbor gate %d; moving neighbor out of reader field",
        gate._name, gate._gate, uid, spool, left_gate)
    gate._console(
        "[WARN] NFC[%d]: read left neighbor gate %d; clearing neighbor "
        "from reader field" % (gate._gate, left_gate))
    if not shift_left_neighbor(gate, left_gate, uid):
        msg = (
            "NFC[%d]: failed to clear left neighbor gate %d; aborting scan "
            "to avoid assigning the neighbor spool"
            % (gate._gate, left_gate))
        logger.warning("%s", msg)
        gate._console("[WARN] " + msg)
        clear_false_scan_result(gate)
        gate._rewind_and_exit_scan()
        return True
    clear_false_scan_result(gate)
    gate._scan_next_chunk_time = (
        gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY)
    return True


def disconnect_cleanup(gate):
    """Leave scan-jog state coherent when Klippy disconnects mid-scan."""
    if getattr(gate, '_scan_left_neighbor_shifted', False):
        logger.warning(
            "nfc_gate: [%s] gate %d disconnect during left-neighbor "
            "clearance; attempting to restore gate %d before clearing scan "
            "state",
            gate._name, gate._gate,
            getattr(gate, '_scan_left_neighbor_gate', -1))
        restore_left_neighbor(gate)
    gate._scan_mode = False
    gate._scan_timer = None
    gate._scan_found_event = None
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_uid = None


def reset_uid_only_read(gate, uid):
    if (gate._state.current_uid == uid
            and gate._state.current_spool in (None, DIRECT_METADATA_SPOOL)):
        gate._state.current_uid = None
        gate._state.current_spool = None
        gate._state.miss_count = 0


def decode_retry_config(gate):
    max_rounds = max(0, int(getattr(gate, '_scan_decode_retry_rounds', 5)))
    max_attempts = max_rounds * 2
    retry_mm = max(0.0, float(getattr(gate, '_scan_decode_retry_mm', 2.0)))
    return max_attempts, retry_mm


def decode_retry_in_progress(gate):
    max_attempts, retry_mm = decode_retry_config(gate)
    return (
        max_attempts > 0 and retry_mm > 0.0
        and gate._scan_decode_retry_uid is not None
        and gate._scan_decode_retry_attempts > 0
        and gate._scan_decode_retry_attempts < max_attempts)


def decode_retry_exhausted(gate):
    max_attempts, retry_mm = decode_retry_config(gate)
    return (
        max_attempts > 0 and retry_mm > 0.0
        and gate._scan_decode_retry_uid is not None
        and gate._scan_decode_retry_attempts >= max_attempts)


def resume_scan_after_decode_retry(gate, now):
    uid = gate._scan_decode_retry_uid
    max_attempts, _retry_mm = decode_retry_config(gate)
    msg = ("NFC[%d]: tag decode still incomplete after %d retries; "
           "continuing scan-jog" % (gate._gate, max_attempts))
    logger.warning("%s (uid=%s)", msg, uid)
    gate._console("[WARN] " + msg)
    if uid is not None:
        reset_uid_only_read(gate, uid)
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_next_chunk_time = now


def log_decode_retry_poll_start(gate):
    max_attempts, _retry_mm = decode_retry_config(gate)
    if gate._debug >= 3:
        logger.info(
            "nfc_gate: [%s] gate %d decode retry poll %d/%d at "
            "scan position %.1f / %.1fmm (offset %.1fmm)",
            gate._name, gate._gate,
            gate._scan_decode_retry_attempts, max_attempts,
            gate._scan_mm_total, gate._scan_max_mm,
            getattr(gate, '_scan_decode_retry_offset', 0.0))


def log_decode_retry_poll_result(gate, tag_found):
    max_attempts, _retry_mm = decode_retry_config(gate)
    attempt = gate._scan_decode_retry_attempts
    tag = gate._state.current_tag
    if tag_found:
        uid = getattr(tag, 'uid', None) or gate._state.current_uid
        incomplete = bool(getattr(tag, 'read_incomplete', False))
        raw = getattr(tag, 'raw_tag_data', None)
        block_count = None
        if isinstance(raw, dict) and raw.get('blocks') is not None:
            try:
                block_count = len(raw.get('blocks'))
            except Exception:
                block_count = None
        if gate._debug >= 3:
            if block_count is None:
                logger.info(
                    "nfc_gate: [%s] gate %d decode retry poll %d/%d "
                    "read uid=%s incomplete=%s",
                    gate._name, gate._gate, attempt, max_attempts,
                    uid, incomplete)
            else:
                logger.info(
                    "nfc_gate: [%s] gate %d decode retry poll %d/%d "
                    "read uid=%s incomplete=%s blocks=%d",
                    gate._name, gate._gate, attempt, max_attempts,
                    uid, incomplete, block_count)
        return

    if gate._debug >= 3:
        logger.info(
            "nfc_gate: [%s] gate %d decode retry poll %d/%d found no tag "
            "at scan position %.1f / %.1fmm (offset %.1fmm)",
            gate._name, gate._gate, attempt, max_attempts,
            gate._scan_mm_total, gate._scan_max_mm,
            getattr(gate, '_scan_decode_retry_offset', 0.0))


def next_decode_retry_move(gate, max_attempts, retry_mm):
    move = 0.0
    while gate._scan_decode_retry_attempts < max_attempts:
        attempt_index = gate._scan_decode_retry_attempts
        round_index = attempt_index // 2
        side = 1.0 if attempt_index % 2 == 0 else -1.0
        target_offset = side * retry_mm * (round_index + 1)
        current_offset = getattr(gate, '_scan_decode_retry_offset', 0.0)
        move = target_offset - current_offset
        next_total = gate._scan_mm_total + move
        if next_total < 0.0:
            move = -gate._scan_mm_total
        elif next_total > gate._scan_max_mm:
            move = gate._scan_max_mm - gate._scan_mm_total
        gate._scan_decode_retry_attempts += 1
        if abs(move) > 0.001:
            return move
        gate._scan_decode_retry_offset += move
        move = 0.0
    return 0.0


def queue_decode_retry_move(gate, now, uid, reason, max_attempts, retry_mm):
    move = next_decode_retry_move(gate, max_attempts, retry_mm)
    if abs(move) <= 0.001:
        return False

    attempt = gate._scan_decode_retry_attempts
    msg = ("NFC[%d]: tag decode incomplete; retry %d/%d after %.1fmm jog"
           % (gate._gate, attempt, max_attempts, move))
    logger.warning("%s (uid=%s reason=%s)", msg, uid, reason)
    gate._console("[WARN] " + msg)
    reset_uid_only_read(gate, uid)
    gate._run_jog(move)
    gate._scan_mm_total += move
    gate._scan_decode_retry_offset += move
    gate._scan_next_chunk_time = gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY
    logger.info(
        "NFC[%d]: decode retry move queued %.1fmm  scan position %.1f / %.1fmm",
        gate._gate, move, gate._scan_mm_total, gate._scan_max_mm)
    return True


def retry_incomplete_decode(gate, now):
    if not current_tag_decode_incomplete(gate):
        return False

    tag = gate._state.current_tag
    uid = tag.uid
    max_attempts, retry_mm = decode_retry_config(gate)
    if max_attempts <= 0 or retry_mm <= 0.0:
        return False

    if gate._scan_decode_retry_uid != uid:
        gate._scan_decode_retry_uid = uid
        gate._scan_decode_retry_attempts = 0
        gate._scan_decode_retry_offset = 0.0

    if gate._scan_decode_retry_attempts >= max_attempts:
        msg = ("NFC[%d]: tag decode still incomplete after %d retries; "
               "using current result" % (gate._gate, max_attempts))
        logger.warning(msg)
        gate._console("[WARN] " + msg)
        return False

    reason = getattr(tag, 'read_retry_reason', None)
    if not reason:
        auth_failed = getattr(tag, 'mifare_auth_failed_sectors', None) or []
        if getattr(tag, 'parse_error', None):
            reason = tag.parse_error
        elif auth_failed:
            reason = "auth failed sectors %s" % auth_failed
        else:
            reason = "incomplete rich tag read"

    move = 0.0
    while gate._scan_decode_retry_attempts < max_attempts:
        attempt_index = gate._scan_decode_retry_attempts
        round_index = attempt_index // 2
        side = 1.0 if attempt_index % 2 == 0 else -1.0
        target_offset = side * retry_mm * (round_index + 1)
        current_offset = getattr(gate, '_scan_decode_retry_offset', 0.0)
        move = target_offset - current_offset
        next_total = gate._scan_mm_total + move
        if next_total < 0.0:
            move = -gate._scan_mm_total
        elif next_total > gate._scan_max_mm:
            move = gate._scan_max_mm - gate._scan_mm_total
        gate._scan_decode_retry_attempts += 1
        if abs(move) > 0.001:
            break
        gate._scan_decode_retry_offset += move
        move = 0.0

    if abs(move) <= 0.001:
        return False

    attempt = gate._scan_decode_retry_attempts
    msg = ("NFC[%d]: tag decode incomplete; retry %d/%d after %.1fmm jog"
           % (gate._gate, attempt, max_attempts, move))
    logger.warning("%s (uid=%s reason=%s)", msg, uid, reason)
    gate._console("[WARN] " + msg)
    reset_uid_only_read(gate, uid)
    gate._run_jog(move)
    gate._scan_mm_total += move
    gate._scan_decode_retry_offset += move
    gate._scan_next_chunk_time = gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY
    logger.info(
        "NFC[%d]: decode retry move queued %.1fmm  scan position %.1f / %.1fmm",
        gate._gate, move, gate._scan_mm_total, gate._scan_max_mm)
    return True


def continue_decode_retry(gate, now):
    uid = gate._scan_decode_retry_uid
    max_attempts, retry_mm = decode_retry_config(gate)
    if gate._scan_decode_retry_attempts >= max_attempts:
        msg = ("NFC[%d]: tag decode still incomplete after %d retries; "
               "using current result" % (gate._gate, max_attempts))
        logger.warning(msg)
        gate._console("[WARN] " + msg)
        return False
    return queue_decode_retry_move(
        gate, now, uid, "no tag at retry position", max_attempts, retry_mm)


def finish(gate):
    gate._scan_mode = False
    gate._state.miss_count = 0
    found_msg = "[OK] NFC[%d]: tag found" % gate._gate
    info_both(found_msg)
    gate._console(found_msg)
    msg = _rewind_message(gate, "[REWIND]")
    logger.info(msg)
    gate._console(msg)
    gate._run_rewind()
    restore_left_neighbor(gate)
    gate.__class__._active_scan_gate = None
    # Filament is back at the gate — dispatch the event that was suppressed during the jog.
    if gate._scan_found_event is not None:
        event = gate._scan_found_event
        if len(event) == 5:
            event_type, g, uid, spool, meta = event
        else:
            event_type, g, uid, spool = event
            meta = None
        gate._scan_found_event = None
        if event_type == 'changed' and meta is not None and spool is None:
            gate._klipper.dispatch(event_type, g, uid, spool, meta=meta)
        else:
            gate._poll_klipper_dispatch(event_type, g, uid, spool)
        if event_type == 'changed' and spool is not None:
            gate._hh_load_paused = True
            gate._state.miss_count = 0
        if event_type == 'changed' and spool is not None:
            msg = "[OK] NFC[%d]: spool %s assigned" % (g, spool)
            info_both(msg)
            gate._console(msg)
        elif event_type == 'changed' and meta is not None:
            msg = "[OK] NFC[%d]: tag metadata assigned" % g
            info_both(msg)
            gate._console(msg)
        elif event_type == 'uid_only':
            msg = "[WARN] NFC[%d]: tag has no Spoolman match" % g
            logger.warning(msg)
            gate._console(msg)
    gate._scan_previous_uid = None
    gate._scan_previous_spool = None
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_uid = None
    gate._resume_poll_after_rewind()


def rewind_and_exit(gate):
    gate._scan_mode = False
    gate._state.miss_count = 0
    msg = _rewind_message(gate, "[WARN]", prefix="no tag found; ")
    logger.warning(msg)
    gate._console(msg)
    gate._run_rewind()
    restore_left_neighbor(gate)
    gate.__class__._active_scan_gate = None
    previous_uid = getattr(gate, '_scan_previous_uid', None)
    previous_spool = getattr(gate, '_scan_previous_spool', None)
    if previous_spool is not None:
        gate._state.current_uid = previous_uid
        gate._state.current_spool = previous_spool
        hh = gate._read_hh_status()
        gate._hh_load_paused = bool(
            hh.present and hh.available and hh.spool == previous_spool)
    else:
        gate._hh_load_paused = False
    gate._scan_previous_uid = None
    gate._scan_previous_spool = None
    if gate._debug >= 3:
        logger.info(
            "nfc_gate: [%s] gate %d scan mode — no tag found, "
            "NFC state and HH gate cache cleared after rewind",
            gate._name, gate._gate)
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_uid = None
    gate._resume_poll_after_rewind()


def console(gate, msg):
    """Send a message directly to the Klipper console, bypassing the logger."""
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return
    try:
        gcode.respond_info(_color_tags(msg))
    except Exception:
        pass


def run_jog(gate, mm):
    gcode = gate.printer.lookup_object('gcode')
    if not gate._scan_gate_selected:
        gate._scan_gate_selected = True
        gcode.run_script("MMU_SELECT GATE=%d\nMMU_TEST_MOVE MOVE=%.2f QUIET=1"
                         % (gate._gate, mm))
    else:
        gcode.run_script("MMU_TEST_MOVE MOVE=%.2f QUIET=1" % mm)


def _rewind_parts(gate):
    scan_mm = max(0.0, float(getattr(gate, '_scan_mm_total', 0.0) or 0.0))
    buffer_mm = max(
        0.0,
        float(getattr(gate, '_scan_rewind_buffer_mm', 30.0) or 0.0))
    fast_rewind = max(0.0, scan_mm - buffer_mm)
    return scan_mm, buffer_mm, fast_rewind


def _rewind_message(gate, level, prefix=""):
    scan_mm, buffer_mm, fast_rewind = _rewind_parts(gate)
    if fast_rewind > 0.0:
        return ("%s NFC[%d]: %srewinding %.1fmm "
                "(scan=%.1fmm buffer=%.1fmm)" % (
                    level, gate._gate, prefix, fast_rewind,
                    scan_mm, buffer_mm))
    return ("%s NFC[%d]: %srewind fast move skipped "
            "(scan=%.1fmm buffer=%.1fmm)" % (
                level, gate._gate, prefix, scan_mm, buffer_mm))


def run_rewind(gate):
    if gate._scan_mm_total <= 0.0:
        return
    gcode = gate.printer.lookup_object('gcode')
    _, _, fast_rewind = _rewind_parts(gate)
    if fast_rewind > 0.0:
        gcode.run_script("MMU_TEST_MOVE MOVE=%.2f QUIET=1\nM400"
                         % (-fast_rewind))
    gcode.run_script("_MMU_STEP_UNLOAD_GATE")
