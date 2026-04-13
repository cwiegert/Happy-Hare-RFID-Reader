# klippy/extras/nfc_gates/manager.py
#
# All gate coordination logic for both hardware paths:
#
#   NFCGateDefaults  — shared config defaults from the base [nfc_gate] section
#   NFCGate          — per-lane manager for [nfc_gate laneN] (one PN532 per EBB42)
#   NFCGateManager   — shared-MCU manager for [nfc_gates] (RC522/PN532 on a Pico)
#
# Internal helpers (not imported externally):
#   GateState        — per-gate debounce state machine
#   KlipperInterface — thread-safe GCode macro dispatcher
#
# Threading model
# ───────────────
# NFC polling runs in a background thread.  GCode execution must happen in
# the Klipper reactor thread.  reactor.register_callback() is thread-safe and
# is used as the inter-thread dispatch mechanism.  SPI/I2C transactions block
# the background thread while awaiting MCU responses; the reactor continues
# processing other events normally.
#
# Ownership boundaries
# ────────────────────
# Reader drivers are hardware/protocol adapters only.  PN532Driver and
# RC522Driver read tag identity and return UID values; they do not know about
# lanes, Spoolman records, Happy Hare, or spool assignment policy.
#
# SpoolmanClient is a lookup/cache client only.  It resolves UID → spool record
# / spool_id and may discover the Spoolman URL from Moonraker, but it does not
# own gates and must not issue Happy Hare commands or write gate assignments.
#
# NFCGate / NFCGateManager own the lane/gate state machine.  They decide
# whether a read is unchanged, changed, UID-only, or removed, and they are the
# only layer that orchestrates Happy Hare-facing commands.  All MMU_GATE_MAP
# and MMU_SPOOLMAN calls should flow from this manager so Happy Hare remains
# the source of truth for gate maps and Spoolman synchronization.
#
# Intended command flow:
#   New spool:  MMU_GATE_MAP GATE=<gate> SPOOLID=<spool_id>
#               MMU_SPOOLMAN UPDATE=1 GATE=<gate> SPOOLID=<spool_id>
#   UID only:   _NFC_TAG_NO_SPOOL GATE=<gate> UID=<uid>
#   Removed:    MMU_GATE_MAP GATE=<gate> SPOOLID=-1
#               MMU_SPOOLMAN UPDATE=1 GATE=<gate>
#   Same tag:   no command

import re
import threading
import time

import bus as bus_module

from .log            import configure, logger
from .pn532_driver   import (
    PN532Driver,
    PN532_COMMAND_GETFIRMWAREVERSION,
    PN532_COMMAND_SAMCONFIGURATION,
    PN532_COMMAND_INLISTPASSIVETARGET,
)
from .rc522_driver   import RC522Driver
from .spoolman_client import SpoolmanClient


def _get_console_config(config, default_enabled=False, default_level='warning'):
    """
    Read UI/console logging settings.

    console_* is the preferred spelling.  ui_* is accepted as a Happy Hare
    style alias for users already thinking in those terms.
    """
    enabled = config.getboolean('console_output',
                                config.getboolean('ui_output',
                                                  default_enabled))
    level = config.get('console_log_level',
                       config.get('ui_log_level', default_level))
    return enabled, level


def _get_low_level_debug(config, default=False):
    """Read the guarded raw PN532 debug flag."""
    return config.getboolean(
        'low_level_debug',
        config.getboolean('Low_Level_debug', default))


def _parse_hex_bytes(value):
    value = value.replace(',', ' ').replace(':', ' ').replace('-', ' ')
    data = []
    for token in value.split():
        token = token.strip().strip('"\'')
        if not token:
            continue
        if token.lower().startswith('0x'):
            token = token[2:]
        data.append(int(token, 16) & 0xFF)
    return data


def _hex(data):
    return ' '.join('%02X' % (b & 0xFF) for b in data)


def _low_level_requested(gcmd):
    return (
        gcmd.get_int("HELP", 0) or
        gcmd.get("STEP", None) is not None or
        gcmd.get_int("RAW_READ", 0) or
        gcmd.get("RAW_WRITE", None) is not None or
        gcmd.get("RAW_CMD", None) is not None or
        gcmd.get_int("READY_READ", 0) or
        gcmd.get_int("ACK_READ", 0))


def _low_level_help_lines(command_base):
    return [
        "PN532 is NOT initialized. Run Phase 1 + Phase 2 before anything else.",
        "--- Phase 1: Wake and firmware check (REQUIRED) ---",
        "1. %s STEP=WAKEUP" % command_base,
        "2. %s STEP=READY" % command_base,
        "3. %s STEP=FIRMWARE_WRITE" % command_base,
        "4. %s STEP=FIRMWARE_ACK" % command_base,
        "5. %s STEP=FIRMWARE_READY" % command_base,
        "6. %s STEP=FIRMWARE_RESPONSE" % command_base,
        "   Direct ACK timing probe (optional):",
        "   %s STEP=FIRMWARE_ACK_DIRECT DELAY=0.050" % command_base,
        "--- Phase 2: SAMConfiguration (REQUIRED) ---",
        "7. %s STEP=SAM_WRITE" % command_base,
        "8. %s STEP=SAM_ACK" % command_base,
        "9. %s STEP=SAM_READY" % command_base,
        "10. %s STEP=SAM_RESPONSE" % command_base,
        "--- Phase 3: Tag detect (optional, requires Phase 1 + 2) ---",
        "11. %s STEP=PASSIVE_WRITE" % command_base,
        "12. %s STEP=PASSIVE_ACK" % command_base,
        "13. %s STEP=PASSIVE_READY" % command_base,
        "14. %s STEP=PASSIVE_RESPONSE LEN=30" % command_base,
        "--- Raw tools ---",
        "%s RAW_READ=1 LEN=1" % command_base,
        "%s RAW_WRITE=00" % command_base,
        "%s RAW_CMD=02" % command_base,
        "%s READY_READ=1" % command_base,
        "%s ACK_READ=1 LEN=7" % command_base,
    ]


def _low_level_response(gcmd, label, message):
    gcmd.respond_info("NFC_GATE[%s]: %s" % (label, message))


def _low_level_next(gcmd, label, command_base, next_args):
    _low_level_response(gcmd, label, "NEXT: %s %s" %
                        (command_base, next_args))


def _low_level_write(gcmd, reader, label, op, data):
    _low_level_response(gcmd, label, "%s WRITE before: %s" %
                        (op, _hex(data)))
    written = reader.low_level_raw_write(data)
    _low_level_response(gcmd, label, "%s WRITE after: OK" % op)
    return written


def _low_level_command_write(gcmd, reader, label, op, cmd_and_params):
    frame = reader.low_level_command_frame(cmd_and_params)
    _low_level_write(gcmd, reader, label, op, frame)
    return frame


