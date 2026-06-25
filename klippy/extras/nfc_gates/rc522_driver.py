# klippy/extras/nfc_gates/rc522_driver.py
#
# EMU NFC Gate Reader — RC522 SPI driver (UID-only)
# Version 1.0.0  |  2026-04-14
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ─────────────────────────────────────────────────────────────────────────────
# RC522 NFC reader driver — communicates with the RC522 chip over SPI using
# Klipper's MCU_SPI interface.
#
# Integration model
# ─────────────────
# This driver uses Approach B (UID lookup): it reads only the tag's factory
# UID — the simplest possible NFC operation.  No data is read from tag memory;
# tags never need to be written to.  The UID is passed up to the gate manager,
# which queries the Spoolman API to resolve it to a spool ID.
#
# ISO14443A UID read sequence used here
# ──────────────────────────────────────
# Only two stages are needed to obtain the UID:
#   Stage 1  REQA   — broadcast to wake idle tags; expect 16-bit ATQA response.
#   Stage 2  ANTICOLL — returns the 4-byte UID + XOR checksum byte.
#
# SELECT (stage 3), memory READ, and MIFARE authentication are intentionally
# omitted in this UID-only driver because:
#   - SELECT requires hardware CRC, adding two extra SPI round-trips per poll.
#   - Memory reads add further SPI traffic and are completely unnecessary.
# Skipping these stages also means the _calc_crc() helper is no longer needed.
#
# Threading notes:
#   All methods are designed to be called from a dedicated background thread
#   (not the Klipper reactor thread).  spi_send() and spi_transfer() route
#   commands to the MCU over CAN; the background thread blocks on each
#   response while the reactor continues processing other events normally.


import time
import traceback

from .log import logger

# ─────────────────────────────────────────────────────────────────────────────
# RC522 register addresses
# ─────────────────────────────────────────────────────────────────────────────

_CommandReg     = 0x01
_ComIEnReg      = 0x02
_ComIrqReg      = 0x04
_ErrorReg       = 0x06
_FIFODataReg    = 0x09
_FIFOLevelReg   = 0x0A
_ControlReg     = 0x0C
_BitFramingReg  = 0x0D
_ModeReg        = 0x11
_TxControlReg   = 0x14
_TxASKReg       = 0x15
_TModeReg       = 0x2A
_TPrescalerReg  = 0x2B
_TReloadRegH    = 0x2C
_TReloadRegL    = 0x2D

# RC522 PCD (reader chip) commands
_PCD_IDLE       = 0x00
_PCD_TRANSCEIVE = 0x0C
_PCD_RESETPHASE = 0x0F

# PICC (tag) commands
_PICC_REQIDL    = 0x26   # Request idle — wake tags in the RF field
_PICC_ANTICOLL  = 0x93   # Anti-collision command byte

# Operation results
MI_OK  = 0
MI_ERR = 1

# Human-readable register names used in debug=2 trace output
_REG_NAMES = {
    _CommandReg:    'CommandReg',
    _ComIEnReg:     'ComIEnReg',
    _ComIrqReg:     'ComIrqReg',
    _ErrorReg:      'ErrorReg',
    _FIFODataReg:   'FIFODataReg',
    _FIFOLevelReg:  'FIFOLevelReg',
    _ControlReg:    'ControlReg',
    _BitFramingReg: 'BitFramingReg',
    _ModeReg:       'ModeReg',
    _TxControlReg:  'TxControlReg',
    _TxASKReg:      'TxASKReg',
    _TModeReg:      'TModeReg',
    _TPrescalerReg: 'TPrescalerReg',
    _TReloadRegH:   'TReloadRegH',
    _TReloadRegL:   'TReloadRegL',
}
_REG_BY_NAME = dict((v.lower(), k) for k, v in _REG_NAMES.items())
_REG_BY_NAME.update(dict((v.lower().replace('reg', ''), k)
                         for k, v in _REG_NAMES.items()))

