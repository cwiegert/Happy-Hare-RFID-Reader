# klippy/extras/nfc_gates/__init__.py
#
# EMU NFC Gate Reader — Klipper extras package entry point
#
# Polls 1–8 RC522 or PN532 NFC readers attached to a CAN-connected MCU.
# No custom MCU C code is required.
#
# Integration model — UID lookup via Spoolman
# ─────────────────────────────────────────────────────────
# Tags are NEVER written to.  Stick a blank NFC tag on each spool.
# Scan the tag's UID with your phone and paste it into the "rfid" extra
# field on the matching spool record in Spoolman.  When a tag is presented
# the reader reads only the UID (the minimum possible NFC operation), then
# this module queries the Spoolman REST API to find which spool carries that UID.
#
#   tag (blank)  →  RC522/PN532 reads UID  →  Spoolman API lookup  →  spool_id
#                                                                           │
#                                                           MMU_GATE_MAP GATE=N SPOOLMAN_ID=X
#
# Spoolman setup (one-time, per spool)
# ─────────────────────────────────────
# 1. In Spoolman: Settings → Extra fields → Spool → Add field
#      Field name: rfid    Type: Text
# 2. For each spool: open the spool record, set "rfid" to the tag UID
#    (uppercase hex, no separators — exactly as NFC_GATE_STATUS reports it,
#     e.g.  A3F200CC ).
#    You can read the UID with the NFC Tools app (Android/iOS) — you only need
#    to read the UID, not write anything to the tag.
#
# Architecture (SPI / RC522 path)
# ────────────────────────────────
# The Pico runs the standard Klipper MCU firmware (compiled with CAN support).
# That firmware already understands SPI commands (config_spi / spi_transfer /
# spi_send) which Klipper's MCU_SPI class sends over the CAN bus.  This module
# runs inside the klippy host process on the Raspberry Pi, uses MCU_SPI to
# talk to the RC522 readers on the Pico's SPI1 bus, and dispatches GCode macros
# to Happy Hare when spool tags change.
#
#   [RC522 readers]
#        │ SPI
#   [Pico: standard Klipper MCU firmware]
#        │ CAN bus - SN65HVD230 CAN Bus Transceiver connected to gpio4/gpio5 and CAN H/ CAN L
#   [Pi: klippy + this module (nfc_gates)]
#        │ GCode macros
#   [Happy Hare]
#
# Configuration (see config/nfc_gates_spi_rc522.cfg):
#
#   [mcu nfc_pico]
#   canbus_uuid: <uuid from canbus_query.py>
#
#   [nfc_gates]
#   cs_pin:           nfc_pico:gpio0      # Gate 0 CS (also selects SPI bus type)
#   extra_cs_pins:    nfc_pico:gpio1, nfc_pico:gpio2, nfc_pico:gpio3, nfc_pico:gpio4
#   spi_bus:          spi1                # Hardware SPI1: SCK=GP10 MOSI=GP11 MISO=GP8
#   spi_speed:        1000000
#   spoolman_url:     http://192.168.1.50:7912
#   poll_interval:    30
#   absent_threshold: 3
#
# Threading model
# ───────────────
# A dedicated background thread sleeps poll_interval seconds between NFC
# cycles.  SPI transactions (via MCU_SPI.spi_transfer) block the background
# thread while awaiting MCU responses; the Klipper reactor thread continues
# processing events normally during this time.  GCode macros are dispatched
# back into the reactor thread via reactor.register_callback().
#
# Install -- CDW 03/17/2026 -- update this section to account for the pn532
# ───────
# 1. Copy this directory to ~/klipper/klippy/extras/nfc_gates/
# 2. Add [include nfc_gates_spi_rc522.cfg] to printer.cfg
# 3. Restart Klipper: sudo systemctl restart klipper

import threading

from .log import logger, configure as _configure_log

import extras.bus as bus_module

from .rc522_driver      import RC522Driver
from .pn532_driver      import PN532Driver
from .gate_state        import GateState
from .klipper_interface import KlipperInterface
from .spoolman_client   import SpoolmanClient


