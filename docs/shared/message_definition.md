# Message Definitions

[← Commands](klipper-functions.md) | [Shared Reader](shared-reader.md)

This page defines NFC messages that may appear in the Fluidd/Mainsail console
and, when applicable, the matching `nfc_reader.log` entry.

`nfc_reader.log` lines use this format:

```text
YYYY-MM-DD HH:MM:SS.mmm LEVEL    <message>
```

Console prefixes are used consistently:

- `💥` means NFC tried the operation and it failed.
- `⚠️` means NFC skipped, ignored, or warned but kept the system recoverable.
- `⛔` means an action was blocked by a safety/precondition check.
- `[OK]` means the requested action completed.
- `[OK]` means a tag was found/read successfully.
- `🔍` means scan-jog started.
- `⏪` means scan-jog is rewinding.

Warnings and errors are also forwarded to `klippy.log`. Info/debug records stay
in `nfc_reader.log` unless the code explicitly mirrors them to the console.
When `console_output: true`, logger messages at or above `console_log_level` may
also appear on screen prefixed with `NFC:`.

## Common Messages

These apply to both per-lane readers and the shared reader.

| Case | Console message | `nfc_reader.log` |
|---|---|---|
| Reader object connected | `📡 NFC Gate [<name>] connected` | `INFO     nfc_gate: [<name>] connected` |
| Manual init OK | `[OK] NFC[<name>]: reader OK` | `INFO     nfc_gate: [<name>] PN532 reader OK` |
| Manual init not responding | `💥 NFC[<name>]: reader not responding` | `ERROR    nfc_gate: [<name>] PN532 did not respond — check wiring and I2C address (default 0x24)` |
| Manual init exception | `💥 NFC[<name>]: init failed: <error>` | `ERROR    nfc_gate: [<name>] init error: <error>` |
| Delayed startup init failed | `💥 NFC[<name>]: reader not ready — check wiring. Run <init command> after fixing.` | `ERROR    nfc_gate: [<name>] PN532 did not respond — check wiring and I2C address (default 0x24)` or `ERROR    nfc_gate: [<name>] init error: <error>` |
| Manual raw scan, no tag | `NFC[<name>]: no tag detected` | Console command output only |
| Manual raw scan, tag found | `NFC[<name>]: UID=<uid> Tg=<target> SENS_RES=0x<value> SAK=0x<value> UIDLen=<n>` | Console command output only |
| Manual polling start | `NFC[<name>]: polling started` | Per-lane: console command output only. Shared: see shared table. |
| Manual polling stop | `NFC[<name>]: polling stop requested` | Per-lane: console command output only. Shared: see shared table. |
| One manual poll complete | `NFC[<name>]: one poll complete; <status>` | Per-lane: console command output only. Shared: see shared table. |
| Status command | Per-lane `NFC GATE=<#> STATUS`, global `NFC_STATUS`, shared `NFC_SHARED STATUS`, and `SUMMARY=1` print status text. | Console command output only |
| Help command | `NFC_HELP`, `NFC GATE=<#> HELP`, `NFC_SHARED HELP`, or low-level debug help prints command help. | Console command output only |

## Per-Lane Reader Messages

Per-lane readers are the normal EMU lane readers driven by `NFC GATE=<#> ...`
commands and scan-jog.

