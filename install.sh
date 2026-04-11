#!/bin/bash
# =============================================================================
# EMU NFC Gate Reader — Install Script
# =============================================================================
# What this script does:
#   1. Symlinks the Python extras into ~/klipper/klippy/extras/ so that
#      git pull + Klipper restart is all that is needed to update the code.
#      Two symlinks are created:
#        nfc_gate.py   — entry point for [nfc_gate laneN]  (Path C / EBB42)
#        nfc_gates/    — package for [nfc_gates]            (Paths A & B / Pico)
#
#   2. Installs config files into ~/printer_data/config/NFC/ using a
#      non-destructive merge strategy:
#        - If a file does not exist yet, it is copied from the repo template.
#        - If a file already exists, only sections that are present in the
#          repo template but MISSING from the existing file are appended.
#          Sections the user has already configured are never overwritten.
#
# Usage:
#   bash install.sh
#
# Can be run from anywhere — the script resolves its own location.
# =============================================================================

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_EXTRAS="${HOME}/klipper/klippy/extras"
PRINTER_CONFIG="${HOME}/printer_data/config"
NFC_CONFIG_DIR="${PRINTER_CONFIG}/NFC"

# ── Verify Klipper is present ─────────────────────────────────────────────────
if [ ! -d "${KLIPPER_EXTRAS}" ]; then
    echo "ERROR: Klipper extras directory not found at ${KLIPPER_EXTRAS}"
    echo "       Is Klipper installed? Expected: ~/klipper/klippy/extras/"
    exit 1
fi

# ── Verify printer config directory is present ────────────────────────────────
if [ ! -d "${PRINTER_CONFIG}" ]; then
    echo "ERROR: Printer config directory not found at ${PRINTER_CONFIG}"
    echo "       Expected: ~/printer_data/config/"
    exit 1
fi

# ── Symlink Python extras into Klipper ───────────────────────────────────────

# Remove the old flat nfc_gates.py symlink if it exists from a previous install
if [ -L "${KLIPPER_EXTRAS}/nfc_gates.py" ]; then
    echo "Removing old nfc_gates.py symlink (replaced by nfc_gates/ package)..."
    rm "${KLIPPER_EXTRAS}/nfc_gates.py"
fi

echo "Linking nfc_gate.py..."
ln -sfn "${REPO_DIR}/klippy/extras/nfc_gate.py" "${KLIPPER_EXTRAS}/nfc_gate.py"

echo "Linking nfc_gates/ package..."
ln -sfn "${REPO_DIR}/klippy/extras/nfc_gates" "${KLIPPER_EXTRAS}/nfc_gates"

# ── Create NFC config directory if it does not exist ─────────────────────────
mkdir -p "${NFC_CONFIG_DIR}"

# ── Merge helper — copy file or append missing sections ──────────────────────
#
# Usage: merge_config <src> <dst>
#
# If <dst> does not exist: copies <src> to <dst> and reports [copied].
# If <dst> exists: parses Klipper-style section headers ( [section name] ) in
# both files.  For each section present in <src> but absent in <dst>, the full
# section block is appended to <dst>.  Existing sections are left untouched.
# Reports [skip] / [append] per section, or "(no new sections)" if up-to-date.
#
merge_config() {
    local src="$1"
    local dst="$2"
    local name
    name="$(basename "${dst}")"

    if [ ! -f "${dst}" ]; then
        cp "${src}" "${dst}"
        echo "  [copied]   ${name}"
        return
    fi

    echo "  [exists]   ${name} — checking for missing sections..."
    python3 - "${src}" "${dst}" <<'PYEOF' \
        || echo "    WARNING: merge script failed — ${name} left unchanged"
import sys
import re

src_path, dst_path = sys.argv[1], sys.argv[2]


def parse_sections(text):
    """Return (preamble_str, [(header_str, body_str), ...]).

    preamble_str — all text before the first [section] line.
    header_str   — the [section name] line, stripped of trailing whitespace.
    body_str     — all lines after the header up to (not including) the next
                   header, as a single string with newlines preserved.
    """
    preamble = []
    sections = []
    current_header = None
    current_body = []
    in_preamble = True

    for line in text.splitlines(keepends=True):
        if re.match(r'^\[', line):
            if in_preamble:
                in_preamble = False
                preamble = current_body[:]
            elif current_header is not None:
                sections.append((current_header, ''.join(current_body)))
            current_header = line.rstrip('\r\n')
            current_body = []
        else:
            current_body.append(line)

    if current_header is not None:
        sections.append((current_header, ''.join(current_body)))

    return ''.join(preamble), sections


with open(src_path) as f:
    src_text = f.read()
with open(dst_path) as f:
    dst_text = f.read()

_, src_sections = parse_sections(src_text)
_, dst_sections = parse_sections(dst_text)
dst_headers = {h for h, _ in dst_sections}

appended = []
skipped = []

with open(dst_path, 'a') as out:
    for header, body in src_sections:
        if header in dst_headers:
            skipped.append(header)
        else:
            appended.append(header)
            # Ensure there is a newline before the appended block
            if dst_text and not dst_text.endswith('\n'):
                out.write('\n')
                dst_text += '\n'
            out.write('\n' + header + '\n' + body)
            dst_text += '\n' + header + '\n' + body

for h in skipped:
    print('    [skip]    {}'.format(h))
for h in appended:
    print('    [append]  {}'.format(h))
if not appended:
    print('    (no new sections — file is up to date)')
PYEOF
}

