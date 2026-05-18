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

# ── CLI arguments ─────────────────────────────────────────────────────────────
_CLI_PROFILE=""
while getopts "p:" _opt; do
    case "$_opt" in
        p) _CLI_PROFILE="$OPTARG" ;;
    esac
done
shift $(( OPTIND - 1 ))

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_EXTRAS="${HOME}/klipper/klippy/extras"
PRINTER_CONFIG="${HOME}/printer_data/config"
NFC_CONFIG_DIR="${PRINTER_CONFIG}/nfc"
NFC_READER_CFG="${NFC_CONFIG_DIR}/nfc_reader.cfg"
NFC_READER_HW_CFG="${NFC_CONFIG_DIR}/nfc_reader_hw.cfg"
NFC_READER_SHARED_CFG="${NFC_CONFIG_DIR}/nfc_reader_shared.cfg"
MMU_HW_CFG="${PRINTER_CONFIG}/mmu/base/mmu_hardware.cfg"

if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"
    RESET="$(printf '\033[0m')"
    GREEN="$(printf '\033[32m')"
    CYAN="$(printf '\033[1;96m')"
    BRIGHT_GREEN="$(printf '\033[1;32m')"
    YELLOW="$(printf '\033[1;33m')"
    MAGENTA="$(printf '\033[1;35m')"
    WHITE="$(printf '\033[1;37m')"
    ORANGE="$(printf '\033[38;5;214m')"
    DEFAULT="${CYAN}"   # interim default until profile is resolved
else
    BOLD="" RESET="" GREEN="" CYAN="" BRIGHT_GREEN="" YELLOW="" MAGENTA="" WHITE="" ORANGE="" DEFAULT=""
fi

# ── Terminal profile → highlight color map ──────────────────────────────────
# Called once after _CLI_PROFILE is known (from -p or the interactive prompt).
# Sets DEFAULT to the best-contrast color for the named profile.
apply_profile_color() {
    local _color
    case "$1" in
        # ── macOS Terminal.app ─────────────────────────────────────────
        "Homebrew")         _color="$(printf '\033[1;96m')"  ;;  # bright cyan   — green text bg, cyan contrasts
        "Ocean")            _color="$(printf '\033[1;32m')"  ;;  # bright green  — pops on dark blue background
        "Grass")            _color="$(printf '\033[1;33m')"  ;;  # bright yellow — readable on green bg
        "Novel")            _color="$(printf '\033[0;34m')"  ;;  # dark blue     — light tan background
        "Pro")              _color="$(printf '\033[1;36m')"  ;;  # bright cyan   — dark background
        "Basic")            _color="$(printf '\033[0;34m')"  ;;  # dark blue     — white background
        "Manuscript")       _color="$(printf '\033[0;34m')"  ;;  # dark blue     — cream background
        "Red Sands")        _color="$(printf '\033[1;33m')"  ;;  # bright yellow — dark red background
        "Silver Aerogel")   _color="$(printf '\033[0;36m')"  ;;  # dark cyan     — grey background
        "Solid Colors")     _color="$(printf '\033[1;37m')"  ;;  # bright white  — unknown bg

        # ── iTerm2 built-in ────────────────────────────────────────────
        "Default")          _color="$(printf '\033[1;96m')"  ;;  # bright cyan
        "Dark Background")  _color="$(printf '\033[1;96m')"  ;;  # bright cyan
        "Light Background") _color="$(printf '\033[0;34m')"  ;;  # dark blue
        "Pastel"*)          _color="$(printf '\033[1;96m')"  ;;  # bright cyan   — dark pastel bg
        "Solarized Dark")   _color="$(printf '\033[1;33m')"  ;;  # bright yellow — solarized accent
        "Solarized Light")  _color="$(printf '\033[0;34m')"  ;;  # dark blue
        "Tango Dark")       _color="$(printf '\033[1;96m')"  ;;  # bright cyan
        "Tango Light")      _color="$(printf '\033[0;34m')"  ;;  # dark blue
        "Smoooooth")        _color="$(printf '\033[1;96m')"  ;;  # bright cyan

        # ── Popular community themes (iTerm2) ─────────────────────────
        "Dracula")          _color="$(printf '\033[1;35m')"  ;;  # bright magenta — matches Dracula purple
        "Monokai"*)         _color="$(printf '\033[1;33m')"  ;;  # bright yellow  — matches Monokai
        "Gruvbox Dark")     _color="$(printf '\033[1;33m')"  ;;  # bright yellow/orange
        "Gruvbox Light")    _color="$(printf '\033[0;31m')"  ;;  # dark red
        "Nord")             _color="$(printf '\033[1;96m')"  ;;  # bright cyan    — Nord frost palette, contrasts on dark blue-grey
        "One Dark")         _color="$(printf '\033[1;96m')"  ;;  # bright cyan    — Atom One Dark
        "Cobalt2")          _color="$(printf '\033[1;33m')"  ;;  # bright yellow
        "Catppuccin"*)      _color="$(printf '\033[1;35m')"  ;;  # bright magenta — matches Catppuccin mauve

        *)                  _color="$(printf '\033[1;96m')"  ;;  # bright cyan fallback
    esac
    DEFAULT="${_color}"
}

