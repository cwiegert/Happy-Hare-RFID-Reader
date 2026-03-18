# klippy/extras/nfc_gates/rc522_driver.py
#
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
# SELECT (stage 3) and READ (stage 4) are intentionally omitted because:
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
                 debug=0):
        self._spi              = spi
        self._gate             = gate
        self._transceive_delay = transceive_delay
        self._debug            = debug

    # ─────────────────────────────────────────────────────────────────────────
    # Register read / write (one SPI transaction each, CS toggled by MCU_SPI)
    # ─────────────────────────────────────────────────────────────────────────

    def _write(self, reg, val):
        """Write one byte to an RC522 register (no response expected)."""
        if self._debug >= 2:
            logger.debug("nfc_gates: gate %d  W %-15s (0x%02X) = 0x%02X",
                          self._gate, _REG_NAMES.get(reg, '?'), reg, val & 0xFF)
        self._spi.spi_send([(reg << 1) & 0x7E, val & 0xFF])

    def _read(self, reg):
        """Read one byte from an RC522 register and return it as an integer."""
        resp = self._spi.spi_transfer([((reg << 1) & 0x7E) | 0x80, 0x00])
        val = resp['response'][1]
        if self._debug >= 2:
            logger.debug("nfc_gates: gate %d  R %-15s (0x%02X) -> 0x%02X",
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
        if self._debug >= 1:
            logger.info("nfc_gates: gate %d init — soft-resetting RC522", self._gate)
        self._write(_CommandReg,    _PCD_RESETPHASE)
        time.sleep(0.050)            # Datasheet: max reset time 37.74 ms; 50 ms is safe
        if self._debug >= 2:
            logger.debug("nfc_gates: gate %d init — reset done, configuring timer "
                          "and modulation", self._gate)
        self._write(_TModeReg,      0x8D)
        self._write(_TPrescalerReg, 0x3E)
        self._write(_TReloadRegH,   0x00)
        self._write(_TReloadRegL,   0x1E)
        self._write(_TxASKReg,      0x40)
        self._write(_ModeReg,       0x3D)
        # Enable antenna TX pins (bits 0–1 of TxControlReg)
        tx = self._read(_TxControlReg)
        if not (tx & 0x03):
            if self._debug >= 2:
                logger.debug("nfc_gates: gate %d init — enabling antenna TX pins "
                              "(TxControl was 0x%02X)", self._gate, tx)
            self._write(_TxControlReg, tx | 0x03)
        tx_final = self._read(_TxControlReg)
        if self._debug >= 1:
            logger.info("nfc_gates: gate %d RC522 init OK (TxControl=0x%02X)",
                         self._gate, tx_final)

    def is_alive(self):
        """Return True if the reader is responding (antenna TX bits are set)."""
        try:
            return bool(self._read(_TxControlReg) & 0x03)
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # FIFO transceive
    # ─────────────────────────────────────────────────────────────────────────

    def _transceive(self, send_data):
        """
        Load send_data into the RC522 FIFO, trigger TRANSCEIVE, wait
        transceive_delay for a tag response, then return the received bytes.

        Returns (MI_OK, data_bytes, bit_length) on success,
                (MI_ERR, [], 0) on timeout, collision, or protocol error.
        """
        if self._debug >= 2:
            logger.debug("nfc_gates: gate %d  _transceive send=[%s]",
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

        if self._debug >= 2:
            logger.debug("nfc_gates: gate %d  _transceive — transmission started, "
                          "waiting %.0f ms for response",
                          self._gate, self._transceive_delay * 1000)

        # Wait for tag response (or internal timer timeout at ~0.5 ms)
        time.sleep(self._transceive_delay)

        # Clear StartSend
        self._write(_BitFramingReg, self._read(_BitFramingReg) & 0x7F)

        irq = self._read(_ComIrqReg)
        if self._debug >= 2:
            logger.debug("nfc_gates: gate %d  _transceive IRQ=0x%02X "
                          "(TimerIRq=%d RxIRq=%d IdleIRq=%d)",
                          self._gate, irq,
                          (irq >> 0) & 1, (irq >> 5) & 1, (irq >> 4) & 1)

        # TimerIRq (bit 0) set with no RxIRq (bit 5) or IdleIRq (bit 4) → no tag
        if (irq & 0x01) and not (irq & 0x30):
            if self._debug >= 2:
                logger.debug("nfc_gates: gate %d  _transceive -> MI_ERR (timer "
                              "expired, no tag response)", self._gate)
            return MI_ERR, [], 0

        # Protocol error (collision, CRC error, buffer overflow, parity error)
        err = self._read(_ErrorReg)
        if err & 0x1B:
            if self._debug >= 2:
                logger.debug("nfc_gates: gate %d  _transceive -> MI_ERR "
                              "(ErrorReg=0x%02X: collision=%d CRC=%d overflow=%d "
                              "parity=%d)",
                              self._gate, err,
                              (err >> 3) & 1, (err >> 2) & 1,
                              (err >> 4) & 1, (err >> 1) & 1)
            return MI_ERR, [], 0

        # Read received bytes from FIFO
        fifo_len = self._read(_FIFOLevelReg)
        if fifo_len == 0:
            if self._debug >= 2:
                logger.debug("nfc_gates: gate %d  _transceive -> MI_ERR "
                              "(FIFO empty after IRQ)", self._gate)
            return MI_ERR, [], 0

        last_bits = self._read(_ControlReg) & 0x07
        bit_len = (fifo_len - 1) * 8 + last_bits if last_bits else fifo_len * 8

        if fifo_len > 16:
            fifo_len = 16
        back_data = [self._read(_FIFODataReg) for _ in range(fifo_len)]

        if self._debug >= 2:
            logger.debug("nfc_gates: gate %d  _transceive -> MI_OK "
                          "fifo=%d bits=%d data=[%s]",
                          self._gate, fifo_len, bit_len,
                          ' '.join('0x%02X' % b for b in back_data))

        return MI_OK, back_data, bit_len

    # ─────────────────────────────────────────────────────────────────────────
    # UID read — REQA + ANTICOLL only (Approach B)
    # ─────────────────────────────────────────────────────────────────────────

    def read_tag(self):
        """
        Attempt to read the UID of any tag in the RF field.

        Performs only REQA and ANTICOLL — the minimum needed to retrieve the
        4-byte UID.  SELECT and memory READ are intentionally skipped; no CRC
        calculation is required.

        Returns
        -------
        str
            8-character uppercase hex UID string (e.g. "A3F200CC").
        None
            No tag in the RF field, or a communication error occurred.
        """
        if self._debug >= 2:
            logger.debug("nfc_gates: gate %d read_tag — begin", self._gate)

        # ── Stage 1: REQA ────────────────────────────────────────────────────
        if self._debug >= 2:
            logger.debug("nfc_gates: gate %d read_tag — stage 1: REQA", self._gate)
        self._write(_BitFramingReg, 0x07)   # 7-bit frame
        status, data, bits = self._transceive([_PICC_REQIDL])
        if status != MI_OK or bits != 0x10:  # Expect 16-bit ATQA
            if self._debug >= 2:
                logger.debug("nfc_gates: gate %d read_tag — REQA failed "
                              "(status=%s bits=%d), no tag",
                              self._gate, 'OK' if status == MI_OK else 'ERR', bits)
            elif self._debug >= 1:
                logger.info("nfc_gates: gate %d — no tag detected", self._gate)
            return None

        if self._debug >= 2:
            logger.debug("nfc_gates: gate %d read_tag — REQA OK (ATQA bits=%d)",
                          self._gate, bits)

        # ── Stage 2: Anti-collision ───────────────────────────────────────────
        if self._debug >= 2:
            logger.debug("nfc_gates: gate %d read_tag — stage 2: ANTICOLL",
                          self._gate)
        self._write(_BitFramingReg, 0x00)
        status, data, bits = self._transceive([_PICC_ANTICOLL, 0x20])
        if status != MI_OK or len(data) < 5:
            if self._debug >= 2:
                logger.debug("nfc_gates: gate %d read_tag — ANTICOLL failed "
                              "(status=%s data_len=%d)",
                              self._gate, 'OK' if status == MI_OK else 'ERR',
                              len(data))
            return None

        # Verify XOR checksum over 4 UID bytes
        chk = data[0] ^ data[1] ^ data[2] ^ data[3]
        if chk != data[4]:
            if self._debug >= 2:
                logger.debug("nfc_gates: gate %d read_tag — ANTICOLL XOR checksum "
                              "mismatch (calc=0x%02X got=0x%02X)",
                              self._gate, chk, data[4])
            return None

        uid_hex = "{:02X}{:02X}{:02X}{:02X}".format(*data[:4])

        if self._debug >= 1:
            logger.info("nfc_gates: gate %d read_tag — uid=%s", self._gate, uid_hex)

        return uid_hex
