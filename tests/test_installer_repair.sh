#!/bin/bash
set -eu

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/nfc-installer-repair.XXXXXX")"
trap 'rm -rf "${TEST_ROOT}"' EXIT

TEST_HOME="${TEST_ROOT}/home"
TEST_CONFIG="${TEST_HOME}/printer_data/config"
TEST_EXTRAS="${TEST_HOME}/klipper/klippy/extras"
mkdir -p "${TEST_CONFIG}/nfc" "${TEST_EXTRAS}"

printf '%s\n' \
    '# user printer config' \
    '[include mainsail.cfg]' \
    '[include nfc/nfc_reader_hw.cfg]' \
    > "${TEST_CONFIG}/printer.cfg"

printf '%s\n' \
    '[server]' \
    'host: 0.0.0.0' \
    '' \
    '[update_manager Happy-Hare-RFID-Reader]' \
    'type: git_repo' \
    "path: ${REPO_DIR}" \
    'origin: https://github.com/cwiegert/Happy-Hare-RFID-Reader.git' \
    'primary_branch: main' \
    'managed_services: klipper' \
    'install_script: install.sh' \
    > "${TEST_CONFIG}/moonraker.conf"

printf '%s\n' \
    '[nfc_gate]' \
    'spoolman_url: http://user-value:7912' \
    'poll_interval: 77' \
    > "${TEST_CONFIG}/nfc/nfc_reader.cfg"

printf '%s\n' \
    '[nfc_gate lane0]' \
    'i2c_mcu: custom_lane_mcu' \
    > "${TEST_CONFIG}/nfc/nfc_reader_hw.cfg"

printf '%s\n' '# locally customized macros' \
    > "${TEST_CONFIG}/nfc/nfc_macros.cfg"

READER_BEFORE="$(cksum "${TEST_CONFIG}/nfc/nfc_reader.cfg")"
HW_BEFORE="$(cksum "${TEST_CONFIG}/nfc/nfc_reader_hw.cfg")"

HOME="${TEST_HOME}" \
RFID_READER_ALLOW_DEV_PATH=yes \
RFID_READER_PRINTER_CONFIG="${TEST_CONFIG}" \
RFID_READER_KLIPPER_EXTRAS="${TEST_EXTRAS}" \
bash "${REPO_DIR}/install.sh" -r >/dev/null

test "$(cksum "${TEST_CONFIG}/nfc/nfc_reader.cfg")" = "${READER_BEFORE}"
test "$(cksum "${TEST_CONFIG}/nfc/nfc_reader_hw.cfg")" = "${HW_BEFORE}"
test -L "${TEST_CONFIG}/nfc/nfc_macros.cfg"
test "$(readlink "${TEST_CONFIG}/nfc/nfc_macros.cfg")" = \
    "${REPO_DIR}/config/nfc_macros.cfg"
find "${TEST_CONFIG}/nfc" -maxdepth 1 \
    -name 'nfc_macros.cfg.pre-read-only-*' | grep -q .

test "$(readlink "${TEST_EXTRAS}/nfc_gate.py")" = \
    "${REPO_DIR}/klippy/extras/nfc_gate.py"
test "$(readlink "${TEST_EXTRAS}/mmu_nfc_endstop.py")" = \
    "${REPO_DIR}/klippy/extras/mmu_nfc_endstop.py"
test "$(readlink "${TEST_EXTRAS}/nfc_gates")" = \
    "${REPO_DIR}/klippy/extras/nfc_gates"

python3 - "${TEST_CONFIG}/printer.cfg" <<'PYEOF'
import sys

with open(sys.argv[1]) as f:
    lines = [line.strip() for line in f]
wanted = [
    '[include nfc/nfc_reader.cfg]',
    '[include nfc/nfc_macros.cfg]',
    '[include nfc/nfc_reader_hw.cfg]',
]
positions = [lines.index(item) for item in wanted]
assert positions == sorted(positions)
assert all(lines.count(item) == 1 for item in wanted)
PYEOF

