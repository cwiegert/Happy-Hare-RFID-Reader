# Architecture Decisions

[← Back to README](../../Readme.md)

---

This document captures the design decisions made while building the PN532 I2C path, including the reasoning and trade-offs behind each choice. It is meant to explain *why* the system is structured the way it is — so that future changes can be made with the context that drove the original decisions.

---

## Decision: Strict Layer Ownership

**What we decided:** Each layer owns exactly one responsibility and may not reach across its boundary into another layer's domain.

| Layer | Owns | Explicitly does not own |
|---|---|---|
| `PN532Driver` | PN532 wire protocol, I2C frames, UID extraction | Spoolman, gate policy, Happy Hare commands |
| `SpoolmanClient` | UID → spool record lookup and cache | Gate state, lane assignment, MMU commands |
| `NFC_Manager` | Gate state machine, changed/removed decisions, macro dispatch | PN532 protocol details |
| `nfc_macros.cfg` | Happy Hare-facing GCode calls | NFC bus reads, Spoolman HTTP requests |

**Why:** The alternative was a flat design where one module reads the tag, queries Spoolman, and calls `MMU_GATE_MAP`. We rejected this because it creates a single module that breaks for three independent reasons: hardware issues, Spoolman API changes, and Happy Hare version differences. Each layer now has a single reason to change.

**The boundary that matters most:** `SpoolmanClient` never calls Happy Hare. `PN532Driver` never calls Spoolman. NFC_Manager is the only module that sees both a UID and a gate assignment, and it dispatches a macro rather than calling Happy Hare directly.

---

## Decision: Tags Are Read-Only — UID Lookup Only

**What we decided:** The system reads the factory UID only. Tags are never written to. The UID is stored as a Spoolman extra field on the spool record.

**Why we rejected writing tags:** Writing tag payload data would require:
- A tag format standard (which format? which pages? how versioned?)
- A tag programming workflow for users
- Handling format mismatches on old tags after upgrades
- Dealing with write failures and partially-written tags

The UID approach requires none of this. Any blank NFC sticker from any supplier works. The spool association is managed entirely in Spoolman. If you need to associate a different spool with a tag, you update one extra field in Spoolman — the tag is unchanged.

**Trade-off accepted:** The tag must be registered in Spoolman before it can resolve to a spool. An unregistered tag produces `_NFC_TAG_NO_SPOOL` instead of silently failing.

---

## Decision: SpoolmanClient Is a Pure Lookup Client

**What we decided:** SpoolmanClient resolves UID → spool record and caches results. It does not PATCH Spoolman location, and it does not call `MMU_GATE_MAP`, `MMU_SPOOLMAN`, or any Happy Hare command.

**Why:** The gate a spool belongs to is a *physical fact*, not a Spoolman record fact. Spoolman knows spool IDs and filament metadata. It does not know the MMU gate layout. The thing that knows both a spool ID and a gate number is NFC_Manager — and that is exactly where the gate assignment decision lives.

Keeping Happy Hare commands and gate-aware Spoolman writes out of SpoolmanClient means it can still be used from any context where you have a UID and need a spool ID. Happy Hare owns gate map state and synchronizes that state to Spoolman via `MMU_SPOOLMAN SYNC=1 QUIET=1` in `nfc_macros.cfg`.

---

## Decision: Happy Hare Calls Are Behind `_NFC_*` Macros

**What we decided:** NFC_Manager dispatches three GCode macros. Those macros call `MMU_GATE_MAP` for Happy Hare runtime state and pass `SYNC=1` when we want Happy Hare to synchronize the gate map to Spoolman. NFC_Manager does not call Happy Hare commands directly.

```
NFC_Manager → _NFC_SPOOL_CHANGED GATE=n SPOOL_ID=id UID=uid
           → _NFC_SPOOL_REMOVED  GATE=n
           → _NFC_TAG_NO_SPOOL   GATE=n UID=uid

nfc_macros.cfg → MMU_GATE_MAP GATE={gate} SPOOLID={spool_id} AVAILABLE=1 SYNC=1 QUIET=1
```

**Why:** Happy Hare's GCode API evolves between versions. The exact command for "tell Happy Hare about this spool" belongs in editable GCode config, not Python. The default macro is designed for Happy Hare `spoolman_support: push`: `MMU_GATE_MAP` sets the local runtime gate map and `SYNC=1` lets Happy Hare synchronize that local map to Spoolman. NFC_Manager already knows the physical gate that produced the read, so it passes both `GATE` and `SPOOL_ID` into the macro boundary.

This also makes the integration boundary visible. Anyone debugging a Happy Hare integration problem knows to look in `nfc_macros.cfg` — not in `pn532_driver.py`.