choice_style() {
    case "$1" in
        auto|spoolman|lane)
            printf '%s%s%s%s' "${DEFAULT}" "${BOLD}" "$1" "${RESET}"
            ;;
        direct|rich|shared)
            printf '%s' "$1"
            ;;
        *)
            printf '%s%s%s' "${BOLD}" "$1" "${RESET}"
            ;;
    esac
}

print_banner() {
    printf '%s' "${DEFAULT}${BOLD}"
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
    read -r -p "${prompt_text} [${DEFAULT}${BOLD}${default_value}${RESET}]: " reply
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
        default_hint="${DEFAULT}${BOLD}Y${RESET}/n"
    else
        default_hint="y/${DEFAULT}${BOLD}N${RESET}"
    fi

    while true; do
        read -r -p "${prompt_text} [${default_hint}]: " reply
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
        choice_label="${choice}"
        if [ "${choice}" = "${default_value}" ] ||
           [ "${choice}" = "direct" ] || [ "${choice}" = "rich" ]; then
            choice_label="$(choice_style "${choice}")"
        fi
        if [ -z "${choices_label}" ]; then
            choices_label="${choice_label}"
        else
            choices_label="${choices_label}/${choice_label}"
        fi
    done

    while true; do
        read -r -p "${prompt_text} [${choices_label}]: " reply
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

count_lane_sections() {
    local file_path="$1"
    python3 - "${file_path}" <<'PYEOF'
import re
import sys

path = sys.argv[1]
try:
    text = open(path, 'r').read()
except FileNotFoundError:
    print(5)
    raise SystemExit

count = len(re.findall(r'^\[nfc_gate lane\d+\]\s*$', text, flags=re.M))
print(count or 5)
PYEOF
}

write_lane_config() {
    local file_path="$1"
    local lane_count="$2"

    python3 - "${file_path}" "${lane_count}" <<'PYEOF'
import re
import sys

path = sys.argv[1]
lane_count = int(sys.argv[2])
existing = {}
current_lane = None

try:
    lines = open(path, 'r').read().splitlines()
except FileNotFoundError:
    lines = []

for line in lines:
    stripped = line.strip()
    match = re.match(r'^\[nfc_gate lane(\d+)\]$', stripped)
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
    f.write("# EMU NFC Gate Reader — PN532 I2C Lane Hardware\n")
    f.write("# =============================================================================\n")
    f.write("# Supported documented path:\n")
    f.write("#   one PN532 module per lane MCU / EBB42 board.\n")
    f.write("#\n")
    f.write("# Include after nfc_reader.cfg and nfc_macros.cfg:\n")
    f.write("#   [include nfc/nfc_reader.cfg]\n")
    f.write("#   [include nfc/nfc_macros.cfg]\n")
    f.write("#   [include nfc/nfc_reader_hw.cfg]\n")
    f.write("#\n")
    f.write("# Each [nfc_gate laneN] section maps:\n")
    f.write("#   Happy Hare gate number -> Klipper lane MCU -> NFC reader hardware.\n")
    f.write("#\n")
    f.write("# i2c_mcu: is consumed by Klipper's I2C bus layer (MCU_I2C_from_config),\n")
    f.write("# not read explicitly by the plugin.  The MCU name must already exist in\n")
    f.write("# Klipper / Happy Hare config.  Example: [mcu lane0], [mcu lane1], etc.\n")
    f.write("#\n")
    f.write("# ⚠️ After updating Klipper, rebuild and flash the lane MCU / EBB42 firmware.\n")
    f.write("# Updating the host alone is not enough for PN532 I2C troubleshooting.\n")
    f.write("# =============================================================================\n\n")

    for lane in range(lane_count):
        lane_existing = existing.get(lane, {})
        mcu = lane_existing.get('i2c_mcu', f'mmu{lane}')
        startup_delay = lane_existing.get('startup_poll_delay',
                                          f'{lane * 0.5:.1f}')
        f.write(f"# ── Lane {lane} ────────────────────────────────────────────────────────────────────\n")
        f.write(f"[nfc_gate lane{lane}]\n")
        f.write(f"mmu_gate:                {lane}\n")
        f.write(f"i2c_mcu:                 {mcu}\n")
        f.write(f"startup_poll_delay:      {startup_delay}\n\n")

    example_lane = lane_count
    f.write(f"# ── Lane {example_lane} example ───────────────────────────────────────────────────────\n")
    f.write(f"# [nfc_gate lane{example_lane}]\n")
    f.write(f"# mmu_gate:                {example_lane}\n")
    f.write(f"# i2c_mcu:                 mmu{example_lane}\n")
    f.write(f"# startup_poll_delay:      {example_lane * 0.5:.1f}\n")
PYEOF
}

detect_reader_type() {
    local shared_cfg="$1"
    python3 - "${shared_cfg}" <<'PYEOF'
import re, sys

try:
    text = open(sys.argv[1], 'r').read()
    if re.search(r'^\[nfc_gate shared\]\s*$', text, flags=re.M):
        print('shared')
        raise SystemExit
except FileNotFoundError:
    pass
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
            "5. I2C bus on MCU '${mcu}' for the shared PN532 (e.g. i2c3_PB3_PB4)" \
            "$default_val"
        return
    fi

    echo "5. I2C bus on MCU '${mcu}' for the shared PN532"
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
    read -r -p "   Select I2C bus [0-${#buses[@]}]: " choice

    if [[ "$choice" =~ ^[1-9][0-9]*$ ]] && [ "$choice" -le "${#buses[@]}" ]; then
        eval "${varname}='${buses[$((choice-1))]}'"
    else
        local custom=""
        read -r -p "   Enter I2C bus name [${default_val}]: " custom
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
    f.write("# EMU NFC Gate Reader — Shared PN532 Hardware\n")
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
    f.write("# ⚠️ After updating Klipper, rebuild and flash the MCU hosting the PN532\n")
    f.write(f"#    ({i2c_mcu}).  The MCU must be on the same Klipper version as the host.\n")
    f.write("# =============================================================================\n\n")
    f.write("[nfc_gate shared]\n")
    f.write(f"i2c_mcu:                {i2c_mcu}\n")
    f.write(f"i2c_bus:                {i2c_bus}\n")
    f.write(f"shared:                 true\n")
    f.write(f"startup_polling:        {startup_polling}\n")
    f.write("\n")
    f.write("# HH LED segment used for per-gate effects (exit, entry, status).\n")
    f.write("# NFC builds the effect name as {effect}_{segment}_{gate_index},\n")
    f.write("# e.g. mmu4 + exit → mmu_RFID_read_exit_4.\n")
    f.write("shared_led_segment:     exit\n")
    f.write("\n")
    f.write("# [mmu_led_effect] to flash bright yellow 2x when a tag is read (defined in nfc_macros.cfg).\n")
    f.write("shared_tag_read_effect: mmu_RFID_read\n")
    f.write("\n")
    f.write("# [mmu_led_effect] to flash bright green 2x when the spool ID is ready.\n")
    f.write("shared_spool_ready_effect: mmu_RFID_ready\n")
    f.write("\n")
    f.write("# [mmu_led_effect] to flash bright red 5x when a tag UID does not resolve.\n")
    f.write("shared_tag_unresolved_effect: mmu_RFID_unresolved\n")
    f.write("\n")
    f.write("# [mmu_led_effect] to run a bright yellow chase while Spoolman creates a missing spool.\n")
    f.write("shared_auto_create_effect: mmu_RFID_creating\n")
    f.write("\n")
    f.write("# Seconds a scanned spool stays pending (eligible for the next preload).\n")
    f.write("# shared_pending_timeout: 120.0\n")
    f.write("\n")
    f.write("# Seconds polling may run after NFC_SHARED READ=1 without resolving a tag\n")
    f.write("# before auto-stopping.  No effect for startup_polling or post-PRELOAD_CHECK.\n")
    f.write("# shared_read_timeout: 120.0\n")
    f.write("\n")
    f.write("# Consecutive unresolvable UIDs before the console advises MMU_PRELOAD.\n")
    f.write("# shared_missed_limit: 3\n")
    f.write("\n")
    f.write("# Set to true to block pregate loads entirely when no spool is staged.\n")
    f.write("# force_spool_id: false\n")
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

# ── Sparse checkout — keep docs on remote only ───────────────────────────────
# Always re-applies the exclusion patterns so that new entries added in future
# versions of install.sh take effect on re-runs.  sparse-checkout set is idempotent.
git -C "${REPO_DIR}" sparse-checkout init --no-cone
git -C "${REPO_DIR}" sparse-checkout set --no-cone '/*' '!/docs/' '!/Readme.md' '!/VENDORED.md' '!/NFC Mounting Bracket/' '!/PR.md' '!/README-private.md' '!/.github/'
echo "  [ok]     sparse checkout configured — documentation excluded from this machine"
echo ""

# ── Profile color setup ───────────────────────────────────────────────────────
if [ -n "${_CLI_PROFILE}" ]; then
    apply_profile_color "${_CLI_PROFILE}"
elif [ -t 0 ] && [ -t 1 ]; then
    echo ""
    echo "Terminal profile — pick your theme so highlights render correctly:"
    echo ""
    echo "  macOS Terminal.app"
    echo "   1) Homebrew          → bright cyan"
    echo "   2) Ocean             → bright green"
    echo "   3) Grass             → bright yellow"
    echo "   4) Novel             → dark blue"
    echo "   5) Pro               → bright cyan"
    echo "   6) Basic             → dark blue  [default]"
    echo "   7) Manuscript        → dark blue"
    echo "   8) Red Sands         → bright yellow"
    echo "   9) Silver Aerogel    → dark cyan"
    echo ""
    echo "  iTerm2 / community"
    echo "  10) Default / Dark Background  → bright cyan"
    echo "  11) Solarized Dark             → bright yellow"
    echo "  12) Solarized Light            → dark blue"
    echo "  13) Tango Dark                 → bright cyan"
    echo "  14) Tango Light                → dark blue"
    echo "  15) Dracula                    → bright magenta"
    echo "  16) Monokai                    → bright yellow"
    echo "  17) Gruvbox Dark               → bright yellow"
    echo "  18) Nord                       → bright cyan"
    echo "  19) One Dark                   → bright cyan"
    echo "  20) Catppuccin                 → bright magenta"
    echo ""
    _profile_reply=""
    read -r -p "  Profile number or exact name [default=6]: " _profile_reply
    case "${_profile_reply}" in
        1|Homebrew|homebrew)             _CLI_PROFILE="Homebrew" ;;
        2|Ocean|ocean)                   _CLI_PROFILE="Ocean" ;;
        3|Grass|grass)                   _CLI_PROFILE="Grass" ;;
        4|Novel|novel)                   _CLI_PROFILE="Novel" ;;
        5|Pro|pro)                       _CLI_PROFILE="Pro" ;;
        6|Basic|basic)                   _CLI_PROFILE="Basic" ;;
        7|Manuscript|manuscript)         _CLI_PROFILE="Manuscript" ;;
        8|"Red Sands"|"red sands"|redsands) _CLI_PROFILE="Red Sands" ;;
        9|"Silver Aerogel"|"silver aerogel") _CLI_PROFILE="Silver Aerogel" ;;
        10|Default|default|"Dark Background") _CLI_PROFILE="Default" ;;
        11|"Solarized Dark"|"solarized dark")  _CLI_PROFILE="Solarized Dark" ;;
        12|"Solarized Light"|"solarized light") _CLI_PROFILE="Solarized Light" ;;
        13|"Tango Dark"|"tango dark"|tangodark) _CLI_PROFILE="Tango Dark" ;;
        14|"Tango Light"|"tango light"|tangolight) _CLI_PROFILE="Tango Light" ;;
        15|Dracula|dracula)              _CLI_PROFILE="Dracula" ;;
        16|Monokai|monokai)              _CLI_PROFILE="Monokai" ;;
        17|"Gruvbox Dark"|"gruvbox dark") _CLI_PROFILE="Gruvbox Dark" ;;
        18|Nord|nord)                    _CLI_PROFILE="Nord" ;;
        19|"One Dark"|"one dark")        _CLI_PROFILE="One Dark" ;;
        20|Catppuccin|catppuccin)        _CLI_PROFILE="Catppuccin" ;;
        *)                               _CLI_PROFILE="Basic" ;;   # 6, blank, or unknown
    esac
    apply_profile_color "${_CLI_PROFILE}"