grep -qF '[update_manager Happy-Hare-RFID-Reader]' \
    "${TEST_CONFIG}/moonraker.conf"
! grep -qF 'install_script:' "${TEST_CONFIG}/moonraker.conf"
grep -qF 'install_schema=1' "${TEST_CONFIG}/nfc/.install-state"
grep -qF 'layout=lane' "${TEST_CONFIG}/nfc/.install-state"

BACKUPS_BEFORE="$(find "${TEST_CONFIG}" -type f -name '*.pre-*' | wc -l | tr -d ' ')"
HOME="${TEST_HOME}" \
RFID_READER_ALLOW_DEV_PATH=yes \
RFID_READER_PRINTER_CONFIG="${TEST_CONFIG}" \
RFID_READER_KLIPPER_EXTRAS="${TEST_EXTRAS}" \
bash "${REPO_DIR}/install.sh" -r >/dev/null
BACKUPS_AFTER="$(find "${TEST_CONFIG}" -type f -name '*.pre-*' | wc -l | tr -d ' ')"
test "${BACKUPS_AFTER}" = "${BACKUPS_BEFORE}"

OUTPUT="$(HOME="${TEST_HOME}" \
    RFID_READER_ALLOW_DEV_PATH=yes \
    RFID_READER_PRINTER_CONFIG="${TEST_CONFIG}" \
    RFID_READER_KLIPPER_EXTRAS="${TEST_EXTRAS}" \
    bash "${REPO_DIR}/install.sh")"
printf '%s\n' "${OUTPUT}" | grep -q 'already installed and healthy'

# A pre-marker hybrid install must retain both hardware includes during repair.
printf '%s\n' \
    '[nfc_gate shared]' \
    'i2c_mcu: custom_shared_mcu' \
    > "${TEST_CONFIG}/nfc/nfc_reader_shared.cfg"
printf '%s\n' '[include nfc/nfc_reader_shared.cfg]' \
    >> "${TEST_CONFIG}/printer.cfg"
rm "${TEST_CONFIG}/nfc/.install-state"

HOME="${TEST_HOME}" \
RFID_READER_ALLOW_DEV_PATH=yes \
RFID_READER_PRINTER_CONFIG="${TEST_CONFIG}" \
RFID_READER_KLIPPER_EXTRAS="${TEST_EXTRAS}" \
bash "${REPO_DIR}/install.sh" -r >/dev/null

grep -qF 'layout=hybrid' "${TEST_CONFIG}/nfc/.install-state"
grep -qF '[include nfc/nfc_reader_hw.cfg]' "${TEST_CONFIG}/printer.cfg"
grep -qF '[include nfc/nfc_reader_shared.cfg]' "${TEST_CONFIG}/printer.cfg"

# A fresh install runs interactively once and records a healthy installation.
FRESH_HOME="${TEST_ROOT}/fresh-home"
FRESH_CONFIG="${FRESH_HOME}/printer_data/config"
FRESH_EXTRAS="${FRESH_HOME}/klipper/klippy/extras"
mkdir -p "${FRESH_CONFIG}" "${FRESH_EXTRAS}"
printf '%s\n' '[include mainsail.cfg]' > "${FRESH_CONFIG}/printer.cfg"
printf '%s\n' '[server]' 'host: 0.0.0.0' > "${FRESH_CONFIG}/moonraker.conf"

printf '\n%.0s' {1..20} | \
HOME="${FRESH_HOME}" \
RFID_READER_ALLOW_DEV_PATH=yes \
RFID_READER_PRINTER_CONFIG="${FRESH_CONFIG}" \
RFID_READER_KLIPPER_EXTRAS="${FRESH_EXTRAS}" \
bash "${REPO_DIR}/install.sh" >/dev/null

