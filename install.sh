#!/bin/bash
# =============================================================================
# EMU NFC Gate Reader — Install Script
# =============================================================================
# What this script does:
#   1. Symlinks the Python extras into ~/klipper/klippy/extras/ so that
#      git pull + Klipper restart is all that is needed to update the code.
#      Two symlinks are created:
#        nfc_gate.py   — entry point for [nfc_gate laneN]
#        mmu_nfc_endstop.py — virtual endstop wrapper for lane readers
#        nfc_gates/    — shared implementation package
#
#   2. Installs config files into ~/printer_data/config/nfc/ using a
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
RFID_READER_REPO_URL="${RFID_READER_REPO_URL:-https://github.com/cwiegert/Happy-Hare-RFID-Reader.git}"
RFID_READER_INSTALL_DIR="${RFID_READER_INSTALL_DIR:-${HOME}/rfid-reader}"
RFID_READER_LEGACY_DIR="${RFID_READER_LEGACY_DIR:-${HOME}/emu-nfc-reader}"
KLIPPER_EXTRAS="${RFID_READER_KLIPPER_EXTRAS:-${HOME}/klipper/klippy/extras}"
PRINTER_CONFIG="${RFID_READER_PRINTER_CONFIG:-${HOME}/printer_data/config}"
PRINTER_CFG="${PRINTER_CONFIG}/printer.cfg"
NFC_CONFIG_DIR="${PRINTER_CONFIG}/nfc"
NFC_READER_CFG="${NFC_CONFIG_DIR}/nfc_reader.cfg"
NFC_READER_HW_CFG="${NFC_CONFIG_DIR}/nfc_reader_hw.cfg"
NFC_READER_SHARED_CFG="${NFC_CONFIG_DIR}/nfc_reader_shared.cfg"
NFC_SHARED_READER_CFG="${NFC_CONFIG_DIR}/nfc_shared_reader.cfg"
MMU_HW_CFG="${PRINTER_CONFIG}/mmu/base/mmu_hardware.cfg"

if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"
    RESET="$(printf '\033[0m')"
else
    BOLD="" RESET=""
fi

choice_style() {
    if [ "$1" = "$2" ]; then
        printf '%s%s%s' "${BOLD}" "$1" "${RESET}"
    else
        printf '%s' "$1"
    fi
}

prompt_style() {
    printf '%s' "$1"
}

print_banner() {
    printf '%s' "${BOLD}"
    cat <<'EOF'
███╗   ██╗███████╗ ██████╗
████╗  ██║██╔════╝██╔════╝
██╔██╗ ██║█████╗  ██║
██║╚██╗██║██╔══╝  ██║
██║ ╚████║██║     ╚██████╗
╚═╝  ╚═══╝╚═╝      ╚═════╝

██████╗ ███████╗ █████╗ ██████╗ ███████╗██████╗
██╔══██╗██╔════╝██╔══██╗██╔══██╗██╔════╝██╔══██╗
██████╔╝█████╗  ███████║██║  ██║█████╗  ██████╔╝
██╔══██╗██╔══╝  ██╔══██║██║  ██║██╔══╝  ██╔══██╗
██║  ██║███████╗██║  ██║██████╔╝███████╗██║  ██║
╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═════╝ ╚══════╝╚═╝  ╚═╝
EOF
    printf '%s\n' "${RESET}"
}

prompt_with_default() {
    local __var_name="$1"
    local prompt_text="$2"
    local default_value="$3"
    local reply
    read -r -p "$(prompt_style "${prompt_text}") [${BOLD}${default_value}${RESET}]: " reply
    if [ -z "${reply}" ]; then
        reply="${default_value}"
    fi
    printf -v "${__var_name}" '%s' "${reply}"
}

prompt_yes_no() {
    local __var_name="$1"
    local prompt_text="$2"
    local default_value="$3"
    local default_hint="y/N"
    local reply

    if [ "${default_value}" = "yes" ]; then
        default_hint="${BOLD}Y${RESET}/n"
    else
        default_hint="y/${BOLD}N${RESET}"
    fi

    while true; do
        read -r -p "$(prompt_style "${prompt_text}") [${default_hint}]: " reply
        if [ -z "${reply}" ]; then
            reply="${default_value}"
        fi
        case "$(printf '%s' "${reply}" | tr '[:upper:]' '[:lower:]')" in
            y|yes)
                printf -v "${__var_name}" '%s' "yes"
                return
                ;;
            n|no)
                printf -v "${__var_name}" '%s' "no"
                return
                ;;
        esac
        echo "Please answer yes or no."
    done
}

next_available_path() {
    local base="$1"
    local candidate="${base}"
    local i=1
    while [ -e "${candidate}" ]; do
        candidate="${base}_${i}"
        i=$((i + 1))
    done
    printf '%s\n' "${candidate}"
}

backup_nfc_config_for_cutover() {
    LEGACY_NFC_BACKUP=""
    if [ ! -e "${NFC_CONFIG_DIR}" ]; then
        echo "  [skip]   no NFC config directory found at ${NFC_CONFIG_DIR}"
        return 0
    fi
    if [ ! -d "${NFC_CONFIG_DIR}" ]; then
        echo "  [skip]   ${NFC_CONFIG_DIR} exists but is not a directory; leaving it untouched"
        return 0
    fi
    local backup_base backup_dir
    backup_base="${PRINTER_CONFIG}/nfc_beta_cutover_$(date +%Y%m%d_%H%M%S)"
    backup_dir="$(next_available_path "${backup_base}")"
    mv "${NFC_CONFIG_DIR}" "${backup_dir}"
    LEGACY_NFC_BACKUP="${backup_dir}"
    echo "  [backup] NFC config saved to ${LEGACY_NFC_BACKUP}"
}

remove_moonraker_section() {
    local conf_path="$1"
    local section="$2"
    [ -f "${conf_path}" ] || return 1
    python3 - "${conf_path}" "${section}" <<'PYEOF'
import sys

path, section = sys.argv[1:3]
with open(path) as f:
    lines = f.readlines()

out = []
skip = False
changed = False
for line in lines:
    stripped = line.strip()
    if stripped == section:
        skip = True
        changed = True
        continue
    if skip and stripped.startswith('[') and stripped.endswith(']'):
        skip = False
    if not skip:
        out.append(line)

if changed:
    while out and out[-1].strip() == '':
        out.pop()
    out.append('\n')
    with open(path, 'w') as f:
        f.writelines(out)

sys.exit(0 if changed else 1)
PYEOF
}

backup_and_remove_legacy_moonraker_section() {
    local section="${1:-[update_manager emu_nfc_reader]}"
    LEGACY_MOONRAKER_BACKUP=""
    if [ ! -f "${MOONRAKER_CONF}" ]; then
        echo "  [skip]   moonraker.conf not found at ${MOONRAKER_CONF}"
        return 0
    fi
    if ! grep -qF "${section}" "${MOONRAKER_CONF}"; then
        return 0
    fi
    local backup_base backup_file
    backup_base="${MOONRAKER_CONF}.nfc_beta_cutover_$(date +%Y%m%d_%H%M%S)"
    backup_file="$(next_available_path "${backup_base}")"
    cp "${MOONRAKER_CONF}" "${backup_file}"
    LEGACY_MOONRAKER_BACKUP="${backup_file}"
    if remove_moonraker_section "${MOONRAKER_CONF}" "${section}"; then
        echo "  [removed] legacy ${section} from ${MOONRAKER_CONF}"
        echo "  [backup]  moonraker.conf saved to ${LEGACY_MOONRAKER_BACKUP}"
    else
        echo "  WARNING: failed to remove legacy ${section}; backup saved to ${LEGACY_MOONRAKER_BACKUP}"
    fi
}

remove_legacy_klipper_symlinks() {
    local target
    for target in \
        "${KLIPPER_EXTRAS}/nfc_gate.py" \
        "${KLIPPER_EXTRAS}/mmu_nfc_endstop.py" \
        "${KLIPPER_EXTRAS}/nfc_gates" \
        "${KLIPPER_EXTRAS}/nfc_gates.py"
    do
        if [ -L "${target}" ]; then
            rm "${target}"
            echo "  [removed] legacy symlink ${target}"
        fi
    done
}

ensure_rfid_reader_repo() {
    if [ -d "${RFID_READER_INSTALL_DIR}/.git" ]; then
        local origin
        origin="$(git -C "${RFID_READER_INSTALL_DIR}" remote get-url origin 2>/dev/null || true)"
        if [ "${origin}" != "${RFID_READER_REPO_URL}" ]; then
            echo "ERROR: ${RFID_READER_INSTALL_DIR} already exists but origin is:"
            echo "       ${origin:-<none>}"
            echo "       Expected:"
            echo "       ${RFID_READER_REPO_URL}"
            echo "       Move that directory or fix its origin, then rerun install.sh."
            exit 1
        fi
        echo "  [ok]     ${RFID_READER_INSTALL_DIR} already points at ${RFID_READER_REPO_URL}"
        return 0
    fi
    if [ -e "${RFID_READER_INSTALL_DIR}" ]; then
        echo "ERROR: ${RFID_READER_INSTALL_DIR} exists but is not a git checkout."
        echo "       Move it out of the way, then rerun install.sh."
        exit 1
    fi
    echo "  [clone]  ${RFID_READER_REPO_URL} -> ${RFID_READER_INSTALL_DIR}"
    git clone "${RFID_READER_REPO_URL}" "${RFID_READER_INSTALL_DIR}"
}

confirm_legacy_cutover() {
    if [ "${RFID_READER_LEGACY_CLEANUP_CONFIRMED:-}" = "yes" ]; then
        return 0
    fi
    echo ""
    echo "${BOLD}Legacy beta install found at ${RFID_READER_LEGACY_DIR}.${RESET}"
    echo "This release uses ${RFID_READER_INSTALL_DIR} and does not migrate old installs in place."
    echo ""
    echo "If you continue, the installer will:"
    echo "  - back up ${NFC_CONFIG_DIR}"
    echo "  - back up moonraker.conf before editing it"
    echo "  - remove old Klipper NFC symlinks"
    echo "  - remove [update_manager emu_nfc_reader] from moonraker.conf"
    echo "  - remove ${RFID_READER_LEGACY_DIR}"
    echo "  - clone or verify ${RFID_READER_INSTALL_DIR}"
    echo "  - continue with a fresh install"
    echo ""
    local answer
    read -r -p "$(prompt_style "Continue with legacy cleanup and fresh install?") [y/${BOLD}N${RESET}]: " answer
    case "${answer}" in
        [yY][eE][sS]|[yY]) ;;
        *)
            echo "Aborted. Clone ${RFID_READER_REPO_URL} into ${RFID_READER_INSTALL_DIR},"
            echo "then rerun install.sh when you are ready for the beta cutover."
            exit 0
            ;;
    esac
}

