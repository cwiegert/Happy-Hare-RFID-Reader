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

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_EXTRAS="${HOME}/klipper/klippy/extras"
PRINTER_CONFIG="${HOME}/printer_data/config"
NFC_CONFIG_DIR="${PRINTER_CONFIG}/nfc"
NFC_READER_CFG="${NFC_CONFIG_DIR}/nfc_reader.cfg"
NFC_READER_HW_CFG="${NFC_CONFIG_DIR}/nfc_reader_hw.cfg"

if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"
    GREEN="$(printf '\033[32m')"
    CYAN="$(printf '\033[1;96m')"
    RESET="$(printf '\033[0m')"
else
    BOLD=""
    GREEN=""
    CYAN=""
    RESET=""
fi

choice_style() {
    case "$1" in
        auto|spoolman)
            printf '%s%s%s%s' "${CYAN}" "${BOLD}" "$1" "${RESET}"
            ;;
        direct|rich)
            printf '%s' "$1"
            ;;
        *)
            printf '%s%s%s' "${BOLD}" "$1" "${RESET}"
            ;;
    esac
}

print_banner() {
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
    echo ""
}

prompt_with_default() {
    local __var_name="$1"
    local prompt_text="$2"
    local default_value="$3"
    local reply
    read -r -p "${prompt_text} [${BOLD}${default_value}${RESET}]: " reply
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
        default_hint="${CYAN}${BOLD}Y${RESET}/n"
    else
        default_hint="y/${CYAN}${BOLD}N${RESET}"
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

print_banner
echo "Interactive setup"
echo ""

DEFAULT_LANE_COUNT="$(count_lane_sections "${NFC_READER_HW_CFG}")"
prompt_with_default LANE_COUNT \
    "1. How many NFC readers / lanes are you configuring?" \
    "${DEFAULT_LANE_COUNT}"
while ! printf '%s' "${LANE_COUNT}" | grep -Eq '^[1-9][0-9]*$'; do
    echo "Please enter a whole number greater than zero."
    prompt_with_default LANE_COUNT \
        "1. How many NFC readers / lanes are you configuring?" \
        "${DEFAULT_LANE_COUNT}"
done

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
    "3. Start NFC polling automatically on Klipper startup?" \
    "yes"

prompt_yes_no SCAN_ENABLED \
    "4. Enable scan-jog when a loaded tag is out of read range?" \
    "yes"

echo "5. Tag read mode"
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
        "6. Will you read factory-tagged Bambu spools with rich metadata?" \
        "no"
    prompt_yes_no SPOOLMAN_AUTO_CREATE \
        "7. Auto-create missing Spoolman spools from rich tag metadata?" \
        "yes"
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

merge_config "${REPO_DIR}/config/nfc_reader.cfg"   "${NFC_READER_CFG}"
merge_config "${REPO_DIR}/config/nfc_macros.cfg" "${NFC_CONFIG_DIR}/nfc_macros.cfg"
merge_config "${REPO_DIR}/config/nfc_reader_hw.cfg"  "${NFC_READER_HW_CFG}"

echo ""
echo "Applying selected settings..."

set_config_value "${NFC_READER_CFG}" "nfc_gate" "spoolman_url" "${SPOOLMAN_URL}"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "startup_polling" \
    "$( [ "${STARTUP_POLLING}" = "yes" ] && echo "1" || echo "-1" )"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "scan_enabled" \
    "$( [ "${SCAN_ENABLED}" = "yes" ] && echo "True" || echo "False" )"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "tag_parsing" \
    "$( [ "${TAG_MODE}" = "rich" ] && echo "True" || echo "False" )"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "bambu_reads" \
    "$( [ "${BAMBU_READS}" = "yes" ] && echo "True" || echo "False" )"
set_config_value "${NFC_READER_CFG}" "nfc_gate" "spoolman_auto_create" \
    "$( [ "${SPOOLMAN_AUTO_CREATE}" = "yes" ] && echo "True" || echo "False" )"

write_lane_config "${NFC_READER_HW_CFG}" "${LANE_COUNT}"

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
echo "Install complete."
echo ""
echo "  Selected options:"
echo "    lanes:              ${LANE_COUNT}"
echo "    spoolman_url:       ${SPOOLMAN_URL}"
echo "    startup_polling:    ${STARTUP_POLLING}"
echo "    scan_jog:           ${SCAN_ENABLED}"
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
echo "    bambu_dependency:   ${BAMBU_READS}"
echo "    spool_auto_create:  ${SPOOLMAN_AUTO_CREATE}"
echo ""
echo "  Python extras (symlinked — auto-updates with git pull):"
echo "    ${KLIPPER_EXTRAS}/nfc_gate.py  ->  ${REPO_DIR}/klippy/extras/nfc_gate.py"
echo "    ${KLIPPER_EXTRAS}/nfc_gates    ->  ${REPO_DIR}/klippy/extras/nfc_gates/"
echo ""
echo "  Config files in ${NFC_CONFIG_DIR}/:"
echo "    nfc_reader.cfg   ← user settings (Spoolman URL, poll interval, debug)"
echo "    nfc_macros.cfg ← Happy Hare handoff macros"
echo "    nfc_reader_hw.cfg  ← hardware layout (one [nfc_gate laneN] per physical reader)"
echo ""
echo "Next steps (first install only):"
echo ""
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