class RC522Driver:
    """
    Driver for one RC522 NFC reader module.

    Reads only the tag UID (Approach B — UID lookup via Spoolman).
    No SELECT, CRC, or memory READ operations are performed.

    Parameters
    ----------
    spi : MCU_SPI
        A Klipper MCU_SPI object configured for this reader's CS pin.
        Must be fully initialised (klippy:connect completed) before calling
        init() or read_tag().
    gate : int
        Gate number (0-based), used only for logging.
    transceive_delay : float
        Seconds to wait after triggering TRANSCEIVE before reading the result.
        The RC522 internal timer fires at ~0.5 ms when no tag is present;
        35 ms gives tags (which respond in <2 ms) ample time while CAN
        round-trips add negligible overhead at 30-second poll intervals.
    debug : int
        0 = silent, 1 = major events, 2 = full trace.
    """

    def __init__(self, spi, gate,
                 transceive_delay=0.035,
                 debug=0,
                 sleep_fn=None):
        self._spi              = spi
        self._gate             = gate
        self._transceive_delay = transceive_delay
        self._debug            = debug
        self._sleep            = sleep_fn if sleep_fn is not None else time.sleep
        self._clear_current_card()

    def _clear_current_card(self):
        self.current_target = None
        self.current_uid = None
        self.current_uid_hex = ''
        self.current_target_info = None

    def _set_current_card(self, target_info):
        self.current_target_info = dict(target_info)
        self.current_target = target_info.get('target')
        self.current_uid = list(target_info.get('uid_bytes') or [])
        self.current_uid_hex = target_info.get('uid', '')

    # ─────────────────────────────────────────────────────────────────────────
    # Register read / write (one SPI transaction each, CS toggled by MCU_SPI)
    # ─────────────────────────────────────────────────────────────────────────

    def _write(self, reg, val):
        """Write one byte to an RC522 register (no response expected)."""
        if self._debug >= 4:
            logger.debug("RC522: gate %d  W %-15s (0x%02X) = 0x%02X",
                          self._gate, _REG_NAMES.get(reg, '?'), reg, val & 0xFF)
        self._spi.spi_send([(reg << 1) & 0x7E, val & 0xFF])

    def _read(self, reg):
        """Read one byte from an RC522 register and return it as an integer."""
        resp = self._spi.spi_transfer([((reg << 1) & 0x7E) | 0x80, 0x00])
        val = resp['response'][1]
        if self._debug >= 4:
            logger.debug("RC522: gate %d  R %-15s (0x%02X) -> 0x%02X",
                          self._gate, _REG_NAMES.get(reg, '?'), reg, val)
        return val

    # ─────────────────────────────────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────────────────────────────────

    def init(self):
        """
        Soft-reset the RC522 and configure it for 13.56 MHz ISO14443A operation.
        Must be called once after klippy:connect, before the first read_tag().
        """
        try:
            if self._debug >= 4:
                logger.debug("RC522: gate %d init — soft-resetting", self._gate)
            self._write(_CommandReg,    _PCD_RESETPHASE)
            self._sleep(0.050)           # Datasheet: max reset time 37.74 ms; 50 ms is safe
            if self._debug >= 4:
                logger.debug("RC522: gate %d init — reset done, configuring timer "
                             "and modulation", self._gate)
            self._write(_TModeReg,      0x8D)
            self._write(_TPrescalerReg, 0x3E)
            self._write(_TReloadRegH,   0x00)
            self._write(_TReloadRegL,   0x1E)
            self._write(_TxASKReg,      0x40)
            self._write(_ModeReg,       0x3D)
            # Enable antenna TX pins (bits 0-1 of TxControlReg)
            tx = self._read(_TxControlReg)
            if not (tx & 0x03):
                if self._debug >= 4:
                    logger.debug("RC522: gate %d init — enabling antenna TX pins "
                                 "(TxControl was 0x%02X)", self._gate, tx)
                self._write(_TxControlReg, tx | 0x03)
            tx_final = self._read(_TxControlReg)
            logger.info("RC522: gate %d init OK (TxControl=0x%02X)",
                        self._gate, tx_final)
        except Exception as e:
            logger.warning(
                "RC522: gate %d init failed — check SPI wiring, cs_pin, "
                "spi_bus/software SPI pins, power, and ground: %s",
                self._gate, e)
            if self._debug >= 4:
                logger.debug("RC522: gate %d init traceback:\n%s",
                             self._gate, traceback.format_exc())
            raise

    def is_alive(self):
        """Return True if the reader is responding (antenna TX bits are set)."""
        try:
            tx = self._read(_TxControlReg)
            alive = bool(tx & 0x03)
            if not alive:
                logger.warning(
                    "RC522: gate %d not responding — antenna TX bits are off "
                    "(TxControl=0x%02X)", self._gate, tx)
            elif self._debug >= 4:
                logger.debug("RC522: gate %d alive (TxControl=0x%02X)",
                             self._gate, tx)
            return alive
        except Exception as e:
            logger.warning(
                "RC522: gate %d health check failed — SPI reader did not "
                "respond: %s", self._gate, e)
            if self._debug >= 4:
                logger.debug("RC522: gate %d is_alive traceback:\n%s",
                             self._gate, traceback.format_exc())
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # FIFO transceive
    # ─────────────────────────────────────────────────────────────────────────

    def _transceive(self, send_data, timeout=None):
        """
        Load send_data into the RC522 FIFO, trigger TRANSCEIVE, wait
        transceive_delay for a tag response, then return the received bytes.

        Returns (MI_OK, data_bytes, bit_length) on success,
                (MI_ERR, [], 0) on timeout, collision, or protocol error.
        """
        if self._debug >= 4:
            logger.debug("RC522: gate %d  _transceive send=[%s]",
                          self._gate,
                          ' '.join('0x%02X' % b for b in send_data))

        # Enable all interrupt sources; clear pending flags; flush FIFO
        self._write(_ComIEnReg,    self._read(_ComIEnReg) | 0x80)
        self._write(_ComIrqReg,    self._read(_ComIrqReg) & 0x7F)
        self._write(_FIFOLevelReg, self._read(_FIFOLevelReg) | 0x80)
        self._write(_CommandReg,   _PCD_IDLE)

        # Load data into FIFO
        for byte in send_data:
            self._write(_FIFODataReg, byte)

        # Start transmission
        self._write(_CommandReg,    _PCD_TRANSCEIVE)
        self._write(_BitFramingReg, self._read(_BitFramingReg) | 0x80)  # StartSend

        delay = self._transceive_delay if timeout is None else max(
            0.0, min(float(timeout), self._transceive_delay))
        if self._debug >= 4:
            logger.debug("RC522: gate %d  _transceive — transmission started, "
                          "waiting %.0f ms for response",
                          self._gate, delay * 1000)

        # Wait for tag response (or internal timer timeout at ~0.5 ms)
        self._sleep(delay)

        # Clear StartSend
        self._write(_BitFramingReg, self._read(_BitFramingReg) & 0x7F)

        irq = self._read(_ComIrqReg)
        if self._debug >= 4:
            logger.debug("RC522: gate %d  _transceive IRQ=0x%02X "
                          "(TimerIRq=%d RxIRq=%d IdleIRq=%d)",
                          self._gate, irq,
                          (irq >> 0) & 1, (irq >> 5) & 1, (irq >> 4) & 1)

        # TimerIRq (bit 0) set with no RxIRq (bit 5) or IdleIRq (bit 4) → no tag
        if (irq & 0x01) and not (irq & 0x30):
            if self._debug >= 4:
                logger.debug("RC522: gate %d  _transceive -> MI_ERR (timer "
                              "expired, no tag response)", self._gate)
            return MI_ERR, [], 0

        # Protocol error (collision, CRC error, buffer overflow, parity error)
        err = self._read(_ErrorReg)
        if err & 0x1B:
            if self._debug >= 4:
                logger.debug("RC522: gate %d  _transceive -> MI_ERR "
                              "(ErrorReg=0x%02X: collision=%d CRC=%d overflow=%d "
                              "parity=%d)",
                              self._gate, err,
                              (err >> 3) & 1, (err >> 2) & 1,
                              (err >> 4) & 1, (err >> 1) & 1)
            return MI_ERR, [], 0

        # Read received bytes from FIFO
        fifo_len = self._read(_FIFOLevelReg)
        if fifo_len == 0:
            if self._debug >= 4:
                logger.debug("RC522: gate %d  _transceive -> MI_ERR "
                              "(FIFO empty after IRQ)", self._gate)
            return MI_ERR, [], 0

        last_bits = self._read(_ControlReg) & 0x07
        bit_len = (fifo_len - 1) * 8 + last_bits if last_bits else fifo_len * 8

        if fifo_len > 16:
            fifo_len = 16
        back_data = [self._read(_FIFODataReg) for _ in range(fifo_len)]

        if self._debug >= 4:
            logger.debug("RC522: gate %d  _transceive -> MI_OK "
                          "fifo=%d bits=%d data=[%s]",
                          self._gate, fifo_len, bit_len,
                          ' '.join('0x%02X' % b for b in back_data))

        return MI_OK, back_data, bit_len

    # ─────────────────────────────────────────────────────────────────────────
    # UID read — REQA + ANTICOLL only (Approach B)
    # ─────────────────────────────────────────────────────────────────────────

    def read_target(self, timeout=None):
        """
        Attempt to read the UID of any ISO14443A tag in the RF field.

        Performs only REQA and ANTICOLL — the minimum needed to retrieve the
        first cascade-level UID. SELECT and memory READ are intentionally
        skipped; no CRC calculation is required.

        Returns
        -------
        dict
            UID-only target information. The protocol is explicitly marked
            uid_only so tag_handler will not attempt rich memory reads.
        None
            No tag in the RF field, or a communication error occurred.
        """
        try:
            return self._read_target_once(timeout=timeout)
        except Exception as e:
            logger.warning(
                "RC522: gate %d UID read failed — check SPI wiring and reader "
                "state: %s", self._gate, e)
            if self._debug >= 4:
                logger.debug("RC522: gate %d read_target traceback:\n%s",
                             self._gate, traceback.format_exc())
            self._clear_current_card()
            return None

    def _read_target_once(self, timeout=None):
        if self._debug >= 4:
            logger.debug("RC522: gate %d read_target — begin", self._gate)

        # ── Stage 1: REQA ────────────────────────────────────────────────────
        if self._debug >= 4:
            logger.debug("RC522: gate %d read_target — stage 1: REQA", self._gate)
        self._write(_BitFramingReg, 0x07)   # 7-bit frame
        status, data, bits = self._transceive([_PICC_REQIDL], timeout=timeout)
        if status != MI_OK or bits != 0x10:  # Expect 16-bit ATQA
            self._clear_current_card()
            if self._debug >= 4:
                logger.debug("RC522: gate %d read_target — REQA failed "
                              "(status=%s bits=%d), no tag",
                              self._gate, 'OK' if status == MI_OK else 'ERR', bits)
            return None

        if self._debug >= 4:
            logger.debug("RC522: gate %d read_target — REQA OK (ATQA bits=%d)",
                          self._gate, bits)
        atqa_bytes = list(data[:2])
        atqa = ((atqa_bytes[0] << 8) | atqa_bytes[1]
                if len(atqa_bytes) >= 2 else 0)

        # ── Stage 2: Anti-collision ───────────────────────────────────────────
        if self._debug >= 4:
            logger.debug("RC522: gate %d read_target — stage 2: ANTICOLL",
                          self._gate)
        self._write(_BitFramingReg, 0x00)
        status, data, bits = self._transceive([_PICC_ANTICOLL, 0x20],
                                              timeout=timeout)
        if status != MI_OK or len(data) < 5:
            self._clear_current_card()
            logger.warning(
                "RC522: gate %d ANTICOLL failed after REQA "
                "(status=%s data_len=%d)",
                self._gate, 'OK' if status == MI_OK else 'ERR', len(data))
            if self._debug >= 4:
                logger.debug("RC522: gate %d ANTICOLL response bits=%d data=[%s]",
                             self._gate, bits,
                             ' '.join('0x%02X' % b for b in data))
            return None

        # Verify XOR checksum over 4 UID bytes
        chk = data[0] ^ data[1] ^ data[2] ^ data[3]
        if chk != data[4]:
            self._clear_current_card()
            logger.warning(
                "RC522: gate %d ANTICOLL UID checksum mismatch "
                "(calc=0x%02X got=0x%02X)", self._gate, chk, data[4])
            if self._debug >= 4:
                logger.debug("RC522: gate %d ANTICOLL checksum data=[%s]",
                             self._gate,
                             ' '.join('0x%02X' % b for b in data))
            return None

        uid_bytes = list(data[:4])
        uid_hex = "{:02X}{:02X}{:02X}{:02X}".format(*data[:4])
        target_info = {
            'reader': 'rc522',
            'protocol': 'uid_only',
            'protocol_name': 'ISO14443A_UID_ONLY',
            'target': 1,
            'tg': 1,
            'uid': uid_hex,
            'uid_bytes': uid_bytes,
            'uid_length': len(uid_bytes),
            'sak': None,
            'sens_res': atqa,
            'atqa': atqa,
            'sens_res_bytes': atqa_bytes,
        }
        self._set_current_card(target_info)

        if self._debug >= 4:
            logger.debug("RC522: gate %d read_target — uid=%s",
                         self._gate, uid_hex)
        elif self._debug >= 3:
            logger.info("RC522: gate %d UID-only target uid=%s ATQA=0x%04X",
                        self._gate, uid_hex, atqa)

        return target_info

    def read_tag(self, timeout=None):
        """Read and return an uppercase UID string, or None if no tag is present."""
        try:
            target_info = self.read_target(timeout=timeout)
            if target_info is None:
                return None
            return target_info.get('uid')
        except Exception as e:
            if self._debug >= 3:
                logger.info("RC522: gate %d read_tag error: %s\n%s",
                            self._gate, e, traceback.format_exc())
            self._clear_current_card()
            return None

    def _release_current_target(self, reason="manual"):
        """Clear cached UID state; RC522 UID-only reads do not select a target."""
        if self._debug >= 4:
            logger.debug("RC522: gate %d release target reason=%s",
                         self._gate, reason)
        self._clear_current_card()

    # ─────────────────────────────────────────────────────────────────────────
    # Low-level debug helpers
    # ─────────────────────────────────────────────────────────────────────────

    def low_level_reg_read(self, reg):
        return self._read(_rc522_reg_value(reg))

    def low_level_reg_write(self, reg, value):
        reg_value = _rc522_reg_value(reg)
        self._write(reg_value, int(value) & 0xFF)
        return self._read(reg_value)

    def low_level_dump_registers(self):
        regs = [
            _CommandReg, _ComIEnReg, _ComIrqReg, _ErrorReg,
            _FIFOLevelReg, _ControlReg, _BitFramingReg, _ModeReg,
            _TxControlReg, _TxASKReg, _TModeReg, _TPrescalerReg,
            _TReloadRegH, _TReloadRegL,
        ]
        return [(reg, _REG_NAMES.get(reg, '0x%02X' % reg), self._read(reg))
                for reg in regs]

    def low_level_antenna(self, enable=None):
        before = self._read(_TxControlReg)
        after = before
        if enable is not None:
            if enable:
                after = before | 0x03
            else:
                after = before & ~0x03
            self._write(_TxControlReg, after)
            after = self._read(_TxControlReg)
        return before, after, bool(after & 0x03)

    def low_level_fifo_transceive(self, data, bit_framing=0x00, timeout=None):
        self._write(_BitFramingReg, int(bit_framing) & 0x07)
        return self._transceive(list(data), timeout=timeout)

    def low_level_tag_wake(self, timeout=None):
        return self.low_level_fifo_transceive(
            [_PICC_REQIDL], bit_framing=0x07, timeout=timeout)


