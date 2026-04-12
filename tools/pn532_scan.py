#!/usr/bin/env python3
# pn532_scan.py
#
# Standalone PN532 I2C scanner for Raspberry Pi.
# No Klipper, no MMU, no Spoolman — just the PN532.
#
# Wiring (Pi GPIO header):
#   PN532 VCC → Pin 1  (3.3V)
#   PN532 GND → Pin 6  (GND)
#   PN532 SDA → Pin 3  (GPIO2, I2C1 SDA)
#   PN532 SCL → Pin 5  (GPIO3, I2C1 SCL)
#
# PN532 must be in I2C mode (DIP switch / solder jumper).
#
# Prerequisites:
#   sudo apt install python3-smbus2
#   sudo raspi-config → Interface Options → I2C → Enable
#
# Usage:
#   python3 pn532_scan.py [--bus N] [--address 0x24] [--debug] [--scan-bus]

import argparse
import sys
import time

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:
    print("ERROR: smbus2 is not installed.")
    print("       Run:  sudo apt install python3-smbus2")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# PN532 frame constants
# ─────────────────────────────────────────────────────────────────────────────

_TFI_HOST  = 0xD4   # direction byte: host → PN532
_TFI_PN532 = 0xD5   # direction byte: PN532 → host

_CMD_GETFIRMWAREVERSION  = 0x02
_CMD_SAMCONFIGURATION    = 0x14
_CMD_INLISTPASSIVETARGET = 0x4A
_CMD_INRELEASE           = 0x52

_STATUS_READY = 0x01
_STATUS_BUSY  = 0x00

_MAX_RESPONSE = 32

# The PN532 ACK frame (I2C form): status byte + ACK pattern
# STATUS(0x01) + 00 00 FF 00 FF 00
_ACK_FRAME = bytes([0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00])


# ─────────────────────────────────────────────────────────────────────────────
# Frame construction
# ─────────────────────────────────────────────────────────────────────────────

def build_frame(cmd_and_params):
    """
    Build a complete PN532 host-to-chip command frame.

    Format: [0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD, params..., DCS, 0x00]
    """
    data   = [_TFI_HOST] + list(cmd_and_params)
    length = len(data)
    lcs    = (-length) & 0xFF
    dcs    = (-sum(data)) & 0xFF
    return [0x00, 0x00, 0xFF, length, lcs] + data + [dcs, 0x00]


# ─────────────────────────────────────────────────────────────────────────────
# Raw I2C primitives
# ─────────────────────────────────────────────────────────────────────────────

def i2c_write(bus, address, data):
    """Write a list of bytes to the device."""
    msg = i2c_msg.write(address, data)
    bus.i2c_rdwr(msg)


