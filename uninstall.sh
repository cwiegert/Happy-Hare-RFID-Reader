#!/bin/bash
# =============================================================================
# EMU NFC Gate Reader — Uninstall Script
# =============================================================================
# What this script does automatically:
#   1. Removes ~/klipper/klippy/extras/nfc_gate.py  (symlink)
#   2. Removes ~/klipper/klippy/extras/nfc_gates    (symlink to package dir)
#   3. Removes ~/klipper/klippy/extras/nfc_gates.py (flat file, if present
#      from a previous install)
#   4. Backs up ~/printer_data/config/nfc/ to nfc_removed_<timestamp>/
#   5. Restarts Klipper
#
# What you must do manually afterward:
#   - Remove the [include nfc/...] lines from printer.cfg
#   - Remove the [update_manager emu_nfc_reader] block from moonraker.conf
#     and restart Moonraker: sudo systemctl restart moonraker
#   - Optionally delete the repo: rm -rf ~/emu-nfc-reader
#
# Usage:
#   bash uninstall.sh
# =============================================================================

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_EXTRAS="${HOME}/klipper/klippy/extras"
PRINTER_CONFIG="${HOME}/printer_data/config"
NFC_CONFIG_DIR="${PRINTER_CONFIG}/nfc"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

echo ""
echo "EMU NFC Gate Reader — Uninstall"
echo "================================"
echo ""

# ── Confirmation ──────────────────────────────────────────────────────────────
read -r -p "This will remove the NFC extras and config files. Continue? [y/N] " confirm
case "$confirm" in
    [yY][eE][sS]|[yY]) ;;
    *)
        echo "Aborted."
        exit 0
        ;;
esac
echo ""

# ── Remove Klipper extra symlinks ─────────────────────────────────────────────
echo "Removing Klipper extra symlinks..."

for target in \
    "${KLIPPER_EXTRAS}/nfc_gate.py" \
    "${KLIPPER_EXTRAS}/nfc_gates" \
    "${KLIPPER_EXTRAS}/nfc_gates.py"
do
    if [ -L "$target" ]; then
        rm "$target"
        echo "  Removed: $target"
    elif [ -e "$target" ]; then
        echo "  WARNING: $target exists but is not a symlink — skipping (remove manually)"
    else
        echo "  Not found (already removed): $target"
    fi
done

# ── Remove standalone scanner ─────────────────────────────────────────────────
echo ""
if [ -f "${HOME}/pn532_scan.py" ]; then
    rm "${HOME}/pn532_scan.py"
    echo "Removed: ${HOME}/pn532_scan.py"
else
    echo "Standalone scanner not found — already removed."
fi

# ── Back up and remove NFC config directory ───────────────────────────────────
echo ""
if [ -d "${NFC_CONFIG_DIR}" ]; then
    BACKUP_DIR="${PRINTER_CONFIG}/nfc_removed_${TIMESTAMP}"
    echo "Backing up nfc config to $(basename "${BACKUP_DIR}")..."
    mv "${NFC_CONFIG_DIR}" "${BACKUP_DIR}"
    echo "  Saved: ${BACKUP_DIR}"
    echo "  Delete the backup when you no longer need it:"
    echo "    rm -rf ${BACKUP_DIR}"
elif [ -e "${NFC_CONFIG_DIR}" ]; then
    rmdir "${NFC_CONFIG_DIR}" 2>/dev/null && echo "  Removed empty ${NFC_CONFIG_DIR}" \
        || echo "  WARNING: ${NFC_CONFIG_DIR} exists but is not a directory — remove manually"
else
    echo "NFC config directory not found — nothing to back up."
fi

# ── Restart Klipper ───────────────────────────────────────────────────────────
echo ""
echo "Restarting Klipper..."
if sudo systemctl restart klipper; then
    echo "  Klipper restarted."
else
    echo "  WARNING: Klipper restart failed — restart manually:"
    echo "    sudo systemctl restart klipper"
fi

# ── Manual steps reminder ─────────────────────────────────────────────────────
echo ""
echo "Done. Two manual steps remain:"
echo ""
echo "  1. Remove the nfc include lines from printer.cfg:"
echo "       [include nfc/nfc_reader.cfg]"
echo "       [include nfc/nfc_macros.cfg]"
echo "       [include nfc/nfc_reader_hw.cfg]"
echo "     If you have older experimental SPI/Pico include lines, remove those too."
echo ""
echo "  2. Remove the update manager block from moonraker.conf:"
echo "       [update_manager emu_nfc_reader]"
echo "       ..."
echo "     Then restart Moonraker:"
echo "       sudo systemctl restart moonraker"
echo ""

# ── Optional: remove the repo clone ──────────────────────────────────────────
read -r -p "Remove the repo clone at ${REPO_DIR}? [y/N] " remove_repo
case "$remove_repo" in
    [yY][eE][sS]|[yY])
        rm -rf "${REPO_DIR}"
        echo "  Repo removed."
        ;;
    *)
        echo "  Repo kept at ${REPO_DIR}"
        ;;
esac

echo ""
echo "Uninstall complete."
echo ""