fi

print_banner
echo "Interactive setup"
echo ""

# ── Q1: Reader type ───────────────────────────────────────────────────────────
DEFAULT_READER_TYPE="$(detect_reader_type "${NFC_READER_SHARED_CFG}")"
echo "1. Reader type"
echo "   $(choice_style lane)   = per-lane PN532, one per EBB42 board"
echo "   $(choice_style shared) = single reader inside the MMU body for staging spools"
prompt_choice READER_TYPE \
    "   Select reader type" \
    "${DEFAULT_READER_TYPE}" \
    "lane" "shared"
echo ""

# ── Lane path ─────────────────────────────────────────────────────────────────
if [ "${READER_TYPE}" = "lane" ]; then

    DEFAULT_LANE_COUNT="$(count_lane_sections "${NFC_READER_HW_CFG}")"
    prompt_with_default LANE_COUNT \
        "2. How many NFC readers / lanes are you configuring?" \
        "${DEFAULT_LANE_COUNT}"
    while ! printf '%s' "${LANE_COUNT}" | grep -Eq '^[1-9][0-9]*$'; do
        echo "Please enter a whole number greater than zero."
        prompt_with_default LANE_COUNT \
            "2. How many NFC readers / lanes are you configuring?" \
            "${DEFAULT_LANE_COUNT}"
    done

    echo "3. Spoolman connection"
    echo "   $(choice_style auto)   = read the URL from Moonraker's [spoolman] section (recommended)"
    echo "   $(choice_style direct) = enter a fixed URL such as http://127.0.0.1:7912"
    prompt_choice SPOOLMAN_MODE \
        "   Select Spoolman connection mode" \
        "auto" \
        "auto" "direct"
    SPOOLMAN_URL="auto"
    if [ "${SPOOLMAN_MODE}" = "direct" ]; then
        prompt_with_default SPOOLMAN_URL \
            "   Enter the direct Spoolman URL" \
            "http://127.0.0.1:7912"
    fi

    prompt_yes_no STARTUP_POLLING \
        "4. Start NFC polling automatically on Klipper startup?" \
        "yes"

    prompt_yes_no SCAN_ENABLED \
        "5. Enable scan-jog when a loaded tag is out of read range?" \
        "yes"

    echo "6. Tag read mode"
    echo "   $(choice_style spoolman) = UID-only lookup in Spoolman's extra field (default)"
    echo "   $(choice_style rich)     = read tag metadata, then resolve/create Spoolman records"
    prompt_choice TAG_MODE \
        "   Select tag read mode" \
        "spoolman" \
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
            "7. Will you read factory-tagged Bambu spools with rich metadata?" \
            "no"
        prompt_yes_no SPOOLMAN_AUTO_CREATE \
            "8. Auto-create missing Spoolman spools from rich tag metadata?" \
            "yes"
    fi

    I2C_MCU=""   # not applicable for lane installs

