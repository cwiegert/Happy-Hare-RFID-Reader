# Design Note: Per-Lane UID Persistence When Spoolman Is Disabled

Status: **Design only — not yet implemented.**

Revised per direct guidance from Happy Hare's author: use Happy Hare's own
batched variable-persistence layer on the `mmu` controller instead of
touching `mmu_vars.cfg` as a raw file. This replaces the first draft of this
doc, which read/wrote `mmu_vars.cfg` directly and has been dropped in favor
of the model below. Further revised to support both Happy Hare major
versions — `v4`'s `var_manager` and `v3`'s equivalent (but un-namespaced)
methods directly on `mmu` — instead of gating the whole feature on `v4`.

---

## Problem

`NFCGate` keeps each lane's currently-known tag identity in
`GateState` (`gate_state.py`) — `_current_uid` / `_current_spool` — and that
object is pure in-memory Python state. It is never persisted.

When Spoolman is reachable, this doesn't matter much: Happy Hare's own gate
map persists `gate_spool_id` across restarts, and `_seed_cache_from_hh()`
(`nfc_manager.py:1762`) reads that back at startup to pre-arm a one-shot
suppression so the first real poll after a restart doesn't re-fire
`_NFC_SPOOL_CHANGED` for a spool Happy Hare already knows about.

With `spoolman_url: disabled`, that recovery path doesn't exist:

- There is no Spoolman `spool_id` to persist in Happy Hare's gate map in the
  first place — metadata-only tags resolve to `DIRECT_METADATA_SPOOL`
  (`gate_state.py:16`), a local-only sentinel, not a database-backed ID.
- `_seed_cache_from_hh()` seeds from `hh.spool` (Happy Hare's
  `gate_spool_id`), which stays unset/`-1` for metadata-only gates, so it has
  nothing useful to seed from.

Result: on every Klipper restart, a lane running without Spoolman forgets
what tag it last saw. The first poll after restart always looks like a brand
new tag, because `GateState._current_uid` starts at `None` regardless of
physical reality.

## Goal

When Spoolman is disabled, persist each per-lane reader's last-known tag UID
through Happy Hare's own variable-persistence layer, and restore it at
startup with the same one-shot suppression semantics `_seed_cache_from_hh()`
already uses for the Spoolman-backed path. Scope is the raw UID only, not
rich-tag metadata (material/color) — those are cheap to re-read from the tag
on the next real poll and don't need a persisted copy.