def _low_level_read(gcmd, reader, label, op, length):
    _low_level_response(gcmd, label, "%s READ before: %d byte(s)" %
                        (op, length))
    data = reader.low_level_raw_read(length)
    _low_level_response(gcmd, label, "%s READ after: %s" %
                        (op, _hex(data)))
    return data


def _low_level_ready(gcmd, reader, label):
    data = _low_level_read(gcmd, reader, label, "READY", 1)
    if not data:
        _low_level_response(gcmd, label, "READY result: no bytes returned")
        return False
    if data[0] == 0x01:
        _low_level_response(gcmd, label, "READY result: ready (0x01)")
        return True
    elif data[0] == 0x00:
        _low_level_response(gcmd, label, "READY result: busy (0x00)")
    else:
        _low_level_response(gcmd, label,
                            "READY result: unknown status 0x%02X" % data[0])
    return False


def _low_level_ack(gcmd, reader, label, command_base, length):
    ready = _low_level_read(gcmd, reader, label, "ACK_READY", 1)
    if not ready:
        _low_level_response(gcmd, label, "ACK_READY result: no bytes returned")
        return False
    if ready[0] != 0x01:
        _low_level_response(
            gcmd, label,
            "ACK_READY result: busy/unknown 0x%02X; not reading ACK yet" %
            ready[0])
        _low_level_next(gcmd, label, command_base, "STEP=%s" %
                        gcmd.get("STEP", "FIRMWARE_ACK").upper())
        return False
    ack = _low_level_read(gcmd, reader, label, "ACK", length)
    return _low_level_report_ack(gcmd, label, "ACK", ack, length)


def _low_level_report_ack(gcmd, label, op, ack, length):
    if not ack:
        _low_level_response(gcmd, label, "%s result: no bytes returned" % op)
        return False
    if length < 7:
        _low_level_response(gcmd, label,
                            "%s probe only: read %d byte(s), raw=%s" %
                            (op, length, _hex(ack)))
        _low_level_response(gcmd, label,
                            "Try the same ACK step with LEN=%d next" %
                            min(length + 1, 7))
        return False
    elif len(ack) >= 7 and ack[1:] == [0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00]:
        _low_level_response(gcmd, label, "%s status byte: 0x%02X" %
                            (op, ack[0]))
        _low_level_response(gcmd, label, "%s frame: %s" %
                            (op, _hex(ack[1:])))
        _low_level_response(gcmd, label,
                            "%s result: valid PN532 ACK" % op)
        return True
    elif ack == [0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00]:
        _low_level_response(gcmd, label, "%s frame: %s" %
                            (op, _hex(ack)))
        _low_level_response(gcmd, label,
                            "%s result: valid PN532 ACK" % op)
        return True
    else:
        _low_level_response(gcmd, label,
                            "%s result: invalid, expected 00 00 FF 00 FF 00"
                            % op)
        return False


def _low_level_parse_response(gcmd, label, name, data, expected_cmd):
    if not data:
        _low_level_response(gcmd, label, "%s response: no bytes returned" % name)
        return False
    status = None
    if len(data) >= 4 and data[0] == 0x00 and data[1] == 0x00 and \
            data[2] == 0xFF:
        frame = data
    else:
        status = data[0]
        frame = data[1:]
    if status is not None:
        _low_level_response(gcmd, label, "%s status byte: 0x%02X" %
                            (name, status))
    if len(frame) >= 7 and frame[0] == 0x00 and frame[1] == 0x00 and \
            frame[2] == 0xFF and frame[5] == 0xD5 and frame[6] == expected_cmd:
        if expected_cmd == 0x03 and len(frame) >= 11:
            _low_level_response(
                gcmd, label,
                "Firmware parsed: v%d.%d IC=0x%02X support=0x%02X" %
                (frame[8], frame[9], frame[7], frame[10]))
        elif expected_cmd == 0x15:
            _low_level_response(gcmd, label, "SAM response parsed: OK")
        elif expected_cmd == 0x4B:
            _low_level_response(gcmd, label,
                                "Passive response parsed header: OK")
        return True
    _low_level_response(gcmd, label,
                        "%s response did not match expected PN532 frame" % name)
    return False


