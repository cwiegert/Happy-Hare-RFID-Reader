# klippy/extras/nfc_gates/pn532_driver.py
#
# PN532 NFC reader driver — I2C variant, using Klipper's MCU_I2C.
#
# Drop-in replacement for rc522_driver.py.  The public interface is identical:
# init(), is_alive(), and read_tag() return the same types so NFCGateManager
# works unchanged regardless of which driver is selected.
#
# Integration model
# ─────────────────
# This driver uses UID lookup: it reads only the tag's factory
# UID — the simplest possible NFC operation.  No data is ever written to the
# tag.  The UID is passed up to the gate manager, which queries the Spoolman
# API to resolve it to a spool ID.  Tags can be blank NTAG stickers straight
# from the packet.
#
# Why PN532 over RC522 for I2C?
# ──────────────────────────────
# The PN532 implements the full ISO14443A stack in hardware.  One
# InListPassiveTarget command hands back the tag UID — no manual REQA /
# ANTICOLL / SELECT sequence required.  One InRelease cleans up.  This cuts
# CAN bus traffic significantly compared to an RC522 doing the equivalent.
#
# PN532 I2C protocol overview
# ───────────────────────────
# All communication uses length-framed packets with checksums:
#
#   Write frame:  [0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD, params..., DCS, 0x00]
#   Read  frame:  [STATUS, 0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD, data..., DCS, 0x00]
#
#   STATUS byte (first byte of every I2C read):
#     0x01 = ready     (response is in the buffer)
#     0x00 = busy      (PN532 still processing)
#
#   LEN  = number of bytes in the data field (TFI + CMD + payload)
#   LCS  = (-LEN) & 0xFF   (LEN + LCS = 0 mod 256)
#   TFI  = 0xD4 host→PN532 / 0xD5 PN532→host
#   DCS  = (-sum(data_field)) & 0xFF
#
# Since we cannot use the IRQ pin directly from a Klipper reactor callback
# (it would require a custom MCU command), we use fixed time.sleep() delays
# identical to the RC522 driver approach.  The two configurable delays are
# exposed under the same config keys as the RC522 driver so a single config
# section works for either chip (with different recommended values):
#
#   transceive_delay  maps to InListPassiveTarget wait (250 ms default).
#     The PN532 scans until a tag is found or its internal timer expires.
#     250 ms covers the no-tag timeout safely.
#   crc_delay         maps to InRelease wait (50 ms default).
#     Deselect is fast; 50 ms is very conservative.
#
# I2C address and wiring
# ──────────────────────
# The PN532 default I2C address is 0x24 (7-bit).  Some breakout boards expose
# address-select pads to choose among 0x24–0x27.  For multiple readers on one
# bus use a TCA9548A 1-to-8 I2C multiplexer.
#
# EBB42 v1.x I2C1 pins: SCL = PB6, SDA = PB7.
#
# Threading notes
# ───────────────
# All methods are called from the background polling thread.  i2c_write() and
# i2c_read() block that thread waiting for CAN round-trips; the Klipper
# reactor thread continues normally.

import time
import traceback

from .log import logger

# ─────────────────────────────────────────────────────────────────────────────
# PN532 frame constants
# ─────────────────────────────────────────────────────────────────────────────

_TFI_HOST_TO_PN532 = 0xD4
_TFI_PN532_TO_HOST = 0xD5

# PN532 command codes (sent from host to PN532)
_CMD_GETFIRMWAREVERSION  = 0x02
_CMD_SAMCONFIGURATION    = 0x14
_CMD_INLISTPASSIVETARGET = 0x4A
_CMD_INRELEASE           = 0x52

# InListPassiveTarget baud-rate/type codes
_BRTY_ISO14443A_106KBPS  = 0x00   # Standard NFC Type A — covers NTAG and Mifare

# Byte offsets inside a parsed read buffer
# [STATUS, 0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD, payload...]
_OFF_STATUS  = 0
_OFF_LEN     = 4
_OFF_TFI     = 6
_OFF_CMD     = 7
_OFF_PAYLOAD = 8

