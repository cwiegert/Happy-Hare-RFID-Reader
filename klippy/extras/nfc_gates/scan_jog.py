# klippy/extras/nfc_gates/scan_jog.py
#
# Scan-and-jog mode helpers for NFCGate.

from . import hh_status
from .LED_effect_mgr import (
    EVENT_REWIND, EVENT_SCAN_START, EVENT_TAG_READ, LEDEffectManager)
from .gate_state import DIRECT_METADATA_SPOOL
from .log import info_both, logger

try:
    from .log import color_console_tags
except ImportError:
    def color_console_tags(text):
        text = str(text)
        text = text.replace('[SCAN]', '<span style="color:#FFA040">[SCAN]</span>')
        text = text.replace('[MOVE]', '<span style="color:#FFA040">[MOVE]</span>')
        text = text.replace('[WARN]', '<span style="color:#FFFF00">[WARN]</span>')
        text = text.replace('[OK]', '<span style="color:#90EE90">[OK]</span>')
        text = text.replace('[REWIND]', '<span style="color:#90EE90">[REWIND]</span>')
        text = text.replace('[ERROR]', '<span style="color:#FF6060">[ERROR]</span>')
        return text


DECODE_RETRY_SETTLE_DELAY = 1.0
SCAN_JOG_SUBSTEPS = 3
LEFT_NEIGHBOR_CLEARANCE_MM = 75.0
LEFT_NEIGHBOR_CLEARANCE_RETRIES = 3
TAG_READ_HOLD_DELAY = 0.1

LED_SEARCHING  = 'mmu_clockwise_slow'
LED_TAG_READ   = 'mmu_RFID_read'
LED_REWINDING  = 'mmu_anticlock_fast'
LED_REASSERT_DELAY = 0.25


def _color_tags(text):
    return color_console_tags(text)


def _led_effect(gate, effect_name):
    """Apply effect_name to this gate's LED only (HH _exit_N per-gate naming).

    Called from reactor timer context — run_script is safe here (no GCode mutex held).
    Must be synchronous so LED state is correct before the next blocking operation.
    """
    if not effect_name:
        return
    led = LEDEffectManager(
        gate.printer, reactor=gate.reactor, name=gate._name,
        console=getattr(gate, '_console', None))
    result = led.play_lane_event(
        _scan_led_event(effect_name), effect_name, gate._gate, replace=True)
    if not result.ok and result.error is not None:
        gate._console("[WARN] NFC[%s]: LED effect failed — %s"
                      % (gate._name, result.error))


def _scan_led_event(effect_name):
    if effect_name == LED_SEARCHING:
        return EVENT_SCAN_START
    if effect_name == LED_TAG_READ:
        return EVENT_TAG_READ
    if effect_name == LED_REWINDING:
        return EVENT_REWIND
    return EVENT_SCAN_START


def _led_reassert_callback(gate, eventtime):
    """Re-apply the scan LED after Happy Hare has had time to repaint LEDs."""
    effect_name = getattr(gate, '_scan_led_reassert_effect', None)
    gate._scan_led_reassert_effect = None
    if effect_name and getattr(gate, '_scan_mode', False):
        _led_effect(gate, effect_name)
    return gate.reactor.NEVER


def _schedule_led_reassert(gate, effect_name):
    """Queue a delayed LED reassert so HH LED updates do not win the race."""
    if not effect_name:
        return
    gate._scan_led_reassert_effect = effect_name
    when = gate.reactor.monotonic() + LED_REASSERT_DELAY
    if getattr(gate, '_scan_led_timer', None) is None:
        gate._scan_led_timer = gate.reactor.register_timer(
            lambda et, _g=gate: _led_reassert_callback(_g, et), when)
    else:
        gate.reactor.update_timer(gate._scan_led_timer, when)


def _cancel_led_reassert(gate):
    gate._scan_led_reassert_effect = None
    timer = getattr(gate, '_scan_led_timer', None)
    if timer is not None:
        gate.reactor.update_timer(timer, gate.reactor.NEVER)