def _run_low_level_debug(gcmd, reader, label, command_base, enabled):
    if not _low_level_requested(gcmd):
        return False
    if not enabled:
        _low_level_response(gcmd, label,
                            "low_level_debug is disabled in config")
        return True
    if not hasattr(reader, 'low_level_raw_read'):
        _low_level_response(gcmd, label,
                            "reader does not support low-level debug")
        return True

    raw_write = gcmd.get("RAW_WRITE", None)
    if raw_write is not None:
        data = _parse_hex_bytes(raw_write)
        _low_level_write(gcmd, reader, label, "RAW", data)
        _low_level_next(gcmd, label, command_base, "RAW_READ=1 LEN=1")
        return True
    raw_cmd = gcmd.get("RAW_CMD", None)
    if raw_cmd is not None:
        cmd = _parse_hex_bytes(raw_cmd)
        _low_level_command_write(gcmd, reader, label, "RAW_CMD", cmd)
        _low_level_next(gcmd, label, command_base, "ACK_READ=1 LEN=7")
        return True
    if gcmd.get_int("RAW_READ", 0):
        length = gcmd.get_int("LEN", 1, minval=1, maxval=64)
        _low_level_read(gcmd, reader, label, "RAW", length)
        return True
    if gcmd.get_int("READY_READ", 0):
        _low_level_ready(gcmd, reader, label)
        return True
    if gcmd.get_int("ACK_READ", 0):
        length = gcmd.get_int("LEN", 7, minval=1, maxval=64)
        _low_level_ack(gcmd, reader, label, command_base, length)
        return True

    step = gcmd.get("STEP", "HELP").upper()
    if step == "HELP":
        gcmd.respond_info('\n'.join(_low_level_help_lines(command_base)))
    elif step == "WAKEUP":
        _low_level_write(gcmd, reader, label, "WAKEUP", [0x00])
        time.sleep(0.05)
        _low_level_next(gcmd, label, command_base, "STEP=READY")
    elif step == "READY":
        if _low_level_ready(gcmd, reader, label):
            _low_level_next(gcmd, label, command_base, "STEP=FIRMWARE_WRITE")
    elif step == "FIRMWARE_WRITE":
        _low_level_command_write(
            gcmd, reader, label, "FIRMWARE",
            [PN532_COMMAND_GETFIRMWAREVERSION])
        _low_level_next(gcmd, label, command_base, "STEP=FIRMWARE_ACK")
    elif step == "FIRMWARE_ACK":
        if _low_level_ack(gcmd, reader, label, command_base,
                          gcmd.get_int("LEN", 7, minval=1, maxval=64)):
            _low_level_next(gcmd, label, command_base, "STEP=FIRMWARE_READY")
    elif step == "FIRMWARE_READY":
        if _low_level_ready(gcmd, reader, label):
            _low_level_next(gcmd, label, command_base,
                            "STEP=FIRMWARE_RESPONSE")
    elif step == "FIRMWARE_RESPONSE":
        data = _low_level_read(gcmd, reader, label, "FIRMWARE_RESPONSE",
                               gcmd.get_int("LEN", 14,
                                            minval=1, maxval=64))
        if _low_level_parse_response(gcmd, label, "Firmware", data, 0x03):
            _low_level_next(gcmd, label, command_base, "STEP=SAM_WRITE")
    elif step == "FIRMWARE_ACK_DIRECT":
        delay = gcmd.get_float("DELAY", 0.050, minval=0.0, maxval=2.0)
        _low_level_command_write(
            gcmd, reader, label, "FIRMWARE_DIRECT",
            [PN532_COMMAND_GETFIRMWAREVERSION])
        _low_level_response(
            gcmd, label,
            "FIRMWARE_DIRECT waiting %.3f seconds before ACK read" % delay)
        time.sleep(delay)
        length = gcmd.get_int("LEN", 7, minval=1, maxval=64)
        data = _low_level_read(gcmd, reader, label,
                               "FIRMWARE_DIRECT_ACK", length)
        if _low_level_report_ack(gcmd, label, "FIRMWARE_DIRECT_ACK",
                                 data, length):
            _low_level_next(gcmd, label, command_base,
                            "STEP=FIRMWARE_READY")
    elif step == "SAM_WRITE":
        _low_level_command_write(
            gcmd, reader, label, "SAM",
            [PN532_COMMAND_SAMCONFIGURATION, 0x01, 0x14, 0x01])
        _low_level_next(gcmd, label, command_base, "STEP=SAM_ACK")
    elif step == "SAM_ACK":
        if _low_level_ack(gcmd, reader, label, command_base,
                          gcmd.get_int("LEN", 7, minval=1, maxval=64)):
            _low_level_next(gcmd, label, command_base, "STEP=SAM_READY")
    elif step == "SAM_READY":
        if _low_level_ready(gcmd, reader, label):
            _low_level_next(gcmd, label, command_base, "STEP=SAM_RESPONSE")
    elif step == "SAM_RESPONSE":
        data = _low_level_read(gcmd, reader, label, "SAM_RESPONSE",
                               gcmd.get_int("LEN", 9,
                                            minval=1, maxval=64))
        if _low_level_parse_response(gcmd, label, "SAM", data, 0x15):
            _low_level_next(gcmd, label, command_base, "STEP=PASSIVE_WRITE")
    elif step == "PASSIVE_WRITE":
        _low_level_command_write(
            gcmd, reader, label, "PASSIVE",
            [PN532_COMMAND_INLISTPASSIVETARGET, 0x01, 0x00])
        _low_level_next(gcmd, label, command_base, "STEP=PASSIVE_ACK")
    elif step == "PASSIVE_ACK":
        if _low_level_ack(gcmd, reader, label, command_base,
                          gcmd.get_int("LEN", 7, minval=1, maxval=64)):
            _low_level_next(gcmd, label, command_base, "STEP=PASSIVE_READY")
    elif step == "PASSIVE_READY":
        if _low_level_ready(gcmd, reader, label):
            _low_level_next(gcmd, label, command_base,
                            "STEP=PASSIVE_RESPONSE LEN=30")
    elif step == "PASSIVE_RESPONSE":
        data = _low_level_read(gcmd, reader, label, "PASSIVE_RESPONSE",
                               gcmd.get_int("LEN", 30,
                                            minval=1, maxval=64))
        if data:
            _low_level_response(
                gcmd, label,
                "Passive response raw includes leading transport/status byte")
        _low_level_parse_response(gcmd, label, "Passive", data, 0x4B)
    else:
        _low_level_response(gcmd, label, "Unknown STEP=%s" % step)
        gcmd.respond_info('\n'.join(_low_level_help_lines(command_base)))
    return True


# ─────────────────────────────────────────────────────────────────────────────
# GateState — per-gate debounce state machine
# ─────────────────────────────────────────────────────────────────────────────
#
# On each poll cycle, call process_read() with the result from read_tag().
# Returns an event tuple only when state changes; returns None when nothing
# changed, keeping GCode traffic minimal.
#
# Removal debounce: a single missed read is not treated as removal — the tag
# must be absent for absent_threshold consecutive polls before a REMOVED event
# fires.  At the default 30 s interval, 3 misses ≈ 90 s of real absence.

EVENT_CHANGED  = 'changed'   # New or replaced spool
EVENT_UID_ONLY = 'uid_only'  # Tag present but UID not in Spoolman
EVENT_REMOVED  = 'removed'   # Tag gone after absent_threshold misses


class GateState:
    def __init__(self, gate, absent_threshold=3):
        self.gate             = gate
        self.current_uid      = None
        self.current_spool    = None
        self.miss_count       = 0
        self.absent_threshold = absent_threshold

    def process_read(self, uid_hex, spool_id):
        if uid_hex is not None:
            self.miss_count = 0
            if self.current_uid == uid_hex and self.current_spool == spool_id:
                return None
            self.current_uid   = uid_hex
            self.current_spool = spool_id
            if spool_id is not None:
                return (EVENT_CHANGED, self.gate, uid_hex, spool_id)
            return (EVENT_UID_ONLY, self.gate, uid_hex, None)
        else:
            self.miss_count += 1
            if self.miss_count >= self.absent_threshold and self.current_uid is not None:
                old_spool          = self.current_spool
                self.current_uid   = None
                self.current_spool = None
                return (EVENT_REMOVED, self.gate, None, old_spool)
            return None

    def __repr__(self):
        if self.current_uid is None:
            return "Gate({} empty, misses={})".format(self.gate, self.miss_count)
        return "Gate({} uid={} spool={} misses={})".format(
            self.gate, self.current_uid, self.current_spool, self.miss_count)


# ─────────────────────────────────────────────────────────────────────────────
# KlipperInterface — reactor-thread GCode macro dispatcher
# ─────────────────────────────────────────────────────────────────────────────
#
# Receives gate change events from the background polling thread and
# dispatches them as GCode macro calls in the Klipper reactor thread.
#
# Macros called (define these in printer.cfg / nfc_macros.cfg):
#
#   _NFC_SPOOL_CHANGED  GATE=<n>  SPOOL_ID=<id>  UID=<hex>
#   _NFC_SPOOL_REMOVED  GATE=<n>
#   _NFC_TAG_NO_SPOOL   GATE=<n>  UID=<hex>

