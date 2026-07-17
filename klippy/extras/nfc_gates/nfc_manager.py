# klippy/extras/nfc_gates/nfc_manager.py
#
# EMU NFC Gate Reader — gate manager
# Version 1.0.0  |  2026-04-14
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Gate coordination logic for per-lane and shared NFC Readers:
#
#   NFCGateDefaults  — shared config defaults from the base [nfc_gate] section
#   NFCGate          — per-lane manager for [nfc_gate laneN]
#
# Internal helpers (not imported externally):
#   GateState        — per-gate debounce state machine; owns process_read(),
#                      removal debounce, and event generation
#   CurrentTag       — dataclass holding the full tag observation for one read
#                      window: UID, reader target identity, raw tag pages,
#                      parsed metadata, parse errors, and resolution path;
#                      stored on GateState.current_tag; populated by
#                      _read_current_tag() and enriched by _resolve_spool()
#   KlipperInterface — thread-safe GCode macro dispatcher
#
# Threading model
# ───────────────
# NFC polling runs on Klipper reactor timers.  Klipper MCU I2C/SPI helpers use
# reactor greenlets internally, so hardware transactions must stay on the
# reactor thread.  Do not move reader polling into a normal Python thread.
#
# Ownership boundaries
# ────────────────────
# Reader drivers are hardware/protocol adapters only.  They read tag
# identity and returns UID values; it does not know about lanes, Spoolman
# records, Happy Hare, or spool assignment policy.
#
# SpoolmanClient is a lookup/cache client only.  It resolves UID → spool record
# / spool_id and may discover the Spoolman URL from Moonraker, but it does not
# own gates and must not issue Happy Hare commands or write gate assignments.
#
# NFCGate owns the lane/gate state machine.  It decides whether a read is
# unchanged, changed, UID-only, or removed, and it is the only layer that
# orchestrates Happy Hare-facing commands.  The default macro boundary uses
# MMU_GATE_MAP so Happy Hare remains the source of truth for gate maps and
# Spoolman synchronization.
#
# Intended command flow:
#   New spool:  _NFC_SPOOL_CHANGED GATE=<gate> SPOOL_ID=<spool_id> UID=<uid>
#   UID only:   _NFC_TAG_NO_SPOOL GATE=<gate> UID=<uid>
#   Removed:    _NFC_SPOOL_REMOVED GATE=<gate>
#   Same tag:   no command

import ast
import os
import re

from . import (hh_status, pn532_driver, rc522_driver, reader_factory, scan_jog,
               shared_preload, tag_handler)
from .LED_effect_mgr import (
    EVENT_AUTO_CREATE, EVENT_RELEASE, EVENT_SPOOL_READY, EVENT_TAG_READ,
    EVENT_UNRESOLVED, EVENT_WARNING, LEDEffectManager, shared_effect_name)
from .gate_state      import (CurrentTag, GateState,
                               EVENT_CHANGED, EVENT_UID_ONLY, EVENT_REMOVED,
                               DIRECT_METADATA_SPOOL)
from .klipper_interface import KlipperInterface
from .log              import configure, logger

try:
    from .log import color_console_tags
except ImportError:
    def color_console_tags(text):
        text = str(text)
        text = text.replace('[WARN]', '<span style="color:#FFFF00">[WARN]</span>')
        text = text.replace('[OK]', '<span style="color:#90EE90">[OK]</span>')
        text = text.replace('[ERROR]', '<span style="color:#FF6060">[ERROR]</span>')
        text = text.replace('[SCAN]', '<span style="color:#FFA040">[SCAN]</span>')
        text = text.replace('[MOVE]', '<span style="color:#FFA040">[MOVE]</span>')
        text = text.replace('[REWIND]', '<span style="color:#90EE90">[REWIND]</span>')
        return text
from .spoolman_client  import SpoolmanClient

LANE_LED_TEST_DURATION = 2.0
LANE_LED_TEST_GAP = 0.15
LANE_LED_TEST_DEFAULT_CYCLES = 2
LANE_LED_TEST_MAX_CYCLES = 20
STARTUP_UNKNOWN_GATE_CHECK_DELAY = 5.0
STARTUP_UNKNOWN_GATE_CHECK_STAGGER = 1.0


def _spoolman_url_enabled(url):
    value = str(url or '').strip().lower()
    return value not in ('', 'disabled', 'disable', 'false', 'off', 'none', 'no')


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


def _flag_param(gcmd, name):
    value = gcmd.get(name, None)
    if value is None:
        return False
    if value == '':
        return True
    try:
        return bool(gcmd.get_int(name, minval=0, maxval=1))
    except Exception:
        return bool(value)


def _gcmd_get_any(gcmd, names, default=None):
    for name in names:
        value = gcmd.get(name, None)
        if value is not None:
            return value
    return default


def _get_scan_motion_mode(config, default='continuous'):
    mode = str(config.get('scan_motion_mode', default) or '').strip().lower()
    if mode not in ('stopped', 'continuous'):
        raise config.error(
            "scan_motion_mode must be 'stopped' or 'continuous'")
    return mode


def _normalise_command_uid(uid):
    uid_norm = SpoolmanClient._normalise_uid(str(uid or ''))
    if not uid_norm:
        return None
    if len(uid_norm) % 2 != 0 or not re.match(r'^[0-9A-F]+$', uid_norm):
        return None
    return uid_norm


def _respond_register_error(gcmd, msg):
    logger.error(msg, extra={'nfc_no_console': True})
    gcmd.respond_info(color_console_tags(msg))


def _cmd_register_uid_to_spool(gcode, spoolman, gcmd):
    uid = _normalise_command_uid(_gcmd_get_any(gcmd, ('UID', 'Uid', 'uid')))
    spool_raw = _gcmd_get_any(
        gcmd, ('SPOOL_ID', 'Spool_id', 'spool_id', 'SPOOLID', 'Spoolid'))

    if not uid:
        _respond_register_error(
            gcmd, "[ERROR] NFC: NFC_REGISTER requires UID=TAG_UID")
        return

    try:
        spool_id = int(spool_raw)
    except Exception:
        _respond_register_error(
            gcmd, "[ERROR] NFC: NFC_REGISTER requires SPOOL_ID=SPOOL_ID")
        return
    if spool_id <= 0:
        _respond_register_error(
            gcmd, "[ERROR] NFC: SPOOL_ID must be greater than 0")
        return

    if spoolman is None:
        msg = "[ERROR] NFC: Spoolman is disabled; cannot register UID %s" % uid
        _respond_register_error(gcmd, msg)
        return

    spool = spoolman.lookup_spool_by_id(spool_id)
    if not spool:
        msg = "[ERROR] NFC: Spoolman spool %d was not found" % spool_id
        _respond_register_error(gcmd, msg)
        return

    if not spoolman.set_spool_uid(spool_id, uid):
        msg = ("[ERROR] NFC: failed to assign UID %s to Spoolman spool %d"
               % (uid, spool_id))
        _respond_register_error(gcmd, msg)
        return

    spoolman.clear_cache()

    logger.info("NFC_Register: UID %s assigned to Spoolman spool %d",
                uid, spool_id)
    gcmd.respond_info(color_console_tags(
        "[OK] NFC: UID %s assigned to Spoolman spool %d; NFC cache cleared. "
        "Happy Hare/Fluidd will refresh on their normal Spoolman polling cycle."
        % (uid, spool_id)))


def _enabled_lane_gates():
    return sorted(
        (gate for gate in _lane_instances
         if (not getattr(gate, '_shared', False)
             and getattr(gate, '_enabled', True))),
        key=lambda gate: gate._gate)


def _cmd_led_test_all(gcmd):
    if not _flag_param(gcmd, 'ALL'):
        gcmd.respond_info(color_console_tags(
            "NFC_LED_TEST commands:\n"
            "  NFC_LED_TEST ALL=1 - test configured lane tag-read LED effect "
            "on every enabled lane\n"
            "  NFC_LED_TEST ALL=1 DELAY=0.20 CYCLES=2 - set chase delay "
            "and cycles"))
        return
    lanes = _enabled_lane_gates()
    if not lanes:
        gcmd.respond_info(color_console_tags(
            "[WARN] NFC: no enabled per-lane readers configured"))
        return
    try:
        delay = float(_gcmd_get_any(
            gcmd, ('DELAY', 'Delay', 'delay'), 0.20))
    except Exception:
        delay = 0.20
    delay = max(0.0, min(delay, 5.0))
    cycles = _led_test_cycles_from_gcmd(gcmd)

    scheduled = []
    for index, gate in enumerate(lanes):
        _schedule_lane_led_test(gate, index * delay, cycles)
        scheduled.append(str(gate._gate))
    msg = ("[OK] NFC: lane LED chase test scheduled for gates %s "
           "(delay=%.2fs cycles=%d)" % (", ".join(scheduled), delay, cycles))
    logger.info("NFC_LED_TEST ALL=1 — scheduled=%s delay=%.2fs cycles=%d",
                ",".join(scheduled), delay, cycles)
    gcmd.respond_info(color_console_tags(msg))


def _led_test_cycles_from_gcmd(gcmd):
    try:
        value = int(_gcmd_get_any(
            gcmd, ('CYCLES', 'Cycles', 'cycles'),
            LANE_LED_TEST_DEFAULT_CYCLES))
    except Exception:
        value = LANE_LED_TEST_DEFAULT_CYCLES
    return max(1, min(value, LANE_LED_TEST_MAX_CYCLES))


def _schedule_lane_led_test(gate, delay, cycles):
    if delay <= 0:
        gate._lane_led_test(gcmd=None, respond=False, cycles=cycles)
        return

    reactor = gate.reactor

    def _run(eventtime, lane=gate):
        lane._lane_led_test(gcmd=None, respond=False, cycles=cycles)
        return lane.reactor.NEVER

    eventtime = reactor.monotonic() + delay
    try:
        reactor.register_timer(_run, eventtime)
    except TypeError:
        timer = reactor.register_timer(_run)
        reactor.update_timer(timer, eventtime)


# ─────────────────────────────────────────────────────────────────────────────
# NFCGateDefaults / NFCGate — per-lane/shared NFC Reader path
# ─────────────────────────────────────────────────────────────────────────────
#
# One NFCGate instance per [nfc_gate laneN] config section.
# Each manages one configured NFC Reader.
#
# NFCGateDefaults holds shared values from the optional base [nfc_gate]
# section.  Lane sections inherit these and can override any key locally.

# Module-level registry for NFC_STATUS across all configured lanes.
_lane_instances = []

# Single shared reader instance, set by NFCGate._handle_connect when shared=true.
_shared_instance = None
_shared_configured = False

# Internal gate number for the shared reader.  Not exposed to users — the shared
# reader has no Happy Hare gate assignment and does not use this value for Happy Hare
# orchestration.  It serves only as a unique key for reader drivers / GateState
# logging and (internally) as a guard against accidentally seeding from Happy Hare.
_SHARED_GATE_SENTINEL            = 255
_SHARED_MISSED_RESOLUTION_LIMIT  = 3
_SHARED_READ_EFFECT_DURATION        = 4.0
_SHARED_BYPASS_READ_EFFECT_DURATION = 4.0
_SHARED_BYPASS_READY_EFFECT_DURATION = 2.0
_SHARED_UNRESOLVED_EFFECT_DURATION  = 2.0
_diagnostic_warnings = []


def _add_diagnostic_warning(message):
    if message not in _diagnostic_warnings:
        _diagnostic_warnings.append(message)
        logger.warning("nfc_gate: %s", message)


def nfc_gate_for_gate_number(gate_number):
    for candidate in _lane_instances:
        if (candidate._gate == gate_number
                and getattr(candidate, '_enabled', True)):
            return candidate
    return None


def _status_html_words(text):
    text = re.sub(r'\bavailable\b',
                  '<span style="color:#90EE90">available</span>', text)
    text = re.sub(r'\bempty\b',
                  '<span style="color:#87CEEB">empty</span>', text)
    text = re.sub(r'\bassigned\b',
                  '<span style="color:#FFFF00">assigned</span>', text)
    return text


def _reader_label(reader_type):
    return "NFC Reader (%s)" % (reader_type,)


def _reader_wiring_hint(reader_type):
    if reader_type == 'rc522':
        return "check SPI wiring, cs_pin, spi_bus/software SPI pins, power, and ground"
    return "check wiring and I2C address"


def _lookup_objects_safe(printer, name):
    try:
        return list(printer.lookup_objects(name))
    except Exception:
        return []


def _optional_float_config(config, key, default=None, minval=None, maxval=None):
    raw = config.get(key, None)
    if raw is None:
        return default
    try:
        value = float(raw)
    except Exception:
        raise config.error("Option '%s' must be a number" % key)
    if minval is not None and value < minval:
        raise config.error("Option '%s' must be at least %.3f" % (key, minval))
    if maxval is not None and value > maxval:
        raise config.error("Option '%s' must be at most %.3f" % (key, maxval))
    return value


def _raw_klipper_config(printer):
    try:
        configfile = printer.lookup_object('configfile', None)
        if configfile is None:
            return {}
        return configfile.get_status(0).get('config', {}) or {}
    except Exception:
        return {}


def _nfc_endstop_section(gate):
    return "mmu_nfc_endstop lane%d" % int(gate._gate)


def _nfc_endstop_name(gate):
    return "nfc_lane%d" % int(gate._gate)


def _has_nfc_endstop(printer, gate, raw_config=None):
    section = _nfc_endstop_section(gate)
    if raw_config is None:
        raw_config = _raw_klipper_config(printer)
    if section in raw_config:
        return True
    return printer.lookup_object(section, None) is not None


def _nfc_endstop_block(gate):
    lane = int(gate._gate)
    return (
        "[mmu_nfc_endstop lane{lane}]\n"
        "nfc_gate:                lane{lane}\n"
        "endstop_name:            nfc_lane{lane}\n"
        "poll_interval:           0.05\n"
        "register_sensor:         True\n"
    ).format(lane=lane)


def _default_nfc_reader_hw_path():
    return os.path.expanduser('~/printer_data/config/nfc/nfc_reader_hw.cfg')


def _insert_nfc_endstop_after_lane(text, gate):
    lane = int(gate._gate)
    lane_header_re = re.compile(r'^\[nfc_gate lane%d\]\s*$' % lane)
    any_section_re = re.compile(r'^\[[^\]]+\]\s*(?:[#;].*)?$')
    if re.search(r'^\[mmu_nfc_endstop lane%d\]\s*$' % lane, text, re.M):
        return text, False, "present"

    lines = text.splitlines(keepends=True)
    lane_start = None
    for idx, line in enumerate(lines):
        if lane_header_re.match(line.strip()):
            lane_start = idx
            break

    block = "\n" + _nfc_endstop_block(gate)
    if lane_start is None:
        if text and not text.endswith('\n'):
            text += '\n'
        return text + block, True, "appended"

    insert_at = len(lines)
    for idx in range(lane_start + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped == '' or any_section_re.match(stripped):
            insert_at = idx
            break
    lines[insert_at:insert_at] = [block]
    return ''.join(lines), True, "inserted"


def _nfc_endstop_status_text(printer, gate):
    return ("Loaded" if printer.lookup_object(_nfc_endstop_section(gate), None) is not None
            else "Unloaded")


def _find_gate_by_number(gate_number):
    for gate in _lane_instances:
        if (not getattr(gate, '_shared', False)
                and int(getattr(gate, '_gate', -1)) == int(gate_number)):
            return gate
    return None


def _cmd_doctor_endstop_add(printer, gcmd):
    gate_number = gcmd.get_int('GATE', None, minval=0)
    if gate_number is None:
        raise gcmd.error("NFC_DOCTOR ENDSTOP=ADD requires GATE=<#>")
    gate = _find_gate_by_number(gate_number)
    if gate is None:
        raise gcmd.error("No [nfc_gate lane%d] reader is configured" %
                         gate_number)

    raw_config = _raw_klipper_config(printer)
    if _has_nfc_endstop(printer, gate, raw_config):
        gcmd.respond_info(color_console_tags(
            "[OK] NFC_DOCTOR: [%s] already exists for ENDSTOP=%s"
            % (_nfc_endstop_section(gate), _nfc_endstop_name(gate))))
        return

    path = _default_nfc_reader_hw_path()
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                text = f.read()
        else:
            text = ""
        new_text, changed, mode = _insert_nfc_endstop_after_lane(text, gate)
        if not changed:
            gcmd.respond_info(color_console_tags(
                "[OK] NFC_DOCTOR: [%s] already exists in %s. "
                "Restart Klipper to load it."
                % (_nfc_endstop_section(gate), path)))
            return
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, 'w') as f:
            f.write(new_text)
    except Exception as e:
        logger.exception("NFC_DOCTOR ENDSTOP=ADD failed")
        raise gcmd.error(
            "Failed to add [%s] to %s: %s"
            % (_nfc_endstop_section(gate), path, e))

    gcmd.respond_info(color_console_tags(
        "[OK] NFC_DOCTOR: %s [%s] in %s for ENDSTOP=%s. "
        "Restart Klipper to load the new virtual endstop."
        % ("inserted" if mode == "inserted" else "appended",
           _nfc_endstop_section(gate), path, _nfc_endstop_name(gate))))


def _handle_doctor_action(printer, gcmd):
    endstop_action = str(gcmd.get('ENDSTOP', '') or '').strip().lower()
    if endstop_action:
        if endstop_action == 'add':
            _cmd_doctor_endstop_add(printer, gcmd)
            return True
        raise gcmd.error("Unknown NFC_DOCTOR ENDSTOP action '%s'" %
                         endstop_action)
    return False


def _detect_happy_hare_version(printer):
    """Return the Happy Hare software version string, or None if unavailable."""
    try:
        mmu = printer.lookup_object('mmu', None)
        if mmu is None:
            return None
        version = getattr(mmu, 'version', None)
        if version is None:
            mmu_machine = getattr(mmu, 'mmu_machine', None)
            version = getattr(mmu_machine, 'happy_hare_version', None)
        return version
    except Exception:
        return None


