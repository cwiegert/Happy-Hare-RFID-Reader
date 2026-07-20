# klippy/extras/nfc_gates/scan_jog.py
#
# Scan-and-jog mode helpers for NFCGate.

from . import hh_status
from .LED_effect_mgr import (
    EVENT_REWIND, EVENT_SCAN_START, EVENT_TAG_READ, LEDEffectManager)
from .gate_state import (
    DIRECT_METADATA_SPOOL, EVENT_CHANGED, EVENT_UID_ONLY, CurrentTag)
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


# Short settle after a decode-retry jog/backup before trying rich tag reads again.
DECODE_RETRY_SETTLE_DELAY = 0.2
SCAN_JOG_SUBSTEPS = 3
LEFT_NEIGHBOR_CLEARANCE_MM = 75.0
LEFT_NEIGHBOR_CLEARANCE_RETRIES = 3
TAG_READ_HOLD_DELAY = 0.35
CONTINUOUS_QUEUE_COMPLETE_POLL_FRACTION = 0.25
CONTINUOUS_QUEUE_COMPLETE_MIN_EPSILON = 0.01
# Treat sub-step floating-point residue as the end of a scan move.  Formatting
# such a residue as MOVE=0.00 would start an unnecessary extra NFC homing cycle.
CONTINUOUS_END_DISTANCE_EPSILON = 0.1

LED_SEARCHING  = 'mmu_clockwise_slow'
LED_TAG_READ   = 'mmu_RFID_read'
LED_REWINDING  = 'mmu_anticlock_fast'
LED_REASSERT_DELAY = 0.25


def _color_tags(text):
    return color_console_tags(text)


def _led_effect(gate, effect_name):
    """Apply effect_name to this gate's LED only (Happy Hare _exit_N per-gate naming).

    Called from reactor timer context — run_script is safe here (no GCode mutex held).
    Must be synchronous so LED state is correct before the next blocking operation.
    """
    if not effect_name:
        return
    led = LEDEffectManager(
        gate.printer, reactor=gate.reactor, name=gate._name,
        console=getattr(gate, '_console', None),
        happy_hare_v4=getattr(gate, '_happy_hare_v4', False))
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
    """Queue a delayed LED reassert so Happy Hare LED updates do not win the race."""
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
    if getattr(gate, '_scan_rewind_led_released', False):
        return True
    _cancel_led_reassert(gate)
    led = LEDEffectManager(
        gate.printer, reactor=gate.reactor, name=gate._name,
        happy_hare_v4=getattr(gate, '_happy_hare_v4', False))
    result = led.release(gate=gate._gate)
    if result.ok:
        gate._scan_rewind_led_released = True
    return result.ok


def _restore_hh_gate_led_quiet(gate):
    """Restore this gate's normal Happy Hare LED state after parking."""
    script = "MMU_SET_LED GATE=%d EXIT_EFFECT=gate_status FADETIME=0" % gate._gate
    try:
        run_hh_script(gate, script)
        logger.info("[%s]: Happy Hare gate LED restored", gate._name)
        return True
    except Exception as e:
        logger.warning("[%s]: Happy Hare gate LED restore failed: %s",
                       gate._name, e)
        return False


def manual_jog_scan(gate, gcmd):
    """Start scan-and-jog on demand, matching the automatic trigger path.

    SOURCE=AUTO identifies the trusted call from Happy Hare's
    post_preload_extension hook (_NFC_SCAN_JOG_PRELOAD). That call can land
    while Happy Hare v4 reports action=checking, so it uses the Happy Hare
    version-aware scan-safe check. Any other caller -- manual console command,
    button, macro -- gets no such context guarantee and still requires strict
    idle.
    """
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
    trusted_auto = str(gcmd.get('SOURCE', '') or '').strip().upper() == 'AUTO'
    hh = gate._read_hh_status()
    if hh.present:
        busy = (not gate._happy_hare_allows_scan_action(hh.action)
                if trusted_auto else not hh.idle)
        if busy:
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
    start(
        gate, max_mm=max_mm,
        shared_fallback=gcmd.get_int('SHARED_FALLBACK', 0) == 1)
    if getattr(gate, '_scan_motion_mode', 'stopped') == 'continuous':
        msg = ("[SCAN] NFC[%s]: continuous scan-jog started for gate %d"
               % (gate._name, gate._gate))
    else:
        msg = ("[SCAN] NFC[%s]: stopped scan-jog started for gate %d"
               % (gate._name, gate._gate))
    logger.info(msg)
    gcmd.respond_info(_color_tags(msg))
    if gate._debug >= 3:
        if getattr(gate, '_scan_motion_mode', 'stopped') == 'continuous':
            logger.info(
                "[%s]: continuous scan-jog settings — "
                "homing_max=%.0fmm speed=%.1fmm/s "
                "accel=%.1fmm/s^2 poll=%.2fs",
                gate._name, gate._scan_max_mm,
                gate._scan_continuous_speed,
                gate._scan_continuous_accel,
                gate._scan_continuous_poll_interval)
        else:
            logger.info(
                "[%s]: stopped scan-jog settings — "
                "homing_max=%.0fmm "
                "reads=%d poll=%.2fs",
                gate._name, gate._scan_max_mm,
                max(1, int(getattr(gate, '_scan_reads_per_position', 3))),
                gate._scan_poll_interval)


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
    """Estimate a continuous scan chunk duration using trapezoid timing."""
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


def distance_from_trapezoid_time(mm, elapsed, speed, accel):
    """Estimate distance covered by a trapezoid-limited move after elapsed time."""
    distance = abs(float(mm))
    elapsed = max(0.0, float(elapsed))
    if distance <= 0.0 or elapsed <= 0.0:
        return 0.0
    speed = max(0.001, float(speed))
    accel = max(0.001, float(accel))
    accel_time = speed / accel
    accel_distance = (speed * speed) / (2.0 * accel)
    if (2.0 * accel_distance) >= distance:
        peak_speed = (distance * accel) ** 0.5
        accel_time = peak_speed / accel
        total_time = 2.0 * accel_time
        if elapsed >= total_time:
            travelled = distance
        elif elapsed <= accel_time:
            travelled = 0.5 * accel * elapsed * elapsed
        else:
            decel_time = total_time - elapsed
            travelled = distance - (0.5 * accel * decel_time * decel_time)
    else:
        cruise_distance = distance - (2.0 * accel_distance)
        cruise_time = cruise_distance / speed
        total_time = (2.0 * accel_time) + cruise_time
        if elapsed >= total_time:
            travelled = distance
        elif elapsed <= accel_time:
            travelled = 0.5 * accel * elapsed * elapsed
        elif elapsed <= (accel_time + cruise_time):
            travelled = accel_distance + (speed * (elapsed - accel_time))
        else:
            decel_time = total_time - elapsed
            travelled = distance - (0.5 * accel * decel_time * decel_time)
    return min(distance, max(0.0, travelled))


def homing_distance_from_elapsed(gate, mm, elapsed, speed=None, accel=None):
    move_speed = get_speed(gate) if speed is None else speed
    move_accel = (
        getattr(gate, '_scan_continuous_accel', 2000.0)
        if accel is None else accel)
    estimated = distance_from_trapezoid_time(
        mm, elapsed, move_speed, move_accel)
    return estimated if mm >= 0.0 else -estimated


def corrected_homing_actual(gate, mm, actual, elapsed, speed=None, accel=None):
    reported = float(actual or 0.0)
    estimated = homing_distance_from_elapsed(
        gate, mm, elapsed, speed=speed, accel=accel)
    reported_abs = abs(reported)
    estimated_abs = abs(estimated)
    requested_abs = abs(float(mm))
    use_estimate = reported_abs < 0.001
    use_estimate = use_estimate or (
        requested_abs > 0.0
        and estimated_abs > 0.001
        and reported_abs >= requested_abs - 0.001
        and estimated_abs < reported_abs - 1.0)
    if not use_estimate:
        return reported
    if gate._debug >= 3:
        logger.info(
            "[%s]: NFC homing move reported %.1fmm; "
            "estimated %.1fmm from %.2fs elapsed",
            gate._name.capitalize(), reported, estimated, elapsed)
    return estimated


def continuous_probe_uid(gate):
    """Lightweight UID-only probe while a continuous chunk is in flight."""
    uid = None
    target_info = None
    probe_timeout = getattr(gate, '_scan_continuous_poll_interval', 0.050)
    probe_start_pos = estimate_continuous_probe_position(gate)
    read_tag = getattr(gate._reader, 'read_tag', None)
    if read_tag is not None:
        uid = read_tag(timeout=probe_timeout)
    else:
        read_target = getattr(gate._reader, 'read_target', None)
        if read_target is None:
            return False
        target_info = read_target(timeout=probe_timeout)
        if target_info is None:
            return False
        uid = target_info.get('uid')
        release = getattr(gate._reader, '_release_current_target', None)
        if release is not None:
            try:
                release(reason="continuous_uid_probe")
            except TypeError:
                release()
    probe_end_pos = estimate_continuous_probe_position(gate)
    if not uid:
        if gate._debug >= 4:
            logger.debug(
                "[%s]: continuous UID probe found no tag "
                "probe_window=%.1f..%.1fmm",
                gate._name.capitalize(),
                min(probe_start_pos, probe_end_pos),
                max(probe_start_pos, probe_end_pos))
        return False
    gate._scan_continuous_pending_uid = uid
    gate._scan_continuous_pending_target_info = (
        dict(target_info) if isinstance(target_info, dict) else None)
    record_continuous_uid_hit(gate, uid, probe_start_pos, probe_end_pos)
    if gate._debug >= 3:
        logger.info(
            "[%s]: continuous scan UID probe found uid=%s; "
            "deferring Spoolman/rich resolution until current move completes",
            gate._name.capitalize(), uid)
    return True


def estimate_continuous_probe_position(gate):
    """Estimate where the spool was when the latest in-flight UID probe hit."""
    move = float(getattr(gate, '_scan_continuous_last_move_mm', 0.0))
    end_mm = float(getattr(gate, '_scan_mm_total', 0.0))
    if abs(move) <= 0.001:
        return end_mm
    start_mm = end_mm - move
    duration = continuous_chunk_interval(gate, move)
    if duration <= 0.0:
        return end_mm

    progress = None
    if getattr(gate, '_scan_continuous_move_source', None) == "Direct Move":
        remaining = continuous_queue_remaining(gate)
        if remaining is not None:
            active_remaining = max(
                0.0,
                remaining - getattr(gate, '_scan_continuous_queue_baseline', 0.0))
            progress = 1.0 - (active_remaining / duration)

    if progress is None:
        complete_time = float(
            getattr(gate, '_scan_continuous_move_complete_time', 0.0) or 0.0)
        progress = 1.0 - (
            max(0.0, complete_time - gate.reactor.monotonic()) / duration)

    progress = min(1.0, max(0.0, progress))
    return start_mm + (move * progress)