class KlipperInterface:
    def __init__(self, printer, reactor):
        self._printer = printer
        self._reactor = reactor

    def dispatch(self, event_type, gate, uid_hex, spool_id):
        """Schedule a GCode macro call for the given gate event.  Thread-safe."""
        self._reactor.register_callback(
            lambda e, et=event_type, g=gate, u=uid_hex, s=spool_id:
                self._run_gcode(et, g, u, s))

    def _run_gcode(self, event_type, gate, uid_hex, spool_id):
        gcode = self._printer.lookup_object('gcode')
        try:
            if event_type == EVENT_CHANGED:
                script = "_NFC_SPOOL_CHANGED GATE={} SPOOL_ID={} UID={}".format(
                    gate, spool_id, uid_hex)
                logger.info("nfc_gates: gate %d → spool %d detected (UID %s)",
                             gate, spool_id, uid_hex)
            elif event_type == EVENT_UID_ONLY:
                script = "_NFC_TAG_NO_SPOOL GATE={} UID={}".format(gate, uid_hex)
                logger.info("nfc_gates: gate %d → tag %s (no spool ID in Spoolman)",
                             gate, uid_hex)
            elif event_type == EVENT_REMOVED:
                script = "_NFC_SPOOL_REMOVED GATE={}".format(gate)
                logger.info("nfc_gates: gate %d → spool removed (was spool_id=%s)",
                             gate, spool_id)
            else:
                logger.warning("nfc_gates: unknown event type %r", event_type)
                return
            gcode.run_script(script)
        except Exception:
            logger.exception("nfc_gates: GCode dispatch failed for gate %d event %r",
                              gate, event_type)


# ─────────────────────────────────────────────────────────────────────────────
# NFCGateDefaults / NFCGate — per-lane I2C/PN532 path
# ─────────────────────────────────────────────────────────────────────────────
#
# One NFCGate instance per [nfc_gate laneN] config section.
# Each manages a single PN532 on one EBB42 lane board (I2C, per-lane MCU).
#
# NFCGateDefaults holds shared values from the optional base [nfc_gate]
# section.  Lane sections inherit these and can override any key locally.

# Module-level registry for NFC_GATE_STATUS across all configured lanes.
_lane_instances = []


def _lane_status_lines(printer):
    """Build NFC_GATE_STATUS output lines cross-referenced against the MMU
    lane MCUs registered in Klipper (mirrors how HH reads [board_pins lane]).

    For each lane MCU (e.g. lane0…lane4):
      - If an NFCGate is configured for that MCU → show its spool/UID state.
      - If no NFCGate is configured         → note that no reader is set up.
    Falls back to listing _lane_instances directly when no lane MCUs are found.
    """
    # Collect MCU names that match "lane<N>" from Klipper's object registry.
    lane_names = []
    for obj_name, _ in printer.lookup_objects('mcu'):
        parts = obj_name.split(None, 1)
        if len(parts) == 2 and re.match(r'^lane\d+$', parts[1]):
            lane_names.append(parts[1])
    lane_names.sort(key=lambda n: int(n[4:]))

    nfc_by_lane = {gate._name: gate for gate in _lane_instances}

    if not lane_names:
        # No MMU lane MCUs visible — fall back to plain list.
        if not nfc_by_lane:
            return ["No [nfc_gate] sections are configured."]
        lines = ["NFC gate status  (%d gate%s configured):"
                 % (len(nfc_by_lane), 's' if len(nfc_by_lane) != 1 else '')]
        for gate in sorted(_lane_instances, key=lambda g: g._gate):
            lines.append(gate.status_line())
        return lines

    lines = ["NFC gate status — %d MMU lane(s), %d NFC reader(s) configured:"
             % (len(lane_names), len(nfc_by_lane))]
    for lane in lane_names:
        if lane in nfc_by_lane:
            lines.append(nfc_by_lane[lane].status_line())
        else:
            lines.append("  %-8s  no NFC reader configured" % (lane + ':'))
    return lines


class NFCGateDefaults:
    def __init__(self, config):
        self.spoolman_url       = config.get('spoolman_url', '')
        self.moonraker_url      = config.get('moonraker_url',
                                             'http://127.0.0.1:7125')
        self.spoolman_rfid_key  = config.get('spoolman_rfid_key', 'rfid')
        self.spoolman_timeout   = config.getfloat('spoolman_timeout', 5.0,
                                                   minval=0.5, maxval=30.0)
        self.spoolman_cache_ttl = config.getfloat('spoolman_cache_ttl', 300.0,
                                                   minval=0., maxval=3600.)
        self.poll_interval      = config.getfloat('poll_interval', 30.,
                                                   minval=1., maxval=3600.)
        self.absent_threshold   = config.getint('absent_threshold', 3,
                                                 minval=1, maxval=255)
        self.transceive_delay   = config.getfloat('transceive_delay', 0.250,
                                                   minval=0.050, maxval=2.0)
        self.crc_delay          = config.getfloat('crc_delay', 0.050,
                                                   minval=0.005, maxval=1.0)
        self.debug              = config.getint('debug', 1, minval=0, maxval=2)
        self.console_output, self.console_log_level = _get_console_config(config)
        self.low_level_debug    = _get_low_level_debug(config)
        self.i2c_address        = config.getint('i2c_address', 0x24,
                                                 minval=0, maxval=127)

        self._printer = config.get_printer()
        gcode         = self._printer.lookup_object('gcode')
        gcode.register_command(
            'NFC_GATE_STATUS', self.cmd_NFC_GATE_STATUS,
            desc="Report spool state for all configured NFC gates")

        log_file = config.get('log_file', '')
        try:
            configure(log_file, printer=self._printer,
                      console_output=self.console_output,
                      console_log_level=self.console_log_level)
        except Exception as e:
            import logging
            logging.getLogger().warning(
                "nfc_gate: could not configure NFC logging %r: %s",
                log_file, e)

    def cmd_NFC_GATE_STATUS(self, gcmd):
        gcmd.respond_info('\n'.join(_lane_status_lines(self._printer)))