def _led_release(gate):
    """Return LED control to Happy Hare."""
    _cancel_led_reassert(gate)
    led = LEDEffectManager(gate.printer, reactor=gate.reactor, name=gate._name)
    led.release()



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
    if hh.present and hh.status == hh_status.GATE_EMPTY:
        msg = ("[ERROR] NFC[%s]: jog_scan is not enabled for an empty gate"
               % gate._name)
        logger.error(msg, extra={'nfc_no_console': True})
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
    msg = ("[SCAN] NFC[%s]: scan-jog started for gate %d"
           " (max=%.0fmm  poll=%.2fs)"
           % (gate._name, gate._gate,
              gate._scan_max_mm, gate._scan_poll_interval))
    logger.info(msg)
    gcmd.respond_info(_color_tags(msg))


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


def continuous_chunk_interval(gate, mm):
    """Estimate a WAIT=0 scan chunk duration using trapezoid motion timing."""
    distance = abs(float(mm))
    if distance <= 0.0:
        return 0.0
    speed = max(0.001, float(getattr(gate, '_scan_continuous_speed', 150.0)))
    accel = max(0.001, float(getattr(gate, '_scan_continuous_accel', 2000.0)))
    accel_time = speed / accel
    accel_distance = (speed * speed) / (2.0 * accel)
    if (2.0 * accel_distance) >= distance:
        # Triangular move: never reaches target speed.
        return 2.0 * (distance / accel) ** 0.5
    cruise_distance = distance - (2.0 * accel_distance)
    return (2.0 * accel_time) + (cruise_distance / speed)


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
                "[%s]: gate %d scan mode — syncing HH Spoolman "
                "state before scan-jog",
                gate._name, gate._gate)
        gcode.run_script("MMU_SPOOLMAN SYNC=1 QUIET=1")
    except Exception as e:
        logger.warning(
            "[%s]: gate %d scan mode — MMU_SPOOLMAN SYNC failed: %s",
            gate._name, gate._gate, e)


def clear_hh_gate_cache(gate):
    """Mark the gate loaded but unknown before scan-jog resolves the spool."""
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return
    try:
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d scan mode — clearing HH gate cache "
                "before scan-jog",
                gate._name, gate._gate)
        gcode.run_script("_NFC_GATE_CLEAR_CACHE GATE=%d" % gate._gate)
    except Exception as e:
        logger.warning(
            "[%s]: gate %d scan mode — _NFC_GATE_CLEAR_CACHE failed: %s",
            gate._name, gate._gate, e)


def run_pending_hh_prep(gate):
    """Run HH prep once from the scan timer, outside the hook call stack."""
    if not getattr(gate, '_scan_hh_prep_pending', False):
        return
    gate._scan_hh_prep_pending = False
    # HH calls first — both touch MMU_GATE_MAP which resets LED state.
    # Searching effect fires last, then again shortly after any HH repaint.
    clear_hh_gate_cache(gate)
    sync_spoolman_before_scan(gate)
    effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
    _led_effect(gate, effect_name)
    _schedule_led_reassert(gate, effect_name)


def clear_unresolved_scan(gate):
    """Clear stale HH metadata when scan-jog ends without a spool id."""
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return
    try:
        gcode.run_script("_NFC_SCAN_UNRESOLVED GATE=%d" % gate._gate)
    except Exception as e:
        logger.warning(
            "[%s]: gate %d scan mode — _NFC_SCAN_UNRESOLVED failed: %s",
            gate._name, gate._gate, e)


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
            "[%s]: gate %d scan mode — failed to restore "
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
    gate._scan_continuous_move_inflight = False
    gate._scan_continuous_move_complete_time = 0.0
    gate._scan_continuous_last_move_mm = 0.0
    gate._scan_position_reads_done = 0
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_identity = None
    gate._scan_left_neighbor_attempts = 0
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
    gate._scan_led_reassert_effect = None

    gate._scan_timer = gate.reactor.register_timer(
        gate._scan_step_event,
        gate.reactor.monotonic())
    if gate._debug >= 3:
        if getattr(gate, '_scan_motion_mode', 'stopped') == 'continuous':
            logger.info(
                "[%s]: gate %d continuous scan mode started — "
                "step=%.1fmm max=%.1fmm speed=%.1fmm/s accel=%.1fmm/s^2 "
                "gap=%.2fs",
                gate._name, gate._gate,
                gate._scan_continuous_step_mm, gate._scan_max_mm,
                gate._scan_continuous_speed, gate._scan_continuous_accel,
                gate._scan_continuous_poll_interval)
            return
        logger.info(
            "[%s]: gate %d scan mode started — "
            "chunk=%.1fmm substep=%.1fmm max=%.1fmm speed=%.1fmm/s "
            "reads_per_position=%d poll=%.2fs",
            gate._name, gate._gate,
            gate._scan_jog_mm, substep_distance(gate), gate._scan_max_mm,
            get_speed(gate),
            max(1, int(getattr(gate, '_scan_reads_per_position', 3))),
            gate._scan_poll_interval)