def record_continuous_uid_hit(gate, uid, start_pos, end_pos):
    hits = getattr(gate, '_scan_continuous_uid_hits', None)
    if hits is None:
        hits = []
        gate._scan_continuous_uid_hits = hits
    low = min(start_pos, end_pos)
    high = max(start_pos, end_pos)
    center = (low + high) * 0.5
    hits.append((uid, center, low, high))
    if gate._debug >= 4:
        window = continuous_uid_hit_window(gate, uid)
        if window is not None:
            hit_low, hit_high, hit_center, hit_count = window
        else:
            hit_low, hit_high, hit_center, hit_count = low, high, center, 1
        logger.debug(
            "[%s]: continuous UID hit uid=%s estimated_pos=%.1fmm "
            "probe_window=%.1f..%.1fmm "
            "chunk_start=%.1fmm chunk_end=%.1fmm "
            "hit_window=%.1f..%.1fmm center=%.1fmm hits=%d",
            gate._name.capitalize(), uid, center, low, high,
            getattr(gate, '_scan_mm_total', 0.0)
            - getattr(gate, '_scan_continuous_last_move_mm', 0.0),
            getattr(gate, '_scan_mm_total', 0.0),
            hit_low, hit_high, hit_center, hit_count)


def continuous_uid_hit_window(gate, uid):
    matching = [
        (center, low, high)
        for hit_uid, center, low, high
        in getattr(gate, '_scan_continuous_uid_hits', [])
        if hit_uid == uid
    ]
    if not matching:
        return None
    low = min(hit_center for hit_center, _hit_low, _hit_high in matching)
    high = max(hit_center for hit_center, _hit_low, _hit_high in matching)
    return low, high, (low + high) * 0.5, len(matching)


def continuous_overshoot_backup_mm(gate, uid):
    """Choose the rich-read recenter point after continuous UID hits.

    UID-only and Spoolman-resolved scans still finish from the cached UID.  Rich
    tag parsing needs the spool to be stopped near the readable area, so use the
    estimated middle of the observed UID hit window.  If there is no hit window,
    the tag was found by a stationary poll and should use the normal small
    decode-retry sweep instead of a continuous recenter move.
    """
    window = continuous_uid_hit_window(gate, uid)
    if window is None:
        current = float(getattr(gate, '_scan_mm_total', 0.0))
        return 0.0, current, "no_uid_hit_window", current, current, 0

    low, high, center, count = window
    current = float(getattr(gate, '_scan_mm_total', 0.0))
    backup_mm = max(0.0, current - center)
    backup_mm = min(backup_mm, max(0.0, gate._scan_mm_total))
    return backup_mm, center, "uid_hit_window", low, high, count


def log_continuous_uid_hit_window(gate, uid, label):
    if gate._debug < 3 or not uid:
        return
    window = continuous_uid_hit_window(gate, uid)
    if window is None:
        logger.info(
            "[%s]: continuous UID hit window %s uid=%s none "
            "current=%.1fmm",
            gate._name.capitalize(), label, uid,
            getattr(gate, '_scan_mm_total', 0.0))
        return
    low, high, center, count = window
    logger.info(
        "[%s]: continuous UID hit window %s uid=%s %.1f..%.1fmm "
        "center=%.1fmm hits=%d current=%.1fmm",
        gate._name.capitalize(), label, uid, low, high, center, count,
        getattr(gate, '_scan_mm_total', 0.0))


def should_backup_before_rich_read(gate, uid):
    """Prefer the observed UID hit-window center before rich tag reads."""
    if not getattr(gate, '_tag_parsing', False):
        return False
    if getattr(gate, '_scan_continuous_overshoot_backed_up', False):
        return False
    window = continuous_uid_hit_window(gate, uid)
    if window is None:
        return False
    if gate._debug >= 3:
        low, high, center, count = window
        logger.info(
            "[%s]: continuous scan uid=%s has hit_window=%.1f..%.1fmm "
            "center=%.1fmm hits=%d; recentering before rich read",
            gate._name.capitalize(), uid, low, high, center, count)
    return True


def log_continuous_queue_remaining(gate, label):
    """Log Klipper's remaining queued MMU move time for continuous-scan debug."""
    if gate._debug < 4:
        return None
    mmu = gate.printer.lookup_object('mmu', None)
    mmu_toolhead = getattr(mmu, 'mmu_toolhead', None) if mmu is not None else None
    if mmu_toolhead is None:
        logger.debug(
            "[%s]: continuous queue timing %s unavailable: no mmu_toolhead",
            gate._name.capitalize(), label)
        return None
    last_move_time, estimated_print_time = _continuous_timing_snapshot(
        gate, mmu_toolhead)
    queue_remaining = (
        last_move_time - estimated_print_time
        if last_move_time is not None and estimated_print_time is not None
        else None)
    logger.debug(
        "[%s]: continuous queue timing %s "
        "mmu_last=%s mcu_est=%s queue_remaining=%s",
        gate._name.capitalize(), label,
        _fmt_optional_float(last_move_time),
        _fmt_optional_float(estimated_print_time),
        _fmt_optional_float(queue_remaining))
    return queue_remaining


def _cache_continuous_resolved_uid(
        gate, uid, spool_id, path, spool_identity=None):
    tag = CurrentTag(uid=uid)
    target_info = getattr(gate, '_scan_continuous_pending_target_info', None)
    if target_info is not None:
        tag.target_info = dict(target_info)
    tag.resolution = {'path': path, 'spool_id': spool_id}
    tag.spool_identity = spool_identity or None
    gate._state.current_tag = tag
    gate._state.current_uid = uid
    gate._state.current_spool = spool_id
    gate._state.miss_count = 0
    gate._scan_found_event = (EVENT_CHANGED, gate._gate, uid, spool_id, None)
    gate._scan_continuous_pending_uid = None
    gate._scan_continuous_pending_target_info = None


def _cache_continuous_uid_only(gate, uid):
    tag = CurrentTag(uid=uid)
    target_info = getattr(gate, '_scan_continuous_pending_target_info', None)
    if target_info is not None:
        tag.target_info = dict(target_info)
    tag.resolution = {'path': 'continuous_uid_only'}
    gate._state.current_tag = tag
    gate._state.current_uid = uid
    gate._state.current_spool = None
    gate._state.miss_count = 0
    gate._scan_found_event = (EVENT_UID_ONLY, gate._gate, uid, None, None)
    gate._scan_continuous_pending_uid = None
    gate._scan_continuous_pending_target_info = None


def _left_neighbor_spool_for_interference(gate):
    if gate._gate <= 0:
        return None
    left_nfc = gate._nfc_gate_for_gate_number(gate._gate - 1)
    if left_nfc is None:
        return None
    return getattr(left_nfc._state, 'current_spool', None)


def _same_spool_id(left_spool, spool_id):
    if left_spool is None or spool_id is None:
        return False
    try:
        return int(left_spool) == int(spool_id)
    except (TypeError, ValueError):
        return str(left_spool) == str(spool_id)


def resolve_continuous_pending_uid(gate, now):
    """Resolve a UID captured during motion without reading rich tag data."""
    uid = getattr(gate, '_scan_continuous_pending_uid', None)
    if not uid:
        return False
    previous_uid = getattr(gate, '_scan_previous_uid', None)
    previous_spool = getattr(gate, '_scan_previous_spool', None)
    previous_identity = getattr(gate, '_scan_previous_spool_identity', None)
    if (uid == previous_uid and previous_spool is not None
            and previous_spool is not DIRECT_METADATA_SPOOL):
        if gate._spoolman is not None:
            try:
                spool_id = gate._spoolman.lookup_spool_by_uid(uid)
            except Exception:
                logger.exception(
                    "[%s]: continuous scan Spoolman UID lookup failed for "
                    "matched stashed uid=%s",
                    gate._name.capitalize(), uid)
                spool_id = None
            if spool_id is not None:
                left_spool = _left_neighbor_spool_for_interference(gate)
                if (getattr(gate, '_tag_parsing', False)
                        and not previous_identity
                        and _same_spool_id(left_spool, spool_id)):
                    if gate._debug >= 3:
                        logger.info(
                            "[%s]: continuous scan uid=%s matched stashed UID; "
                            "Spoolman lookup resolved spool_id=%s matching "
                            "left_spool=%s but spool_identity=None; forcing "
                            "rich tag parse before interference handling",
                            gate._name.capitalize(), uid, spool_id, left_spool)
                    return False
                _cache_continuous_resolved_uid(
                    gate, uid, spool_id, 'scan_previous_uid_spoolman_lookup',
                    spool_identity=previous_identity)
                if gate._debug >= 3:
                    logger.info(
                        "[%s]: continuous scan uid=%s matched stashed UID; "
                        "Spoolman lookup resolved spool_id=%s "
                        "spool_identity=%s left_spool=%s",
                        gate._name.capitalize(), uid, spool_id,
                        previous_identity if previous_identity else "None",
                        left_spool if left_spool is not None else "None")
                return True
            if gate._debug >= 3:
                logger.info(
                    "[%s]: continuous scan uid=%s matched stashed UID but "
                    "Spoolman UID lookup missed",
                    gate._name.capitalize(), uid)
        if getattr(gate, '_tag_parsing', False):
            if gate._debug >= 3:
                logger.info(
                    "[%s]: continuous scan uid=%s matched stashed UID/spool_id=%s "
                    "but no Spoolman UID match; forcing rich tag parse "
                    "before spool_identity/auto-create checks",
                    gate._name.capitalize(), uid, previous_spool)
            return False
        _cache_continuous_resolved_uid(
            gate, uid, previous_spool, 'scan_previous_uid',
            spool_identity=previous_identity)
        if gate._debug >= 3:
            logger.info(
                "[%s]: continuous scan uid=%s matched stashed UID; "
                "using stashed spool_id=%s spool_identity=%s",
                gate._name.capitalize(), uid, previous_spool,
                previous_identity if previous_identity else "None")
        return True
    if gate._spoolman is None:
        return False
    try:
        spool_id = gate._spoolman.lookup_spool_by_uid(uid)
    except Exception:
        logger.exception(
            "[%s]: continuous scan Spoolman UID lookup failed for uid=%s",
            gate._name.capitalize(), uid)
        return False
    if spool_id is None:
        if gate._debug >= 3:
            logger.info(
                "[%s]: continuous scan uid=%s not found in Spoolman; "
                "rich tag parse will run after hit-window recenter if enabled",
                gate._name.capitalize(), uid)
        return False
    if getattr(gate, '_tag_parsing', False):
        left_spool = _left_neighbor_spool_for_interference(gate)
        if not _same_spool_id(left_spool, spool_id):
            _cache_continuous_resolved_uid(
                gate, uid, spool_id, 'continuous_uid_lookup')
            if gate._debug >= 3:
                logger.info(
                    "[%s]: continuous scan uid=%s resolved through Spoolman "
                    "after move complete: spool_id=%s left_spool=%s; "
                    "accepting UID result without rich identity parse",
                    gate._name.capitalize(), uid, spool_id,
                    left_spool if left_spool is not None else "None")
            return True
        if gate._debug >= 3:
            logger.info(
                "[%s]: continuous scan uid=%s resolved through Spoolman "
                "after move complete: spool_id=%s matches left_spool=%s; "
                "forcing rich tag parse before interference handling",
                gate._name.capitalize(), uid, spool_id, left_spool)
        return False
    _cache_continuous_resolved_uid(gate, uid, spool_id, 'continuous_uid_lookup')
    if gate._debug >= 3:
        logger.info(
            "[%s]: continuous scan uid=%s resolved through Spoolman "
            "after move complete: spool_id=%s spool_identity=None "
            "(UID lookup does not include parsed tag metadata)",
            gate._name.capitalize(), uid, spool_id)
    return True


