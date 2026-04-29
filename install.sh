#!/bin/bash
# =============================================================================
# EMU NFC Gate Reader — Install Script
# =============================================================================
# What this script does:
#   1. Symlinks the Python extras into ~/klipper/klippy/extras/ so that
#      git pull + Klipper restart is all that is needed to update the code.
#      Two symlinks are created:
#        nfc_gate.py   — entry point for [nfc_gate laneN]
#        nfc_gates/    — shared implementation package
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

# Remove legacy porting-reference symlink if an older install exposed it.
LEGACY_HH_PORTING="${KLIPPER_EXTRAS}/HH_code - for porting"
if [ -L "${LEGACY_HH_PORTING}" ]; then
    echo "Removing legacy HH_code - for porting symlink (not installed at runtime)..."
    rm "${LEGACY_HH_PORTING}"
elif [ -e "${LEGACY_HH_PORTING}" ]; then
    echo "WARNING: ${LEGACY_HH_PORTING} exists but is not a symlink — leaving it untouched."
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

# ── nfc_vars.cfg migrations ──────────────────────────────────────────────────
#
# merge_config intentionally does not overwrite existing sections.  When a key
# is removed or added inside [nfc_gate], handle that as a small migration here.
migrate_nfc_vars() {
    local dst="$1"
    local name
    name="$(basename "${dst}")"

    if [ ! -f "${dst}" ]; then
        return
    fi

    python3 - "${dst}" <<'PYEOF' \
        || echo "    WARNING: migration script failed — ${name} left unchanged"
import re
import sys

path = sys.argv[1]

with open(path) as f:
    lines = f.readlines()

changed = False
out = []
removed_scan_interval = False
removed_scan_max_mm = False
removed_scan_settle_time = False
has_scan_poll_interval = any(
    re.match(r'^\s*scan_poll_interval\s*:', line) for line in lines)

old_scan_interval_comment = (
    'Seconds between NFC read attempts during scan mode',
    'This must be long enough for MMU_TEST_MOVE',
    'the next read fires',
    'commands stack up',
    'conservative floor',
    'motor is still settling',
)

for line in lines:
    if re.match(r'^\s*scan_interval\s*:', line):
        # Drop the old explanatory block from the template when present.
        while out and out[-1].lstrip().startswith('#') and any(
                phrase in out[-1] for phrase in old_scan_interval_comment):
            out.pop()
        removed_scan_interval = True
        changed = True
        continue
    if re.match(r'^\s*scan_max_mm\s*:', line):
        while out and out[-1].lstrip().startswith('#') and (
                'Maximum total advance' in out[-1]
                or 'one full spool rotation' in out[-1]):
            out.pop()
        removed_scan_max_mm = True
        changed = True
        continue
    if re.match(r'^\s*scan_settle_time\s*:', line):
        while out and out[-1].lstrip().startswith('#') and (
                'Extra seconds to wait' in out[-1]
                or 'Lower values reduce time between jogs' in out[-1]
                or 'MCU needs more time to settle' in out[-1]):
            out.pop()
        removed_scan_settle_time = True
        changed = True
        continue
    out.append(line)

if not has_scan_poll_interval:
    insert = [
        '\n',
        '# Seconds between NFC read attempts while scan-jog is active.  Jog chunk cadence\n',
        '# is calculated automatically from scan_jog_mm / Happy Hare gear_short_move_speed\n',
        '# so there is no manual move interval to tune.\n',
        'scan_poll_interval:  0.1\n',
    ]
    inserted = False
    for i, line in enumerate(out):
        if re.match(r'^\s*scan_jog_mm\s*:', line):
            out[i + 1:i + 1] = insert
            inserted = True
            changed = True
            break
    if not inserted:
        out.extend(insert)
        changed = True

if changed:
    with open(path, 'w') as f:
        f.writelines(out)
    if removed_scan_interval:
        print('    [migrate] removed deprecated scan_interval from {}'.format(path))
    if removed_scan_max_mm:
        print('    [migrate] removed scan_max_mm from {}'.format(path))
    if removed_scan_settle_time:
        print('    [migrate] removed scan_settle_time from {}'.format(path))
    if not has_scan_poll_interval:
        print('    [migrate] added scan_poll_interval to {}'.format(path))
PYEOF
}

# ── Install / merge config files ──────────────────────────────────────────────
echo ""
echo "Installing config files to ${NFC_CONFIG_DIR}/..."
echo ""

merge_config "${REPO_DIR}/config/nfc_vars.cfg"   "${NFC_CONFIG_DIR}/nfc_vars.cfg"
migrate_nfc_vars "${NFC_CONFIG_DIR}/nfc_vars.cfg"
merge_config "${REPO_DIR}/config/nfc_macros.cfg" "${NFC_CONFIG_DIR}/nfc_macros.cfg"
merge_config "${REPO_DIR}/config/pn532_i2C.cfg"  "${NFC_CONFIG_DIR}/pn532_i2C.cfg"

# ── Install standalone scanner ────────────────────────────────────────────────
TOOLS_DST="${HOME}/pn532_scan.py"
echo "Installing standalone scanner to ${TOOLS_DST}..."
cp "${REPO_DIR}/tools/pn532_scan.py" "${TOOLS_DST}"
chmod +x "${TOOLS_DST}"
echo "  [copied]   pn532_scan.py"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Install complete."
echo ""
echo "  Python extras (symlinked — auto-updates with git pull):"
echo "    ${KLIPPER_EXTRAS}/nfc_gate.py  ->  ${REPO_DIR}/klippy/extras/nfc_gate.py"
echo "    ${KLIPPER_EXTRAS}/nfc_gates    ->  ${REPO_DIR}/klippy/extras/nfc_gates/"
echo ""
echo "  Standalone scanner:"
echo "    ${TOOLS_DST}"
echo ""
echo "  Config files in ${NFC_CONFIG_DIR}/:"
echo "    nfc_vars.cfg   ← user settings (Spoolman URL, poll interval, debug)"
echo "    nfc_macros.cfg ← Happy Hare handoff macros"
echo "    pn532_i2C.cfg  ← PN532 over I2C on lane boards"
echo ""
echo "Next steps (first install only):"
echo ""
echo "  1. Edit ~/printer_data/config/NFC/nfc_vars.cfg"
echo "     Set spoolman_url to auto or to your Spoolman instance URL."
echo ""
echo "  2. Add includes to printer.cfg:"
echo "       [include NFC/nfc_vars.cfg]"
echo "       [include NFC/nfc_macros.cfg]"
echo "       [include NFC/pn532_i2C.cfg]"
echo ""
echo "  3. Restart Klipper:"
echo "     sudo systemctl restart klipper"
echo ""
echo "  4. Update and flash Klipper on each lane MCU / EBB42 board used by NFC."
echo ""
echo "  5. Add the Moonraker update manager entry to moonraker.conf"
echo "     (see Readme.md for the block to paste in)"
echo ""
echo "  To test a PN532 wired directly to this Pi's GPIO I2C pins:"
echo "     python3 ~/pn532_scan.py --scan-bus"
echo "     python3 ~/pn532_scan.py"
echo ""