# =============================================================================
# RC522 low-level debug command helpers
# =============================================================================

def _rc522_reg_value(value):
    if isinstance(value, int):
        reg = value
    else:
        token = str(value or '').strip().strip('"\'').lower()
        if token.startswith('0x'):
            reg = int(token, 16)
        elif token in _REG_BY_NAME:
            reg = _REG_BY_NAME[token]
        else:
            reg = int(token, 16 if any(c in token for c in 'abcdef') else 10)
    if reg < 0 or reg > 0x3F:
        raise ValueError("RC522 register out of range: %s" % value)
    return reg


def _rc522_parse_hex_bytes(value):
    value = str(value or '').replace(',', ' ').replace(':', ' ').replace('-', ' ')
    data = []
    for token in value.split():
        token = token.strip().strip('"\'')
        if not token:
            continue
        if token.lower().startswith('0x'):
            token = token[2:]
        data.append(int(token, 16) & 0xFF)
    return data


def _rc522_parse_byte(value):
    token = str(value or '').strip().strip('"\'')
    if token.lower().startswith('0x'):
        return int(token, 16) & 0xFF
    return int(token, 16 if any(c in token.lower() for c in 'abcdef') else 10) & 0xFF


def _rc522_hex(data):
    return ' '.join('%02X' % (b & 0xFF) for b in data)