def cache_continuous_uid_only_if_needed(gate):
    uid = getattr(gate, '_scan_continuous_pending_uid', None)
    if not uid or getattr(gate, '_tag_parsing', False):
        return False
    _cache_continuous_uid_only(gate, uid)
    if gate._debug >= 3:
        logger.info(
            "[%s]: continuous scan uid=%s unresolved and rich parsing disabled; "
            "using UID-only scan result",
            gate._name.capitalize(), uid)
    return True


def retry_continuous_overshoot_position(gate, now, max_attempts=3):
    uid = getattr(gate, '_scan_continuous_pending_uid', None)
    if not uid:
        return False
    if not getattr(gate, '_tag_parsing', False):
        return False
    if getattr(gate, '_scan_continuous_overshoot_backed_up', False):
        return False
    attempts = int(getattr(
        gate, '_scan_continuous_overshoot_position_attempts', 0)) + 1
    gate._scan_continuous_overshoot_position_attempts = attempts
    if attempts >= max_attempts:
        return False
    if gate._debug >= 3:
        logger.info(
            "[%s]: continuous scan uid=%s unresolved at current "
            "position; retrying rich tag parse attempt %d/%d before recenter",
            gate._name.capitalize(), uid, attempts + 1, max_attempts)
    gate._scan_next_chunk_time = (
        gate.reactor.monotonic()
        + getattr(gate, '_scan_continuous_poll_interval', 0.05))
    gate._scan_continuous_tag_pending = True
    return True


def queue_continuous_homing_backtrack_retry(gate, now, reason):
    uid = getattr(gate, '_scan_continuous_pending_uid', None)
    if not uid:
        return False
    if getattr(gate, '_scan_motion_mode', 'stopped') != 'continuous':
        return False
    if getattr(gate, '_scan_continuous_move_source', None) != "NFC Homing Move":
        return False
    if not getattr(gate, '_tag_parsing', False):
        return False

    max_attempts = max(0, int(getattr(gate, '_scan_decode_retry_rounds', 5)))
    retry_mm = max(0.0, float(getattr(gate, '_scan_decode_retry_mm', 5.0)))
    if max_attempts <= 0 or retry_mm <= 0.0:
        return False

    gate._scan_decode_retry_mode = 'homing_backtrack'
    gate._scan_decode_retry_uid = uid
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_offset = 0.0
    gate._scan_continuous_overshoot_backed_up = True
    gate._scan_continuous_overshoot_uid = uid
    gate._scan_continuous_overshoot_origin_mm = gate._scan_mm_total
    gate._scan_continuous_overshoot_start_mm = gate._scan_mm_total
    gate._scan_continuous_chunk_start_mm = gate._scan_mm_total
    if gate._debug >= 3:
        logger.info(
            "[%s]: continuous NFC homing uid=%s did not resolve at stop "
            "position %.1fmm; starting one-way backtrack rich-read retry "
            "(step=%.1fmm attempts=%d reason=%s)",
            gate._name.capitalize(), uid, gate._scan_mm_total,
            retry_mm, max_attempts, reason)
    return queue_decode_retry_move(
        gate, now, uid, reason, max_attempts, retry_mm)


def queue_continuous_overshoot_backup(gate, now):
    uid = getattr(gate, '_scan_continuous_pending_uid', None)
    if not uid:
        return False
    if getattr(gate, '_scan_motion_mode', 'stopped') != 'continuous':
        return False
    if getattr(gate, '_scan_continuous_overshoot_backed_up', False):
        return False
    if not getattr(gate, '_tag_parsing', False):
        return False
    (backup_mm, center_mm, backup_source,
     window_low, window_high, window_hits) = continuous_overshoot_backup_mm(
         gate, uid)
    gate._scan_continuous_overshoot_backed_up = True
    if backup_mm <= 0.001:
        return False
    move = -backup_mm
    gate._scan_continuous_chunk_start_mm = max(
        0.0, gate._scan_mm_total - gate._scan_continuous_last_move_mm)
    if gate._spoolman is None:
        msg = ("[WARN] NFC[%s]: uid=%s not resolved after rich tag reads; "
               "recentering %.1fmm before retry"
               % (gate._name.capitalize(), uid, backup_mm))
    else:
        msg = ("[WARN] NFC[%s]: uid=%s not resolved in Spoolman or rich tag reads; "
               "recentering %.1fmm before retry"
               % (gate._name.capitalize(), uid, backup_mm))
    logger.info(msg)
    if gate._debug >= 3:
        logger.info(
            "[%s]: continuous hit-window recenter target %.1fmm "
            "(source=%s current=%.1fmm hit_window=%.1f..%.1fmm hits=%d)",
            gate._name.capitalize(), center_mm, backup_source,
            gate._scan_mm_total, window_low, window_high, window_hits)
    gate._console(msg)
    gate._run_jog(move)
    effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
    _led_effect(gate, effect_name)
    _schedule_led_reassert(gate, effect_name)
    gate._scan_continuous_overshoot_origin_mm = gate._scan_mm_total
    gate._scan_continuous_overshoot_start_mm = max(0.0, gate._scan_mm_total + move)
    gate._scan_continuous_retry_phase = 1
    gate._scan_mm_total = max(0.0, gate._scan_mm_total + move)
    gate._scan_continuous_overshoot_uid = uid
    gate._scan_decode_retry_uid = uid
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_offset = 0.0
    gate._scan_next_chunk_time = gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY
    if gate._debug >= 3:
        logger.info(
            "[%s]: continuous unresolved UID recenter queued %.1fmm  "
            "scan position %.1f / %.1fmm",
            gate._name.capitalize(), move, gate._scan_mm_total, gate._scan_max_mm)
    return True


def queue_continuous_post_backup_retry(gate, now):
    uid = getattr(gate, '_scan_decode_retry_uid', None)
    if not uid:
        return False
    if getattr(gate, '_scan_motion_mode', 'stopped') != 'continuous':
        return False
    if not getattr(gate, '_scan_continuous_overshoot_backed_up', False):
        return False
    if getattr(gate, '_scan_continuous_overshoot_uid', None) != uid:
        return False
    max_attempts, retry_mm = decode_retry_config(gate)
    if max_attempts <= 0 or retry_mm <= 0.0:
        return False
    if gate._scan_decode_retry_attempts >= max_attempts:
        return False
    return queue_decode_retry_move(
        gate, now, uid,
        "no complete tag read after continuous hit-window recenter",
        max_attempts, retry_mm)


def full_poll_after_continuous_probe(gate):
    """Run the normal poll/resolve path after a deferred continuous UID probe."""
    tag_found = False
    try:
        tag_found = gate._poll()
    finally:
        if tag_found:
            gate._scan_continuous_pending_uid = None
            gate._scan_continuous_pending_target_info = None
    return tag_found


def full_poll_after_continuous_probe_resolved(gate):
    """Return True only when the deferred UID resolves to spool or metadata."""
    uid = getattr(gate, '_scan_continuous_pending_uid', None)
    tag_found = False
    try:
        tag_found = gate._poll()
    finally:
        resolved = (
            tag_found
            and gate._state.current_uid == uid
            and gate._state.current_spool is not None)
        if resolved:
            gate._scan_continuous_pending_uid = None
            gate._scan_continuous_pending_target_info = None
        elif tag_found and gate._state.current_uid == uid:
            gate._scan_found_event = None
    return resolved


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
                "[%s]: gate %d scan mode — syncing Happy Hare Spoolman "
                "state before scan-jog",
                gate._name, gate._gate)
        # Every other Happy Hare call in this module goes through
        # run_hh_script() so mmu.wrap_suppress_visual_log() keeps HH's
        # gate-map/filament-position banners off the console during scan-jog.
        # This call used gcode.run_script() directly, so it was the one path
        # that could still trigger an unsuppressed HH status print.
        run_hh_script(gate, "MMU_SPOOLMAN SYNC=1 QUIET=1")
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
                "[%s]: gate %d scan mode — clearing Happy Hare gate cache "
                "before scan-jog",
                gate._name, gate._gate)
        run_hh_script(gate, "_NFC_GATE_CLEAR_CACHE GATE=%d" % gate._gate)
    except Exception as e:
        logger.warning(
            "[%s]: gate %d scan mode — _NFC_GATE_CLEAR_CACHE failed: %s",
            gate._name, gate._gate, e)


def run_pending_hh_prep(gate):
    """Run Happy Hare prep once from the scan timer, outside the hook call stack."""
    if not getattr(gate, '_scan_hh_prep_pending', False):
        return
    gate._scan_hh_prep_pending = False
    # Happy Hare calls first — both touch MMU_GATE_MAP which resets LED state.
    # Searching effect fires last, then again shortly after any Happy Hare repaint.
    clear_hh_gate_cache(gate)
    sync_spoolman_before_scan(gate)
    effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
    _led_effect(gate, effect_name)
    _schedule_led_reassert(gate, effect_name)


def clear_unresolved_scan(gate):
    """Clear stale Happy Hare metadata when scan-jog ends without a spool id."""
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return
    try:
        gcode.run_script("_NFC_SCAN_UNRESOLVED GATE=%d" % gate._gate)
    except Exception as e:
        logger.warning(
            "[%s]: gate %d scan mode — _NFC_SCAN_UNRESOLVED failed: %s",
            gate._name, gate._gate, e)


def apply_shared_fallback(gate):
    """Apply staged shared-reader data after a hybrid lane scan fails."""
    if not getattr(gate, '_scan_shared_fallback', False):
        return False
    shared = gate.printer.lookup_object('nfc_gate shared', None)
    if shared is None or not getattr(shared, '_shared', False):
        return False
    try:
        applied = shared._shared_preload_policy().fallback_to_gate(gate._gate)
    except Exception:
        logger.exception(
            "[%s]: gate %d scan mode - shared fallback failed",
            gate._name, gate._gate)
        return False
    if applied:
        msg = ("[OK] NFC[%s]: lane scan found no tag; applied staged "
               "shared-reader data to gate %d"
               % (gate._name.capitalize(), gate._gate))
        logger.info(msg)
        gate._console(msg)
    return applied


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
        select_gate_quiet(gate, previous_gate)
    except Exception as e:
        logger.warning(
            "[%s]: gate %d scan mode — failed to restore "
            "Happy Hare selected gate %d: %s",
            gate._name, gate._gate, previous_gate, e)


