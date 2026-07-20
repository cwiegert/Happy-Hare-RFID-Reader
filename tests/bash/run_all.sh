#!/usr/bin/env bash
# Runs every tests/bash/test_*.sh file and reports a combined result.
# Usage: bash tests/bash/run_all.sh

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
overall_status=0

for file in "${HERE}"/test_*.sh; do
    [ -e "${file}" ] || continue
    echo "== $(basename "${file}") =="
    if ! bash "${file}"; then
        overall_status=1
    fi
    echo ""
done

exit "${overall_status}"