# Maximum bytes to read for any PN532 response (covers all commands used here)
_MAX_RESPONSE_BYTES = 32


class PN532Driver:
    """
    Driver for one PN532 NFC reader module connected via I2C.

    Reads only the tag UID 
    No data is read from tag memory; tags never need to be written to.

    The public interface is identical to RC522Driver — NFCGateManager can use
    either driver without any other code changes.

    Parameters
    ----------
    i2c : MCU_I2C
        A Klipper MCU_I2C object configured for this reader's I2C address.
        Must be fully initialised (klippy:connect completed) before calling
        init() or read_tag().
    gate : int
        Gate number (0-based), used for logging.
    transceive_delay : float
        Seconds to wait after InListPassiveTarget before reading the result.
        The PN532 scans until a tag is found or its internal timer expires.
        250 ms is a safe default.
    crc_delay : float
        Seconds to wait after InRelease.
        50 ms is conservative; 20 ms usually works.
    debug : int
        0 = silent, 1 = major events, 2 = full trace.
    """

    def __init__(self, i2c, gate,
                 transceive_delay=0.250,
                 crc_delay=0.050,
                 debug=1):
        self._i2c            = i2c
        self._gate           = gate
        self._scan_delay     = transceive_delay   # InListPassiveTarget wait
        self._release_delay  = crc_delay          # InRelease wait
        self._debug          = debug

    # ─────────────────────────────────────────────────────────────────────────
    # Frame construction and parsing
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_frame(cmd_and_params):
        """
        Build a complete PN532 host-to-chip command frame.

        Parameters
        ----------
        cmd_and_params : list of int
            The command byte followed by any parameters.
            TFI (0xD4) is prepended automatically.

        Returns
        -------
        list of int
            The full frame: preamble + start + LEN + LCS + TFI + data + DCS + postamble.
        """
        data = [_TFI_HOST_TO_PN532] + list(cmd_and_params)
        length = len(data)
        lcs    = (-length) & 0xFF
        dcs    = (-sum(data)) & 0xFF
        return [0x00, 0x00, 0xFF, length, lcs] + data + [dcs, 0x00]

    @staticmethod
    def _check_frame(raw, expected_cmd_resp):
        """
        Validate a raw read buffer and return the payload bytes.

        Parameters
        ----------
        raw : list / bytearray
            The full byte sequence returned by i2c_read(), including the
            leading STATUS byte.
        expected_cmd_resp : int
            The command-response code we expect at raw[_OFF_CMD].

        Returns
        -------
        list of int or None
            Payload bytes (after TFI and CMD_RESP), or None on any error.
        """
        if len(raw) < _OFF_PAYLOAD:
            return None
        if raw[_OFF_STATUS] != 0x01:              # PN532 not ready
            return None
        if raw[1] != 0x00 or raw[2] != 0x00 or raw[3] != 0xFF:
            return None                            # Corrupted start code
        if raw[_OFF_TFI] != _TFI_PN532_TO_HOST:
            return None
        if raw[_OFF_CMD] != expected_cmd_resp:
            return None
        length  = raw[_OFF_LEN]
        payload = list(raw[_OFF_PAYLOAD: _OFF_PAYLOAD + length - 2])
        return payload                             # Bytes after TFI and CMD_RESP

    # ─────────────────────────────────────────────────────────────────────────
    # Low-level I2C helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _send(self, cmd_and_params):
        """Write a command frame to the PN532."""
        frame = self._build_frame(cmd_and_params)
        if self._debug >= 2:
            logger.debug("_send: gate %d (PN532) TX  cmd=0x%02X  frame=%s",
                          self._gate, cmd_and_params[0],
                          ' '.join('%02X' % b for b in frame))
        self._i2c.i2c_write(frame)

    def _recv(self, expected_cmd_resp, read_len=_MAX_RESPONSE_BYTES,
              timeout=1.0, poll_interval=0.005):
        """
        Poll the PN532 with 1-byte reads until STATUS=0x01 (ready),
        then read the full response frame.

        Parameters
        ----------
        expected_cmd_resp : int
            The response command byte expected at raw[_OFF_CMD].
        read_len : int
            Number of bytes to read for the full response frame.
        timeout : float
            Maximum seconds to poll before giving up.
        poll_interval : float
            Seconds to wait between poll attempts.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = self._i2c.i2c_read([], 1)
                raw1 = bytearray(result['response'])
                pn_status = raw1[0] if raw1 else 0xFF
            except Exception as e:
                logger.error("_recv: gate %d (PN532) poll failed: %s\n%s",
                             self._gate, e, traceback.format_exc())
                return None

            if self._debug >= 2:
                logger.debug("_recv: gate %d (PN532) poll result=%s pn_status=0x%02X",
                             self._gate,
                             ' '.join('%02X' % b for b in raw1),
                             pn_status)

            if pn_status == 0x01:
                try:
                    params = self._i2c.i2c_read([], read_len)
                    raw = bytearray(params['response'])
                    payload = self._check_frame(raw, expected_cmd_resp)
                    if self._debug >= 2:
                        status_byte = raw[0] if raw else 0xFF
                        if payload is not None:
                            logger.debug(
                                "_recv: gate %d (PN532) DATA: expect=0x%02X "
                                "pn_status=0x%02X raw=%s",
                                self._gate, expected_cmd_resp, status_byte,
                                ' '.join('%02X' % b for b in raw))
                            logger.debug("_recv: gate %d (PN532) payload: %s",
                                         self._gate,
                                         ' '.join('%02X' % b for b in payload))
                        else:
                            logger.debug(
                                "_recv: gate %d (PN532) DATA ERROR: expect=0x%02X "
                                "pn_status=0x%02X raw=%s",
                                self._gate, expected_cmd_resp, status_byte,
                                ' '.join('%02X' % b for b in raw) if raw else '(empty)')
                    return payload
                except Exception as e:
                    logger.error("_recv: gate %d (PN532) DATA read failed: %s\n%s",
                                 self._gate, e, traceback.format_exc())
                    return None

            time.sleep(poll_interval)

        if self._debug >= 2:
            logger.debug("_recv: gate %d (PN532) timeout after %.1fs waiting for ready",
                         self._gate, timeout)
        return None
    # ─────────────────────────────────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────────────────────────────────

    def _wake_pn532(self, attempts=3):
        """
        Wake the PN532 from power-save mode using GetFirmwareVersion.

        Uses a single i2c_write + i2c_read per attempt to avoid Klipper MCU
        command re-entrancy (two sequential i2c_read calls in one attempt
        caused recursive send → _do_send loops in mcu.py).

        First attempt waits 150 ms after TX (cold-start settling).
        Subsequent attempts wait 75 ms.  A 50 ms gap separates each attempt.

        Returns True if the chip responded, False if all attempts failed.
        """
        for attempt in range(attempts):
            if self._debug >= 2:
                logger.debug(
                    "_wake_pn532: gate %d (PN532) attempt %d/%d — "
                    "sending GetFirmwareVersion",
                    self._gate, attempt + 1, attempts)
            try:
                self._send([_CMD_GETFIRMWAREVERSION])
                payload = self._recv(0x03, read_len=15, timeout=0.500)
                if payload is not None and len(payload) >= 4:
                    logger.info(
                        "_wake_pn532: gate %d (PN532) OK on attempt %d — "
                        "IC=0x%02X Ver=%d.%d",
                        self._gate, attempt + 1,
                        payload[0], payload[1], payload[2])
                    return True
                if self._debug >= 2:
                    logger.debug(
                        "_wake_pn532: gate %d (PN532) attempt %d — "
                        "no valid response",
                        self._gate, attempt + 1)
            except Exception as e:
                level = logger.debug if attempt == 0 else logger.info
                level("_wake_pn532: gate %d (PN532) attempt %d failed: %s\n%s",
                      self._gate, attempt + 1, e, traceback.format_exc())
            time.sleep(0.050)

        logger.warning("_wake_pn532: gate %d (PN532) failed after "
                        "%d attempts — check wiring and I2C address 0x%02X",
                        self._gate, attempts, self._i2c.i2c_address
                        if hasattr(self._i2c, 'i2c_address') else 0x24)
        return False

    def init(self):
        """
        Wake the PN532 then configure it for ISO14443A normal operation.

        Sends GetFirmwareVersion (with retries) to bring the chip out of
        power-save, then SAMConfiguration (Normal mode, no SAM timeout,
        no IRQ output).  Must be called once after klippy:connect.

        Raises RuntimeError if the chip does not respond after retries.
        """
        if self._debug >= 2:
            logger.debug("init: gate %d (PN532) starting wake sequence",
                          self._gate)

        if not self._wake_pn532():
            raise RuntimeError(
                "PN532 gate %d did not respond — check wiring and I2C address"
                % self._gate)

        if self._debug >= 2:
            logger.debug("init: gate %d (PN532) sending SAMConfiguration "
                          "(Normal mode, timeout=0, no IRQ)", self._gate)

        # SAMConfiguration: Normal mode(0x01), timeout=0x00, IRQ=0x00
        self._send([_CMD_SAMCONFIGURATION, 0x01, 0x00, 0x00])
        payload = self._recv(0x15, read_len=12, timeout=0.200)
        if payload is None:
            logger.warning("init: gate %d (PN532) SAMConfiguration "
                            "no response — reader may be unstable",
                            self._gate)
        elif self._debug >= 2:
            logger.debug("init: gate %d (PN532) SAMConfiguration OK",
                          self._gate)

    def is_alive(self):
        """
        Return True if the PN532 has already responded during init().

        After init() succeeds the chip is confirmed alive.  This method is
        kept for API compatibility with RC522Driver — callers should call
        init() first and check for RuntimeError rather than calling is_alive()
        standalone.
        """
        try:
            self._send([_CMD_GETFIRMWAREVERSION])
            payload = self._recv(0x03, read_len=14, timeout=0.200)
            return payload is not None and len(payload) >= 4
        except Exception as e:
            logger.debug("is_alive: gate %d (PN532) error: %s\n%s",
                          self._gate, e, traceback.format_exc())
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Tag read — UID only 
    # ─────────────────────────────────────────────────────────────────────────

    def read_tag(self):
        """
        Attempt to read the UID of any tag in the RF field.

        Uses InListPassiveTarget to let the PN532 handle REQA / ANTICOLL /
        SELECT internally, then InRelease to deselect so the next scan starts
        clean.  No data is read from tag memory.

        Returns
        -------
        str
            Tag UID as uppercase hex (8, 10, or 14 chars for 4-, 5-, 7-byte UIDs).
        None
            No tag in the RF field, or a communication error occurred.
        """
        try:
            uid_hex, tg = self._list_passive_target()
            if uid_hex is None:
                return None

            self._release_target()

            if self._debug >= 2:
                logger.debug("read_tag: gate %d (PN532) uid=%s",
                              self._gate, uid_hex)

            return uid_hex
        except Exception as e:
            # Runtime NACKs (e.g. tag removed mid-scan) are non-fatal.
            if self._debug >= 1:
                logger.info("read_tag: gate %d (PN532) I2C error "
                             "(tag removed mid-scan?): %s\n%s",
                             self._gate, e, traceback.format_exc())
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _list_passive_target(self):
        """
        Send InListPassiveTarget and parse the response.

        Returns (uid_hex, tg_num) when a tag is found, (None, None) otherwise.
        tg_num is the PN532's internal target number (always 1 for MaxTg=1).
        """
        if self._debug >= 2:
            logger.debug("_list_passive_target: gate %d (PN532) scanning (wait=%.3fs)",
                          self._gate, self._scan_delay)

        # MaxTg=1 (detect one tag), BrTy=0x00 (ISO14443A 106 kbps)
        self._send([_CMD_INLISTPASSIVETARGET, 0x01, _BRTY_ISO14443A_106KBPS])

        # Response CMD code for InListPassiveTarget is 0x4B
        # Worst case: status(1)+frame_overhead(7)+NbTg(1)+per_tag(1+2+1+1+7)=21 bytes
        # Read 32 bytes to cover 7-byte UIDs and any optional ATS data.
        payload = self._recv(0x4B, read_len=_MAX_RESPONSE_BYTES, timeout=self._scan_delay + 0.100)
        if payload is None:
            if self._debug >= 2:
                logger.debug("_list_passive_target: gate %d (PN532) no valid response",
                              self._gate)
            return None, None

        # payload[0] = NbTg (number of targets found)
        if not payload or payload[0] == 0:
            if self._debug >= 2:
                logger.debug("_list_passive_target: gate %d (PN532) no tag (NbTg=0)",
                              self._gate)
            return None, None

        # Parse first target
        # payload: [NbTg, Tg, ATQA(2), SAK, NFCIDLen, NFCID...]
        if len(payload) < 7:   # NbTg + Tg + ATQA(2) + SAK + NFCIDLen + 1 byte UID minimum
            return None, None

        tg          = payload[1]
        # atqa      = payload[2:4]   (not used)
        # sak       = payload[4]     (not used)
        nfcid_len   = payload[5]

        if nfcid_len == 0 or len(payload) < 6 + nfcid_len:
            return None, None

        nfcid   = payload[6:6 + nfcid_len]
        uid_hex = ''.join('{:02X}'.format(b) for b in nfcid)

        if self._debug >= 2:
            atqa = payload[2:4]
            sak  = payload[4]
            logger.debug(
                "_list_passive_target: gate %d (PN532) tag found  "
                "tg=%d  ATQA=%s  SAK=0x%02X  NFCIDLen=%d  UID=%s",
                self._gate, tg,
                ' '.join('%02X' % b for b in atqa),
                sak, nfcid_len, uid_hex)

        return uid_hex, tg

    def _release_target(self):
        """
        Send InRelease to deselect all activated targets.
        Must be called after each tag detection so the next InListPassiveTarget
        starts a fresh scan rather than trying to talk to the old target.
        """
        if self._debug >= 2:
            logger.debug("_release_target: gate %d (PN532) deselecting all targets",
                          self._gate)
        # InRelease: Tg=0x00 releases all targets
        self._send([_CMD_INRELEASE, 0x00])
        # Response CMD code for InRelease is 0x53; payload is just [Status]
        payload = self._recv(0x53, read_len=12, timeout=0.200)
        if self._debug >= 2:
            if payload is not None:
                logger.debug("_release_target: gate %d (PN532) OK  "
                              "status=0x%02X", self._gate, payload[0] if payload else 0xFF)
            else:
                logger.debug("_release_target: gate %d (PN532) no response (non-fatal)",
                              self._gate)
        # Ignore errors — even if release fails, the next scan will recover.


# =============================================================================
# PN532SPIDriver — SPI variant
# =============================================================================
#
# PN532 SPI protocol overview
# ───────────────────────────
# SPI mode 0 (CPOL=0, CPHA=0), LSB first.  Each transaction is framed by CS.
#
# Direction bytes (sent as first byte of every CS transaction):
#   0x01  Data Writing  — host sends a command frame to the PN532
#   0x02  Status Reading — host polls whether the PN532 has a response ready
#                          PN532 returns 0x01 when ready, 0x00 when busy
#   0x03  Data Reading  — host reads the response frame from the PN532
#
# All bytes (direction byte and frame bytes) are transmitted LSB first.
# Most SPI controllers (including the RP2040 default) send MSB first, so
# every byte is bit-reversed in software before sending and after receiving.
#
# The response frame in SPI mode does NOT include the STATUS prefix byte that
# appears in the I2C response.  The frame starts directly with the preamble:
#   [0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD, payload..., DCS, 0x00]
#
# Public interface is identical to PN532Driver (I2C) so NFCGateManager works
# with either driver without modification.

# SPI frame byte offsets (no STATUS prefix, unlike I2C)
_SPI_OFF_LEN     = 3
_SPI_OFF_TFI     = 5
_SPI_OFF_CMD     = 6
_SPI_OFF_PAYLOAD = 7

# PN532 SPI direction bytes (before bit reversal)
_SPI_DIR_WRITE        = 0x01
_SPI_DIR_READ_STATUS  = 0x02
_SPI_DIR_READ_DATA    = 0x03


def _rev8(b):
    """Reverse the bits in a single byte (PN532 SPI is LSB first)."""
    b = ((b & 0xF0) >> 4) | ((b & 0x0F) << 4)
    b = ((b & 0xCC) >> 2) | ((b & 0x33) << 2)
    b = ((b & 0xAA) >> 1) | ((b & 0x55) << 1)
    return b


def _rev_list(data):
    """Bit-reverse every byte in a list."""
    return [_rev8(b) for b in data]


class PN532SPIDriver:
    """
    Driver for one PN532 NFC reader module connected via SPI.

    Reads only the tag UID.  No data is ever written to the tag.
    The public interface is identical to PN532Driver (I2C variant).

    Parameters
    ----------
    spi : MCU_SPI
        A Klipper MCU_SPI object configured for this reader's CS pin.
        Must be fully initialised (klippy:connect completed) before calling
        init() or read_tag().
    gate : int
        Gate number (0-based), used for logging.
    transceive_delay : float
        Scan timeout passed to InListPassiveTarget poll loop.
    crc_delay : float
        Timeout for InRelease and SAMConfiguration responses.
    debug : int
        0 = silent, 1 = major events, 2 = full trace.
    """

    def __init__(self, spi, gate,
                 transceive_delay=0.250,
                 crc_delay=0.050,
                 debug=1):
        self._spi           = spi
        self._gate          = gate
        self._scan_delay    = transceive_delay
        self._release_delay = crc_delay
        self._debug         = debug

    # ─────────────────────────────────────────────────────────────────────────
    # Frame construction and parsing
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_frame(cmd_and_params):
        """Build a PN532 host-to-chip command frame (same structure as I2C)."""
        data   = [_TFI_HOST_TO_PN532] + list(cmd_and_params)
        length = len(data)
        lcs    = (-length) & 0xFF
        dcs    = (-sum(data)) & 0xFF
        return [0x00, 0x00, 0xFF, length, lcs] + data + [dcs, 0x00]

    @staticmethod
    def _check_frame(raw, expected_cmd_resp):
        """
        Validate a raw SPI read buffer and return the payload bytes.

        SPI frames have no STATUS prefix byte — the frame starts with the
        preamble [0x00, 0x00, 0xFF, ...] at offset 0.
        """
        if len(raw) < _SPI_OFF_PAYLOAD:
            return None
        if raw[0] != 0x00 or raw[1] != 0x00 or raw[2] != 0xFF:
            return None
        if raw[_SPI_OFF_TFI] != _TFI_PN532_TO_HOST:
            return None
        if raw[_SPI_OFF_CMD] != expected_cmd_resp:
            return None
        length  = raw[_SPI_OFF_LEN]
        payload = list(raw[_SPI_OFF_PAYLOAD: _SPI_OFF_PAYLOAD + length - 2])
        return payload

    # ─────────────────────────────────────────────────────────────────────────
    # Low-level SPI helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _send(self, cmd_and_params):
        """Write a command frame to the PN532 (direction byte 0x01)."""
        frame = self._build_frame(cmd_and_params)
        wire  = _rev_list([_SPI_DIR_WRITE] + frame)
        if self._debug >= 2:
            logger.debug("_send: gate %d (PN532 SPI) TX  cmd=0x%02X  frame=%s",
                          self._gate, cmd_and_params[0],
                          ' '.join('%02X' % b for b in frame))
        self._spi.spi_send(wire)

    def _recv(self, expected_cmd_resp, read_len=_MAX_RESPONSE_BYTES,
              timeout=1.0, poll_interval=0.005):
        """
        Poll the PN532 with status reads (direction byte 0x02) until ready
        (0x01), then read the full response frame (direction byte 0x03).

        Parameters
        ----------
        expected_cmd_resp : int
            The response command byte expected at raw[_SPI_OFF_CMD].
        read_len : int
            Number of frame bytes to read (not including the direction byte).
        timeout : float
            Maximum seconds to poll before giving up.
        poll_interval : float
            Seconds between poll attempts.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                # Send direction byte 0x02, read 1 status byte back
                resp   = self._spi.spi_transfer(_rev_list([_SPI_DIR_READ_STATUS, 0x00]))
                status = _rev8(bytearray(resp['response'])[1])
            except Exception as e:
                logger.error("_recv: gate %d (PN532 SPI) poll failed: %s\n%s",
                             self._gate, e, traceback.format_exc())
                return None

            if self._debug >= 2:
                logger.debug("_recv: gate %d (PN532 SPI) poll status=0x%02X",
                             self._gate, status)

            if status == 0x01:
                try:
                    # Send direction byte 0x03, read read_len data bytes
                    out    = _rev_list([_SPI_DIR_READ_DATA] + [0x00] * read_len)
                    params = self._spi.spi_transfer(out)
                    raw    = bytearray(_rev8(b) for b in bytearray(params['response'])[1:])
                    payload = self._check_frame(raw, expected_cmd_resp)
                    if self._debug >= 2:
                        if payload is not None:
                            logger.debug(
                                "_recv: gate %d (PN532 SPI) DATA: expect=0x%02X raw=%s",
                                self._gate, expected_cmd_resp,
                                ' '.join('%02X' % b for b in raw))
                            logger.debug("_recv: gate %d (PN532 SPI) payload: %s",
                                         self._gate,
                                         ' '.join('%02X' % b for b in payload))
                        else:
                            logger.debug(
                                "_recv: gate %d (PN532 SPI) DATA ERROR: expect=0x%02X raw=%s",
                                self._gate, expected_cmd_resp,
                                ' '.join('%02X' % b for b in raw) if raw else '(empty)')
                    return payload
                except Exception as e:
                    logger.error("_recv: gate %d (PN532 SPI) DATA read failed: %s\n%s",
                                 self._gate, e, traceback.format_exc())
                    return None

            time.sleep(poll_interval)

        if self._debug >= 2:
            logger.debug("_recv: gate %d (PN532 SPI) timeout after %.1fs",
                         self._gate, timeout)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────────────────────────────────

    def _wake_pn532(self, attempts=3):
        """Send GetFirmwareVersion to wake the PN532 and confirm it responds."""
        for attempt in range(attempts):
            if self._debug >= 2:
                logger.debug(
                    "_wake_pn532: gate %d (PN532 SPI) attempt %d/%d",
                    self._gate, attempt + 1, attempts)
            try:
                self._send([_CMD_GETFIRMWAREVERSION])
                payload = self._recv(0x03, read_len=15, timeout=0.500)
                if payload is not None and len(payload) >= 4:
                    logger.info(
                        "_wake_pn532: gate %d (PN532 SPI) OK on attempt %d — "
                        "IC=0x%02X Ver=%d.%d",
                        self._gate, attempt + 1,
                        payload[0], payload[1], payload[2])
                    return True
                if self._debug >= 2:
                    logger.debug(
                        "_wake_pn532: gate %d (PN532 SPI) attempt %d — no valid response",
                        self._gate, attempt + 1)
            except Exception as e:
                level = logger.debug if attempt == 0 else logger.info
                level("_wake_pn532: gate %d (PN532 SPI) attempt %d failed: %s\n%s",
                      self._gate, attempt + 1, e, traceback.format_exc())
            time.sleep(0.050)

        logger.warning("_wake_pn532: gate %d (PN532 SPI) failed after %d attempts",
                        self._gate, attempts)
        return False

    def init(self):
        """Wake the PN532 and configure it for ISO14443A normal operation."""
        if self._debug >= 2:
            logger.debug("init: gate %d (PN532 SPI) starting wake sequence", self._gate)

        if not self._wake_pn532():
            raise RuntimeError(
                "PN532 gate %d did not respond — check wiring and SPI CS pin"
                % self._gate)

        if self._debug >= 2:
            logger.debug("init: gate %d (PN532 SPI) sending SAMConfiguration", self._gate)

        self._send([_CMD_SAMCONFIGURATION, 0x01, 0x00, 0x00])
        payload = self._recv(0x15, read_len=12, timeout=0.200)
        if payload is None:
            logger.warning("init: gate %d (PN532 SPI) SAMConfiguration no response — "
                            "reader may be unstable", self._gate)
        elif self._debug >= 2:
            logger.debug("init: gate %d (PN532 SPI) SAMConfiguration OK", self._gate)

    def is_alive(self):
        """Return True if the PN532 responds to GetFirmwareVersion."""
        try:
            self._send([_CMD_GETFIRMWAREVERSION])
            payload = self._recv(0x03, read_len=14, timeout=0.200)
            return payload is not None and len(payload) >= 4
        except Exception as e:
            logger.debug("is_alive: gate %d (PN532 SPI) error: %s\n%s",
                          self._gate, e, traceback.format_exc())
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Tag read — UID only
    # ─────────────────────────────────────────────────────────────────────────

    def read_tag(self):
        """
        Attempt to read the UID of any tag in the RF field.

        Returns str (UID hex) or None.
        """
        try:
            uid_hex, tg = self._list_passive_target()
            if uid_hex is None:
                return None
            self._release_target()
            if self._debug >= 2:
                logger.debug("read_tag: gate %d (PN532 SPI) uid=%s",
                              self._gate, uid_hex)
            return uid_hex
        except Exception as e:
            if self._debug >= 1:
                logger.info("read_tag: gate %d (PN532 SPI) I2C error "
                             "(tag removed mid-scan?): %s\n%s",
                             self._gate, e, traceback.format_exc())
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _list_passive_target(self):
        """Send InListPassiveTarget and parse the response."""
        if self._debug >= 2:
            logger.debug("_list_passive_target: gate %d (PN532 SPI) scanning",
                          self._gate)

        self._send([_CMD_INLISTPASSIVETARGET, 0x01, _BRTY_ISO14443A_106KBPS])
        payload = self._recv(0x4B, read_len=_MAX_RESPONSE_BYTES,
                             timeout=self._scan_delay + 0.100)
        if payload is None:
            if self._debug >= 2:
                logger.debug("_list_passive_target: gate %d (PN532 SPI) no valid response",
                              self._gate)
            return None, None

        if not payload or payload[0] == 0:
            if self._debug >= 2:
                logger.debug("_list_passive_target: gate %d (PN532 SPI) no tag (NbTg=0)",
                              self._gate)
            return None, None

        if len(payload) < 7:
            return None, None

        tg        = payload[1]
        nfcid_len = payload[5]

        if nfcid_len == 0 or len(payload) < 6 + nfcid_len:
            return None, None

        nfcid   = payload[6:6 + nfcid_len]
        uid_hex = ''.join('{:02X}'.format(b) for b in nfcid)

        if self._debug >= 2:
            atqa = payload[2:4]
            sak  = payload[4]
            logger.debug(
                "_list_passive_target: gate %d (PN532 SPI) tag found  "
                "tg=%d  ATQA=%s  SAK=0x%02X  NFCIDLen=%d  UID=%s",
                self._gate, tg,
                ' '.join('%02X' % b for b in atqa),
                sak, nfcid_len, uid_hex)

        return uid_hex, tg

    def _release_target(self):
        """Send InRelease to deselect all activated targets."""
        if self._debug >= 2:
            logger.debug("_release_target: gate %d (PN532 SPI) deselecting all targets",
                          self._gate)
        self._send([_CMD_INRELEASE, 0x00])
        payload = self._recv(0x53, read_len=12, timeout=0.200)
        if self._debug >= 2:
            if payload is not None:
                logger.debug("_release_target: gate %d (PN532 SPI) OK  status=0x%02X",
                              self._gate, payload[0] if payload else 0xFF)
            else:
                logger.debug("_release_target: gate %d (PN532 SPI) no response (non-fatal)",
                              self._gate)