def run_hh_script(gate, script, suppress_visual=True):
    gcode = gate.printer.lookup_object('gcode')
    mmu = gate.printer.lookup_object('mmu', None)
    if suppress_visual and mmu is not None and hasattr(mmu, 'wrap_suppress_visual_log'):
        with mmu.wrap_suppress_visual_log():
            gcode.run_script(script)
    else:
        gcode.run_script(script)


class _NoopContext:
    def __enter__(self):
        return None

    def __exit__(self, _exc_type, _exc, _tb):
        return False


def suppress_hh_visual_log(mmu):
    if mmu is not None and hasattr(mmu, 'wrap_suppress_visual_log'):
        return mmu.wrap_suppress_visual_log()
    return _NoopContext()


def select_gate_quiet(gate, gate_num):
    """Select gate_num without HH's cmd_MMU_SELECT gate-table/visual banner.

    cmd_MMU_SELECT (the MMU_SELECT gcode command) calls mmu.select_gate()
    to do the physical selection, then unconditionally prints the full
    gate-table + visual-state banner -- that print isn't gated by QUIET=1
    or by wrap_suppress_visual_log() at all. mmu.select_gate() is the same
    call the gcode command makes for the actual selection and has no such
    print, so calling it directly (already the pattern run_direct_continuous_jog
    uses) selects the gate with nothing to suppress.
    """
    mmu = gate.printer.lookup_object('mmu', None)
    if mmu is not None and hasattr(mmu, 'select_gate'):
        try:
            mmu.select_gate(gate_num)
            return
        except Exception:
            logger.exception(
                "[%s]: gate %d scan mode — direct select_gate(%d) failed; "
                "falling back to MMU_SELECT gcode",
                gate._name, gate._gate, gate_num)
    run_hh_script(gate, "MMU_SELECT GATE=%d QUIET=1" % gate_num)


def resume_poll_after_rewind(gate):
    """Restart regular polling after the queued rewind move can finish."""
    delay = gate._poll_interval
    if gate._scan_mm_total > 0.0:
        delay += chunk_interval(gate, gate._scan_mm_total)
    gate.reactor.update_timer(
        gate._poll_timer,
        gate.reactor.monotonic() + delay)


def _hh_reset_filament_position(mmu):
    """Reset Happy Hare's raw gear position counter.

    HH v3 names this _initialize_filament_position (single-underscore
    "private" convention used throughout the v3 mmu.py monolith); HH v4's
    split-module drive() rewrite renamed it to initialize_filament_position.
    Try both so this keeps working across HH versions.
    """
    for name in ('_initialize_filament_position', 'initialize_filament_position'):
        fn = getattr(mmu, name, None)
        if fn is not None:
            fn()
            return True
    return False


def start(gate, max_mm=None, shared_fallback=False):
    if max_mm is not None:
        gate._scan_max_mm = float(max_mm)
    # Happy Hare's own MMU_LOAD/MMU_EJECT sequences zero this counter before
    # they issue any gear moves. Scan-jog drives the gear directly through
    # the low-level _MMU_STEP_* primitives instead of those sequences, so
    # without this reset HH's tracked gear position (shown as the
    # "UNLOADED N.Nmm" console readout) keeps accumulating every jog from
    # every past scan-jog run forever.
    mmu = gate.printer.lookup_object('mmu', None)
    if mmu is not None:
        try:
            _hh_reset_filament_position(mmu)
        except Exception:
            logger.exception(
                "[%s]: gate %d scan mode — failed to reset Happy Hare "
                "filament position before scan-jog",
                gate._name, gate._gate)
    gate.__class__._active_scan_gate = gate._gate
    gate._scan_mode = True
    gate._scan_shared_fallback = bool(shared_fallback)
    gate._scan_mm_total = 0.0
    gate._scan_next_chunk_time = gate.reactor.monotonic()
    gate._scan_continuous_move_inflight = False
    gate._scan_continuous_move_source = None
    gate._scan_continuous_move_complete_time = 0.0
    gate._scan_continuous_full_travel = False
    gate._scan_continuous_last_move_mm = 0.0
    gate._scan_continuous_probe_due = False
    gate._scan_continuous_tag_pending = False
    gate._scan_continuous_pending_uid = None
    gate._scan_continuous_pending_target_info = None
    gate._scan_continuous_uid_hits = []
    gate._scan_continuous_queue_baseline = 0.0
    gate._scan_continuous_queue_active_remaining = 0.0
    gate._scan_continuous_direct_available = True
    gate._scan_continuous_overshoot_backed_up = False
    gate._scan_continuous_overshoot_uid = None
    gate._scan_continuous_overshoot_position_attempts = 0
    gate._scan_continuous_chunk_start_mm = None
    gate._scan_position_reads_done = 0
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_decode_retry_mode = None
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
    gate._scan_previous_spool_identity = current_spool_identity(gate)
    if gate._debug >= 3 and gate._scan_previous_uid:
        logger.info(
            "[%s]: gate %d scan mode — stashed previous uid=%s "
            "spool=%s spool_identity=%s",
            gate._name, gate._gate, gate._scan_previous_uid,
            gate._scan_previous_spool,
            gate._scan_previous_spool_identity or "None")
    gate._state.current_uid   = None  # force changed event on first read
    gate._state.current_spool = None
    gate._hh_load_paused = False
    gate._scan_gate_selected = False  # deferred to first jog (must run from timer, not GCode handler)
    gate._scan_hh_prep_pending = True
    gate._scan_led_reassert_effect = None
    gate._scan_rewind_led_released = False

    gate._scan_timer = gate.reactor.register_timer(
        gate._scan_step_event,
        gate.reactor.monotonic())
    if gate._debug >= 3:
        if getattr(gate, '_scan_motion_mode', 'stopped') == 'continuous':
            logger.info(
                "[%s]: gate %d continuous scan mode started — "
                "homing_max=%.1fmm speed=%.1fmm/s accel=%.1fmm/s^2 "
                "gap=%.2fs",
                gate._name, gate._gate,
                gate._scan_max_mm,
                gate._scan_continuous_speed, gate._scan_continuous_accel,
                gate._scan_continuous_poll_interval)
            return
        logger.info(
            "[%s]: gate %d scan mode started — "
            "homing_max=%.1fmm speed=%.1fmm/s "
            "reads_per_position=%d poll=%.2fs",
            gate._name, gate._gate,
            gate._scan_max_mm, get_speed(gate),
            max(1, int(getattr(gate, '_scan_reads_per_position', 3))),
            gate._scan_poll_interval)


def step_event(gate, eventtime):
    if getattr(gate, '_scan_motion_mode', 'stopped') == 'continuous':
        return continuous_step_event(gate, eventtime)
    return stopped_step_event(gate, eventtime)


def stopped_step_event(gate, eventtime):
    if not gate._scan_mode:
        return gate.reactor.NEVER

    # Re-assert searching LED every step after the initial Happy Hare prep.
    # Happy Hare's own LED timer both kill custom effects — this
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
                if fail_continuous_uid_resolution_after_retries(gate):
                    return gate.reactor.NEVER
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
        if fail_continuous_uid_resolution_after_retries(gate):
            return gate.reactor.NEVER
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

    # NFC virtual homing moves block until the tag trips the lane endstop or
    # the full scan travel completes.  Use the whole remaining travel here;
    # the reader is the endstop that stops the move early.
    if now >= gate._scan_next_chunk_time:
        remaining = gate._scan_max_mm - gate._scan_mm_total
        chunk = remaining
        next_position = gate._scan_mm_total + chunk
        msg = ("[SCAN] NFC[%s]: homing up to %.1fmm  "
               "scan position %.1f / %.1fmm"
               % (gate._name.capitalize(), chunk, next_position,
                  gate._scan_max_mm))
        logger.info(msg)
        gate._console(msg)
        if gate._debug >= 4:
            logger.debug("[%s]: run_script %s",
                         gate._name.capitalize(),
                         homing_jog_command(gate, chunk))
        gate._run_jog(chunk)
        actual = scan_last_jog_actual(gate, chunk)
        # MMU_TEST_MOVE causes Happy Hare to update its LED state. Re-assert now and
        # once more after Happy Hare's own LED refresh has had time to land.
        effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
        _led_effect(gate, effect_name)
        _schedule_led_reassert(gate, effect_name)
        gate._scan_mm_total += max(0.0, actual)
        gate._scan_position_reads_done = 0
        gate._scan_next_chunk_time = (
            gate.reactor.monotonic() + gate._scan_poll_interval)
        logger.info(
            "[%s]: homing move completed %.1fmm requested %.1fmm  "
            "scan position %.1f / %.1fmm",
            gate._name.capitalize(), actual, chunk,
            gate._scan_mm_total, gate._scan_max_mm)

    return gate._scan_next_chunk_time