| Case | Console message | `nfc_reader.log` |
|---|---|---|
| Startup ready with HH seed | `[OK] NFC[laneN]: reader ready.  HH seed: spool_id=<spool>  Startup polling is enabled; first poll in <delay>s.` | `INFO     nfc_gate: [laneN] PN532 reader OK` plus HH seed info |
| Startup ready, HH reports empty | `[OK] NFC[laneN]: reader ready.  HH reports gate empty  Run NFC GATE=<#> READ=1 to start polling.` | `INFO     nfc_gate: [laneN] PN532 reader OK` plus HH seed/empty info |
| Manual polling while reader failed | `💥 NFC[laneN]: reader failed; run INIT=1 first` | `ERROR    nfc_gate: [laneN] gate <n> READ=1 refused — reader failed; run INIT=1 first` |
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
| Reader failed | `💥 NFC[laneN]: reader failed — run NFC GATE=<#> INIT=1 first` | `ERROR    💥 NFC[laneN]: reader failed — run NFC GATE=<#> INIT=1 first` |
| Print active | `⛔ NFC[laneN]: print is active — cannot start scan-jog while printing` | `WARNING  ⛔ NFC[laneN]: print is active — cannot start scan-jog while printing` |
| Happy Hare busy | `⛔ NFC[laneN]: Happy Hare is busy (action=<action>) — wait for idle before starting scan-jog` | `WARNING  ⛔ NFC[laneN]: Happy Hare is busy (action=<action>) — wait for idle before starting scan-jog` |
| Another gate scanning | `⛔ NFC[laneN]: gate <n> is already scanning — only one gate may scan at a time` | `WARNING  ⛔ NFC[laneN]: gate <n> is already scanning — only one gate may scan at a time` |
| Same gate already scanning | `⛔ NFC[laneN]: scan-jog already in progress for this gate` | `WARNING  ⛔ NFC[laneN]: scan-jog already in progress for this gate` |
| Preflight failed | `⛔ NFC[laneN]: scan-jog not available while <reason>` | `WARNING  ⛔ NFC[laneN]: scan-jog not available while <reason>` |
| Scan-jog started | `🔍 NFC[laneN]: scan-jog started for gate <n> (max=<mm>mm  poll=<seconds>s)` | `INFO     nfc_gate: [laneN] gate <n> scan mode started — chunk=<mm>mm max=<mm>mm speed=<mm/s> chunk_interval=<seconds>s dwell=<seconds>s poll=<seconds>s` at `debug: 3` |
| Move step queued | `NFC[<n>]: moving <mm>mm  scan position <mm> / <mm>mm` | `INFO     NFC[<n>]: moving <mm>mm  scan position <mm> / <mm>mm` and `INFO     NFC[<n>]: move queued <mm>mm  scan position <mm> / <mm>mm` |
| Scan poll failed | `💥 NFC[<n>]: scan poll failed` | `ERROR    💥 NFC[<n>]: scan poll failed` |
| Tag found | `[OK] NFC[<n>]: tag found` | `INFO     [OK] NFC[<n>]: tag found` and mirrored to `klippy.log` |
| Rewinding after tag found | `⏪ NFC[<n>]: rewinding <mm>mm` | `INFO     ⏪ NFC[<n>]: rewinding <mm>mm` |
| Spool assigned | `[OK] NFC[<n>]: spool <spool> assigned` | `INFO     [OK] NFC[<n>]: spool <spool> assigned` and mirrored to `klippy.log` |
| Metadata assigned | `[OK] NFC[<n>]: tag metadata assigned` | `INFO     [OK] NFC[<n>]: tag metadata assigned` and mirrored to `klippy.log` |
| Tag has no Spoolman match | `⚠️ NFC[<n>]: tag has no Spoolman match` | `WARNING  ⚠️ NFC[<n>]: tag has no Spoolman match` |
| No tag found | `⚠️ NFC[<n>]: no tag found — ⏪ rewinding <mm>mm` | `WARNING  ⚠️ NFC[<n>]: no tag found — ⏪ rewinding <mm>mm` |
| Print starts during scan | No direct console message unless a rewind/no-tag message follows. | `WARNING  nfc_gate: [laneN] scan mode: print started — aborting` |

## Shared Reader Messages

Shared reader messages are specific to `[nfc_gate shared]` and `NFC_SHARED`.