def step_event(gate, eventtime):
    if getattr(gate, '_scan_motion_mode', 'stopped') == 'continuous':
        return continuous_step_event(gate, eventtime)
    return stopped_step_event(gate, eventtime)


def stopped_step_event(gate, eventtime):
    if not gate._scan_mode:
        return gate.reactor.NEVER

    # Re-assert searching LED every step after the initial HH prep.
    # MMU_TEST_MOVE and HH's own LED timer both kill custom effects — this
    # keeps the clockwise animation alive between and after every jog.
    if not getattr(gate, '_scan_hh_prep_pending', True):
        _led_effect(gate, getattr(gate, '_scan_searching_effect', LED_SEARCHING))

    if is_printing(gate):
        logger.warning(
            "[%s]: scan mode: print started — aborting",
            gate._name)
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    now = gate.reactor.monotonic()

    retry_poll = decode_retry_in_progress(gate)
    if retry_poll and now < gate._scan_next_chunk_time:
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d decode retry waiting %.2fs "
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
                "[%s]: gate %d waiting %.2fs before next "
                "stopped-position read at scan position %.1f / %.1fmm",
                gate._name, gate._gate,
                gate._scan_next_chunk_time - now,
                gate._scan_mm_total, gate._scan_max_mm)
        return gate._scan_next_chunk_time

    run_pending_hh_prep(gate)

    try:
        tag_found = gate._poll()
    except Exception:
        logger.exception("[%s]: scan step poll error", gate._name)
        msg = "[ERROR] NFC[%s]: scan poll failed" % gate._name.capitalize()
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
                "[%s]: gate %d stopped-position read %d/%d "
                "found no tag at scan position %.1f / %.1fmm",
                gate._name, gate._gate,
                gate._scan_position_reads_done, reads_per_position,
                gate._scan_mm_total, gate._scan_max_mm)
        return gate.reactor.monotonic() + gate._scan_poll_interval

    if gate._scan_mm_total >= gate._scan_max_mm:
        if gate._scan_found_event is not None:
            msg = ("[WARN] NFC[%s]: scan reached max distance after decode retries; "
                   "using best incomplete result" % gate._name.capitalize())
            logger.info(msg)
            gate._console(msg)
            gate._finish_scan()
            return gate.reactor.NEVER
        logger.warning(
            "[%s]: scan mode: no tag after %.1fmm — rewinding",
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
        msg = ("[SCAN] NFC[%s]: moving %.1fmm  scan position %.1f / %.1fmm "
               "(substep of %.1fmm)"
               % (gate._name.capitalize(), chunk, next_position, gate._scan_max_mm,
                  gate._scan_jog_mm))
        logger.info(msg)
        gate._console(msg)
        if gate._debug >= 4:
            logger.debug("[%s]: run_script MMU_TEST_MOVE MOVE=%.2f QUIET=1",
                         gate._name.capitalize(), chunk)
        gate._run_jog(chunk)
        # MMU_TEST_MOVE causes HH to update its LED state. Re-assert now and
        # once more after HH's own LED refresh has had time to land.
        effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
        _led_effect(gate, effect_name)
        _schedule_led_reassert(gate, effect_name)
        gate._scan_mm_total += chunk
        gate._scan_position_reads_done = 0
        gate._scan_next_chunk_time = (
            gate.reactor.monotonic() + gate._scan_poll_interval)
        logger.info(
            "[%s]: move queued %.1fmm  scan position %.1f / %.1fmm",
            gate._name.capitalize(), chunk, gate._scan_mm_total, gate._scan_max_mm)

    return gate._scan_next_chunk_time


def continuous_step_event(gate, eventtime):
    if not gate._scan_mode:
        return gate.reactor.NEVER

    if (decode_retry_in_progress(gate) or decode_retry_exhausted(gate)):
        # Rich-tag retry moves intentionally keep the existing stopped/blocking
        # behavior. Continuous mode changes only the primary forward search jog.
        return stopped_step_event(gate, eventtime)

    if not getattr(gate, '_scan_hh_prep_pending', True):
        _led_effect(gate, getattr(gate, '_scan_searching_effect', LED_SEARCHING))

    if is_printing(gate):
        logger.warning(
            "[%s]: continuous scan mode: print started — aborting",
            gate._name)
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    now = gate.reactor.monotonic()

    if getattr(gate, '_scan_continuous_move_inflight', False):
        complete_time = getattr(gate, '_scan_continuous_move_complete_time', 0.0)
        if now < complete_time:
            return complete_time + gate._scan_continuous_poll_interval
        gate._scan_continuous_move_inflight = False

    run_pending_hh_prep(gate)

    try:
        tag_found = gate._poll()
    except Exception:
        logger.exception("[%s]: continuous scan poll error", gate._name)
        msg = "[ERROR] NFC[%s]: continuous scan poll failed" % gate._name.capitalize()
        logger.error(msg)
        gate._console(msg)
        tag_found = False

    if tag_found and handle_left_neighbor_interference(gate):
        gate._scan_continuous_move_inflight = False
        if not gate._scan_mode:
            return gate.reactor.NEVER
        return max(gate._scan_next_chunk_time,
                   gate.reactor.monotonic() + gate._scan_continuous_poll_interval)

    if tag_found:
        if current_tag_decode_incomplete(gate):
            if retry_incomplete_decode(gate, now):
                gate._scan_continuous_move_inflight = False
                return gate.reactor.monotonic() + gate._scan_continuous_poll_interval
            if decode_retry_exhausted(gate):
                msg = ("[WARN] NFC[%s]: tag decode still incomplete after retries; "
                       "using best incomplete result" % gate._name.capitalize())
                logger.info(msg)
                gate._console(msg)
        gate._finish_scan()
        return gate.reactor.NEVER

    if gate._scan_mm_total >= gate._scan_max_mm:
        if gate._scan_found_event is not None:
            msg = ("[WARN] NFC[%s]: continuous scan reached max distance after "
                   "decode retries; using best incomplete result"
                   % gate._name.capitalize())
            logger.info(msg)
            gate._console(msg)
            gate._finish_scan()
            return gate.reactor.NEVER
        logger.warning(
            "[%s]: continuous scan mode: no tag after %.1fmm — rewinding",
            gate._name, gate._scan_mm_total)
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    remaining = gate._scan_max_mm - gate._scan_mm_total
    move = min(gate._scan_continuous_step_mm, remaining)
    if move <= 0.0:
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    next_position = gate._scan_mm_total + move
    msg = ("[SCAN] NFC[%s]: continuous move %.1fmm  scan position %.1f / %.1fmm"
           % (gate._name.capitalize(), move, next_position, gate._scan_max_mm))
    logger.info(msg)
    gate._console(msg)
    if gate._debug >= 4:
        logger.debug(
            "[%s]: run_script MMU_TEST_MOVE MOVE=%.2f SPEED=%.1f ACCEL=%.1f WAIT=0 QUIET=1",
            gate._name.capitalize(), move,
            gate._scan_continuous_speed, gate._scan_continuous_accel)
    run_continuous_jog(gate, move)
    effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
    _led_effect(gate, effect_name)
    _schedule_led_reassert(gate, effect_name)
    gate._scan_mm_total += move
    gate._scan_continuous_last_move_mm = move
    gate._scan_continuous_move_inflight = True
    gate._scan_continuous_move_complete_time = (
        gate.reactor.monotonic() + continuous_chunk_interval(gate, move))
    gate._scan_next_chunk_time = (
        gate._scan_continuous_move_complete_time
        + gate._scan_continuous_poll_interval)
    logger.info(
        "[%s]: continuous move queued %.1fmm  scan position %.1f / %.1fmm "
        "(next read in %.2fs)",
        gate._name.capitalize(), move, gate._scan_mm_total, gate._scan_max_mm,
        gate._scan_next_chunk_time - gate.reactor.monotonic())
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


def current_spool_identity(gate):
    tag = gate._state.current_tag
    if tag is None:
        return None
    return getattr(tag, 'spool_identity', None) or None


def spool_identity_for_gate(gate, target_gate):
    left_nfc = gate._nfc_gate_for_gate_number(target_gate)
    if left_nfc is None:
        if gate._debug >= 4:
            logger.debug("[%s]: left gate %d has no NFC instance",
                         gate._name.capitalize(), target_gate)
        return None
    left_tag = getattr(left_nfc._state, 'current_tag', None)
    left_identity = (
        getattr(left_tag, 'spool_identity', None)
        if left_tag is not None else None)
    if not left_identity:
        if gate._debug >= 4:
            logger.debug("[%s]: left gate %d NFC cache has no spool_identity",
                         gate._name.capitalize(), target_gate)
        return None

    left_hh = hh_status.read(gate.printer, target_gate)
    if left_hh.present and not left_hh.available:
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d - left gate %d spool_identity=%s "
                "suppressed: HH reports gate empty (status=%d)",
                gate._name, gate._gate, target_gate, left_identity,
                left_hh.status)
        return None
    if gate._debug >= 4:
        logger.debug(
            "[%s]: left gate %d spool_identity=%s hh_available=%s",
            gate._name.capitalize(), target_gate, left_identity,
            left_hh.available if left_hh.present else "hh-absent")
    return left_identity