# ── Shared path ───────────────────────────────────────────────────────────────
else

    LANE_COUNT="0"
    SCAN_ENABLED="no"   # always disabled for shared reader

    echo "2. Spoolman connection"
    echo "   $(choice_style auto)   = read the URL from Moonraker's [spoolman] section (recommended)"
    echo "   $(choice_style direct) = enter a fixed URL such as http://127.0.0.1:7912"
    prompt_choice SPOOLMAN_MODE \
        "   Select Spoolman connection mode" \
        "auto" \
        "auto" "direct"
    SPOOLMAN_URL="auto"
    if [ "${SPOOLMAN_MODE}" = "direct" ]; then
        prompt_with_default SPOOLMAN_URL \
            "   Enter the direct Spoolman URL" \
            "http://127.0.0.1:7912"
    fi

    prompt_yes_no STARTUP_POLLING \
        "3. Poll at Klipper boot so you can tap a spool at any time? (recommended)" \
        "yes"

    DEFAULT_I2C_MCU="$(detect_shared_mcu "${NFC_READER_SHARED_CFG}")"
    prompt_with_default I2C_MCU \
        "4. Klipper MCU the shared PN532 is wired to (must match a [mcu ...] section)" \
        "${DEFAULT_I2C_MCU}"

    DEFAULT_I2C_BUS="$(detect_shared_i2c_bus "${NFC_READER_SHARED_CFG}")"
    prompt_i2c_bus_select I2C_BUS "${I2C_MCU}" "${PRINTER_CONFIG}" "${DEFAULT_I2C_BUS}"

    echo "6. Tag read mode"
    echo "   $(choice_style spoolman) = UID-only lookup in Spoolman's extra field (default)"
    echo "   $(choice_style rich)     = read tag metadata, then resolve/create Spoolman records"
    prompt_choice TAG_MODE \
        "   Select tag read mode" \
        "spoolman" \
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
            "7. Will you read factory-tagged Bambu spools with rich metadata?" \
            "no"
        prompt_yes_no SPOOLMAN_AUTO_CREATE \
            "8. Auto-create missing Spoolman spools from rich tag metadata?" \
            "yes"
    fi