def _happy_hare_major_from_version(version):
    if version is None:
        return None
    m = re.match(r'\s*v?(\d+)', str(version), re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _shared_preload_hook_message(hook, name='shared'):
    hook = str(hook or '').strip()
    if '_NFC_SHARED_PRELOAD' in hook:
        return None
    if 'JOG_SCAN' in hook or 'NFC JOG_SCAN' in hook:
        return ("[%s]: shared reader is enabled but Happy Hare "
                "user_post_preload_extension is wired to per-lane "
                "NFC JOG_SCAN; set "
                "_MMU_SEQUENCE_VARS.variable_user_post_preload_extension "
                "to '_NFC_SHARED_PRELOAD'" % name)
    if hook:
        return ("[%s]: shared reader is enabled but Happy Hare "
                "user_post_preload_extension is '%s'; set "
                "_MMU_SEQUENCE_VARS.variable_user_post_preload_extension "
                "to '_NFC_SHARED_PRELOAD'" % (name, hook))
    return ("[%s]: shared reader is enabled but "
            "_MMU_SEQUENCE_VARS.variable_user_post_preload_extension "
            "does not contain _NFC_SHARED_PRELOAD" % name)


def _lane_status_lines(printer):
    """Build NFC_STATUS output lines cross-referenced against the MMU
    lane MCUs registered in Klipper (mirrors how Happy Hare reads [board_pins lane]).

    For each lane MCU (e.g. lane0…lane4):
      - If an NFCGate is configured for that MCU → show its spool/UID state.
      - If no NFCGate is configured         → note that no reader is set up.
    Falls back to listing _lane_instances directly when no lane MCUs are found.
    """
    # Collect MCU names that match "lane<N>" from Klipper's object registry.
    lane_names = []
    for obj_name, _ in _lookup_objects_safe(printer, 'mcu'):
        parts = obj_name.split(None, 1)
        if len(parts) == 2 and re.match(r'^lane\d+$', parts[1]):
            lane_names.append(parts[1])
    lane_names.sort(key=lambda n: int(n[4:]))

    nfc_by_lane = {gate._name: gate for gate in _lane_instances
                   if not getattr(gate, '_shared', False)}

    if not lane_names:
        # No MMU lane MCUs visible — fall back to plain list.
        if not nfc_by_lane:
            return ["No [nfc_gate] sections are configured."]
        lines = ["NFC gate status  (%d gate%s configured):"
                 % (len(nfc_by_lane), 's' if len(nfc_by_lane) != 1 else '')]
        for gate in sorted(_lane_instances, key=lambda g: g._gate):
            if not getattr(gate, '_shared', False):
                lines.append(gate.status_line())
        _append_shared_status(lines)
        return lines

    lines = ["NFC gate status — %d MMU lane(s), %d NFC reader(s) configured:"
             % (len(lane_names), len(nfc_by_lane))]
    for lane in lane_names:
        if lane in nfc_by_lane:
            lines.append(nfc_by_lane[lane].status_line())
        else:
            lines.append("  %-8s  no NFC reader configured" % (lane + ':'))
    _append_shared_status(lines)
    return lines


def _append_shared_status(lines):
    if (_shared_instance is not None
            and getattr(_shared_instance, '_enabled', True)):
        lines.append(_shared_instance.shared_status_line())


def _doctor_lines(printer):
    lines = ["NFC doctor:"]
    raw_config = _raw_klipper_config(printer)
    lane_readers = [gate for gate in _lane_instances
                    if not getattr(gate, '_shared', False)]
    enabled_lanes = [gate for gate in lane_readers
                     if getattr(gate, '_enabled', True)]
    disabled_lanes = [gate for gate in lane_readers
                      if not getattr(gate, '_enabled', True)]
    shared_readers = [gate for gate in _lane_instances
                      if getattr(gate, '_shared', False)]
    enabled_shared = [gate for gate in shared_readers
                      if getattr(gate, '_enabled', True)]

    def mark(ok):
        return "[OK]" if ok else "[WARN]"

    lines.append("  %s lane readers: %d enabled, %d disabled" %
                 (mark(bool(enabled_lanes) or bool(enabled_shared)),
                  len(enabled_lanes), len(disabled_lanes)))
    for gate in sorted(lane_readers, key=lambda g: g._gate):
        if not getattr(gate, '_enabled', True):
            lines.append("    Gate %d [%s/%s]: disabled by config" %
                         (gate._gate, gate._name, gate._reader_type))
        else:
            state = "failed" if gate._failed else "ready/pending init"
            lines.append("    Gate %d [%s/%s]: enabled, %s, virtual_endstop: %s" %
                         (gate._gate, gate._name, gate._reader_type, state,
                          _nfc_endstop_status_text(printer, gate)))

    missing_endstops = []
    for gate in sorted(enabled_lanes, key=lambda g: g._gate):
        if not _has_nfc_endstop(printer, gate, raw_config):
            missing_endstops.append(gate)
    if missing_endstops:
        lines.append("  [ERROR] NFC virtual endstops: %d missing" %
                     len(missing_endstops))
        for gate in missing_endstops:
            lines.append(
                "    [ERROR] Gate %d [%s]: missing [%s] for ENDSTOP=%s; "
                "run NFC_DOCTOR GATE=%d ENDSTOP=ADD"
                % (gate._gate, gate._name, _nfc_endstop_section(gate),
                   _nfc_endstop_name(gate), gate._gate))
    elif enabled_lanes:
        lines.append("  [OK] NFC virtual endstops: configured for every enabled lane reader")
    else:
        lines.append("  [OK] NFC virtual endstops: no enabled lane readers")

    if enabled_shared:
        shared = enabled_shared[0]
        lines.append("  %s shared reader: enabled [%s/%s]" %
                     (mark(not shared._failed), shared._name,
                      shared._reader_type))
    elif shared_readers:
        lines.append("  [OK] shared reader: configured but disabled")
    else:
        lines.append("  [OK] shared reader: not configured")

    defaults = printer.lookup_object('nfc_gate', None)
    spoolman = getattr(defaults, '_spoolman', None)
    if spoolman is not None:
        lines.append("  [OK] Spoolman: enabled")
    else:
        url = getattr(defaults, 'spoolman_url', '')
        lines.append("  [WARN] Spoolman: disabled or unavailable"
                     if not url else "  [OK] Spoolman: disabled by config")

    if enabled_shared:
        macro = raw_config.get('gcode_macro _MMU_SEQUENCE_VARS', {})
        hook = str(macro.get('variable_user_post_preload_extension', ''))
        warning = _shared_preload_hook_message(hook)
        if warning is None:
            lines.append("  [OK] shared preload hook: _NFC_SHARED_PRELOAD")
        else:
            lines.append("  [WARN] shared preload hook: %s" % warning)

    warnings = list(_diagnostic_warnings)
    for gate in _lane_instances:
        warnings.extend(getattr(gate, '_diagnostic_warnings', []))
    if warnings:
        lines.append("  Warnings:")
        for warning in sorted(set(warnings)):
            lines.append("    [WARN] %s" % warning)
    else:
        lines.append("  [OK] no static config warnings")
    return lines


def _nfc_help(gcmd=None):
    advanced = bool(gcmd.get_int('ADVANCED', 0, minval=0, maxval=1)
                    if gcmd is not None else False)
    callbacks = bool(gcmd.get_int('CALLBACKS', 0, minval=0, maxval=1)
                     if gcmd is not None else False)
    low_level = bool(gcmd.get_int('LOW_LEVEL', 0, minval=0, maxval=1)
                     if gcmd is not None else False)
    lane_gates = sorted(gate._gate for gate in _lane_instances
                        if (not getattr(gate, '_shared', False)
                            and getattr(gate, '_enabled', True)))
    has_shared = _shared_configured or _shared_instance is not None or any(
        getattr(gate, '_shared', False) and getattr(gate, '_enabled', True)
        for gate in _lane_instances)

    lines = [
        "NFC Reader commands: (use NFC_HELP ADVANCED=1 CALLBACKS=1 "
        "LOW_LEVEL=1 for full command set)",
        "NFC_HELP : Display the complete set of NFC commands and functions",
        "NFC_STATUS : Show every configured NFC reader",
        "NFC_DOCTOR : Check NFC config, readers, virtual endstops, Spoolman, and Happy Hare hooks",
        "NFC_DOCTOR GATE=<#> ENDSTOP=ADD : Add missing NFC virtual endstop config for one lane",
        "NFC_REGISTER UID=TAG_UID SPOOL_ID=SPOOL_ID : Assign a UID to an existing Spoolman spool",
        "NFC_LED_TEST ALL=1 CYCLES=2 : Test configured lane tag-read LED effect on every enabled lane",
        "NFC GATE=<#> HELP=1 : Show commands for one per-lane reader",
        "NFC GATE=<#> STATUS : Show one per-lane reader state",
        "NFC GATE=<#> INIT=1 : Re-run reader hardware init",
        "NFC GATE=<#> SCAN=1 : Scan hardware once, no Spoolman/Happy Hare dispatch",
        "NFC GATE=<#> JOG_SCAN=1 : Start scan-jog to find tag on a loaded spool",
        "NFC GATE=<#> LED_TEST=1 CYCLES=2 : Test configured lane tag-read LED effect",
        "NFC GATE=<#> POLL=1 : Run one full read/resolve cycle",
        "NFC GATE=<#> APPLY=1 : Send cached spool to Happy Hare now",
        "NFC GATE=<#> CLEAR_CACHE=1 : Clear cached spool/UID, no Happy Hare dispatch",
        "NFC GATE=<#> HH_SYNC=1 SPOOL_ID=<n> : Seed lane cache from Happy Hare gate map",
        "NFC GATE=<#> READ=1 : Start timer polling",
        "NFC GATE=<#> READ=0 : Stop timer polling",
    ]
    if lane_gates:
        lines.append("Configured lane gates : %s" %
                     ", ".join(str(gate) for gate in lane_gates))
    else:
        lines.append("Configured lane gates : none")

    if has_shared:
        lines.extend([
            "",
            "Shared reader commands:",
            "NFC_SHARED HELP=1 : Show shared reader commands",
            "NFC_SHARED STATUS=1 : Show detailed shared reader state",
            "NFC_SHARED SUMMARY=1 : Show one-line shared reader state",
            "NFC_SHARED READ=1 : Start shared polling",
            "NFC_SHARED READ=0 : Stop shared polling",
            "NFC_SHARED CANCEL=1 : Cancel a staged shared spool",
            "NFC_SHARED REPLACE=1 : Discard a staged spool and scan another",
            "NFC_SHARED RESET=1 : Clear shared state, restore LEDs, and poll",
            "NFC_SHARED LED_TEST=1 : Test configured shared tag-read LED effect",
        ])
        if advanced:
            lines.extend([
                "",
                "Advanced shared-reader commands:",
                "NFC_SHARED CLEAR=1 : Clear pending state and stop polling",
                "NFC_SHARED PRELOAD_CHECK=1 : Happy Hare hook command; approve NEXT_SPOOLID if valid",
                "NFC_SHARED PRELOAD_COMMIT=1 SPOOL_ID=<n> : Happy Hare hook command; clear pending after NEXT_SPOOLID",
                "NFC_SHARED PRELOAD_CLEAR_ASSIGNED=1 SPOOL_ID=<n> GATE=<n> : Happy Hare hook command; clear already-assigned shared spool",
                "NFC_SHARED POLL=1 : Run one full read/resolve cycle",
                "NFC_SHARED SCAN=1 : Raw hardware scan only",
                "NFC_SHARED INIT=1 : Re-run NFC Reader init",
                "NFC_SHARED CLEAR_CACHE=1 : Clear tag cache, keeping pending spool",
            ])

    if callbacks:
        lines.extend([
            "",
            "Callbacks and macros:",
            "_NFC_SPOOL_CHANGED : Per-lane spool assignment callback",
            "_NFC_TAG_NO_SPOOL : Per-lane UID-only callback",
            "_NFC_SPOOL_REMOVED : Per-lane spool removal callback",
            "_NFC_HH_SYNC_ONE : Re-seed one lane cache from Happy Hare",
            "NFC_HH_SYNC_CACHE : Re-seed all lane caches from Happy Hare",
            "_NFC_SHARED_PRELOAD : Happy Hare pre-load hook for shared reader",
        ])

    if low_level:
        lines.extend([
            "",
            "Low-level debug commands:",
            "PN532 I2C/frame debug:",
            "NFC GATE=<#> STEP=HELP : Show PN532 low-level debug help",
            "NFC GATE=<#> STEP=WAKEUP : Write wake byte to PN532",
            "NFC GATE=<#> STEP=READY : Read PN532 ready status byte",
            "NFC GATE=<#> STEP=FIRMWARE_WRITE : Send GetFirmwareVersion frame",
            "NFC GATE=<#> STEP=FIRMWARE_RESPONSE : Read firmware response",
            "NFC GATE=<#> STEP=SAM_WRITE : Send SAMConfiguration frame",
            "NFC GATE=<#> STEP=SAM_RESPONSE : Read SAMConfiguration response",
            "RC522 SPI/register debug:",
            "NFC_SHARED RC522_DUMP_REGS=1 : Read key RC522 registers",
            "NFC_SHARED RC522_REGISTER=TxControlReg : Read one RC522 register",
            "NFC_SHARED RC522_REGISTER=TxControlReg VALUE=83 : Write one RC522 register",
            "NFC_SHARED RC522_ANTENNA_ENABLE=1 : Enable RC522 antenna TX bits",
            "NFC_SHARED RC522_TAG_WAKE=1 : Run a 7-bit REQA tag-wake probe",
            "NFC_SHARED RC522_FIFO_TRANSCEIVE='93 20' BIT_FRAMING=0 : Raw FIFO transceive",
        ])
    return lines


class NFCGateDefaults:
    def __init__(self, config):
        self.reader_type        = reader_factory.reader_type_from_config(config)
        self.spoolman_url       = config.get('spoolman_url', '')
        self.moonraker_url      = config.get('moonraker_url',
                                             'http://127.0.0.1:7125')
        self.spoolman_rfid_key  = config.get('spoolman_rfid_key', 'rfid_tag')
        self.spoolman_timeout   = config.getfloat('spoolman_timeout', 5.0,
                                                   minval=0.5, maxval=30.0)
        self.spoolman_cache_ttl = config.getfloat('spoolman_cache_ttl', 300.0,
                                                   minval=0., maxval=3600.)
        self.poll_interval      = config.getfloat('poll_interval', 10.,
                                                   minval=1., maxval=3600.)
        self.startup_polling    = config.getint('startup_polling', -1,
                                                 minval=-1, maxval=1)
        self.startup_poll_delay = config.getfloat('startup_poll_delay', 0.,
                                                   minval=0., maxval=3600.)
        self.absent_threshold   = config.getint('absent_threshold', 3,
                                                 minval=1, maxval=255)
        self.transceive_delay   = config.getfloat('transceive_delay', 0.250,
                                                   minval=0.050, maxval=2.0)
        self.crc_delay          = config.getfloat('crc_delay', 0.050,
                                                   minval=0.005, maxval=1.0)
        self.debug              = config.getint('debug', 2, minval=0, maxval=4)
        self.console_output, self.console_log_level = _get_console_config(config)
        self.low_level_debug    = pn532_driver.get_low_level_debug(config)
        self.i2c_address        = config.getint('i2c_address', 0x24,
                                                 minval=0, maxval=127)
        self.i2c_bus            = config.get('i2c_bus', None)
        self.scan_jog_mm        = config.getfloat('scan_jog_mm', 50.0,
                                                   minval=1.0, maxval=500.0)
        self.scan_jog_max       = _optional_float_config(
            config, 'scan_jog_max', None, minval=1.0, maxval=5000.0)
        self.scan_rewind_buffer_mm = config.getfloat(
            'scan_rewind_buffer_mm', 30.0,
            minval=0.0, maxval=500.0)
        self.scan_decode_retry_mm = config.getfloat(
            'scan_decode_retry_mm', 5.0,
            minval=0.0, maxval=50.0)
        self.scan_decode_retry_rounds = config.getint(
            'scan_decode_retry_rounds', 5,
            minval=0, maxval=10)
        self.scan_reads_per_position = config.getint(
            'scan_reads_per_position', 3,
            minval=1, maxval=20)
        self.scan_poll_interval = config.getfloat('scan_poll_interval', 0.1,
                                                   minval=0.1, maxval=5.0)
        self.scan_motion_mode = _get_scan_motion_mode(config, 'continuous')
        self.scan_continuous_step_mm = config.getfloat(
            'scan_continuous_step_mm', 150.0,
            minval=1.0, maxval=500.0)
        self.scan_continuous_speed = config.getfloat(
            'scan_continuous_speed', 150.0,
            minval=1.0, maxval=500.0)
        self.scan_continuous_accel = config.getfloat(
            'scan_continuous_accel', 2000.0,
            minval=1.0, maxval=10000.0)
        self.scan_continuous_poll_interval = config.getfloat(
            'scan_continuous_poll_interval', 0.05,
            minval=0.01, maxval=5.0)
        self.scan_enabled         = config.getboolean('scan_enabled', True)
        self.tag_parsing          = config.getboolean('tag_parsing', False)
        self.tag_max_pages        = config.getint('tag_max_pages', 16,
                                                   minval=4, maxval=135)
        self.bambu_reads          = config.getboolean('bambu_reads', False)
        self.spoolman_auto_create     = config.getboolean('spoolman_auto_create', False)
        self.scan_searching_effect    = config.get('scan_searching_effect',   'mmu_clockwise_slow')
        self.scan_tag_read_effect     = config.get('scan_tag_read_effect',    'mmu_RFID_read')
        self.scan_rewind_effect       = config.get('scan_rewind_effect',      'mmu_anticlock_fast')
        self.lane_auto_create_effect  = config.get('lane_auto_create_effect', 'mmu_RFID_creating')
        self.lane_unresolved_effect   = config.get('lane_unresolved_effect',  'mmu_RFID_unresolved')
        if self.bambu_reads and not self.tag_parsing:
            _add_diagnostic_warning(
                "bambu_reads=True has no effect while tag_parsing=False")
        if self.spoolman_auto_create and not self.tag_parsing:
            _add_diagnostic_warning(
                "spoolman_auto_create=True has no effect while tag_parsing=False")
        if (self.spoolman_auto_create
                and not _spoolman_url_enabled(self.spoolman_url)):
            _add_diagnostic_warning(
                "spoolman_auto_create=True requires Spoolman to be enabled")

        self._printer = config.get_printer()
        gcode         = self._printer.lookup_object('gcode')
        gcode.register_command(
            'NFC_STATUS', self.cmd_NFC_STATUS,
            desc="Report spool state for all configured NFC gates")
        gcode.register_command(
            'NFC_HELP', self.cmd_NFC_HELP,
            desc="Show NFC reader command help")
        gcode.register_command(
            'NFC_DOCTOR', self.cmd_NFC_DOCTOR,
            desc="Check NFC reader setup and common configuration problems")
        gcode.register_command(
            'NFC_REGISTER', self.cmd_NFC_REGISTER,
            desc="Assign an NFC UID to a Spoolman spool")
        gcode.register_command(
            'NFC_LED_TEST', self.cmd_NFC_LED_TEST,
            desc="Test NFC lane LED effects")

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

        if _spoolman_url_enabled(self.spoolman_url):
            self._spoolman = SpoolmanClient(
                self.spoolman_url,
                rfid_key=self.spoolman_rfid_key,
                timeout=self.spoolman_timeout,
                cache_ttl=self.spoolman_cache_ttl,
                debug=self.debug,
                moonraker_url=self.moonraker_url)
            logger.info("nfc_gate: Spoolman enabled — url=%s rfid_key=%s",
                        self.spoolman_url, self.spoolman_rfid_key)
        else:
            self._spoolman = None
            if self.spoolman_url:
                logger.info("nfc_gate: Spoolman disabled by config")
            else:
                logger.warning(
                    "nfc_gate: spoolman_url not set — set spoolman_url in "
                    "[nfc_gate]. Use 'auto' to read Moonraker.")

    def cmd_NFC_STATUS(self, gcmd):
        gcmd.respond_info('\n'.join(_lane_status_lines(self._printer)))

    def cmd_NFC_HELP(self, gcmd):
        gcmd.respond_info('\n'.join(_nfc_help(gcmd)))

    def cmd_NFC_DOCTOR(self, gcmd):
        if _handle_doctor_action(self._printer, gcmd):
            return
        gcmd.respond_info(color_console_tags(
            '\n'.join(_doctor_lines(self._printer))))

    def cmd_NFC_REGISTER(self, gcmd):
        gcode = self._printer.lookup_object('gcode')
        _cmd_register_uid_to_spool(gcode, self._spoolman, gcmd)

    def cmd_NFC_LED_TEST(self, gcmd):
        _cmd_led_test_all(gcmd)


class NFCGate:
    _active_scan_gate = None  # class-level scan lock; shared across all instances

    def __init__(self, config, defaults=None):
        self._HAPPY_HARE_VERSION = None
        self.printer  = config.get_printer()
        self.reactor  = self.printer.get_reactor()
        self._name    = config.get_name().split()[-1]
        self._refresh_happy_hare_version()

        d = defaults
        self._defaults = defaults

        # Read shared first — it controls how subsequent params are parsed.
        self._shared = config.getboolean('shared', False)
        self._enabled = config.getboolean('enabled', True)
        self._reader_type = reader_factory.reader_type_from_config(
            config, d.reader_type if d else 'pn532')
        if self._shared and self._enabled:
            global _shared_configured
            _shared_configured = True
        _i2c_mcu_name = config.get('i2c_mcu', 'mcu')
        _m = re.search(r'(\d+)$', _i2c_mcu_name)
        self._shared_mcu_index = int(_m.group(1)) if _m else None
        if self._shared and self._enabled:
            for existing in _lane_instances:
                if (getattr(existing, '_shared', False)
                        and getattr(existing, '_enabled', True)):
                    raise config.error(
                        "nfc_gate [%s]: only one shared reader may be configured"
                        % self._name)

        # Gate number: required for lane readers; internal sentinel for shared.
        # The shared reader has no Happy Hare gate assignment — the sentinel is never
        # passed to Happy Hare and is not user-configurable.
        if self._shared:
            self._gate = _SHARED_GATE_SENTINEL
        else:
            self._gate = config.getint('mmu_gate', minval=0)
        self._diagnostic_warnings = []
        if not self._enabled:
            self._poll_interval = 0.0
            self._startup_polling = 0
            self._startup_poll_delay = 0.0
            self._absent_threshold = 3
            self._debug = d.debug if d else 2
            self._low_level_debug = False
            self._reader_type = reader_factory.reader_type_from_config(
                config, d.reader_type if d else 'pn532')
            self._console_output = d.console_output if d else False
            self._console_log_level = d.console_log_level if d else 'warning'
            self._spoolman = d._spoolman if d is not None else None
            self._reader = None
            self._state = GateState(self._gate, self._absent_threshold)
            self._failed = False
            self._polling = False
            self._scan_enabled = False
            self._scan_motion_mode = d.scan_motion_mode if d else 'continuous'
            self._scan_continuous_step_mm = d.scan_continuous_step_mm if d else 150.0
            self._scan_continuous_speed = d.scan_continuous_speed if d else 150.0
            self._scan_continuous_accel = d.scan_continuous_accel if d else 2000.0
            self._scan_continuous_poll_interval = d.scan_continuous_poll_interval if d else 0.05
            self._tag_parsing = False
            self._bambu_reads = False
            self._spoolman_auto_create = False
            self._scan_searching_effect   = d.scan_searching_effect   if d else 'mmu_clockwise_slow'
            self._scan_tag_read_effect    = d.scan_tag_read_effect    if d else 'mmu_RFID_read'
            self._scan_rewind_effect      = d.scan_rewind_effect      if d else 'mmu_anticlock_fast'
            self._lane_auto_create_effect = d.lane_auto_create_effect if d else 'mmu_RFID_creating'
            self._lane_unresolved_effect  = d.lane_unresolved_effect  if d else 'mmu_RFID_unresolved'
            self._hh_load_paused = False
            self._shared_pending_spool = None
            self._shared_pending_uid = None
            self._shared_last_error = None
            self._shared_read_deadline = 0.0
            self._has_per_lane_readers = False
            self._gcode = None
            self._commands_registered = True
            self._status_registered = False
            self._help_registered = False
            self._shared_cmd_registered = False
            logger.info("[%s]: NFC reader disabled by config", self._name)
            return
        self._poll_interval    = config.getfloat('poll_interval',
                                                  d.poll_interval if d else 10.,
                                                  minval=1., maxval=3600.)
        self._startup_polling  = config.getint('startup_polling',
                                                d.startup_polling if d else -1,
                                                minval=-1, maxval=1)
        self._startup_poll_delay = config.getfloat(
            'startup_poll_delay',
            d.startup_poll_delay if d else 0.,
            minval=0., maxval=3600.)
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
                                               d.debug if d else 2,
                                               minval=0, maxval=4)
        self._low_level_debug  = pn532_driver.get_low_level_debug(
            config, d.low_level_debug if d else False)
        console_output, console_log_level = _get_console_config(
            config,
            d.console_output if d else False,
            d.console_log_level if d else 'warning')
        self._console_output = console_output
        self._console_log_level = console_log_level
        if d is None:
            log_file = config.get('log_file', '')
            configure(log_file, printer=self.printer,
                      console_output=console_output,
                      console_log_level=console_log_level)

        if d is not None:
            # Share the single SpoolmanClient created by NFCGateDefaults.
            self._spoolman = d._spoolman
        else:
            # No base [nfc_gate] section — create a per-lane client as fallback.
            spoolman_url      = config.get('spoolman_url', '')
            moonraker_url     = config.get('moonraker_url', 'http://127.0.0.1:7125')
            spoolman_rfid_key = config.get('spoolman_rfid_key', 'rfid_tag')
            spoolman_timeout  = config.getfloat('spoolman_timeout', 5.0,
                                                 minval=0.5, maxval=30.0)
            spoolman_cache_ttl = config.getfloat('spoolman_cache_ttl', 300.0,
                                                  minval=0., maxval=3600.)
            if _spoolman_url_enabled(spoolman_url):
                self._spoolman = SpoolmanClient(
                    spoolman_url,
                    rfid_key=spoolman_rfid_key,
                    timeout=spoolman_timeout,
                    cache_ttl=spoolman_cache_ttl,
                    debug=self._debug,
                    moonraker_url=moonraker_url)
                logger.info("[%s]: Spoolman enabled — url=%s rfid_key=%s",
                            self._name, spoolman_url, spoolman_rfid_key)
            else:
                self._spoolman = None
                if spoolman_url:
                    logger.info("[%s]: Spoolman disabled by config",
                                self._name)
                else:
                    logger.warning(
                        "[%s]: spoolman_url not set — set spoolman_url in "
                        "[nfc_gate] or [nfc_gate %s]. Use 'auto' to read Moonraker.",
                        self._name, self._name)

        self._reader     = reader_factory.create_reader(
            config, d, self._reader_type, self._gate, self._debug,
            low_level_debug=self._low_level_debug,
            sleep_fn=self._reactor_sleep,
            transceive_delay=transceive_delay,
            crc_delay=crc_delay)
        self._state      = GateState(self._gate, self._absent_threshold)
        self._suppress_next_dispatch_uid   = None
        self._suppress_next_dispatch_spool = None  # paired with uid — suppress only when both match
        self._hh_seed_spool_id   = None  # set on startup from Happy Hare gate map; cleared after first match
        self._hh_seed_available  = False  # True only when Happy Hare had the gate marked available at seed time
        self._hh_confirmed_spool = None  # last spool Happy Hare acknowledged; enables _check_hh_cleared
        self._hh_load_paused     = False  # True while Happy Hare owns this gate assignment
        self._failed     = False
        self._klipper    = KlipperInterface(
            self.printer, self.reactor, self._debug, name=self._name,
            spoolman_enabled=self._spoolman is not None)
        self._polling    = False
        self._poll_timer    = self.reactor.register_timer(self._poll_timer_event)
        self._startup_check_timer = self.reactor.register_timer(
            self._startup_check_unknown_gate_event)
        self._warning_timer = self.reactor.register_timer(
            self._warning_timer_event)
        self._shared_led_failsafe_timer = self.reactor.register_timer(
            self._shared_led_failsafe_event)

        self._scan_jog_mm   = config.getfloat('scan_jog_mm',
                                               d.scan_jog_mm if d else 50.0,
                                               minval=1.0, maxval=500.0)
        self._scan_jog_max = _optional_float_config(
            config, 'scan_jog_max',
            d.scan_jog_max if d else None,
            minval=1.0, maxval=5000.0)
        self._scan_rewind_buffer_mm = config.getfloat(
            'scan_rewind_buffer_mm',
            d.scan_rewind_buffer_mm if d else 30.0,
            minval=0.0, maxval=500.0)
        self._scan_decode_retry_mm = config.getfloat(
            'scan_decode_retry_mm',
            d.scan_decode_retry_mm if d else 5.0,
            minval=0.0, maxval=50.0)
        self._scan_decode_retry_rounds = config.getint(
            'scan_decode_retry_rounds',
            d.scan_decode_retry_rounds if d else 5,
            minval=0, maxval=10)
        self._scan_reads_per_position = config.getint(
            'scan_reads_per_position',
            d.scan_reads_per_position if d else 3,
            minval=1, maxval=20)
        self._scan_max_mm   = None
        self._mmu_vars_path = None
        self._bowden_lengths = None
        self._scan_poll_interval = config.getfloat('scan_poll_interval',
                                                    d.scan_poll_interval if d else 0.1,
                                                    minval=0.1, maxval=5.0)
        self._scan_motion_mode = _get_scan_motion_mode(
            config, d.scan_motion_mode if d else 'continuous')
        self._scan_continuous_step_mm = config.getfloat(
            'scan_continuous_step_mm',
            d.scan_continuous_step_mm if d else 150.0,
            minval=1.0, maxval=500.0)
        self._scan_continuous_speed = config.getfloat(
            'scan_continuous_speed',
            d.scan_continuous_speed if d else 150.0,
            minval=1.0, maxval=500.0)
        self._scan_continuous_accel = config.getfloat(
            'scan_continuous_accel',
            d.scan_continuous_accel if d else 2000.0,
            minval=1.0, maxval=10000.0)
        self._scan_continuous_poll_interval = config.getfloat(
            'scan_continuous_poll_interval',
            d.scan_continuous_poll_interval if d else 0.05,
            minval=0.01, maxval=5.0)
        # scan_enabled: forced false for shared (no physical EMU lane for jog).
        if self._shared:
            self._scan_enabled = False
        else:
            self._scan_enabled = config.getboolean('scan_enabled',
                                                    d.scan_enabled if d else True)
        self._tag_parsing          = config.getboolean('tag_parsing',
                                                        d.tag_parsing if d else False)
        self._tag_max_pages        = config.getint('tag_max_pages',
                                                    d.tag_max_pages if d else 16,
                                                    minval=4, maxval=135)
        self._bambu_reads          = config.getboolean('bambu_reads',
                                                        d.bambu_reads if d else False)
        if self._bambu_reads and not self._tag_parsing:
            warning = ("[%s]: bambu_reads=True has no effect when "
                       "tag_parsing=False — set tag_parsing: True to enable "
                       "Bambu/MIFARE reads" % self._name)
            logger.warning(warning)
            self._diagnostic_warnings.append(warning)
        self._spoolman_auto_create = config.getboolean('spoolman_auto_create',
                                                        d.spoolman_auto_create if d else False)
        if self._spoolman_auto_create and not self._tag_parsing:
            warning = ("[%s]: spoolman_auto_create=True has no effect when "
                       "tag_parsing=False" % self._name)
            logger.warning(warning)
            self._diagnostic_warnings.append(warning)
        if self._spoolman_auto_create and self._spoolman is None:
            warning = ("[%s]: spoolman_auto_create=True requires Spoolman to "
                       "be enabled" % self._name)
            logger.warning(warning)
            self._diagnostic_warnings.append(warning)
        self._scan_searching_effect   = config.get(
            'scan_searching_effect',
            d.scan_searching_effect if d else 'mmu_clockwise_slow')
        self._scan_tag_read_effect    = config.get(
            'scan_tag_read_effect',
            d.scan_tag_read_effect if d else 'mmu_RFID_read')
        self._scan_rewind_effect      = config.get(
            'scan_rewind_effect',
            d.scan_rewind_effect if d else 'mmu_anticlock_fast')
        self._lane_auto_create_effect = config.get(
            'lane_auto_create_effect',
            d.lane_auto_create_effect if d else 'mmu_RFID_creating')
        self._lane_unresolved_effect  = config.get(
            'lane_unresolved_effect',
            d.lane_unresolved_effect if d else 'mmu_RFID_unresolved')

        self._scan_timer           = None
        self._scan_mode            = False
        self._scan_mm_total        = 0.0
        self._scan_next_chunk_time = 0.0
        self._scan_continuous_move_inflight = False
        self._scan_continuous_move_complete_time = 0.0
        self._scan_continuous_last_move_mm = 0.0
        self._scan_continuous_tag_pending = False
        self._scan_continuous_direct_available = True
        self._scan_decode_retry_attempts = 0
        self._scan_decode_retry_uid      = None
        self._scan_decode_retry_offset   = 0.0
        self._scan_left_neighbor_gate = -1
        self._scan_left_neighbor_shift_mm = 0.0
        self._scan_left_neighbor_shifted = False
        self._scan_left_neighbor_identity = None
        self._scan_left_neighbor_attempts = 0
        self._scan_idle_ready_time = 0.0
        self._scan_found_event     = None  # cached event suppressed during jog; dispatched after rewind
        self._prev_gate_status     = -1   # -1 = unknown; prevents false trigger on cold start
        self._scan_pending           = False  # armed on 0→1 edge; fires when Happy Hare confirms idle
        self._scan_deferred_notified = False  # True after first console msg for this deferral

        # ── Shared reader config and state ───────────────────────────────────
        # (_shared, _gate, _scan_enabled already set above)
        if self._shared:
            self._shared_pending_timeout = 30.0  # overwritten at connect from [mmu] pending_spool_id_timeout
            self._shared_read_timeout = config.getfloat(
                'shared_read_timeout', 120.0, minval=1.0)
            self._shared_led_segment = config.get(
                'shared_led_segment', 'exit').strip().lower()
            self._shared_tag_read_effect = config.get(
                'shared_tag_read_effect', '')
            self._shared_read_effect_duration = config.getfloat(
                'read_effect_duration', _SHARED_READ_EFFECT_DURATION,
                minval=0.1)
            self._shared_bypass_tag_read_effect = config.get(
                'shared_bypass_tag_read_effect', 'mmu_RFID_bypass_read')
            self._shared_bypass_read_effect_duration = config.getfloat(
                'bypass_read_effect_duration',
                _SHARED_BYPASS_READ_EFFECT_DURATION, minval=0.1)
            self._shared_spool_ready_effect = config.get(
                'shared_spool_ready_effect', '')
            self._shared_bypass_spool_ready_effect = config.get(
                'shared_bypass_spool_ready_effect', 'mmu_RFID_bypass_ready')
            self._shared_bypass_ready_effect_duration = config.getfloat(
                'bypass_ready_effect_duration',
                _SHARED_BYPASS_READY_EFFECT_DURATION, minval=0.1)
            self._shared_tag_unresolved_effect = config.get(
                'shared_tag_unresolved_effect', '')
            self._shared_unresolved_effect_duration = config.getfloat(
                'unresolved_effect_duration',
                _SHARED_UNRESOLVED_EFFECT_DURATION, minval=0.1)
            self._shared_spool_warning_effect = config.get(
                'shared_spool_warning_effect', 'mmu_RFID_warning')
            self._shared_auto_create_effect = config.get(
                'shared_auto_create_effect', '')
            self._shared_force_spool_id  = config.getboolean(
                'force_spool_id', True)
            self._shared_missed_limit    = config.getint(
                'shared_missed_limit', _SHARED_MISSED_RESOLUTION_LIMIT,
                minval=1)
        else:
            self._shared_pending_timeout = 30.0
            self._shared_read_timeout    = 120.0
            self._shared_tag_read_effect    = ''
            self._shared_read_effect_duration = _SHARED_READ_EFFECT_DURATION
            self._shared_bypass_tag_read_effect = ''
            self._shared_bypass_read_effect_duration = (
                _SHARED_BYPASS_READ_EFFECT_DURATION)
            self._shared_spool_ready_effect = ''
            self._shared_bypass_spool_ready_effect = ''
            self._shared_bypass_ready_effect_duration = (
                _SHARED_BYPASS_READY_EFFECT_DURATION)
            self._shared_tag_unresolved_effect = ''
            self._shared_unresolved_effect_duration = (
                _SHARED_UNRESOLVED_EFFECT_DURATION)
            self._shared_spool_warning_effect  = ''
            self._shared_auto_create_effect = ''
            self._shared_force_spool_id     = False
            self._shared_missed_limit    = _SHARED_MISSED_RESOLUTION_LIMIT

        self._shared_pending_uid            = None
        self._shared_pending_spool          = None
        self._shared_pending_deadline       = 0.0
        self._shared_pending_warning_fired  = False
        self._shared_pending_auto_created   = False
        self._shared_last_error             = None
        self._shared_last_action            = None
        self._shared_read_deadline          = 0.0
        self._shared_missed_resolutions     = 0
        self._shared_warning_timer          = None
        self._shared_led_failsafe_deadline  = 0.0
        self._shared_led_failsafe_reason    = None
        self._shared_preload_spool        = None
        self._shared_preload_uid          = None
        self._shared_preload_auto_created = False
        self._shared_preload_coordinator  = None
        self._shared_polling_suspended_for_print = False
        self._has_per_lane_readers        = False
        self._mmu_led_unit                = 'unit0'

        # delayed-init state
        self._gcode = None
        self._commands_registered = False
        self._status_registered = False
        self._help_registered = False
        self._shared_cmd_registered = False

        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.printer.register_event_handler('klippy:disconnect',
                                            self._handle_disconnect)
        if self._shared and self._enabled:
            self.printer.register_event_handler(
                'idle_timeout:printing', self._handle_print_start)
            self.printer.register_event_handler(
                'idle_timeout:ready', self._handle_print_end)

    def _cmd_NFC_STATUS_fallback(self, gcmd):
        gcmd.respond_info('\n'.join(_lane_status_lines(self.printer)))

    def _refresh_happy_hare_version(self):
        self._HAPPY_HARE_VERSION = _detect_happy_hare_version(self.printer)
        return self._HAPPY_HARE_VERSION

    def _happy_hare_version(self):
        if getattr(self, '_HAPPY_HARE_VERSION', None) is None:
            self._refresh_happy_hare_version()
        return getattr(self, '_HAPPY_HARE_VERSION', None)

    def _happy_hare_major_version(self):
        return _happy_hare_major_from_version(self._happy_hare_version())

    def _happy_hare_allows_scan_action(self, action):
        if action == 'idle':
            return True
        major = self._happy_hare_major_version()
        return major is not None and major >= 4 and action == 'checking'

    def _cmd_NFC_HELP_fallback(self, gcmd):
        gcmd.respond_info('\n'.join(_nfc_help(gcmd)))

    def _cmd_NFC_DOCTOR_fallback(self, gcmd):
        if _handle_doctor_action(self.printer, gcmd):
            return
        gcmd.respond_info(color_console_tags(
            '\n'.join(_doctor_lines(self.printer))))

    def _cmd_NFC_REGISTER_fallback(self, gcmd):
        _cmd_register_uid_to_spool(self._gcode, self._spoolman, gcmd)

    def _cmd_NFC_LED_TEST_fallback(self, gcmd):
        _cmd_led_test_all(gcmd)

    def _cmd_help(self, gcmd):
        lines = [
            "NFC GATE=%d commands:" % self._gate,
            "  NFC GATE=%d HELP     - show this help" % self._gate,
            "  NFC GATE=%d STATUS   - show this gate state" % self._gate,
            "  NFC GATE=%d INIT=1    - re-run reader init" % self._gate,
            "  NFC GATE=%d SCAN=1    - scan hardware once, no Spoolman/Happy Hare dispatch" % self._gate,
            "  NFC GATE=%d LED_TEST=1 CYCLES=2 - test configured lane tag-read LED effect" % self._gate,
            "  NFC GATE=%d JOG_SCAN=1 - start scan-jog (same as automatic pre-load trigger)" % self._gate,
            "  NFC GATE=%d POLL=1    - run one full NFC_Manager poll for this gate" % self._gate,
            "  NFC GATE=%d APPLY=1   - send cached spool to Happy Hare now" % self._gate,
            "  NFC GATE=%d CLEAR_CACHE=1 - clear cached spool lookup, no Happy Hare dispatch" % self._gate,
            "  NFC GATE=%d HH_SYNC=1 SPOOL_ID=<n> - seed lane cache from Happy Hare gate map (called by NFC_HH_SYNC_CACHE macro)" % self._gate,
            "  NFC GATE=%d READ=1    - start timer polling" % self._gate,
            "  NFC GATE=%d READ=0    - stop timer polling" % self._gate,
        ]
        if self._low_level_debug and self._reader_type == 'pn532':
            lines.extend(pn532_driver.low_level_debug_help_lines(
                "NFC GATE=%d" % self._gate))
        if self._low_level_debug and self._reader_type == 'rc522':
            lines.extend(rc522_driver.low_level_debug_help_lines(
                "NFC GATE=%d" % self._gate))
        gcmd.respond_info('\n'.join(lines))

    def _manual_scan(self, gcmd):
        if self._shared and self._is_printing():
            logger.warning(
                "[%s]: shared scan skipped while printing",
                self._name)
            gcmd.respond_info(
                "[WARN] NFC[%s]: shared scan skipped while printing" % self._name)
            return
        try:
            target_info = self._reader.read_target()
            if target_info is None:
                logger.info("[%s]: no tag detected", self._name)
                gcmd.respond_info(color_console_tags(
                    "NFC[%s]: no tag detected" % self._name))
                return
            sens_res = int(target_info.get('sens_res', 0) or 0)
            sak = target_info.get('sak')
            sak_text = "N/A" if sak is None else "0x%02X" % int(sak)
            logger.info(
                "[%s]: UID=%s Tg=%s SENS_RES=0x%04X SAK=%s UIDLen=%d",
                self._name, target_info['uid'], target_info['target'],
                sens_res, sak_text, target_info['uid_length'])
            gcmd.respond_info(
                color_console_tags(
                    "NFC[%s]: UID=%s Tg=%s SENS_RES=0x%04X SAK=%s UIDLen=%d"
                    % (self._name, target_info['uid'], target_info['target'],
                       sens_res, sak_text, target_info['uid_length'])))
        finally:
            if hasattr(self._reader, '_release_current_target'):
                self._reader._release_current_target(reason="manual_scan")

    def _manual_init(self, gcmd):
        self._failed = False
        try:
            self._reader.init()
            alive = self._reader.is_alive()
            self._failed = not alive
            reader_label = _reader_label(self._reader_type)
            if alive:
                logger.info("[%s]: %s OK", self._name, reader_label)
            else:
                logger.error(
                    "[%s]: %s did not respond — %s",
                    self._name, reader_label,
                    _reader_wiring_hint(self._reader_type))
            gcmd.respond_info(color_console_tags(
                "%s NFC[%s]: %s %s" %
                ("[OK]" if alive else "[WARN]", self._name, reader_label,
                 "OK" if alive else "not responding")))
            if (self._shared and alive and self._startup_polling == 1
                    and not self._is_printing()
                    and self._shared_pending_spool is None):
                self._shared_read_deadline = 0.0
                self._polling = True
                self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
                logger.info(
                    "[%s]: startup polling enabled; first poll in %.1fs",
                    self._name, 0.0)
                gcmd.respond_info(color_console_tags(
                    "NFC[%s]: startup polling resumed" % self._name))
        except Exception as e:
            self._failed = True
            logger.error("[%s]: init error: %s", self._name, e)
            gcmd.respond_info(color_console_tags(
                "[WARN] NFC[%s]: init failed: %s" % (self._name, e)))

    def _lane_led_test(self, gcmd=None, respond=True, cycles=None):
        effect = (getattr(self, '_scan_tag_read_effect', '') or '').strip()
        if not effect:
            msg = "[WARN] NFC[%s]: no lane tag-read LED effect configured" % self._name
            logger.warning(msg)
            if respond and gcmd is not None:
                gcmd.respond_info(color_console_tags(msg))
            return False
        if cycles is None:
            cycles = (_led_test_cycles_from_gcmd(gcmd)
                      if gcmd is not None
                      else LANE_LED_TEST_DEFAULT_CYCLES)
        cycles = max(1, min(int(cycles), LANE_LED_TEST_MAX_CYCLES))
        result = self._play_lane_led_test_effect(effect)
        gate_effect = result.effect
        if not result.ok:
            msg = ("[WARN] NFC[%s]: LED test failed for effect %s: %s"
                   % (self._name, gate_effect, result.error))
            logger.warning(msg)
            if respond and gcmd is not None:
                gcmd.respond_info(color_console_tags(msg))
            return False
        self._schedule_lane_led_test_cycles(effect, cycles)
        logger.info("[%s]: lane LED test started effect=%s cycles=%d",
                    self._name, gate_effect, cycles)
        if respond and gcmd is not None:
            gcmd.respond_info(color_console_tags(
                "[OK] NFC[%s]: lane LED test started (%s cycles=%d)"
                % (self._name, gate_effect, cycles)))
        return True

    def _play_lane_led_test_effect(self, effect):
        return LEDEffectManager(
            self.printer, reactor=self.reactor, runner=self._safe_run_script,
            name=self._name).play_led_test(
                effect, self._gate, async_dispatch=True, log_failure=False)

    def _schedule_lane_led_test_cycles(self, effect, cycles):
        if not effect:
            return
        LEDEffectManager(
            self.printer, reactor=self.reactor, runner=self._safe_run_script,
            name=self._name).schedule_lane_test_cycles(
                effect, self._gate, cycles=cycles,
                duration=LANE_LED_TEST_DURATION, gap=LANE_LED_TEST_GAP,
                skip_restore=lambda: getattr(self, '_scan_mode', False))

    def _shared_led_target(self):
        segment = getattr(self, '_shared_led_segment', 'exit')
        segment = (segment or 'exit').strip().lower()
        mcu_index = getattr(self, '_shared_mcu_index', None)
        if segment == 'gate':
            mmu = self.printer.lookup_object('mmu', None)
            gate_count = None
            if mmu is not None:
                gate_count = getattr(mmu, 'num_gates', None)
                if gate_count is None:
                    try:
                        status = mmu.get_status(self.reactor.monotonic())
                        gate_count = len(status.get('gate_spool_id', []))
                    except Exception:
                        gate_count = None
            if (mcu_index is None
                    or (gate_count is not None
                        and not (0 <= int(mcu_index) < int(gate_count)))):
                if gate_count is not None:
                    logger.warning(
                        "[%s]: shared_led_segment=gate derived GATE=%s but "
                        "Happy Hare has %s gates; using UNIT LEDs instead",
                        self._name, mcu_index, gate_count)
                else:
                    logger.warning(
                        "[%s]: shared_led_segment=gate has no valid gate; "
                        "using UNIT LEDs instead",
                        self._name)
                segment = 'exit'
                mcu_index = None
        return (
            getattr(self, '_mmu_led_unit', 'unit0'),
            segment,
            mcu_index)

    def _shared_gate_effect_name(self, base):
        """Return the Happy Hare LED effect name for a shared reader event.

        shared_led_segment selects the target style:
          gate -> legacy per-gate effect {base}_exit_{index}
          else -> whole-chain effect {unit}_{base}_{segment}
        """
        led_unit, segment, mcu_index = self._shared_led_target()
        return shared_effect_name(
            base,
            led_unit=led_unit,
            segment=segment,
            mcu_index=mcu_index)

    def _shared_play_led_effect(self, effect_name, gcmd=None, event='',
                                duration=None):
        if not effect_name:
            if gcmd is not None:
                logger.warning(
                    "[%s]: no LED effect configured", self._name)
                gcmd.respond_info(
                    "[WARN] NFC[%s]: no LED effect configured" % self._name)
            return False
        led_unit, segment, mcu_index = self._shared_led_target()
        gate_effect = shared_effect_name(
            effect_name, led_unit=led_unit, segment=segment,
            mcu_index=mcu_index)
        logger.info(
            "[%s]: LED effect %s scheduled",
            self._name, gate_effect)
        if gcmd is not None:
            gcmd.respond_info(
                "NFC[%s]: LED effect %s started" % (self._name, gate_effect))
        # Deferred via async callback — safe from both timer callbacks and
        # GCode handlers (run_script from a GCode handler re-enters the
        # GCode mutex and deadlocks).
        LEDEffectManager(
            self.printer, reactor=self.reactor, runner=self._safe_run_script,
            name=self._name).play_shared_event(
                event, effect_name,
                led_unit=led_unit,
                segment=segment,
                mcu_index=mcu_index,
                replace=True, async_dispatch=True,
                duration=duration)
        return True

    def _shared_arm_led_failsafe(self, timeout, reason):
        if not self._shared:
            return
        try:
            timeout = float(timeout)
        except Exception:
            timeout = 0.0
        if timeout <= 0.0:
            return
        self._shared_led_failsafe_reason = reason
        self._shared_led_failsafe_deadline = self.reactor.monotonic() + timeout
        self.reactor.update_timer(
            self._shared_led_failsafe_timer,
            self._shared_led_failsafe_deadline)
        if self._debug >= 4:
            logger.debug(
                "[%s]: shared LED failsafe armed for %.2fs (%s)",
                self._name, timeout, reason)

    def _shared_cancel_led_failsafe(self):
        if not getattr(self, '_shared', False):
            return
        self._shared_led_failsafe_deadline = 0.0
        self._shared_led_failsafe_reason = None
        self.reactor.update_timer(
            self._shared_led_failsafe_timer, self.reactor.NEVER)

    def _shared_led_failsafe_event(self, eventtime):
        if (not self._shared
                or self._shared_led_failsafe_deadline <= 0.0):
            return self.reactor.NEVER
        if eventtime < self._shared_led_failsafe_deadline:
            return self._shared_led_failsafe_deadline
        reason = self._shared_led_failsafe_reason or "unknown"
        self._shared_led_failsafe_deadline = 0.0
        self._shared_led_failsafe_reason = None
        if self._shared_pending_spool is None:
            logger.info(
                "[%s]: shared LED failsafe released Happy Hare ownership (%s)",
                self._name, reason)
            self._shared_restore_hh_leds()
        return self.reactor.NEVER

    def _shared_play_tag_read_effect(self, gcmd=None, effect_name=None,
                                     duration=None):
        effect_name = effect_name or self._shared_tag_read_effect
        started = self._shared_play_led_effect(
            effect_name, gcmd, event=EVENT_TAG_READ, duration=duration)
        if started and duration is None:
            self._shared_arm_led_failsafe(
                self._shared_read_effect_duration, EVENT_TAG_READ)
        return started

    def _shared_play_spool_ready_effect(self):
        # Normal staged-ready feedback stays active until the shared-reader
        # lifecycle releases Happy Hare ownership on preload commit/cancel/timeout.
        self._shared_cancel_led_failsafe()
        self._shared_play_led_effect(
            self._shared_spool_ready_effect, event=EVENT_SPOOL_READY)

    def _shared_clear_pending_warning_feedback(self):
        self.reactor.update_timer(self._warning_timer, self.reactor.NEVER)
        if self._shared_pending_warning_fired:
            self._shared_restore_hh_leds()

    def _shared_play_tag_unresolved_effect(self):
        self._shared_cancel_led_failsafe()
        self._shared_play_led_effect(
            self._shared_tag_unresolved_effect, event=EVENT_UNRESOLVED,
            duration=self._shared_unresolved_effect_duration)

    def _shared_play_auto_create_effect(self):
        self._shared_cancel_led_failsafe()
        self._shared_play_led_effect(
            self._shared_auto_create_effect, event=EVENT_AUTO_CREATE)


    def _shared_restore_hh_leds(self):
        self._shared_cancel_led_failsafe()
        # Deferred via async callback — safe from both timer callbacks and
        # GCode handlers (run_script from a GCode handler re-enters the
        # GCode mutex and deadlocks).
        LEDEffectManager(
            self.printer, reactor=self.reactor, runner=self._safe_run_script,
            name=self._name).release(async_dispatch=True)

    def _set_reading(self, gcmd, enabled):
        if enabled:
            if self._failed:
                if self._shared:
                    logger.error(
                        "[%s]: shared READ=1 refused — "
                        "reader failed; run INIT=1 first",
                        self._name)
                else:
                    logger.error(
                        "[%s]: gate %d READ=1 refused — "
                        "reader failed; run INIT=1 first",
                        self._name, self._gate)
                gcmd.respond_info(color_console_tags(
                    "[WARN] NFC[%s]: reader failed; run INIT=1 first"
                    % self._name))
                return
            if self._shared:
                if self._is_printing():
                    logger.warning(
                        "[%s]: shared READ=1 refused — printing",
                        self._name)
                    gcmd.respond_info(
                        "[WARN] NFC[%s]: shared polling not started while printing"
                        % self._name)
                    return
                if self._shared_pending_spool is not None:
                    logger.warning(
                        "[%s]: shared READ=1 refused — "
                        "spool %s already pending",
                        self._name, self._shared_pending_spool)
                    gcmd.respond_info(
                        "[WARN] NFC[%s]: spool %s is already pending; use "
                        "NFC_SHARED REPLACE=1 to discard it and scan another, "
                        "or NFC_SHARED CANCEL=1 to cancel"
                        % (self._name, self._shared_pending_spool))
                    return
                self._shared_missed_resolutions = 0
                self._shared_last_error = None
                self._shared_read_deadline = (
                    self.reactor.monotonic() + self._shared_read_timeout)
                logger.info(
                    "[%s]: shared READ=1 — polling started "
                    "with %.0fs read timeout",
                    self._name, self._shared_read_timeout)
            self._polling = True
            self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
            if not self._shared:
                logger.info(
                    "[%s]: gate %d READ=1 — polling started",
                    self._name, self._gate)
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: polling started" % self._name))
        else:
            if self._shared:
                self._shared_read_deadline = 0.0
                logger.info(
                    "[%s]: shared READ=0 — polling stopped; "
                    "pending spool=%s kept",
                    self._name, self._shared_pending_spool)
                if self._shared_pending_spool is None:
                    self._shared_restore_hh_leds()
            else:
                logger.info(
                    "[%s]: gate %d READ=0 — polling stopped",
                    self._name, self._gate)
            self._polling = False
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: polling stop requested" % self._name))

    def _clear_spool_cache(self, gcmd):
        """Clear cached spool resolution without dispatching a state change."""
        old_spool = self._state.current_spool
        self._state.current_spool = None
        self._suppress_next_dispatch_uid   = self._state.current_uid
        self._suppress_next_dispatch_spool = old_spool  # only suppress if spool is also unchanged
        if self._spoolman is not None:
            self._spoolman.clear_cache()
        if hasattr(self._reader, '_clear_current_card'):
            self._reader._clear_current_card()
        logger.info(
            "[%s]: gate %d — spool cache cleared "
            "(uid=%s old_spool=%s); next read will resolve Spoolman again",
            self._name, self._gate, self._state.current_uid, old_spool)
        gcmd.respond_info(
            color_console_tags(
                "NFC[%s]: cleared cached spool_id for gate %d; "
                "no NFC_Manager event was dispatched. Next tag read will resolve "
                "Spoolman again."
                % (self._name, self._gate)))

    def _shared_clear_cache(self, gcmd):
        """Clear shared reader tag/cache state while keeping staged spool."""
        pending_spool = self._shared_pending_spool
        pending_uid   = self._shared_pending_uid
        self._state.reset()
        if self._spoolman is not None:
            self._spoolman.clear_cache()
        if hasattr(self._reader, '_clear_current_card'):
            self._reader._clear_current_card()
        logger.info(
            "[%s]: shared tag cache cleared; "
            "pending spool=%s uid=%s kept",
            self._name, pending_spool, pending_uid)
        gcmd.respond_info(color_console_tags(
            "NFC[%s]: shared tag cache cleared; pending spool kept"
            % self._name))

    def _apply_current_spool(self, gcmd):
        """Dispatch the current cached spool to Happy Hare immediately."""
        if self._state.current_spool is None:
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: no cached spool_id to apply; run POLL=1 first"
                % self._name))
            return
        uid_hex = self._state.current_uid or ''
        spool_id = self._state.current_spool
        if spool_id is DIRECT_METADATA_SPOOL:
            meta = (self._state.current_tag.meta
                    if self._state.current_tag is not None else {})
            logger.info(
                "[%s]: gate %d — manual apply metadata uid=%s",
                self._name, self._gate, uid_hex)
            self._klipper.dispatch(EVENT_CHANGED, self._gate, uid_hex,
                                   None, meta=meta)
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: dispatched cached tag metadata for gate %d to "
                "Happy Hare" % (self._name, self._gate)))
            return
        logger.info(
            "[%s]: gate %d — manual apply spool=%s uid=%s",
            self._name, self._gate, spool_id, uid_hex)
        self._klipper.dispatch(EVENT_CHANGED, self._gate, uid_hex, spool_id)
        gcmd.respond_info(color_console_tags(
            "NFC[%s]: dispatched cached spool_id=%s for gate %d to "
            "Happy Hare"
            % (self._name, spool_id, self._gate)))

    def _cmd_low_level_debug(self, gcmd):
        if rc522_driver.low_level_debug_requested(gcmd):
            if self._polling:
                self._polling = False
                self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
                gcmd.respond_info(color_console_tags(
                    "NFC[%s]: polling paused for low-level RC522 debug" %
                    self._name))
            try:
                command_base = (
                    "NFC_SHARED" if self._shared else
                    "NFC GATE=%d" % self._gate)
                return rc522_driver.run_low_level_debug(
                    gcmd, self._reader, self._name, command_base,
                    self._low_level_debug)
            except Exception as e:
                gcmd.respond_info(color_console_tags(
                    "NFC[%s]: RC522 low-level debug failed: %s"
                    % (self._name, e)))
                return True
        if (pn532_driver.low_level_debug_requested(gcmd)
                and self._reader_type != 'pn532'):
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: PN532 low-level commands are not valid for "
                "reader_type=%s" % (self._name, self._reader_type)))
            return True
        if pn532_driver.low_level_debug_requested(gcmd) and self._polling:
            self._polling = False
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: polling paused for low-level PN532 debug" %
                self._name))
        try:
            return pn532_driver.run_low_level_debug(
                gcmd, self._reader, self._name,
                "NFC GATE=%d" % self._gate,
                self._low_level_debug)
        except Exception as e:
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: low-level debug failed: %s" % (self._name, e)))
            return True

    def cmd_NFC(self, gcmd):
        if _flag_param(gcmd, "HELP"):
            self._cmd_help(gcmd)
            return
        if self._cmd_low_level_debug(gcmd):
            return
        read_value = gcmd.get("READ", None)
        if read_value is not None:
            self._set_reading(gcmd, gcmd.get_int("READ", minval=0, maxval=1) == 1)
            return
        if _flag_param(gcmd, "STATUS"):
            gcmd.respond_info(self.status_line())
            return
        if gcmd.get_int("INIT", 0):
            self._manual_init(gcmd)
            return
        if gcmd.get_int("SCAN", 0):
            self._manual_scan(gcmd)
            return
        if gcmd.get_int("LED_TEST", 0):
            self._lane_led_test(gcmd)
            return
        if gcmd.get_int("JOG_SCAN", 0):
            self._manual_jog_scan(gcmd)
            return
        if gcmd.get_int("CLEAR_CACHE", 0):
            self._clear_spool_cache(gcmd)
            return
        if gcmd.get_int("CLEAR", 0):
            self._clear_spool_cache(gcmd)
            return
        if gcmd.get_int("POLL", 0):
            self._poll()
            status = self.status_line().strip()
            logger.info("[%s]: one poll complete; %s", self._name, status)
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: one poll complete; %s" % (self._name, status)))
            return
        if gcmd.get_int("APPLY", 0):
            self._apply_current_spool(gcmd)
            return
        if gcmd.get_int("HH_SYNC", 0):
            self._hh_sync(gcmd)
            return
        self._cmd_help(gcmd)

    def _read_hh_status(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        return hh_status.read(self.printer, self._gate, eventtime)

    def _seed_cache_from_hh(self, eventtime):
        """Read Happy Hare's gate map and pre-seed this lane's spool cache.

        Called once from _delayed_init() after the NFC Reader initialises
        successfully.  Prevents a spurious _NFC_SPOOL_CHANGED dispatch on the
        very first poll after a Klipper restart — Happy Hare already knows
        which spool is in this gate, so we should not re-tell it.

        The seed is one-shot: it is consumed (cleared) on the first
        EVENT_CHANGED poll result, regardless of whether the spool matches.
        Mismatches still dispatch normally.
        """
        try:
            hh = self._read_hh_status(eventtime)
            if not hh.present:
                logger.info(
                    "[%s]: gate %d — Happy Hare MMU object not found; "
                    "skipping startup cache seed", self._name, self._gate)
                return
            if self._gate >= hh.gate_count:
                logger.info(
                    "[%s]: gate %d — gate index exceeds Happy Hare map length "
                    "(%d gates); skipping seed", self._name, self._gate,
                    hh.gate_count)
                return

            if hh.assigned:
                self._hh_seed_spool_id  = hh.spool
                self._hh_seed_available = hh.available

                if hh.available and self._spoolman is not None:
                    # Gate is physically loaded — pre-populate NFC cache from
                    # Spoolman so status is correct before the first physical scan.
                    uid = self._spoolman.get_uid_for_spool(hh.spool)
                    if uid:
                        self._state.current_uid   = uid
                        self._state.current_spool = hh.spool
                        self._hh_confirmed_spool  = hh.spool
                        logger.info(
                            "[%s]: gate %d — startup: seeded from "
                            "Happy Hare+Spoolman spool_id=%d uid=%s",
                            self._name, self._gate, hh.spool, uid)
                    else:
                        logger.info(
                            "[%s]: gate %d — Happy Hare seed: spool_id=%d "
                            "available (no UID in Spoolman — will verify on "
                            "first poll)",
                            self._name, self._gate, hh.spool)
                else:
                    logger.info(
                        "[%s]: gate %d — Happy Hare seed: spool_id=%d  "
                        "gate_status=%s  (will verify on first physical scan)",
                        self._name, self._gate, hh.spool, hh.status)
            else:
                logger.info(
                    "[%s]: gate %d — Happy Hare reports gate %s "
                    "(spool_id=%s); no seed applied",
                    self._name, self._gate,
                    "found/no spool" if hh.available else "empty/unknown",
                    hh.spool)

        except Exception:
            logger.exception(
                "[%s]: gate %d — error reading Happy Hare gate map for "
                "startup cache seed (non-fatal, polling continues)",
                self._name, self._gate)

    def _hh_sync(self, gcmd):
        """Receive a spool_id from NFC_HH_SYNC_CACHE and set the lane seed.

        Called by NFC GATE=<#> HH_SYNC=1 SPOOL_ID=<n>.
        The macro reads Happy Hare template vars (which GCode macros can access) and
        passes the resolved spool_id here so Python can update the seed without
        needing to walk the Happy Hare object itself.
        """
        spool_id = gcmd.get_int('SPOOL_ID', -1)
        if spool_id > 0:
            self._hh_seed_spool_id = spool_id
            logger.info(
                "[%s]: gate %d — HH_SYNC: seed set to spool_id=%d",
                self._name, self._gate, spool_id)
            gcmd.respond_info(
                "NFC[%s]: Happy Hare seed → spool_id=%d  "
                "(next poll matching this spool will not re-dispatch to Happy Hare)"
                % (self._name, spool_id))
        else:
            self._hh_seed_spool_id = None
            logger.info(
                "[%s]: gate %d — HH_SYNC: gate empty/unknown, "
                "seed cleared", self._name, self._gate)
            gcmd.respond_info(
                "NFC[%s]: Happy Hare reports gate empty — seed cleared" % self._name)

    def _validate_startup_config(self):
        if not getattr(self, '_enabled', True):
            return
        if self._shared:
            raw_config = _raw_klipper_config(self.printer)
            macro = raw_config.get('gcode_macro _MMU_SEQUENCE_VARS', {})
            hook = str(macro.get('variable_user_post_preload_extension', ''))
            warning = _shared_preload_hook_message(hook, self._name)
            if warning:
                warnings = getattr(self, '_diagnostic_warnings', None)
                if warnings is None:
                    warnings = []
                    self._diagnostic_warnings = warnings
                if warning not in warnings:
                    warnings.append(warning)
                    logger.warning(warning)
                    if self._gcode is not None:
                        self._gcode.respond_info(color_console_tags(
                            "[WARN] NFC[%s]: shared preload hook is not wired; "
                            "set variable_user_post_preload_extension: "
                            "'_NFC_SHARED_PRELOAD'" % self._name))

    def _shared_preload_hook_ready(self):
        raw_config = _raw_klipper_config(self.printer)
        macro = raw_config.get('gcode_macro _MMU_SEQUENCE_VARS', {})
        hook = str(macro.get('variable_user_post_preload_extension', ''))
        return _shared_preload_hook_message(hook, self._name) is None

    def _shared_preload_hook_hint(self):
        if self._shared_preload_hook_ready():
            return ""
        return (" Check mmu_macro_vars.cfg: "
                "variable_user_post_preload_extension must be "
                "'_NFC_SHARED_PRELOAD', not 'NFC JOG_SCAN=1'.")

    def _handle_connect(self):
        global _shared_instance
        self._gcode = self.printer.lookup_object('gcode')
        self._validate_startup_config()
        if self._shared:
            self._shared_pending_timeout = self._read_mmu_pending_timeout()
        if self._shared:
            for section_name, _ in self.printer.lookup_objects('mmu_leds'):
                self._mmu_led_unit = section_name.split()[-1]
                break
        if self._shared:
            _shared_instance = self

        if not self._commands_registered:
            # Register the status command once when there is no base [nfc_gate]
            # section. We guard on _lane_instances[0] is self so that only the
            # first lane instance registers it — later lanes skip this block.
            # (self._defaults is None means NFCGateDefaults.__init__ never ran
            # and no one else has registered NFC_STATUS yet.)
            if self._defaults is None and _lane_instances and _lane_instances[0] is self and not self._status_registered:
                self._gcode.register_command(
                    'NFC_STATUS',
                    self._cmd_NFC_STATUS_fallback,
                    desc="Report spool state for all configured NFC gates"
                )
                self._status_registered = True
            if (self._defaults is None and _lane_instances
                    and _lane_instances[0] is self
                    and not self._help_registered):
                self._gcode.register_command(
                    'NFC_HELP',
                    self._cmd_NFC_HELP_fallback,
                    desc="Show NFC reader command help"
                )
                self._help_registered = True
            if (self._defaults is None and _lane_instances
                    and _lane_instances[0] is self):
                self._gcode.register_command(
                    'NFC_DOCTOR',
                    self._cmd_NFC_DOCTOR_fallback,
                    desc="Check NFC reader setup and common configuration problems"
                )
                self._gcode.register_command(
                    'NFC_REGISTER',
                    self._cmd_NFC_REGISTER_fallback,
                    desc="Assign an NFC UID to a Spoolman spool"
                )
                self._gcode.register_command(
                    'NFC_LED_TEST',
                    self._cmd_NFC_LED_TEST_fallback,
                    desc="Test NFC lane LED effects"
                )

            # Shared reader has no mmu_gate — all interaction goes through
            # NFC_SHARED.  Lane readers register the GATE mux command.
            if self._shared:
                self._gcode.register_command(
                    'NFC_SHARED',
                    self.cmd_NFC_SHARED,
                    desc=("Control shared NFC reader: READ, POLL, SCAN, "
                          "STATUS, HELP, CANCEL, REPLACE, RESET")
                )
                self._shared_cmd_registered = True
            else:
                self._gcode.register_mux_command(
                    cmd='NFC',
                    key='GATE',
                    value=str(self._gate),
                    func=self.cmd_NFC,
                    desc="Control or test one configured NFC gate"
                )

            self._commands_registered = True

        logger.info("[%s]: connected", self._name)
        self._gcode.respond_info(f"[CONNECTED] NFC Gate [{self._name}] connected")

        # Schedule NFC Reader init after the rest of Klippy/I2C has settled.
        self.reactor.register_timer(
            self._delayed_init,
            self.reactor.monotonic() + 2.0
        )

    def _startup_run_check_gate(self, gate_number, reason):
        script = "MMU_CHECK_GATE GATE=%d" % gate_number
        try:
            logger.info("[%s]: startup check-gate — %s; running %s",
                        self._name, reason, script)
            self._gcode.run_script(script)
            return True
        except Exception as e:
            logger.warning("[%s]: startup check-gate failed (%s): %s",
                           self._name, script, e)
            return False

    def _startup_check_unknown_gate(self, eventtime):
        """Ask Happy Hare to classify this gate if it still reports unknown."""
        if self._gcode is None:
            return
        if self._is_printing():
            logger.info(
                "[%s]: gate %d — startup check-gate skipped while printing",
                self._name, self._gate)
            return

        hh = self._read_hh_status(eventtime)
        if not hh.present or self._gate >= hh.gate_count:
            return
        if hh.status != -1:
            return
        if not hh.idle:
            logger.info(
                "[%s]: gate %d — startup check-gate skipped because "
                "Happy Hare is busy (action=%s)",
                self._name, self._gate, hh.action)
            return
        if hh.filament_pos != hh_status.FILAMENT_POS_UNLOADED:
            logger.info(
                "[%s]: gate %d — startup check-gate skipped because "
                "filament is not parked (filament_pos=%d)",
                self._name, self._gate, hh.filament_pos)
            return

        if self._startup_run_check_gate(
                self._gate,
                "Happy Hare reports gate %d status=-1" % self._gate):
            refreshed = self._read_hh_status(self.reactor.monotonic())
            logger.info(
                "[%s]: gate %d — startup check-gate complete; "
                "Happy Hare status=%s spool=%s",
                self._name, self._gate, refreshed.status, refreshed.spool)

    def _startup_check_unknown_gate_event(self, eventtime):
        was_polling = self._polling
        if was_polling:
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
        try:
            self._startup_check_unknown_gate(eventtime)
            seed_time = self.reactor.monotonic()
            self._seed_cache_from_hh(seed_time)
            hh = self._read_hh_status(seed_time)
            if hh.present and self._gate < hh.gate_count:
                self._prev_gate_status = hh.status
        finally:
            if was_polling and not self._failed:
                self.reactor.update_timer(
                    self._poll_timer,
                    self.reactor.monotonic() + self._poll_interval)
        return self.reactor.NEVER

    def _delayed_init(self, eventtime):
        """Initialise the NFC Reader after other I2C devices have settled.

        Runs in the reactor thread 2 seconds after klippy:connect fires.
        Returns reactor.NEVER so the timer does not repeat.
        """
        if self._debug >= 4:
            logger.debug(
                "[%s]: delayed init — %s",
                self._name, _reader_label(self._reader_type))

        try:
            self._reader.init()
            reader_label = _reader_label(self._reader_type)
            if self._reader.is_alive():
                self._failed = False
                logger.info("[%s]: %s OK", self._name, reader_label)
            else:
                self._failed = True
                logger.error(
                    "[%s]: %s did not respond — %s",
                    self._name, reader_label,
                    _reader_wiring_hint(self._reader_type))
        except Exception as e:
            self._failed = True
            logger.error("[%s]: init error: %s", self._name, e)

        # Seed lane cache from Happy Hare's current gate map so the first poll
        # after restart does not re-dispatch a spool Happy Hare already knows about.
        # Shared reader has no Happy Hare gate assignment to seed and no scan-jog edge detector.
        if not self._failed and not self._shared:
            seed_time = self.reactor.monotonic()
            self._seed_cache_from_hh(seed_time)
            # Bootstrap the scan-jog edge detector with the current gate status
            # so a pre-loaded gate never triggers a scan on the first poll.
            hh = self._read_hh_status(seed_time)
            if hh.present and self._gate < hh.gate_count:
                self._prev_gate_status = hh.status
            if self._spoolman is None:
                delay = (STARTUP_UNKNOWN_GATE_CHECK_DELAY
                         + (self._gate
                            * STARTUP_UNKNOWN_GATE_CHECK_STAGGER))
                self.reactor.update_timer(
                    self._startup_check_timer,
                    self.reactor.monotonic() + delay)
                if self._debug >= 3:
                    logger.info(
                        "[%s]: gate %d — Spoolman disabled; startup "
                        "unknown-gate check scheduled in %.1fs",
                        self._name, self._gate, delay)

        if self._gcode is not None:
            if self._failed:
                init_cmd = ("NFC_SHARED INIT=1" if self._shared
                            else "NFC GATE=%d INIT=1" % self._gate)
                logger.warning(
                    "[%s]: not ready — %s. Run %s after fixing.",
                    self._name, _reader_wiring_hint(self._reader_type),
                    init_cmd)
                self._gcode.respond_info(scan_jog._color_tags(
                    "[WARN] NFC[%s]: not ready — %s. "
                    "Run %s after fixing."
                    % (self._name, _reader_wiring_hint(self._reader_type),
                       init_cmd)))
            else:
                if self._shared:
                    seed_note = ""
                    read_cmd = "NFC_SHARED READ=1"
                else:
                    seed_note = ("  Happy Hare seed: spool_id=%d" % self._hh_seed_spool_id
                                 if self._hh_seed_spool_id is not None
                                 else "  Happy Hare reports gate empty")
                    read_cmd = "NFC GATE=%d READ=1" % self._gate
                if self._debug >= 3:
                    logger.info(
                        "[%s]: ready.%s  %s",
                        self._name,
                        seed_note,
                        ("Startup polling is enabled; first poll in %.1fs."
                         % self._startup_poll_delay)
                        if self._startup_polling == 1
                        else ("Run %s to start polling." % read_cmd))
                self._gcode.respond_info(scan_jog._color_tags(
                    "[OK] NFC[%s]: ready.%s  %s"
                    % (self._name,
                       seed_note,
                       "Startup polling is enabled; first poll in %.1fs."
                       % self._startup_poll_delay
                       if self._startup_polling == 1
                       else "Run %s to start polling." % read_cmd)))

        if not self._failed and self._startup_polling == 1:
            self._polling = True
            first_poll = self.reactor.monotonic() + self._startup_poll_delay
            self.reactor.update_timer(self._poll_timer, first_poll)
            logger.info("[%s]: startup polling enabled; first poll in %.1fs",
                        self._name, self._startup_poll_delay)

        return self.reactor.NEVER

    def _handle_disconnect(self):
        if self._debug >= 4:
            logger.debug("[%s]: disconnect — stopping polling timer",
                         self._name)
        self._polling = False
        self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
        self.reactor.update_timer(self._startup_check_timer,
                                  self.reactor.NEVER)
        if self._shared and self._shared_pending_spool is None:
            self._shared_restore_hh_leds()
        if self._scan_timer is not None:
            self.reactor.update_timer(self._scan_timer, self.reactor.NEVER)
        if self._scan_mode:
            scan_jog.disconnect_cleanup(self)
        if NFCGate._active_scan_gate == self._gate:
            NFCGate._active_scan_gate = None

    def _reactor_sleep(self, duration):
        self.reactor.pause(self.reactor.monotonic() + duration)

    def _handle_print_start(self, print_time):
        if not self._polling:
            return
        if not self._is_printing():
            return
        logger.info(
            "[%s]: printing started — shared polling suspended",
            self._name)
        self._shared_polling_suspended_for_print = True
        self._polling = False
        self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
        if self._shared_pending_spool is None:
            self._shared_restore_hh_leds()

    def _handle_print_end(self, print_time):
        if not self._shared_polling_suspended_for_print:
            return
        if self._polling or self._failed:
            self._shared_polling_suspended_for_print = False
            return
        self._shared_polling_suspended_for_print = False
        if self._startup_polling == 1:
            self._shared_expire_pending_if_needed()
            # Don't restart polling while a valid spool is already staged —
            # the design keeps polling stopped between a successful tag read
            # and PRELOAD_CHECK so the pending spool is not accidentally
            # overwritten by the next tag that drifts into range.
            if self._shared_pending_spool is not None:
                now = self.reactor.monotonic()
                if (self._shared_pending_deadline <= 0.0
                        or now < self._shared_pending_deadline):
                    logger.info(
                        "[%s]: printing complete — spool %d still "
                        "pending; polling stays stopped until PRELOAD_CHECK",
                        self._name, self._shared_pending_spool)
                    return
            logger.info(
                "[%s]: printing complete — shared polling resumed",
                self._name)
            self._shared_read_deadline = 0.0
            self._polling = True
            self.reactor.update_timer(self._poll_timer, self.reactor.NOW)

    def _warning_timer_event(self, eventtime):
        if not self._shared or self._shared_pending_spool is None:
            return self.reactor.NEVER
        if self._shared_pending_warning_fired:
            if eventtime >= self._shared_pending_deadline:
                self._shared_expire_pending_and_maybe_resume()
                return self.reactor.NEVER
            return self._shared_pending_deadline
        if eventtime >= self._shared_pending_deadline:
            self._shared_expire_pending_and_maybe_resume()
            return self.reactor.NEVER
        self._shared_pending_warning_fired = True
        remaining = max(1.0, self._shared_pending_deadline - eventtime)
        if self._shared_spool_warning_effect:
            self._shared_play_led_effect(
                self._shared_spool_warning_effect, event=EVENT_WARNING)
            # Re-arm this timer for the actual deadline so timeout cleanup does
            # not depend on the polling timer firing while polling is paused.
            self.reactor.update_timer(
                self._warning_timer, self._shared_pending_deadline)
        msg = ("[WARN] NFC[%s]: spool %d staged — load into gate soon "
               "or tap tag again (%.0fs remaining)"
               % (self._name, self._shared_pending_spool, remaining))
        logger.warning(msg, extra={'nfc_no_console': True})
        if self._gcode is not None:
            self._gcode.respond_info(color_console_tags(msg))
        return self._shared_pending_deadline

    def _poll_timer_event(self, eventtime):
        if not self._polling:
            if self._shared:
                if (self._shared_pending_spool is not None
                        and not self._shared_pending_warning_fired):
                    warning_time = (self._shared_pending_deadline
                                    - 0.2 * self._shared_pending_timeout)
                    if eventtime >= warning_time:
                        self._warning_timer_event(eventtime)
                self._shared_expire_pending_and_maybe_resume()
                if self._polling:
                    return self.reactor.NOW
                if self._shared_pending_spool is not None:
                    return self._shared_pending_deadline
            return self.reactor.NEVER
        if self._failed:
            init_cmd = "NFC_SHARED INIT=1" if self._shared else "NFC GATE=%d INIT=1" % self._gate
            logger.warning("[%s]: polling stopped — reader failed; "
                           "run %s first",
                           self._name, init_cmd)
            self._polling = False
            if self._shared and self._shared_pending_spool is None:
                self._shared_restore_hh_leds()
            return self.reactor.NEVER

        # Shared read-timeout: stop polling if READ=1 has been active too long
        # without resolving a valid tag.
        if self._shared and self._shared_read_deadline > 0.0:
            if eventtime >= self._shared_read_deadline:
                logger.info(
                    "[%s]: shared read timeout (%.0fs) — stopping poll",
                    self._name, self._shared_read_timeout)
                self._shared_read_deadline = 0.0
                self._polling = False
                if self._shared_pending_spool is None:
                    self._shared_restore_hh_leds()
                return self.reactor.NEVER

        # Scan-jog gate-status edge detection.
        # Reads Happy Hare gate_status on every tick — Python dict only, no I2C.
        # When gate is empty (curr==0) skip the I2C read entirely.
        # On 0 -> >=1 transition with Happy Hare idle and not printing, enter scan mode.
        # A -1 -> >=1 transition is Happy Hare resolving an unknown state, not a new load.
        if self._scan_enabled:
            hh = self._read_hh_status(eventtime)
            if hh.present and self._gate < hh.gate_count:
                curr = hh.status
                prev = self._prev_gate_status
                self._prev_gate_status = curr
                if self._debug >= 4:
                    logger.debug(
                        "[%s]: gate %d — Happy Hare poll: "
                        "prev=%s curr=%s action=%s pending=%s printing=%s "
                        "active_scan=%s load_paused=%s",
                        self._name, self._gate,
                        prev, curr, hh.action,
                        getattr(self, '_scan_pending', False),
                        self._is_printing(),
                        NFCGate._active_scan_gate if NFCGate._active_scan_gate is not None else 'none',
                        self._hh_load_paused)
                if (curr >= 1 and self._state.current_spool is not None
                        and self._state.current_spool is not DIRECT_METADATA_SPOOL):
                    self._scan_pending = False
                    if not self._hh_load_paused:
                        self._hh_load_paused = True
                        logger.info(
                            "[%s]: gate %d — Happy Hare reports filament "
                            "present; NFC already has spool=%s — "
                            "suspending scan-jog",
                            self._name, self._gate,
                            self._state.current_spool)
                    self._state.miss_count = 0
                    return self.reactor.monotonic() + self._poll_interval
                if curr <= 0:
                    self._scan_pending = False
                    nfc_spool = self._state.current_spool
                    if hh.assigned and nfc_spool == hh.spool:
                        if not self._hh_load_paused:
                            self._hh_load_paused = True
                            logger.info(
                                "[%s]: gate %d — Happy Hare has assigned "
                                "spool=%d; suspending NFC poll",
                                self._name, self._gate, hh.spool)
                        self._state.miss_count = 0
                        return self.reactor.monotonic() + self._poll_interval
                    if self._hh_load_paused:
                        self._hh_load_paused      = False
                        self._state.current_uid   = None
                        self._state.current_spool = None
                        self._state.miss_count    = 0
                        self._hh_confirmed_spool  = None
                        logger.info(
                            "[%s]: gate %d — gate ejected; "
                            "resuming poll and clearing NFC cache",
                            self._name, self._gate)
                        return self.reactor.monotonic() + 1.0
                    return self.reactor.monotonic() + self._poll_interval
                # 0→1 edge: arm pending flag and let Happy Hare fully settle.
                # Do not treat -1→1 as a preload; that is unknown-state recovery.
                if prev == hh_status.GATE_EMPTY and curr >= 1:
                    self._scan_pending = True
                    self._scan_deferred_notified = False
                    self._scan_idle_ready_time = 0.0
                    if self._debug >= 3:
                        logger.info(
                            "[%s]: gate %d — gate loaded; "
                            "waiting for Happy Hare scan-safe state before scan",
                            self._name, self._gate)
                # Fire scan once Happy Hare is scan-safe and gate is confirmed
                # loaded. Happy Hare v4 wraps post-preload hooks in action=checking.
                if (getattr(self, '_scan_pending', False) and curr >= 1
                        and self._happy_hare_allows_scan_action(hh.action)
                        and not self._is_printing()):
                    now = self.reactor.monotonic()
                    if self._scan_idle_ready_time <= 0.0:
                        self._scan_idle_ready_time = now + 0.1
                        if self._debug >= 3:
                            logger.info(
                                "[%s]: gate %d — Happy Hare scan-safe; "
                                "waiting 0.1s before scan-jog",
                                self._name, self._gate)
                        return self._scan_idle_ready_time
                    if now < self._scan_idle_ready_time:
                        return self._scan_idle_ready_time
                    self._scan_pending = False
                    self._scan_idle_ready_time = 0.0
                    if NFCGate._active_scan_gate is not None:
                        if not self._scan_deferred_notified:
                            msg = ("[SCAN] NFC[%d]: scan-jog waiting — "
                                   "gate %d is already scanning"
                                   % (self._gate, NFCGate._active_scan_gate))
                            logger.info("[%s]: %s", self._name, msg)
                            self._console(msg)
                            self._scan_deferred_notified = True
                        self._scan_pending = True  # re-arm; retry after active scan has time to progress
                        self._scan_idle_ready_time = now + 3.0
                        return self._scan_idle_ready_time
                    ok, reason, max_mm = self._prepare_scan_jog(eventtime)
                    if not ok:
                        msg = "NFC[%d]: scan-jog not available while %s" % (
                            self._gate, reason)
                        logger.warning("[%s]: %s", self._name, msg)
                        self._console("[WARN] " + msg)
                        return self.reactor.monotonic() + self._poll_interval
                    msg = ("[SCAN] NFC[%d]: starting scan-jog "
                           "(max=%.0fmm  poll=%.2fs)"
                           % (self._gate, max_mm, self._scan_poll_interval))
                    logger.warning("[%s]: %s", self._name, msg)
                    self._console(msg)
                    self._start_scan_mode(max_mm=max_mm)
                    return self.reactor.NEVER
                if getattr(self, '_scan_pending', False):
                    return self.reactor.monotonic() + .25

        if self._debug >= 4:
            logger.debug("[%s]: poll cycle start — "
                         "current state: uid=%s spool=%s misses=%d",
                         self._name,
                         self._state.current_uid or 'none',
                         self._state.current_spool
                         if self._state.current_spool is not None else 'none',
                         self._state.miss_count)
        try:
            self._poll()
        except Exception:
            logger.exception("[%s]: poll error", self._name)
        next_interval = (self._scan_poll_interval
                         if self._shared else self._poll_interval)
        if self._debug >= 4:
            logger.debug("[%s]: poll cycle done — "
                         "next poll in %.2fs", self._name, next_interval)
        return self.reactor.monotonic() + next_interval

    def _read_current_tag(self):
        return tag_handler.read_current_tag(self)

    def _resolve_spool(self, uid_hex):
        return tag_handler.resolve_spool(self, uid_hex)

    def _check_hh_cleared(self):
        """Reset lane cache if Happy Hare cleared this gate from outside the NFC system.

        Only active after Happy Hare has confirmed the spool at least once (_hh_confirmed_spool
        is set when Happy Hare's gate_spool_id matches what NFC dispatched).  This prevents a
        loop where NFC dispatches spool 49, Happy Hare hasn't processed it yet, the check sees
        Happy Hare=-1, clears the cache, NFC dispatches again next poll, and so on forever.
        """
        if self._state.current_spool is None:
            return  # Lane cache already empty — nothing to cross-check
        if self._hh_confirmed_spool != self._state.current_spool:
            return  # Happy Hare hasn't acknowledged this spool yet — don't second-guess it
        hh = self._read_hh_status()
        if not hh.present:
            return
        nfc_spool = self._state.current_spool
        hh_differs = (not hh.assigned) or (hh.spool != nfc_spool)
        if hh_differs:
            if not hh.assigned:
                reason = "Happy Hare cleared gate externally (NFC cache had spool=%d)" % nfc_spool
            else:
                reason = ("Happy Hare has spool=%d but NFC cache has spool=%d "
                          "(manual gate map change?)" % (hh.spool, nfc_spool))
            logger.info(
                "[%s]: gate %d — %s; resetting lane cache so "
                "next tag read re-dispatches _NFC_SPOOL_CHANGED",
                self._name, self._gate, reason)
            self._state.current_uid   = None
            self._state.current_spool = None
            self._state.miss_count    = 0
            self._hh_confirmed_spool  = None

    def _hh_gate_matches_current_spool(self):
        """Return True when Happy Hare already owns this gate's current spool.

        Happy Hare may report a gate as merely assigned (gate_spool_id > 0,
        gate_status == 0) or available/loaded (gate_status >= 1).  Once NFC has
        read and cached that same spool, either state is enough to stop NFC
        polling until Happy Hare clears the assignment.
        """
        nfc_spool = self._state.current_spool
        if nfc_spool is None:
            return False
        hh = self._read_hh_status()
        return hh.present and hh.spool == nfc_spool

    def _poll(self):
        if self._poll_hh_pause_check():
            return
        self._check_hh_cleared()
        uid_hex  = self._read_current_tag()
        new_shared_uid = uid_hex is not None and uid_hex != self._state.current_uid
        if (self._shared and new_shared_uid
                and self._shared_missed_resolutions == 0):
            if self._shared_tag_read_effect or self._shared_bypass_tag_read_effect:
                if self._shared_bypass_selected() and self._shared_bypass_tag_read_effect:
                    self._shared_play_tag_read_effect(
                        effect_name=self._shared_bypass_tag_read_effect)
                else:
                    self._shared_play_tag_read_effect()
            if self._debug >= 2:
                logger.info(
                    "[%s]: tag read uid=%s — resolving...",
                    self._name, uid_hex)
        spool_id = self._resolve_spool(uid_hex)
        event    = self._state.process_read(uid_hex, spool_id,
                                            scan_mode=self._scan_mode)
        self._poll_debug_trace(uid_hex, event)
        if event is not None:
            self._poll_dispatch_event(event)
        elif (self._shared and uid_hex is not None
                and self._state.current_spool is None):
            if self._shared_missed_resolutions < self._shared_missed_limit:
                self._shared_missed_resolutions += 1
                if (self._shared_tag_unresolved_effect
                        and self._shared_missed_resolutions == 1):
                    self._shared_play_tag_unresolved_effect()
                if self._shared_missed_resolutions == 1 and self._debug >= 2:
                    logger.warning(
                        "[%s]: uid=%s not in Spoolman",
                        self._name, uid_hex)
                if self._shared_missed_resolutions == self._shared_missed_limit:
                    self._shared_unresolved_limit_reached(uid_hex)
        return uid_hex is not None

    def _shared_bypass_selected(self):
        """Return True when Happy Hare currently has bypass selected."""
        mmu = self.printer.lookup_object('mmu', None)
        if mmu is None:
            return False
        try:
            status = mmu.get_status(self.reactor.monotonic())
        except Exception:
            status = {}
        tool = status.get('tool', getattr(mmu, 'tool', -1))
        try:
            return int(tool) == -2
        except (TypeError, ValueError):
            return False

    def _shared_apply_bypass_spool(self, spool, uid, auto_created=False):
        if not self._shared_bypass_selected():
            return False

        script = "_NFC_SHARED_BYPASS_SPOOL_CHANGED SPOOL_ID=%d UID=%s" % (
            spool, uid or "")
        self.reactor.register_async_callback(
            lambda et, _s=script: self._safe_run_script(_s))
        self._shared_pending_uid            = None
        self._shared_pending_spool          = None
        self._shared_pending_deadline       = 0.0
        self._shared_pending_warning_fired  = False
        self._shared_pending_auto_created   = False
        self._shared_last_error             = None
        self._shared_read_deadline          = 0.0
        self._shared_missed_resolutions     = 0
        self._shared_clear_preload_approval()
        if self._shared_bypass_spool_ready_effect:
            self._shared_play_led_effect(
                self._shared_bypass_spool_ready_effect,
                event=EVENT_SPOOL_READY,
                duration=self._shared_bypass_ready_effect_duration)
        elif self._shared_spool_ready_effect:
            self._shared_play_led_effect(
                self._shared_spool_ready_effect, event=EVENT_SPOOL_READY,
                duration=self._shared_bypass_ready_effect_duration)
        self._shared_last_action = (
            "bypass active spool set to %d uid=%s auto_created=%s"
            % (spool, uid, auto_created))
        logger.info(
            "[%s]: shared tag resolved while bypass selected — "
            "setting active spool=%d uid=%s auto_created=%s",
            self._name, spool, uid, auto_created)
        self._console("[OK] NFC[shared]: bypass active spool set to %d (UID %s)"
                      % (spool, uid or ""))
        return True

    def _shared_stage_next_spool_id(self, spool, auto_created=False):
        script_lines = []
        if auto_created:
            script_lines.append("MMU_SPOOLMAN REFRESH=1 QUIET=1")
        script_lines.append("MMU_GATE_MAP NEXT_SPOOLID=%d QUIET=1" % spool)
        script = "\n".join(script_lines)
        self.reactor.register_async_callback(
            lambda et, _s=script: self._safe_run_script(_s))

    def _poll_hh_pause_check(self):
        """Suspend polling while Happy Hare says filament is still present."""
        if not self._scan_mode:
            hh = self._read_hh_status()
            if hh.present and hh.available:
                if not self._hh_load_paused:
                    self._hh_load_paused = True
                    logger.info(
                        "[%s]: gate %d — Happy Hare reports filament "
                        "present (status=%s spool=%s); suspending NFC poll "
                        "until ejected",
                        self._name, self._gate, hh.status, hh.spool)
                self._state.miss_count = 0
                return True
        if (not self._scan_mode
                and self._hh_gate_matches_current_spool()
                and self._state.current_spool is not None):
            if not self._hh_load_paused:
                self._hh_load_paused = True
                logger.info(
                    "[%s]: gate %d — spool confirmed by NFC; "
                    "Happy Hare owns same spool — suspending poll until ejected",
                    self._name, self._gate)
            self._state.miss_count = 0
            return True
        if self._hh_load_paused:
            if self._state.current_spool is None:
                self._hh_load_paused = False
                return False
            hh = self._read_hh_status()
            if hh.present and hh.available:
                self._state.miss_count = 0
                if self._debug >= 3:
                    logger.info(
                        "[%s]: gate %d — Happy Hare still reports filament "
                        "present (status=%s spool=%s); keeping NFC spool=%s",
                        self._name, self._gate, hh.status, hh.spool,
                        self._state.current_spool)
                return True
            self._hh_load_paused      = False
            self._state.current_uid   = None
            self._state.current_spool = None
            self._state.miss_count    = 0
            self._hh_confirmed_spool  = None
            logger.info(
                "[%s]: gate %d — filament unloaded; resuming NFC scan",
                self._name, self._gate)
        return False

    def _poll_debug_trace(self, uid_hex, event):
        if self._debug < 4:
            return
        if uid_hex is not None:
            read_str = "tag=%-16s" % uid_hex
        else:
            read_str = "no tag  miss=%d/%d" % (
                self._state.miss_count, self._state.absent_threshold)
        if event is None:
            if uid_hex is not None:
                action_str = "quiet  (spool=%s, uid unchanged)" % (
                    self._state.current_spool,)
            else:
                action_str = "quiet  (waiting, %d more miss(es) until removal)" % (
                    max(0, self._state.absent_threshold - self._state.miss_count),)
        else:
            etype = event[0]
            if etype == EVENT_CHANGED:
                action_str = "CHANGED  →  spool=%s  uid=%s" % (event[3], event[2])
            elif etype == EVENT_REMOVED:
                action_str = "REMOVED  (tag absent for %d consecutive polls)" % (
                    self._state.absent_threshold,)
            elif etype == EVENT_UID_ONLY:
                if self._spoolman is None:
                    action_str = "NO_SPOOL  (uid=%s no metadata/spool assignment)" % (
                        event[2],)
                else:
                    action_str = "NO_SPOOL  (uid=%s not registered in Spoolman)" % (
                        event[2],)
            else:
                action_str = str(etype)
        logger.debug("[%s]: POLL  gate=%-2d  %-28s  →  %s",
                     self._name, self._gate, read_str, action_str)

    def _poll_dispatch_event(self, event):
        event_type, gate, uid, spool = event
        if self._debug >= 3:
            logger.info("[%s]: gate %d — %s uid=%s spool=%s",
                        self._name, gate, event_type, uid, spool)

        if self._shared:
            self._shared_handle_event(event_type, uid, spool)
            return

        suppress = (self._hh_seed_spool_id is not None
                    and event_type == EVENT_CHANGED
                    and spool == self._hh_seed_spool_id
                    and self._hh_seed_available)
        self._hh_seed_spool_id  = None  # one-shot, always clear
        self._hh_seed_available = False

        if self._is_printing():
            if self._debug >= 3:
                logger.info(
                    "[%s]: gate %d — %s detected during print; "
                    "Spoolman and Happy Hare dispatch suppressed",
                    self._name, gate, event_type)
        elif self._scan_mode:
            meta = None
            if (event_type == EVENT_CHANGED
                    and self._state.current_spool is DIRECT_METADATA_SPOOL
                    and self._state.current_tag is not None):
                meta = self._state.current_tag.meta
            self._scan_found_event = (event_type, gate, uid, spool, meta)
            if self._debug >= 3:
                logger.info(
                    "[%s]: gate %d — %s detected during scan-jog; "
                    "dispatch deferred until rewind complete",
                    self._name, gate, event_type)
        else:
            if suppress:
                if self._debug >= 3:
                    logger.info(
                        "[%s]: gate %d — startup seed match "
                        "spool=%s; skipping Happy Hare dispatch",
                        self._name, gate, spool)
            else:
                self._poll_klipper_dispatch(event_type, gate, uid, spool)

    def _poll_klipper_dispatch(self, event_type, gate, uid, spool,
                               scan_finish=False):
        meta = None
        auto_created = False
        if event_type == EVENT_CHANGED and self._state.current_tag is not None:
            res = self._state.current_tag.resolution or {}
            auto_created = isinstance(res, dict) and res.get('path') == 'auto_create'
            if self._state.current_spool is DIRECT_METADATA_SPOOL:
                meta = self._state.current_tag.meta
        self._klipper.dispatch(event_type, gate, uid, spool,
                               meta=meta, auto_created=auto_created,
                               scan_finish=scan_finish)
        if event_type == EVENT_CHANGED and spool is not None:
            self._hh_confirmed_spool = spool
        elif event_type == EVENT_REMOVED:
            self._hh_confirmed_spool = None

    # ── Scan-and-jog mode ────────────────────────────────────────────────────

    def _manual_jog_scan(self, gcmd):
        return scan_jog.manual_jog_scan(self, gcmd)

    def _all_lanes_parked_or_empty(self, eventtime=None):
        status = hh_status.read_full(
            self.printer,
            eventtime if eventtime is not None else self.reactor.monotonic())
        if not status.present:
            return False, "Happy Hare status unavailable"

        if status.filament_pos != hh_status.FILAMENT_POS_UNLOADED:
            if status.active_gate >= 0 and status.action:
                return False, "lane %d is %s; filament is not parked (filament_pos=%d)" % (
                    status.active_gate, status.action, status.filament_pos)
            return False, "filament is not parked (filament_pos=%d)" % (
                status.filament_pos,)

        if not status.gate_statuses:
            return False, "Happy Hare gate status unavailable"

        for lane, gate_state in enumerate(status.gate_statuses):
            safe = gate_state in (hh_status.GATE_EMPTY,
                                  hh_status.GATE_AVAILABLE,
                                  hh_status.GATE_INBUFFER)
            if self._debug >= 3:
                logger.info(
                    "[%s]: scan preflight — lane %d gate_status=%d %s",
                    self._name, lane, gate_state,
                    "safe" if safe else "not safe")
            if not safe:
                return False, "lane %d is not parked or empty (status=%d)" % (
                    lane, gate_state)

        return True, None

    def _expand_mmu_vars_path(self, path):
        path = os.path.expanduser(str(path).strip())
        if os.path.isabs(path):
            return path
        return os.path.abspath(os.path.join(
            os.path.expanduser('~/printer_data/config'), path))

    def _read_mmu_pending_timeout(self, default=30.0):
        try:
            configfile = self.printer.lookup_object('configfile', None)
            if configfile is not None:
                raw_config = configfile.get_status(0).get('config', {})
                val = raw_config.get('mmu', {}).get('pending_spool_id_timeout', None)
                if val is not None:
                    timeout = float(val)
                    if timeout >= 1.0:
                        return timeout
        except Exception:
            logger.warning(
                "[%s]: could not read pending_spool_id_timeout from [mmu] config; "
                "using %.0fs default", self._name, default)
        return default

    def _resolve_mmu_vars_path(self):
        cached = getattr(self, '_mmu_vars_path', None)
        if cached:
            return cached

        configfile = self.printer.lookup_object('configfile', None)
        if configfile is not None and hasattr(configfile, 'get_status'):
            try:
                raw_config = configfile.get_status(0).get('config', {})
                save_vars = raw_config.get('save_variables', {})
                filename = save_vars.get('filename', None)
                if filename:
                    self._mmu_vars_path = self._expand_mmu_vars_path(filename)
                    return self._mmu_vars_path
            except Exception:
                logger.exception(
                    "[%s]: could not read [save_variables] filename",
                    self._name)

        fallback = '~/printer_data/config/mmu/mmu_vars.cfg'
        self._mmu_vars_path = self._expand_mmu_vars_path(fallback)
        return self._mmu_vars_path

    def _load_bowden_lengths(self):
        path = self._resolve_mmu_vars_path()
        if not path or not os.path.exists(path):
            return None

        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if not line.startswith('mmu_calibration_bowden_lengths'):
                        continue
                    parts = line.split('=', 1)
                    if len(parts) != 2:
                        return None
                    values = ast.literal_eval(parts[1].strip())
                    if not isinstance(values, (list, tuple)):
                        return None
                    lengths = []
                    for value in values:
                        length = float(value)
                        if length <= 0.0:
                            return None
                        lengths.append(length)
                    self._bowden_lengths = lengths
                    return lengths
        except Exception:
            logger.exception(
                "[%s]: could not read Bowden lengths from %s",
                self._name, path)
            return None

        return None

    def _get_lane_scan_max_mm(self):
        if self._scan_jog_max is not None:
            return float(self._scan_jog_max)
        lengths = self._load_bowden_lengths()
        if lengths is None:
            return None
        if self._gate < 0 or self._gate >= len(lengths):
            return None
        return float(lengths[self._gate])

    def _prepare_scan_jog(self, eventtime=None):
        ok, reason = self._all_lanes_parked_or_empty(eventtime)
        if not ok:
            return False, reason, None
        max_mm = self._get_lane_scan_max_mm()
        if max_mm is None:
            return False, "missing Bowden calibration length for gate %d" % self._gate, None
        return True, None, max_mm

    def _is_printing(self):
        return scan_jog.is_printing(self)

    def _get_scan_speed(self):
        return scan_jog.get_speed(self)

    def _scan_chunk_interval(self, mm):
        return scan_jog.chunk_interval(self, mm)

    def _scan_next_event_time(self, mm):
        return scan_jog.next_event_time(self, mm)

    def _resume_poll_after_rewind(self):
        return scan_jog.resume_poll_after_rewind(self)

    def _start_scan_mode(self, max_mm=None):
        return scan_jog.start(self, max_mm)

    def _scan_step_event(self, eventtime):
        return scan_jog.step_event(self, eventtime)

    def _finish_scan(self):
        return scan_jog.finish(self)

    def _rewind_and_exit_scan(self):
        return scan_jog.rewind_and_exit(self)

    def _console(self, msg):
        return scan_jog.console(self, msg)

    def _run_jog(self, mm):
        return scan_jog.run_jog(self, mm)

    def _run_rewind(self):
        return scan_jog.run_rewind(self)

    def _nfc_gate_for_gate_number(self, gate_number):
        return nfc_gate_for_gate_number(gate_number)

    def status_line(self):
        label = ("shared" if self._shared
                 else "Gate %d" % self._gate)
        label = "%s (%s)" % (label, self._reader_type)
        if not getattr(self, '_enabled', True):
            return "  %s  [%s]:  disabled by config" % (label, self._name)
        if self._failed:
            return ("  %s  [%s]:  READER FAILED (check wiring, address 0x24)"
                    % (label, self._name))
        if self._hh_load_paused:
            poll_state = "polling suspended"
        elif self._polling:
            poll_state = "polling"
        else:
            poll_state = "not polling"
        hh = self._read_hh_status()
        if hh.present and hh.available and not self._scan_mode:
            poll_state = "polling suspended"
        hh_label = hh.label()
        sync_note = ''
        nfc_spool = self._state.current_spool
        hh_empty = (hh.present and not hh.available
                    and not (hh.active_gate == self._gate
                             and hh.filament_pos > 0))
        if (hh.present and hh.assigned and nfc_spool is not None
                and nfc_spool is not DIRECT_METADATA_SPOOL
                and hh.spool != nfc_spool):
            sync_note = "  [SYNC MISMATCH: NFC spool %s, Happy Hare spool %s]" % (
                nfc_spool, hh.spool)
        elif (hh.present and hh.assigned and nfc_spool is None):
            hh_label = hh_label + ", NFC cache empty"
        elif (hh.present and not hh.assigned and nfc_spool is not None
                and nfc_spool is not DIRECT_METADATA_SPOOL):
            if hh.available:
                sync_note = "  [NFC has spool %s; Happy Hare found/no spool]" % nfc_spool
            else:
                sync_note = "  [NFC has spool %s; Happy Hare empty]" % nfc_spool
        if hh_empty:
            return _status_html_words(
                "  %s:  empty   [%s]%s  [%s]"
                % (label, poll_state, sync_note, hh_label))
        if self._state.current_spool is DIRECT_METADATA_SPOOL:
            tag = self._state.current_tag
            meta = tag.meta if tag is not None else {}
            material = (meta or {}).get('material', '')
            color = (meta or {}).get('color_hex', '')
            return _status_html_words(
                "  %s:  tag %s  metadata material=%s color=%s   [%s]%s  [%s]"
                % (label, self._state.current_uid,
                   material, color, poll_state, sync_note, hh_label))
        if self._state.current_spool is not None:
            return _status_html_words(
                "  %s:  spool %-2d  UID %s   [%s]%s   [%s]"
                % (label,
                   self._state.current_spool, self._state.current_uid,
                   poll_state, sync_note, hh_label))
        if self._state.current_uid is not None:
            return _status_html_words(
                "  %s:  tag %s  (UID not in Spoolman)   [%s]%s  [%s]"
                % (label, self._state.current_uid, poll_state,
                   sync_note, hh_label))
        if hh.present and hh.available:
            return _status_html_words(
                "  %s:  occupied   [%s]%s  [%s]"
                % (label, poll_state, sync_note, hh_label))
        return _status_html_words(
            "  %s:  empty   [%s]%s  [%s]"
            % (label, poll_state, sync_note, hh_label))

    # ── Shared reader ────────────────────────────────────────────────────────

    def _shared_handle_event(self, event_type, uid, spool):
        if event_type == EVENT_CHANGED and spool is DIRECT_METADATA_SPOOL:
            # Rich tag without a Spoolman spool ID — NEXT_SPOOLID requires an
            # integer.  Treat as unresolved unless spoolman_auto_create creates
            # a spool first (auto_create returns a real ID, not this sentinel).
            self._shared_last_error = (
                "rich tag has no Spoolman spool ID — "
                "enable spoolman_auto_create to create one automatically")
            if self._shared_missed_resolutions < self._shared_missed_limit:
                self._shared_missed_resolutions += 1
                logger.info(
                    "[%s]: shared rich tag uid=%s — no Spoolman spool ID; "
                    "enable spoolman_auto_create or register the spool manually "
                    "(attempt %d/%d)",
                    self._name, uid,
                    self._shared_missed_resolutions, self._shared_missed_limit)
                if (self._shared_tag_unresolved_effect
                        and self._shared_missed_resolutions == 1):
                    self._shared_play_tag_unresolved_effect()
                if self._shared_missed_resolutions == self._shared_missed_limit:
                    self._shared_unresolved_limit_reached(uid)
            return

        if event_type == EVENT_CHANGED and spool is not None:
            self._shared_expire_pending_if_needed()
            if self._shared_pending_spool is not None:
                pending_spool = self._shared_pending_spool
                if pending_spool == spool:
                    logger.info(
                        "[%s]: shared duplicate tag ignored — "
                        "spool=%d uid=%s",
                        self._name, spool, uid)
                    self._shared_last_action = (
                        "ignored duplicate read for pending spool %d" % spool)
                else:
                    logger.warning(
                        "[%s]: spool %d already pending; new spool=%d "
                        "uid=%s ignored — use NFC_SHARED REPLACE=1 to replace",
                        self._name, pending_spool, spool, uid)
                    self._shared_last_action = (
                        "ignored spool %d while spool %d pending"
                        % (spool, pending_spool))
                return
            auto_created = False
            if self._state.current_tag is not None:
                res = self._state.current_tag.resolution or {}
                auto_created = isinstance(res, dict) and res.get('path') == 'auto_create'
            if self._shared_apply_bypass_spool(spool, uid, auto_created):
                return
            now = self.reactor.monotonic()
            self._shared_pending_uid            = uid
            self._shared_pending_spool          = spool
            self._shared_pending_deadline       = now + self._shared_pending_timeout
            self._shared_pending_warning_fired  = False
            self._shared_pending_auto_created   = auto_created
            self._shared_last_error             = None
            self._shared_read_deadline          = 0.0
            self._shared_missed_resolutions     = 0
            self._shared_stage_next_spool_id(spool, auto_created)
            # Stop polling — pending spool survives tag removal.
            self._polling = False
            self.reactor.update_timer(
                self._poll_timer, now + 0.8 * self._shared_pending_timeout)
            # Warning timer fires at 80% of the pending timeout.
            self.reactor.update_timer(
                self._warning_timer,
                now + 0.8 * self._shared_pending_timeout)
            logger.info(
                "[%s]: shared tag resolved — spool=%d uid=%s "
                "auto_created=%s pending for %.0fs",
                self._name, spool, uid, auto_created,
                self._shared_pending_timeout)
            if self._debug >= 3:
                logger.info(
                    "[%s]: shared CHANGED — spool=%d uid=%s "
                    "auto_created=%s; polling stopped, awaiting PRELOAD_CHECK",
                    self._name, spool, uid, auto_created)
            _ac_note = " [new spool]" if auto_created else ""
            logger.info(
                "[%s]: spool %d detected (UID %s)%s — "
                "load spool into gate now",
                self._name, spool, uid, _ac_note)
            if self._gcode is not None:
                self._gcode.respond_info(color_console_tags(
                    "[OK] NFC[%s]: read tag — spool %d staged%s"
                    % (self._name, spool, _ac_note)))
            if self._shared_spool_ready_effect:
                self._shared_play_spool_ready_effect()
            self._shared_last_action = (
                "tag staged spool %d uid=%s auto_created=%s"
                % (spool, uid, auto_created))

        elif event_type == EVENT_UID_ONLY:
            if self._shared_pending_spool is None:
                self._shared_last_error = "tag uid=%s not in Spoolman" % uid
                if self._shared_missed_resolutions < self._shared_missed_limit:
                    self._shared_missed_resolutions += 1
                    logger.info(
                        "[%s]: shared UID-only — %s (attempt %d/%d)",
                        self._name, self._shared_last_error,
                        self._shared_missed_resolutions,
                        self._shared_missed_limit)
                    if (self._shared_tag_unresolved_effect
                            and self._shared_missed_resolutions == 1):
                        self._shared_play_tag_unresolved_effect()
                    if self._shared_missed_resolutions == 1 and self._debug >= 2:
                        logger.warning(
                            "[%s]: uid=%s not in Spoolman",
                            self._name, uid)
                    if self._shared_missed_resolutions == self._shared_missed_limit:
                        self._shared_unresolved_limit_reached(uid)
                tag  = self._state.current_tag
                meta = (tag.meta
                        if tag is not None and isinstance(tag.meta, dict)
                        else {})
                if not any(k not in ('uid',) for k in meta):
                    self._state.current_uid   = None
                    self._state.current_spool = None
            elif self._debug >= 3:
                logger.info(
                    "[%s]: shared UID-only ignored — pending "
                    "spool=%s uid=%s kept; new uid=%s unresolved",
                    self._name, self._shared_pending_spool,
                    self._shared_pending_uid, uid)

        elif event_type == EVENT_REMOVED:
            # Restore Happy Hare only when no spool is staged.
            # If a spool is pending, the ready/warning effect must stay visible.
            if self._shared_pending_spool is None:
                self._shared_restore_hh_leds()
            self._shared_missed_resolutions = 0
            if self._debug >= 3:
                logger.info(
                    "[%s]: shared tag removed — "
                    "pending spool=%s kept",
                    self._name, self._shared_pending_spool)

    def _shared_unresolved_limit_reached(self, uid):
        self._shared_missed_resolutions = 0
        self._state.current_uid   = None
        self._state.current_spool = None
        self._shared_read_deadline = 0.0
        logger.error(
            "[%s]: uid=%s not in Spoolman after %d attempts",
            self._name, uid, self._shared_missed_limit)
        logger.info(
            "[%s]: reader ready for next tag",
            self._name)

    def _shared_expire_pending_if_needed(self):
        if (self._shared_pending_spool is not None
                and self.reactor.monotonic() >= self._shared_pending_deadline):
            spool_id = self._shared_pending_spool
            logger.info(
                "[%s]: shared pending spool=%d timed out after %.0fs",
                self._name, spool_id, self._shared_pending_timeout)
            self._shared_clear_pending()
            self._shared_last_error = (
                "pending spool %d expired; tap tag again" % spool_id)
            self._shared_last_action = "pending spool %d expired" % spool_id
            return True
        return False

    def _shared_clear_pending(self):
        if self._debug >= 4:
            logger.debug(
                "[%s]: shared pending cleared "
                "(was spool=%s uid=%s)",
                self._name,
                self._shared_pending_spool,
                self._shared_pending_uid)
        self.reactor.update_timer(self._warning_timer, self.reactor.NEVER)
        self._shared_restore_hh_leds()
        self._shared_pending_uid            = None
        self._shared_pending_spool          = None
        self._shared_pending_deadline       = 0.0
        self._shared_pending_warning_fired  = False
        self._shared_pending_auto_created   = False
        self._shared_missed_resolutions     = 0
        self._shared_clear_preload_approval()

    def _shared_clear_preload_approval(self):
        self._shared_preload_spool        = None
        self._shared_preload_uid          = None
        self._shared_preload_auto_created = False

    def _shared_resume_startup_polling(self):
        if (self._startup_polling == 1 and not self._failed
                and not self._is_printing()
                and self._shared_pending_spool is None):
            self._shared_read_deadline = 0.0
            self._polling = True
            self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
            return True
        return False

    def _shared_expire_pending_and_maybe_resume(self):
        if self._shared_expire_pending_if_needed():
            # Always restart polling after timeout (equivalent to
            # NFC_SHARED REPLACE=1) so the user can tap a new tag
            # immediately without a manual command.
            polling_resumed = not self._failed and not self._is_printing()
            if polling_resumed:
                self._shared_missed_resolutions = 0
                self._shared_last_error = None
                self._shared_read_deadline = (
                    self.reactor.monotonic() + self._shared_read_timeout)
                self._polling = True
                self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
                logger.info(
                    "[%s]: shared pending timeout — "
                    "polling restarted (NFC_SHARED REPLACE=1 behavior)",
                    self._name)
            else:
                logger.info(
                    "[%s]: shared pending timeout — "
                    "polling not resumed (failed=%s printing=%s)",
                    self._name, self._failed, self._is_printing())
            resume_note = " Reader polling resumed." if polling_resumed else ""
            hook_hint = self._shared_preload_hook_hint()
            logger.error(
                "[ERROR] NFC[%s]: timeout after %.0fs — no spool was "
                "loaded.%s%s Tap tag to stage again.",
                self._name, self._shared_pending_timeout,
                resume_note, hook_hint)
            return True
        return False

    def _safe_run_script(self, script):
        try:
            self._gcode.run_script(script)
            return True
        except Exception as e:
            logger.debug(
                "[%s]: deferred run_script failed: %s",
                self._name, e)
            return False

    def _shared_preload_check(self, gcmd):
        self._shared_preload_policy().check(gcmd)

    def _shared_preload_commit(self, gcmd):
        self._shared_preload_policy().commit(gcmd)

    def _shared_preload_clear_assigned(self, gcmd):
        self._shared_preload_policy().clear_assigned(gcmd)

    def _shared_preload_policy(self):
        coordinator = getattr(self, '_shared_preload_coordinator', None)
        if coordinator is None:
            coordinator = shared_preload.SharedPreloadCoordinator(self)
            self._shared_preload_coordinator = coordinator
        return coordinator

    def _shared_state_text(self):
        if not getattr(self, '_enabled', True):
            return "disabled by config"
        now = self.reactor.monotonic()
        if self._failed:
            return "READER FAILED (check wiring)"
        if self._shared_pending_spool is not None:
            remaining = max(0.0, self._shared_pending_deadline - now)
            if remaining <= 0.0:
                spool_id = self._shared_pending_spool
                uid = self._shared_pending_uid or ''
                self._shared_expire_pending_and_maybe_resume()
                return "expired  spool %d  uid=%s" % (spool_id, uid)
            return ("pending spool %d  uid=%s  expires in %.0fs"
                    % (self._shared_pending_spool,
                       self._shared_pending_uid or '',
                       remaining))
        if self._shared_last_error:
            return "error  %s" % self._shared_last_error
        if self._polling:
            return "polling, no tag pending"
        return "idle"

    def shared_status_line(self):
        return "  shared (%s):  %s" % (
            self._reader_type, self._shared_state_text())

    def shared_summary_line(self):
        return "[%s]: %s  next: %s" % (
            self._name, self._shared_state_text(), self._shared_next_action())

    def _shared_next_action(self):
        if not getattr(self, '_enabled', True):
            return "set enabled: True and restart Klipper"
        if self._failed:
            return "run NFC_SHARED INIT=1 after fixing wiring"
        if self._is_printing():
            return "wait for printing to finish; shared reads are blocked"
        if self._shared_pending_spool is not None:
            return "insert filament before timeout, or run NFC_SHARED REPLACE=1"
        if self._shared_last_error:
            last_action = self._shared_last_action or ''
            if "expired" in self._shared_last_error:
                return "tap the tag again"
            if "not in Spoolman" in self._shared_last_error:
                return "register the tag in Spoolman, or use MMU_PRELOAD"
            return "fix the reported issue, then trigger preload again"
        if self._polling:
            return "tap a spool tag"
        if self._startup_polling == 1:
            return "polling should resume automatically; run NFC_SHARED READ=1 if needed"
        return "run NFC_SHARED READ=1 to scan a spool"

    def shared_summary_line(self):
        return "%s  next: %s" % (
            self.shared_status_line().strip(), self._shared_next_action())

    def shared_status_detail(self):
        now = self.reactor.monotonic()
        lines = [self.shared_status_line()]
        if self._failed:
            lines.append("    recovery: run NFC_SHARED INIT=1 after fixing wiring")
        lines.append("    polling: %s" % ("on" if self._polling else "off"))
        lines.append("    startup_polling: %s" %
                     ("on" if self._startup_polling == 1 else "off"))
        lines.append("    read_deadline: %s" %
                     ("none" if self._shared_read_deadline <= 0.0
                      else "in %.0fs" % max(0.0, self._shared_read_deadline - now)))
        lines.append("    pending_spool: %s" %
                     (self._shared_pending_spool
                      if self._shared_pending_spool is not None else "none"))
        lines.append("    pending_uid: %s" %
                     (self._shared_pending_uid or "none"))
        lines.append("    pending_auto_created: %s" %
                     ("yes" if self._shared_pending_auto_created else "no"))
        lines.append("    preload_spool: %s" %
                     (self._shared_preload_spool
                      if self._shared_preload_spool is not None else "none"))
        lines.append("    preload_auto_created: %s" %
                     ("yes" if self._shared_preload_auto_created else "no"))
        lines.append("    pending_timeout: %.0fs" % self._shared_pending_timeout)
        lines.append("    read_timeout: %.0fs" % self._shared_read_timeout)
        lines.append("    missed_resolutions: %d/%d" %
                     (self._shared_missed_resolutions,
                      self._shared_missed_limit))
        lines.append("    force_spool_id: %s" %
                     ("on" if self._shared_force_spool_id else "off"))
        lines.append("    tag_read_effect:    %s" %
                     (self._shared_tag_read_effect or "none"))
        lines.append("    spool_ready_effect: %s" %
                     (self._shared_spool_ready_effect or "none"))
        lines.append("    tag_unresolved_effect: %s" %
                     (self._shared_tag_unresolved_effect or "none"))
        lines.append("    auto_create_effect: %s" %
                     (self._shared_auto_create_effect or "none"))
        lines.append("    last_action: %s" %
                     (self._shared_last_action or "none"))
        lines.append("    next: %s" % self._shared_next_action())
        if self._is_printing():
            lines.append("    safety: printing; shared reads are blocked")
        if self._shared_last_error:
            lines.append("    last_error: %s" % self._shared_last_error)
        return "\n".join(lines)

    def _shared_replace_pending(self, gcmd):
        if self._failed:
            logger.error(
                "[%s]: shared REPLACE=1 refused — reader failed; "
                "run INIT=1 first",
                self._name)
            gcmd.respond_info(color_console_tags(
                "[WARN] NFC[%s]: reader failed; run INIT=1 first"
                % self._name))
            return
        if self._is_printing():
            logger.warning(
                "[%s]: shared REPLACE=1 refused — printing",
                self._name)
            gcmd.respond_info(
                "[WARN] NFC[%s]: shared polling not started while printing"
                % self._name)
            return
        pending_spool = self._shared_pending_spool
        if pending_spool is not None:
            self._shared_clear_pending()
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: discarded pending spool %s; polling restarted"
                % (self._name, pending_spool)))
        else:
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: no pending spool to replace; polling started"
                % self._name))
        self._shared_missed_resolutions = 0
        self._shared_last_error = None
        self._shared_last_action = "replacement scan started"
        self._shared_read_deadline = (
            self.reactor.monotonic() + self._shared_read_timeout)
        self._polling = True
        self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
        logger.info(
            "[%s]: shared REPLACE=1 — discarded spool=%s; "
            "polling restarted with %.0fs read timeout",
            self._name, pending_spool, self._shared_read_timeout)

    def _shared_reset_and_poll(self, gcmd):
        if self._failed:
            logger.error(
                "[%s]: shared RESET=1 refused — reader failed; "
                "run INIT=1 first",
                self._name)
            gcmd.respond_info(color_console_tags(
                "[WARN] NFC[%s]: reader failed; run INIT=1 first"
                % self._name))
            return
        if self._is_printing():
            logger.warning(
                "[%s]: shared RESET=1 refused — printing",
                self._name)
            gcmd.respond_info(
                "[WARN] NFC[%s]: shared reset not started while printing"
                % self._name)
            return
        pending_spool = self._shared_pending_spool
        self._shared_clear_pending()
        self._shared_clear_preload_approval()
        self._shared_missed_resolutions = 0
        self._shared_last_error = None
        self._shared_last_action = "shared reset; polling restarted"
        self._state.current_uid = None
        self._state.current_spool = None
        self._shared_read_deadline = (
            self.reactor.monotonic() + self._shared_read_timeout)
        self._polling = True
        self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
        logger.info(
            "[%s]: shared RESET=1 — cleared spool=%s; "
            "polling restarted with %.0fs read timeout",
            self._name, pending_spool, self._shared_read_timeout)
        gcmd.respond_info(color_console_tags(
            "NFC[%s]: shared reset; LEDs restored and polling restarted"
            % self._name))

    def cmd_NFC_SHARED(self, gcmd):
        if self._cmd_low_level_debug(gcmd):
            return
        read_value = gcmd.get("READ", None)
        if read_value is not None:
            self._set_reading(gcmd, gcmd.get_int("READ", minval=0, maxval=1) == 1)
            return
        if _flag_param(gcmd, 'STATUS'):
            gcmd.respond_info(color_console_tags(
                "NFC %s" % self.shared_status_detail()))
            return
        if _flag_param(gcmd, 'SUMMARY'):
            gcmd.respond_info(color_console_tags(
                "NFC %s" % self.shared_summary_line()))
            return
        if _flag_param(gcmd, 'HELP'):
            self._shared_help(gcmd)
            return
        if _flag_param(gcmd, 'REPLACE'):
            self._shared_replace_pending(gcmd)
            return
        if _flag_param(gcmd, 'RESET'):
            self._shared_reset_and_poll(gcmd)
            return
        if _flag_param(gcmd, 'CLEAR'):
            self._shared_clear_pending()
            self._shared_last_error = None
            self._shared_last_action = "shared state cleared"
            self._polling = False
            self._shared_read_deadline = 0.0
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
            self._state.current_uid   = None
            self._state.current_spool = None
            logger.info("[%s]: shared state cleared", self._name)
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: shared state cleared" % self._name))
            return
        if _flag_param(gcmd, 'PRELOAD_CHECK'):
            self._shared_preload_check(gcmd)
            return
        if _flag_param(gcmd, 'PRELOAD_COMMIT'):
            self._shared_preload_commit(gcmd)
            return
        if _flag_param(gcmd, 'PRELOAD_CLEAR_ASSIGNED'):
            self._shared_preload_clear_assigned(gcmd)
            return
        if _flag_param(gcmd, 'CANCEL'):
            self._shared_clear_pending()
            self._shared_last_error = None
            self._shared_last_action = "pending spool canceled"
            self._polling = False
            self._shared_read_deadline = 0.0
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
            logger.info("[%s]: pending spool canceled", self._name)
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: pending spool canceled" % self._name))
            return
        if _flag_param(gcmd, 'POLL'):
            if self._is_printing():
                logger.warning(
                    "[%s]: shared poll skipped while printing",
                    self._name)
                gcmd.respond_info(
                    "[WARN] NFC[%s]: shared poll skipped while printing" % self._name)
                return
            self._poll()
            logger.info(
                "[%s]: shared POLL=1 complete — %s",
                self._name, self.shared_status_line().strip())
            gcmd.respond_info(color_console_tags(
                "NFC[%s]: one poll complete; %s"
                % (self._name, self.shared_status_line().strip())))
            return
        if _flag_param(gcmd, 'SCAN'):
            self._manual_scan(gcmd)
            return
        if _flag_param(gcmd, 'INIT'):
            self._manual_init(gcmd)
            return
        if _flag_param(gcmd, 'LED_TEST'):
            self._shared_play_tag_read_effect(
                gcmd, duration=self._shared_read_effect_duration)
            return
        if _flag_param(gcmd, 'CLEAR_CACHE'):
            self._shared_clear_cache(gcmd)
            return
        self._shared_help(gcmd)

    def _shared_help(self, gcmd):
        lines = [
            "NFC_SHARED commands:",
            "  Add =1 to action flags; Klipper rejects bare forms like NFC_SHARED CANCEL.",
            "  NFC_SHARED READ=1          - start polling (rejected while printing)",
            "  NFC_SHARED READ=0          - stop polling (keeps pending spool)",
            "  NFC_SHARED STATUS=1        - show detailed shared reader state",
            "  NFC_SHARED SUMMARY=1       - show one-line shared reader state",
            "  NFC_SHARED HELP=1          - show this help",
            "  NFC_SHARED CANCEL=1        - cancel pending spool and stop polling",
            "  NFC_SHARED REPLACE=1       - discard pending spool and scan another",
            "  NFC_SHARED RESET=1         - clear shared state, restore LEDs, and poll",
            "  NFC_SHARED LED_TEST=1      - test configured shared tag-read LED effect",
            "",
            "Advanced shared-reader commands:",
            "  NFC_SHARED CLEAR=1         - clear pending state and stop polling",
            "  NFC_SHARED PRELOAD_CHECK=1 - Happy Hare hook command; approve NEXT_SPOOLID if valid",
            "  NFC_SHARED PRELOAD_COMMIT=1 SPOOL_ID=<n> - Happy Hare hook command; clear pending after NEXT_SPOOLID",
            "  NFC_SHARED PRELOAD_CLEAR_ASSIGNED=1 SPOOL_ID=<n> GATE=<n> - Happy Hare hook command; clear already-assigned shared spool",
            "  NFC_SHARED POLL=1          - run one full read/resolve cycle (skips printing)",
            "  NFC_SHARED SCAN=1          - raw hardware scan only (skips printing)",
            "  NFC_SHARED INIT=1          - re-run NFC Reader init; resumes startup polling if enabled",
            "  NFC_SHARED CLEAR_CACHE=1   - clear tag cache (keeps pending spool)",
        ]
        if self._low_level_debug and self._reader_type == 'rc522':
            lines.extend(rc522_driver.low_level_debug_help_lines("NFC_SHARED"))
        gcmd.respond_info('\n'.join(lines))

    def get_status(self, _eventtime=None):
        if not getattr(self, '_enabled', True):
            return {
                'gate':                self._gate,
                'enabled':             False,
                'reader_type':         self._reader_type,
                'tag_present':         False,
                'spool_id':            -1,
                'uid':                 '',
                'failed':              False,
                'resolution':          'disabled',
                'pending_spool_id':    -1,
                'pending_auto_created': False,
                'preload_spool_id':    -1,
                'preload_auto_created': False,
                'has_per_lane_readers': False,
            }
        tag = self._state.current_tag
        is_meta_direct = self._state.current_spool is DIRECT_METADATA_SPOOL
        tag_present = self._state.current_uid is not None
        resolution = ''
        if is_meta_direct:
            resolution = 'metadata_direct'
        elif tag is not None and isinstance(tag.resolution, dict):
            resolution = tag.resolution.get('path', '')
        shared = getattr(self, '_shared', False)
        pending_spool = (getattr(self, '_shared_pending_spool', None)
                         if shared else None)
        pending_auto_created = (
            bool(getattr(self, '_shared_pending_auto_created', False))
            if shared else False)
        preload_spool = (getattr(self, '_shared_preload_spool', None)
                         if shared else None)
        preload_auto_created = (
            bool(getattr(self, '_shared_preload_auto_created', False))
            if shared else False)
        return {
            'gate':                self._gate,
            'enabled':             True,
            'reader_type':         self._reader_type,
            'tag_present':         tag_present,
            'spool_id':            (-1 if is_meta_direct
                                    else self._state.current_spool
                                    if self._state.current_spool is not None else -1),
            'uid':                 self._state.current_uid or '',
            'failed':              self._failed,
            'resolution':          resolution,
            'pending_spool_id':    pending_spool if pending_spool is not None else -1,
            'pending_auto_created': pending_auto_created,
            'preload_spool_id':    preload_spool if preload_spool is not None else -1,
            'preload_auto_created': preload_auto_created,
            'has_per_lane_readers': bool(getattr(self, '_has_per_lane_readers', False))
                                    if shared else False,
        }
