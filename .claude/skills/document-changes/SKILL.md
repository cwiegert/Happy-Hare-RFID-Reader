---
name: document-changes
description: Update this repo's documentation, write a CHANGELOG entry, and draft a copy-paste GitHub PR description for a change. Use when asked to update docs, document a feature/fix, write release notes or a changelog entry, or produce a PR description / PR body for the EMU NFC Gate Reader.
---

# Document a change (docs + changelog + PR description)

This is an **authoring/workflow skill**, not an app launcher. It captures how
*this* repo keeps its hand-written docs and `CHANGELOG.md` in lockstep with the
code, and how to write a PR description that matches the project's level of
detail. Start every documentation task by running the analyzer — it maps a diff
to the exact docs that need touching so you don't guess.

All paths are relative to the repo root. Everything here runs on stock
`python3` and `git`; no dependencies, no network.

## Run (agent path) — the impact analyzer

`.claude/skills/document-changes/docsync.py` reads a diff and prints: which docs
likely need updating (and whether you've edited them yet), any G-code commands
or config keys the diff adds/removes (which *must* be documented), which docs
reference each changed code file, and whether `CHANGELOG.md [Unreleased]` has
content. It edits nothing.

```bash
# working tree vs HEAD (what you're about to commit)
python3 .claude/skills/document-changes/docsync.py

# this branch vs main — the PR view, use before writing the PR description
python3 .claude/skills/document-changes/docsync.py main
```

Work the checklist it prints top to bottom. A line marked `✗ NOT yet edited`
is a doc the change's content matched but you haven't touched — open it and
confirm whether it actually needs the update (the matcher is deliberately
broad; see Gotchas).

## The doc map (where each kind of change is documented)

Canonical index lives in the `Readme.md` table. The mapping the analyzer
encodes:

| Change | Document |
|---|---|
| New/changed **G-code command or macro** | `docs/shared/klipper-functions.md` **and** the `NFC_HELP` text in `klippy/extras/nfc_gates/nfc_manager.py` |
| New/changed **config key** | `docs/shared/configuration.md` (key, default, inheritance rule) |
| **Installer** behavior (`install.sh`/`uninstall.sh`) | `docs/shared/install-uninstall.md` |
| Runtime **flow / poll loop / dispatch** | `docs/shared/how-it-works.md` |
| **Design rationale / trade-off** | `docs/shared/architecture-decisions.md` |
| **Console output / log line** format | `docs/shared/message_definition.md` |
| **Spoolman** field setup / lookup | `docs/shared/spoolman-integration.md` |
| **Shared-reader** workflow | `docs/shared/shared-reader.md` |
| **Wiring / hardware / setup** | `docs/i2c-nfc/*.md` |
| User-facing **feature/support** claim | `Readme.md` |

`klipper-functions.md` and `configuration.md` are by far the most-edited docs —
most code changes touch one of them. A new command that isn't also added to
`NFC_HELP` in code is a common miss.

## Write the CHANGELOG entry

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/) with a
fixed emoji legend. Add entries under the `## [Unreleased]` heading at the top
(create it if missing), grouped beneath a `### <Theme Title>` subheading. One
theme per logical change; multiple bullets under it.

Legend (use these exact emoji):
`✨ Added · 🐛 Fixed · ♻️ Changed · 📝 Docs · ✅ Verified · 💡 Note`

Bullet shape — **bold lead-in, em-dash, then detail that names the concrete
symbol** (function, file, macro, config key). This specificity is the house
style; a vague bullet is wrong here:

```markdown
## [Unreleased]

### One-Time Installer and Moonraker Web Updates

- ✨ **Added `-r` / `--repair`** — repair restores installer-owned Python and
  macro links, ordered printer includes, the Moonraker updater, and the install
  state marker while preserving NFC reader and hardware settings.
- ♻️ **`nfc_macros.cfg` is read-only** — the protected Happy Hare interface is
  linked to the shipped file and existing local copies are backed up once.
```

On **release** (not every change), the `[Unreleased]` heading becomes a version
line in the same style as prior entries:
`## [X.Y.Z] - MM/DD/YYYY - <Author>` (e.g. `## [1.3.1] - 07/17/2026 - WoodWorker`).
Bump the version per semver: fixes → patch, new user-facing capability → minor.

## Write a PR description

When asked for a PR description, output a **single fenced ```markdown block** the
user copies straight into the GitHub PR body — nothing else needs editing. Match
the changelog's altitude: name concrete symbols, group by theme, state why, and
list what you verified. Right-sized means **thorough but scannable** — a reader
should grasp scope in the summary and find specifics in the sections. Omit
sections that don't apply (don't pad).

Template:

````markdown
```markdown
## Summary