fi

# ── Summary + confirm before any writes ──────────────────────────────────────
echo ""
echo "${DEFAULT}${BOLD}════════════════════════════════════════════════════════════════${RESET}"
echo "${DEFAULT}${BOLD}  Install summary — review before writing${RESET}"
echo "${DEFAULT}${BOLD}════════════════════════════════════════════════════════════════${RESET}"
echo "  Reader type:       ${READER_TYPE}"
echo "  Spoolman:          ${SPOOLMAN_URL}"
echo "  Startup polling:   ${STARTUP_POLLING}"
if [ "${READER_TYPE}" = "shared" ]; then
    echo "  i2c_mcu:           ${I2C_MCU}"
    echo "  i2c_bus:           ${I2C_BUS}"
    # Compute per-gate HH effect name from MCU trailing digit (mmu0 → index 1)
    if [[ "${I2C_MCU}" =~ ([0-9]+)$ ]]; then
        _gate_idx="${BASH_REMATCH[1]}"
        _gate_led_idx=$(( _gate_idx + 1 ))
        echo "  LED effects:       ${DEFAULT}mmu_RFID_read_gates_${_gate_led_idx} / mmu_RFID_ready_gates_${_gate_led_idx} / mmu_RFID_unresolved_gates_${_gate_led_idx} / mmu_RFID_creating_gates_${_gate_led_idx}${RESET}"
        echo "                     (gate ${_gate_idx}, via _MMU_SET_LED_EFFECT)"
    else
        echo "  LED effects:       ${DEFAULT}mmu_RFID_read / mmu_RFID_ready / mmu_RFID_unresolved / mmu_RFID_creating${RESET} (all gates — no digit in MCU name)"
    fi
