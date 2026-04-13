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

**What we decided:** SpoolmanClient resolves UID → spool record and caches results. It does not call `MMU_GATE_MAP`, `MMU_SPOOLMAN`, or any Happy Hare command. It does not know which gate it is serving.

**Why:** The gate a spool belongs to is a *physical fact*, not a Spoolman record fact. Spoolman knows spool IDs and filament metadata. It does not know the MMU gate layout. The thing that knows both a spool ID and a gate number is NFC_Manager — and that is exactly where the gate assignment decision lives.

Keeping SpoolmanClient gate-agnostic also means it can be used from any context where you have a UID and need a spool ID, without dragging in MMU knowledge.

---

## Decision: Happy Hare Calls Are Behind `_NFC_*` Macros

**What we decided:** NFC_Manager dispatches three GCode macros. Those macros call `MMU_SPOOLMAN`. NFC_Manager does not call `MMU_SPOOLMAN` directly.

```
NFC_Manager → _NFC_SPOOL_CHANGED GATE=n SPOOL_ID=id UID=uid
           → _NFC_SPOOL_REMOVED  GATE=n
           → _NFC_TAG_NO_SPOOL   GATE=n UID=uid

nfc_macros.cfg → MMU_SPOOLMAN UPDATE=1 GATE={gate} SPOOLID={spool_id}
```

**Why:** Happy Hare's GCode API evolves between versions. The exact command for "tell Happy Hare about this spool" belongs in editable GCode config, not Python. The default macro uses `MMU_SPOOLMAN UPDATE=1` because Happy Hare owns the Spoolman-backed gate mapping and cache. NFC_Manager already knows the physical gate that produced the read, so it passes both `GATE` and `SPOOLID` into Happy Hare's Spoolman synchronization path.

This also makes the integration boundary visible. Anyone debugging a Happy Hare integration problem knows to look in `nfc_macros.cfg` — not in `pn532_driver.py`.

**Corollary:** If a Happy Hare version requires a different synchronization command, adjust `nfc_macros.cfg`. Do not add it to `PN532Driver` or `SpoolmanClient`.

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

**What we decided:** Each gate has one PN532, wired to the I2C bus on that lane's EBB42. There is no shared I2C bus across lanes.

**Why we chose this over a shared bus / multiplexer:**
- No I2C address management. Every PN532 can stay at `0x24` because they are on separate buses.
- No multiplexer hardware (`TCA9548A` or similar) to buy, wire, or configure.
- Failures are isolated. A PN532 that holds SDA low only affects one lane.
- The lane MCU already exists as part of the Happy Hare hardware setup. Its I2C bus is available with no extra wiring effort.

**Trade-off accepted:** This design only works when each lane has its own MCU. A printer with a single MCU for all lanes would need a different approach (multiplexer or shared bus with unique addresses).

---

## Decision: NFC_Manager Owns the Baseline Startup Policy

**What we decided:** On startup, NFC does not blindly overwrite Happy Hare's gate map with whatever the readers see at boot time.

**Why:** Happy Hare already starts with a known gate/filament map from its own config and persisted state. Tags may not be physically near readers at startup. A spool may be in a gate but temporarily unreadable. If NFC declared every gate empty on boot and then started filling them in as reads succeed, it would create a noisy burst of `MMU_SPOOLMAN UPDATE=1` updates during Klipper startup.

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

## Decision: Config Files Are User-Owned (Non-Destructive Merge)

**What we decided:** `install.sh` never overwrites a config file section the user has already configured. On subsequent runs, it only appends sections that are missing.

**Why:** Config files in `~/printer_data/config/NFC/` are part of the user's printer configuration. Overwriting them on update would destroy local customizations — particularly in `nfc_macros.cfg` where users may have adapted the Happy Hare calls for their version, and in `pn532_i2C.cfg` where the exact lane names and MCU names are specific to each user's hardware.

The merge strategy (copy-if-absent, append-missing-sections) means that new features that add new config sections are picked up on the next `install.sh` run, while existing customizations survive.

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
                               │  MMU_SPOOLMAN    │
                               │  (Happy Hare)    │
                               └─────────────────┘
```

No layer reaches back up the stack. No layer reaches sideways. The only direction is down and out through the macro boundary.

---

## SPI and Remote Pico Readers

SPI and remote Pico reader designs remain architectural possibilities within the codebase (`RC522Driver` exists, `NFCGateManager` handles the shared-MCU path). They are intentionally not documented as supported install paths because the I2C per-lane design is the one that has been worked through to completion. Documenting partial or unvalidated paths as supported would create support burden without benefit.

When SPI support is ready, it will be documented as a separate setup guide alongside this one.