**Corollary:** If a Happy Hare version requires a different synchronization command, adjust `nfc_macros.cfg`. Do not add it to `PN532Driver` or `SpoolmanClient`.

**Exception — scan-jog's motion path:** `scan_jog.py` calls Happy Hare's Python
motion API directly in several places (`mmu.move_filament()`,
`mmu.select_gate()`, `mmu._initialize_filament_position()`/
`initialize_filament_position()`), rather than going exclusively through
gcode macros. Motion needs return values on the same call — actual distance
homed, whether the move triggered an endstop — that a fire-and-forget gcode
macro dispatch can't hand back cleanly, and gate selection specifically needs
to avoid `cmd_MMU_SELECT`'s own unconditional console print (see
`select_gate_quiet()`). Every direct call has a gcode fallback
(`_MMU_STEP_HOMING_MOVE`, `_MMU_STEP_MOVE`, `MMU_SELECT`) for Happy Hare
installs that don't expose the Python method, so the version-fragility this
decision is meant to avoid is still contained — just pushed into a
`hasattr()` check in `scan_jog.py` instead of a macro file.

---

## Decision: Removal Is Debounced

**What we decided:** A single missed read does not declare a spool removed. The gate must be absent for `poll_interval × absent_threshold` seconds before `_NFC_SPOOL_REMOVED` fires.

Default:
```ini
poll_interval:    30   # seconds
absent_threshold: 3    # consecutive missed reads
# ≈ 90 seconds before removal fires
```

**Why:** NFC reads can miss for reasons that have nothing to do with spool removal: tag angle, distance, vibration, RF interference from steppers, or a single delayed I2C transaction. If removal fired on every miss, a spool sitting in a gate would trigger constant remove/detect cycles during a print.

90 seconds of absence is unambiguous. If a spool is physically gone, 90 seconds is fast enough to be useful. If it was a transient read miss, it recovers on the next successful poll.

---

## Decision: Per-Lane I2C Bus (One PN532 Per EBB42)

**What we decided:** Each per-lane gate reader has one PN532, wired to the I2C
bus on that lane's EBB42. There is no shared I2C bus across the lane readers.

**Why we chose this over a shared bus / multiplexer:**
- No I2C address management. Every PN532 can stay at `0x24` because they are on separate buses.
- No multiplexer hardware (`TCA9548A` or similar) to buy, wire, or configure.
- Failures are isolated. A PN532 that holds SDA low only affects one lane.
- The lane MCU already exists as part of the Happy Hare hardware setup. Its I2C bus is available with no extra wiring effort.

**Trade-off accepted:** The per-lane reader design only works when each lane has
its own MCU. A printer with a single MCU for all lane readers would need a
different approach (multiplexer or shared bus with unique addresses).

**Shared-reader exception:** The optional shared reader is not part of the
per-lane reader set. It is a single PN532 mounted inside the MMU body and wired
to whichever MCU hosts that I2C bus. It stages `NEXT_SPOOLID` for Happy Hare
instead of assigning a physical lane directly. See
[Shared Reader](shared-reader.md).

---

## Decision: NFC_Manager Owns the Baseline Startup Policy

**What we decided:** On startup, NFC does not blindly overwrite Happy Hare's gate map with whatever the readers see at boot time.

**Why:** Happy Hare already starts with a known gate/filament map from its own config and persisted state. Tags may not be physically near readers at startup. A spool may be in a gate but temporarily unreadable. If NFC declared every gate empty on boot and then started filling them in as reads succeed, it would create a noisy burst of `MMU_GATE_MAP ... SYNC=1` updates during Klipper startup.

**Current implication:** NFC_Manager treats reads as events (state changes), not as complete authority over the gate map. It updates Happy Hare when something changes, not when it confirms that the gate is in the state it was already in.

**Future work:** A proper baseline sync — querying Happy Hare state at startup and comparing it with what the readers see — is the right long-term approach. This would let NFC detect discrepancies between persisted state and physical reality after a power cycle.

---

## Decision: Expert I2C Debug Commands Are Hidden by Default

**What we decided:** The low-level PN532 bus commands (`STEP=`, `RAW_READ`, `RAW_WRITE`, etc.) are not available unless `low_level_debug: True` is set explicitly.

**Why:** Raw bus commands can disturb the PN532 state machine. Sending `PASSIVE_WRITE` without following up with `PASSIVE_ACK` and `PASSIVE_RESPONSE` leaves the PN532 waiting for a read that never comes, which can cause subsequent normal polls to fail until the reader is reinitialized. These commands are useful for bring-up diagnostics but would be confusing and dangerous in normal operation.

Gating them behind a config flag means they are discoverable for bring-up but cannot be called accidentally.

---

## Decision: Python Extras Are Symlinked, Not Copied

**What we decided:** `install.sh` creates symlinks from Klipper's extras directory into the repo clone. It does not copy files.

