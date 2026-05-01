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
# Gate coordination logic for the supported per-lane PN532/I2C path:
#
#   NFCGateDefaults  — shared config defaults from the base [nfc_gate] section
#   NFCGate          — per-lane manager for [nfc_gate laneN] (one PN532 per EBB42)
#
# Internal helpers (not imported externally):
#   GateState        — per-gate debounce state machine
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
# Reader drivers are hardware/protocol adapters only.  PN532Driver reads tag
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
from dataclasses import dataclass, field
try:
    from .. import bus as bus_module
except ImportError:
    import bus as bus_module

from . import hh_status, pn532_driver, scan_jog
from .log            import configure, logger
from .pn532_driver   import PN532Driver
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


def _spoolman_url_enabled(url):
    value = str(url or '').strip().lower()
    return value not in ('', 'disabled', 'disable', 'false', 'off', 'none', 'no')


class _BusDefaultConfig:
    """Wraps a Klipper ConfigWrapper to supply an inherited default for i2c_bus."""
    def __init__(self, config, default_bus):
        self._cfg = config
        self._default_bus = default_bus
    def get(self, key, default=None):
        if key == 'i2c_bus':
            return self._cfg.get(key, self._default_bus if default is None else default)
        return self._cfg.get(key, default)
    def __getattr__(self, name):
        return getattr(self._cfg, name)


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
DIRECT_METADATA_SPOOL = object()


@dataclass
class CurrentTag:
    uid: str
    spool_id: object = None
    target_info: object = None
    raw_tag_data: object = None
    meta: dict = field(default_factory=dict)
    parse_error: object = None
    resolution: object = None


class GateState:
    def __init__(self, gate, absent_threshold=3):
        self.gate             = gate
        self._current_uid     = None
        self._current_spool   = None
        self.current_tag      = None
        self.miss_count       = 0
        self.absent_threshold = absent_threshold

    @property
    def current_uid(self):
        return self._current_uid

    @current_uid.setter
    def current_uid(self, uid_hex):
        self._current_uid = uid_hex
        self._sync_current_tag()

    @property
    def current_spool(self):
        return self._current_spool

    @current_spool.setter
    def current_spool(self, spool_id):
        self._current_spool = spool_id
        self._sync_current_tag()

    def _sync_current_tag(self):
        if self._current_uid is None:
            self.current_tag = None
            return
        if self.current_tag is None or self.current_tag.uid != self._current_uid:
            self.current_tag = CurrentTag(uid=self._current_uid,
                                          spool_id=self._current_spool)
            return
        self.current_tag.spool_id = self._current_spool

    def process_read(self, uid_hex, spool_id, scan_mode=False):
        if uid_hex is not None:
            self.miss_count = 0
            if spool_id is DIRECT_METADATA_SPOOL:
                if (self.current_uid == uid_hex
                        and self.current_spool is DIRECT_METADATA_SPOOL):
                    return None
                self.current_uid = uid_hex
                self.current_spool = DIRECT_METADATA_SPOOL
                return (EVENT_CHANGED, self.gate, uid_hex, None)
            if self.current_uid == uid_hex and self.current_spool == spool_id:
                return None
            self.current_uid   = uid_hex
            self.current_spool = spool_id
            if spool_id is not None:
                return (EVENT_CHANGED, self.gate, uid_hex, spool_id)
            return (EVENT_UID_ONLY, self.gate, uid_hex, None)
        else:
            if not scan_mode:
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
# Receives gate change events and dispatches them as GCode macro calls in the
# Klipper reactor thread.
#
# Macros called (define these in printer.cfg / nfc_macros.cfg):
#
#   _NFC_SPOOL_CHANGED  GATE=<n>  SPOOL_ID=<id>  UID=<hex>
#   _NFC_SPOOL_REMOVED  GATE=<n>
#   _NFC_TAG_NO_SPOOL   GATE=<n>  UID=<hex>

class KlipperInterface:
    def __init__(self, printer, reactor, debug=2):
        self._printer = printer
        self._reactor = reactor
        self._debug = debug

    def dispatch(self, event_type, gate, uid_hex, spool_id, meta=None):
        """Schedule a GCode macro call for the given gate event."""
        self._reactor.register_callback(
            lambda e, et=event_type, g=gate, u=uid_hex, s=spool_id, m=meta:
                self._run_gcode(et, g, u, s, m))

    @staticmethod
    def _macro_value(value):
        value = str(value or '').strip()
        value = re.sub(r'\s+', '_', value)
        return re.sub(r'[^A-Za-z0-9_#.+-]', '', value)

    def _run_gcode(self, event_type, gate, uid_hex, spool_id, meta=None):
        gcode = self._printer.lookup_object('gcode')
        try:
            if event_type == EVENT_CHANGED:
                if spool_id is not None:
                    script = "_NFC_SPOOL_CHANGED GATE={} SPOOL_ID={} UID={}".format(
                        gate, spool_id, uid_hex)
                    logger.info("nfc_gates: gate %d → spool %d detected (UID %s)",
                                 gate, spool_id, uid_hex)
                else:
                    material = self._macro_value((meta or {}).get('material', ''))
                    color = self._macro_value((meta or {}).get('color_hex', ''))
                    vendor = self._macro_value((meta or {}).get('brand')
                                               or (meta or {}).get('vendor', ''))
                    script = ("_NFC_SPOOL_CHANGED GATE={} MATERIAL={} "
                              "COLOR={} VENDOR={} UID={}").format(
                                  gate, material, color, vendor, uid_hex)
                    logger.info("nfc_gates: gate %d → tag %s metadata-only "
                                "(material=%s color=%s)",
                                gate, uid_hex, material, color)
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
            if self._debug >= 3:
                logger.info("nfc_gates: dispatching GCode: %s", script)
            gcode.run_script(script)
            if self._debug >= 3:
                logger.info("nfc_gates: dispatched GCode OK: %s", script)
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

# Module-level registry for NFC_STATUS across all configured lanes.
_lane_instances = []