class NFCGate:
    def __init__(self, config, defaults=None):
        self.printer  = config.get_printer()
        self.reactor  = self.printer.get_reactor()
        self._name    = config.get_name().split()[-1]

        d = defaults
        self._gate             = config.getint('mmu_gate', minval=0)
        self._poll_interval    = config.getfloat('poll_interval',
                                                  d.poll_interval if d else 30.,
                                                  minval=1., maxval=3600.)
        self._absent_threshold = config.getint('absent_threshold',
                                                d.absent_threshold if d else 3,
                                                minval=1, maxval=255)
        transceive_delay       = config.getfloat('transceive_delay',
                                                  d.transceive_delay if d else 0.250,
                                                  minval=0.050, maxval=2.0)
        crc_delay              = config.getfloat('crc_delay',
                                                  d.crc_delay if d else 0.050,
                                                  minval=0.005, maxval=1.0)
        self._debug            = config.getint('debug',
                                               d.debug if d else 1,
                                               minval=0, maxval=2)
        self._low_level_debug  = _get_low_level_debug(
            config, d.low_level_debug if d else False)
        console_output, console_log_level = _get_console_config(
            config,
            d.console_output if d else False,
            d.console_log_level if d else 'warning')
        if d is None:
            log_file = config.get('log_file', '')
            configure(log_file, printer=self.printer,
                      console_output=console_output,
                      console_log_level=console_log_level)

        spoolman_url       = config.get('spoolman_url',
                                        d.spoolman_url if d else '')
        moonraker_url      = config.get('moonraker_url',
                                        d.moonraker_url if d else 'http://127.0.0.1:7125')
        spoolman_rfid_key  = config.get('spoolman_rfid_key',
                                        d.spoolman_rfid_key if d else 'rfid')
        spoolman_timeout   = config.getfloat('spoolman_timeout',
                                              d.spoolman_timeout if d else 5.0,
                                              minval=0.5, maxval=30.0)
        spoolman_cache_ttl = config.getfloat('spoolman_cache_ttl',
                                              d.spoolman_cache_ttl if d else 300.0,
                                              minval=0., maxval=3600.)

        if spoolman_url:
            self._spoolman = SpoolmanClient(
                spoolman_url,
                rfid_key=spoolman_rfid_key,
                timeout=spoolman_timeout,
                cache_ttl=spoolman_cache_ttl,
                debug=self._debug,
                moonraker_url=moonraker_url)
            logger.info("nfc_gate: [%s] Spoolman enabled — url=%s rfid_key=%s",
                         self._name, spoolman_url, spoolman_rfid_key)
        else:
            self._spoolman = None
            logger.warning(
                "nfc_gate: [%s] spoolman_url not set — set spoolman_url in "
                "[nfc_gate] or [nfc_gate %s]. Use 'auto' to read Moonraker.",
                self._name, self._name)

        default_i2c_addr = d.i2c_address if d else 0x24
        i2c = bus_module.MCU_I2C_from_config(config,
                                              default_addr=default_i2c_addr,
                                              default_speed=100000)

        self._reader     = PN532Driver(i2c, self._gate,
                                       transceive_delay, crc_delay,
                                       self._debug,
                                       low_level_debug=self._low_level_debug)
        self._state      = GateState(self._gate, self._absent_threshold)
        self._failed     = False
        self._klipper    = KlipperInterface(self.printer, self.reactor)
        self._stop_event = threading.Event()
        self._thread     = threading.Thread(
            target=self._poll_loop,
            name='nfc-gate-%s' % self._name,
            daemon=True)

        # Register the status command when there is no base [nfc_gate] section
        # (defaults is None means load_config was never called, so
        # NFCGateDefaults.__init__ never ran and no one registered it yet).
        if defaults is None and not _lane_instances:
            gcode = self.printer.lookup_object('gcode')
            gcode.register_command(
                'NFC_GATE_STATUS', self._cmd_NFC_GATE_STATUS_fallback,
                desc="Report spool state for all configured NFC gates")
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command(
            cmd='NFC_GATE',
            key='NAME',
            value=self._name,
            func=self.cmd_NFC_GATE,
            desc="Control or test one configured NFC gate")

        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.printer.register_event_handler('klippy:disconnect',
                                            self._handle_disconnect)

    def _cmd_NFC_GATE_STATUS_fallback(self, gcmd):
        gcmd.respond_info('\n'.join(_lane_status_lines(self.printer)))

    def _cmd_help(self, gcmd):
        lines = [
            "NFC_GATE NAME=%s commands:" % self._name,
            "  NFC_GATE NAME=%s STATUS=1  - show this gate state" % self._name,
            "  NFC_GATE NAME=%s INIT=1    - re-run reader init" % self._name,
            "  NFC_GATE NAME=%s SCAN=1    - scan hardware once, no Spoolman/HH dispatch" % self._name,
            "  NFC_GATE NAME=%s POLL=1    - run one full NFC_Manager poll for this gate" % self._name,
            "  NFC_GATE NAME=%s READ=1    - start background polling" % self._name,
            "  NFC_GATE NAME=%s READ=0    - stop background polling" % self._name,
        ]
        if self._low_level_debug:
            lines.extend(_low_level_help_lines(
                "NFC_GATE NAME=%s" % self._name))
        gcmd.respond_info('\n'.join(lines))

    def _manual_scan(self, gcmd):
        try:
            target_info = self._reader.read_target()
            if target_info is None:
                gcmd.respond_info("NFC_GATE[%s]: no tag detected" % self._name)
                return
            gcmd.respond_info(
                "NFC_GATE[%s]: UID=%s Tg=%s SENS_RES=0x%04X SAK=0x%02X UIDLen=%d"
                % (self._name, target_info['uid'], target_info['target'],
                   target_info['sens_res'], target_info['sak'],
                   target_info['uid_length']))
        finally:
            if hasattr(self._reader, '_release_current_target'):
                self._reader._release_current_target(reason="manual_scan")

    def _manual_init(self, gcmd):
        self._failed = False
        try:
            self._reader.init()
            alive = self._reader.is_alive()
            self._failed = not alive
            gcmd.respond_info("NFC_GATE[%s]: reader %s" %
                              (self._name, "OK" if alive else "not responding"))
        except Exception as e:
            self._failed = True
            gcmd.respond_info("NFC_GATE[%s]: init failed: %s" %
                              (self._name, e))

    def _set_reading(self, gcmd, enabled):
        if enabled:
            if self._failed:
                gcmd.respond_info("NFC_GATE[%s]: reader failed; run INIT=1 first"
                                  % self._name)
                return
            self._stop_event.clear()
            if not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._poll_loop,
                    name='nfc-gate-%s' % self._name,
                    daemon=True)
                self._thread.start()
            gcmd.respond_info("NFC_GATE[%s]: polling started" % self._name)
        else:
            self._stop_event.set()
            gcmd.respond_info("NFC_GATE[%s]: polling stop requested" % self._name)

    def _cmd_low_level_debug(self, gcmd):
        try:
            return _run_low_level_debug(
                gcmd, self._reader, self._name,
                "NFC_GATE NAME=%s" % self._name,
                self._low_level_debug)
        except Exception as e:
            gcmd.respond_info("NFC_GATE[%s]: low-level debug failed: %s" %
                              (self._name, e))
            return True

    def cmd_NFC_GATE(self, gcmd):
        if self._cmd_low_level_debug(gcmd):
            return
        read_value = gcmd.get("READ", None)
        if read_value is not None:
            self._set_reading(gcmd, gcmd.get_int("READ", minval=0, maxval=1) == 1)
            return
        if gcmd.get_int("STATUS", 0):
            gcmd.respond_info(self.status_line())
            return
        if gcmd.get_int("INIT", 0):
            self._manual_init(gcmd)
            return
        if gcmd.get_int("SCAN", 0):
            self._manual_scan(gcmd)
            return
        if gcmd.get_int("POLL", 0):
            self._poll()
            gcmd.respond_info("NFC_GATE[%s]: one poll complete; %s" %
                              (self._name, self.status_line().strip()))
            return
        self._cmd_help(gcmd)

    def _handle_connect(self):
        logger.info(
            "nfc_gate: [%s] connected — gate=%d, poll=%.0fs, "
            "absent_threshold=%d, debug=%d",
            self._name, self._gate, self._poll_interval,
            self._absent_threshold, self._debug)
        if self._debug >= 2:
            logger.debug(
                "nfc_gate: [%s] calling reader.init() — "
                "wake + SAMConfiguration sequence starting", self._name)
        try:
            self._reader.init()
            if self._reader.is_alive():
                logger.info("nfc_gate: [%s] PN532 reader OK", self._name)
            else:
                self._failed = True
                logger.error(
                    "nfc_gate: [%s] PN532 did not respond — "
                    "check wiring and I2C address (default 0x24)", self._name)
        except Exception as e:
            self._failed = True
            logger.error("nfc_gate: [%s] init error: %s", self._name, e)

        gcode = self._printer.lookup_object('gcode')
        if self._failed:
            gcode.respond_info(
                "NFC[%s]: reader not ready — check wiring. "
                "Run NFC_GATE NAME=%s INIT=1 after fixing."
                % (self._name, self._name))
        else:
            gcode.respond_info(
                "NFC[%s]: reader ready. "
                "Run NFC_GATE NAME=%s READ=1 to start polling."
                % (self._name, self._name))

    def _handle_disconnect(self):
        if self._debug >= 2:
            logger.debug("nfc_gate: [%s] disconnect — stopping polling thread",
                          self._name)
        self._stop_event.set()

    def _poll_loop(self):
        logger.info("nfc_gate: [%s] polling thread started", self._name)
        while not self._stop_event.is_set():
            if self._debug >= 2:
                logger.debug("nfc_gate: [%s] poll cycle start — "
                              "current state: uid=%s spool=%s misses=%d",
                              self._name,
                              self._state.current_uid or 'none',
                              self._state.current_spool if self._state.current_spool is not None else 'none',
                              self._state.miss_count)
            try:
                self._poll()
            except Exception:
                logger.exception("nfc_gate: [%s] poll error", self._name)
            if self._debug >= 2:
                logger.debug("nfc_gate: [%s] poll cycle done — "
                              "sleeping %.0fs", self._name, self._poll_interval)
            self._stop_event.wait(timeout=self._poll_interval)
        logger.info("nfc_gate: [%s] polling thread stopped", self._name)

    def _poll(self):
        uid_hex = self._reader.read_tag()

        if uid_hex is None:
            if self._debug >= 1:
                logger.info("nfc_gate: [%s] gate %d — no tag (miss %d)",
                             self._name, self._gate, self._state.miss_count + 1)
        else:
            if self._debug >= 2:
                logger.debug("nfc_gate: [%s] gate %d — tag read uid=%s",
                              self._name, self._gate, uid_hex)

        if uid_hex is not None:
            if uid_hex == self._state.current_uid:
                spool_id = self._state.current_spool
                if self._debug >= 2:
                    logger.debug(
                        "nfc_gate: [%s] gate %d — uid=%s already known, "
                        "spool_id=%s (skipping Spoolman lookup)",
                        self._name, self._gate, uid_hex, spool_id)
            elif self._spoolman is not None:
                if self._debug >= 2:
                    logger.debug(
                        "nfc_gate: [%s] gate %d — new uid=%s, "
                        "querying Spoolman", self._name, self._gate, uid_hex)
                spool_id = self._spoolman.lookup_spool_by_uid(uid_hex)
                if self._debug >= 2:
                    logger.debug(
                        "nfc_gate: [%s] gate %d — Spoolman returned spool_id=%s",
                        self._name, self._gate, spool_id)
            else:
                spool_id = None
                if self._debug >= 2:
                    logger.debug(
                        "nfc_gate: [%s] gate %d — uid=%s, no Spoolman configured",
                        self._name, self._gate, uid_hex)
        else:
            spool_id = None

        event = self._state.process_read(uid_hex, spool_id)
        if event is not None:
            event_type, gate, uid, spool = event
            if self._debug >= 1:
                logger.info("nfc_gate: [%s] gate %d — %s uid=%s spool=%s",
                             self._name, gate, event_type, uid, spool)
            if self._debug >= 2:
                logger.debug("nfc_gate: [%s] gate %d — dispatching GCode "
                              "for event %s", self._name, gate, event_type)
            self._klipper.dispatch(event_type, gate, uid, spool)
        elif self._debug >= 2:
            logger.debug("nfc_gate: [%s] gate %d — no state change  "
                          "state=%r", self._name, self._gate, self._state)

    def status_line(self):
        if self._failed:
            return ("  Gate %d  [%s]:  READER FAILED (check wiring, address 0x24)"
                    % (self._gate, self._name))
        if self._state.current_spool is not None:
            return ("  Gate %d  [%s]:  spool %-6d   UID %s"
                    % (self._gate, self._name,
                       self._state.current_spool, self._state.current_uid))
        if self._state.current_uid is not None:
            return ("  Gate %d  [%s]:  tag %s  (UID not in Spoolman)"
                    % (self._gate, self._name, self._state.current_uid))
        return "  Gate %d  [%s]:  empty" % (self._gate, self._name)

    def get_status(self, _eventtime=None):
        return {
            'gate':     self._gate,
            'spool_id': self._state.current_spool if self._state.current_spool is not None else -1,
            'uid':      self._state.current_uid or '',
            'failed':   self._failed,
        }