**Why:** If files were copied, every update would require running `install.sh` to copy the new versions. With symlinks, `git pull` in the repo directory is sufficient — Klipper reads the updated Python files directly from the repo at next restart. The Moonraker update manager calls `install.sh` after a `git pull`, which updates the symlinks if they ever change targets. But for normal code-only updates, the symlinks just keep working.

**Implication:** The repo clone must remain present on the Pi. If the repo is deleted, the symlinks break and Klipper will fail to start.

---

## Decision: Configuration Is User-Owned; Shipped Macros Are Read-Only

**What we decided:** `install.sh` never overwrites a user-owned config file section that is already configured. On subsequent runs, it only appends sections that are missing. `nfc_macros.cfg` is intentionally different: the installed file is read-only.

**Why:** Reader and hardware files in `~/printer_data/config/nfc/` contain machine-specific choices and must preserve local customization. Keeping the macro file read-only ensures fixes arrive with updates and prevents installed copies from drifting.

The merge strategy (copy-if-absent, append-missing-sections) still applies to user-owned files. During the one-time macro migration, an existing regular `nfc_macros.cfg` is moved to a timestamped `nfc_macros.cfg.pre-managed-*` backup before the read-only link is created. Later installer runs verify and maintain the link. Users who need changes should make and include their own copy.

---

## Decision: NFC Tag Detection Is a Klipper Homing Endstop, Not a Poll Loop

**What we decided:** Per-lane scan-jog's forward search does not jog a fixed
distance and then poll the reader to see if it got lucky. `mmu_nfc_endstop.py`
registers the lane's existing NFC reader as a real Klipper/Happy Hare
gear-rail endstop (`ENDSTOP=nfc_lane<N>`), and the forward search is a genuine
homing move against it — Klipper's own homing machinery stops the move the
instant the endstop trips, using the same mechanism a physical limit switch
would.

**Why:** A jog-then-poll loop only ever knows the tag was "somewhere in the
last chunk" — the actual position is an estimate reconstructed from move
timing and speed/accel profiles after the fact (see
`homing_distance_from_elapsed`/`corrected_homing_actual` in `scan_jog.py`,
still used as a fallback when the direct Happy Hare motion API isn't
available). A homing move reports the real, physically-measured trigger
position directly from Klipper's stepper/endstop code — the same accuracy
guarantee any other homed axis gets. Continuous mode's UID hit-window
recentering before rich tag parsing exists specifically to correct for the
residual uncertainty of *where within the tag's read range* it triggered, not
to compensate for chunk-boundary guessing.

**How it works:** `mmu_nfc_endstop.py` implements Klipper's standard MCU
endstop interface (`home_start`/`home_wait`/`query_endstop`/`get_steppers`).
It does not add any new hardware or wiring — `home_start()` just starts a
reactor timer that polls the *same* reader object the matching `[nfc_gate
laneN]` section already owns, at `poll_interval` (default `0.05s`), and calls
back into Klipper's homing completion the moment a read succeeds.

**Trade-off accepted:** The endstop's trigger latency is bounded by
`poll_interval`, not by hardware response time like a real switch. At the
default `0.05s` and typical scan speeds this is well inside the reader's own
practical read-range tolerance, but it means homing accuracy here is
software-timed, not electrically-timed — worth knowing if `poll_interval` is
tuned much higher.

**Corollary:** `[mmu_nfc_endstop laneN]` requires its matching `[nfc_gate
laneN]` to be enabled with a working reader at Klipper connect time — it
raises a config error rather than silently no-op'ing if the lane is disabled,
so both sections must be enabled or disabled together.

---

## Boundary Summary

```
┌─────────────┐    UID only    ┌──────────────────┐   spool_id    ┌────────────────┐
│ PN532Driver │ ─────────────► │   NFC_Manager    │ ────────────► │ SpoolmanClient │
│             │                │                  │               │                │
│ I2C frames  │                │ gate state       │               │ UID lookup     │
│ UID extract │                │ changed/removed  │               │ cache          │
└─────────────┘                │ macro dispatch   │               └────────────────┘
                               └────────┬─────────┘
                                        │ _NFC_SPOOL_CHANGED
                                        │ _NFC_SPOOL_REMOVED
                                        │ _NFC_TAG_NO_SPOOL
                                        ▼
                               ┌─────────────────┐
                               │  nfc_macros.cfg  │
                               │                  │
                               │  MMU_GATE_MAP    │
                               │  MMU_SPOOLMAN    │
                               │  (Happy Hare)    │
                               └─────────────────┘
```

No layer reaches back up the stack. No layer reaches sideways. The only direction is down and out through the macro boundary.

---

*Copyright (C) 2026 WoodWorker. Licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) — see [LICENSE](../../LICENSE).*
