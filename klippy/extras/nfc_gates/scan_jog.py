# klippy/extras/nfc_gates/scan_jog.py
#
# Scan-and-jog mode helpers for NFCGate.

from .log import logger


def manual_jog_scan(gate, gcmd):
    """Start scan-and-jog on demand, matching the automatic trigger path."""
    if gate._failed:
        gcmd.respond_info(
            "NFC_GATE[%s]: reader failed — run NFC_GATE GATE=%d INIT=1 first"
            % (gate._name, gate._gate))
        return
    if is_printing(gate):
        gcmd.respond_info(
            "NFC_GATE[%s]: print is active — cannot start scan-jog while printing"
            % gate._name)
        return
    hh = gate._read_hh_status()
    if hh.present and not hh.idle:
        gcmd.respond_info(
            "NFC_GATE[%s]: Happy Hare is busy (action=%s) — "
            "wait for idle before starting scan-jog"
            % (gate._name, hh.action))
        return
    if gate.__class__._active_scan_gate is not None:
        gcmd.respond_info(
            "NFC_GATE[%s]: gate %d is already scanning — "
            "only one gate may scan at a time"
            % (gate._name, gate.__class__._active_scan_gate))
        return
    if gate._scan_mode:
        gcmd.respond_info(
            "NFC_GATE[%s]: scan-jog already in progress for this gate"
            % gate._name)
        return

    gate.reactor.update_timer(gate._poll_timer, gate.reactor.NEVER)
    start(gate)
    gcmd.respond_info(
        "NFC_GATE[%s]: scan-jog started for gate %d "
        "(max=%.0fmm  poll=%.2fs)"
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
    return (abs(mm) / get_speed(gate)) + gate._scan_settle_time


def next_event_time(gate, mm):
    """Return when it is safe to read after a queued scan chunk."""
    return gate.reactor.monotonic() + max(
        chunk_interval(gate, mm),
        gate._scan_poll_interval)


def resume_poll_after_rewind(gate):
    """Restart regular polling after the queued rewind move can finish."""
    delay = gate._poll_interval
    if gate._scan_mm_total > 0.0:
        delay += chunk_interval(gate, gate._scan_mm_total)
    gate.reactor.update_timer(
        gate._poll_timer,
        gate.reactor.monotonic() + delay)


def start(gate):
    gate.__class__._active_scan_gate = gate._gate
    gate._scan_mode = True
    gate._scan_mm_total = 0.0
    gate._scan_next_chunk_time = gate.reactor.monotonic()
    gate._hh_seed_spool_id = None
    gate._hh_seed_available = False

    gate._scan_timer = gate.reactor.register_timer(
        gate._scan_step_event,
        gate.reactor.monotonic())
    if gate._debug >= 3:
        logger.info(
            "nfc_gate: [%s] gate %d scan mode started — "
            "chunk=%.1fmm max=%.1fmm speed=%.1fmm/s chunk_interval=%.2fs settle=%.2fs poll=%.2fs",
            gate._name, gate._gate,
            gate._scan_jog_mm, gate._scan_max_mm,
            get_speed(gate),
            chunk_interval(gate, gate._scan_jog_mm),
            gate._scan_settle_time,
            gate._scan_poll_interval)


def step_event(gate, eventtime):
    if not gate._scan_mode:
        return gate.reactor.NEVER

    now = gate.reactor.monotonic()
    if now < gate._scan_next_chunk_time:
        return gate._scan_next_chunk_time

    if is_printing(gate):
        logger.warning(
            "nfc_gate: [%s] scan mode: print started — aborting",
            gate._name)
        gate._rewind_and_exit_scan()
        return gate.reactor.NEVER

    try:
        tag_found = gate._poll()
    except Exception:
        logger.exception("nfc_gate: [%s] scan step poll error", gate._name)
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

    remaining = gate._scan_max_mm - gate._scan_mm_total
    chunk = min(gate._scan_jog_mm, remaining)
    gate._run_jog(chunk)
    gate._scan_mm_total += chunk
    gate._scan_next_chunk_time = next_event_time(gate, chunk)
    msg = ("NFC Gate[%d] - moved %.1fmm  total %.1fmm / %.1fmm"
           % (gate._gate, chunk, gate._scan_mm_total, gate._scan_max_mm))
    logger.info(msg)
    gate._console(msg)

    return gate._scan_next_chunk_time


def finish(gate):
    gate._scan_mode = False
    gate.__class__._active_scan_gate = None
    gate._state.miss_count = 0
    msg = "NFC Gate[%d]: rewinding %.1fmm" % (gate._gate, gate._scan_mm_total)
    logger.info(msg)
    gate._console(msg)
    gate._run_rewind()
    gate._resume_poll_after_rewind()


def rewind_and_exit(gate):
    gate._scan_mode = False
    gate.__class__._active_scan_gate = None
    gate._state.miss_count = 0
    msg = "NFC Gate[%d]: no tag found — rewinding %.1fmm" % (
        gate._gate, gate._scan_mm_total)
    logger.warning(msg)
    gate._console(msg)
    gate._run_rewind()
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
    gcode.run_script("MMU_SELECT GATE=%d\nMMU_TEST_MOVE MOVE=%.2f QUIET=1"
                     % (gate._gate, mm))


def run_rewind(gate):
    if gate._scan_mm_total <= 0.0:
        return
    gcode = gate.printer.lookup_object('gcode')
    gcode.run_script("MMU_SELECT GATE=%d\nMMU_TEST_MOVE MOVE=%.2f QUIET=1\nM400"
                     % (gate._gate, -gate._scan_mm_total))
