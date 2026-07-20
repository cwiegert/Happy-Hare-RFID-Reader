#!/usr/bin/env bash
# Regression tests for install_managed_macros() in install.sh — the function
# that turns nfc_macros.cfg into a read-only symlink to the repo's shipped
# copy. See docs/shared/architecture-decisions.md ("Configuration Is
# User-Owned; Shipped Macros Are Read-Only") for why this exists.
#
# Run directly: bash tests/bash/test_install_managed_macros.sh
# Run via the suite: bash tests/bash/run_all.sh

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/harness.sh
source "${HERE}/lib/harness.sh"
# shellcheck source=lib/extract_functions.sh
source "${HERE}/lib/extract_functions.sh"

source_functions "${REPO_ROOT}/install.sh" next_available_path install_managed_macros || exit 1

# Sets REPO_DIR / NFC_MACROS_CFG for a scenario and creates the shipped
# source file. Each test_* runs in HARNESS_SANDBOX, a fresh dir per test.
_setup_repo_and_target() {
    mkdir -p "${HARNESS_SANDBOX}/repo/config" "${HARNESS_SANDBOX}/printer_config/nfc"
    echo "# shipped macros" > "${HARNESS_SANDBOX}/repo/config/nfc_macros.cfg"
    REPO_DIR="${HARNESS_SANDBOX}/repo"
    NFC_MACROS_CFG="${HARNESS_SANDBOX}/printer_config/nfc/nfc_macros.cfg"
}

test_fresh_install_creates_symlink() {
    _setup_repo_and_target
    install_managed_macros >/dev/null
    assert_symlink "${NFC_MACROS_CFG}" "${REPO_DIR}/config/nfc_macros.cfg" \
        "fresh install links nfc_macros.cfg to the repo source"
}

test_rerun_is_idempotent() {
    _setup_repo_and_target
    install_managed_macros >/dev/null
    local before after
    before="$(readlink "${NFC_MACROS_CFG}")"
    install_managed_macros >/dev/null
    after="$(readlink "${NFC_MACROS_CFG}")"
    assert_eq "${before}" "${after}" "re-running with the correct link already in place is a no-op"
}

test_existing_user_file_is_backed_up_then_linked() {
    _setup_repo_and_target
    echo "# user's own macro edits" > "${NFC_MACROS_CFG}"
    install_managed_macros >/dev/null
    assert_symlink "${NFC_MACROS_CFG}" "${REPO_DIR}/config/nfc_macros.cfg" \
        "a plain existing file is replaced by the managed symlink"

    local backups
    backups="$(find "${HARNESS_SANDBOX}/printer_config/nfc" -maxdepth 1 -name 'nfc_macros.cfg.pre-managed-*')"
    assert_ne "" "${backups}" "a pre-managed backup file was created"
    if [ -n "${backups}" ]; then
        assert_contains "$(cat "${backups}")" "user's own macro edits" \
            "the backup preserves the pre-migration content"
    fi
}

test_stale_symlink_is_replaced_without_backup() {
    _setup_repo_and_target
    ln -s "/some/old/repo/config/nfc_macros.cfg" "${NFC_MACROS_CFG}"
    install_managed_macros >/dev/null
    assert_symlink "${NFC_MACROS_CFG}" "${REPO_DIR}/config/nfc_macros.cfg" \
        "a symlink pointing at a different (old) repo path is repointed"

    local backups
    backups="$(find "${HARNESS_SANDBOX}/printer_config/nfc" -maxdepth 1 -name 'nfc_macros.cfg.pre-managed-*')"
    assert_eq "" "${backups}" "no backup is made when replacing a symlink — there is no user content to lose"
}

test_missing_repo_source_is_a_hard_error() {
    _setup_repo_and_target
    rm "${REPO_DIR}/config/nfc_macros.cfg"
    local output status
    output="$(install_managed_macros 2>&1)"
    status=$?
    assert_ne "0" "${status}" "a missing repo source file must fail the install, not silently continue"
    assert_contains "${output}" "ERROR" "the failure message says why"
}

run_test_file "${BASH_SOURCE[0]}"
harness_summary