def continuous_step_event(gate, eventtime):
    if not gate._scan_mode:
        return gate.reactor.NEVER

    if (decode_retry_in_progress(gate) or decode_retry_exhausted(gate)):
        # Rich-tag retry moves intentionally keep the existing stopped/blocking
        # behavior. Continuous mode changes only the primary forward search jog.
        return stopped_step_event(gate, eventtime)

    if is_printing(gate):
        logger.warning(
            "[%s]: continuous scan mode: print started — aborting",
            gate._name)
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    now = gate.reactor.monotonic()
    move_inflight = getattr(gate, '_scan_continuous_move_inflight', False)
    complete_time = getattr(gate, '_scan_continuous_move_complete_time', 0.0)
    probe_due = getattr(gate, '_scan_continuous_probe_due', False)
    move_complete, complete_time = refresh_continuous_move_complete(
        gate, move_inflight, complete_time)
    completed_continuous_move = move_inflight and move_complete

    if move_inflight and move_complete and not probe_due:
        gate._scan_continuous_move_inflight = False
        move_inflight = False

    run_pending_hh_prep(gate)

    handled_pending_tag = False
    pending_tag = getattr(gate, '_scan_continuous_tag_pending', False)
    full_travel = getattr(gate, '_scan_continuous_full_travel', False)
    pending_uid = getattr(gate, '_scan_continuous_pending_uid', None)
    if full_travel and not pending_tag and not pending_uid:
        # The virtual endstop owns PN532 discovery during homing. Its final
        # no-tag request may still be busy, so do not start another discovery.
        gate._scan_continuous_move_inflight = False
        gate._scan_continuous_probe_due = False
        gate._scan_continuous_full_travel = False
        logger.warning(
            "[%s]: continuous scan reached max distance with no UID — rewinding",
            gate._name)
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    if pending_tag and move_complete:
        handled_pending_tag = True
        gate._scan_continuous_tag_pending = False
        log_continuous_uid_hit_window(
            gate, getattr(gate, '_scan_continuous_pending_uid', None),
            "before_tag_handling")
        tag_found = resolve_continuous_pending_uid(gate, now)
        if not tag_found:
            if getattr(gate, '_tag_parsing', False):
                uid = getattr(gate, '_scan_continuous_pending_uid', None)
                if should_backup_before_rich_read(gate, uid):
                    if queue_continuous_overshoot_backup(gate, now):
                        return (gate.reactor.monotonic()
                                + gate._scan_continuous_poll_interval)
                elif full_poll_after_continuous_probe_resolved(gate):
                    tag_found = True
                elif queue_continuous_homing_backtrack_retry(
                        gate, now,
                        "no complete tag read at NFC homing stop"):
                    return gate.reactor.monotonic() + gate._scan_continuous_poll_interval
                elif retry_continuous_overshoot_position(gate, now):
                    return gate.reactor.monotonic() + gate._scan_continuous_poll_interval
                elif queue_continuous_overshoot_backup(gate, now):
                    return gate.reactor.monotonic() + gate._scan_continuous_poll_interval
            else:
                tag_found = (
                    cache_continuous_uid_only_if_needed(gate)
                    or full_poll_after_continuous_probe(gate))
        if tag_found:
            gate._scan_continuous_move_inflight = False
            gate._scan_continuous_probe_due = False
            move_inflight = False
            probe_due = False
    else:
        try:
            if probe_due:
                gate._scan_continuous_probe_due = False
                log_continuous_queue_remaining(gate, "probe_due before")
                tag_found = continuous_probe_uid(gate)
                log_continuous_queue_remaining(gate, "probe_due after")
            elif move_inflight and not move_complete:
                log_continuous_queue_remaining(gate, "inflight probe before")
                tag_found = continuous_probe_uid(gate)
                log_continuous_queue_remaining(gate, "inflight probe after")
            elif getattr(gate, '_scan_continuous_pending_uid', None):
                tag_found = full_poll_after_continuous_probe(gate)
            elif completed_continuous_move:
                # The in-flight UID probe already checked this chunk.  If it
                # found nothing, queue the next chunk instead of inserting a
                # stopped, full-timeout poll that breaks continuous motion.
                tag_found = False
            elif gate._scan_mm_total <= 0.001:
                # Continuous scan should start by moving. The virtual endstop
                # handles in-motion UID capture, and this preserves the
                # scan-jog stash semantics from the stopped path: the previous
                # UID is moved off the reader before any found UID is accepted.
                tag_found = False
            else:
                tag_found = gate._poll()
        except Exception:
            logger.exception("[%s]: continuous scan poll error", gate._name)
            msg = "[ERROR] NFC[%s]: continuous scan poll failed" % gate._name.capitalize()
            logger.error(msg)
            gate._console(msg)
            tag_found = False

    if (tag_found and not handled_pending_tag
            and move_inflight and (probe_due or not move_complete)):
        gate._scan_continuous_tag_pending = True
        if move_complete:
            logger.info(
                "[%s]: continuous scan found tag at chunk end; "
                "starting tag handling",
                gate._name.capitalize())
            return gate.reactor.monotonic()
        if gate._debug >= 3:
            log_continuous_uid_hit_window(
                gate, getattr(gate, '_scan_continuous_pending_uid', None),
                "inflight")
            logger.info(
                "[%s]: continuous scan found uid=%s during in-flight move; "
                "continuing UID probes for %.2fs before tag handling",
                gate._name.capitalize(),
                getattr(gate, '_scan_continuous_pending_uid', None),
                max(0.0, complete_time - gate.reactor.monotonic()))
        return min(
            complete_time,
            gate.reactor.monotonic() + gate._scan_continuous_poll_interval)

    if tag_found and handle_left_neighbor_interference(gate):
        gate._scan_continuous_move_inflight = False
        gate._scan_continuous_tag_pending = False
        if not gate._scan_mode:
            return gate.reactor.NEVER
        return max(gate._scan_next_chunk_time,
                   gate.reactor.monotonic() + gate._scan_continuous_poll_interval)

    if tag_found:
        if current_tag_decode_incomplete(gate):
            if retry_incomplete_decode(gate, now):
                gate._scan_continuous_move_inflight = False
                gate._scan_continuous_tag_pending = False
                return gate.reactor.monotonic() + gate._scan_continuous_poll_interval
            if decode_retry_exhausted(gate):
                if fail_continuous_uid_resolution_after_retries(gate):
                    return gate.reactor.NEVER
                msg = ("[WARN] NFC[%s]: tag decode still incomplete after retries; "
                       "using best incomplete result" % gate._name.capitalize())
                logger.info(msg)
                gate._console(msg)
        gate._finish_scan()
        return gate.reactor.NEVER

    if move_inflight and not move_complete:
        # No artificial delay — read_tag() blocks for poll_interval already
        return min(complete_time, gate.reactor.monotonic())

    if queue_continuous_post_backup_retry(gate, now):
        return gate.reactor.monotonic() + gate._scan_continuous_poll_interval

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
    move = remaining
    if move <= CONTINUOUS_END_DISTANCE_EPSILON:
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    next_position = gate._scan_mm_total + move
    if gate._debug >= 4:
        logger.debug(
            "[%s]: preparing continuous scan move %.2fmm  "
            "scan position %.1f / %.1fmm",
            gate._name.capitalize(), move, next_position, gate._scan_max_mm)
    gate_was_selected = getattr(gate, '_scan_gate_selected', False)
    command_start = gate.reactor.monotonic()
    move_path = run_continuous_jog(gate, move)
    actual_move = max(0.0, scan_last_jog_actual(gate, move))
    move_source = continuous_move_source(move_path)
    if gate._debug >= 4:
        logger.debug(
            "[%s]: continuous scan move submitted via %s: "
            "move=%.2fmm requested=%.2fmm speed=%.1fmm/s accel=%.1fmm/s^2",
            gate._name.capitalize(), move_source, actual_move, move,
            gate._scan_continuous_speed, gate._scan_continuous_accel)
    msg = ("[SCAN] NFC[%s]: continuous %s %.1fmm  "
           "scan position %.1f / %.1fmm"
           % (gate._name.capitalize(), move_source, actual_move,
              gate._scan_mm_total + actual_move, gate._scan_max_mm))
    logger.info(msg)
    gate._console(msg)
    if gate._debug >= 4:
        logger.debug(
            "[%s]: continuous scan move timing source=%s "
            "move=%.2fmm requested=%.2fmm speed=%.1fmm/s accel=%.1fmm/s^2",
            gate._name.capitalize(), move_source, actual_move, move,
            gate._scan_continuous_speed, gate._scan_continuous_accel)
    command_elapsed = max(0.0, gate.reactor.monotonic() - command_start)
    expected_duration = continuous_chunk_interval(gate, move)
    # mmu.select_gate() blocks while the gate servo positions (~0.5s) before the
    # move enters the queue. If gate selection ran during this call, command_elapsed
    # includes that overhead and remaining_duration would be zero even though the
    # move just started. Use expected_duration instead so in-flight probing fires
    # for the first chunk.
    gate_selected_this_call = (
        not gate_was_selected and getattr(gate, '_scan_gate_selected', False))
    if move_source == "NFC Homing Move":
        remaining_duration = 0.0
        timing_basis = "blocking_homing"
    elif move_source == "Direct Move":
        # For the direct Happy Hare path, command_elapsed is mostly Klipper/HH
        # queueing work and is not reliable motion progress. Use Klipper's
        # queued MMU move time instead so tag handling waits for real move end.
        remaining_duration = max(0.0, gate._scan_continuous_queue_remaining)
        timing_basis = "queue"
    elif gate_selected_this_call:
        remaining_duration = expected_duration
        timing_basis = "expected_after_gate_select"
    else:
        remaining_duration = max(0.0, expected_duration - command_elapsed)
        timing_basis = "estimated"
    effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
    _led_effect(gate, effect_name)
    _schedule_led_reassert(gate, effect_name)
    gate._scan_mm_total += actual_move
    gate._scan_continuous_full_travel = (
        move_source == "NFC Homing Move"
        and actual_move >= move - CONTINUOUS_END_DISTANCE_EPSILON)
    gate._scan_continuous_uid_hits = []
    if gate._debug >= 4:
        logger.debug(
            "[%s]: continuous UID hit window reset for new %.1fmm chunk "
            "%.1f..%.1fmm",
            gate._name.capitalize(), actual_move,
            gate._scan_mm_total - actual_move, gate._scan_mm_total)
    gate._scan_continuous_last_move_mm = actual_move
    gate._scan_continuous_probe_due = True
    gate._scan_continuous_move_inflight = True
    gate._scan_continuous_move_source = move_source
    gate._scan_continuous_move_complete_time = (
        gate.reactor.monotonic() + remaining_duration)
    gate._scan_next_chunk_time = (
        gate._scan_continuous_move_complete_time
        + gate._scan_continuous_poll_interval)
    logger.info(
        "[%s]: continuous %s queued %.1fmm  scan position %.1f / %.1fmm "
        "(requested %.1fmm; next read in %.2fs; call returned in %.2fs, "
        "remaining move %.2fs, basis=%s)",
        gate._name.capitalize(), move_source, actual_move,
        gate._scan_mm_total, gate._scan_max_mm, move,
        gate._scan_next_chunk_time - gate.reactor.monotonic(),
        command_elapsed, remaining_duration, timing_basis)
    return gate.reactor.monotonic()


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
        if gate._debug >= 3:
            logger.info("[%s]: left gate %d has no NFC instance for "
                        "spool_identity lookup",
                        gate._name.capitalize(), target_gate)
        return None
    left_tag = getattr(left_nfc._state, 'current_tag', None)
    left_uid = getattr(left_nfc._state, 'current_uid', None)
    left_spool = getattr(left_nfc._state, 'current_spool', None)
    left_identity = (
        getattr(left_tag, 'spool_identity', None)
        if left_tag is not None else None)
    if not left_identity:
        if gate._debug >= 3:
            logger.info(
                "[%s]: left gate %d spool_identity lookup — "
                "uid=%s spool=%s current_tag=%s spool_identity=None",
                gate._name.capitalize(), target_gate, left_uid, left_spool,
                "yes" if left_tag is not None else "no")
        return None

    left_hh = hh_status.read(gate.printer, target_gate)
    if left_hh.present and not left_hh.available:
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d - left gate %d spool_identity=%s "
                "suppressed: Happy Hare reports gate empty (status=%d)",
                gate._name, gate._gate, target_gate, left_identity,
                left_hh.status)
        return None
    if gate._debug >= 3:
        logger.info(
            "[%s]: left gate %d spool_identity lookup — "
            "uid=%s spool=%s spool_identity=%s hh_available=%s",
            gate._name.capitalize(), target_gate, left_uid, left_spool,
            left_identity, left_hh.available if left_hh.present else "hh-absent")
    return left_identity


