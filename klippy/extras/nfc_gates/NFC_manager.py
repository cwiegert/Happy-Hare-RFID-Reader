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

import re
import threading
import time

import bus as bus_module

from .log            import configure, logger
from .pn532_driver   import PN532Driver
from .rc522_driver   import RC522Driver
from .spoolman_client import SpoolmanClient


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
        self.i2c_address        = config.getint('i2c_address', 0x24,
                                                 minval=0, maxval=127)

        self._printer = config.get_printer()
        gcode         = self._printer.lookup_object('gcode')
        gcode.register_command(
            'NFC_GATE_STATUS', self.cmd_NFC_GATE_STATUS,
            desc="Report spool state for all configured NFC gates")

        log_file = config.get('log_file', '')
        if log_file:
            try:
                configure(log_file)
            except Exception as e:
                import logging
                logging.getLogger().warning(
                    "nfc_gate: could not open log file %r: %s", log_file, e)

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

        spoolman_url       = config.get('spoolman_url',
                                        d.spoolman_url if d else '')
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
                debug=self._debug)
            logger.info("nfc_gate: [%s] Spoolman enabled — url=%s rfid_key=%s",
                         self._name, spoolman_url, spoolman_rfid_key)
        else:
            self._spoolman = None
            logger.warning(
                "nfc_gate: [%s] spoolman_url not set — set spoolman_url in "
                "[nfc_gate] or [nfc_gate %s].", self._name, self._name)

        default_i2c_addr = d.i2c_address if d else 0x24
        i2c = bus_module.MCU_I2C_from_config(config,
                                              default_addr=default_i2c_addr,
                                              default_speed=100000)

        self._reader     = PN532Driver(i2c, self._gate,
                                       transceive_delay, crc_delay,
                                       self._debug)
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

        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.printer.register_event_handler('klippy:disconnect',
                                            self._handle_disconnect)

    def _cmd_NFC_GATE_STATUS_fallback(self, gcmd):
        gcmd.respond_info('\n'.join(_lane_status_lines(self.printer)))

    def _handle_connect(self):
        logger.info(
            "nfc_gate: [%s] connected — gate=%d, poll=%.0fs, "
            "absent_threshold=%d, debug=%d",
            self._name, self._gate, self._poll_interval,
            self._absent_threshold, self._debug)
        # Allow the PN532 time to complete its power-on reset before the first
        # I2C transaction.  The PN532 datasheet specifies up to 1 s power-on
        # reset time; 2 s is conservative and harmless.
        time.sleep(2.0)

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

        if self._failed:
            if self._debug >= 2:
                logger.debug("nfc_gate: [%s] init failed — polling thread "
                              "will NOT start", self._name)
        else:
            if self._debug >= 2:
                logger.debug("nfc_gate: [%s] init OK — starting polling thread "
                              "(interval=%.0fs)", self._name, self._poll_interval)
            self._stop_event.clear()
            if not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._poll_loop,
                    name='nfc-gate-%s' % self._name,
                    daemon=True)
                self._thread.start()

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

        log_file = config.get('log_file', '')
        if log_file:
            configure(log_file)

        spoolman_url       = config.get('spoolman_url', '')
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
                debug=self._debug)
            logger.info("nfc_gates: Spoolman enabled — url=%s rfid_key=%s",
                         spoolman_url, spoolman_rfid_key)
        else:
            self._spoolman = None
            logger.warning(
                "nfc_gates: spoolman_url not set — gates will report UIDs "
                "but cannot resolve spool IDs.  Add spoolman_url to [nfc_gates].")

        i2c_addrs_str = config.get('gate_i2c_addresses', '')
        if i2c_addrs_str:
            bus_objects   = self._setup_i2c(config, i2c_addrs_str)
            self._readers = [PN532Driver(b, i, transceive_delay, debug=self._debug)
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
            if self._reader_failed[i]:
                if self._debug >= 2:
                    logger.debug("nfc_gates: gate %d skipped (reader failed)", i)
                continue

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

    cmd_NFC_GATE_STATUS_help = (
        "Report current NFC gate spool assignments (host-side state mirror)")

    def cmd_NFC_GATE_STATUS(self, gcmd):
        lines = [
            "NFC gate status — %d gates, poll %.0fs, absent threshold %d:"
            % (self._gate_count, self._poll_interval, self._absent_threshold)
        ]
        for i, state in enumerate(self._states):
            if self._reader_failed[i]:
                lines.append("  Gate %d: READER FAILED (check wiring)" % i)
            elif state.current_spool is not None:
                lines.append("  Gate %d: spool %-6d  UID %s"
                             % (i, state.current_spool, state.current_uid))
            elif state.current_uid is not None:
                lines.append("  Gate %d: tag %s (UID not in Spoolman)"
                             % (i, state.current_uid))
            else:
                lines.append("  Gate %d: empty" % i)
        gcmd.respond_info('\n'.join(lines))