else
    echo "  Lane count:        ${LANE_COUNT}"
    echo "  Scan-jog:          ${SCAN_ENABLED}"
fi
echo "  Tag mode:          ${TAG_MODE}"
if [ "${TAG_MODE}" = "rich" ]; then
    echo "  Bambu reads:       ${BAMBU_READS}"
    echo "  Auto-create:       ${SPOOLMAN_AUTO_CREATE}"
fi
echo ""
echo "  Files that will be written / merged:"
echo "    ${DEFAULT}${NFC_READER_CFG}${RESET}"
echo "    ${DEFAULT}${NFC_CONFIG_DIR}/nfc_macros.cfg${RESET}"
if [ "${READER_TYPE}" = "shared" ]; then
    echo "    ${DEFAULT}${NFC_READER_SHARED_CFG}${RESET}  (settings applied)"
    echo "    ${DEFAULT}${NFC_READER_HW_CFG}${RESET}  (template — ready for lane readers later)"
else
    echo "    ${DEFAULT}${NFC_READER_HW_CFG}${RESET}  (settings applied)"
    echo "    ${DEFAULT}${NFC_READER_SHARED_CFG}${RESET}  (template — ready for shared reader later)"
fi
echo "${DEFAULT}${BOLD}════════════════════════════════════════════════════════════════${RESET}"
echo ""
# Debug confirmation prompt kept here for installer development. Normal installs
# continue after the summary without requiring another prompt.
# read -r -p "  Proceed with install? [y/N]: " _confirm
# case "${_confirm}" in
#     [yY]|[yY][eE][sS]) ;;
#     *)
#         echo "  Install cancelled — no files were written."
#         exit 0
#         ;;
# esac
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
merge_config "${REPO_DIR}/config/nfc_reader_hw.cfg"      "${NFC_READER_HW_CFG}"
merge_config "${REPO_DIR}/config/nfc_reader_shared.cfg"  "${NFC_READER_SHARED_CFG}"