| Case | Console message | `nfc_reader.log` |
|---|---|---|
| Startup ready with polling | `[OK] NFC[shared]: shared reader ready.  Startup polling is enabled; first poll in <delay>s.` | `INFO     nfc_gate: [shared] PN532 reader OK` and `INFO     nfc_gate: [shared] startup polling enabled; first poll in <delay>s` |
| Startup ready, manual polling needed | `[OK] NFC[shared]: shared reader ready.  Run NFC_SHARED READ=1 to start polling.` | `INFO     nfc_gate: [shared] PN532 reader OK` |
| Startup polling resumed after manual init | `NFC[shared]: startup polling resumed` | `INFO     nfc_gate: [shared] startup polling enabled; first poll in <delay>s` |
| `READ=1` while reader failed | `💥 NFC[shared]: reader failed; run INIT=1 first` | `ERROR    nfc_gate: [shared] shared READ=1 refused — reader failed; run INIT=1 first` |
| `READ=1` while printing | `⚠️ NFC[shared]: shared polling not started while printing` | `WARNING  nfc_gate: [shared] shared READ=1 refused — printing` |
| `READ=1` while spool pending | `⚠️ NFC[shared]: spool <spool> is already pending; use NFC_SHARED REPLACE to discard it and scan another, or NFC_SHARED CANCEL to cancel` | `WARNING  nfc_gate: [shared] shared READ=1 refused — spool <spool> already pending` |
| `READ=1` starts polling | `NFC[shared]: polling started` | `INFO     nfc_gate: [shared] shared READ=1 — polling started with <seconds>s read timeout` |
| `READ=0` stops polling | `NFC[shared]: polling stop requested` | `INFO     nfc_gate: [shared] shared READ=0 — polling stopped; pending spool=<spool> kept` |
| Manual scan while printing | `⚠️ NFC[shared]: shared scan skipped while printing` | `WARNING  nfc_gate: [shared] shared scan skipped while printing` |
| Manual poll while printing | `⚠️ NFC[shared]: shared poll skipped while printing` | `WARNING  nfc_gate: [shared] shared poll skipped while printing` |
| Successful tag read | `[OK] NFC[shared]: spool <spool> detected (UID <uid>) — load spool into gate now` | `INFO     nfc_gate: [shared] shared tag resolved — spool=<spool> uid=<uid> auto_created=False pending for <seconds>s` |
| Successful auto-created tag read | `[OK] NFC[shared]: spool <spool> detected (UID <uid>) [new spool] — load spool into gate now` | `INFO     nfc_gate: [shared] shared tag resolved — spool=<spool> uid=<uid> auto_created=True pending for <seconds>s` |
| Level-3 tag detail | No extra console message. | `INFO     nfc_gate: [shared] shared CHANGED — spool=<spool> uid=<uid> auto_created=<bool>; polling stopped, awaiting PRELOAD_CHECK` at `debug: 3` |
| Duplicate pending tag | `⚠️ NFC[shared]: spool <spool> is already pending; duplicate tag read ignored` | `INFO     nfc_gate: [shared] shared duplicate tag ignored — spool=<spool> uid=<uid>` |
| Different tag while pending | `⚠️ NFC[shared]: spool <pending> is already pending; read spool <new> uid=<uid> ignored. Run NFC_SHARED REPLACE to discard the pending spool and scan another` | `WARNING  nfc_gate: [shared] shared tag ignored — pending spool=<pending>, new spool=<new> uid=<uid>; use NFC_SHARED REPLACE to replace` |
| Rich tag has no spool ID after limit | `⚠️ NFC[shared]: rich tag uid=<uid> has no Spoolman spool ID after <n> attempts — enable spoolman_auto_create or use MMU_PRELOAD to load without spool assignment` | `INFO     nfc_gate: [shared] shared rich tag uid=<uid> — no Spoolman spool ID; enable spoolman_auto_create or register the spool manually (<count>/<limit>)` |
| UID not found after limit | `⚠️ NFC[shared]: tag uid=<uid> not found in Spoolman after <n> attempts — use MMU_PRELOAD to load without spool assignment` | `INFO     nfc_gate: [shared] shared UID-only — tag uid=<uid> not in Spoolman (missed=<count>/<limit>)` |
| Pending timeout | `⚠️ NFC[shared]: pending spool timed out after <seconds>s; tap tag again` | `INFO     nfc_gate: [shared] shared pending spool=<spool> timed out after <seconds>s` |
| Pending timeout with resume | `⚠️ NFC[shared]: pending spool timed out after <seconds>s; tap tag again; polling resumed` | `INFO     nfc_gate: [shared] shared pending timeout — startup polling resumed` |
| `PRELOAD_CHECK` while printing | `⚠️ NFC[shared]: PRELOAD_CHECK skipped while printing; pending spool kept` | `INFO     nfc_gate: [shared] PRELOAD_CHECK skipped — printing` |
| `PRELOAD_CHECK` no staged spool | `⛔ NFC[shared]: no spool staged — tap your spool tag on the shared reader first, or use MMU_PRELOAD to load without spool assignment` | `INFO     nfc_gate: [shared] PRELOAD_CHECK — no pending spool; advising manual preload` |
| `force_spool_id` blocks load | `⛔ NFC[shared]: force_spool_id is set — tap your spool tag on the shared reader before loading, or disable force_spool_id to allow untagged loads` | Same message is raised as a G-code error; no separate logger line currently |
| Spool already assigned | `⚠️ NFC[shared]: spool <spool> is already assigned to a gate — possible duplicate load or stale assignment; no NEXT_SPOOLID staged` | `WARNING  nfc_gate: [shared] PRELOAD_CLEAR_ASSIGNED — spool <spool> already assigned to a gate; possible duplicate load or stale assignment; skipping NEXT_SPOOLID` |
| Spool approved for bridge | `[OK] NFC[shared]: spool <spool> approved — macro will send to Happy Hare` | `INFO     nfc_gate: [shared] PRELOAD_CHECK — spool <spool> validated, macro responsible for MMU_GATE_MAP NEXT_SPOOLID` |
| Auto-created spool approved | `[OK] NFC[shared]: spool <spool> approved [new spool synced] — macro will send to Happy Hare` | Same as above; the macro runs `MMU_SPOOLMAN REFRESH=1 QUIET=1` before `MMU_GATE_MAP NEXT_SPOOLID=<spool>` |
| Spool staged successfully | No extra console message beyond macro command output. | `INFO     nfc_gate: [shared] PRELOAD_CHECK complete — pending cleared, polling restarted` |
| `REPLACE` while reader failed | `💥 NFC[shared]: reader failed; run INIT=1 first` | `ERROR    nfc_gate: [shared] shared REPLACE refused — reader failed; run INIT=1 first` |
| `REPLACE` while printing | `⚠️ NFC[shared]: shared polling not started while printing` | `WARNING  nfc_gate: [shared] shared REPLACE refused — printing` |
| `REPLACE` with pending spool | `NFC[shared]: discarded pending spool <spool>; polling restarted` | `INFO     nfc_gate: [shared] shared REPLACE — discarded spool=<spool>; polling restarted with <seconds>s read timeout` |
| `REPLACE` with no pending spool | `NFC[shared]: no pending spool to replace; polling started` | `INFO     nfc_gate: [shared] shared REPLACE — discarded spool=None; polling restarted with <seconds>s read timeout` |
| `POLL=1` completes | `NFC[shared]: one poll complete; <status>` | `INFO     nfc_gate: [shared] shared POLL=1 complete — <status>` |
| `CANCEL` | `NFC[shared]: pending spool canceled` | `INFO     nfc_gate: [shared] pending spool canceled` |
| `CLEAR=1` | `NFC[shared]: shared state cleared` | `INFO     nfc_gate: [shared] shared state cleared` |
| `CLEAR_CACHE=1` | `NFC[shared]: shared tag cache cleared; pending spool kept` | `INFO     nfc_gate: [shared] shared tag cache cleared; pending spool=<spool> uid=<uid> kept` |
| `LED_TEST`, no effect configured | `⚠️ NFC[shared]: no LED effect configured` | `WARNING  nfc_gate: [shared] no LED effect configured` |
| LED effect starts | `NFC[shared]: LED effect <effect> started` | `INFO     nfc_gate: [shared] LED effect <effect> started` |
| LED effect fails | `⚠️ NFC[shared]: LED effect <effect> failed` | `WARNING  nfc_gate: [shared] LED effect <effect> failed (mmu_led_effect not defined or HH LED plugin missing): <error>` |

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