Out of scope: the shared reader (it stages spools for whichever gate loads
next, not a fixed lane, so "last-known UID for lane N" doesn't apply) and
Spoolman-enabled installs (already covered by the existing HH-seed path).

## Two Happy Hare APIs, same underlying model

Happy Hare v4 centralizes all `save_variables` reads/writes through
`SaveVariableManager` (`mmu_utils.py`), exposed on the controller as
`mmu.var_manager`. Happy Hare v3's monolithic `mmu.py` has the *same*
mechanism, just not split into a separate object or given a namespace
helper — it's a few plain methods directly on `mmu` itself:

```python
# v3 — extras/mmu/mmu.py, directly on the mmu controller
def save_variable(self, variable, value, write=False):
    self.save_variables.allVariables[variable] = value
    if write:
        self.write_variables()

def delete_variable(self, variable, write=False):
    _ = self.save_variables.allVariables.pop(variable, None)
    if write:
        self.write_variables()

def write_variables(self):
    if self._can_write_variables:
        mmu_vars_revision = self.save_variables.allVariables.get(self.VARS_MMU_REVISION, 0) + 1
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_REVISION, mmu_vars_revision))
```

```python
# v4 — extras/mmu/mmu_utils.py, class SaveVariableManager, exposed as mmu.var_manager
def get(self, variable, default, namespace=None): ...
def set(self, variable, value, namespace=None, write=False): ...
def delete(self, variable, namespace=None, write=False): ...
def write(self): ...  # bumps a revision counter, which triggers Klipper's own flush
```

Both read/write the *same* in-memory dict Klipper's stock `save_variables`
extra owns (`self.save_variables.allVariables`). `set`/`save_variable` and
`delete`/`delete_variable` mutate that dict directly and don't touch gcode at
all unless `write=True`. A `write=True` (or explicit `.write()`/
`write_variables()` call) issues one lightweight `SAVE_VARIABLE
VARIABLE=mmu__revision VALUE=<n+1>` gcode call — and because Klipper's stock
`SAVE_VARIABLE` command serializes the *entire* `allVariables` dict to disk
on every invocation, that one call flushes everything currently pending in
memory, not just the revision counter. `write=False` just updates memory;
nothing hits disk until something else (Happy Hare's own load/unload
bookkeeping, a calibration save, etc.) eventually triggers a flush, at which
point our pending change rides along.

Either way NFC never touches `mmu_vars.cfg` as a file — no path resolution,
no raw parsing, no hand-formatted `SAVE_VARIABLE VALUE=...` literals.

**Why `write=False` for this feature, per the author's guidance:** writing to
`mmu_vars.cfg` is expensive and can cause a Klipper TTC (Timer Too Close) on
systems with a slow SD card. Both versions' batching exists specifically so
non-essential updates don't force an immediate flush per change. A per-lane
UID cache used only to suppress a redundant console message after a restart
is exactly the kind of non-essential write this is meant for — so every
set/delete call in this design uses `write=False` on both versions.

**Durability trade-off, and why it's acceptable here:** if Klipper restarts
or crashes before anything triggers a natural flush, the most recent UID
update for a lane can be lost. The failure mode is graceful: on the next
restart, that one lane's seed either doesn't match (harmless — NFC just
dispatches as if this feature didn't exist, identical to today's behavior)
or matches a slightly stale-but-still-correct UID. There is no case where a
missed write causes worse behavior than not having this feature at all.

## Key naming: namespaced on v4, flat on v3

Happy Hare v4 supports multiple MMU units per printer, and unit *order* is
not guaranteed stable across config changes — so unit-scoped state must be
keyed by the unit's **name**, not its index, and by a **local** (per-unit,
0-based) gate number, not the global/logical gate number. This is the same
model Happy Hare v4 uses for its own per-gate statistics
(`VARS_MMU_GATE_STATISTICS_PREFIX = "mmu_statistics_gate_"`,
`mmu_controller.py`):

```python
mmu_unit = self.mmu_unit(gate)                                        # owning MmuUnit for this global gate
gstats = self.var_manager.get(
    "%s%d" % (VARS_MMU_GATE_STATISTICS_PREFIX, mmu_unit.local_gate(gate)),
    None, namespace=mmu_unit.name)
```

`SaveVariableManager.namespace()` builds the final key by substring-replacing
the literal `"mmu_"` prefix: `variable.replace("mmu_", "mmu_%s_" %
namespace)`. So `"mmu_statistics_gate_0"` namespaced under `"unit0"` becomes
`"mmu_unit0_statistics_gate_0"` — matching the author's example exactly.
**The base variable name must start with `mmu_` for this to work**, even
though the data is NFC-owned, not core Happy Hare state. v3 has no
multi-unit concept at all (single MMU only), so there's nothing to
namespace and no `mmu_unit()`/`local_gate()` methods to call — the global
gate number *is* the only numbering scheme that exists.

Both versions use the same base variable name, `mmu_nfc_uid_gate_<N>` — only
how `N` is derived, and whether it gets namespaced, differs:

| | v4 | v3 |
|---|---|---|
| `N` | `mmu_unit.local_gate(gate._gate)` — 0-based, per unit | `gate._gate` — global gate number directly |
| Final key | `mmu_unit<unit.name>_nfc_uid_gate_<N>` (e.g. `mmu_unit0_nfc_uid_gate_0`) | `mmu_nfc_uid_gate_<N>` (e.g. `mmu_nfc_uid_gate_0`) |
| Set | `mmu.var_manager.set(base, value, namespace=unit.name, write=False)` | `mmu.save_variable(key, value, write=False)` |
| Get | `mmu.var_manager.get(base, default, namespace=unit.name)` | `mmu.save_variables.allVariables.get(key, default)` — no getter wrapper on v3, read the dict directly |
| Delete | `mmu.var_manager.delete(base, namespace=unit.name, write=False)` | `mmu.delete_variable(key, write=False)` |

A small resolver in NFC picks the right shape:

```python
def _nfc_uid_var(mmu, gate_num):
    """Return (get, set, delete) closures for this gate's persisted-UID key,
    using v4's namespaced var_manager if present, else v3's flat mmu methods."""
    if hasattr(mmu, 'var_manager'):
        unit = mmu.mmu_unit(gate=gate_num)
        name = "mmu_nfc_uid_gate_%d" % unit.local_gate(gate_num)
        return (lambda default=None: mmu.var_manager.get(name, default, namespace=unit.name),
                lambda value: mmu.var_manager.set(name, value, namespace=unit.name, write=False),
                lambda: mmu.var_manager.delete(name, namespace=unit.name, write=False))
    if hasattr(mmu, 'save_variable'):
        name = "mmu_nfc_uid_gate_%d" % gate_num
        return (lambda default=None: mmu.save_variables.allVariables.get(name, default),
                lambda value: mmu.save_variable(name, value, write=False),
                lambda: mmu.delete_variable(name, write=False))
    return (None, None, None)  # neither API present — caller skips persistence
```

(Shown here as a sketch to pin down the call shapes for implementation —
the actual code should follow whatever function/method style the rest of
`nfc_manager.py` uses, not necessarily closures.)

## Write hook

Same integration point as the previous draft: `_poll_dispatch_event()`
(`nfc_manager.py:2639`), the single choke point every state-changing read —
scan-jog or background polling — passes through exactly once per real
change (`GateState.process_read()` already filters out no-op polls before
this method is ever reached).

Gated on `not self._shared and self._spoolman is None`, using the resolver
above (skip entirely if it returns `(None, None, None)`):

- `EVENT_CHANGED` / `EVENT_UID_ONLY` → `set_uid(event[2])` — persists the new UID.
- `EVENT_REMOVED` → `delete_uid()` — removes the key entirely rather than
  setting it to `None`, using the delete capability (available on both
  versions: `var_manager.delete()` on v4, `mmu.delete_variable()` on v3).

`EVENT_REMOVED` already only fires after `absent_threshold` consecutive
misses (see `docs/shared/architecture-decisions.md` → "Decision: Removal Is
Debounced"), so this inherits that debounce for free.

## Read / seed-restore hook

Mirror `_seed_cache_from_hh()` (`nfc_manager.py:1762`) and its call site in
`_startup_check_unknown_gate_event()` (`nfc_manager.py:2022`), during the
same startup window described in `docs/shared/how-it-works.md` → "Startup —
Boot Sequence." Add a parallel `_seed_uid_from_persisted_vars()` that:

1. Runs only when `self._spoolman is None` and `not self._shared`.
2. Uses the same resolver to get a `get_uid()` closure for this lane's
   `gate._gate` — works identically regardless of which Happy Hare version
   is installed; skips (does nothing) if neither API is present.
3. Calls `get_uid()` — a direct in-memory lookup on both versions, no file I/O.
4. If a UID comes back, stores it on a new one-shot field — e.g.
   `self._nfc_persisted_uid_seed` — mirroring `_hh_seed_spool_id` /
   `_hh_seed_available` (`nfc_manager.py:961-962`).

Then, in `_poll_dispatch_event()`, extend the existing seed-suppression
check (currently keyed on `_hh_seed_spool_id`) with a matching UID check: if
`event[2] == self._nfc_persisted_uid_seed`, suppress the Happy Hare dispatch
the same way a matching HH seed does today. If the UIDs don't match (spool
swapped while Klipper was down, or the gate is now empty), dispatch proceeds
normally — that's a real change. Like the existing HH seed, this is
one-shot: clear `self._nfc_persisted_uid_seed` unconditionally after the
first real event is evaluated against it, whether or not it matched.