echo ""
echo "Applying selected settings..."

# Settings common to both reader types
set_config_value "${NFC_READER_CFG}" "nfc_gate" "spoolman_url" "${SPOOLMAN_URL}"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "tag_parsing" \
    "$( [ "${TAG_MODE}" = "rich" ] && echo "True" || echo "False" )"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "bambu_reads" \
    "$( [ "${BAMBU_READS}" = "yes" ] && echo "True" || echo "False" )"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "spoolman_auto_create" \
    "$( [ "${SPOOLMAN_AUTO_CREATE}" = "yes" ] && echo "True" || echo "False" )"

if [ "${READER_TYPE}" = "shared" ]; then
    # startup_polling and scan_enabled live in [nfc_gate shared], not [nfc_gate]
    write_shared_config "${NFC_READER_SHARED_CFG}" "${I2C_MCU}" "${I2C_BUS}" \
        "$( [ "${STARTUP_POLLING}" = "yes" ] && echo "1" || echo "0" )"
else
    set_config_value "${NFC_READER_CFG}" "nfc_gate" "startup_polling" \
        "$( [ "${STARTUP_POLLING}" = "yes" ] && echo "1" || echo "-1" )"
    set_config_value "${NFC_READER_CFG}" "nfc_gate" "scan_enabled" \
        "$( [ "${SCAN_ENABLED}" = "yes" ] && echo "True" || echo "False" )"
    write_lane_config "${NFC_READER_HW_CFG}" "${LANE_COUNT}"
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
# Append [update_manager emu_nfc_reader] to moonraker.conf if not already present.
# The section is identical every install so idempotency is a simple grep check.
#
MOONRAKER_CONF="${PRINTER_CONFIG}/moonraker.conf"
MOONRAKER_SECTION="[update_manager emu_nfc_reader]"

if [ ! -f "${MOONRAKER_CONF}" ]; then
    echo "  [skip]   moonraker.conf not found at ${MOONRAKER_CONF} — add update_manager manually"
elif grep -qF "${MOONRAKER_SECTION}" "${MOONRAKER_CONF}"; then
    echo "  [skip]   moonraker.conf already has ${MOONRAKER_SECTION}"
else
    ORIGIN="$(git -C "${REPO_DIR}" remote get-url origin 2>/dev/null || echo 'https://github.com/YOUR_USERNAME/NFC-Reader.git')"
    cat >> "${MOONRAKER_CONF}" <<MOONRAKER