class NfcGateManager:
    """
    Main orchestrator: creates MCU_SPI/MCU_I2C objects, reader drivers, and
    gate state machines; manages the background polling thread.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        ppins        = self.printer.lookup_object('pins')

        # ── Config ───────────────────────────────────────────────────────────
        self._poll_interval     = config.getfloat('poll_interval', 30.,
                                                   minval=1., maxval=3600.)
        self._absent_threshold  = config.getint('absent_threshold', 3,
                                                 minval=1, maxval=255)
        transceive_delay = config.getfloat('transceive_delay', 0.035,
                                            minval=0.001, maxval=1.0)
        # debug: 0=off, 1=major events (reads/writes/connections), 2=full trace
        self._debug      = config.getint('debug', 1, minval=0, maxval=2)
        log_file = config.get('log_file', '')
        if log_file:
            _configure_log(log_file)

        # ── Spoolman integration ──────────────────────────────────────────────
        spoolman_url      = config.get('spoolman_url', '')
        spoolman_rfid_key = config.get('spoolman_rfid_key', 'rfid')
        spoolman_timeout  = config.getfloat('spoolman_timeout', 5.0,
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

        # ── Interface mode: I2C (PN532) or SPI (RC522) ───────────────────────
        # Presence of  gate_i2c_addresses  selects I2C/PN532 mode.
        # Absence falls through to the existing SPI/RC522 path unchanged.
        i2c_addrs_str = config.get('gate_i2c_addresses', '')

        if i2c_addrs_str:
            bus_objects = self._setup_i2c(config, i2c_addrs_str)
            self._readers = [PN532Driver(bus_obj, i, transceive_delay,
                                         debug=self._debug)
                             for i, bus_obj in enumerate(bus_objects)]
        else:
            bus_objects = self._setup_spi(config, ppins)
            self._readers = [RC522Driver(bus_obj, i, transceive_delay,
                                          debug=self._debug)
                             for i, bus_obj in enumerate(bus_objects)]

        self._gate_count = len(bus_objects)
        if self._gate_count < 1 or self._gate_count > 8:
            raise config.error(
                "nfc_gates: gate count must be 1–8; got %d"
                % self._gate_count)

        # ── Per-gate state machines ───────────────────────────────────────────
        self._states  = [GateState(i, self._absent_threshold)
                         for i in range(self._gate_count)]
        self._reader_failed = [False] * self._gate_count

        # ── Klipper GCode bridge ─────────────────────────────────────────────
        self._klipper = KlipperInterface(self.printer, self.reactor)

        # ── Background polling thread ────────────────────────────────────────
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._poll_loop, name='nfc-gates', daemon=True)

        # ── GCode command ────────────────────────────────────────────────────
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command(
            'NFC_GATE_STATUS', self.cmd_NFC_GATE_STATUS,
            desc=self.cmd_NFC_GATE_STATUS_help)

        # ── Lifecycle handlers ───────────────────────────────────────────────
        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.printer.register_event_handler('klippy:disconnect',
                                            self._handle_disconnect)

    # ─────────────────────────────────────────────────────────────────────────
    # Interface setup helpers (called once from __init__)
    # ─────────────────────────────────────────────────────────────────────────

    def _setup_spi(self, config, ppins):
        """
        Create one MCU_SPI object per gate, sharing the same SPI bus.
        Gate 0's MCU_SPI is built via MCU_SPI.lookup() which reads cs_pin,
        spi_bus / spi_software_* and spi_speed from the config section.
        Additional gates use extra_cs_pins and share all bus parameters.
        """
        spi_speed   = config.getint('spi_speed', 1000000, minval=100000)
        primary_spi = bus_module.MCU_SPI.lookup(config, default_speed=spi_speed)
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
        """
        Create one MCU_I2C object per gate, each with its own I2C address.
        gate_i2c_addresses is a comma-separated list of hex or decimal addresses.
        All gates share the same I2C bus and speed.
        """
        try:
            addrs = [int(a.strip(), 0)
                     for a in addrs_str.split(',') if a.strip()]
        except ValueError as e:
            raise config.error(
                "nfc_gates: gate_i2c_addresses parse error: %s" % e)

        i2c_speed = config.getint('i2c_speed', 400000, minval=10000)

        primary_i2c = bus_module.MCU_I2C.lookup(config, default_addr=addrs[0],
                                                  default_speed=i2c_speed)
        self._mcu   = primary_i2c._mcu

        all_i2cs = [primary_i2c]
        for addr in addrs[1:]:
            all_i2cs.append(bus_module.MCU_I2C(
                self._mcu,
                primary_i2c._bus,
                addr,
                i2c_speed,
            ))
        return all_i2cs

    # ─────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_connect(self):
        """Called after klippy:connect — MCU_SPI/MCU_I2C objects are ready to use."""
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

        logger.info("nfc_gates: %d/%d readers initialised", ok_count,
                     self._gate_count)

        self._stop_event.clear()
        if not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._poll_loop, name='nfc-gates', daemon=True)
            self._thread.start()

    def _handle_disconnect(self):
        """Stop the polling thread cleanly on klippy disconnect."""
        self._stop_event.set()

    # ─────────────────────────────────────────────────────────────────────────
    # Background polling loop
    # ─────────────────────────────────────────────────────────────────────────

    def _poll_loop(self):
        """
        Background thread: polls all gates every poll_interval seconds.

        The first poll begins immediately on startup (before the first sleep)
        so the host learns the current gate state as soon as Klipper connects.
        """
        logger.info("nfc_gates: polling thread started")
        while not self._stop_event.is_set():
            try:
                self._poll_all_gates()
            except Exception:
                logger.exception("nfc_gates: unexpected error in poll cycle")

            self._stop_event.wait(timeout=self._poll_interval)

        logger.info("nfc_gates: polling thread stopped")

    def _poll_all_gates(self):
        """Read each gate and dispatch events for any that changed."""
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

            # ── Spoolman lookup (only when UID is new or changed) ─────────────
            if uid_hex is not None:
                if uid_hex == self._states[i].current_uid:
                    # Same tag still present — reuse cached state, no API call
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
                                 "(uid=%s spool=%s)",
                                 i, event_type, uid, spool)
                self._klipper.dispatch(event_type, gate, uid, spool)
            elif self._debug >= 2:
                logger.debug("nfc_gates: gate %d — no state change (%r)",
                              i, self._states[i])

    # ─────────────────────────────────────────────────────────────────────────
    # GCode command: NFC_GATE_STATUS
    # ─────────────────────────────────────────────────────────────────────────

    cmd_NFC_GATE_STATUS_help = (
        "Report current NFC gate spool assignments (host-side state mirror)")

    def cmd_NFC_GATE_STATUS(self, gcmd):
        """
        NFC_GATE_STATUS
        Shows the most recently polled state of every gate.
        This reflects the last poll result, not a live SPI read.
        """
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
                lines.append("  Gate %d: tag %s (UID not in Spoolman — "
                             "set the 'rfid' field on the spool record)"
                             % (i, state.current_uid))
            else:
                lines.append("  Gate %d: empty" % i)
        gcmd.respond_info('\n'.join(lines))


def load_config(config):
    return NfcGateManager(config)