def is_left_neighbor_spool_identity_match(gate):
    if gate._gate <= 0:
        return False
    identity = current_spool_identity(gate)
    if not identity:
        if gate._debug >= 3:
            logger.info(
                "[%s]: interference check skipped — gate %d uid=%s "
                "spool=%s current spool_identity=None",
                gate._name.capitalize(), gate._gate,
                read_uid_from_scan_event(gate), read_spool_from_scan_event(gate))
        return False
    left_identity = spool_identity_for_gate(gate, gate._gate - 1)
    result = left_identity is not None and left_identity == identity
    if gate._debug >= 3:
        logger.info(
            "[%s]: interference check — gate %d uid=%s spool=%s "
            "spool_identity=%s left_gate=%d left_spool_identity=%s -> %s",
            gate._name.capitalize(), gate._gate, read_uid_from_scan_event(gate),
            read_spool_from_scan_event(gate), identity, gate._gate - 1,
            left_identity,
            "match" if result else "no match")
    return result


def clear_false_scan_result(gate):
    gate._scan_found_event = None
    gate._state.current_uid = None
    gate._state.current_spool = None
    gate._state.current_tag = None
    gate._state.miss_count = 0
    gate._scan_continuous_move_inflight = False
    gate._scan_continuous_move_source = None
    gate._scan_continuous_move_complete_time = 0.0
    gate._scan_continuous_last_move_mm = 0.0
    gate._scan_continuous_probe_due = False
    gate._scan_continuous_tag_pending = False
    gate._scan_continuous_pending_uid = None
    gate._scan_continuous_pending_target_info = None
    gate._scan_continuous_uid_hits = []
    gate._scan_continuous_full_travel = False
    gate._scan_continuous_overshoot_backed_up = False
    gate._scan_continuous_overshoot_uid = None
    gate._scan_continuous_overshoot_position_attempts = 0
    gate._scan_continuous_chunk_start_mm = None
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_decode_retry_mode = None


def reset_current_lane_for_full_scan_after_left_clearance(gate):
    """Reset scan-local state as if a new scan-jog started on this lane."""
    gate._scan_mm_total = 0.0
    gate._scan_next_chunk_time = gate.reactor.monotonic()
    gate._scan_continuous_move_inflight = False
    gate._scan_continuous_move_source = None
    gate._scan_continuous_move_complete_time = 0.0
    gate._scan_continuous_full_travel = False
    gate._scan_continuous_last_move_mm = 0.0
    gate._scan_continuous_probe_due = False
    gate._scan_continuous_tag_pending = False
    gate._scan_continuous_pending_uid = None
    gate._scan_continuous_pending_target_info = None
    gate._scan_continuous_uid_hits = []
    gate._scan_continuous_queue_baseline = 0.0
    gate._scan_continuous_queue_active_remaining = 0.0
    gate._scan_continuous_direct_available = True
    gate._scan_continuous_overshoot_backed_up = False
    gate._scan_continuous_overshoot_uid = None
    gate._scan_continuous_overshoot_position_attempts = 0
    gate._scan_continuous_chunk_start_mm = None
    gate._scan_position_reads_done = 0
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_decode_retry_mode = None
    gate._scan_found_event = None
    gate._state.current_uid = None
    gate._state.current_spool = None
    gate._state.current_tag = None
    gate._state.miss_count = 0
    gate._scan_gate_selected = False
    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d scan mode — restarting full lane scan after "
            "left-neighbor clearance",
            gate._name, gate._gate)


def shift_left_neighbor(gate, left_gate, identity):
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return False
    try:
        select_gate_quiet(gate, left_gate)
        run_hh_script(gate,
            "MMU_TEST_MOVE MOVE=%.2f QUIET=1\n"
            "M400"
            % LEFT_NEIGHBOR_CLEARANCE_MM)
        select_gate_quiet(gate, gate._gate)
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
        select_gate_quiet(gate, left_gate)
        gate._console(msg)
        run_hh_script(gate, "_MMU_STEP_UNLOAD_GATE")
        select_gate_quiet(gate, gate._gate)
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
    reset_current_lane_for_full_scan_after_left_clearance(gate)
    msg = (
        "[SCAN] NFC[%s]: restarting full lane scan after left lane clearance"
        % gate._name.capitalize())
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
    gate._scan_continuous_move_source = None
    gate._scan_continuous_move_complete_time = 0.0
    gate._scan_continuous_last_move_mm = 0.0
    gate._scan_continuous_probe_due = False
    gate._scan_continuous_tag_pending = False
    gate._scan_continuous_pending_uid = None
    gate._scan_continuous_pending_target_info = None
    gate._scan_continuous_uid_hits = []
    gate._scan_continuous_queue_baseline = 0.0
    gate._scan_continuous_queue_active_remaining = 0.0
    gate._scan_continuous_direct_available = True
    gate._scan_continuous_overshoot_backed_up = False
    gate._scan_continuous_overshoot_uid = None
    gate._scan_continuous_overshoot_position_attempts = 0
    gate._scan_continuous_chunk_start_mm = None
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_decode_retry_mode = None
    gate._scan_continuous_uid_hits = []
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
    if getattr(gate, '_scan_decode_retry_mode', None) == 'homing_backtrack':
        max_attempts = max_rounds
    else:
        max_attempts = max_rounds * 2
    retry_mm = max(0.0, float(getattr(gate, '_scan_decode_retry_mm', 5.0)))
    return max_attempts, retry_mm


def decode_retry_in_progress(gate):
    max_attempts, retry_mm = decode_retry_config(gate)
    return (
        max_attempts > 0 and retry_mm > 0.0
        and gate._scan_decode_retry_uid is not None
        and gate._scan_decode_retry_attempts > 0
        and gate._scan_decode_retry_attempts <= max_attempts)


def decode_retry_exhausted(gate):
    max_attempts, retry_mm = decode_retry_config(gate)
    return (
        max_attempts > 0 and retry_mm > 0.0
        and gate._scan_decode_retry_uid is not None
        and gate._scan_decode_retry_attempts >= max_attempts)


def fail_continuous_uid_resolution_after_retries(gate):
    if getattr(gate, '_scan_motion_mode', 'stopped') != 'continuous':
        return False
    if getattr(gate, '_scan_decode_retry_mode', None) == 'homing_backtrack':
        return False
    uid = getattr(gate, '_scan_decode_retry_uid', None)
    if not uid:
        return False
    if getattr(gate, '_scan_continuous_overshoot_uid', None) != uid:
        return False
    if not getattr(gate, '_scan_continuous_overshoot_backed_up', False):
        return False
    if not decode_retry_exhausted(gate):
        return False
    max_attempts, _retry_mm = decode_retry_config(gate)
    msg = ("[WARN] NFC[%s]: uid=%s rich tag read failed after %d local "
           "retries; rewinding without resuming scan-jog"
           % (gate._name.capitalize(), uid, max_attempts))
    logger.info(msg)
    gate._console(msg)
    reset_uid_only_read(gate, uid)
    gate._rewind_and_exit_scan()
    return True


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
    gate._scan_decode_retry_mode = None
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


def next_continuous_overshoot_retry_move(gate, max_attempts, retry_mm):
    """Retry from the best continuous rich-read position.

    NFC homing moves stop after the first UID hit, so there is no hit window to
    center around.  In that mode, walk back along the just-scanned direction:
    with retry_mm=5 and rounds=5, target offsets are -5, -10, -15, -20, -25mm.
    The older direct/probe path below still retries around the hit-window
    center with +step, -step, +2step, -2step, ...
    """
    if getattr(gate, '_scan_decode_retry_mode', None) == 'homing_backtrack':
        while gate._scan_decode_retry_attempts < max_attempts:
            move = -retry_mm
            next_total = gate._scan_mm_total + move
            if next_total < 0.0:
                move = -gate._scan_mm_total
            gate._scan_decode_retry_attempts += 1
            if abs(move) > 0.001:
                return move
            gate._scan_decode_retry_offset += move
        return 0.0

    start_mm = getattr(
        gate, '_scan_continuous_overshoot_start_mm', gate._scan_mm_total)
    current_offset = gate._scan_mm_total - start_mm

    while gate._scan_decode_retry_attempts < max_attempts:
        attempt_index = gate._scan_decode_retry_attempts
        round_index = attempt_index // 2
        side = 1.0 if attempt_index % 2 == 0 else -1.0
        target_offset = side * retry_mm * (round_index + 1)
        move = target_offset - current_offset
        next_total = gate._scan_mm_total + move
        if next_total < 0.0:
            move = -gate._scan_mm_total
        elif next_total > gate._scan_max_mm:
            move = gate._scan_max_mm - gate._scan_mm_total
        gate._scan_decode_retry_attempts += 1
        if abs(move) > 0.001:
            return move
        current_offset += move
    return 0.0