def _rc522_response(gcmd, label, message):
    gcmd.respond_info("[%s]: %s" % (label, message))


def low_level_debug_requested(gcmd):
    return (
        gcmd.get_int("RC522_HELP", 0) or
        gcmd.get("RC522_REG_READ", None) is not None or
        gcmd.get("RC522_REG_WRITE", None) is not None or
        gcmd.get_int("RC522_DUMP_REGS", 0) or
        gcmd.get("RC522_ANTENNA", None) is not None or
        gcmd.get("RC522_TRANSCEIVE", None) is not None or
        gcmd.get_int("RC522_REQA", 0) or
        gcmd.get_int("RC522_WAKE", 0))


def low_level_debug_help_lines(command_base):
    return [
        "--- RC522 UID-only SPI debug ---",
        "%s INIT=1                         - normal RC522 init/antenna enable" % command_base,
        "%s SCAN=1                         - normal REQA + ANTICOLL UID scan" % command_base,
        "%s RC522_DUMP_REGS=1              - read key RC522 registers" % command_base,
        "%s RC522_REG_READ=TxControlReg    - read one register" % command_base,
        "%s RC522_REG_WRITE=TxControlReg VALUE=83 - write one register, then read back" % command_base,
        "%s RC522_ANTENNA=1                - enable antenna TX bits" % command_base,
        "%s RC522_ANTENNA=0                - disable antenna TX bits" % command_base,
        "%s RC522_REQA=1                   - tag wake probe (7-bit REQA)" % command_base,
        "%s RC522_TRANSCEIVE='93 20' BIT_FRAMING=0 - FIFO transceive raw bytes" % command_base,
    ]