def _lane_status_lines(printer):
    """Build NFC_STATUS output lines cross-referenced against the MMU
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
        self.scan_poll_interval = config.getfloat('scan_poll_interval', 0.1,
                                                   minval=0.1, maxval=5.0)
        self.scan_enabled         = config.getboolean('scan_enabled', True)
        self.tag_parsing          = config.getboolean('tag_parsing', False)
        self.tag_max_pages        = config.getint('tag_max_pages', 64,
                                                   minval=4, maxval=135)
        self.spoolman_auto_create = config.getboolean('spoolman_auto_create', False)

        self._printer = config.get_printer()
        gcode         = self._printer.lookup_object('gcode')
        gcode.register_command(
            'NFC_STATUS', self.cmd_NFC_STATUS,
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


class NFCGate:
    _active_scan_gate = None  # class-level scan lock; shared across all instances

    def __init__(self, config, defaults=None):
        self.printer  = config.get_printer()
        self.reactor  = self.printer.get_reactor()
        self._name    = config.get_name().split()[-1]

        d = defaults
        self._defaults         = defaults
        self._gate             = config.getint('mmu_gate', minval=0)
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
                logger.info("nfc_gate: [%s] Spoolman enabled — url=%s rfid_key=%s",
                            self._name, spoolman_url, spoolman_rfid_key)
            else:
                self._spoolman = None
                if spoolman_url:
                    logger.info("nfc_gate: [%s] Spoolman disabled by config",
                                self._name)
                else:
                    logger.warning(
                        "nfc_gate: [%s] spoolman_url not set — set spoolman_url in "
                        "[nfc_gate] or [nfc_gate %s]. Use 'auto' to read Moonraker.",
                        self._name, self._name)

        default_i2c_addr = d.i2c_address if d else 0x24
        default_i2c_bus  = d.i2c_bus if d else None
        i2c = bus_module.MCU_I2C_from_config(
            _BusDefaultConfig(config, default_i2c_bus),
            default_addr=default_i2c_addr,
            default_speed=100000)

        self._reader     = PN532Driver(i2c, self._gate,
                                       transceive_delay, crc_delay,
                                       self._debug,
                                       low_level_debug=self._low_level_debug)
        self._state      = GateState(self._gate, self._absent_threshold)
        self._suppress_next_dispatch_uid   = None
        self._suppress_next_dispatch_spool = None  # paired with uid — suppress only when both match
        self._hh_seed_spool_id   = None  # set on startup from HH gate map; cleared after first match
        self._hh_seed_available  = False  # True only when HH had the gate marked available at seed time
        self._hh_confirmed_spool = None  # last spool HH acknowledged; enables _check_hh_cleared
        self._hh_load_paused     = False  # True while HH owns this gate assignment
        self._failed     = False
        self._klipper    = KlipperInterface(self.printer, self.reactor, self._debug)
        self._polling    = False
        self._poll_timer = self.reactor.register_timer(self._poll_timer_event)

        self._scan_jog_mm   = config.getfloat('scan_jog_mm',
                                               d.scan_jog_mm if d else 50.0,
                                               minval=1.0, maxval=500.0)
        self._scan_max_mm   = None
        self._mmu_vars_path = None
        self._bowden_lengths = None
        self._scan_poll_interval = config.getfloat('scan_poll_interval',
                                                    d.scan_poll_interval if d else 0.1,
                                                    minval=0.1, maxval=5.0)
        self._scan_enabled  = config.getboolean('scan_enabled',
                                                 d.scan_enabled if d else True)
        self._tag_parsing          = config.getboolean('tag_parsing',
                                                        d.tag_parsing if d else False)
        self._tag_max_pages        = config.getint('tag_max_pages',
                                                    d.tag_max_pages if d else 64,
                                                    minval=4, maxval=135)
        self._spoolman_auto_create = config.getboolean('spoolman_auto_create',
                                                        d.spoolman_auto_create if d else False)
        self._scan_timer           = None
        self._scan_mode            = False
        self._scan_mm_total        = 0.0
        self._scan_next_chunk_time = 0.0
        self._scan_idle_ready_time = 0.0
        self._scan_found_event     = None  # cached event suppressed during jog; dispatched after rewind
        self._prev_gate_status     = -1   # -1 = unknown; prevents false trigger on cold start
        self._scan_pending           = False  # armed on 0→1 edge; fires when HH confirms idle
        self._scan_deferred_notified = False  # True after first console msg for this deferral

        # delayed-init state
        self._gcode = None
        self._commands_registered = False
        self._status_registered = False

        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.printer.register_event_handler('klippy:disconnect',
                                            self._handle_disconnect)

    def _cmd_NFC_STATUS_fallback(self, gcmd):
        gcmd.respond_info('\n'.join(_lane_status_lines(self.printer)))

    def _cmd_help(self, gcmd):
        lines = [
            "NFC GATE=%d commands:" % self._gate,
            "  NFC GATE=%d STATUS=1  - show this gate state" % self._gate,
            "  NFC GATE=%d INIT=1    - re-run reader init" % self._gate,
            "  NFC GATE=%d SCAN=1    - scan hardware once, no Spoolman/HH dispatch" % self._gate,
            "  NFC GATE=%d JOG_SCAN=1 - start scan-jog (same as automatic pre-load trigger)" % self._gate,
            "  NFC GATE=%d POLL=1    - run one full NFC_Manager poll for this gate" % self._gate,
            "  NFC GATE=%d APPLY=1   - send cached spool to Happy Hare now" % self._gate,
            "  NFC GATE=%d CLEAR_CACHE=1 - clear cached spool lookup, no HH dispatch" % self._gate,
            "  NFC GATE=%d HH_SYNC=1 SPOOL_ID=<n> - seed lane cache from HH gate map (called by NFC_HH_SYNC_CACHE macro)" % self._gate,
            "  NFC GATE=%d READ=1    - start timer polling" % self._gate,
            "  NFC GATE=%d READ=0    - stop timer polling" % self._gate,
        ]
        if self._low_level_debug:
            lines.extend(pn532_driver.low_level_debug_help_lines(
                "NFC GATE=%d" % self._gate))
        gcmd.respond_info('\n'.join(lines))

    def _manual_scan(self, gcmd):
        try:
            target_info = self._reader.read_target()
            if target_info is None:
                gcmd.respond_info("NFC[%s]: no tag detected" % self._name)
                return
            gcmd.respond_info(
                "NFC[%s]: UID=%s Tg=%s SENS_RES=0x%04X SAK=0x%02X UIDLen=%d"
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
            gcmd.respond_info("NFC[%s]: reader %s" %
                              (self._name, "OK" if alive else "not responding"))
        except Exception as e:
            self._failed = True
            gcmd.respond_info("NFC[%s]: init failed: %s" %
                              (self._name, e))

    def _set_reading(self, gcmd, enabled):
        if enabled:
            if self._failed:
                gcmd.respond_info("NFC[%s]: reader failed; run INIT=1 first"
                                  % self._name)
                return
            self._polling = True
            self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
            gcmd.respond_info("NFC[%s]: polling started" % self._name)
        else:
            self._polling = False
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
            gcmd.respond_info("NFC[%s]: polling stop requested" % self._name)

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
            "nfc_gate: [%s] gate %d — spool cache cleared "
            "(uid=%s old_spool=%s); next read will resolve Spoolman again",
            self._name, self._gate, self._state.current_uid, old_spool)
        gcmd.respond_info(
            "NFC[%s]: cleared cached spool_id for gate %d; "
            "no NFC_Manager event was dispatched. Next tag read will resolve "
            "Spoolman again."
            % (self._name, self._gate))

    def _apply_current_spool(self, gcmd):
        """Dispatch the current cached spool to Happy Hare immediately."""
        if self._state.current_spool is None:
            gcmd.respond_info(
                "NFC[%s]: no cached spool_id to apply; run POLL=1 first"
                % self._name)
            return
        uid_hex = self._state.current_uid or ''
        spool_id = self._state.current_spool
        if spool_id is DIRECT_METADATA_SPOOL:
            meta = (self._state.current_tag.meta
                    if self._state.current_tag is not None else {})
            logger.info(
                "nfc_gate: [%s] gate %d — manual apply metadata uid=%s",
                self._name, self._gate, uid_hex)
            self._klipper.dispatch(EVENT_CHANGED, self._gate, uid_hex,
                                   None, meta=meta)
            gcmd.respond_info(
                "NFC[%s]: dispatched cached tag metadata for gate %d to "
                "Happy Hare" % (self._name, self._gate))
            return
        logger.info(
            "nfc_gate: [%s] gate %d — manual apply spool=%s uid=%s",
            self._name, self._gate, spool_id, uid_hex)
        self._klipper.dispatch(EVENT_CHANGED, self._gate, uid_hex, spool_id)
        gcmd.respond_info(
            "NFC[%s]: dispatched cached spool_id=%s for gate %d to "
            "Happy Hare"
            % (self._name, spool_id, self._gate))

    def _cmd_low_level_debug(self, gcmd):
        if pn532_driver.low_level_debug_requested(gcmd) and self._polling:
            self._polling = False
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
            gcmd.respond_info(
                "NFC[%s]: polling paused for low-level PN532 debug" %
                self._name)
        try:
            return pn532_driver.run_low_level_debug(
                gcmd, self._reader, self._name,
                "NFC GATE=%d" % self._gate,
                self._low_level_debug)
        except Exception as e:
            gcmd.respond_info("NFC[%s]: low-level debug failed: %s" %
                              (self._name, e))
            return True

    def cmd_NFC(self, gcmd):
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
            gcmd.respond_info("NFC[%s]: one poll complete; %s" %
                              (self._name, self.status_line().strip()))
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

        Called once from _delayed_init() after the PN532 initialises
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
                    "nfc_gate: [%s] gate %d — HH MMU object not found; "
                    "skipping startup cache seed", self._name, self._gate)
                return
            if self._gate >= hh.gate_count:
                logger.info(
                    "nfc_gate: [%s] gate %d — gate index exceeds HH map length "
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
                            "nfc_gate: [%s] gate %d — startup: seeded from "
                            "HH+Spoolman spool_id=%d uid=%s",
                            self._name, self._gate, hh.spool, uid)
                    else:
                        logger.info(
                            "nfc_gate: [%s] gate %d — HH seed: spool_id=%d "
                            "available (no UID in Spoolman — will verify on "
                            "first poll)",
                            self._name, self._gate, hh.spool)
                else:
                    logger.info(
                        "nfc_gate: [%s] gate %d — HH seed: spool_id=%d  "
                        "gate_status=%s  (will verify on first physical scan)",
                        self._name, self._gate, hh.spool, hh.status)
            else:
                logger.info(
                    "nfc_gate: [%s] gate %d — HH reports gate empty/unknown "
                    "(spool_id=%s); no seed applied",
                    self._name, self._gate, hh.spool)

        except Exception:
            logger.exception(
                "nfc_gate: [%s] gate %d — error reading HH gate map for "
                "startup cache seed (non-fatal, polling continues)",
                self._name, self._gate)

    def _hh_sync(self, gcmd):
        """Receive a spool_id from NFC_HH_SYNC_CACHE and set the lane seed.

        Called by NFC GATE=<n> HH_SYNC=1 SPOOL_ID=<n>.
        The macro reads HH template vars (which GCode macros can access) and
        passes the resolved spool_id here so Python can update the seed without
        needing to walk the HH object itself.
        """
        spool_id = gcmd.get_int('SPOOL_ID', -1)
        if spool_id > 0:
            self._hh_seed_spool_id = spool_id
            logger.info(
                "nfc_gate: [%s] gate %d — HH_SYNC: seed set to spool_id=%d",
                self._name, self._gate, spool_id)
            gcmd.respond_info(
                "NFC[%s]: HH seed → spool_id=%d  "
                "(next poll matching this spool will not re-dispatch to HH)"
                % (self._name, spool_id))
        else:
            self._hh_seed_spool_id = None
            logger.info(
                "nfc_gate: [%s] gate %d — HH_SYNC: gate empty/unknown, "
                "seed cleared", self._name, self._gate)
            gcmd.respond_info(
                "NFC[%s]: HH reports gate empty — seed cleared" % self._name)

    def _handle_connect(self):
        self._gcode = self.printer.lookup_object('gcode')

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

            self._gcode.register_mux_command(
                cmd='NFC',
                key='GATE',
                value=str(self._gate),
                func=self.cmd_NFC,
                desc="Control or test one configured NFC gate"
            )

            self._commands_registered = True

        self._gcode.respond_info(f"📡 NFC Gate [{self._name}] connected")

        # Schedule PN532 init after the rest of Klippy/I2C has settled
        self.reactor.register_timer(
            self._delayed_init,
            self.reactor.monotonic() + 2.0
        )

    def _delayed_init(self, eventtime):
        """Initialise the PN532 after other I2C devices have had time to settle.

        Runs in the reactor thread 2 seconds after klippy:connect fires.
        Returns reactor.NEVER so the timer does not repeat.
        """
        if self._debug >= 4:
            logger.debug(
                "nfc_gate: [%s] delayed init — wake + SAMConfiguration",
                self._name)

        try:
            self._reader.init()
            if self._reader.is_alive():
                self._failed = False
                logger.info("nfc_gate: [%s] PN532 reader OK", self._name)
            else:
                self._failed = True
                logger.error(
                    "nfc_gate: [%s] PN532 did not respond — "
                    "check wiring and I2C address (default 0x24)", self._name)
        except Exception as e:
            self._failed = True
            logger.error("nfc_gate: [%s] init error: %s", self._name, e)

        # Seed lane cache from Happy Hare's current gate map so the first poll
        # after restart does not re-dispatch a spool HH already knows about.
        if not self._failed:
            self._seed_cache_from_hh(eventtime)
            # Bootstrap the scan-jog edge detector with the current gate status
            # so a pre-loaded gate never triggers a scan on the first poll.
            hh = self._read_hh_status(eventtime)
            if hh.present and self._gate < hh.gate_count:
                self._prev_gate_status = hh.status

        if self._gcode is not None:
            if self._failed:
                self._gcode.respond_info(
                    "❌ NFC[%s]: reader not ready — check wiring. "
                    "Run NFC GATE=%d INIT=1 after fixing."
                    % (self._name, self._gate))
            else:
                seed_note = ("  HH seed: spool_id=%d" % self._hh_seed_spool_id
                             if self._hh_seed_spool_id is not None
                             else "  HH reports gate empty")
                self._gcode.respond_info(
                    "✅ NFC[%s]: reader ready.%s  %s"
                    % (self._name,
                       seed_note,
                       "Startup polling is enabled; first poll in %.1fs."
                       % self._startup_poll_delay
                       if self._startup_polling == 1
                       else "Run NFC GATE=%d READ=1 to start polling."
                            % self._gate))

        if not self._failed and self._startup_polling == 1:
            self._polling = True
            first_poll = self.reactor.monotonic() + self._startup_poll_delay
            self.reactor.update_timer(self._poll_timer, first_poll)
            logger.info("nfc_gate: [%s] startup polling enabled; first poll in %.1fs",
                        self._name, self._startup_poll_delay)

        return self.reactor.NEVER

    def _handle_disconnect(self):
        if self._debug >= 4:
            logger.debug("nfc_gate: [%s] disconnect — stopping polling timer",
                         self._name)
        self._polling = False
        self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
        if self._scan_timer is not None:
            self.reactor.update_timer(self._scan_timer, self.reactor.NEVER)
        if NFCGate._active_scan_gate == self._gate:
            NFCGate._active_scan_gate = None

    def _poll_timer_event(self, eventtime):
        if not self._polling:
            return self.reactor.NEVER
        if self._failed:
            logger.warning("nfc_gate: [%s] polling stopped — reader failed; "
                           "run NFC GATE=%d INIT=1 first",
                           self._name, self._gate)
            self._polling = False
            return self.reactor.NEVER

        # Scan-jog gate-status edge detection.
        # Reads HH gate_status on every tick — Python dict only, no I2C.
        # When gate is empty (curr==0) skip the I2C read entirely.
        # On < 1 -> >=1 transition with HH idle and not printing, enter scan mode.
        if self._scan_enabled:
            hh = self._read_hh_status(eventtime)
            if hh.present and self._gate < hh.gate_count:
                curr = hh.status
                prev = self._prev_gate_status
                self._prev_gate_status = curr
                if self._debug >= 4:
                    logger.debug(
                        "nfc_gate: [%s] gate %d — HH poll: "
                        "prev=%s curr=%s action=%s pending=%s printing=%s "
                        "active_scan=%s load_paused=%s",
                        self._name, self._gate,
                        prev, curr, hh.action,
                        getattr(self, '_scan_pending', False),
                        self._is_printing(),
                        NFCGate._active_scan_gate if NFCGate._active_scan_gate is not None else 'none',
                        self._hh_load_paused)
                if curr <= 0:
                    self._scan_pending = False
                    nfc_spool = self._state.current_spool
                    if hh.assigned and nfc_spool == hh.spool:
                        if not self._hh_load_paused:
                            self._hh_load_paused = True
                            logger.info(
                                "nfc_gate: [%s] gate %d — HH has assigned "
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
                            "nfc_gate: [%s] gate %d — gate ejected; "
                            "resuming poll and clearing NFC cache",
                            self._name, self._gate)
                        return self.reactor.monotonic() + 1.0
                    return self.reactor.monotonic() + self._poll_interval
                # 0→1 edge: arm pending flag and let HH fully settle
                if prev < 1  and curr >= 1:
                    self._scan_pending = True
                    self._scan_deferred_notified = False
                    self._scan_idle_ready_time = 0.0
                    if self._debug >= 3:
                        logger.info(
                            "nfc_gate: [%s] gate %d — gate loaded; "
                            "waiting for HH idle before scan",
                            self._name, self._gate)
                # Fire scan once HH is idle and gate is confirmed loaded
                if (getattr(self, '_scan_pending', False) and curr == 1
                        and hh.idle
                        and not self._is_printing()):
                    now = self.reactor.monotonic()
                    if self._scan_idle_ready_time <= 0.0:
                        self._scan_idle_ready_time = now + 0.1
                        if self._debug >= 3:
                            logger.info(
                                "nfc_gate: [%s] gate %d — HH idle; "
                                "waiting 0.1s before scan-jog",
                                self._name, self._gate)
                        return self._scan_idle_ready_time
                    if now < self._scan_idle_ready_time:
                        return self._scan_idle_ready_time
                    self._scan_pending = False
                    self._scan_idle_ready_time = 0.0
                    if NFCGate._active_scan_gate is not None:
                        if not self._scan_deferred_notified:
                            msg = ("NFC[%d]: scan-jog waiting — "
                                   "gate %d is already scanning"
                                   % (self._gate, NFCGate._active_scan_gate))
                            logger.info("nfc_gate: [%s] %s", self._name, msg)
                            self._console("⏳ " + msg)
                            self._scan_deferred_notified = True
                        self._scan_pending = True  # re-arm; retry after active scan has time to progress
                        self._scan_idle_ready_time = now + 3.0
                        return self._scan_idle_ready_time
                    ok, reason, max_mm = self._prepare_scan_jog(eventtime)
                    if not ok:
                        msg = "NFC[%d]: scan-jog not available while %s" % (
                            self._gate, reason)
                        logger.warning("nfc_gate: [%s] %s", self._name, msg)
                        self._console("⚠️ " + msg)
                        return self.reactor.monotonic() + self._poll_interval
                    msg = ("🔍 NFC[%d]: starting scan-jog "
                           "(max=%.0fmm  poll=%.2fs)"
                           % (self._gate, max_mm, self._scan_poll_interval))
                    if self._debug >= 3:
                        logger.info("nfc_gate: [%s] %s", self._name, msg)
                    self._console(msg)
                    self._start_scan_mode(max_mm=max_mm)
                    return self.reactor.NEVER
                if getattr(self, '_scan_pending', False):
                    return self.reactor.monotonic() + .25

        if self._debug >= 4:
            logger.debug("nfc_gate: [%s] poll cycle start — "
                         "current state: uid=%s spool=%s misses=%d",
                         self._name,
                         self._state.current_uid or 'none',
                         self._state.current_spool
                         if self._state.current_spool is not None else 'none',
                         self._state.miss_count)
        try:
            self._poll()
        except Exception:
            logger.exception("nfc_gate: [%s] poll error", self._name)
        if self._debug >= 4:
            logger.debug("nfc_gate: [%s] poll cycle done — "
                         "next poll in %.0fs", self._name, self._poll_interval)
        return self.reactor.monotonic() + self._poll_interval

    def _classify_tag_target(self, target_info):
        if not isinstance(target_info, dict):
            return 'uid_only'
        try:
            sak = int(target_info.get('sak', 0)) & 0xFF
            uid_length = int(target_info.get('uid_length', 0))
        except (TypeError, ValueError):
            return 'uid_only'

        # Conservative ISO14443A split:
        #   SAK bit 0x08 marks MIFARE Classic-compatible targets.
        #   SAK 0x00 is the common Type-2 / Ultralight / NTAG case.
        # Everything else falls back to UID-only until explicitly supported.
        if sak & 0x08:
            return 'mifare_classic'
        if sak == 0x00 and uid_length in (4, 7, 10):
            return 'ntag_type2'
        return 'uid_only'

    def _release_reader_target(self, reason):
        release = getattr(self._reader, '_release_current_target', None)
        if release is not None:
            try:
                release(reason=reason)
            except TypeError:
                release()
            except Exception as e:
                if self._debug >= 4:
                    logger.debug(
                        "nfc_gate: [%s] gate %d — target release failed "
                        "(%s): %s", self._name, self._gate, reason, e)

    def _parse_current_tag(self, tag):
        uid_hex = tag.uid
        if not tag.raw_tag_data:
            tag.meta = {'uid': uid_hex}
            return
        try:
            from .vendor.rfid_tag_parser import parse_tag
            info = parse_tag(bytes(tag.raw_tag_data), uid_hex=uid_hex)
            if isinstance(info, dict) and 'uid' not in info:
                info = dict(info)
                info['uid'] = uid_hex
            if info is None:
                tag.meta = {'uid': uid_hex}
                tag.parse_error = None
            else:
                tag.meta = info
                tag.parse_error = info.get('parse_error') or info.get('error')
            if self._debug >= 3:
                logger.info("nfc_gate: [%s] gate %d — uid=%s  parse_tag → %s",
                            self._name, self._gate, uid_hex,
                            {k: v for k, v in tag.meta.items()
                             if k in ('material', 'vendor', 'color_hex',
                                      'spoolman_id', 'parse_error')})
            if self._debug >= 4:
                logger.debug("nfc_gate: [%s] gate %d — uid=%s  full meta: %s",
                             self._name, self._gate, uid_hex, tag.meta)
        except Exception as e:
            tag.parse_error = 'parse failed: {}'.format(e)
            logger.error("nfc_gate: [%s] gate %d — uid=%s  parse_tag raised: %s",
                         self._name, self._gate, uid_hex, e)

    def _capture_ntag_metadata(self, tag):
        uid_hex = tag.uid
        try:
            raw = self._reader.ntag_read_user_memory(
                start_page=4, end_page=4 + self._tag_max_pages - 1)
            tag.raw_tag_data = raw
            if self._debug >= 3:
                logger.info("nfc_gate: [%s] gate %d — uid=%s  NTAG read %d bytes",
                            self._name, self._gate, uid_hex, len(raw))
        except Exception as e:
            tag.parse_error = 'ntag read failed: {}'.format(e)
            tag.meta = {'uid': uid_hex}
            logger.warning("nfc_gate: [%s] gate %d — uid=%s  NTAG read failed: %s",
                           self._name, self._gate, uid_hex, e)
            return
        if not raw:
            tag.parse_error = 'empty ntag read'
            tag.meta = {'uid': uid_hex}
            logger.warning("nfc_gate: [%s] gate %d — uid=%s  NTAG read returned no data",
                           self._name, self._gate, uid_hex)
            return
        self._parse_current_tag(tag)

    def _capture_tag_metadata(self, uid_hex):
        tag = self._state.current_tag
        if tag is None or tag.uid != uid_hex:
            tag = CurrentTag(uid=uid_hex)
            self._state.current_tag = tag
        self._capture_ntag_metadata(tag)

    def _read_current_tag(self):
        if not self._tag_parsing:
            return self._reader.read_tag()

        target_info = self._reader.read_target()
        if target_info is None:
            return None

        uid_hex = target_info.get('uid')
        if not uid_hex:
            self._release_reader_target("missing_uid")
            return None

        tag = CurrentTag(uid=uid_hex, target_info=dict(target_info))
        tag.meta = {'uid': uid_hex}
        self._state.current_tag = tag

        strategy = self._classify_tag_target(target_info)
        if self._debug >= 3:
            logger.info(
                "nfc_gate: [%s] gate %d — uid=%s  target strategy=%s "
                "SAK=0x%02X ATQA=0x%04X",
                self._name, self._gate, uid_hex, strategy,
                int(target_info.get('sak', 0) or 0),
                int(target_info.get('atqa', target_info.get('sens_res', 0)) or 0))

        if strategy == 'ntag_type2':
            self._capture_ntag_metadata(tag)
        elif strategy == 'mifare_classic':
            tag.parse_error = 'mifare_classic metadata read not implemented'
            self._release_reader_target("mifare_uid_only_fallback")
            if self._debug >= 3:
                logger.info(
                    "nfc_gate: [%s] gate %d — uid=%s  MIFARE Classic "
                    "metadata read not implemented; UID-only fallback",
                    self._name, self._gate, uid_hex)
        else:
            tag.parse_error = 'unsupported target; uid-only fallback'
            self._release_reader_target("unsupported_uid_only_fallback")
            if self._debug >= 3:
                logger.info(
                    "nfc_gate: [%s] gate %d — uid=%s  unsupported target; "
                    "UID-only fallback", self._name, self._gate, uid_hex)

        return uid_hex

    def _resolve_spool(self, uid_hex):
        if uid_hex is None:
            return None
        tag = self._state.current_tag
        if tag is not None and tag.uid != uid_hex:
            tag = None
        meta = {}
        if self._tag_parsing and tag is not None and isinstance(tag.meta, dict):
            meta = tag.meta
        material = str(meta.get('material') or meta.get('type') or '').strip()
        color = str(meta.get('color_hex') or meta.get('color') or '').strip()

        if self._spoolman is None:
            if material or color:
                if tag is not None:
                    tag.resolution = {'path': 'metadata_direct'}
                if self._debug >= 3:
                    logger.info(
                        "nfc_gate: [%s] gate %d — uid=%s  no Spoolman; "
                        "using tag metadata material=%s color=%s",
                        self._name, self._gate, uid_hex, material, color)
                return DIRECT_METADATA_SPOOL
            if self._debug >= 3:
                logger.info("nfc_gate: [%s] gate %d — uid=%s  no Spoolman configured",
                            self._name, self._gate, uid_hex)
            return None

        spoolman_id = meta.get('spoolman_id')
        if spoolman_id not in (None, ''):
            try:
                spoolman_id = int(spoolman_id)
            except (TypeError, ValueError):
                spoolman_id = None
                logger.warning(
                    "nfc_gate: [%s] gate %d — uid=%s  invalid embedded "
                    "spoolman_id=%r; falling back to UID lookup",
                    self._name, self._gate, uid_hex, meta.get('spoolman_id'))
            if spoolman_id is not None:
                spool = self._spoolman.lookup_spool_by_id(spoolman_id)
                if spool:
                    raw_id = spool.get('id', spoolman_id)
                    try:
                        resolved_id = int(raw_id)
                    except (TypeError, ValueError):
                        resolved_id = spoolman_id
                    if tag is not None:
                        tag.resolution = {'path': 'embedded_spoolman_id',
                                          'spool_id': resolved_id}
                    if self._debug >= 3:
                        logger.info(
                            "nfc_gate: [%s] gate %d — uid=%s  "
                            "embedded spoolman_id=%s resolved",
                            self._name, self._gate, uid_hex, resolved_id)
                    return resolved_id
                if self._debug >= 3:
                    logger.info(
                        "nfc_gate: [%s] gate %d — uid=%s  "
                        "embedded spoolman_id=%s not found; falling back",
                        self._name, self._gate, uid_hex, spoolman_id)

        spool_id = self._spoolman.lookup_spool_by_uid(uid_hex)
        if spool_id is not None:
            if tag is not None:
                tag.resolution = {'path': 'uid_lookup', 'spool_id': spool_id}
            if self._debug >= 3:
                logger.info("nfc_gate: [%s] gate %d — uid=%s  Spoolman→spool_id=%s",
                            self._name, self._gate, uid_hex, spool_id)
            return spool_id

        try:
            base_url = self._spoolman._resolve_base_url()
        except Exception as e:
            base_url = None
            logger.warning(
                "nfc_gate: [%s] gate %d — uid=%s  Spoolman URL "
                "resolution failed: %s",
                self._name, self._gate, uid_hex, e)
        if not base_url and (material or color):
            if tag is not None:
                tag.resolution = {'path': 'metadata_direct'}
            if self._debug >= 3:
                logger.info(
                    "nfc_gate: [%s] gate %d — uid=%s  Spoolman disabled "
                    "or undiscovered; using tag metadata material=%s color=%s",
                    self._name, self._gate, uid_hex, material, color)
            return DIRECT_METADATA_SPOOL

        if self._spoolman_auto_create and material:
            if base_url:
                try:
                    from .vendor.lameandboard_spoolman import (
                        SpoolmanClient as LBSpoolmanClient)
                    lb = LBSpoolmanClient(base_url=base_url,
                                          timeout=self._spoolman._timeout)
                    vendor_name = str(meta.get('brand')
                                      or meta.get('vendor')
                                      or 'Generic').strip() or 'Generic'
                    vendor_id = lb.find_or_create_vendor(vendor_name)
                    filament_name = material
                    filament_id = lb.find_or_create_filament(
                        name=filament_name,
                        vendor_id=vendor_id,
                        material=material,
                        color_hex=color or None,
                        diameter=meta.get('diameter_mm') or 1.75)
                    weight_g = meta.get('weight_g')
                    if weight_g is None:
                        weight_g = meta.get('weight')
                    spool = lb.create_spool(
                        filament_id=filament_id,
                        initial_weight=weight_g,
                        remaining_weight=weight_g,
                        spool_weight=meta.get('spool_weight_g'),
                        lot_nr=meta.get('tray_uid'),
                        extra={self._spoolman._rfid_key: uid_hex})
                    if isinstance(spool, dict) and spool.get('id') is not None:
                        new_spool_id = int(spool.get('id'))
                        self._spoolman.clear_cache()
                        if tag is not None:
                            tag.resolution = {'path': 'auto_create',
                                              'spool_id': new_spool_id}
                        if self._debug >= 3:
                            logger.info(
                                "nfc_gate: [%s] gate %d — uid=%s  "
                                "auto-created Spoolman spool_id=%s",
                                self._name, self._gate, uid_hex, new_spool_id)
                        return new_spool_id
                    logger.warning(
                        "nfc_gate: [%s] gate %d — uid=%s  auto-create "
                        "returned unexpected spool response: %r",
                        self._name, self._gate, uid_hex, spool)
                except Exception as e:
                    logger.warning(
                        "nfc_gate: [%s] gate %d — uid=%s  "
                        "Spoolman auto-create failed: %s",
                        self._name, self._gate, uid_hex, e)
            elif material or color:
                if tag is not None:
                    tag.resolution = {'path': 'metadata_direct'}
                if self._debug >= 3:
                    logger.info(
                        "nfc_gate: [%s] gate %d — uid=%s  Spoolman "
                        "unavailable; using tag metadata material=%s color=%s",
                        self._name, self._gate, uid_hex, material, color)
                return DIRECT_METADATA_SPOOL

        if tag is not None:
            tag.resolution = {'path': 'unresolved'}
        if self._debug >= 3:
            logger.info("nfc_gate: [%s] gate %d — uid=%s  Spoolman→spool_id=None",
                        self._name, self._gate, uid_hex)
        return None

    def _check_hh_cleared(self):
        """Reset lane cache if HH cleared this gate from outside the NFC system.

        Only active after HH has confirmed the spool at least once (_hh_confirmed_spool
        is set when HH's gate_spool_id matches what NFC dispatched).  This prevents a
        loop where NFC dispatches spool 49, HH hasn't processed it yet, the check sees
        HH=-1, clears the cache, NFC dispatches again next poll, and so on forever.
        """
        if self._state.current_spool is None:
            return  # Lane cache already empty — nothing to cross-check
        if self._hh_confirmed_spool != self._state.current_spool:
            return  # HH hasn't acknowledged this spool yet — don't second-guess it
        hh = self._read_hh_status()
        if not hh.present:
            return
        nfc_spool = self._state.current_spool
        hh_differs = (not hh.assigned) or (hh.spool != nfc_spool)
        if hh_differs:
            if not hh.assigned:
                reason = "HH cleared gate externally (NFC cache had spool=%d)" % nfc_spool
            else:
                reason = ("HH has spool=%d but NFC cache has spool=%d "
                          "(manual gate map change?)" % (hh.spool, nfc_spool))
            logger.info(
                "nfc_gate: [%s] gate %d — %s; resetting lane cache so "
                "next tag read re-dispatches _NFC_SPOOL_CHANGED",
                self._name, self._gate, reason)
            self._state.current_uid   = None
            self._state.current_spool = None
            self._state.miss_count    = 0
            self._hh_confirmed_spool  = None

    def _hh_gate_matches_current_spool(self):
        """Return True when HH already owns this gate's current spool.

        HH may report a gate as merely assigned (gate_spool_id > 0,
        gate_status == 0) or available/loaded (gate_status >= 1).  Once NFC has
        read and cached that same spool, either state is enough to stop NFC
        polling until HH clears the assignment.
        """
        nfc_spool = self._state.current_spool
        if nfc_spool is None:
            return False
        hh = self._read_hh_status()
        return hh.present and hh.spool == nfc_spool

    def _poll(self):
        # Suspend scanning once HH already has the same spool assigned to this
        # gate and NFC has read the tag at least once. Requiring the local NFC
        # cache to have a spool keeps the UID visible in status output.
        if (not self._scan_mode
                and self._hh_gate_matches_current_spool()
                and self._state.current_spool is not None):
            if not self._hh_load_paused:
                self._hh_load_paused = True
                logger.info(
                    "nfc_gate: [%s] gate %d — spool confirmed by NFC; "
                    "HH owns same spool — suspending poll until ejected",
                    self._name, self._gate)
            self._state.miss_count = 0
            return

        if self._hh_load_paused:
            self._hh_load_paused    = False
            self._state.current_uid   = None
            self._state.current_spool = None
            self._state.miss_count    = 0
            self._hh_confirmed_spool  = None
            logger.info(
                "nfc_gate: [%s] gate %d — filament unloaded; resuming NFC scan",
                self._name, self._gate)

        self._check_hh_cleared()
        uid_hex = self._read_current_tag()

        if uid_hex is None:
            if self._debug >= 4:
                logger.debug("nfc_gate: [%s] gate %d — no tag (miss %d)",
                             self._name, self._gate, self._state.miss_count + 1)
        else:
            if self._debug >= 4:
                logger.debug("nfc_gate: [%s] gate %d — tag read uid=%s",
                             self._name, self._gate, uid_hex)

        spool_id = self._resolve_spool(uid_hex)

        event = self._state.process_read(uid_hex, spool_id,
                                         scan_mode=self._scan_mode)

        # ── debug=4 compact per-poll trace ───────────────────────────────────
        # One line per poll: lane, gate, what was read, and what action fired.
        if self._debug >= 4:
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
                    action_str = "NO_SPOOL  (uid=%s not registered in Spoolman)" % (
                        event[2],)
                else:
                    action_str = str(etype)
            logger.debug("nfc_gate: [%s] POLL  gate=%-2d  %-28s  →  %s",
                         self._name, self._gate, read_str, action_str)
        # ─────────────────────────────────────────────────────────────────────

        if event is not None:
            event_type, gate, uid, spool = event
            if self._debug >= 3:
                logger.info("nfc_gate: [%s] gate %d — %s uid=%s spool=%s",
                            self._name, gate, event_type, uid, spool)

            suppress = (self._hh_seed_spool_id is not None
                        and event_type == EVENT_CHANGED
                        and spool == self._hh_seed_spool_id
                        and self._hh_seed_available)
            self._hh_seed_spool_id  = None  # one-shot, always clear
            self._hh_seed_available = False

            if self._is_printing():
                if self._debug >= 3:
                    logger.info(
                        "nfc_gate: [%s] gate %d — %s detected during print; "
                        "Spoolman and HH dispatch suppressed",
                        self._name, gate, event_type)
            elif self._scan_mode:
                # Filament is moving — cache the event and dispatch after rewind.
                meta = None
                if (event_type == EVENT_CHANGED
                        and self._state.current_spool is DIRECT_METADATA_SPOOL
                        and self._state.current_tag is not None):
                    meta = self._state.current_tag.meta
                self._scan_found_event = (event_type, gate, uid, spool, meta)
                if self._debug >= 3:
                    logger.info(
                        "nfc_gate: [%s] gate %d — %s detected during scan-jog; "
                        "dispatch deferred until rewind complete",
                        self._name, gate, event_type)
            else:
                if self._spoolman is not None and spool is not DIRECT_METADATA_SPOOL:
                    if event_type == EVENT_CHANGED and spool is not None:
                        self._spoolman.update_spool_location(spool, gate)
                    elif event_type == EVENT_REMOVED and spool is not None:
                        self._spoolman.clear_spool_location(spool)

                if suppress:
                    if self._debug >= 3:
                        logger.info(
                            "nfc_gate: [%s] gate %d — startup seed match "
                            "spool=%s; skipping HH dispatch",
                            self._name, gate, spool)
                else:
                    meta = None
                    if (event_type == EVENT_CHANGED
                            and self._state.current_spool is DIRECT_METADATA_SPOOL
                            and self._state.current_tag is not None):
                        meta = self._state.current_tag.meta
                    self._klipper.dispatch(event_type, gate, uid, spool, meta=meta)
                    if event_type == EVENT_CHANGED and spool is not None:
                        self._hh_confirmed_spool = spool
                    elif event_type == EVENT_REMOVED:
                        self._hh_confirmed_spool = None

        return uid_hex is not None

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
                    "nfc_gate: [%s] scan preflight — lane %d gate_status=%d %s",
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
                    "nfc_gate: [%s] could not read [save_variables] filename",
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
                "nfc_gate: [%s] could not read Bowden lengths from %s",
                self._name, path)
            return None

        return None

    def _get_lane_scan_max_mm(self):
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

    def _hh_filament_label(self):
        """Return a short string describing this gate's HH spool assignment."""
        hh = self._read_hh_status()
        if not hh.present:
            return "HH: n/a"
        if self._gate >= hh.gate_count:
            return "HH: unknown"
        if hh.active_gate == self._gate and hh.filament_pos > 0:
            return "HH: spool %d  loading (pos %d)" % (hh.spool, hh.filament_pos)
        if hh.assigned:
            return "HH: spool %d  %s" % (
                hh.spool, "available" if hh.available else "assigned")
        return "HH: empty"

    def status_line(self):
        if self._failed:
            return ("  Gate %d  [%s]:  READER FAILED (check wiring, address 0x24)"
                    % (self._gate, self._name))
        if self._hh_load_paused:
            poll_state = "polling suspended"
        elif self._polling:
            poll_state = "polling"
        else:
            poll_state = "not polling"
        hh_label = self._hh_filament_label()
        if self._state.current_spool is DIRECT_METADATA_SPOOL:
            meta = (self._state.current_tag.meta
                    if self._state.current_tag is not None else {})
            material = (meta or {}).get('material', '')
            color = (meta or {}).get('color_hex', '')
            return ("  Gate %d:  tag %s  metadata material=%s color=%s   [%s]  [%s]"
                    % (self._gate, self._state.current_uid,
                       material, color, poll_state, hh_label))
        if self._state.current_spool is not None:
            return ("  Gate %d:  spool %-6d   UID %s   [%s]  [%s]"
                    % (self._gate,
                       self._state.current_spool, self._state.current_uid,
                       poll_state, hh_label))
        if self._state.current_uid is not None:
            return ("  Gate %d:  tag %s  (UID not in Spoolman)   [%s]  [%s]"
                    % (self._gate, self._state.current_uid, poll_state, hh_label))
        return ("  Gate %d:  empty   [%s]  [%s]"
                % (self._gate, poll_state, hh_label))

    def get_status(self, _eventtime=None):
        return {
            'gate':     self._gate,
            'spool_id': (-1 if self._state.current_spool is DIRECT_METADATA_SPOOL
                         else self._state.current_spool
                         if self._state.current_spool is not None else -1),
            'uid':      self._state.current_uid or '',
            'failed':   self._failed,
        }