## Version dependency

Both Happy Hare major versions are supported, via the resolver above:
`hasattr(mmu, 'var_manager')` picks the v4 (namespaced) path,
`hasattr(mmu, 'save_variable')` picks the v3 (flat) path. If a future Happy
Hare version exposes neither, the resolver returns `(None, None, None)` and
NFC skips persistence entirely for that lane — one informational log line,
not a warning, and otherwise identical to today's behavior. No raw
`SAVE_VARIABLE` gcode calls or direct `mmu_vars.cfg` file writes anywhere in
this design, on either version — both of Happy Hare's own APIs already give
us the `write=False` batching property the author asked for, so there's no
need to reintroduce hand-rolled I/O as a fallback.

## Edge cases and non-goals

- **Rich-tag metadata is not persisted.** Only the raw UID. If the tag
  carries material/color and `tag_parsing: True`, that gets re-read from the
  physical tag on the next real poll, consistent with the existing
  read-only-tags architecture decision — there's no cached copy to go stale.
- **No manual cleanup needed on lane disable.** `delete()` on removal keeps
  `allVariables` free of stale UID entries for gates that go empty normally.
  A lane disabled via `enabled: False` simply stops writing/reading; whatever
  key it last held (if any) is harmless and gets overwritten or deleted the
  next time that lane is re-enabled and sees a real state change.
- **Neither API present.** See "Version dependency" above — skip, log once,
  continue. Consistent with how every other Happy-Hare-dependent call in
  this codebase degrades when an optional API isn't present.
- **No new config knob.** Automatic whenever `spoolman_url: disabled` and
  either persistence API is available — not an opt-in toggle, matching how
  the request was framed.
- **Shared reader is out of scope**, as noted above.

## Suggested implementation sequence

1. Add `_nfc_persisted_uid_seed` (and matching "consumed" bookkeeping) to
   `NFCGate.__init__`, next to `_hh_seed_spool_id` / `_hh_seed_available`.
2. Add the resolver helper (v4 `var_manager` + namespacing vs. v3
   `save_variable`/`delete_variable`/flat dict read, falling back to
   no-op) shared by both the read and write hooks.
3. Add `_seed_uid_from_persisted_vars()`, called from the same startup
   window `_seed_cache_from_hh()` runs in, gated on
   `self._spoolman is None` and `not self._shared`.
4. Extend `_poll_dispatch_event()`'s existing seed-suppression check to also
   match against `_nfc_persisted_uid_seed`, clearing it one-shot the same way.
5. Add the write hook in `_poll_dispatch_event()` for `EVENT_CHANGED` /
   `EVENT_UID_ONLY` (set) and `EVENT_REMOVED` (delete), gated the same way.
6. Manual verification, on **each** Happy Hare version available for
   testing: with `spoolman_url: disabled`, tap a tag, confirm the persisted
   key (`mmu_unit<N>_nfc_uid_gate_<lgate>` on v4, `mmu_nfc_uid_gate_<gate>`
   on v3) appears in `mmu_vars.cfg` after Happy Hare's next natural flush,
   restart Klipper, confirm the first poll does not re-fire
   `_NFC_SPOOL_CHANGED` for the same tag, then physically swap the spool and
   confirm a restart *does* correctly detect and dispatch the change.
   Separately confirm a lane on a Happy Hare version with neither API logs
   once and otherwise behaves exactly as it does today.