<1–3 sentences: what this PR does and why it exists.>

## What changed

### <Theme 1>
- **<concrete symbol / file>** — <what changed and the behavior effect>.
- ...

### <Theme 2>
- ...

## Why

<The problem or regression this addresses. Link issues with #NN if any.>

## Testing / verification

- <command you ran> → <result>. e.g. `bash tests/test_installer_repair.sh` → PASS
- <manual check performed, or "on-printer: not run — no hardware in CI">

## Docs & changelog

- [ ] `CHANGELOG.md` `[Unreleased]` updated
- [ ] Docs updated: <list files, or "none needed — internal only">
- [ ] New G-code commands added to `NFC_HELP` (if any)
```
````

Filled example (from this repo's current installer work — real, and its test
command was run):

````markdown
```markdown
## Summary

Make the interactive installer a one-time setup and route ongoing updates
through Moonraker's web UI, and protect `nfc_macros.cfg` as a read-only link to
the shipped file.

## What changed

### Installer modes
- **`install.sh`** — a completed install now exits instead of re-running the
  wizard; added `-r`/`--repair` (restores installer-owned links, ordered
  includes, Moonraker updater, `.install-state`) and `-c`/`--reconfigure`
  (backs up NFC config, reruns the wizard).
- **`nfc_macros.cfg`** — installed as a read-only symlink into the repo
  `config/`; any pre-existing regular file is moved to
  `nfc_macros.cfg.pre-read-only-<timestamp>`.

### Updates
- Removed the deprecated `install_script` Moonraker updater hook; Moonraker
  updates the checkout and restarts Klipper directly.

## Why

Re-running the wizard on every update risked overwriting user config, and the
`install_script` hook is deprecated upstream.

## Testing / verification

- `bash tests/test_installer_repair.sh` → `installer repair test: PASS`
  (fresh install, repair, reconfigure, include order, updater rewrite).
- On-printer smoke: not run — no Klipper/hardware in this environment.

## Docs & changelog

- [x] `CHANGELOG.md` `[Unreleased]` updated
- [x] Docs updated: install-uninstall, configuration, how-it-works,
  architecture-decisions, klipper-functions, i2c-nfc/troubleshooting
- [x] No new G-code commands
```
````

`gh` is **not installed here**, so don't try to open the PR — hand the user the
block to paste. If they later install `gh`, `gh pr create --body-file <file>`
works, but the copy-paste block is the deliverable they asked for.

## Gotchas

- **The analyzer's topic matcher is broad on purpose.** It flags a doc if the
  diff merely *mentions* a topic's keywords, so `spoolman-integration.md` or
  `shared-reader.md` can show `✗ NOT yet edited` for a change that only touched
  them incidentally. Treat every flag as "open and confirm," not "must edit."
- **Config-key detection has false positives.** `config.get('nfc_gate')` and
  `config.get('register_sensor')` are object lookups, not user config keys, but
  match the `config.get('…')` pattern. Sanity-check a flagged key against
  `configuration.md` before documenting it.
- **A new G-code command has two homes, not one.** `klipper-functions.md` *and*
  the `NFC_HELP` string in `nfc_manager.py`. The analyzer reminds you, but the
  `NFC_HELP` half is the one people forget.
- **Doc directory layout was reorganized.** git history shows old paths
  (`docs/design/`, `docs/i2c-pn532/`, `docs/spi-rc522/`) that no longer exist —
  the current tree is `docs/shared/` + `docs/i2c-nfc/` only. Don't recreate a
  path just because an old commit referenced it; check `find docs -type f`.
- **CHANGELOG bullets name the code.** A reviewer should be able to `grep` the
  symbol named in a bullet and land on the change. "Fixed a bug in the
  installer" is below this repo's bar; "`_insert_nfc_endstop_after_lane()` now
  also stops at the first blank line" is the bar.

## Troubleshooting

- `docsync.py` prints nothing under a topic → the diff didn't match that
  topic's patterns; use the per-file "Docs that reference each changed code
  file" section and the doc map table instead.
- Analyzer exits `2` → it found code changes with an empty `[Unreleased]`. Add
  the changelog entry, or ignore the exit code if the change is docs-only.
- `fatal: bad revision 'main...HEAD'` → you're on `main` or `main` is behind;
  run with no argument (working-tree mode) or pass an explicit base like
  `origin/main` or a commit SHA.