def _rc522_require_reader(gcmd, reader, label):
    if not hasattr(reader, 'low_level_reg_read'):
        _rc522_response(gcmd, label, "reader does not support RC522 low-level debug")
        return False
    return True


def _rc522_report_transceive(gcmd, label, op, status, data, bits):
    status_text = "OK" if status == MI_OK else "ERR"
    _rc522_response(
        gcmd, label, "%s result: status=%s bits=%d data=%s" %
        (op, status_text, bits, _rc522_hex(data)))
    return status == MI_OK


def _rc522_optional_float(gcmd, name, minval=0.0, maxval=2.0):
    if gcmd.get(name, None) is None:
        return None
    return gcmd.get_float(name, minval=minval, maxval=maxval)


def run_low_level_debug(gcmd, reader, label, command_base, enabled):
    if not low_level_debug_requested(gcmd):
        return False
    if not enabled:
        _rc522_response(gcmd, label, "low_level_debug is disabled in config")
        return True
    if not _rc522_require_reader(gcmd, reader, label):
        return True

    if gcmd.get_int("RC522_HELP", 0):
        gcmd.respond_info('\n'.join(low_level_debug_help_lines(command_base)))
        return True

    reg_read = gcmd.get("RC522_REG_READ", None)
    if reg_read is not None:
        reg = _rc522_reg_value(reg_read)
        value = reader.low_level_reg_read(reg)
        _rc522_response(
            gcmd, label, "RC522_REG_READ %s (0x%02X) -> 0x%02X" %
            (_REG_NAMES.get(reg, '?'), reg, value))
        return True

    reg_write = gcmd.get("RC522_REG_WRITE", None)
    if reg_write is not None:
        value = _rc522_parse_byte(gcmd.get("VALUE", "0"))
        reg = _rc522_reg_value(reg_write)
        readback = reader.low_level_reg_write(reg, value)
        _rc522_response(
            gcmd, label,
            "RC522_REG_WRITE %s (0x%02X) = 0x%02X; readback=0x%02X" %
            (_REG_NAMES.get(reg, '?'), reg, value, readback))
        return True

    if gcmd.get_int("RC522_DUMP_REGS", 0):
        lines = ["[%s]: RC522 register dump" % label]
        for reg, name, value in reader.low_level_dump_registers():
            lines.append("  %-15s 0x%02X = 0x%02X" % (name, reg, value))
        gcmd.respond_info('\n'.join(lines))
        return True

    antenna_value = gcmd.get("RC522_ANTENNA", None)
    if antenna_value is not None:
        enable = bool(int(str(antenna_value).strip(), 0))
        before, after, enabled_state = reader.low_level_antenna(enable=enable)
        _rc522_response(
            gcmd, label,
            "RC522_ANTENNA before=0x%02X after=0x%02X enabled=%s" %
            (before, after, enabled_state))
        return True

    if gcmd.get_int("RC522_REQA", 0) or gcmd.get_int("RC522_WAKE", 0):
        timeout = _rc522_optional_float(gcmd, "TIMEOUT")
        status, data, bits = reader.low_level_tag_wake(timeout=timeout)
        if _rc522_report_transceive(gcmd, label, "RC522_REQA", status, data, bits):
            if bits == 16 and len(data) >= 2:
                atqa = (data[0] << 8) | data[1]
                _rc522_response(gcmd, label, "RC522_REQA ATQA=0x%04X" % atqa)
            else:
                _rc522_response(
                    gcmd, label,
                    "RC522_REQA expected 16 ATQA bits; got bits=%d" % bits)
        return True

    transceive = gcmd.get("RC522_TRANSCEIVE", None)
    if transceive is not None:
        data = _rc522_parse_hex_bytes(transceive)
        bit_framing = gcmd.get_int("BIT_FRAMING", 0, minval=0, maxval=7)
        timeout = _rc522_optional_float(gcmd, "TIMEOUT")
        status, response, bits = reader.low_level_fifo_transceive(
            data, bit_framing=bit_framing, timeout=timeout)
        _rc522_report_transceive(
            gcmd, label, "RC522_TRANSCEIVE", status, response, bits)
        return True

    gcmd.respond_info('\n'.join(low_level_debug_help_lines(command_base)))
    return True