grep -qF 'install_schema=1' "${FRESH_CONFIG}/nfc/.install-state"
grep -qF 'layout=lane' "${FRESH_CONFIG}/nfc/.install-state"
test -L "${FRESH_CONFIG}/nfc/nfc_macros.cfg"
! grep -qF 'install_script:' "${FRESH_CONFIG}/moonraker.conf"

FRESH_OUTPUT="$(HOME="${FRESH_HOME}" \
    RFID_READER_ALLOW_DEV_PATH=yes \
    RFID_READER_PRINTER_CONFIG="${FRESH_CONFIG}" \
    RFID_READER_KLIPPER_EXTRAS="${FRESH_EXTRAS}" \
    bash "${REPO_DIR}/install.sh")"
printf '%s\n' "${FRESH_OUTPUT}" | grep -q 'already installed and healthy'

# Reconfigure reruns the wizard only when explicitly requested and preserves a
# complete pre-change NFC configuration.
FRESH_READER_BEFORE="$(cksum "${FRESH_CONFIG}/nfc/nfc_reader.cfg" | awk '{print $1, $2}')"
printf '\n%.0s' {1..20} | \
HOME="${FRESH_HOME}" \
RFID_READER_ALLOW_DEV_PATH=yes \
RFID_READER_PRINTER_CONFIG="${FRESH_CONFIG}" \
RFID_READER_KLIPPER_EXTRAS="${FRESH_EXTRAS}" \
bash "${REPO_DIR}/install.sh" --reconfigure >/dev/null

RECONFIGURE_BACKUP="$(find "${FRESH_CONFIG}" -maxdepth 1 -type d \
    -name 'nfc_pre_reconfigure_*' | head -n 1)"
test -n "${RECONFIGURE_BACKUP}"
test "$(cksum "${RECONFIGURE_BACKUP}/nfc_reader.cfg" | awk '{print $1, $2}')" = \
    "${FRESH_READER_BEFORE}"
grep -qF 'install_schema=1' "${FRESH_CONFIG}/nfc/.install-state"

# A missing moonraker.conf must NOT abort a fresh install. Web updates are a
# convenience layered on top of the reader; their absence degrades to a warning,
# still writes the install-state marker, and leaves a recoverable install rather
# than a half-written one. (Regression guard for the set -e post-write abort.)
NOMOON_HOME="${TEST_ROOT}/nomoon-home"
NOMOON_CONFIG="${NOMOON_HOME}/printer_data/config"
NOMOON_EXTRAS="${NOMOON_HOME}/klipper/klippy/extras"
mkdir -p "${NOMOON_CONFIG}" "${NOMOON_EXTRAS}"
printf '%s\n' '[include mainsail.cfg]' > "${NOMOON_CONFIG}/printer.cfg"
# deliberately no moonraker.conf

# The command substitution would fail under set -e if install.sh aborted (exit
# non-zero), so this line alone catches a regression of the blocker.
NOMOON_OUT="$(printf '\n%.0s' {1..25} | \
    HOME="${NOMOON_HOME}" \
    RFID_READER_ALLOW_DEV_PATH=yes \
    RFID_READER_PRINTER_CONFIG="${NOMOON_CONFIG}" \
    RFID_READER_KLIPPER_EXTRAS="${NOMOON_EXTRAS}" \
    bash "${REPO_DIR}/install.sh")"

# The install still completed: state marker and read-only macro link are present.
grep -qF 'install_schema=1' "${NOMOON_CONFIG}/nfc/.install-state"
grep -qF 'layout=lane' "${NOMOON_CONFIG}/nfc/.install-state"
test -L "${NOMOON_CONFIG}/nfc/nfc_macros.cfg"
# The installer did not fabricate a moonraker.conf it was never given.
test ! -e "${NOMOON_CONFIG}/moonraker.conf"
# The user is warned and handed a concrete recovery path, not left guessing.
printf '%s\n' "${NOMOON_OUT}" | grep -qF 'moonraker.conf was not found'
printf '%s\n' "${NOMOON_OUT}" | grep -qF 'bash install.sh -r'

echo "installer repair test: PASS"