def is_left_neighbor_spool_identity_match(gate):
    if gate._gate <= 0:
        return False
    identity = current_spool_identity(gate)
    if not identity:
        if gate._debug >= 4:
            logger.debug(
                "[%s]: interference check skipped; current spool_identity unavailable",
                gate._name.capitalize())
        return False
    left_identity = spool_identity_for_gate(gate, gate._gate - 1)
    result = left_identity is not None and left_identity == identity
    if gate._debug >= 4:
        logger.debug(
            "[%s]: interference check spool_identity=%s left_spool_identity=%s -> %s",
            gate._name.capitalize(), identity, left_identity,
            "match" if result else "no match")
    return result


def clear_false_scan_result(gate):
    gate._scan_found_event = None
    gate._state.current_uid = None
    gate._state.current_spool = None
    gate._state.current_tag = None
    gate._state.miss_count = 0
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0


def shift_left_neighbor(gate, left_gate, identity):
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
            "[%s]: gate %d scan mode — failed to clear left "
            "neighbor gate %d from reader field: %s",
            gate._name, gate._gate, left_gate, e)
        return False
    gate._scan_left_neighbor_gate = left_gate
    gate._scan_left_neighbor_shift_mm = (
        getattr(gate, '_scan_left_neighbor_shift_mm', 0.0)
        + LEFT_NEIGHBOR_CLEARANCE_MM)
    gate._scan_left_neighbor_shifted = True
    gate._scan_left_neighbor_identity = identity
    gate._scan_left_neighbor_attempts = (
        getattr(gate, '_scan_left_neighbor_attempts', 0) + 1)
    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d scan mode — left neighbor gate %d "
            "shifted %.1fmm to clear reader field (attempt %d/%d, total %.1fmm)",
            gate._name, gate._gate, left_gate, LEFT_NEIGHBOR_CLEARANCE_MM,
            gate._scan_left_neighbor_attempts, LEFT_NEIGHBOR_CLEARANCE_RETRIES,
            gate._scan_left_neighbor_shift_mm)
    return True