${MOONRAKER_SECTION}
type:             git_repo
path:             ${REPO_DIR}
origin:           ${ORIGIN}
primary_branch:   main
managed_services: klipper
install_script:   install.sh
info_tags:        desc=EMU NFC Gate Reader for Happy Hare
MOONRAKER
    echo "  [added]  ${MOONRAKER_SECTION} → ${MOONRAKER_CONF}"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "${DEFAULT}${BOLD}Install complete.${RESET}"
echo ""
echo "  Selected options:"
echo "    reader type:        ${READER_TYPE}"
if [ "${READER_TYPE}" = "lane" ]; then
    echo "    lanes:              ${LANE_COUNT}"
else
    echo "    i2c_mcu:            ${I2C_MCU}"
    echo "    i2c_bus:            ${I2C_BUS}"
fi
echo "    spoolman_url:       ${SPOOLMAN_URL}"
echo "    startup_polling:    ${STARTUP_POLLING}"
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
echo "    ${KLIPPER_EXTRAS}/nfc_gates    ->  ${REPO_DIR}/klippy/extras/nfc_gates/"
echo ""
echo "  Config files in ${NFC_CONFIG_DIR}/:"
echo "    nfc_reader.cfg          ← Spoolman URL, tag parsing, debug settings"
echo "    nfc_macros.cfg          ← Happy Hare handoff macros"
if [ "${READER_TYPE}" = "shared" ]; then
    echo "    nfc_reader_shared.cfg   ← [nfc_gate shared] hardware config  (settings applied)"
    echo "    nfc_reader_hw.cfg       ← [nfc_gate laneN] hardware layout   (template — not yet active)"
else
    echo "    nfc_reader_hw.cfg       ← [nfc_gate laneN] hardware layout   (settings applied)"
    echo "    nfc_reader_shared.cfg   ← [nfc_gate shared] hardware config  (template — not yet active)"
fi
echo "  To activate the other hardware config later, re-run install.sh with the"
echo "  other reader type selected, or edit the template file and add the include."
echo ""
echo "Next steps (first install only):"
echo ""

if [ "${READER_TYPE}" = "shared" ]; then
    echo "  1. Confirm i2c_mcu and i2c_bus in nfc_reader_shared.cfg match your hardware."
    echo "     The installer wrote i2c_mcu: ${I2C_MCU} and i2c_bus: ${I2C_BUS}."
    echo ""
    echo "  2. Add includes to printer.cfg:"
    echo "       [include nfc/nfc_reader.cfg]"
    echo "       [include nfc/nfc_macros.cfg]"
    echo "       [include nfc/nfc_reader_shared.cfg]"
    echo ""
    echo "  3. Restart Klipper:"
    echo "     sudo systemctl restart klipper"
    echo ""
    echo "  4. Update and flash the MCU hosting the shared PN532 reader (${I2C_MCU})."
    echo "     The MCU must be on the same Klipper version as the host."
    echo ""
    echo "  5. Wire the Happy Hare post-preload hook in mmu_macro_vars.cfg:"
    echo "       variable_user_post_preload_extension: '_NFC_SHARED_PRELOAD'"
    echo "     Polling pauses/resumes automatically on print start/end —"
    echo "     the post-unload hook is no longer needed."
    echo ""
    echo "  6. Moonraker update_manager — added automatically by this script."
    echo "     If moonraker.conf was not found, add [update_manager emu_nfc_reader] manually."
    echo ""
else
    echo "  1. Review ~/printer_data/config/nfc/nfc_reader.cfg"
    echo "     The installer has applied your selected settings."
    echo "     Also review ~/printer_data/config/nfc/nfc_reader_hw.cfg"
    echo "     and confirm each lane's i2c_mcu name matches your Klipper config."
    echo ""
    echo "  2. Add includes to printer.cfg:"
    echo "       [include nfc/nfc_reader.cfg]"
    echo "       [include nfc/nfc_macros.cfg]"
    echo "       [include nfc/nfc_reader_hw.cfg]"
    echo ""
    echo "  3. Restart Klipper:"
    echo "     sudo systemctl restart klipper"
    echo ""
    echo "  4. Update and flash Klipper on each lane MCU / EBB42 board used by NFC."
    echo ""
    echo "  5. Moonraker update_manager — added automatically by this script."
    echo "     If moonraker.conf was not found, add [update_manager emu_nfc_reader] manually."
    echo ""
fi
