#!/usr/bin/env bash
# Regression tests for merge_config() in install.sh — the non-destructive
# merge used for user-owned config files (nfc_reader.cfg, nfc_reader_hw.cfg,
# nfc_reader_shared.cfg). Section headers already present in the user's file
# must never be touched; only sections missing entirely should be appended.
#
# Run directly: bash tests/bash/test_merge_config.sh
# Run via the suite: bash tests/bash/run_all.sh

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/harness.sh
source "${HERE}/lib/harness.sh"
# shellcheck source=lib/extract_functions.sh
source "${HERE}/lib/extract_functions.sh"

source_functions "${REPO_ROOT}/install.sh" merge_config || exit 1

test_missing_dst_is_copied_verbatim() {
    local src="${HARNESS_SANDBOX}/src.cfg" dst="${HARNESS_SANDBOX}/dst.cfg"
    printf '[section_a]\nfoo: 1\n' > "${src}"
    merge_config "${src}" "${dst}" >/dev/null
    assert_eq "$(cat "${src}")" "$(cat "${dst}")" "a nonexistent dst is a plain copy of src"
}

test_existing_section_is_left_untouched() {
    local src="${HARNESS_SANDBOX}/src.cfg" dst="${HARNESS_SANDBOX}/dst.cfg"
    printf '[section_a]\nfoo: 1\n' > "${src}"
    printf '[section_a]\nfoo: USER_CUSTOM_VALUE\n' > "${dst}"
    merge_config "${src}" "${dst}" >/dev/null
    assert_contains "$(cat "${dst}")" "USER_CUSTOM_VALUE" \
        "a section the user already has is never overwritten"
    assert_eq "0" "$(grep -c 'foo: 1' "${dst}")" \
        "the shipped value for an existing section must not appear"
}

test_missing_section_is_appended() {
    local src="${HARNESS_SANDBOX}/src.cfg" dst="${HARNESS_SANDBOX}/dst.cfg"
    printf '[section_a]\nfoo: 1\n\n[section_b]\nbar: 2\n' > "${src}"
    printf '[section_a]\nfoo: USER_CUSTOM_VALUE\n' > "${dst}"
    merge_config "${src}" "${dst}" >/dev/null
    assert_contains "$(cat "${dst}")" "[section_b]" "the new section header is appended"
    assert_contains "$(cat "${dst}")" "bar: 2" "the new section body is appended"
    assert_contains "$(cat "${dst}")" "USER_CUSTOM_VALUE" "the existing section survives the append"
}

test_rerun_after_append_does_not_duplicate() {
    local src="${HARNESS_SANDBOX}/src.cfg" dst="${HARNESS_SANDBOX}/dst.cfg"
    printf '[section_a]\nfoo: 1\n\n[section_b]\nbar: 2\n' > "${src}"
    printf '[section_a]\nfoo: 1\n' > "${dst}"
    merge_config "${src}" "${dst}" >/dev/null
    merge_config "${src}" "${dst}" >/dev/null
    local count
    count="$(grep -c '^\[section_b\]$' "${dst}")"
    assert_eq "1" "${count}" "running merge_config twice must not duplicate an already-appended section"
}

run_test_file "${BASH_SOURCE[0]}"
harness_summary