def restore_left_neighbor(gate):
    if not getattr(gate, '_scan_left_neighbor_shifted', False):
        return
    left_gate = getattr(gate, '_scan_left_neighbor_gate', -1)
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_identity = None
    gate._scan_left_neighbor_attempts = 0
    if left_gate < 0:
        return
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return
    msg = ("[REWIND] NFC[Lane%d]: parking at gate sensor"
           % left_gate)
    logger.info(msg)
    try:
        gcode.run_script("MMU_SELECT GATE=%d" % left_gate)
        gate._console(msg)
        gcode.run_script(
            "_MMU_STEP_UNLOAD_GATE\n"
            "MMU_SELECT GATE=%d" % gate._gate)
    except Exception as e:
        logger.warning(
            "[%s]: gate %d scan mode — failed to restore left "
            "neighbor gate %d: %s",
            gate._name, gate._gate, left_gate, e)
        gate._console(
            "[WARN] NFC[Lane%d]: failed to park at gate sensor — "
            "move it back manually" % left_gate)
        return
    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d scan mode — left neighbor gate %d "
            "parked at gate sensor",
            gate._name, gate._gate, left_gate)


def handle_left_neighbor_interference(gate):
    if not getattr(gate, '_scan_mode', False) or gate._gate <= 0:
        return False

    uid = read_uid_from_scan_event(gate)
    identity = current_spool_identity(gate)
    if not uid or not is_left_neighbor_spool_identity_match(gate):
        if gate._debug >= 3 and uid:
            logger.info(
                "[%s]: gate %d scan mode — read uid=%s "
                "spool_identity=%s, no left-neighbor interference",
                gate._name, gate._gate, uid, identity)
        return False

    left_gate = gate._gate - 1
    spool = read_spool_from_scan_event(gate)
    already_tracking_same_identity = (
        getattr(gate, '_scan_left_neighbor_shifted', False)
        and getattr(gate, '_scan_left_neighbor_gate', -1) == left_gate
        and getattr(gate, '_scan_left_neighbor_identity', None) == identity)
    attempts = getattr(gate, '_scan_left_neighbor_attempts', 0)
    if already_tracking_same_identity and attempts >= LEFT_NEIGHBOR_CLEARANCE_RETRIES:
        msg = (
            "[ERROR] NFC[%s]: left lane gate %d is interfering with the "
            "current lane read after %d clearance moves (%.0fmm); check "
            "reader position, tag placement, or lane spacing"
            % (gate._name.capitalize(), left_gate, attempts,
               getattr(gate, '_scan_left_neighbor_shift_mm',
                       LEFT_NEIGHBOR_CLEARANCE_MM)))
        logger.error("%s", msg)
        gate._console(msg)
        clear_false_scan_result(gate)
        gate._rewind_and_exit_scan()
        return True

    logger.info(
        "[MOVE] NFC[%s]: uid=%s spool_identity=%s spool=%s belongs to left neighbor gate %d; "
        "clearance move %d/%d to clear neighbor from reader field",
        gate._name.capitalize(), uid, identity, spool, left_gate, attempts + 1,
        LEFT_NEIGHBOR_CLEARANCE_RETRIES)
    gate._console(
            "[MOVE] NFC[%s]: uid=%s spool_identity=%s spool=%s belongs to left neighbor gate %d; "
        "clearance move %d/%d to clear neighbor from reader field"
        % (gate._name.capitalize(), uid, identity, spool, left_gate, attempts + 1,
           LEFT_NEIGHBOR_CLEARANCE_RETRIES))
    if not shift_left_neighbor(gate, left_gate, identity):
        msg = (
            "[WARN] NFC[%s]: failed to clear left neighbor gate %d; aborting scan "
            "to avoid assigning the neighbor spool"
            % (gate._name.capitalize(), left_gate))
        logger.info(msg)
        gate._console(msg)
        clear_false_scan_result(gate)
        gate._rewind_and_exit_scan()
        return True
    clear_false_scan_result(gate)
    gate._scan_next_chunk_time = (
        gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY)
    msg = ("[SCAN] NFC[%s]: re-polling at position %.1fmm after left lane clearance"
           % (gate._name.capitalize(), gate._scan_mm_total))
    logger.info(msg)
    gate._console(msg)
    return True