def i2c_read(bus, address, read_len):
    """Read read_len bytes from the device. Returns bytes."""
    msg = i2c_msg.read(address, read_len)
    bus.i2c_rdwr(msg)
    return bytes(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Frame parsing and detection
# ─────────────────────────────────────────────────────────────────────────────

def is_ack_frame(raw):
    """
    Return True if raw (without the leading status byte) matches the PN532
    ACK pattern: 00 00 FF 00 FF 00

    The PN532 sends ACK to confirm it received a command. It is NOT a
    response — the actual response comes in a subsequent ready+read cycle.
    """
    # raw may include trailing zeros; only the first 6 bytes matter
    return len(raw) >= 6 and bytes(raw[:6]) == _ACK_FRAME


def parse_response_frame(raw, expected_cmd_resp):
    """
    Validate a full read buffer (without the leading status byte) and return
    the payload bytes, or None if the frame is invalid or not the expected cmd.

    I2C response frame layout (after stripping the leading status byte):
      [0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD_RESP, payload..., DCS, 0x00]

    expected_cmd_resp is the command code + 1 (e.g. 0x03 for GetFirmwareVersion).
    """
    if len(raw) < 8:
        return None
    if raw[0] != 0x00 or raw[1] != 0x00 or raw[2] != 0xFF:
        return None    # bad preamble / start code
    if raw[5] != _TFI_PN532:
        return None    # wrong direction byte
    if raw[6] != expected_cmd_resp:
        return None    # not the response we're waiting for
    length  = raw[3]
    payload = list(raw[7: 7 + length - 2])
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# PN532 send / receive
# ─────────────────────────────────────────────────────────────────────────────

def pn532_send(bus, address, cmd_and_params, debug=False):
    """Write a command frame to the PN532."""
    frame = build_frame(cmd_and_params)
    if debug:
        print(f"    TX cmd=0x{cmd_and_params[0]:02X}  "
              f"frame={' '.join('%02X' % b for b in frame)}")
    i2c_write(bus, address, frame)


def pn532_recv(bus, address, expected_cmd_resp,
               read_len=_MAX_RESPONSE, timeout=2.0,
               poll_interval=0.010, debug=False):
    """
    Poll the PN532 until it is ready, then read and validate the response.

    The PN532 uses a two-step response sequence after receiving a command:
      Step 1: poll returns 0x01 (ready) → full read returns the ACK frame
              (00 00 FF 00 FF 00) — this confirms receipt of the command,
              NOT the actual response.
      Step 2: poll returns 0x01 (ready) → full read returns the actual
              response frame (D5 CMD+1 payload...).

    We must not mistake the ACK for the response. This function keeps
    cycling through poll → read until it gets a valid response frame
    or the timeout expires.

    Returns the payload bytes on success, or None on timeout/error.
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        # ── Poll: 1-byte read to check readiness ──────────────────────────
        try:
            status_byte = i2c_read(bus, address, 1)[0]
        except Exception as e:
            if debug:
                print(f"    poll error: {e}")
            time.sleep(poll_interval)
            continue

        if debug:
            print(f"    poll  status=0x{status_byte:02X}  "
                  f"({'ready' if status_byte == _STATUS_READY else 'busy'})")

        if status_byte != _STATUS_READY:
            time.sleep(poll_interval)
            continue

        # ── Full read ──────────────────────────────────────────────────────
        # The first byte of the full read is the status byte again (I2C mode).
        # Remaining bytes are the frame.
        try:
            full = i2c_read(bus, address, 1 + read_len)
        except Exception as e:
            if debug:
                print(f"    full read error: {e}")
            time.sleep(poll_interval)
            continue

        status_byte2 = full[0]
        frame_bytes  = full[1:]

        if debug:
            print(f"    RX  status=0x{status_byte2:02X}  "
                  f"data={' '.join('%02X' % b for b in frame_bytes)}")

        # ── ACK detection ──────────────────────────────────────────────────
        if is_ack_frame(frame_bytes):
            if debug:
                print("    ACK received — waiting for actual response")
            # ACK is not the response; go back and poll again
            time.sleep(poll_interval)
            continue

        # ── Response frame validation ──────────────────────────────────────
        payload = parse_response_frame(frame_bytes, expected_cmd_resp)
        if payload is not None:
            if debug:
                print(f"    response OK  cmd=0x{expected_cmd_resp:02X}  "
                      f"payload={' '.join('%02X' % b for b in payload)}")
            return payload

        # Frame received but not the one we want — keep waiting
        if debug:
            print(f"    unexpected frame (expected cmd=0x{expected_cmd_resp:02X}) — continuing")
        time.sleep(poll_interval)

    if debug:
        remaining = deadline - time.time()
        print(f"    timeout waiting for cmd=0x{expected_cmd_resp:02X} response")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PN532 commands
# ─────────────────────────────────────────────────────────────────────────────

def pn532_get_firmware_version(bus, address, debug=False):
    """
    Send GetFirmwareVersion and return (IC, Ver, Rev, Support) or None.

    Also serves as the wake command — send it first on startup to bring
    the PN532 out of power-save mode.
    """
    pn532_send(bus, address, [_CMD_GETFIRMWAREVERSION], debug)
    # Response command code = 0x02 + 1 = 0x03
    payload = pn532_recv(bus, address, 0x03, read_len=15, timeout=1.0, debug=debug)
    if payload and len(payload) >= 4:
        return tuple(payload[:4])   # (IC, Ver, Rev, Support)
    return None


def pn532_sam_configuration(bus, address, debug=False):
    """
    Send SAMConfiguration: Normal mode, no timeout, no IRQ.
    Returns True on success.
    """
    pn532_send(bus, address, [_CMD_SAMCONFIGURATION, 0x01, 0x00, 0x00], debug)
    # Response command code = 0x14 + 1 = 0x15
    payload = pn532_recv(bus, address, 0x15, read_len=12, timeout=0.500, debug=debug)
    return payload is not None


def pn532_read_passive_target(bus, address, debug=False):
    """
    Send InListPassiveTarget (MaxTg=1, ISO14443A 106kbps).
    Returns UID as uppercase hex string if a tag is present, else None.
    """
    pn532_send(bus, address, [_CMD_INLISTPASSIVETARGET, 0x01, 0x00], debug)
    # Response command code = 0x4A + 1 = 0x4B
    # Allow up to 350ms for the PN532 to scan + extra for two-step response
    payload = pn532_recv(bus, address, 0x4B, read_len=_MAX_RESPONSE,
                         timeout=0.800, debug=debug)

    if not payload or payload[0] == 0:
        return None   # NbTg == 0: no tag found

    # payload layout: [NbTg, Tg, ATQA(2), SAK, NFCIDLen, NFCID...]
    if len(payload) < 7:
        return None

    nfcid_len = payload[5]
    if nfcid_len == 0 or len(payload) < 6 + nfcid_len:
        return None

    uid = payload[6:6 + nfcid_len]
    return ''.join('{:02X}'.format(b) for b in uid)


def pn532_release(bus, address, debug=False):
    """
    Send InRelease to deselect all targets.
    Must be called after each successful tag read so the next scan starts clean.
    """
    pn532_send(bus, address, [_CMD_INRELEASE, 0x00], debug)
    # Response command code = 0x52 + 1 = 0x53
    pn532_recv(bus, address, 0x53, read_len=12, timeout=0.300, debug=debug)
    # Errors here are non-fatal — next scan will recover


# ─────────────────────────────────────────────────────────────────────────────
# Bus scan
# ─────────────────────────────────────────────────────────────────────────────

def scan_bus(bus_num):
    """Probe every valid I2C address and print devices that respond."""
    print(f"Scanning I2C bus {bus_num}...")
    found = []
    with SMBus(bus_num) as bus:
        for addr in range(0x03, 0x78):
            try:
                msg = i2c_msg.read(addr, 1)
                bus.i2c_rdwr(msg)
                found.append(addr)
                print(f"  0x{addr:02X}  ({addr})")
            except OSError:
                pass
    if not found:
        print("  No devices found.")
    else:
        print(f"\n{len(found)} device(s) found.")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

POLL_INTERVAL = 15   # seconds between InListPassiveTarget scans


def main():
    parser = argparse.ArgumentParser(description='PN532 I2C scanner for Raspberry Pi')
    parser.add_argument('--bus',      type=int,  default=1,
                        help='I2C bus number (default: 1 = /dev/i2c-1, GPIO2/3)')
    parser.add_argument('--address',  default='0x24',
                        help='PN532 I2C address in hex (default: 0x24)')
    parser.add_argument('--debug',    action='store_true',
                        help='Show full I2C protocol trace')
    parser.add_argument('--scan-bus', action='store_true',
                        help='Scan I2C bus for all responding devices then exit')
    parser.add_argument('--once',     action='store_true',
                        help='Exit after first tag read')
    args = parser.parse_args()

    address = int(args.address, 16) if args.address.startswith('0x') \
              else int(args.address)

    if args.scan_bus:
        scan_bus(args.bus)
        return

    print(f"PN532 scanner  bus={args.bus}  address=0x{address:02X}  "
          f"poll={POLL_INTERVAL}s  debug={args.debug}")
    print("Ctrl+C to stop\n")

    with SMBus(args.bus) as bus:

        # ── Initialise ────────────────────────────────────────────────────
        print("Initialising PN532...")
        fw = None
        for attempt in range(3):
            wait = 0.150 if attempt == 0 else 0.075
            if args.debug:
                print(f"  Wake attempt {attempt+1}/3  (post-TX wait={wait*1000:.0f}ms)")
            try:
                fw = pn532_get_firmware_version(bus, address, debug=args.debug)
                if fw:
                    break
            except Exception as e:
                if args.debug:
                    print(f"  attempt {attempt+1} error: {e}")
            time.sleep(wait)

        if not fw:
            print("\nERROR: PN532 did not respond.")
            print(f"  Run with --scan-bus to check bus {args.bus}")
            print("  Check I2C mode jumper (SEL0=H, SEL1=L), wiring, and 3.3V power")
            sys.exit(1)

        print(f"  IC=0x{fw[0]:02X}  Ver={fw[1]}.{fw[2]}")

        if not pn532_sam_configuration(bus, address, debug=args.debug):
            print("WARNING: SAMConfiguration got no response — reader may be unstable")
        else:
            print("  SAMConfiguration OK")

        print("\nReady — scanning every 15 seconds.\n")

        # ── Polling loop ──────────────────────────────────────────────────
        last_uid = None
        try:
            while True:
                if args.debug:
                    print("--- InListPassiveTarget ---")

                uid = pn532_read_passive_target(bus, address, debug=args.debug)

                if uid:
                    pn532_release(bus, address, debug=args.debug)
                    if uid != last_uid:
                        print(f"TAG  {uid}")
                        last_uid = uid
                    if args.once:
                        break
                else:
                    if last_uid:
                        print("removed")
                        last_uid = None

                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == '__main__':
    main()