handle_legacy_beta_cutover() {
    LEGACY_CUTOVER_PERFORMED="${RFID_READER_CUTOVER_PERFORMED:-no}"
    LEGACY_NFC_BACKUP="${RFID_READER_NFC_BACKUP:-}"
    LEGACY_MOONRAKER_BACKUP="${RFID_READER_MOONRAKER_BACKUP:-}"
    MOONRAKER_CONF="${PRINTER_CONFIG}/moonraker.conf"

    if [ ! -d "${RFID_READER_LEGACY_DIR}" ]; then
        return 0
    fi

    confirm_legacy_cutover

    if [ "${REPO_DIR}" = "${RFID_READER_LEGACY_DIR}" ]; then
        echo ""
        echo "Bootstrapping fresh repo before removing the legacy checkout..."
        ensure_rfid_reader_repo
        echo "Re-running installer from ${RFID_READER_INSTALL_DIR}..."
        RFID_READER_LEGACY_CLEANUP_CONFIRMED=yes \
        RFID_READER_REPO_URL="${RFID_READER_REPO_URL}" \
        RFID_READER_INSTALL_DIR="${RFID_READER_INSTALL_DIR}" \
        RFID_READER_LEGACY_DIR="${RFID_READER_LEGACY_DIR}" \
        RFID_READER_KLIPPER_EXTRAS="${KLIPPER_EXTRAS}" \
        RFID_READER_PRINTER_CONFIG="${PRINTER_CONFIG}" \
        RFID_READER_CUTOVER_PERFORMED=yes \
        RFID_READER_NFC_BACKUP="${LEGACY_NFC_BACKUP}" \
        RFID_READER_MOONRAKER_BACKUP="${LEGACY_MOONRAKER_BACKUP}" \
        exec "${RFID_READER_INSTALL_DIR}/install.sh"
    fi

    echo ""
    echo "Legacy beta cleanup:"
    backup_nfc_config_for_cutover
    backup_and_remove_legacy_moonraker_section
    remove_legacy_klipper_symlinks
    rm -rf "${RFID_READER_LEGACY_DIR}"
    echo "  [removed] legacy repo ${RFID_READER_LEGACY_DIR}"
    ensure_rfid_reader_repo
    if [ "${REPO_DIR}" != "${RFID_READER_INSTALL_DIR}" ]; then
        echo "Re-running installer from ${RFID_READER_INSTALL_DIR}..."
        RFID_READER_REPO_URL="${RFID_READER_REPO_URL}" \
        RFID_READER_INSTALL_DIR="${RFID_READER_INSTALL_DIR}" \
        RFID_READER_LEGACY_DIR="${RFID_READER_LEGACY_DIR}" \
        RFID_READER_KLIPPER_EXTRAS="${KLIPPER_EXTRAS}" \
        RFID_READER_PRINTER_CONFIG="${PRINTER_CONFIG}" \
        RFID_READER_CUTOVER_PERFORMED=yes \
        RFID_READER_NFC_BACKUP="${LEGACY_NFC_BACKUP}" \
        RFID_READER_MOONRAKER_BACKUP="${LEGACY_MOONRAKER_BACKUP}" \
        exec "${RFID_READER_INSTALL_DIR}/install.sh"
    fi
    LEGACY_CUTOVER_PERFORMED="yes"
    echo ""
}

enforce_supported_install_dir() {
    if [ "${REPO_DIR}" = "${RFID_READER_INSTALL_DIR}" ]; then
        return 0
    fi
    if [ "${RFID_READER_ALLOW_DEV_PATH:-}" = "yes" ]; then
        echo "  [dev]    running from ${REPO_DIR}; supported install dir is ${RFID_READER_INSTALL_DIR}"
        return 0
    fi
    echo ""
    echo "WARNING: supported beta install path is ${RFID_READER_INSTALL_DIR}"
    echo "         current installer path is ${REPO_DIR}"
    echo ""
    local answer
    read -r -p "$(prompt_style "Continue from this developer/private path?") [y/${BOLD}N${RESET}]: " answer
    case "${answer}" in
        [yY][eE][sS]|[yY]) ;;
        *)
            echo "Aborted. Clone ${RFID_READER_REPO_URL} into ${RFID_READER_INSTALL_DIR}, then rerun install.sh."
            exit 0
            ;;
    esac
}

prompt_choice() {
    local __var_name="$1"
    local prompt_text="$2"
    local default_value="$3"
    shift 3
    local choices="$*"
    local choices_label=""
    local choice_label
    local choice
    local reply

    for choice in "$@"; do
        choice_label="$(choice_style "${choice}" "${default_value}")"
        if [ -z "${choices_label}" ]; then
            choices_label="${choice_label}"
        else
            choices_label="${choices_label}/${choice_label}"
        fi
    done

    while true; do
        read -r -p "$(prompt_style "${prompt_text}") [${choices_label}]: " reply
        if [ -z "${reply}" ]; then
            reply="${default_value}"
        fi
        reply="$(printf '%s' "${reply}" | tr '[:upper:]' '[:lower:]')"
        for choice in "$@"; do
            if [ "${reply}" = "${choice}" ]; then
                printf -v "${__var_name}" '%s' "${reply}"
                return
            fi
        done
        echo "Please choose one of: ${choices}."
    done
}