def queue_decode_retry_move(gate, now, uid, reason, max_attempts, retry_mm):
    if (getattr(gate, '_scan_continuous_overshoot_backed_up', False)
            and getattr(gate, '_scan_continuous_chunk_start_mm', None) is not None):
        move = next_continuous_overshoot_retry_move(gate, max_attempts, retry_mm)
    else:
        move = next_decode_retry_move(gate, max_attempts, retry_mm)
    if abs(move) <= 0.001:
        return False

    attempt = gate._scan_decode_retry_attempts
    msg = ("[WARN] NFC[%s]: tag decode incomplete; retry %d/%d after %.1fmm jog"
           % (gate._name.capitalize(), attempt, max_attempts, move))
    if gate._debug >= 3:
        logger.info("%s (uid=%s reason=%s)", msg, uid, reason)
    reset_uid_only_read(gate, uid)
    gate._run_jog(move)
    effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
    _led_effect(gate, effect_name)
    _schedule_led_reassert(gate, effect_name)
    gate._scan_mm_total += move
    gate._scan_decode_retry_offset += move
    gate._scan_next_chunk_time = gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY
    if gate._debug >= 3:
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
        gate._scan_continuous_overshoot_backed_up = (
            getattr(gate, '_scan_continuous_overshoot_uid', None) == uid)

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

    if (getattr(gate, '_scan_motion_mode', 'stopped') == 'continuous'
            and not getattr(gate, '_scan_continuous_overshoot_backed_up', False)):
        (backup_mm, center_mm, backup_source,
         window_low, window_high, window_hits) = continuous_overshoot_backup_mm(
             gate, uid)
        gate._scan_continuous_overshoot_backed_up = True
        if backup_mm > 0.001:
            move = -backup_mm
            gate._scan_continuous_chunk_start_mm = max(
                0.0, gate._scan_mm_total - gate._scan_continuous_last_move_mm)
            msg = ("[WARN] NFC[%s]: tag decode incomplete; backing up %.1fmm "
                   "to continuous hit-window center before retry"
                   % (gate._name.capitalize(), backup_mm))
            if gate._debug >= 3:
                logger.info("%s (uid=%s reason=%s)", msg, uid, reason)
            reset_uid_only_read(gate, uid)
            gate._run_jog(move)
            effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
            _led_effect(gate, effect_name)
            _schedule_led_reassert(gate, effect_name)
            gate._scan_continuous_overshoot_origin_mm = gate._scan_mm_total
            gate._scan_continuous_overshoot_start_mm = max(0.0, gate._scan_mm_total + move)
            gate._scan_continuous_retry_phase = 1
            gate._scan_mm_total = max(0.0, gate._scan_mm_total + move)
            gate._scan_continuous_overshoot_uid = uid
            gate._scan_decode_retry_offset = 0.0
            gate._scan_next_chunk_time = (
                gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY)
            if gate._debug >= 3:
                logger.info(
                    "[%s]: continuous hit-window recenter queued %.1fmm  "
                    "scan position %.1f / %.1fmm target=%.1fmm "
                    "source=%s hit_window=%.1f..%.1fmm hits=%d",
                    gate._name.capitalize(), move,
                    gate._scan_mm_total, gate._scan_max_mm, center_mm,
                    backup_source, window_low, window_high, window_hits)
            return True

    if (getattr(gate, '_scan_motion_mode', 'stopped') == 'continuous'
            and getattr(gate, '_scan_continuous_overshoot_backed_up', False)):
        return queue_decode_retry_move(gate, now, uid, reason, max_attempts, retry_mm)

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
    if gate._debug >= 3:
        logger.info("%s (uid=%s reason=%s)", msg, uid, reason)
    reset_uid_only_read(gate, uid)
    gate._run_jog(move)
    effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
    _led_effect(gate, effect_name)
    _schedule_led_reassert(gate, effect_name)
    gate._scan_mm_total += move
    gate._scan_decode_retry_offset += move
    gate._scan_next_chunk_time = gate.reactor.monotonic() + DECODE_RETRY_SETTLE_DELAY
    if gate._debug >= 3:
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
    gate._scan_rewind_led_released = False
    _led_effect(gate, getattr(gate, '_scan_rewind_effect', LED_REWINDING))
    gate._run_rewind()
    msg = _rewind_complete_message(gate)
    logger.info(msg)
    gate._console(msg)
    restore_left_neighbor(gate)
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
            if gate._spoolman is None:
                msg = ("[WARN] NFC[%s]: tag read, but no rich metadata "
                       "or spool assignment was found" %
                       gate._name.capitalize())
            else:
                msg = "[WARN] NFC[%s]: tag has no Spoolman match" % gate._name.capitalize()
            logger.warning(msg)
            gate._console(msg)
    gate._scan_previous_uid = None
    gate._scan_previous_spool = None
    gate._scan_previous_spool_identity = None
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_continuous_uid_hits = []
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_identity = None
    gate._scan_left_neighbor_attempts = 0
    gate._resume_poll_after_rewind()
    _led_release(gate)
    gate._scan_shared_fallback = False
    # Only release the "one gate scans at a time" guard once every last bit of
    # cleanup above (HH dispatch, poll resume, LED release) is actually done.
    # Clearing it earlier let a second `jog_scan` command be accepted while
    # this session's own tail was still running, racing both sessions'
    # Happy Hare interactions against each other.
    gate.__class__._active_scan_gate = None


def rewind_and_exit(gate):
    _cancel_led_reassert(gate)
    gate._scan_mode = False
    gate._state.miss_count = 0
    msg = _rewind_message(gate, "[REWIND]", prefix="no tag found; ")
    logger.info(msg)
    gate._console(msg)
    gate._scan_rewind_led_released = False
    _led_effect(gate, getattr(gate, '_scan_rewind_effect', LED_REWINDING))
    gate._run_rewind()
    msg = _rewind_complete_message(gate)
    logger.info(msg)
    gate._console(msg)
    restore_left_neighbor(gate)
    shared_fallback_applied = apply_shared_fallback(gate)
    if not shared_fallback_applied:
        clear_unresolved_scan(gate)
    previous_uid = getattr(gate, '_scan_previous_uid', None)
    previous_spool = getattr(gate, '_scan_previous_spool', None)
    if previous_spool is not None and not shared_fallback_applied:
        gate._state.current_uid = previous_uid
        gate._state.current_spool = previous_spool
        hh = gate._read_hh_status()
        gate._hh_load_paused = bool(
            hh.present and hh.available and hh.spool == previous_spool)
    else:
        gate._hh_load_paused = False
    gate._scan_previous_uid = None
    gate._scan_previous_spool = None
    gate._scan_previous_spool_identity = None
    if gate._debug >= 3:
        if shared_fallback_applied:
            logger.info(
                "[%s]: gate %d scan mode - no lane tag found; "
                "staged shared-reader data assigned after rewind",
                gate._name, gate._gate)
        else:
            logger.info(
                "[%s]: gate %d scan mode - no tag found; "
                "NFC state and Happy Hare gate cache cleared after rewind",
                gate._name, gate._gate)
    gate._scan_decode_retry_attempts = 0
    gate._scan_decode_retry_uid = None
    gate._scan_decode_retry_offset = 0.0
    gate._scan_decode_retry_mode = None
    gate._scan_left_neighbor_gate = -1
    gate._scan_left_neighbor_shift_mm = 0.0
    gate._scan_left_neighbor_shifted = False
    gate._scan_left_neighbor_identity = None
    gate._scan_left_neighbor_attempts = 0
    gate._resume_poll_after_rewind()
    _led_release(gate)
    gate._scan_shared_fallback = False
    # See finish(): release the scan guard only after all cleanup is done.
    gate.__class__._active_scan_gate = None


def console(gate, msg):
    """Send a scan-jog message to the Klipper console when enabled."""
    if not getattr(gate, '_console_output', False):
        return
    level_name = str(getattr(gate, '_console_log_level', 'warning') or 'warning').lower()
    level_order = {'debug': 10, 'info': 20, 'warning': 30, 'warn': 30, 'error': 40}
    threshold = level_order.get(level_name, 30)
    msg_text = str(msg)
    if '[ERROR]' in msg_text:
        msg_level = 40
    elif '[WARN]' in msg_text or '[SCAN]' in msg_text or '[REWIND]' in msg_text or '[OK]' in msg_text:
        msg_level = 30
    else:
        msg_level = 20
    if msg_level < threshold:
        return
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return
    try:
        gcode.respond_info(_color_tags(msg))
    except Exception:
        pass


def run_jog(gate, mm):
    gate._scan_last_jog_actual_mm = mm
    if mm > 0.0:
        return run_homing_jog(gate, mm)
    before = mmu_gear_position(gate)
    if not gate._scan_gate_selected:
        gate._scan_gate_selected = True
        select_gate_quiet(gate, gate._gate)
    run_hh_script(gate, "MMU_TEST_MOVE MOVE=%.2f QUIET=1" % mm)
    gate._scan_last_jog_actual_mm = measured_jog_delta(gate, before, mm)


def mmu_gear_position(gate):
    mmu = gate.printer.lookup_object('mmu', None)
    mmu_toolhead = getattr(mmu, 'mmu_toolhead', None) if mmu is not None else None
    if mmu_toolhead is None:
        return None
    try:
        pos = mmu_toolhead.get_position()
        return float(pos[1])
    except Exception:
        return None


def measured_jog_delta(gate, before, fallback):
    after = mmu_gear_position(gate)
    if before is None or after is None:
        return fallback
    actual = after - before
    if fallback < 0.0 and actual > 0.0:
        return -actual
    if fallback > 0.0 and actual < 0.0:
        return -actual
    return actual


def scan_last_jog_actual(gate, fallback):
    try:
        return float(getattr(gate, '_scan_last_jog_actual_mm', fallback))
    except (TypeError, ValueError):
        return fallback


def nfc_endstop_name(gate):
    endstop = nfc_endstop_object(gate)
    endstop_name = getattr(endstop, 'endstop_name', None)
    if not endstop_name:
        raise RuntimeError(
            "No NFC virtual endstop is registered for Happy Hare gate %d"
            % gate._gate)
    return endstop_name


def nfc_endstop_object(gate):
    return getattr(gate, '_mmu_nfc_endstop', None)


def nfc_homing_elapsed(gate, fallback):
    endstop = nfc_endstop_object(gate)
    if endstop is None or not hasattr(endstop, 'get_last_home_elapsed'):
        return fallback
    try:
        elapsed = endstop.get_last_home_elapsed()
    except Exception:
        return fallback
    if elapsed is None:
        return fallback
    try:
        elapsed = float(elapsed)
    except (TypeError, ValueError):
        return fallback
    return elapsed if elapsed > 0.0 else fallback


def homing_jog_command(gate, mm, speed=None, accel=None):
    parts = [
        "_MMU_STEP_HOMING_MOVE",
        "MOVE=%.2f" % mm,
        "ENDSTOP=%s" % nfc_endstop_name(gate),
        "STOP_ON_ENDSTOP=1",
        "MOTOR=gear",
    ]
    if speed is not None:
        parts.append("SPEED=%.1f" % speed)
    if accel is not None:
        parts.append("ACCEL=%.1f" % accel)
    parts.append("ALLOW_BYPASS=1")
    return " ".join(parts)


def run_direct_homing_jog(gate, mm, speed=None, accel=None):
    mmu = gate.printer.lookup_object('mmu', None)
    if (mmu is None or not hasattr(mmu, 'move_filament')
            or not hasattr(mmu, 'wrap_sync_gear_to_extruder')):
        return False
    move_speed = get_speed(gate) if speed is None else speed
    move_accel = accel
    start_time = gate.reactor.monotonic()
    with suppress_hh_visual_log(mmu):
        with mmu.wrap_sync_gear_to_extruder():
            actual, _homed, _measured, _delta = mmu.move_filament(
                "NFC scan homing move",
                mm,
                speed=move_speed,
                accel=move_accel,
                motor="gear",
                homing_move=(1 if mm >= 0.0 else -1),
                endstop_name=nfc_endstop_name(gate),
                wait=True)
    elapsed = nfc_homing_elapsed(
        gate, max(0.0, gate.reactor.monotonic() - start_time))
    actual = corrected_homing_actual(
        gate, mm, actual, elapsed, speed=move_speed, accel=move_accel)
    gate._scan_last_jog_actual_mm = actual
    return True