# ── Install / merge config files ──────────────────────────────────────────────
echo ""
echo "Installing config files to ${NFC_CONFIG_DIR}/..."
echo ""

merge_config "${REPO_DIR}/config/nfc_vars.cfg"       "${NFC_CONFIG_DIR}/nfc_vars.cfg"
merge_config "${REPO_DIR}/config/nfc_macros.cfg"     "${NFC_CONFIG_DIR}/nfc_macros.cfg"
merge_config "${REPO_DIR}/config/rc522_spi.cfg"      "${NFC_CONFIG_DIR}/rc522_spi.cfg"
merge_config "${REPO_DIR}/config/pn532_spi.cfg"      "${NFC_CONFIG_DIR}/pn532_spi.cfg"
merge_config "${REPO_DIR}/config/pn532_pico_i2c.cfg" "${NFC_CONFIG_DIR}/pn532_pico_i2c.cfg"
merge_config "${REPO_DIR}/config/pn532_i2C.cfg"      "${NFC_CONFIG_DIR}/pn532_i2C.cfg"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Install complete."
echo ""
echo "  Python extras (symlinked — auto-updates with git pull):"
echo "    ${KLIPPER_EXTRAS}/nfc_gate.py  ->  ${REPO_DIR}/klippy/extras/nfc_gate.py"
echo "    ${KLIPPER_EXTRAS}/nfc_gates    ->  ${REPO_DIR}/klippy/extras/nfc_gates/"
echo ""
echo "  Config files in ${NFC_CONFIG_DIR}/:"
echo "    nfc_vars.cfg       ← user settings (Spoolman URL, poll interval, debug)"
echo "    nfc_macros.cfg     ← do not edit"
echo "    rc522_spi.cfg      ← SPI / RC522 on Pico"
echo "    pn532_spi.cfg      ← SPI / PN532 on Pico"
echo "    pn532_pico_i2c.cfg ← I2C / PN532 on Pico"
echo "    pn532_i2C.cfg      ← I2C / PN532 on EBB42 lane boards"
echo ""
echo "Next steps (first install only):"
echo ""
echo "  1. Edit ~/printer_data/config/NFC/nfc_vars.cfg"
echo "     Set spoolman_url to your Spoolman instance URL."
echo ""
echo "  2. Add includes to printer.cfg — pick ONE hardware path:"
echo ""
echo "     SPI / RC522 on Pico:"
echo "       [include NFC/nfc_vars.cfg]"
echo "       [include NFC/nfc_macros.cfg]"
echo "       [include NFC/rc522_spi.cfg]"
echo ""
echo "     SPI / PN532 on Pico:"
echo "       [include NFC/nfc_vars.cfg]"
echo "       [include NFC/nfc_macros.cfg]"
echo "       [include NFC/pn532_spi.cfg]"
echo ""
echo "     I2C / PN532 on Pico:"
echo "       [include NFC/nfc_vars.cfg]"
echo "       [include NFC/nfc_macros.cfg]"
echo "       [include NFC/pn532_pico_i2c.cfg]"
echo ""
echo "     I2C / PN532 on EBB42 lane boards:"
echo "       [include NFC/nfc_vars.cfg]"
echo "       [include NFC/nfc_macros.cfg]"
echo "       [include NFC/pn532_i2C.cfg]"
echo ""
echo "  3. Restart Klipper:"
echo "     sudo systemctl restart klipper"
echo ""
echo "  4. Add the Moonraker update manager entry to moonraker.conf"
echo "     (see Readme.md for the block to paste in)"
echo ""
echo "  ── Upgrading from a previous install? ────────────────────────────────────"
echo "  If your nfc_vars.cfg still contains [nfc_gate], move those settings into"
echo "  the [nfc_gate] section in nfc_gate_i2c_pn532.cfg, then remove [nfc_gate]"
echo "  from nfc_vars.cfg to avoid duplicate-section errors from Klipper."
echo ""
