# klippy/extras/nfc_gates/scan_jog.py
#
# Scan-and-jog mode helpers for NFCGate.

from .log import info_both, logger


def manual_jog_scan(gate, gcmd):
    """Start scan-and-jog on demand, matching the automatic trigger path."""
    if gate._failed:
        msg = ("❌ NFC[%s]: reader failed — "
               "run NFC GATE=%d INIT=1 first"
               % (gate._name, gate._gate))
        logger.error(msg)
        gcmd.respond_info(msg)
        return
    if is_printing(gate):
        msg = "🚫 NFC[%s]: print is active — cannot start scan-jog while printing" % gate._name
        logger.warning(msg)
        gcmd.respond_info(msg)
        return
    hh = gate._read_hh_status()
    if hh.present and not hh.idle:
        msg = ("⛔ NFC[%s]: Happy Hare is busy (action=%s) — "
               "wait for idle before starting scan-jog"
               % (gate._name, hh.action))
        logger.warning(msg)
        gcmd.respond_info(msg)
        return
    if gate.__class__._active_scan_gate is not None:
        msg = ("⛔ NFC[%s]: gate %d is already scanning — "
               "only one gate may scan at a time"
               % (gate._name, gate.__class__._active_scan_gate))
        logger.warning(msg)
        gcmd.respond_info(msg)
        return
    if gate._scan_mode:
        msg = "⛔ NFC[%s]: scan-jog already in progress for this gate" % gate._name
        logger.warning(msg)
        gcmd.respond_info(msg)
        return
    ok, reason, max_mm = gate._prepare_scan_jog()
    if not ok:
        msg = "⛔ NFC[%s]: scan-jog not available while %s" % (
            gate._name, reason)
        logger.warning(msg)
        gcmd.respond_info(msg)
        return

    gate.reactor.update_timer(gate._poll_timer, gate.reactor.NEVER)
    start(gate, max_mm=max_mm)
    gcmd.respond_info(
        "🔍 NFC[%s]: scan-jog started for gate %d"
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
    """Return the stationary read window after each scan chunk."""
    """want to make sure to give the reader 3 full pulls to read the tag"""
    return 3.0 * gate._scan_poll_interval


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
    gate._hh_seed_spool_id = None
    gate._hh_seed_available = False
    gate._scan_found_event = None
    gate._scan_previous_uid = gate._state.current_uid
    gate._scan_previous_spool = gate._state.current_spool
    gate._state.current_uid   = None  # force changed event on first read
    gate._state.current_spool = None
    gate._hh_load_paused = False
    gate._scan_gate_selected = False  # deferred to first jog (must run from timer, not GCode handler)

    sync_spoolman_before_scan(gate)

    gate._scan_timer = gate.reactor.register_timer(
        gate._scan_step_event,
        gate.reactor.monotonic())
    if gate._debug >= 3:
        logger.info(
            "nfc_gate: [%s] gate %d scan mode started — "
            "chunk=%.1fmm max=%.1fmm speed=%.1fmm/s "
            "chunk_interval=%.2fs dwell=%.2fs poll=%.2fs",
            gate._name, gate._gate,
            gate._scan_jog_mm, gate._scan_max_mm,
            get_speed(gate),
            chunk_interval(gate, gate._scan_jog_mm),
            chunk_dwell(gate),
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

    try:
        tag_found = gate._poll()
    except Exception:
        logger.exception("nfc_gate: [%s] scan step poll error", gate._name)
        msg = "❌ NFC[%d]: scan poll failed" % gate._gate
        logger.error(msg)
        gate._console(msg)
        tag_found = False

    if tag_found:
        gate._finish_scan()
        return gate.reactor.NEVER

    if gate._scan_mm_total >= gate._scan_max_mm:
        logger.warning(
            "nfc_gate: [%s] scan mode: no tag after %.1fmm — rewinding",
            gate._name, gate._scan_mm_total)
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    # Only queue the next jog when the previous move is estimated complete
    # and the stationary dwell has elapsed. Poll continues at
    # scan_poll_interval during both motion and dwell.
    if now >= gate._scan_next_chunk_time:
        remaining = gate._scan_max_mm - gate._scan_mm_total
        chunk = min(gate._scan_jog_mm, remaining)
        next_position = gate._scan_mm_total + chunk
        msg = ("NFC[%d]: moving %.1fmm  scan position %.1f / %.1fmm"
               % (gate._gate, chunk, next_position, gate._scan_max_mm))
        logger.info(msg)
        gate._console(msg)
        if gate._debug >= 4:
            logger.debug("NFC[%d]: run_script MMU_TEST_MOVE MOVE=%.2f QUIET=1",
                         gate._gate, chunk)
        gate._run_jog(chunk)
        gate._scan_mm_total += chunk
        gate._scan_next_chunk_time = (
            now + chunk_interval(gate, chunk) + chunk_dwell(gate))
        logger.info(
            "NFC[%d]: move queued %.1fmm  scan position %.1f / %.1fmm",
            gate._gate, chunk, gate._scan_mm_total, gate._scan_max_mm)

    return now + gate._scan_poll_interval


def finish(gate):
    gate._scan_mode = False
    gate._state.miss_count = 0
    found_msg = "😊 NFC[%d]: tag found" % gate._gate
    info_both(found_msg)
    gate._console(found_msg)
    msg = "⏪ NFC[%d]: rewinding %.1fmm" % (gate._gate, gate._scan_mm_total)
    logger.info(msg)
    gate._console(msg)
    gate._run_rewind()
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
            msg = "✅ NFC[%d]: spool %s assigned" % (g, spool)
            info_both(msg)
            gate._console(msg)
        elif event_type == 'changed' and meta is not None:
            msg = "✅ NFC[%d]: tag metadata assigned" % g
            info_both(msg)
            gate._console(msg)
        elif event_type == 'uid_only':
            msg = "⚠️ NFC[%d]: tag has no Spoolman match" % g
            logger.warning(msg)
            gate._console(msg)
    gate._scan_previous_uid = None
    gate._scan_previous_spool = None
    gate._resume_poll_after_rewind()


def rewind_and_exit(gate):
    gate._scan_mode = False
    gate._state.miss_count = 0
    msg = "⚠️ NFC[%d]: no tag found — ⏪ rewinding %.1fmm" % (
        gate._gate, gate._scan_mm_total)
    logger.warning(msg)
    gate._console(msg)
    gate._run_rewind()
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
    gate._resume_poll_after_rewind()


def console(gate, msg):
    """Send a message directly to the Klipper console, bypassing the logger."""
    gcode = gate.printer.lookup_object('gcode', None)
    if gcode is None:
        return
    try:
        gcode.respond_info(msg)
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


def run_rewind(gate):
    if gate._scan_mm_total <= 0.0:
        return
    gcode = gate.printer.lookup_object('gcode')
    gcode.run_script("MMU_TEST_MOVE MOVE=%.2f QUIET=1\nM400"
                     % (-(gate._scan_mm_total - 10)))
    gcode.run_script("mmu_check_gate")