def disconnect_cleanup(gate):
    """Leave scan-jog state coherent when Klippy disconnects mid-scan."""
    _cancel_led_reassert(gate)
    if getattr(gate, '_scan_left_neighbor_shifted', False):
        logger.warning(
            "[%s]: gate %d disconnect during left-neighbor "
            "clearance; attempting to restore gate %d before clearing scan "
            "state",
            gate._name, gate._gate,
            getattr(gate, '_scan_left_neighbor_gate', -1))
        restore_left_neighbor(gate)
    gate._scan_mode = False
    gate._scan_timer = None
    gate._scan_found_event = None
    gate._scan_continuous_move_inflight = False
    gate._scan_continuous_move_complete_time = 0.0
    gate._scan_continuous_last_move_mm = 0.0
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_identity = None
    gate._scan_left_neighbor_attempts = 0


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
    msg = ("[WARN] NFC[%s]: tag decode still incomplete after %d retries; "
           "continuing scan-jog" % (gate._name.capitalize(), max_attempts))
    logger.info("%s (uid=%s)", msg, uid)
    gate._console(msg)
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
            "[%s]: gate %d decode retry poll %d/%d at "
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
                    "[%s]: gate %d decode retry poll %d/%d "
                    "read uid=%s incomplete=%s",
                    gate._name, gate._gate, attempt, max_attempts,
                    uid, incomplete)
            else:
                logger.info(
                    "[%s]: gate %d decode retry poll %d/%d "
                    "read uid=%s incomplete=%s blocks=%d",
                    gate._name, gate._gate, attempt, max_attempts,
                    uid, incomplete, block_count)
        return

    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d decode retry poll %d/%d found no tag "
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
    msg = ("[WARN] NFC[%s]: tag decode incomplete; retry %d/%d after %.1fmm jog"
           % (gate._name.capitalize(), attempt, max_attempts, move))
    logger.info("%s (uid=%s reason=%s)", msg, uid, reason)
    gate._console(msg)
    reset_uid_only_read(gate, uid)
    gate._run_jog(move)
    effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
    _led_effect(gate, effect_name)
    _schedule_led_reassert(gate, effect_name)
    gate._scan_mm_total += move
    gate._scan_decode_retry_offset += move
    gate._scan_next_chunk_time = gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY
    logger.info(
        "[%s]: decode retry move queued %.1fmm  scan position %.1f / %.1fmm",
        gate._name.capitalize(), move, gate._scan_mm_total, gate._scan_max_mm)
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
        msg = ("[WARN] NFC[%s]: tag decode still incomplete after %d retries; "
               "using current result" % (gate._name.capitalize(), max_attempts))
        logger.info(msg)
        gate._console(msg)
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
    msg = ("[WARN] NFC[%s]: tag decode incomplete; retry %d/%d after %.1fmm jog"
           % (gate._name.capitalize(), attempt, max_attempts, move))
    logger.info("%s (uid=%s reason=%s)", msg, uid, reason)
    gate._console(msg)
    reset_uid_only_read(gate, uid)
    gate._run_jog(move)
    effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
    _led_effect(gate, effect_name)
    _schedule_led_reassert(gate, effect_name)
    gate._scan_mm_total += move
    gate._scan_decode_retry_offset += move
    gate._scan_next_chunk_time = gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY
    logger.info(
        "[%s]: decode retry move queued %.1fmm  scan position %.1f / %.1fmm",
        gate._name.capitalize(), move, gate._scan_mm_total, gate._scan_max_mm)
    return True


