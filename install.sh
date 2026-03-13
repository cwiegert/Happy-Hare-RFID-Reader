#!/bin/bash
# =============================================================================
# EMU NFC Gate Reader — Install Script
# =============================================================================
# Creates symlinks from this repo into Klipper's extras directory so that
# `git pull` + Klipper restart is all that is needed to apply updates.
#
# Usage:
#   bash install.sh
#
# Run from the cloned repo directory, or from anywhere — the script resolves
# its own location automatically.
# =============================================================================

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_EXTRAS="${HOME}/klipper/klippy/extras"

# ── Verify Klipper is present ─────────────────────────────────────────────────
if [ ! -d "${KLIPPER_EXTRAS}" ]; then
    echo "ERROR: Klipper extras directory not found at ${KLIPPER_EXTRAS}"
    echo "       Is Klipper installed? Expected: ~/klipper/klippy/extras/"
    exit 1
fi

# ── Create symlinks ───────────────────────────────────────────────────────────
echo "Linking nfc_gates package..."
ln -sfn "${REPO_DIR}/klippy/extras/nfc_gates" "${KLIPPER_EXTRAS}/nfc_gates"

echo "Linking nfc_gate.py..."
ln -sfn "${REPO_DIR}/klippy/extras/nfc_gate.py" "${KLIPPER_EXTRAS}/nfc_gate.py"

echo ""
echo "✓ Install complete.  Symlinks created:"
echo "    ${KLIPPER_EXTRAS}/nfc_gates  ->  ${REPO_DIR}/klippy/extras/nfc_gates"
echo "    ${KLIPPER_EXTRAS}/nfc_gate.py  ->  ${REPO_DIR}/klippy/extras/nfc_gate.py"
echo ""
echo "Next steps:"
echo "  1. Copy config files to printer_data/config/:"
echo "     cp ${REPO_DIR}/config/nfc_macros.cfg ~/printer_data/config/"
echo "     cp ${REPO_DIR}/config/nfc_vars.cfg    ~/printer_data/config/"
echo ""
echo "     SPI / RC522:  cp ${REPO_DIR}/config/nfc_gates_spi_rc522.cfg ~/printer_data/config/"
echo "     I2C / PN532:  cp ${REPO_DIR}/config/nfc_gate_i2c_pn532.cfg  ~/printer_data/config/"
echo ""
echo "  2. Edit nfc_vars.cfg — set spoolman_url to your Spoolman instance."
echo ""
echo "  3. Add includes to printer.cfg (nfc_vars.cfg must come first):"
echo "     [include nfc_vars.cfg]"
echo "     [include nfc_macros.cfg]"
echo "     [include nfc_gates_spi_rc522.cfg]   # SPI path"
echo "     — or —"
echo "     [include nfc_vars.cfg]"
echo "     [include nfc_macros.cfg]"
echo "     [include nfc_gate_i2c_pn532.cfg]    # I2C path"
echo ""
echo "  4. Restart Klipper:"
echo "     sudo systemctl restart klipper"
echo ""
echo "  4. Add the Moonraker update manager section to moonraker.conf:"
cat <<'EOF'

# ── paste into moonraker.conf ─────────────────────────────────────────────────
[update_manager emu_nfc_reader]
type: git_repo
path: ~/emu-nfc-reader
origin: YOUR_REPO_URL_HERE
primary_branch: main
managed_services: klipper
install_script: install.sh
# ─────────────────────────────────────────────────────────────────────────────
EOF
