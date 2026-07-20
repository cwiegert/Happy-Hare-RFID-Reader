#!/usr/bin/env python3
# docsync.py — documentation/changelog impact analyzer for a set of changes.
#
# This repo keeps hand-written docs under docs/ and a detailed CHANGELOG.md in
# lockstep with the code. The hard part of "update the docs" is knowing WHICH
# doc to touch — a new G-code command belongs in klipper-functions.md, a new
# config key in configuration.md, installer behavior in install-uninstall.md,
# and so on. This script reads a diff and does that mapping for you, so a
# future agent starts from a concrete checklist instead of guessing.
#
# It does NOT edit anything. It reports:
#   * changed code files, and which docs already mention each (review targets)
#   * G-code commands added/removed in the diff  -> docs/shared/klipper-functions.md
#   * config keys added/removed in the diff      -> docs/shared/configuration.md
#   * whether CHANGELOG.md [Unreleased] has content when code changed
#
# Usage (run from repo root):
#   python3 .claude/skills/document-changes/docsync.py            # working tree vs HEAD
#   python3 .claude/skills/document-changes/docsync.py main       # this branch vs main (PR view)
#   python3 .claude/skills/document-changes/docsync.py A B        # arbitrary range A..B
#
# Exit status: 0 always for the report itself; 2 if it found code changes with
# an empty CHANGELOG [Unreleased] section (a nudge, easy to override by reading).

import os
import re
import subprocess
import sys

REPO = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                      capture_output=True, text=True).stdout.strip()

# Which doc owns which kind of change. Ordered: first match wins for the summary.
# (topic label, doc path, regexes that mark a code change as this topic)
TOPIC_DOCS = [
    ("G-code command / macro", "docs/shared/klipper-functions.md",
     [r"register_command", r"register_mux_command", r"_NFC_[A-Z_]+"]),
    ("config key", "docs/shared/configuration.md",
     [r"config\.get\w*\('"]),
    ("installer behavior", "docs/shared/install-uninstall.md",
     [r"install\.sh", r"uninstall\.sh"]),
    ("console/log message", "docs/shared/message_definition.md",
     [r"logger\.(info|warning|error)", r"info_both"]),
    ("Spoolman integration", "docs/shared/spoolman-integration.md",
     [r"spoolman", r"SpoolmanClient"]),
    ("shared reader", "docs/shared/shared-reader.md",
     [r"shared_preload", r"nfc_gate shared", r"_shared_"]),
]


def sh(*args):
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True).stdout


def diff_args(argv):
    """Return (name-only args, patch args, human label) for the requested range."""
    if len(argv) == 0:
        return (["diff", "--name-only", "HEAD"], ["diff", "HEAD"],
                "working tree vs HEAD")
    if len(argv) == 1:
        base = argv[0]
        rng = f"{base}...HEAD"
        return (["diff", "--name-only", rng], ["diff", rng],
                f"{base}...HEAD (PR view)")
    rng = f"{argv[0]}..{argv[1]}"
    return (["diff", "--name-only", rng], ["diff", rng], rng)


def added_removed_lines(patch):
    added, removed = [], []
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    return added, removed


def find_names(lines, pattern):
    out = set()
    for ln in lines:
        for m in re.finditer(pattern, ln):
            out.add(m.group(1))
    return out


def docs_mentioning(basename):
    """docs/ files that reference this code file by name (review candidates)."""
    hits = []
    out = sh("grep", "-rl", basename, "docs")
    for p in out.splitlines():
        if p.strip():
            hits.append(p.strip())
    return sorted(hits)


def main():
    name_only, patch_args, label = diff_args(sys.argv[1:])
    changed = [f for f in sh("git", *name_only).splitlines() if f.strip()]
    patch = sh("git", *patch_args)
    added, removed = added_removed_lines(patch)

    code_files = [f for f in changed
                  if not f.startswith("docs/")
                  and f not in ("CHANGELOG.md", "Readme.md", "README.md")
                  and not f.startswith(".claude/")
                  and not f.startswith("tests/")]
    doc_files = [f for f in changed if f.startswith("docs/")]

    print(f"# Documentation impact — {label}\n")

    if not changed:
        print("No changes detected. Nothing to document.")
        return 0

    # --- Topic summary: which docs this change most likely touches ----------
    joined_added = "\n".join(added)
    joined_all = patch
    print("## Docs likely needing an update\n")
    flagged = []
    for topic, doc, patterns in TOPIC_DOCS:
        if any(re.search(p, joined_all) for p in patterns):
            touched = "✔ already edited" if doc in doc_files else "✗ NOT yet edited"
            flagged.append((topic, doc, touched))
            print(f"- **{topic}** → `{doc}`  ({touched})")
    if not flagged:
        print("- (no topic-specific docs matched by pattern — review manually)")

    # Always-review anchors
    print(f"\n- Feature/support table → `Readme.md`  "
          f"({'✔ edited' if any(f in ('Readme.md','README.md') for f in changed) else '✗ NOT edited'})")

    # --- Concrete API surface added/removed ---------------------------------
    cmd_pat = r"register(?:_mux)?_command\(\s*[\"']([A-Z0-9_]+)[\"']"
    key_pat = r"config\.get\w*\(\s*'([a-z0-9_]+)'"
    new_cmds = find_names(added, cmd_pat) - find_names(removed, cmd_pat)
    gone_cmds = find_names(removed, cmd_pat) - find_names(added, cmd_pat)
    new_keys = find_names(added, key_pat) - find_names(removed, key_pat)
    gone_keys = find_names(removed, key_pat) - find_names(added, key_pat)

    if new_cmds or gone_cmds or new_keys or gone_keys:
        print("\n## New / removed public surface (must be documented)\n")
        for c in sorted(new_cmds):
            print(f"- + G-code command `{c}` → document in klipper-functions.md "
                  f"and add to NFC_HELP in nfc_manager.py")
        for c in sorted(gone_cmds):
            print(f"- − G-code command `{c}` removed → delete from klipper-functions.md / NFC_HELP")
        for k in sorted(new_keys):
            print(f"- + config key `{k}` → document in configuration.md (key, default, inheritance)")
        for k in sorted(gone_keys):
            print(f"- − config key `{k}` removed → delete from configuration.md")

    # --- Per-code-file: which docs mention it -------------------------------
    if code_files:
        print("\n## Docs that reference each changed code file\n")
        for f in code_files:
            base = os.path.basename(f)
            mentions = docs_mentioning(base)
            if mentions:
                print(f"- `{f}` is referenced in:")
                for m in mentions:
                    edited = " (edited)" if m in doc_files else ""
                    print(f"    - {m}{edited}")
            else:
                print(f"- `{f}` — not referenced by name in docs/")

    # --- Changelog check ----------------------------------------------------
    print("\n## Changelog\n")
    changelog = os.path.join(REPO, "CHANGELOG.md")
    unreleased_has_content = False
    with open(changelog) as fh:
        text = fh.read()
    m = re.search(r"##\s*\[Unreleased\]\s*(.*?)(?=\n##\s|\Z)", text, re.S)
    if m:
        body = m.group(1).strip()
        unreleased_has_content = bool(re.search(r"^\s*-\s+\S", body, re.M))
    code_changed = bool(code_files)
    if unreleased_has_content:
        print("- `[Unreleased]` has entries. Confirm they cover THIS change.")
    else:
        print("- ⚠ `[Unreleased]` is empty. Add a `### <Theme>` group with "
              "`- <emoji> **Lead-in** — detail` bullets (see SKILL.md).")

    if code_changed and not unreleased_has_content:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