detect_klipper_python() {
    local candidate home_dir

    # 1. Explicit override — highest confidence, check first.
    if [ -n "${KLIPPER_VENV:-}" ]; then
        for candidate in \
            "${KLIPPER_VENV}/bin/python3" \
            "${KLIPPER_VENV}/bin/python"
        do
            if [ -x "${candidate}" ]; then
                printf '%s\n' "${candidate}"
                return 0
            fi
        done
    fi

    # 2. Current user's home — installer is almost always run as the printer
    #    user, so this hits the majority of installs on the first try.
    #    Both venv names are checked: klippy-env (Kiauh v3/KIAUH default) and
    #    klipper-env (KIAUH v4 / some distro packages).
    for candidate in \
        "${HOME}/klippy-env/bin/python3" \
        "${HOME}/klippy-env/bin/python"  \
        "${HOME}/klipper-env/bin/python3" \
        "${HOME}/klipper-env/bin/python"
    do
        if [ -x "${candidate}" ]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done

    # 3. All /home/* directories — covers non-standard usernames and the case
    #    where the script is run under a different user (e.g. via sudo).
    for home_dir in /home/*; do
        [ -d "${home_dir}" ] || continue
        for candidate in \
            "${home_dir}/klippy-env/bin/python3" \
            "${home_dir}/klippy-env/bin/python"  \
            "${home_dir}/klipper-env/bin/python3" \
            "${home_dir}/klipper-env/bin/python"
        do
            if [ -x "${candidate}" ]; then
                printf '%s\n' "${candidate}"
                return 0
            fi
        done
    done

    # 4. /root — Docker/container installs and su-based setups where HOME may
    #    not be /root despite running as root.
    for candidate in \
        /root/klippy-env/bin/python3 \
        /root/klippy-env/bin/python  \
        /root/klipper-env/bin/python3 \
        /root/klipper-env/bin/python
    do
        if [ -x "${candidate}" ]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done

    # No Klipper venv found — do not fall back to system Python.
    # Packages installed there are invisible to Klipper.
    return 1
}

ensure_python_module() {
    local module_name="$1"
    local package_name="$2"
    local python_bin

    python_bin="$(detect_klipper_python)"
    if [ -z "${python_bin}" ] || [ ! -x "${python_bin}" ]; then
        echo "WARNING: Could not find the Klipper Python environment."
        echo "         Please install ${package_name} manually."
        return 1
    fi

    if "${python_bin}" -c "import ${module_name}" >/dev/null 2>&1; then
        echo "  [ok]     ${package_name} already available in ${python_bin}"
        return 0
    fi

    echo "  [install] ${package_name} into ${python_bin}"
    if ! "${python_bin}" -m pip install "${package_name}" 2>/dev/null; then
        if ! "${python_bin}" -m pip install "${package_name}" --break-system-packages; then
            echo "WARNING: Failed to install ${package_name} automatically."
            echo "         Install it manually in the Klipper environment with:"
            echo "         ${python_bin} -m pip install ${package_name} --break-system-packages"
            return 1
        fi
    fi
}

set_config_value() {
    local file_path="$1"
    local section_name="$2"
    local key_name="$3"
    local raw_value="$4"

    python3 - "${file_path}" "${section_name}" "${key_name}" "${raw_value}" <<'PYEOF'
import sys

path, section, key, value = sys.argv[1:5]

try:
    with open(path, 'r') as f:
        lines = f.readlines()
except FileNotFoundError:
    lines = []

section_line = f'[{section}]'
key_prefix = f'{key}:'
target_start = None
target_end = len(lines)

for idx, line in enumerate(lines):
    if line.strip() == section_line:
        target_start = idx
        break

if target_start is None:
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    if lines and lines[-1].strip():
        lines.append('\n')
    lines.extend([section_line + '\n', f'{key_prefix:<24}{value}\n'])
else:
    for idx in range(target_start + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            target_end = idx
            break

    replaced = False
    for idx in range(target_start + 1, target_end):
        stripped = lines[idx].strip()
        if stripped.startswith('#'):
            continue
        if stripped.startswith(key_prefix):
            lines[idx] = f'{key_prefix:<24}{value}\n'
            replaced = True
            break

    if not replaced:
        insert_at = target_end
        if insert_at > 0 and lines[insert_at - 1].strip():
            lines.insert(insert_at, '\n')
            insert_at += 1
        lines.insert(insert_at, f'{key_prefix:<24}{value}\n')

with open(path, 'w') as f:
    f.writelines(lines)
PYEOF
}

set_lane_i2c_bus() {
    local file_path="$1"
    local bus_value="$2"
    local bus_label="$3"

    python3 - "${file_path}" "${bus_value}" "${bus_label}" <<'PYEOF'
import sys

path, bus, label = sys.argv[1:4]
slb = 'i2c2_PB10_PB11'
ebb = 'i2c3_PB3_PB4'

try:
    lines = open(path, 'r').read().splitlines(True)
except FileNotFoundError:
    lines = []

section_start = None
section_end = len(lines)
for idx, line in enumerate(lines):
    if line.strip() == '[nfc_gate]':
        section_start = idx
        break

if section_start is None:
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    if lines and lines[-1].strip():
        lines.append('\n')
    lines.append('[nfc_gate]\n')
    section_start = len(lines) - 1
else:
    for idx in range(section_start + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section_end = idx
            break

before = lines[:section_start + 1]
section = lines[section_start + 1:section_end]
after = lines[section_end:]

filtered = []
insert_at = None
for line in section:
    stripped = line.strip()
    uncommented = stripped.startswith('i2c_bus:')
    commented = stripped.startswith('#i2c_bus:')
    is_preset = (slb in stripped or ebb in stripped)
    if uncommented or (commented and is_preset):
        continue
    filtered.append(line)
    if stripped.startswith('i2c_address:'):
        insert_at = len(filtered)

if label == 'SLB':
    bus_lines = [
        f'i2c_bus:                 {slb}          # SLB configuration\n',
        f'#i2c_bus:                {ebb}           # EBB42 configuration\n',
    ]
elif label == 'EBB':
    bus_lines = [
        f'#i2c_bus:                {slb}          # SLB configuration\n',
        f'i2c_bus:                 {ebb}           # EBB42 configuration\n',
    ]
else:
    bus_lines = [
        f'#i2c_bus:                {slb}          # SLB configuration\n',
        f'#i2c_bus:                {ebb}           # EBB42 configuration\n',
        f'i2c_bus:                 {bus}          # custom lane hardware bus\n',
    ]

if insert_at is None:
    insert_at = 0
filtered[insert_at:insert_at] = bus_lines

with open(path, 'w') as f:
    f.writelines(before + filtered + after)
PYEOF
}

set_scan_jog_max_config() {
    local file_path="$1"
    local mode="$2"
    local distance="$3"

    python3 - "${file_path}" "${mode}" "${distance}" <<'PYEOF'
import sys

path, mode, distance = sys.argv[1:4]
key = 'scan_jog_max'
old_key = 'jog_scan_distance'

try:
    lines = open(path, 'r').read().splitlines(True)
except FileNotFoundError:
    lines = []

section_start = None
section_end = len(lines)
for idx, line in enumerate(lines):
    if line.strip() == '[nfc_gate]':
        section_start = idx
        break

if section_start is None:
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    if lines and lines[-1].strip():
        lines.append('\n')
    lines.append('[nfc_gate]\n')
    section_start = len(lines) - 1
else:
    for idx in range(section_start + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section_end = idx
            break

found = False
insert_at = section_end
for idx in range(section_start + 1, section_end):
    stripped = lines[idx].strip()
    active = stripped.startswith(key + ':')
    commented = stripped.startswith('#' + key + ':')
    old_active = stripped.startswith(old_key + ':')
    old_commented = stripped.startswith('#' + old_key + ':')
    if old_active or old_commented:
        current = stripped.split(':', 1)[1].strip() if ':' in stripped else distance
        if not found:
            found = True
            if mode == 'fixed':
                lines[idx] = f'{key}:      {distance}\n'
            else:
                lines[idx] = f'#{key}:     {current}\n'
        else:
            lines[idx] = ''
        continue
    if not active and not commented:
        continue
    found = True
    if mode == 'fixed':
        lines[idx] = f'{key}:      {distance}\n'
    else:
        current = stripped.split(':', 1)[1].strip() if ':' in stripped else distance
        lines[idx] = f'#{key}:     {current}\n'

if not found and mode == 'fixed':
    for idx in range(section_start + 1, section_end):
        if lines[idx].strip().startswith('scan_jog_mm:'):
            insert_at = idx + 1
            break
    if insert_at > 0 and lines[insert_at - 1].strip():
        lines.insert(insert_at, '\n')
        insert_at += 1
    lines.insert(insert_at, f'{key}:      {distance}\n')

with open(path, 'w') as f:
    f.writelines(lines)
PYEOF
}

warn_software_i2c_sensors() {
    local hardware_bus="$1"
    local found=0
    local file
    local match

    while IFS= read -r file; do
        [ -f "${file}" ] || continue
        match="$(grep -nEi '^[[:space:]]*([^#].*)?(i2c_software|i2c_bus[[:space:]]*:[[:space:]]*i2c_software)' "${file}" || true)"
        if [ -n "${match}" ]; then
            if [ "${found}" -eq 0 ]; then
                echo ""
                echo "WARNING: software I2C sensor config detected."
                echo "         PN532 should use hardware I2C. PN7160 supports software I2C,"
                echo "         but hardware I2C is recommended because software I2C increases MCU load."
                echo "         Any sensor on the same lane MCU should use:"
                echo "         i2c_bus: ${hardware_bus}"
                echo "         Check these file(s), commonly emu_macros.cfg:"
            fi
            found=1
            echo "         ${file}"
            printf '%s\n' "${match}" | sed 's/^/           /'
        fi
    done < <(find "${PRINTER_CONFIG}" -type f \( -iname '*emu*macro*.cfg' -o -iname '*sensor*.cfg' -o -iname '*.cfg' \) 2>/dev/null)
}

count_lane_sections() {
    local file_path="$1"
    local hh_version="$2"
    python3 - "${file_path}" "${hh_version}" <<'PYEOF'
import re
import sys

path, hh_version = sys.argv[1:3]
try:
    text = open(path, 'r').read()
except FileNotFoundError:
    print(4)
    raise SystemExit

if hh_version == 'v4':
    section_pattern = r'^\[nfc_gate unit0_lane\d+\]\s*$'
else:
    section_pattern = r'^\[nfc_gate lane\d+\]\s*$'
count = len(re.findall(section_pattern, text, flags=re.M))
print(count or 4)
PYEOF
}

write_lane_config() {
    local file_path="$1"
    local lane_count="$2"
    local lane_mcu_prefix="$3"
    local hh_version="$4"

    python3 - "${file_path}" "${lane_count}" "${lane_mcu_prefix}" "${hh_version}" <<'PYEOF'
import re
import sys

path = sys.argv[1]
lane_count = int(sys.argv[2])
lane_mcu_prefix = sys.argv[3]
hh_version = sys.argv[4]
existing = {}
current_lane = None

if hh_version == 'v4':
    section_pattern = r'^\[nfc_gate unit0_lane(\d+)\]$'
    reader_name = lambda lane: 'unit0_lane%d' % lane
    mcu_name = lambda lane: 'unit0_gate%d' % lane
    endstop_name = lambda lane: 'nfc_unit0_lane%d' % lane
    layout_title = 'HAPPY HARE V4 NFC GATE READER - NFC I2C LANE HARDWARE'
    layout_note = (
        "#   Happy Hare V4 default single-unit layout: unit0_laneN -> "
        "unit0_gateN.\n")
else:
    section_pattern = r'^\[nfc_gate lane(\d+)\]$'
    reader_name = lambda lane: 'lane%d' % lane
    mcu_name = lambda lane: '%s%d' % (lane_mcu_prefix, lane)
    endstop_name = lambda lane: 'nfc_lane%d' % lane
    layout_title = 'EMU NFC GATE READER - NFC I2C LANE HARDWARE'
    layout_note = (
        "#   Happy Hare V3 default layout uses laneN reader sections and the "
        "selected MCU prefix.\n")

try:
    lines = open(path, 'r').read().splitlines()
except FileNotFoundError:
    lines = []

for line in lines:
    stripped = line.strip()
    match = re.match(section_pattern, stripped)
    if match:
        current_lane = int(match.group(1))
        continue
    if current_lane is None or stripped.startswith('#'):
        continue
    if current_lane not in existing:
        existing[current_lane] = {}
    if stripped.startswith('i2c_mcu:'):
        existing[current_lane]['i2c_mcu'] = stripped.split(':', 1)[1].strip()
    elif stripped.startswith('startup_poll_delay:'):
        existing[current_lane]['startup_poll_delay'] = stripped.split(':', 1)[1].strip()

with open(path, 'w') as f:
    f.write("# =============================================================================\n")
    f.write("# ================= %s ================\n" % layout_title)
    f.write("# =============================================================================\n")
    f.write("# Supported documented path:\n")
    f.write("#   one NFC reader module per lane MCU / EBB42 board.\n")
    f.write(layout_note)
    f.write("#   PN532 is the default reader; PN7160, RC522, and PN5180 may be selected\n")
    f.write("#   per lane with reader_type. SPI readers require per-lane spi_bus, cs_pin,\n")
    f.write("#   and any reader-specific pins; configure those overrides manually.\n")
    f.write("#\n")
    f.write("# Include after nfc_reader.cfg and nfc_macros.cfg:\n")
    f.write("#   [include nfc/nfc_reader.cfg]\n")
    f.write("#   [include nfc/nfc_macros.cfg]\n")
    f.write("#   [include nfc/nfc_reader_hw.cfg]\n")
    f.write("#\n")
    f.write("# Each [nfc_gate ...] section maps:\n")
    f.write("#   Happy Hare gate number -> Klipper lane MCU -> NFC reader hardware.\n")
    f.write("# Each [mmu_nfc_endstop ...] section seeds Happy Hare with a homeable\n")
    f.write("# virtual endstop that uses the matching lane reader.\n")
    f.write("# in Happy Hare homing/test moves.\n")
    f.write("# Set enabled: False on a lane to keep the template without creating hardware.\n")
    f.write("#\n")
    f.write("# i2c_mcu: is consumed by Klipper's I2C bus layer (MCU_I2C_from_config),\n")
    f.write("# not read explicitly by the plugin.  The MCU name must already exist in\n")
    f.write("# Klipper / Happy Hare config.\n")
    if hh_version == 'v4':
        f.write("# The V4 default names are unit0_gate0, unit0_gate1, etc.\n")
    else:
        f.write("# The installer writes this from the selected MCU prefix, for example\n")
        f.write("# prefix 'lane' -> [mcu lane0], [mcu lane1], etc.\n")
    f.write("#\n")
    f.write("# [WARN] After updating Klipper, rebuild and flash the lane MCU / EBB42 firmware.\n")
    f.write("# Updating the host alone is not enough for NFC reader I2C troubleshooting.\n")
    f.write("# =============================================================================\n\n")

    for lane in range(lane_count):
        lane_existing = existing.get(lane, {})
        reader = reader_name(lane)
        mcu = mcu_name(lane)
        endstop = endstop_name(lane)
        startup_delay = lane_existing.get('startup_poll_delay',
                                          f'{lane * 0.5:.1f}')
        f.write("# =============================================================================\n")
        f.write(f"# =============================== LANE {lane} =================================\n")
        f.write("# =============================================================================\n")
        f.write(f"[nfc_gate {reader}]\n")
        f.write("enabled:                True\n")
        f.write("# reader_type:            pn532  # PN7160: pn7160; SPI: rc522 or pn5180\n")
        f.write("# i2c_address:            36     # PN7160 valid addresses are 40-43 (0x28-0x2B)\n")
        f.write(f"mmu_gate:                {lane}\n")
        f.write(f"i2c_mcu:                 {mcu}\n")
        f.write(f"startup_poll_delay:      {startup_delay}\n\n")
        f.write(f"[mmu_nfc_endstop {reader}]\n")
        f.write(f"nfc_gate:                {reader}\n")
        f.write(f"endstop_name:            {endstop}\n")
        f.write("poll_interval:           0.05\n")
        f.write("register_sensor:         True\n\n")

    example_lane = lane_count
    f.write("# =============================================================================\n")
    f.write(f"# =========================== LANE {example_lane} EXAMPLE ============================\n")
    f.write("# =============================================================================\n")
    example_reader = reader_name(example_lane)
    example_mcu = mcu_name(example_lane)
    example_endstop = endstop_name(example_lane)
    f.write(f"# [nfc_gate {example_reader}]\n")
    f.write("# enabled:                False\n")
    f.write("# reader_type:            pn532  # PN7160: pn7160; SPI: rc522 or pn5180\n")
    f.write("# i2c_address:            36     # PN7160 valid addresses are 40-43 (0x28-0x2B)\n")
    f.write(f"# mmu_gate:                {example_lane}\n")
    f.write(f"# i2c_mcu:                 {example_mcu}\n")
    f.write(f"# startup_poll_delay:      {example_lane * 0.5:.1f}\n")
    f.write("#\n")
    f.write(f"# [mmu_nfc_endstop {example_reader}]\n")
    f.write(f"# nfc_gate:                {example_reader}\n")
    f.write(f"# endstop_name:            {example_endstop}\n")
    f.write("# poll_interval:           0.05\n")
    f.write("# register_sensor:         True\n")
PYEOF
}

ensure_mmu_nfc_endstops() {
    local file_path="$1"
    local name
    name="$(basename "${file_path}")"

    if [ ! -f "${file_path}" ]; then
        return
    fi

    echo "  [check]    ${name} — ensuring NFC virtual endstops below each lane reader..."
    python3 - "${file_path}" <<'PYEOF' \
        || echo "    WARNING: virtual endstop injection failed — ${name} left unchanged"
import re
import sys

path = sys.argv[1]

with open(path, 'r') as f:
    text = f.read()

lines = text.splitlines(keepends=True)
section_re = re.compile(r'^\[([^\]]+)\]\s*(?:[#;].*)?$')
lane_re = re.compile(r'^nfc_gate lane(\d+)$')
endstop_re = re.compile(r'^mmu_nfc_endstop lane(\d+)$')

sections = []
current = None
for idx, line in enumerate(lines):
    match = section_re.match(line.strip())
    if not match:
        continue
    if current is not None:
        current['end'] = idx
        sections.append(current)
    current = {
        'name': match.group(1).strip(),
        'start': idx,
        'end': len(lines),
    }
if current is not None:
    sections.append(current)

existing_endstops = {}
for section in sections:
    match = endstop_re.match(section['name'])
    if match:
        lane = int(match.group(1))
        existing_endstops[lane] = section

lane_sections = []
for section in sections:
    match = lane_re.match(section['name'])
    if match:
        lane_sections.append((int(match.group(1)), section))

if not lane_sections:
    print("    (no [nfc_gate laneN] sections found)")
    raise SystemExit

remove_ranges = {}
insertions = {}
actions = []

def generated_block(lane):
    return (
        "[mmu_nfc_endstop lane{lane}]\n"
        "nfc_gate:                lane{lane}\n"
        "endstop_name:            nfc_lane{lane}\n"
        "poll_interval:           0.05\n"
        "register_sensor:         True\n"
    ).format(lane=lane)

def section_text(section):
    end = section['start'] + 1
    for idx in range(section['start'] + 1, section['end']):
        stripped = lines[idx].strip()
        if stripped and not stripped.startswith('#') and not stripped.startswith(';'):
            end = idx + 1
    return ''.join(lines[section['start']:end]).strip('\n') + '\n'

def lane_insert_index(section):
    insert_at = section['end']
    for idx in range(section['start'] + 1, section['end']):
        stripped = lines[idx].strip()
        if stripped and not stripped.startswith('#') and not stripped.startswith(';'):
            insert_at = idx + 1
    return insert_at

for lane, section in lane_sections:
    existing = existing_endstops.get(lane)
    insert_at = lane_insert_index(section)
    if existing is None:
        block = generated_block(lane)
        actions.append(("insert", lane))
        insertions.setdefault(insert_at, []).append((lane, block))
    else:
        if existing['start'] == insert_at or existing['start'] == insert_at + 1:
            actions.append(("keep", lane))
        else:
            block = section_text(existing)
            remove_ranges[existing['start']] = existing['end']
            actions.append(("move", lane))
            insertions.setdefault(insert_at, []).append((lane, block))

if all(action == "keep" for action, _lane in actions):
    for _action, lane in actions:
        print("    [skip]    [mmu_nfc_endstop lane{}]".format(lane))
    print("    (virtual endstops already present below lane readers)")
    raise SystemExit

out = []
i = 0
while i < len(lines):
    for lane, block in insertions.get(i, []):
        if out and out[-1].strip():
            out.append('\n')
        out.extend(block.splitlines(keepends=True))
        if out and not out[-1].endswith('\n'):
            out[-1] += '\n'
        out.append('\n')
    if i in remove_ranges:
        i = remove_ranges[i]
        continue
    out.append(lines[i])
    i += 1

for lane, block in insertions.get(len(lines), []):
    if out and out[-1].strip():
        out.append('\n')
    out.extend(block.splitlines(keepends=True))
    out.append('\n')

with open(path, 'w') as f:
    f.write(''.join(out))

for action, lane in actions:
    if action == "keep":
        print("    [skip]    [mmu_nfc_endstop lane{}] already below [nfc_gate lane{}]".format(
            lane, lane))
    elif action == "move":
        print("    [move]    [mmu_nfc_endstop lane{}] below [nfc_gate lane{}]".format(
            lane, lane))
    else:
        print("    [insert]  [mmu_nfc_endstop lane{}] below [nfc_gate lane{}]".format(
            lane, lane))
PYEOF
}

detect_reader_type() {
    python3 - "$@" <<'PYEOF'
import os
import re
import sys

printer_cfg = sys.argv[1]
lane_cfg = sys.argv[2]
shared_paths = sys.argv[3:]
include_re = re.compile(
    r'^\s*\[include\s+nfc/(?:nfc_reader_shared|nfc_shared_reader)\.cfg\]\s*$',
    re.M)
section_re = re.compile(r'^\[nfc_gate shared\]\s*$', re.M)

try:
    if include_re.search(open(printer_cfg, 'r').read()):
        print('shared')
        raise SystemExit
except FileNotFoundError:
    pass

for path in shared_paths:
    try:
        text = open(path, 'r').read()
    except FileNotFoundError:
        continue
    if section_re.search(text) and not os.path.exists(lane_cfg):
        print('shared')
        raise SystemExit
print('lane')
PYEOF
}

detect_shared_mcu() {
    local shared_cfg="$1"
    python3 - "${shared_cfg}" <<'PYEOF'
import sys

try:
    text = open(sys.argv[1], 'r').read()
except FileNotFoundError:
    print('mmu')
    raise SystemExit

in_shared = False
for line in text.splitlines():
    stripped = line.strip()
    if stripped == '[nfc_gate shared]':
        in_shared = True
        continue
    if in_shared:
        if stripped.startswith('['):
            break
        if stripped.startswith('i2c_mcu:'):
            print(stripped.split(':', 1)[1].strip())
            raise SystemExit
print('mmu')
PYEOF
}

detect_shared_i2c_bus() {
    local shared_cfg="$1"
    python3 - "${shared_cfg}" <<'PYEOF'
import sys

try:
    text = open(sys.argv[1], 'r').read()
except FileNotFoundError:
    print('i2c1')
    raise SystemExit

in_shared = False
for line in text.splitlines():
    stripped = line.strip()
    if stripped == '[nfc_gate shared]':
        in_shared = True
        continue
    if in_shared:
        if stripped.startswith('['):
            break
        if stripped.startswith('i2c_bus:'):
            print(stripped.split(':', 1)[1].strip())
            raise SystemExit
print('i2c1')
PYEOF
}

detect_shared_config_value() {
    local shared_cfg="$1"
    local key_name="$2"
    local default_value="$3"
    python3 - "${shared_cfg}" "${key_name}" "${default_value}" <<'PYEOF'
import sys

path, key, default = sys.argv[1:4]
try:
    text = open(path, 'r').read()
except FileNotFoundError:
    print(default)
    raise SystemExit

in_shared = False
for line in text.splitlines():
    stripped = line.strip()
    if stripped == '[nfc_gate shared]':
        in_shared = True
        continue
    if in_shared:
        if stripped.startswith('['):
            break
        if stripped.startswith('#') or not stripped.startswith(key + ':'):
            continue
        print(stripped.split(':', 1)[1].strip())
        raise SystemExit
print(default)
PYEOF
}

detect_lane_i2c_bus() {
    local lane_cfg="$1"
    python3 - "${lane_cfg}" <<'PYEOF'
import re
import sys

try:
    text = open(sys.argv[1], 'r').read()
except FileNotFoundError:
    print('i2c2_PB10_PB11')
    raise SystemExit

in_base = False
for line in text.splitlines():
    stripped = line.strip()
    if stripped == '[nfc_gate]':
        in_base = True
        continue
    if in_base:
        if stripped.startswith('['):
            break
        m = re.match(r'^i2c_bus\s*:\s*(\S+)', stripped)
        if m:
            print(m.group(1))
            raise SystemExit
print('i2c2_PB10_PB11')
PYEOF
}

lane_bus_label() {
    case "$1" in
        i2c2_PB10_PB11) printf '%s' "SLB" ;;
        i2c3_PB3_PB4)   printf '%s' "EBB" ;;
        *)              printf '%s' "$1" ;;
    esac
}

prompt_lane_i2c_bus() {
    local __bus_var="$1"
    local __label_var="$2"
    local default_bus="$3"
    local default_label
    local reply

    default_label="$(lane_bus_label "${default_bus}")"
    echo "6. Per-lane reader I2C bus"
    echo "   $(choice_style SLB "${default_label}") = i2c2_PB10_PB11"
    echo "   $(choice_style EBB "${default_label}") = i2c3_PB3_PB4"
    echo "   Enter a bus name directly if your lane MCUs use different pins."
    while true; do
        read -r -p "$(prompt_style "   Select lane reader bus") [${BOLD}${default_label}${RESET}]: " reply
        if [ -z "${reply}" ]; then
            reply="${default_label}"
        fi
        case "$(printf '%s' "${reply}" | tr '[:lower:]' '[:upper:]')" in
            SLB)
                printf -v "${__bus_var}" '%s' "i2c2_PB10_PB11"
                printf -v "${__label_var}" '%s' "SLB"
                return
                ;;
            EBB|EBB42)
                printf -v "${__bus_var}" '%s' "i2c3_PB3_PB4"
                printf -v "${__label_var}" '%s' "EBB"
                return
                ;;
        esac
        if printf '%s' "${reply}" | grep -Eq '^i2c[[:alnum:]_]+$'; then
            printf -v "${__bus_var}" '%s' "${reply}"
            printf -v "${__label_var}" '%s' "custom"
            return
        fi
        echo "Please enter SLB, EBB, or a Klipper I2C bus name such as i2c3_PB3_PB4."
    done
}

prompt_scan_jog_max_mode() {
    local __mode_var="$1"
    local __distance_var="$2"
    local reply
    local default_mode="fixed"
    local default_distance="480.0"

    echo "7. Scan-jog max travel"
    echo "   $(choice_style fixed "${default_mode}")  = use scan_jog_max, default 480mm"
    echo "   $(choice_style bowden "${default_mode}") = keep trying until the lane Bowden length is reached"
    while true; do
        read -r -p "$(prompt_style "   Select scan-jog max travel") [${BOLD}${default_mode}${RESET}]: " reply
        if [ -z "${reply}" ]; then
            reply="${default_mode}"
        fi
        reply="$(printf '%s' "${reply}" | tr '[:upper:]' '[:lower:]')"
        case "${reply}" in
            fixed|rotation|spool|one|480|480.0)
                local distance_reply
                read -r -p "$(prompt_style "   Enter scan_jog_max in mm") [${BOLD}${default_distance}${RESET}]: " distance_reply
                if [ -z "${distance_reply}" ]; then
                    distance_reply="${default_distance}"
                fi
                if ! printf '%s' "${distance_reply}" | grep -Eq '^[0-9]+([.][0-9]+)?$'; then
                    echo "Please enter a numeric scan_jog_max value in mm."
                    continue
                fi
                printf -v "${__mode_var}" '%s' "fixed"
                printf -v "${__distance_var}" '%s' "${distance_reply}"
                return
                ;;
            bowden|calibration|calibrated)
                printf -v "${__mode_var}" '%s' "bowden"
                printf -v "${__distance_var}" '%s' ""
                return
                ;;
        esac
        echo "Please choose fixed or bowden."
    done
}

detect_mmu_led_unit() {
    local hw_cfg="$1"
    python3 - "${hw_cfg}" <<'PYEOF'
import sys, re

try:
    text = open(sys.argv[1], 'r').read()
except FileNotFoundError:
    print('unit0')
    raise SystemExit

for line in text.splitlines():
    m = re.match(r'^\[mmu_leds\s+(\S+)\]', line.strip())
    if m:
        print(m.group(1))
        raise SystemExit
print('unit0')
PYEOF
}

# ── I2C bus discovery helpers ────────────────────────────────────────────────
#
# list_i2c_buses <mcu> <config_dir>
#   Scans all .cfg files under <config_dir> for sections that set both
#   i2c_mcu = <mcu> and i2c_bus = <value>.  Prints unique bus names, sorted.
#
list_i2c_buses() {
    local mcu="$1"
    local config_dir="$2"
    [ -d "$config_dir" ] || return
    python3 - "$mcu" "$config_dir" <<'PYEOF'
import sys, re, os

mcu, config_dir = sys.argv[1], sys.argv[2]
buses = set()

for root, dirs, files in os.walk(config_dir):
    for fname in sorted(files):
        if not fname.endswith('.cfg'):
            continue
        try:
            text = open(os.path.join(root, fname)).read()
        except Exception:
            continue
        current_mcu = None
        current_bus = None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith('['):
                if current_mcu == mcu and current_bus:
                    buses.add(current_bus)
                current_mcu = None
                current_bus = None
                continue
            m = re.match(r'i2c_mcu\s*[=:]\s*(\S+)', s)
            if m:
                current_mcu = m.group(1).rstrip(';').strip()
                continue
            m = re.match(r'i2c_bus\s*[=:]\s*(\S+)', s)
            if m:
                current_bus = m.group(1).rstrip(';').strip()
        if current_mcu == mcu and current_bus:
            buses.add(current_bus)

for bus in sorted(buses):
    print(bus)
PYEOF
}

# prompt_i2c_bus_select <varname> <mcu> <config_dir> <default>
#   Shows I2C buses found in config files for <mcu> as a numbered list.
#   Option 0 lets the user type a custom value.  Falls back to plain
#   prompt_with_default when no buses are found.
#
prompt_i2c_bus_select() {
    local varname="$1"
    local mcu="$2"
    local config_dir="$3"
    local default_val="$4"

    local buses=()
    while IFS= read -r bus; do
        [ -n "$bus" ] && buses+=("$bus")
    done < <(list_i2c_buses "$mcu" "$config_dir")

    if [ ${#buses[@]} -eq 0 ]; then
        prompt_with_default "$varname" \
            "5. I2C bus on MCU '${mcu}' for the shared NFC reader (e.g. i2c3_PB3_PB4)" \
            "$default_val"
        return
    fi

    echo "5. I2C bus on MCU '${mcu}' for the shared NFC reader"
    echo "   Buses already used on '${mcu}' in your config files:"
    local i
    for i in "${!buses[@]}"; do
        local marker=""
        [ "${buses[$i]}" = "$default_val" ] && marker="  ← current"
        printf "    %d) %s%s\n" "$((i+1))" "${buses[$i]}" "$marker"
    done
    echo "    0) Enter a different bus name"
    echo ""

    local choice=""
    read -r -p "$(prompt_style "   Select I2C bus") [${BOLD}0-${#buses[@]}${RESET}]: " choice

    if [[ "$choice" =~ ^[1-9][0-9]*$ ]] && [ "$choice" -le "${#buses[@]}" ]; then
        eval "${varname}='${buses[$((choice-1))]}'"
    else
        local custom=""
        read -r -p "$(prompt_style "   Enter I2C bus name") [${BOLD}${default_val}${RESET}]: " custom
        eval "${varname}='${custom:-${default_val}}'"
    fi
}


write_shared_config() {
    local file_path="$1"
    local i2c_mcu="$2"
    local i2c_bus="$3"
    local startup_polling_val="$4"

    python3 - "${file_path}" "${i2c_mcu}" "${i2c_bus}" "${startup_polling_val}" <<'PYEOF'
import sys

path, i2c_mcu, i2c_bus, startup_polling = sys.argv[1:5]

with open(path, 'w') as f:
    f.write("# =============================================================================\n")
    f.write("# =================== EMU NFC GATE READER - SHARED NFC HARDWARE ================\n")
    f.write("# =============================================================================\n")
    f.write("# Single reader mounted inside the MMU body.  Tap a tagged spool before\n")
    f.write("# loading; NFC stages the spool ID for the next pregate preload automatically.\n")
    f.write("#\n")
    f.write("# This file is separate from nfc_reader_hw.cfg so that the shared reader\n")
    f.write("# can be added to an existing per-lane install without editing any existing\n")
    f.write("# config file — just add the include below and fill in the hardware values.\n")
    f.write("#\n")
    f.write("# Pure shared-reader install — include this instead of nfc_reader_hw.cfg:\n")
    f.write("#   [include nfc/nfc_reader.cfg]\n")
    f.write("#   [include nfc/nfc_macros.cfg]\n")
    f.write("#   [include nfc/nfc_reader_shared.cfg]\n")
    f.write("#\n")
    f.write("# Hybrid install (per-lane readers + shared reader) — include both:\n")
    f.write("#   [include nfc/nfc_reader.cfg]\n")
    f.write("#   [include nfc/nfc_macros.cfg]\n")
    f.write("#   [include nfc/nfc_reader_hw.cfg]\n")
    f.write("#   [include nfc/nfc_reader_shared.cfg]\n")
    f.write("#\n")
    f.write("# [WARN] After updating Klipper, rebuild and flash the MCU hosting the NFC reader\n")
    f.write(f"#    ({i2c_mcu}).  The MCU must be on the same Klipper version as the host.\n")
    f.write("# =============================================================================\n\n")
    f.write("[nfc_gate shared]\n")
    f.write("enabled:                True\n")
    f.write("# reader_type:            pn532  # PN7160: pn7160; SPI: rc522 or pn5180\n")
    f.write("# i2c_address:            36     # PN7160 valid addresses are 40-43 (0x28-0x2B)\n")
    f.write(f"i2c_mcu:                {i2c_mcu}\n")
    f.write(f"i2c_bus:                {i2c_bus}\n")
    f.write(f"shared:                 true\n")
    f.write(f"startup_polling:        {startup_polling}\n")
    f.write("# LED effect pairs: *_effect is the [mmu_led_effect] name to start;\n")
    f.write("# *_duration is how many seconds NFC waits before sending STOP=1.\n")
    f.write("# Example: shared_tag_unresolved_effect + unresolved_effect_duration\n")
    f.write("# controls which red effect plays and how long NFC lets it run.\n")
    f.write("# [mmu_led_effect] to flash bright green when a tag is read (defined in nfc_macros.cfg).\n")
    f.write("shared_tag_read_effect: mmu_RFID_read\n")
    f.write("read_effect_duration: 4.0\n")
    f.write("\n")
    f.write("# [mmu_led_effect] to flash when a tag is read while bypass is selected.\n")
    f.write("shared_bypass_tag_read_effect: mmu_RFID_bypass_read\n")
    f.write("bypass_read_effect_duration: 4.0\n")
    f.write("\n")
    f.write("# [mmu_led_effect] to flash bright green while a spool is staged and waiting to load.\n")
    f.write("# Duration is used only when this is the immediate bypass fallback confirmation.\n")
    f.write("shared_spool_ready_effect: mmu_RFID_ready\n")
    f.write("ready_effect_duration: 4.0\n")
    f.write("\n")
    f.write("# [mmu_led_effect] to flash bright green when a bypass spool resolves.\n")
    f.write("shared_bypass_spool_ready_effect: mmu_RFID_bypass_ready\n")
    f.write("bypass_ready_effect_duration: 2.0\n")
    f.write("\n")
    f.write("# [mmu_led_effect] to flash bright red 2x when a tag UID does not resolve.\n")
    f.write("shared_tag_unresolved_effect: mmu_RFID_unresolved\n")
    f.write("unresolved_effect_duration: 2.0\n")
    f.write("\n")
    f.write("# [mmu_led_effect] to run a bright yellow chase while Spoolman creates a missing spool.\n")
    f.write("shared_auto_create_effect: mmu_RFID_creating\n")
    f.write("\n")
    f.write("# NFC uses pending_spool_id_timeout when Happy Hare exposes it in the active\n")
    f.write("# [mmu] config. Otherwise, the shared-reader pending timeout is 60 s.\n")
    f.write("\n")
    f.write("# Seconds polling may run after NFC_SHARED READ=1 without resolving a tag\n")
    f.write("# before auto-stopping.  No effect for startup_polling or post-PRELOAD_CHECK.\n")
    f.write("# shared_read_timeout: 120.0\n")
    f.write("\n")
    f.write("# Consecutive unresolvable UIDs before the console advises MMU_PRELOAD.\n")
    f.write("# shared_missed_limit: 3\n")
    f.write("\n")
    f.write("# Set to true to block pregate loads entirely when no spool is staged.\n")
    f.write("force_spool_id: true\n")
PYEOF
}

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

handle_legacy_beta_cutover
enforce_supported_install_dir

print_banner
echo "Interactive setup"
echo ""

if [ -f "${NFC_READER_CFG}" ]; then
    echo "${BOLD}  Existing install detected — updating.${RESET}"
    echo "  Your current configuration will be preserved; only changed values will be written."
    echo ""
fi

# ── Q1: Happy Hare version ────────────────────────────────────────────────────
echo "1. Happy Hare version"
echo "   $(choice_style v3 v3) = V3 LED compatibility transport"
echo "   $(choice_style v4 v3) = V4 generated direct LED transport"
prompt_choice HH_VERSION \
    "   Select Happy Hare version" \
    "v3" \
    "v3" "v4"
echo ""

# ── Q2: Reader layout ─────────────────────────────────────────────────────────
DEFAULT_READER_TYPE="$(detect_reader_type "${PRINTER_CFG}" "${NFC_READER_HW_CFG}" "${NFC_READER_SHARED_CFG}" "${NFC_SHARED_READER_CFG}")"
echo "2. Reader layout"
echo "   $(choice_style lane "${DEFAULT_READER_TYPE}")   = per-lane NFC readers, one per EBB42 board"
echo "   $(choice_style shared "${DEFAULT_READER_TYPE}") = single NFC reader inside the MMU body for staging spools"
prompt_choice READER_TYPE \
    "   Select reader layout" \
    "${DEFAULT_READER_TYPE}" \
    "lane" "shared"
echo ""

# ── Lane path ─────────────────────────────────────────────────────────────────
if [ "${READER_TYPE}" = "lane" ]; then

    DEFAULT_LANE_COUNT="$(count_lane_sections "${NFC_READER_HW_CFG}" "${HH_VERSION}")"
    prompt_with_default LANE_COUNT \
        "3. How many NFC readers / lanes are you configuring?" \
        "${DEFAULT_LANE_COUNT}"
    while ! printf '%s' "${LANE_COUNT}" | grep -Eq '^[1-9][0-9]*$'; do
        echo "Please enter a whole number greater than zero."
        prompt_with_default LANE_COUNT \
            "3. How many NFC readers / lanes are you configuring?" \
            "${DEFAULT_LANE_COUNT}"
    done

    echo "4. Spoolman connection"
    echo "   $(choice_style auto auto)     = read the URL from Moonraker's [spoolman] section (recommended)"
    echo "   $(choice_style direct auto)   = enter a fixed URL such as http://127.0.0.1:7912"
    echo "   $(choice_style disabled auto) = no Spoolman — resolve by tag metadata or UID only"
    prompt_choice SPOOLMAN_MODE \
        "   Select Spoolman connection mode" \
        "auto" \
        "auto" "direct" "disabled"
    SPOOLMAN_URL="auto"
    if [ "${SPOOLMAN_MODE}" = "direct" ]; then
        prompt_with_default SPOOLMAN_URL \
            "   Enter the direct Spoolman URL" \
            "http://127.0.0.1:7912"
    elif [ "${SPOOLMAN_MODE}" = "disabled" ]; then
        SPOOLMAN_URL="disabled"
    fi

    # Per-lane readers are driven by the Happy Hare post-preload hook.
    # Keep optional background polling disabled in the generated config.
    STARTUP_POLLING="no"

    prompt_yes_no SCAN_ENABLED \
        "5. Enable scan-jog when a loaded tag is out of read range?" \
        "yes"

    if [ "${HH_VERSION}" = "v4" ]; then
        LANE_MCU_PREFIX="unit0_gate"
        echo "6. Happy Hare V4 default MCU naming"
        echo "   This writes i2c_mcu values as unit0_gate0, unit0_gate1, etc."
    else
        prompt_with_default LANE_MCU_PREFIX \
            "6. Lane reader MCU name prefix" \
            "mmu"
        echo "   This writes i2c_mcu values as ${LANE_MCU_PREFIX}0, ${LANE_MCU_PREFIX}1, etc."
    fi
    echo ""

    DEFAULT_LANE_I2C_BUS="$(detect_lane_i2c_bus "${NFC_READER_CFG}")"
    prompt_lane_i2c_bus LANE_I2C_BUS LANE_BUS_LABEL "${DEFAULT_LANE_I2C_BUS}"
    echo ""

    prompt_scan_jog_max_mode SCAN_JOG_MAX_MODE SCAN_JOG_MAX
    echo ""

    echo "8. Tag read mode"
    echo "   $(choice_style spoolman spoolman) = UID-only lookup in Spoolman's extra field"
    echo "   $(choice_style rich spoolman)     = read tag metadata, then resolve/create Spoolman records"
    if [ "${SPOOLMAN_MODE}" = "disabled" ]; then
        echo ""
        echo "   NOTE: Spoolman is disabled. Set rich to read material/color/brand from the tag"
        echo "   and send that data to Happy Hare. Without rich mode only a UID is recorded."
    fi
    prompt_choice TAG_MODE \
        "   Select tag read mode" \
        "$( [ "${SPOOLMAN_MODE}" = "disabled" ] && echo "rich" || echo "spoolman" )" \
        "spoolman" "rich"

    BAMBU_READS="no"
    SPOOLMAN_AUTO_CREATE="no"
    if [ "${TAG_MODE}" = "rich" ]; then
        echo ""
        echo "   Rich read supports NTAG/Type-2 metadata tags."
        echo "   Use rich when you want OpenSpool/OpenPrintTag/Bambu metadata reads."
        echo "   Factory-tagged Bambu spools are MIFARE Classic and require"
        echo "   authenticated reads plus the pycryptodome HKDF dependency."
        prompt_yes_no BAMBU_READS \
            "9. Will you read factory-tagged Bambu spools with rich metadata?" \
            "no"
        if [ "${SPOOLMAN_MODE}" != "disabled" ]; then
            prompt_yes_no SPOOLMAN_AUTO_CREATE \
                "10. Auto-create missing Spoolman spools from rich tag metadata?" \
                "yes"
        fi
    fi

    I2C_MCU=""   # not applicable for lane installs

# ── Shared path ───────────────────────────────────────────────────────────────
else

    LANE_COUNT="0"
    LANE_MCU_PREFIX=""
    LANE_I2C_BUS=""
    LANE_BUS_LABEL=""
    SCAN_JOG_MAX_MODE="bowden"
    SCAN_JOG_MAX=""
    SCAN_ENABLED="no"   # always disabled for shared reader

    DEFAULT_SHARED_READER_TYPE="$(detect_shared_config_value "${NFC_READER_SHARED_CFG}" "reader_type" "pn532")"
    echo "2. Shared reader hardware"
    echo "   $(choice_style pn532 "${DEFAULT_SHARED_READER_TYPE}")  = I2C, ISO14443A"
    echo "   $(choice_style pn7160 "${DEFAULT_SHARED_READER_TYPE}") = I2C, ISO14443A and ISO15693"
    echo "   $(choice_style rc522 "${DEFAULT_SHARED_READER_TYPE}")  = SPI, ISO14443A"
    echo "   $(choice_style pn5180 "${DEFAULT_SHARED_READER_TYPE}") = SPI, ISO14443A and ISO15693"
    prompt_choice SHARED_READER_TYPE \
        "   Select shared reader hardware" \
        "${DEFAULT_SHARED_READER_TYPE}" \
        "pn532" "pn7160" "rc522" "pn5180"
    echo ""

    echo "3. Spoolman connection"
    echo "   $(choice_style auto auto)     = read the URL from Moonraker's [spoolman] section (recommended)"
    echo "   $(choice_style direct auto)   = enter a fixed URL such as http://127.0.0.1:7912"
    echo "   $(choice_style disabled auto) = no Spoolman — resolve by tag metadata or UID only"
    prompt_choice SPOOLMAN_MODE \
        "   Select Spoolman connection mode" \
        "auto" \
        "auto" "direct" "disabled"
    SPOOLMAN_URL="auto"
    if [ "${SPOOLMAN_MODE}" = "direct" ]; then
        prompt_with_default SPOOLMAN_URL \
            "   Enter the direct Spoolman URL" \
            "http://127.0.0.1:7912"
    elif [ "${SPOOLMAN_MODE}" = "disabled" ]; then
        SPOOLMAN_URL="disabled"
    fi

    prompt_yes_no STARTUP_POLLING \
        "4. Poll at Klipper boot so you can tap a spool at any time? (recommended)" \
        "yes"

    MMU_LED_UNIT="$(detect_mmu_led_unit "${MMU_HW_CFG}")"
    I2C_MCU=""
    I2C_BUS=""
    I2C_ADDRESS=""
    SPI_BUS=""
    SPI_CS_PIN=""
    SPI_SPEED=""
    PN5180_RESET_PIN=""
    PN5180_BUSY_PIN=""
    if [ "${SHARED_READER_TYPE}" = "pn532" ] || [ "${SHARED_READER_TYPE}" = "pn7160" ]; then
        DEFAULT_I2C_MCU="$(detect_shared_mcu "${NFC_READER_SHARED_CFG}")"
        prompt_with_default I2C_MCU \
            "5. Klipper MCU the shared NFC reader is wired to (must match a [mcu ...] section)" \
            "${DEFAULT_I2C_MCU}"
        DEFAULT_I2C_BUS="$(detect_shared_i2c_bus "${NFC_READER_SHARED_CFG}")"
        prompt_i2c_bus_select I2C_BUS "${I2C_MCU}" "${PRINTER_CONFIG}" "${DEFAULT_I2C_BUS}"
        if [ "${SHARED_READER_TYPE}" = "pn7160" ]; then
            I2C_ADDRESS="$(detect_shared_config_value "${NFC_READER_SHARED_CFG}" "i2c_address" "40")"
            prompt_with_default I2C_ADDRESS \
                "   PN7160 I2C address (40-43)" "${I2C_ADDRESS}"
        else
            I2C_ADDRESS="36"
        fi
    else
        SPI_BUS="$(detect_shared_config_value "${NFC_READER_SHARED_CFG}" "spi_bus" "spi2_PB14_PB15_PB13")"
        SPI_CS_PIN="$(detect_shared_config_value "${NFC_READER_SHARED_CFG}" "cs_pin" "mmu:PA8")"
        SPI_DEFAULT_SPEED="500000"
        SPI_SPEED="$(detect_shared_config_value "${NFC_READER_SHARED_CFG}" "spi_speed" "${SPI_DEFAULT_SPEED}")"
        prompt_with_default SPI_BUS "5. SPI bus name" "${SPI_BUS}"
        prompt_with_default SPI_CS_PIN "6. SPI chip-select pin" "${SPI_CS_PIN}"
        prompt_with_default SPI_SPEED \
            "7. SPI speed (Hz; hardware 500k, software 100k)" "${SPI_SPEED}"
        if [ "${SHARED_READER_TYPE}" = "pn5180" ]; then
            PN5180_RESET_PIN="$(detect_shared_config_value "${NFC_READER_SHARED_CFG}" "reset_pin" "mmu:PC6")"
            PN5180_BUSY_PIN="$(detect_shared_config_value "${NFC_READER_SHARED_CFG}" "busy_pin" "mmu:PB0")"
            prompt_with_default PN5180_RESET_PIN "8. PN5180 reset pin" "${PN5180_RESET_PIN}"
            prompt_with_default PN5180_BUSY_PIN "9. PN5180 BUSY pin" "${PN5180_BUSY_PIN}"
        fi
    fi

    echo "10. Tag read mode"
    echo "   $(choice_style spoolman spoolman) = UID-only lookup in Spoolman's extra field"
    echo "   $(choice_style rich spoolman)     = read tag metadata, then resolve/create Spoolman records"
    if [ "${SPOOLMAN_MODE}" = "disabled" ]; then
        echo ""
        echo "   NOTE: Spoolman is disabled. Set rich to read material/color/brand from the tag"
        echo "   and send that data to Happy Hare. Without rich mode only a UID is recorded."
    fi
    prompt_choice TAG_MODE \
        "   Select tag read mode" \
        "$( [ "${SPOOLMAN_MODE}" = "disabled" ] && echo "rich" || echo "spoolman" )" \
        "spoolman" "rich"

    BAMBU_READS="no"
    SPOOLMAN_AUTO_CREATE="no"
    if [ "${TAG_MODE}" = "rich" ]; then
        echo ""
        echo "   Rich read supports NTAG/Type-2 metadata tags."
        echo "   Use rich when you want OpenSpool/OpenPrintTag/Bambu metadata reads."
        echo "   Factory-tagged Bambu spools are MIFARE Classic and require"
        echo "   authenticated reads plus the pycryptodome HKDF dependency."
        prompt_yes_no BAMBU_READS \
            "11. Will you read factory-tagged Bambu spools with rich metadata?" \
            "no"
        if [ "${SPOOLMAN_MODE}" != "disabled" ]; then
            prompt_yes_no SPOOLMAN_AUTO_CREATE \
                "12. Auto-create missing Spoolman spools from rich tag metadata?" \
                "yes"
        fi
    fi

fi

# ── Summary + confirm before any writes ──────────────────────────────────────
echo ""
echo "${BOLD}════════════════════════════════════════════════════════════════${RESET}"
echo "${BOLD}  Install summary — review before writing${RESET}"
echo "${BOLD}════════════════════════════════════════════════════════════════${RESET}"
echo "  Happy Hare:        ${HH_VERSION}"
echo "  Reader layout:     ${READER_TYPE}"
echo "  Spoolman:          ${SPOOLMAN_URL}"
if [ "${READER_TYPE}" = "lane" ]; then
    echo "  Lane polling:      disabled (post-preload hook)"
else
    echo "  Startup polling:   ${STARTUP_POLLING}"
fi
if [ "${READER_TYPE}" = "shared" ]; then
    echo "  Reader hardware:   ${SHARED_READER_TYPE}"
    if [ "${SHARED_READER_TYPE}" = "pn532" ] || [ "${SHARED_READER_TYPE}" = "pn7160" ]; then
        echo "  i2c_mcu:           ${I2C_MCU}"
        echo "  i2c_bus:           ${I2C_BUS}"
        echo "  i2c_address:       ${I2C_ADDRESS}"
    else
        echo "  spi_bus:           ${SPI_BUS}"
        echo "  cs_pin:            ${SPI_CS_PIN}"
        echo "  spi_speed:         ${SPI_SPEED}"
        if [ "${SHARED_READER_TYPE}" = "pn5180" ]; then
            echo "  reset_pin:         ${PN5180_RESET_PIN}"
            echo "  busy_pin:          ${PN5180_BUSY_PIN}"
        fi
    fi
    echo "  LED effects:       (whole-chain — all gate exit LEDs flash simultaneously)"
    echo "    tag detected:    ${BOLD}${MMU_LED_UNIT}_mmu_RFID_read_exit${RESET}"
    echo "    spool ready:     ${BOLD}${MMU_LED_UNIT}_mmu_RFID_ready_exit${RESET}"
    echo "    unresolved:      ${BOLD}${MMU_LED_UNIT}_mmu_RFID_unresolved_exit${RESET}"
    echo "    auto-create:     ${BOLD}${MMU_LED_UNIT}_mmu_RFID_creating_exit${RESET}"
    echo "    bypass read:     ${BOLD}${MMU_LED_UNIT}_mmu_RFID_bypass_read_exit${RESET}"
    echo "    bypass ready:    ${BOLD}${MMU_LED_UNIT}_mmu_RFID_bypass_ready_exit${RESET}"
else
    echo "  Lane count:        ${LANE_COUNT}"
    if [ "${HH_VERSION}" = "v4" ]; then
        echo "  Lane mapping:      unit0_laneN -> unit0_gateN"
        echo "  Endstop mapping:   nfc_unit0_laneN"
    else
        echo "  Lane MCU prefix:   ${LANE_MCU_PREFIX}  (${LANE_MCU_PREFIX}0, ${LANE_MCU_PREFIX}1, ...)"
    fi
    echo "  Lane I2C bus:      ${LANE_I2C_BUS} (${LANE_BUS_LABEL})"
    echo "  Scan-jog:          ${SCAN_ENABLED}"
    if [ "${SCAN_JOG_MAX_MODE}" = "fixed" ]; then
        echo "  Scan max travel:   ${SCAN_JOG_MAX}mm (scan_jog_max)"
    else
        echo "  Scan max travel:   Happy Hare Bowden calibration per lane"
    fi
    echo "  Note: one bus question assumes homogeneous lane MCUs; edit nfc_reader.cfg or"
    echo "        add per-lane overrides in nfc_reader_hw.cfg for mixed MCU setups."
    echo "  LED effects:       (per active gate — _exit_N suffix applied automatically)"
    echo "    searching:       ${DEFAULT}mmu_RFID_searching${RESET}"
    echo "    tag read:        ${DEFAULT}mmu_RFID_read${RESET}"
    echo "    rewinding:       ${DEFAULT}mmu_RFID_rewinding${RESET}"
    echo "    auto-create:     ${DEFAULT}mmu_RFID_creating${RESET}"
    echo "    unresolved:      ${DEFAULT}mmu_RFID_unresolved${RESET}"
fi
echo "  Tag mode:          ${TAG_MODE}"
if [ "${TAG_MODE}" = "rich" ]; then
    echo "  Bambu reads:       ${BAMBU_READS}"
    echo "  Auto-create:       ${SPOOLMAN_AUTO_CREATE}"
fi
echo ""
echo "  Files that will be written / merged:"
echo "    ${BOLD}${NFC_READER_CFG}${RESET}"
echo "    ${BOLD}${NFC_CONFIG_DIR}/nfc_macros.cfg${RESET}"
if [ "${READER_TYPE}" = "shared" ]; then
    echo "    ${BOLD}${NFC_READER_SHARED_CFG}${RESET}  (settings applied)"
else
    echo "    ${BOLD}${NFC_READER_HW_CFG}${RESET}  (settings applied)"
fi
echo "${BOLD}════════════════════════════════════════════════════════════════${RESET}"
echo ""
prompt_yes_no _CONFIRM_INSTALL \
    "  Start the install with these settings?" \
    "yes"
if [ "${_CONFIRM_INSTALL}" != "yes" ]; then
    echo "  Install cancelled — no files were written."
    exit 0
fi
echo ""

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

echo "Linking mmu_nfc_endstop.py..."
ln -sfn "${REPO_DIR}/klippy/extras/mmu_nfc_endstop.py" "${KLIPPER_EXTRAS}/mmu_nfc_endstop.py"

echo "Linking nfc_gates/ package..."
ln -sfn "${REPO_DIR}/klippy/extras/nfc_gates" "${KLIPPER_EXTRAS}/nfc_gates"

# ── Create NFC config directory if it does not exist ─────────────────────────
if [ -e "${NFC_CONFIG_DIR}" ] && [ ! -d "${NFC_CONFIG_DIR}" ]; then
    echo "ERROR: ${NFC_CONFIG_DIR} exists but is not a directory."
    echo "       Remove or rename it, then re-run install.sh."
    exit 1
fi
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

merge_config "${REPO_DIR}/config/nfc_reader.cfg"        "${NFC_READER_CFG}"
merge_config "${REPO_DIR}/config/nfc_macros.cfg"         "${NFC_CONFIG_DIR}/nfc_macros.cfg"
if [ "${READER_TYPE}" != "shared" ]; then
    merge_config "${REPO_DIR}/config/nfc_reader_hw.cfg"  "${NFC_READER_HW_CFG}"
    echo "  [notice]   Adding NFC virtual endstop definitions below each per-lane reader when missing."
    ensure_mmu_nfc_endstops "${NFC_READER_HW_CFG}"
else
    echo "  [skip]     nfc_reader_hw.cfg — shared reader install does not need lane sections"
fi
if [ "${READER_TYPE}" = "shared" ]; then
    merge_config "${REPO_DIR}/config/nfc_reader_shared.cfg"  "${NFC_READER_SHARED_CFG}"
else
    echo "  [skip]     nfc_reader_shared.cfg — lane reader install does not need shared-reader config"
fi

echo ""
echo "Applying selected settings..."

# Settings common to both reader layouts
set_config_value "${NFC_READER_CFG}" "nfc_gate" "spoolman_url" "${SPOOLMAN_URL}"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "tag_parsing" \
    "$( [ "${TAG_MODE}" = "rich" ] && echo "True" || echo "False" )"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "bambu_reads" \
    "$( [ "${BAMBU_READS}" = "yes" ] && echo "True" || echo "False" )"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "spoolman_auto_create" \
    "$( [ "${SPOOLMAN_AUTO_CREATE}" = "yes" ] && echo "True" || echo "False" )"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "happy_hare_v4" \
    "$( [ "${HH_VERSION}" = "v4" ] && echo "True" || echo "False" )"

if [ "${READER_TYPE}" = "shared" ]; then
    # startup_polling and scan_enabled live in [nfc_gate shared], not [nfc_gate]
    set_config_value "${NFC_READER_SHARED_CFG}" "nfc_gate shared" "reader_type" "${SHARED_READER_TYPE}"
    if [ "${SHARED_READER_TYPE}" = "pn532" ] || [ "${SHARED_READER_TYPE}" = "pn7160" ]; then
        set_config_value "${NFC_READER_SHARED_CFG}" "nfc_gate shared" "i2c_mcu" "${I2C_MCU}"
        set_config_value "${NFC_READER_SHARED_CFG}" "nfc_gate shared" "i2c_bus" "${I2C_BUS}"
        set_config_value "${NFC_READER_SHARED_CFG}" "nfc_gate shared" "i2c_address" "${I2C_ADDRESS}"
    else
        set_config_value "${NFC_READER_SHARED_CFG}" "nfc_gate shared" "spi_bus" "${SPI_BUS}"
        set_config_value "${NFC_READER_SHARED_CFG}" "nfc_gate shared" "cs_pin" "${SPI_CS_PIN}"
        set_config_value "${NFC_READER_SHARED_CFG}" "nfc_gate shared" "spi_speed" "${SPI_SPEED}"
        if [ "${SHARED_READER_TYPE}" = "pn5180" ]; then
            set_config_value "${NFC_READER_SHARED_CFG}" "nfc_gate shared" "reset_pin" "${PN5180_RESET_PIN}"
            set_config_value "${NFC_READER_SHARED_CFG}" "nfc_gate shared" "busy_pin" "${PN5180_BUSY_PIN}"
        fi
    fi
    set_config_value "${NFC_READER_SHARED_CFG}" "nfc_gate shared" "shared" "true"
    set_config_value "${NFC_READER_SHARED_CFG}" "nfc_gate shared" "startup_polling" \
        "$( [ "${STARTUP_POLLING}" = "yes" ] && echo "1" || echo "0" )"
else
    set_lane_i2c_bus "${NFC_READER_CFG}" "${LANE_I2C_BUS}" "${LANE_BUS_LABEL}"
    set_scan_jog_max_config "${NFC_READER_CFG}" \
        "${SCAN_JOG_MAX_MODE}" "${SCAN_JOG_MAX:-480.0}"
    set_config_value "${NFC_READER_CFG}" "nfc_gate" "startup_polling" \
        "$( [ "${STARTUP_POLLING}" = "yes" ] && echo "1" || echo "-1" )"
    set_config_value "${NFC_READER_CFG}" "nfc_gate" "scan_enabled" \
        "$( [ "${SCAN_ENABLED}" = "yes" ] && echo "True" || echo "False" )"
    write_lane_config "${NFC_READER_HW_CFG}" "${LANE_COUNT}" "${LANE_MCU_PREFIX}"
    echo "  [notice]   Verifying NFC virtual endstop definitions below each per-lane reader."
    ensure_mmu_nfc_endstops "${NFC_READER_HW_CFG}"
    warn_software_i2c_sensors "${LANE_I2C_BUS}"
fi

CBOR_STATUS="not needed in UID-only mode"
if [ "${TAG_MODE}" = "rich" ]; then
    CBOR_STATUS="not installed; built-in minimal fallback active"
    echo ""
    echo "Checking OpenPrintTag CBOR dependency..."
    if ensure_python_module "cbor2" "cbor2"; then
        CBOR_STATUS="installed (OpenPrintTag CBOR support)"
    else
        echo ""
        echo "WARNING: cbor2 not installed — complex OpenPrintTag CBOR payloads will fall back"
        echo "         to the built-in minimal decoder. Most tags work fine without it."
        echo "         To add it later: \$(detect_klipper_python) -m pip install cbor2"
    fi
fi

if [ "${BAMBU_READS}" = "yes" ]; then
    echo ""
    echo "Checking Bambu/MIFARE crypto dependency..."
    if ! ensure_python_module "Crypto.Protocol.KDF" "pycryptodome"; then
        echo ""
        echo "ERROR: Bambu rich reads need pycryptodome in the Klipper Python environment."
        echo "       Re-run this installer after installing it, or answer 'no' to Bambu reads."
        exit 1
    fi
fi

# ── Moonraker update_manager ──────────────────────────────────────────────────
#
# Append [update_manager Happy-Hare-RFID-Reader] to moonraker.conf if not already present.
# The section is identical every install so idempotency is a simple grep check.
#
MOONRAKER_CONF="${PRINTER_CONFIG}/moonraker.conf"
MOONRAKER_SECTION="[update_manager Happy-Hare-RFID-Reader]"
LEGACY_MOONRAKER_SECTION="[update_manager emu_nfc_reader]"
PREVIOUS_MOONRAKER_SECTION="[update_manager happy_hare_rfid_reader]"
PREVIOUS_MOONRAKER_SECTION_MIXED="[update_manager Happy-Hare-rfid-reader]"

if [ ! -f "${MOONRAKER_CONF}" ]; then
    echo "  [skip]   moonraker.conf not found at ${MOONRAKER_CONF} — add update_manager manually"
    echo "           ${MOONRAKER_SECTION}"
    echo "           type:             git_repo"
    echo "           path:             ${REPO_DIR}"
    echo "           origin:           ${RFID_READER_REPO_URL}"
    echo "           primary_branch:   main"
    echo "           managed_services: klipper"
    echo "           install_script:   install.sh"
elif grep -qF "${MOONRAKER_SECTION}" "${MOONRAKER_CONF}"; then
    echo "  [skip]   moonraker.conf already has ${MOONRAKER_SECTION}"
else
    if grep -qF "${LEGACY_MOONRAKER_SECTION}" "${MOONRAKER_CONF}"; then
        backup_and_remove_legacy_moonraker_section "${LEGACY_MOONRAKER_SECTION}"
    fi
    if grep -qF "${PREVIOUS_MOONRAKER_SECTION}" "${MOONRAKER_CONF}"; then
        backup_and_remove_legacy_moonraker_section "${PREVIOUS_MOONRAKER_SECTION}"
    fi
    if grep -qF "${PREVIOUS_MOONRAKER_SECTION_MIXED}" "${MOONRAKER_CONF}"; then
        backup_and_remove_legacy_moonraker_section "${PREVIOUS_MOONRAKER_SECTION_MIXED}"
    fi
    cat >> "${MOONRAKER_CONF}" <<MOONRAKER

${MOONRAKER_SECTION}
type:             git_repo
path:             ${REPO_DIR}
origin:           ${RFID_READER_REPO_URL}
primary_branch:   main
managed_services: klipper
install_script:   install.sh
info_tags:        desc=EMU NFC Gate Reader for Happy Hare
MOONRAKER
    echo "  [added]  ${MOONRAKER_SECTION} → ${MOONRAKER_CONF}"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "${BOLD}Install complete.${RESET}"
echo ""
if [ "${LEGACY_CUTOVER_PERFORMED:-no}" = "yes" ]; then
    echo "  Legacy beta cutover:"
    echo "    old repo removed:   ${RFID_READER_LEGACY_DIR}"
    echo "    fresh repo:         ${RFID_READER_INSTALL_DIR}"
    if [ -n "${LEGACY_NFC_BACKUP:-}" ]; then
        echo "    config backup:      ${LEGACY_NFC_BACKUP}"
    else
        echo "    config backup:      no previous NFC config directory found"
    fi
    if [ -n "${LEGACY_MOONRAKER_BACKUP:-}" ]; then
        echo "    moonraker backup:   ${LEGACY_MOONRAKER_BACKUP}"
    else
        echo "    moonraker backup:   no legacy Moonraker block found"
    fi
    echo ""
fi
echo "  Selected options:"
    echo "    reader layout:      ${READER_TYPE}"
if [ "${READER_TYPE}" = "lane" ]; then
    echo "    lanes:              ${LANE_COUNT}"
    echo "    lane_mcu_prefix:    ${LANE_MCU_PREFIX}"
    echo "    lane_i2c_bus:       ${LANE_I2C_BUS} (${LANE_BUS_LABEL})"
    if [ "${SCAN_JOG_MAX_MODE}" = "fixed" ]; then
        echo "    scan_max_travel:    ${SCAN_JOG_MAX}mm"
    else
        echo "    scan_max_travel:    Bowden calibration"
    fi
else
    echo "    reader_type:        ${SHARED_READER_TYPE}"
    if [ "${SHARED_READER_TYPE}" = "pn532" ] || [ "${SHARED_READER_TYPE}" = "pn7160" ]; then
        echo "    i2c_mcu:            ${I2C_MCU}"
        echo "    i2c_bus:            ${I2C_BUS}"
        echo "    i2c_address:        ${I2C_ADDRESS}"
    else
        echo "    spi_bus:            ${SPI_BUS}"
        echo "    cs_pin:             ${SPI_CS_PIN}"
        echo "    spi_speed:          ${SPI_SPEED}"
        if [ "${SHARED_READER_TYPE}" = "pn5180" ]; then
            echo "    reset_pin:          ${PN5180_RESET_PIN}"
            echo "    busy_pin:           ${PN5180_BUSY_PIN}"
        fi
    fi
fi
echo "    spoolman_url:       ${SPOOLMAN_URL}"
if [ "${READER_TYPE}" = "lane" ]; then
    echo "    startup_polling:    -1 (post-preload hook)"
else
    echo "    startup_polling:    ${STARTUP_POLLING}"
fi
if [ "${READER_TYPE}" = "lane" ]; then
    echo "    scan_jog:           ${SCAN_ENABLED}"
fi
if [ "${TAG_MODE}" = "rich" ]; then
    echo "    tag_resolution:     rich tag metadata"
else
    echo "    tag_resolution:     Spoolman UID lookup"
fi
if [ "${TAG_MODE}" != "rich" ]; then
    echo "    bambu_mifare:       not active in UID-only mode"
elif [ "${BAMBU_READS}" = "yes" ]; then
    echo "    bambu_mifare:       enabled (authenticated rich read)"
else
    echo "    bambu_mifare:       installer did not add crypto; UID fallback if absent"
fi
echo "    cbor2:              ${CBOR_STATUS}"
echo "    spool_auto_create:  ${SPOOLMAN_AUTO_CREATE}"
echo ""
echo "  Python extras (symlinked — auto-updates with git pull):"
echo "    ${KLIPPER_EXTRAS}/nfc_gate.py  ->  ${REPO_DIR}/klippy/extras/nfc_gate.py"
echo "    ${KLIPPER_EXTRAS}/mmu_nfc_endstop.py  ->  ${REPO_DIR}/klippy/extras/mmu_nfc_endstop.py"
echo "    ${KLIPPER_EXTRAS}/nfc_gates    ->  ${REPO_DIR}/klippy/extras/nfc_gates/"
echo ""
echo "  Config files in ${NFC_CONFIG_DIR}/:"
echo "    nfc_reader.cfg          ← Spoolman URL, tag parsing, debug settings"
echo "    nfc_macros.cfg          ← Happy Hare handoff macros"
if [ "${READER_TYPE}" = "shared" ]; then
    echo "    nfc_reader_shared.cfg   ← [nfc_gate shared] hardware config  (settings applied)"
else
    echo "    nfc_reader_hw.cfg       ← [nfc_gate laneN] hardware layout   (settings applied)"
fi
echo "  To add the other hardware config later, re-run install.sh with the other"
echo "  reader layout selected."
echo ""
echo "Next steps (first install only):"
echo ""

if [ "${READER_TYPE}" = "shared" ]; then
    if [ "${SHARED_READER_TYPE}" = "pn532" ] || [ "${SHARED_READER_TYPE}" = "pn7160" ]; then
        echo "  1. Confirm i2c_mcu, i2c_bus, and i2c_address in nfc_reader_shared.cfg."
        echo "     The installer wrote i2c_mcu: ${I2C_MCU}, i2c_bus: ${I2C_BUS}, and address: ${I2C_ADDRESS}."
    else
        echo "  1. Confirm SPI wiring in nfc_reader_shared.cfg."
        echo "     The installer wrote spi_bus: ${SPI_BUS}, cs_pin: ${SPI_CS_PIN}, and speed: ${SPI_SPEED}."
        if [ "${SHARED_READER_TYPE}" = "pn5180" ]; then
            echo "     PN5180 reset_pin: ${PN5180_RESET_PIN}; busy_pin: ${PN5180_BUSY_PIN}."
        fi
    fi
    echo ""
    echo "  2. Add includes to printer.cfg:"
    echo "       [include nfc/nfc_reader.cfg]"
    echo "       [include nfc/nfc_macros.cfg]"
    echo "       [include nfc/nfc_reader_shared.cfg]"
    echo ""
    echo "  3. Restart Klipper:"
    echo "     sudo systemctl restart klipper"
    echo ""
    echo "  4. Update and flash the MCU hosting the shared NFC reader."
    echo "     The MCU must be on the same Klipper version as the host."
    echo ""
    echo "  5. Wire the Happy Hare post-preload hook in mmu_macro_vars.cfg:"
    echo "       variable_user_post_preload_extension: '_NFC_SHARED_PRELOAD'"
    echo "     If this printer also has per-lane readers, use:"
    echo "       variable_user_post_preload_extension: '_NFC_HYBRID_PRELOAD'"
    echo "     Polling pauses/resumes automatically on print start/end —"
    echo "     the post-unload hook is no longer needed."
    echo ""
    echo "  6. Moonraker update_manager — added automatically by this script."
    echo "     If moonraker.conf was not found, add [update_manager Happy-Hare-RFID-Reader] manually."
    echo ""
    if [ "${SHARED_READER_TYPE}" = "pn7160" ]; then
        echo "  PN7160 note:"
        echo "     i2c_address must be 40-43, matching the module address switches."
        echo "     See docs/i2c-nfc/pn7160-wiring.md for VEN/IRQ and I2C bus notes."
        echo ""
    elif [ "${SHARED_READER_TYPE}" = "pn5180" ]; then
        echo "  PN5180 note:"
        echo "     BUSY and RST are mandatory; BUSY is active high. Both 5V and PSF 3.3V"
        echo "     power connections are required. See docs/i2c-nfc/pn5180-wiring.md."
        echo "     Do not use pn5180_command_delay; the driver synchronizes every SPI"
        echo "     command with busy_pin instead."
        echo ""
    fi
else
    echo "  1. Review ~/printer_data/config/nfc/nfc_reader.cfg"
    echo "     The installer has applied your selected settings."
    echo "     The installer wrote i2c_bus: ${LANE_I2C_BUS} in the base [nfc_gate]."
    if [ "${HH_VERSION}" = "v4" ]; then
        echo "     The installer wrote V4 lane sections as unit0_lane0, unit0_lane1, etc."
        echo "     Their i2c_mcu values are unit0_gate0, unit0_gate1, etc."
    else
        echo "     The installer wrote lane i2c_mcu values as ${LANE_MCU_PREFIX}0, ${LANE_MCU_PREFIX}1, etc."
    fi
    if [ "${SCAN_JOG_MAX_MODE}" = "fixed" ]; then
        echo "     The installer wrote scan_jog_max: ${SCAN_JOG_MAX} for scan-jog."
    else
        echo "     scan_jog_max is commented; scan-jog uses Happy Hare Bowden lengths."
    fi
    echo "     If your lane MCUs are not homogeneous, add per-lane i2c_bus overrides"
    echo "     in ~/printer_data/config/nfc/nfc_reader_hw.cfg."
    echo "     Also review ~/printer_data/config/nfc/nfc_reader_hw.cfg"
    echo "     and confirm each lane's i2c_mcu name matches your Klipper config."
    echo ""
    echo "  2. Add includes to printer.cfg:"
    echo "       [include nfc/nfc_reader.cfg]"
    echo "       [include nfc/nfc_macros.cfg]"
    echo "       [include nfc/nfc_reader_hw.cfg]"
    echo ""
    echo "  Configure the Happy Hare post-preload hook in mmu_macro_vars.cfg:"
    echo "       variable_user_post_preload_extension: '_NFC_SCAN_JOG_PRELOAD'"
    echo "     If you later add nfc_reader_shared.cfg, replace it with:"
    echo "       variable_user_post_preload_extension: '_NFC_HYBRID_PRELOAD'"
    echo ""
    echo "  3. Restart Klipper:"
    echo "     sudo systemctl restart klipper"
    echo ""
    echo "  4. Update and flash Klipper on each lane MCU / EBB42 board used by NFC."
    echo ""
    echo "  5. Moonraker update_manager — added automatically by this script."
    echo "     If moonraker.conf was not found, add [update_manager Happy-Hare-RFID-Reader] manually."
    echo ""
    echo "  PN7160 note:"
    if [ "${HH_VERSION}" = "v4" ]; then
        echo "     For each PN7160 lane, edit that [nfc_gate unit0_laneN] section in nfc_reader_hw.cfg:"
    else
        echo "     For each PN7160 lane, edit that [nfc_gate laneN] section in nfc_reader_hw.cfg:"
    fi
    echo "       reader_type: pn7160"
    echo "       i2c_address: 40   # 40-43 depending on address switches"
    echo "     See docs/i2c-nfc/pn7160-wiring.md for VEN/IRQ and I2C bus notes."
    echo ""
fi