def run_homing_jog(gate, mm, speed=None, accel=None):
    gate._scan_last_jog_actual_mm = mm
    cmd = homing_jog_command(gate, mm, speed=speed, accel=accel)
    before = mmu_gear_position(gate)
    if not gate._scan_gate_selected:
        gate._scan_gate_selected = True
        select_gate_quiet(gate, gate._gate)
    if getattr(gate, '_scan_mode', False) and mm > 0.0:
        effect_name = getattr(gate, '_scan_searching_effect', LED_SEARCHING)
        _led_effect(gate, effect_name)
        _schedule_led_reassert(gate, effect_name)
    if run_direct_homing_jog(gate, mm, speed=speed, accel=accel):
        return "homing"
    start_time = gate.reactor.monotonic()
    run_hh_script(gate, cmd)
    gate._scan_last_jog_actual_mm = measured_jog_delta(gate, before, mm)
    elapsed = nfc_homing_elapsed(
        gate, max(0.0, gate.reactor.monotonic() - start_time))
    gate._scan_last_jog_actual_mm = corrected_homing_actual(
        gate, mm, gate._scan_last_jog_actual_mm, elapsed,
        speed=speed, accel=accel)
    return "homing"


def continuous_move_source(move_path):
    if move_path == "homing":
        return "NFC Homing Move"
    if move_path == "direct":
        return "Direct Move"
    if move_path == "gcode":
        return "MMU_TEST_MOVE"
    return str(move_path or "unknown")


def _fmt_optional_float(value):
    return "n/a" if value is None else "%.6f" % value


def _continuous_timing_snapshot(gate, mmu_toolhead):
    mcu = getattr(mmu_toolhead, 'mcu', None)
    if mcu is None:
        mcu = gate.printer.lookup_object('mcu', None)
    last_move_time = float(mmu_toolhead.get_last_move_time())
    estimated_print_time = float(
        mcu.estimated_print_time(gate.reactor.monotonic()))
    return last_move_time, estimated_print_time


def continuous_lookahead_flush(mmu_toolhead):
    flush = getattr(mmu_toolhead, '_nfc_continuous_lookahead_flush', None)
    if flush is not None:
        return flush
    if hasattr(mmu_toolhead, '_process_lookahead'):
        flush = mmu_toolhead._process_lookahead
    elif hasattr(mmu_toolhead, 'lookahead'):
        flush = mmu_toolhead.lookahead.flush
    else:
        flush = False
    setattr(mmu_toolhead, '_nfc_continuous_lookahead_flush', flush)
    return flush


def continuous_queue_remaining(gate):
    mmu = gate.printer.lookup_object('mmu', None)
    mmu_toolhead = getattr(mmu, 'mmu_toolhead', None) if mmu is not None else None
    if mmu_toolhead is None:
        return None
    last_move_time, estimated_print_time = _continuous_timing_snapshot(
        gate, mmu_toolhead)
    return last_move_time - estimated_print_time


def refresh_continuous_move_complete(gate, move_inflight, complete_time):
    if not move_inflight:
        return True, complete_time
    if getattr(gate, '_scan_continuous_move_source', None) != "Direct Move":
        return gate.reactor.monotonic() >= complete_time, complete_time
    remaining = continuous_queue_remaining(gate)
    gate._scan_continuous_queue_remaining = remaining
    # Klipper toolhead timing normally keeps a startup buffer
    # (BUFFER_TIME_START, commonly 0.250s) between get_last_move_time() and
    # estimated_print_time().  Treat that pre-existing queue depth as the
    # baseline and only wait for the extra time added by this Direct Move.
    active_remaining = max(
        0.0, remaining - getattr(gate, '_scan_continuous_queue_baseline', 0.0))
    gate._scan_continuous_queue_active_remaining = active_remaining
    completion_threshold = max(
        CONTINUOUS_QUEUE_COMPLETE_MIN_EPSILON,
        gate._scan_continuous_poll_interval * CONTINUOUS_QUEUE_COMPLETE_POLL_FRACTION)
    if gate._debug >= 4:
        logger.debug(
            "[%s]: continuous queue timing completion check "
            "queue_remaining=%s baseline=%s active_remaining=%s threshold=%.3f",
            gate._name.capitalize(),
            _fmt_optional_float(remaining),
            _fmt_optional_float(getattr(
                gate, '_scan_continuous_queue_baseline', 0.0)),
            _fmt_optional_float(active_remaining),
            completion_threshold)
    if active_remaining <= completion_threshold:
        gate._scan_continuous_move_complete_time = gate.reactor.monotonic()
        return True, gate._scan_continuous_move_complete_time
    gate._scan_continuous_move_complete_time = (
        gate.reactor.monotonic() + max(0.0, active_remaining))
    return False, gate._scan_continuous_move_complete_time


def run_continuous_jog(gate, mm):
    if mm > 0.0:
        return run_homing_jog(
            gate, mm,
            speed=gate._scan_continuous_speed,
            accel=gate._scan_continuous_accel)
    cmd = ("MMU_TEST_MOVE MOVE=%.2f SPEED=%.1f ACCEL=%.1f WAIT=0 QUIET=1"
           % (mm, gate._scan_continuous_speed, gate._scan_continuous_accel))
    if not gate._scan_gate_selected:
        gate._scan_gate_selected = True
        select_gate_quiet(gate, gate._gate)
    run_hh_script(gate, cmd)
    return "gcode"


def run_direct_mmu_move(gate, mm, speed=None, accel=None):
    mmu = gate.printer.lookup_object('mmu', None)
    if (mmu is None or not hasattr(mmu, 'move_filament')
            or not hasattr(mmu, 'wrap_sync_gear_to_extruder')):
        return False
    with suppress_hh_visual_log(mmu):
        with mmu.wrap_sync_gear_to_extruder():
            mmu.move_filament(
                "NFC scan move",
                mm,
                speed=speed,
                accel=accel,
                motor="gear",
                wait=True)
    return True


def run_direct_continuous_jog(gate, mm):
    """Queue a continuous scan move through Happy Hare's MMU toolhead.

    This avoids the public MMU_TEST_MOVE gcode command for the forward search
    path so the scan timer can continue polling NFC while the chunk is moving.
    If the installed Happy Hare version does not expose the expected internals,
    callers fall back to MMU_TEST_MOVE WAIT=0.
    """
    mmu = gate.printer.lookup_object('mmu', None)
    if mmu is None:
        return False
    mmu_toolhead = getattr(mmu, 'mmu_toolhead', None)
    if mmu_toolhead is None:
        return False
    mcu = getattr(mmu_toolhead, 'mcu', None)
    if mcu is None:
        mcu = gate.printer.lookup_object('mcu', None)
    if mcu is None:
        return False
    try:
        gate._scan_continuous_queue_remaining = None
        gate._scan_continuous_queue_baseline = 0.0
        gate._scan_continuous_queue_active_remaining = 0.0
        timing_before = (None, None)
        timing_before = _continuous_timing_snapshot(gate, mmu_toolhead)
        if not gate._scan_gate_selected:
            mmu.select_gate(gate._gate)
            gate._scan_gate_selected = True

        # Keep the gear rail under Happy Hare ownership and in the same sync mode used
        # by MMU_TEST_MOVE MOTOR=gear, but avoid the gcode/logging wrapper.
        gear_only = getattr(mmu_toolhead, 'GEAR_ONLY')
        mmu_toolhead.sync(gear_only)
        if hasattr(mmu, '_restore_gear_current'):
            mmu._restore_gear_current()

        speed = float(gate._scan_continuous_speed)
        accel = float(gate._scan_continuous_accel)
        if getattr(mmu, 'gate_selected', -1) >= 0:
            overrides = getattr(mmu, 'gate_speed_override', None)
            selected = int(getattr(mmu, 'gate_selected', -1))
            if overrides is not None and selected < len(overrides):
                adjust = float(overrides[selected]) / 100.0
                speed *= adjust
                accel *= adjust

        pos = list(mmu_toolhead.get_position())
        while len(pos) < 4:
            pos.append(0.0)
        pos[1] += float(mm)
        with mmu.wrap_accel(accel):
            mmu_toolhead.move(pos, speed)
        # Do not call flush_step_generation() here.  On current Klipper,
        # flush_all_steps() waits until only about BGFLUSH_HIGH_TIME (0.400s)
        # remains in the motion queue, so a longer continuous scan chunk would
        # return just as late as a short one.  Process lookahead enough to put
        # the move in the MMU trapq and let Klipper's background flusher send
        # steps while NFC UID probes run.
        flush_lookahead = continuous_lookahead_flush(mmu_toolhead)
        if flush_lookahead:
            flush_lookahead()
        last_after = float(mmu_toolhead.print_time)
        est_after = float(
            mcu.estimated_print_time(gate.reactor.monotonic()))
        last_before, est_before = timing_before
        # This baseline is usually close to Klipper's BUFFER_TIME_START
        # (0.250s).  It is not part of the newly queued MMU move, so completion
        # checks compare against active_remaining instead of absolute queue time.
        queue_baseline = last_before - est_before
        queue_remaining = last_after - est_after
        queue_active_remaining = max(0.0, queue_remaining - queue_baseline)
        gate._scan_continuous_queue_baseline = queue_baseline
        gate._scan_continuous_queue_remaining = queue_remaining
        gate._scan_continuous_queue_active_remaining = queue_active_remaining
        if gate._debug >= 4:
            last_delta = (
                last_after - last_before
                if last_after is not None and last_before is not None
                else None)
            logger.debug(
                "[%s]: continuous Direct Move timing detail "
                "mmu_last_before=%s mmu_last_after=%s "
                "last_delta=%s mcu_est_before=%s mcu_est_after=%s "
                "queue_baseline=%s queue_remaining=%s active_remaining=%s",
                gate._name.capitalize(),
                _fmt_optional_float(last_before),
                _fmt_optional_float(last_after),
                _fmt_optional_float(last_delta),
                _fmt_optional_float(est_before),
                _fmt_optional_float(est_after),
                _fmt_optional_float(queue_baseline),
                _fmt_optional_float(queue_remaining),
                _fmt_optional_float(queue_active_remaining))
        return True
    except Exception:
        logger.exception(
            "[%s]: direct Happy Hare continuous move failed",
            gate._name.capitalize())
        return False


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
        _led_release(gate)
        return
    gcode = gate.printer.lookup_object('gcode')
    _, _, fast_rewind = _rewind_parts(gate)
    try:
        if fast_rewind > 0.0:
            if not run_direct_mmu_move(gate, -fast_rewind):
                run_hh_script(
                    gate,
                    "_MMU_STEP_MOVE MOVE=%.2f MOTOR=gear ALLOW_BYPASS=1"
                    % (-fast_rewind))
    finally:
        _led_release(gate)
    run_hh_script(gate, "_MMU_STEP_UNLOAD_GATE")
    _restore_hh_gate_led_quiet(gate)