# ─────────────────────────────────────────────────────────────────────────────
# NFCGateManager — shared-MCU orchestrator for [nfc_gates]
# ─────────────────────────────────────────────────────────────────────────────
#
# Handles 1–8 RC522 or PN532 readers all wired to a single CAN-connected MCU
# (typically a Raspberry Pi Pico running standard Klipper firmware).
#
# Reader selection:
#   gate_i2c_addresses present  → I2C / PN532 path
#   gate_i2c_addresses absent   → SPI / RC522 path

class NFCGateManager:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        ppins        = self.printer.lookup_object('pins')

        self._poll_interval    = config.getfloat('poll_interval', 30.,
                                                  minval=1., maxval=3600.)
        self._absent_threshold = config.getint('absent_threshold', 3,
                                                minval=1, maxval=255)
        transceive_delay = config.getfloat('transceive_delay', 0.035,
                                            minval=0.001, maxval=1.0)
        self._debug      = config.getint('debug', 1, minval=0, maxval=2)
        self._low_level_debug = _get_low_level_debug(config)
        console_output, console_log_level = _get_console_config(config)

        log_file = config.get('log_file', '')
        configure(log_file, printer=self.printer,
                  console_output=console_output,
                  console_log_level=console_log_level)

        spoolman_url       = config.get('spoolman_url', '')
        moonraker_url      = config.get('moonraker_url',
                                        'http://127.0.0.1:7125')
        spoolman_rfid_key  = config.get('spoolman_rfid_key', 'rfid')
        spoolman_timeout   = config.getfloat('spoolman_timeout', 5.0,
                                              minval=0.5, maxval=30.0)
        spoolman_cache_ttl = config.getfloat('spoolman_cache_ttl', 300.0,
                                              minval=0., maxval=3600.)

        if spoolman_url:
            self._spoolman = SpoolmanClient(
                spoolman_url,
                rfid_key=spoolman_rfid_key,
                timeout=spoolman_timeout,
                cache_ttl=spoolman_cache_ttl,
                debug=self._debug,
                moonraker_url=moonraker_url)
            logger.info("nfc_gates: Spoolman enabled — url=%s rfid_key=%s",
                         spoolman_url, spoolman_rfid_key)
        else:
            self._spoolman = None
            logger.warning(
                "nfc_gates: spoolman_url not set — gates will report UIDs "
                "but cannot resolve spool IDs.  Add spoolman_url to [nfc_gates] "
                "or set spoolman_url: auto to read Moonraker.")

        i2c_addrs_str = config.get('gate_i2c_addresses', '')
        if i2c_addrs_str:
            bus_objects   = self._setup_i2c(config, i2c_addrs_str)
            self._readers = [PN532Driver(
                b, i, transceive_delay, debug=self._debug,
                low_level_debug=self._low_level_debug)
                             for i, b in enumerate(bus_objects)]
        else:
            bus_objects   = self._setup_spi(config, ppins)
            self._readers = [RC522Driver(b, i, transceive_delay, debug=self._debug)
                             for i, b in enumerate(bus_objects)]

        self._gate_count = len(bus_objects)
        if not (1 <= self._gate_count <= 8):
            raise config.error(
                "nfc_gates: gate count must be 1–8; got %d" % self._gate_count)

        self._states        = [GateState(i, self._absent_threshold)
                               for i in range(self._gate_count)]
        self._reader_failed = [False] * self._gate_count
        self._klipper       = KlipperInterface(self.printer, self.reactor)
        self._stop_event    = threading.Event()
        self._thread        = threading.Thread(
            target=self._poll_loop, name='nfc-gates', daemon=True)

        gcode = self.printer.lookup_object('gcode')
        gcode.register_command(
            'NFC_GATE_STATUS', self.cmd_NFC_GATE_STATUS,
            desc="Report current NFC gate spool assignments")
        for i in range(self._gate_count):
            gcode.register_mux_command(
                cmd='NFC_GATE',
                key='NAME',
                value='gate%d' % i,
                func=lambda gcmd, gate=i: self.cmd_NFC_GATE(gcmd, gate),
                desc="Control or test one configured NFC gate")

        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.printer.register_event_handler('klippy:disconnect',
                                            self._handle_disconnect)

    def _setup_spi(self, config, ppins):
        spi_speed   = config.getint('spi_speed', 1000000, minval=100000)
        primary_spi = bus_module.MCU_SPI_from_config(config, mode=0,
                                                     default_speed=spi_speed)
        self._mcu   = primary_spi._mcu

        extra_cs_names = [p.strip()
                          for p in config.get('extra_cs_pins', '').split(',')
                          if p.strip()]
        all_spis = [primary_spi]
        for cs_name in extra_cs_names:
            cs_params = ppins.lookup_pin(cs_name, can_invert=False,
                                         can_pullup=False)
            if cs_params['chip'] is not self._mcu:
                raise config.error(
                    "nfc_gates: extra CS pin '%s' must be on the same MCU "
                    "as cs_pin" % cs_name)
            all_spis.append(bus_module.MCU_SPI(
                self._mcu,
                primary_spi._bus,
                cs_params['pin'],
                primary_spi._mode,
                primary_spi._speed,
                primary_spi._sw_pins,
            ))
        return all_spis

    def _setup_i2c(self, config, addrs_str):
        try:
            addrs = [int(a.strip(), 0)
                     for a in addrs_str.split(',') if a.strip()]
        except ValueError as e:
            raise config.error(
                "nfc_gates: gate_i2c_addresses parse error: %s" % e)

        i2c_speed   = config.getint('i2c_speed', 400000, minval=10000)
        primary_i2c = bus_module.MCU_I2C_from_config(config,
                                                     default_addr=addrs[0],
                                                     default_speed=i2c_speed)
        self._mcu   = primary_i2c._mcu

        all_i2cs = [primary_i2c]
        for addr in addrs[1:]:
            all_i2cs.append(bus_module.MCU_I2C(
                self._mcu, primary_i2c._bus, addr, i2c_speed))
        return all_i2cs

    def _handle_connect(self):
        logger.info(
            "nfc_gates: connected to MCU '%s', initialising %d gates "
            "(poll=%.0fs, absent_threshold=%d, debug=%d)",
            self._mcu.get_name(), self._gate_count,
            self._poll_interval, self._absent_threshold, self._debug)

        ok_count = 0
        for i, reader in enumerate(self._readers):
            try:
                reader.init()
                if reader.is_alive():
                    ok_count += 1
                    logger.info("nfc_gates: gate %d reader OK", i)
                else:
                    self._reader_failed[i] = True
                    logger.error("nfc_gates: gate %d reader did not respond "
                                  "after init (check wiring)", i)
            except Exception as e:
                self._reader_failed[i] = True
                logger.error("nfc_gates: gate %d init error: %s", i, e)

        logger.info("nfc_gates: %d/%d readers initialised",
                     ok_count, self._gate_count)

        self._stop_event.clear()
        if not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._poll_loop, name='nfc-gates', daemon=True)
            self._thread.start()

    def _handle_disconnect(self):
        self._stop_event.set()

    def _poll_loop(self):
        logger.info("nfc_gates: polling thread started")
        while not self._stop_event.is_set():
            try:
                self._poll_all_gates()
            except Exception:
                logger.exception("nfc_gates: unexpected error in poll cycle")
            self._stop_event.wait(timeout=self._poll_interval)
        logger.info("nfc_gates: polling thread stopped")

    def _poll_all_gates(self):
        if self._debug >= 1:
            logger.info("nfc_gates: poll cycle — checking %d gate(s)",
                         self._gate_count)

        for i in range(self._gate_count):
            self._poll_gate(i)

    def _poll_gate(self, i):
        if self._reader_failed[i]:
            if self._debug >= 2:
                logger.debug("nfc_gates: gate %d skipped (reader failed)", i)
            return

        try:
            uid_hex = self._readers[i].read_tag()
        except Exception as e:
            logger.error("nfc_gates: gate %d read error: %s", i, e)
            uid_hex = None

        if self._debug >= 1 and uid_hex is None:
            logger.info("nfc_gates: gate %d — no tag (miss_count=%d)",
                         i, self._states[i].miss_count + 1)

        if uid_hex is not None:
            if uid_hex == self._states[i].current_uid:
                spool_id = self._states[i].current_spool
            elif self._spoolman is not None:
                spool_id = self._spoolman.lookup_spool_by_uid(uid_hex)
            else:
                spool_id = None
        else:
            spool_id = None

        event = self._states[i].process_read(uid_hex, spool_id)
        if event is not None:
            event_type, gate, uid, spool = event
            if self._debug >= 1:
                logger.info("nfc_gates: gate %d — state change: %s "
                            "(uid=%s spool=%s)", i, event_type, uid, spool)
            self._klipper.dispatch(event_type, gate, uid, spool)
        elif self._debug >= 2:
            logger.debug("nfc_gates: gate %d — no state change (%r)",
                         i, self._states[i])

    def _gate_status_line(self, i):
        state = self._states[i]
        if self._reader_failed[i]:
            return "Gate %d: READER FAILED (check wiring)" % i
        if state.current_spool is not None:
            return "Gate %d: spool %-6d  UID %s" % (
                i, state.current_spool, state.current_uid)
        if state.current_uid is not None:
            return "Gate %d: tag %s (UID not in Spoolman)" % (
                i, state.current_uid)
        return "Gate %d: empty" % i

    def _manual_scan_gate(self, gcmd, i):
        try:
            reader = self._readers[i]
            if hasattr(reader, 'read_target'):
                target_info = reader.read_target()
                if target_info is None:
                    gcmd.respond_info("NFC_GATE[gate%d]: no tag detected" % i)
                    return
                gcmd.respond_info(
                    "NFC_GATE[gate%d]: UID=%s Tg=%s SENS_RES=0x%04X SAK=0x%02X UIDLen=%d"
                    % (i, target_info['uid'], target_info['target'],
                       target_info['sens_res'], target_info['sak'],
                       target_info['uid_length']))
            else:
                uid_hex = reader.read_tag()
                if uid_hex is None:
                    gcmd.respond_info("NFC_GATE[gate%d]: no tag detected" % i)
                    return
                gcmd.respond_info("NFC_GATE[gate%d]: UID=%s" % (i, uid_hex))
        finally:
            reader = self._readers[i]
            if hasattr(reader, '_release_current_target'):
                reader._release_current_target(reason="manual_scan")

    def _manual_init_gate(self, gcmd, i):
        self._reader_failed[i] = False
        try:
            self._readers[i].init()
            alive = self._readers[i].is_alive()
            self._reader_failed[i] = not alive
            gcmd.respond_info("NFC_GATE[gate%d]: reader %s" %
                              (i, "OK" if alive else "not responding"))
        except Exception as e:
            self._reader_failed[i] = True
            gcmd.respond_info("NFC_GATE[gate%d]: init failed: %s" % (i, e))

    def _cmd_low_level_debug_gate(self, gcmd, i):
        try:
            return _run_low_level_debug(
                gcmd, self._readers[i], "gate%d" % i,
                "NFC_GATE NAME=gate%d" % i,
                self._low_level_debug)
        except Exception as e:
            gcmd.respond_info("NFC_GATE[gate%d]: low-level debug failed: %s" %
                              (i, e))
            return True

    def cmd_NFC_GATE(self, gcmd, gate):
        if gate < 0 or gate >= self._gate_count:
            gcmd.respond_info("NFC_GATE: invalid gate %s" % gate)
            return

        if self._cmd_low_level_debug_gate(gcmd, gate):
            return

        read_value = gcmd.get("READ", None)
        if read_value is not None:
            enabled = gcmd.get_int("READ", minval=0, maxval=1) == 1
            if enabled:
                self._stop_event.clear()
                if not self._thread.is_alive():
                    self._thread = threading.Thread(
                        target=self._poll_loop, name='nfc-gates', daemon=True)
                    self._thread.start()
                gcmd.respond_info("NFC_GATE[gate%d]: polling started" % gate)
            else:
                self._stop_event.set()
                gcmd.respond_info("NFC_GATE[gate%d]: polling stop requested" % gate)
            return
        if gcmd.get_int("STATUS", 0):
            gcmd.respond_info("NFC_GATE[gate%d]: %s" %
                              (gate, self._gate_status_line(gate)))
            return
        if gcmd.get_int("INIT", 0):
            self._manual_init_gate(gcmd, gate)
            return
        if gcmd.get_int("SCAN", 0):
            self._manual_scan_gate(gcmd, gate)
            return
        if gcmd.get_int("POLL", 0):
            self._poll_gate(gate)
            gcmd.respond_info("NFC_GATE[gate%d]: one poll complete; %s" %
                              (gate, self._gate_status_line(gate)))
            return

        lines = [
            "NFC_GATE NAME=gate%d commands:" % gate,
            "  NFC_GATE NAME=gate%d STATUS=1" % gate,
            "  NFC_GATE NAME=gate%d INIT=1" % gate,
            "  NFC_GATE NAME=gate%d SCAN=1" % gate,
            "  NFC_GATE NAME=gate%d POLL=1" % gate,
            "  NFC_GATE NAME=gate%d READ=1" % gate,
            "  NFC_GATE NAME=gate%d READ=0" % gate,
        ]
        if self._low_level_debug:
            lines.extend(_low_level_help_lines(
                "NFC_GATE NAME=gate%d" % gate))
        gcmd.respond_info('\n'.join(lines))

    cmd_NFC_GATE_STATUS_help = (
        "Report current NFC gate spool assignments (host-side state mirror)")

    def cmd_NFC_GATE_STATUS(self, gcmd):
        lines = [
            "NFC gate status — %d gates, poll %.0fs, absent threshold %d:"
            % (self._gate_count, self._poll_interval, self._absent_threshold)
        ]
        for i, state in enumerate(self._states):
            lines.append("  " + self._gate_status_line(i))
        gcmd.respond_info('\n'.join(lines))
