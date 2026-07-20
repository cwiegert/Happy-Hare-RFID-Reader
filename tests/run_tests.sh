#!/usr/bin/env bash
# Runs the full test suite: bash install-script tests + Python unit tests.
# Usage: bash tests/run_tests.sh
#
# Python tests require pytest: pip install -r tests/python/requirements-dev.txt

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
overall_status=0

echo "### bash tests (install.sh / uninstall.sh) ###"
if ! bash "${HERE}/bash/run_all.sh"; then
    overall_status=1
fi

echo ""
echo "### python tests (klippy/extras/nfc_gates) ###"
if command -v pytest >/dev/null 2>&1; then
    if ! pytest "${HERE}/python"; then
        overall_status=1
    fi
elif python3 -m pytest --version >/dev/null 2>&1; then
    if ! python3 -m pytest "${HERE}/python"; then
        overall_status=1
    fi
else
    echo "pytest not found — install it with:"
    echo "  pip install -r ${HERE}/python/requirements-dev.txt"
    overall_status=1
fi

exit "${overall_status}"
