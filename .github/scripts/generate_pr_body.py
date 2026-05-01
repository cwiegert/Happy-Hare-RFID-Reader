#!/usr/bin/env python3
"""Generate the PR body for a lameandboard/rfid vendor sync PR.

Reads from environment variables set by the GitHub Action:
  SHORT_SHA      - upstream commit short SHA
  TESTS_PASSED   - 'true' or 'false'
  TEST_OUTPUT    - last 30 lines of pytest output
  NEEDS_REVIEW   - first checklist item text

Writes the body to /tmp/pr_body.md.
"""
import os

short_sha    = os.environ['SHORT_SHA']
tests_passed = os.environ.get('TESTS_PASSED', 'false') == 'true'
test_output  = os.environ.get('TEST_OUTPUT', '(no output)')
needs_review = os.environ.get('NEEDS_REVIEW', 'review adapter code in nfc_manager.py')

if tests_passed:
    test_summary = '✅ Contract regression tests passed — adapter API is compatible.'
else:
    test_summary = '❌ **Contract regression tests FAILED** — upstream changes break the adapter API. Review `nfc_manager.py` before merging.'

body = f"""\
Automated sync of vendored files from \
[lameandboard/rfid](https://github.com/lameandboard/rfid) @ `{short_sha}`.

## Regression test result

{test_summary}

<details>
<summary>Test output</summary>

```
{test_output}
```

</details>

## Changed files
- `klippy/extras/nfc_gates/vendor/rfid_tag_parser.py`
- `klippy/extras/nfc_gates/vendor/lameandboard_spoolman.py`

## Review checklist
- [ ] {needs_review}
- [ ] Check if `spoolman_client.py` changes affect adapter call sites in `nfc_manager.py`
- [ ] Check if `rfid_tag_parser.py` changes affect `parse_tag()` return format
- [ ] Confirm `auto_create_spool()` is still present but unused in `lameandboard_spoolman.py`
"""

with open('/tmp/pr_body.md', 'w') as f:
    f.write(body)

print("PR body written to /tmp/pr_body.md")