def continue_decode_retry(gate, now):
    uid = gate._scan_decode_retry_uid
    max_attempts, retry_mm = decode_retry_config(gate)
    if gate._scan_decode_retry_attempts >= max_attempts:
        msg = ("[WARN] NFC[%s]: tag decode still incomplete after %d retries; "
               "using current result" % (gate._name.capitalize(), max_attempts))
        logger.info(msg)
        gate._console(msg)
        return False
    return queue_decode_retry_move(
        gate, now, uid, "no tag at retry position", max_attempts, retry_mm)


def finish(gate):
    _cancel_led_reassert(gate)
    gate._scan_mode = False
    gate._state.miss_count = 0
    _led_effect(gate, getattr(gate, '_scan_tag_read_effect', LED_TAG_READ))
    found_msg = "[OK] NFC[%s]: tag found" % gate._name.capitalize()
    logger.info(found_msg)
    gate._console(found_msg)
    # reactor.pause() yields via greenlet — other reactor timers (including the LED
    # update timer) keep firing, so the tag-read flash is visible before rewind.
    gate.reactor.pause(gate.reactor.monotonic() + TAG_READ_HOLD_DELAY)
    msg = _rewind_message(gate, "[REWIND]")
    logger.info(msg)
    gate._console(msg)
    # Rewind LED fires before _run_rewind() so it shows during the entire move.
    _led_effect(gate, getattr(gate, '_scan_rewind_effect', LED_REWINDING))
    gate._run_rewind()
    # _led_release() is called at the end of finish() after all work is done.
    msg = _rewind_complete_message(gate)
    logger.info(msg)
    gate._console(msg)
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
            gate._klipper.dispatch(event_type, g, uid, spool, meta=meta,
                                   scan_finish=True)
        else:
            gate._poll_klipper_dispatch(event_type, g, uid, spool,
                                        scan_finish=True)
        if event_type == 'changed' and spool is not None:
            gate._hh_load_paused = True
            gate._state.miss_count = 0
        if event_type == 'changed' and spool is not None:
            msg = "[OK] NFC[%s]: spool %s assigned" % (gate._name.capitalize(), spool)
            logger.info(msg)
            gate._console(msg)
        elif event_type == 'changed' and meta is not None:
            msg = "[OK] NFC[%s]: tag metadata assigned" % gate._name.capitalize()
            logger.info(msg)
            gate._console(msg)
        elif event_type == 'uid_only':
            msg = "[WARN] NFC[%s]: tag has no Spoolman match" % gate._name.capitalize()
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
    gate._scan_left_neighbor_identity = None
    gate._scan_left_neighbor_attempts = 0
    gate._resume_poll_after_rewind()
    _led_release(gate)


