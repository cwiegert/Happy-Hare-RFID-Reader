# Message Definitions

[← Commands](klipper-functions.md) | [Shared Reader](shared-reader.md)

This page defines NFC messages that may appear in the Fluidd/Mainsail console
and, when applicable, the matching `nfc_reader.log` entry.

`nfc_reader.log` lines use this format:

```text
YYYY-MM-DD HH:MM:SS.mmm LEVEL    <message>
```

Console prefixes are used consistently. In Fluidd/Mainsail each prefix renders in its own color:

| Prefix | Color | Meaning |
|---|---|---|
| `NFC` (prefix) | ![#4FC3F7](https://placehold.co/15x15/4FC3F7/4FC3F7.png) `#4FC3F7` light blue | Identifies the NFC reader in any message |
| `[CONNECTED]` | plain | Reader object registered with Klipper |
| `[OK]` | ![#90EE90](https://placehold.co/15x15/90EE90/90EE90.png) `#90EE90` light green | Action completed or tag found/read successfully |
| `[WARN]` | ![#FFFF00](https://placehold.co/15x15/FFFF00/FFFF00.png) `#FFFF00` yellow | NFC skipped, ignored, or warned but system recoverable |
| `[ERROR]` | ![#FF6060](https://placehold.co/15x15/FF6060/FF6060.png) `#FF6060` red | Action failed or blocked by a safety/precondition check |
| `[SCAN]` | ![#FFA040](https://placehold.co/15x15/FFA040/FFA040.png) `#FFA040` orange | Scan-jog is starting, moving, or re-polling |
| `[MOVE]` | ![#FFA040](https://placehold.co/15x15/FFA040/FFA040.png) `#FFA040` orange | Scan-jog is moving filament to clear a lane conflict |
| `[REWIND]` | ![#90EE90](https://placehold.co/15x15/90EE90/90EE90.png) `#90EE90` light green | Scan-jog is rewinding or parking after a scan |

Warnings and errors are also forwarded to `klippy.log`. Info/debug records stay
in `nfc_reader.log` only.

Every message that appears on the Klipper console also goes to `nfc_reader.log`
at the corresponding level:

| Console prefix | Log level | `debug:` setting required |
|---|---|---|
| `[ERROR]` | `ERROR` | `debug: 1` (or higher) |
| `[WARN]` | `WARNING` | `debug: 2` (or higher, default) |
| `[OK]`, informational, `[CONNECTED]` | `INFO` | `debug: 3` (or higher) |
| `[SCAN]` started, `[REWIND]`, `[OK]` scan result | `INFO` | `debug: 3` (or higher) |
| `[SCAN]` move steps, position detail | `INFO` | `debug: 3` (or higher) |

When `console_output: true`, logger messages at or above `console_log_level` may
also appear on screen with the same bracketed prefix style; messages logged as
`[laneN]: ...` are normalized to `NFC[laneN]: ...` for the console.

## Common Messages

These apply to both per-lane readers and the shared reader.

| Case | Console message | `nfc_reader.log` |
|---|---|---|
| Reader object connected | `[CONNECTED] NFC Gate [<name>] connected` | `INFO     nfc_gate: [<name>] connected` |
| Manual init OK | `[OK] NFC[<name>]: reader OK` | `INFO     nfc_gate: [<name>] NFC Reader (<reader_type>) OK` |
| Manual init not responding | `[WARN] NFC[<name>]: reader not responding` | `ERROR    nfc_gate: [<name>] NFC Reader (<reader_type>) did not respond - check wiring and I2C address` |
| Manual init exception | `[WARN] NFC[<name>]: init failed: <error>` | `ERROR    nfc_gate: [<name>] init error: <error>` |
| Delayed startup init failed | `[WARN] NFC[<name>]: not ready — check wiring. Run <init command> after fixing.` | `ERROR    nfc_gate: [<name>] NFC Reader (<reader_type>) did not respond ...` or `ERROR    nfc_gate: [<name>] init error: <error>` plus `WARNING  nfc_gate: [<name>] not ready — check wiring. Run <init command> after fixing.` |
| Manual raw scan, no tag | `NFC[<name>]: no tag detected` | `INFO     nfc_gate: [<name>] no tag detected` |
| Manual raw scan, tag found | `NFC[<name>]: UID=<uid> Tg=<target> SENS_RES=0x<value> SAK=0x<value> UIDLen=<n>` | `INFO     nfc_gate: [<name>] UID=<uid> Tg=<target> SENS_RES=0x<value> SAK=0x<value> UIDLen=<n>` |
| Manual polling start | `NFC[<name>]: polling started` | Per-lane: `INFO     nfc_gate: [<name>] gate <n> READ=1 — polling started`. Shared: see shared table. |
| Manual polling stop | `NFC[<name>]: polling stop requested` | Per-lane: `INFO     nfc_gate: [<name>] gate <n> READ=0 — polling stopped`. Shared: see shared table. |
| One manual poll complete | `NFC[<name>]: one poll complete; <status>` | Per-lane: `INFO     nfc_gate: [<name>] one poll complete; <status>`. Shared: see shared table. |
| Per-lane LED test started | `[OK] NFC[<name>]: lane LED test started (<effect>_exit_<gate> cycles=<count>)` | `INFO     nfc_gate: [<name>] lane LED test started effect=<effect>_exit_<gate> cycles=<count>` |
| All-lanes LED chase scheduled | `[OK] NFC: lane LED chase test scheduled for gates <list> (delay=<seconds>s cycles=<count>)` | `INFO     NFC_LED_TEST ALL=1 — scheduled=<list> delay=<seconds>s cycles=<count>` |
| Status command | Per-lane `NFC GATE=<#> STATUS`, global `NFC_STATUS`, shared `NFC_SHARED STATUS=1`, and `SUMMARY=1` print status text. | Console command output only |
| Help command | `NFC_HELP`, `NFC GATE=<#> HELP`, `NFC_SHARED HELP=1`, or low-level debug help prints command help. | Console command output only |
| UID registered to existing spool | `[OK] NFC: UID <uid> assigned to Spoolman spool <spool>; NFC cache cleared. Happy Hare/Fluidd will refresh on their normal Spoolman polling cycle.` | `INFO     NFC_Register: UID <uid> assigned to Spoolman spool <spool>` |
| UID registration failed | `[ERROR] NFC: <reason>` | Same message at `ERROR`; emitted once to the console by the command response. If the Spoolman update succeeded but Happy Hare refresh failed, the refresh failure is logged as `WARNING` |

## Per-Lane Reader Messages

Per-lane readers are the normal EMU lane readers driven by `NFC GATE=<#> ...`
commands and scan-jog.

| Case | Console message | `nfc_reader.log` |
|---|---|---|
| Startup ready with HH seed | `[OK] NFC[laneN]: ready.  HH seed: spool_id=<spool>  Startup polling is enabled; first poll in <delay>s.` | `INFO     nfc_gate: [laneN] NFC Reader (<reader_type>) OK` then `INFO     nfc_gate: [laneN] ready.  HH seed: spool_id=<spool> ...` at `debug: 3` |
| Startup ready, HH reports empty | `[OK] NFC[laneN]: ready.  HH reports gate empty  Run NFC GATE=<#> READ=1 to start polling.` | `INFO     nfc_gate: [laneN] NFC Reader (<reader_type>) OK` then `INFO     nfc_gate: [laneN] ready.  HH reports gate empty ...` at `debug: 3` |
| Manual polling while reader failed | `[WARN] NFC[laneN]: reader failed; run INIT=1 first` | `ERROR    nfc_gate: [laneN] gate <n> READ=1 refused — reader failed; run INIT=1 first` |
| Clear spool cache | `NFC[laneN]: cleared cached spool_id for gate <n>; no NFC_Manager event was dispatched. Next tag read will resolve Spoolman again.` | `INFO     nfc_gate: [laneN] gate <n> — spool cache cleared (uid=<uid> old_spool=<spool>); next read will resolve Spoolman again` |
| Apply with no cached spool | `NFC[laneN]: no cached spool_id to apply; run POLL=1 first` | Console command output only |
| Apply cached spool | `NFC[laneN]: dispatched cached spool_id=<spool> for gate <n> to Happy Hare` | `INFO     nfc_gate: [laneN] gate <n> — manual apply spool=<spool> uid=<uid>` |
| Apply metadata-only tag | `NFC[laneN]: dispatched cached tag metadata for gate <n> to Happy Hare` | `INFO     nfc_gate: [laneN] gate <n> — manual apply metadata uid=<uid>` |
| HH sync with spool | `NFC[laneN]: HH seed → spool_id=<spool>  (next poll matching this spool will not re-dispatch to HH)` | `INFO     nfc_gate: [laneN] gate <n> — HH_SYNC: seed set to spool_id=<spool>` |
| HH sync empty | `NFC[laneN]: HH reports gate empty — seed cleared` | `INFO     nfc_gate: [laneN] gate <n> — HH_SYNC: gate empty/unknown, seed cleared` |
| Poll event detected | No direct console message from Python; configured macros may respond. | `INFO     nfc_gate: [laneN] gate <n> — <event> uid=<uid> spool=<spool>` at `debug: 3` |
| Spool dispatch to Happy Hare | Macro output, if any, comes from the configured `_NFC_SPOOL_CHANGED` macro. | `INFO     nfc_gates: gate <n> → spool <spool> detected (UID <uid>)` |
| Metadata-only dispatch to Happy Hare | Macro output, if any, comes from the configured `_NFC_SPOOL_CHANGED` macro. | `INFO     nfc_gates: gate <n> → tag <uid> metadata-only (material=<material> color=<color> temp=<temp>)` |
| UID has no Spoolman spool | Macro output, if any, comes from the configured `_NFC_TAG_NO_SPOOL` macro. | `INFO     nfc_gates: gate <n> → tag <uid> (no spool ID in Spoolman)` |
| Spool removed dispatch | Macro output, if any, comes from the configured `_NFC_SPOOL_REMOVED` macro. | `INFO     nfc_gates: gate <n> → spool removed (was spool_id=<spool>)` |
| G-code dispatch failed | No direct console message from Python. Klipper may show the macro error. | `ERROR    nfc_gates: GCode dispatch failed for gate <n> event <event>` |
| HH already owns NFC spool | No direct console message. | `INFO     nfc_gate: [laneN] gate <n> — spool confirmed by NFC; HH owns same spool — suspending poll until ejected` |
| Unregistered tag held while filament present | No direct console message. | `INFO     nfc_gate: [laneN] gate <n> — unregistered tag confirmed by NFC; HH reports filament present — suspending poll until ejected` |
| Filament unloaded | No direct console message. | `INFO     nfc_gate: [laneN] gate <n> — filament unloaded; resuming NFC scan` |

## Per-Lane Scan-Jog Messages

Scan-jog messages are per-lane only. They are produced by `NFC GATE=<#>
JOG_SCAN=1` or by the automatic scan-jog trigger.

| Case | Console message | `nfc_reader.log` |
|---|---|---|
| Reader failed | `[ERROR] NFC[laneN]: reader failed - run NFC GATE=<#> INIT=1 first` | `ERROR    [ERROR] NFC[laneN]: reader failed - run NFC GATE=<#> INIT=1 first` |
| Print active | `[WARN] NFC[laneN]: print is active - cannot start scan-jog while printing` | `WARNING  [WARN] NFC[laneN]: print is active - cannot start scan-jog while printing` |
| Happy Hare busy | `[WARN] NFC[laneN]: Happy Hare is busy (action=<action>) — wait for idle before starting scan-jog` | `WARNING  [WARN] NFC[laneN]: Happy Hare is busy (action=<action>) — wait for idle before starting scan-jog` |
| Empty gate manual jog | `[ERROR] NFC[laneN]: jog_scan is not enabled for an empty gate` | Same message at `ERROR`; console mirroring is suppressed so the manual command shows the error once |
| Another gate scanning | `[WARN] NFC[laneN]: gate <n> is already scanning — only one gate may scan at a time` | `WARNING  [WARN] NFC[laneN]: gate <n> is already scanning — only one gate may scan at a time` |
| Same gate already scanning | `[WARN] NFC[laneN]: scan-jog already in progress for this gate` | `WARNING  [WARN] NFC[laneN]: scan-jog already in progress for this gate` |
| Preflight failed | `[WARN] NFC[laneN]: scan-jog not available while <reason>` | `WARNING  [WARN] NFC[laneN]: scan-jog not available while <reason>` |
| Stopped scan-jog started | `[SCAN] NFC[laneN]: stopped scan-jog started for gate <n>` | Same message at `INFO`; debug logs include the stopped-mode chunk/substep/read settings |
| Continuous scan-jog started | `[SCAN] NFC[laneN]: continuous scan-jog started for gate <n>` | Same message at `INFO`; debug logs include the continuous-mode step/speed/accel/poll settings |
| Auto scan-jog waiting | `[SCAN] NFC[<n>]: scan-jog waiting — gate <other> is already scanning` | `INFO     nfc_gate: [laneN] [SCAN] NFC[<n>]: scan-jog waiting — gate <other> is already scanning` |
| Auto scan-jog unavailable | `[WARN] NFC[<n>]: scan-jog not available while <reason>` | `WARNING  nfc_gate: [laneN] NFC[<n>]: scan-jog not available while <reason>` |
| Auto scan-jog started | `[SCAN] NFC[<n>]: starting scan-jog (max=<mm>mm  poll=<seconds>s)` | `WARNING  nfc_gate: [laneN] [SCAN] NFC[<n>]: starting scan-jog (max=<mm>mm  poll=<seconds>s)` |
| Stopped move step queued | `[SCAN] NFC[<n>]: moving <mm>mm  scan position <mm> / <mm>mm` | `INFO     [SCAN] NFC[<n>]: moving <mm>mm  scan position <mm> / <mm>mm` and `INFO     NFC[<n>]: move queued <mm>mm  scan position <mm> / <mm>mm` |
| Continuous move step queued | `[SCAN] NFC[<n>]: continuous <source> <mm>mm  scan position <mm> / <mm>mm` | Same message at `INFO`, followed by `INFO     [<n>]: continuous <source> queued <mm>mm ...`; `<source>` is `Direct Move` for the direct MMU-toolhead path or `MMU_TEST_MOVE` for the G-code fallback |
| Scan poll failed | `[ERROR] NFC[<n>]: scan poll failed` | `ERROR    [ERROR] NFC[<n>]: scan poll failed` |
| Decode retry queued | `[WARN] NFC[<n>]: tag decode incomplete; retry <try>/<max> after <mm>mm jog` | `INFO     [WARN] NFC[<n>]: tag decode incomplete; retry <try>/<max> after <mm>mm jog (uid=<uid> reason=<reason>)` |
| Decode retry exhausted, continue | `[WARN] NFC[<n>]: tag decode still incomplete after <max> retries; continuing scan-jog` | `INFO     [WARN] NFC[<n>]: tag decode still incomplete after <max> retries; continuing scan-jog (uid=<uid>)` |
| Decode retry exhausted, use current result | `[WARN] NFC[<n>]: tag decode still incomplete after <max> retries; using current result` | `INFO     [WARN] NFC[<n>]: tag decode still incomplete after <max> retries; using current result` |
| Max distance after decode retries | `[WARN] NFC[<n>]: scan reached max distance after decode retries; using best incomplete result` | `INFO     [WARN] NFC[<n>]: scan reached max distance after decode retries; using best incomplete result` |
| Left-neighbor interference detected | `[MOVE] NFC[<n>]: uid=<uid> spool_identity=<identity> spool=<spool> belongs to left neighbor gate <gate>; clearance move <try>/<max> to clear neighbor from reader field` | Same message at `INFO` |
| Left-neighbor clearance failed | `[WARN] NFC[<n>]: failed to clear left neighbor gate <gate>; aborting scan to avoid assigning the neighbor spool` | Same message at `INFO` |
| Left-neighbor still interfering | `[ERROR] NFC[<n>]: left lane gate <gate> is interfering with the current lane read after <count> clearance moves (<mm>mm); check reader position, tag placement, or lane spacing` | Same message at `ERROR` |
| Left-neighbor re-poll | `[SCAN] NFC[<n>]: re-polling at position <mm>mm after left lane clearance` | Same message at `INFO` |
| Left-neighbor parking | `[REWIND] NFC[Lane<n>]: parking at gate sensor` | Same message at `INFO` |
| Left-neighbor parking failed | `[WARN] NFC[Lane<n>]: failed to park at gate sensor — move it back manually` | `WARNING  nfc_gate: [laneN] gate <n> scan mode — failed to restore left neighbor gate <gate>: <error>` |
| Tag found | `[OK] NFC[<n>]: tag found` | Same message at `INFO` |
| Rewinding after tag found | `[REWIND] NFC[<n>]: rewinding <mm>mm` | Same message at `INFO` |
| Rewind skipped | `[REWIND] NFC[<n>]: rewind fast move skipped (scan=<mm>mm buffer=<mm>mm)` | Same message at `INFO` |
| Rewind complete | `[REWIND] NFC[<n>]: rewind complete; gate parking handed to Happy Hare (rewound=<mm>mm scan=<mm>mm buffer=<mm>mm)` | Same message at `INFO` |
| Spool assigned | `[OK] NFC[<n>]: spool <spool> assigned` | Same message at `INFO` |
| Metadata assigned | `[OK] NFC[<n>]: tag metadata assigned` | Same message at `INFO` |
| Tag has no Spoolman match | `[WARN] NFC[<n>]: tag has no Spoolman match` | `WARNING  [WARN] NFC[<n>]: tag has no Spoolman match` |
| No tag found | `[REWIND] NFC[<n>]: no tag found; rewinding <mm>mm (scan=<mm>mm buffer=<mm>mm)` | Same message at `INFO` |
| No tag found, rewind skipped | `[REWIND] NFC[<n>]: no tag found; rewind fast move skipped (scan=<mm>mm buffer=<mm>mm)` | Same message at `INFO` |
| Print starts during scan | No direct console message unless a rewind/no-tag message follows. | `WARNING  nfc_gate: [laneN] scan mode: print started — aborting` |

## Shared Reader Messages

Shared reader messages are specific to `[nfc_gate shared]` and `NFC_SHARED`.

| Case | Console message | `nfc_reader.log` |
|---|---|---|
| Startup ready with polling | `[OK] NFC[shared]: ready.  Startup polling is enabled; first poll in <delay>s.` | `INFO     nfc_gate: [shared] NFC Reader (<reader_type>) OK` and `INFO     nfc_gate: [shared] startup polling enabled; first poll in <delay>s` |
| Startup ready, manual polling needed | `[OK] NFC[shared]: ready.  Run NFC_SHARED READ=1 to start polling.` | `INFO     nfc_gate: [shared] NFC Reader (<reader_type>) OK` |
| Startup polling resumed after manual init | `NFC[shared]: startup polling resumed` | `INFO     nfc_gate: [shared] startup polling enabled; first poll in <delay>s` |
| `READ=1` while reader failed | `[WARN] NFC[shared]: reader failed; run INIT=1 first` | `ERROR    nfc_gate: [shared] shared READ=1 refused — reader failed; run INIT=1 first` |
| `READ=1` while printing | `[WARN] NFC[shared]: shared polling not started while printing` | `WARNING  nfc_gate: [shared] shared READ=1 refused — printing` |
| `READ=1` while spool pending | `[WARN] NFC[shared]: spool <spool> is already pending; use NFC_SHARED REPLACE=1 to discard it and scan another, or NFC_SHARED CANCEL=1 to cancel` | `WARNING  nfc_gate: [shared] shared READ=1 refused — spool <spool> already pending` |
| `READ=1` starts polling | `NFC[shared]: polling started` | `INFO     nfc_gate: [shared] shared READ=1 — polling started with <seconds>s read timeout` |
| `READ=0` stops polling | `NFC[shared]: polling stop requested` | `INFO     nfc_gate: [shared] shared READ=0 — polling stopped; pending spool=<spool> kept` |
| Manual scan while printing | `[WARN] NFC[shared]: shared scan skipped while printing` | `WARNING  nfc_gate: [shared] shared scan skipped while printing` |
| Manual poll while printing | `[WARN] NFC[shared]: shared poll skipped while printing` | `WARNING  nfc_gate: [shared] shared poll skipped while printing` |
| Successful tag read | `[OK] NFC[shared]: read tag — spool <spool> staged` | `INFO     nfc_gate: [shared] shared tag resolved — spool=<spool> uid=<uid> auto_created=False pending for <seconds>s` |
| Successful auto-created tag read | `[OK] NFC[shared]: read tag — spool <spool> staged [new spool]` | `INFO     nfc_gate: [shared] shared tag resolved — spool=<spool> uid=<uid> auto_created=True pending for <seconds>s` |
| Level-3 tag detail | No extra console message. | `INFO     nfc_gate: [shared] shared CHANGED — spool=<spool> uid=<uid> auto_created=<bool>; polling stopped, awaiting PRELOAD_CHECK` at `debug: 3` |
| Tag first detected (`debug: 2`) | `NFC[shared]: tag read uid=<uid> — resolving...` | Debug console only; no `nfc_reader.log` entry |
| First unresolved miss (`debug: 2`) | `[WARN] NFC[shared]: uid=<uid> not in Spoolman` | Debug console output and `WARNING  nfc_gate: [shared] uid=<uid> not in Spoolman` |
| Duplicate pending tag | `[WARN] NFC[shared]: spool <spool> is already pending; duplicate tag read ignored` | `INFO     nfc_gate: [shared] shared duplicate tag ignored — spool=<spool> uid=<uid>` |
| Different tag while pending | `[WARN] NFC[shared]: spool <pending> is already pending; read spool <new> uid=<uid> ignored. Run NFC_SHARED REPLACE=1 to discard the pending spool and scan another` | `WARNING  nfc_gate: [shared] shared tag ignored — pending spool=<pending>, new spool=<new> uid=<uid>; use NFC_SHARED REPLACE=1 to replace` |
| Rich tag has no spool ID after limit | `[ERROR] NFC[shared]: uid=<uid> not in Spoolman after <n> attempts` followed by `NFC[shared]: reader ready for next tag`; counter and state reset, polling continues | `ERROR    nfc_gate: [shared] uid=<uid> not in Spoolman after <n> attempts` and `INFO     nfc_gate: [shared] reader ready for next tag` |
| UID not found after limit | `[ERROR] NFC[shared]: uid=<uid> not in Spoolman after <n> attempts` followed by `NFC[shared]: reader ready for next tag`; counter and state reset, polling continues | `ERROR    nfc_gate: [shared] uid=<uid> not in Spoolman after <n> attempts` and `INFO     nfc_gate: [shared] reader ready for next tag` |
| Pending spool at 80% timeout | `[WARN] NFC[shared]: spool <spool> staged — load into gate soon or tap tag again (<seconds>s remaining)` | `WARNING  nfc_gate: [WARN] NFC[shared]: spool <spool> staged — load into gate soon or tap tag again (<seconds>s remaining)` |
| Pending timeout (no resume) | `[ERROR] NFC[shared]: timeout after <seconds>s — no spool was loaded. Tap tag to stage again.` | `ERROR    nfc_gate: [ERROR] NFC[shared]: timeout after <seconds>s — no spool was loaded. Tap tag to stage again.` |
| Pending timeout (polling resumed) | `[ERROR] NFC[shared]: timeout after <seconds>s — no spool was loaded. Reader polling resumed. Tap tag to stage again.` | `ERROR    nfc_gate: [ERROR] NFC[shared]: timeout after <seconds>s — no spool was loaded. Reader polling resumed. Tap tag to stage again.` |
| `PRELOAD_CHECK` while printing | `[WARN] NFC[shared]: PRELOAD_CHECK skipped while printing; pending spool kept` | `INFO     nfc_gate: [shared] PRELOAD_CHECK skipped — printing` |
| `PRELOAD_CHECK` expected spool invalid while printing | `[WARN] NFC[shared]: PRELOAD_CHECK skipped while printing; NEXT_SPOOLID not staged` | `WARNING  nfc_gate: [shared] pending spool <spool> is no longer valid; NEXT_SPOOLID not staged` |
| Expected spool expired or missing | `[WARN] NFC[shared]: pending spool <spool> is no longer valid; NEXT_SPOOLID not staged` or `[WARN] NFC[shared]: pending spool <spool> is no longer valid (expired); NEXT_SPOOLID not staged` | Matching warning in `nfc_reader.log` |
| `PRELOAD_CHECK` no staged spool | `[ERROR] NFC[shared]: no spool staged — tap your spool tag on the shared reader first, or use MMU_PRELOAD to load without spool assignment` | `INFO     nfc_gate: [shared] PRELOAD_CHECK — no pending spool; advising manual preload` |
| `force_spool_id` with no staged spool | `[ERROR] NFC[shared]: force_spool_id is set — tap your spool tag on the shared reader before loading, or disable force_spool_id to allow untagged loads` | `INFO     nfc_gate: [shared] PRELOAD_CHECK — no pending spool; advising manual preload` |
| Spool already assigned | `[WARN] NFC[shared]: spool <spool> already assigned to gate <gate>; clearing stale Happy Hare assignment` | `INFO     nfc_gate: [shared] PRELOAD_CLEAR_ASSIGNED — spool <spool> already assigned to gate <gate>; clearing stale Happy Hare assignment` |
| Spool approved for bridge | `[OK] NFC[shared]: spool <spool> approved — ready for preload commit` | `INFO     nfc_gate: [shared] PRELOAD_CHECK — spool <spool> validated, waiting for PRELOAD_COMMIT` |
| Auto-created spool approved | `[OK] NFC[shared]: spool <spool> approved [new spool synced] — ready for preload commit` | Same as above |
| Spool staged successfully | `[OK] NFC[shared]: spool <spool> loaded — ready for next tag` | `INFO     nfc_gate: [shared] PRELOAD_CHECK complete — pending cleared, polling restarted` |
| `PRELOAD_COMMIT` without approval | `[WARN] NFC[shared]: PRELOAD_COMMIT without approved spool; pending spool kept` | `WARNING  nfc_gate: [shared] PRELOAD_COMMIT without approved spool; pending spool kept` |
| `PRELOAD_COMMIT` spool mismatch | `[WARN] NFC[shared]: PRELOAD_COMMIT spool mismatch (got <spool>, approved <spool>); pending spool kept` | `WARNING  nfc_gate: [shared] PRELOAD_COMMIT spool mismatch (got <spool>, approved <spool>); pending spool kept` |
| Pending spool changed before commit | `[WARN] NFC[shared]: pending spool changed before commit (got <spool>, approved <spool>); pending spool kept` | `WARNING  nfc_gate: [shared] pending spool changed before commit (got <spool>, approved <spool>); pending spool kept` |
| `REPLACE` while reader failed | `[WARN] NFC[shared]: reader failed; run INIT=1 first` | `ERROR    nfc_gate: [shared] shared REPLACE refused — reader failed; run INIT=1 first` |
| `REPLACE` while printing | `[WARN] NFC[shared]: shared polling not started while printing` | `WARNING  nfc_gate: [shared] shared REPLACE refused — printing` |
| `REPLACE` with pending spool | `NFC[shared]: discarded pending spool <spool>; polling restarted` | `INFO     nfc_gate: [shared] shared REPLACE — discarded spool=<spool>; polling restarted with <seconds>s read timeout` |
| `REPLACE` with no pending spool | `NFC[shared]: no pending spool to replace; polling started` | `INFO     nfc_gate: [shared] shared REPLACE — discarded spool=None; polling restarted with <seconds>s read timeout` |
| `RESET=1` | `NFC[shared]: shared reset; LEDs restored and polling restarted` | `INFO     nfc_gate: [shared] shared RESET=1 — cleared spool=<spool>; polling restarted with <seconds>s read timeout` |
| `POLL=1` completes | `NFC[shared]: one poll complete; <status>` | `INFO     nfc_gate: [shared] shared POLL=1 complete — <status>` |
| `CANCEL` | `NFC[shared]: pending spool canceled` | `INFO     nfc_gate: [shared] pending spool canceled` |
| `CLEAR=1` | `NFC[shared]: shared state cleared` | `INFO     nfc_gate: [shared] shared state cleared` |
| `CLEAR_CACHE=1` | `NFC[shared]: shared tag cache cleared; pending spool kept` | `INFO     nfc_gate: [shared] shared tag cache cleared; pending spool=<spool> uid=<uid> kept` |
| `LED_TEST`, no effect configured | `[WARN] NFC[shared]: no LED effect configured` | `WARNING  nfc_gate: [shared] no LED effect configured` |
| LED effect starts | `NFC[shared]: LED effect <effect> started` | `INFO     nfc_gate: [shared] LED effect <effect> started` |
| LED effect fails | `[WARN] NFC[shared]: LED effect <effect> failed` | `WARNING  nfc_gate: [shared] LED effect <effect> failed (mmu_led_effect not defined or HH LED plugin missing): <error>` |

## Low-Level Debug Messages

Low-level PN532 debug output is available only when `low_level_debug: true` is
configured. These messages are command-output probes and are intentionally not
normal workflow events.

| Case | Console message | `nfc_reader.log` |
|---|---|---|
| Low-level debug disabled | `NFC[<name>]: low_level_debug is disabled in config` | Console command output only |
| Reader lacks debug support | `NFC[<name>]: reader does not support low-level debug` | Console command output only |
| Polling paused for debug | `NFC[<name>]: polling paused for low-level PN532 debug` | Console command output only |
| Low-level debug failed | `NFC[<name>]: low-level debug failed: <error>` | Console command output only |
| Raw write | `NFC[<name>]: <op> WRITE before: <hex>` then `NFC[<name>]: <op> WRITE after: OK` | Console command output only |
| Raw read | `NFC[<name>]: <op> READ before: <n> byte(s)` then `NFC[<name>]: <op> READ after: <hex>` | Console command output only |
| Ready result | `NFC[<name>]: READY result: ready (0x01)`, `busy (0x00)`, or `unknown status 0x<value>` | Console command output only |
| ACK result | `NFC[<name>]: ACK result: valid PN532 ACK`, `invalid, expected 00 00 FF 00 FF 00`, or related probe guidance | Console command output only |
| Parsed response | `NFC[<name>]: Firmware parsed: ...`, `SAM response parsed: OK`, or `Passive response parsed header: OK` | Console command output only |
| Next suggested step | `NFC[<name>]: NEXT: NFC GATE=<#> <args>` | Console command output only |