def rewind_and_exit(gate):
    _cancel_led_reassert(gate)
    gate._scan_mode = False
    gate._state.miss_count = 0
    msg = _rewind_message(gate, "[REWIND]", prefix="no tag found; ")
    logger.info(msg)
    gate._console(msg)
    _led_effect(gate, getattr(gate, '_scan_rewind_effect', LED_REWINDING))
    gate._run_rewind()
    msg = _rewind_complete_message(gate)
    logger.info(msg)
    gate._console(msg)
    restore_left_neighbor(gate)
    clear_unresolved_scan(gate)
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
            "[%s]: gate %d scan mode — no tag found, "
            "NFC state and HH gate cache cleared after rewind",
            gate._name, gate._gate)
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_identity = None
    gate._scan_left_neighbor_attempts = 0
    gate._resume_poll_after_rewind()
    _led_release(gate)


def console(gate, msg):
    """Send a message directly to the Klipper console."""
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


def run_continuous_jog(gate, mm):
    gcode = gate.printer.lookup_object('gcode')
    cmd = ("MMU_TEST_MOVE MOVE=%.2f SPEED=%.1f ACCEL=%.1f WAIT=0 QUIET=1"
           % (mm, gate._scan_continuous_speed, gate._scan_continuous_accel))
    if not gate._scan_gate_selected:
        gate._scan_gate_selected = True
        gcode.run_script("MMU_SELECT GATE=%d\n%s" % (gate._gate, cmd))
    else:
        gcode.run_script(cmd)


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
        return ("%s NFC[%s]: %srewinding %.1fmm "
                "(scan=%.1fmm buffer=%.1fmm)" % (
                    level, gate._name.capitalize(), prefix, fast_rewind,
                    scan_mm, buffer_mm))
    return ("%s NFC[%s]: %srewind fast move skipped "
            "(scan=%.1fmm buffer=%.1fmm)" % (
                level, gate._name.capitalize(), prefix, scan_mm, buffer_mm))


def _rewind_complete_message(gate):
    scan_mm, buffer_mm, fast_rewind = _rewind_parts(gate)
    return ("[REWIND] NFC[%s]: rewind complete; gate parking handed to "
            "Happy Hare (rewound=%.1fmm scan=%.1fmm buffer=%.1fmm)" % (
                gate._name.capitalize(), fast_rewind, scan_mm, buffer_mm))


def run_rewind(gate):
    if gate._scan_mm_total <= 0.0:
        return
    gcode = gate.printer.lookup_object('gcode')
    _, _, fast_rewind = _rewind_parts(gate)
    if fast_rewind > 0.0:
        gcode.run_script("MMU_TEST_MOVE MOVE=%.2f QUIET=1\nM400"
                         % (-fast_rewind))
    gcode.run_script("_MMU_STEP_UNLOAD_GATE")
