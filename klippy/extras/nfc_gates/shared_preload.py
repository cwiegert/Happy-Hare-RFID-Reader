# klippy/extras/nfc_gates/shared_preload.py
#
# Shared reader preload coordination.
#
# Keeps the shared-reader preload transaction policy separate from NFCGate's
# hardware polling and tag-resolution responsibilities.  Per-lane assignments
# take precedence over shared-reader staging.

from .log import logger


class SharedPreloadCoordinator:
    def __init__(self, gate):
        self._gate = gate

    def check(self, gcmd):
        gate = self._gate
        expected_spool = gcmd.get_int('EXPECTED_SPOOL_ID', -1)
        gate._shared_clear_preload_approval()
        if gate._debug >= 3:
            logger.info(
                "nfc_gate: [%s] PRELOAD_CHECK entered — pending spool=%s uid=%s",
                gate._name, gate._shared_pending_spool,
                gate._shared_pending_uid)
        if gate._is_printing():
            logger.info(
                "nfc_gate: [%s] PRELOAD_CHECK skipped — printing",
                gate._name)
            gcmd.respond_info(
                "[WARN] NFC[%s]: PRELOAD_CHECK skipped while printing; "
                "pending spool kept" % gate._name)
            if expected_spool > 0:
                raise gcmd.error(
                    "[WARN] NFC[%s]: PRELOAD_CHECK skipped while printing; "
                    "NEXT_SPOOLID not staged" % gate._name)
            return

        expired = gate._shared_expire_pending_if_needed()
        if gate._shared_pending_spool is None:
            logger.info(
                "nfc_gate: [%s] PRELOAD_CHECK — no pending spool; "
                "advising manual preload",
                gate._name)
            if expected_spool > 0:
                raise gcmd.error(
                    "[WARN] NFC[%s]: pending spool %d is no longer valid%s; "
                    "NEXT_SPOOLID not staged"
                    % (gate._name, expected_spool,
                       " (expired)" if expired else ""))
            if gate._shared_force_spool_id:
                raise gcmd.error(
                    "⛔ NFC[%s]: force_spool_id is set — tap your spool tag on "
                    "the shared reader before loading, or disable "
                    "force_spool_id to allow untagged loads" % gate._name)
            gcmd.respond_info(
                "⛔ NFC[%s]: no spool staged — tap your spool tag on the "
                "shared reader first, or use MMU_PRELOAD to load without "
                "spool assignment" % gate._name)
            gate._shared_last_action = "preload check found no staged spool"
            return

        spool_id = gate._shared_pending_spool
        auto_created = gate._shared_pending_auto_created
        if expected_spool > 0 and expected_spool != spool_id:
            logger.warning(
                "nfc_gate: [%s] PRELOAD_CHECK — macro saw spool %d but "
                "pending spool is %d; aborting stale bridge",
                gate._name, expected_spool, spool_id)
            raise gcmd.error(
                "[WARN] NFC[%s]: pending spool changed from %d to %d; "
                "NEXT_SPOOLID not staged. Trigger the preload again."
                % (gate._name, expected_spool, spool_id))

        logger.info(
            "nfc_gate: [%s] PRELOAD_CHECK — staging NEXT_SPOOLID=%d "
            "uid=%s auto_created=%s",
            gate._name, spool_id, gate._shared_pending_uid, auto_created)
        if gate._debug >= 3:
            logger.info(
                "nfc_gate: [%s] PRELOAD_CHECK — sending spool %d to Happy Hare "
                "via MMU_GATE_MAP NEXT_SPOOLID",
                gate._name, spool_id)

        # MMU_GATE_MAP and MMU_SPOOLMAN REFRESH are called from the
        # _NFC_SHARED_PRELOAD macro, not here.  Calling run_script() from
        # inside a GCode command handler deadlocks Klipper's GCode queue.
        gate._shared_preload_spool        = spool_id
        gate._shared_preload_uid          = gate._shared_pending_uid
        gate._shared_preload_auto_created = auto_created
        _ac_note = " [new spool synced]" if auto_created else ""
        gcmd.respond_info(
            "[OK] NFC[%s]: spool %d approved%s — macro will send to Happy Hare"
            % (gate._name, spool_id, _ac_note))
        logger.info(
            "nfc_gate: [%s] PRELOAD_CHECK — spool %d validated, "
            "macro responsible for MMU_GATE_MAP NEXT_SPOOLID",
            gate._name, spool_id)
        gate._shared_last_action = (
            "approved spool %d for NEXT_SPOOLID" % spool_id)

    def commit(self, gcmd):
        gate = self._gate
        spool_id = gcmd.get_int('SPOOL_ID', -1)
        if gate._shared_preload_spool is None:
            raise gcmd.error(
                "[WARN] NFC[%s]: PRELOAD_COMMIT without approved spool; "
                "pending spool kept" % gate._name)
        if spool_id != gate._shared_preload_spool:
            raise gcmd.error(
                "[WARN] NFC[%s]: PRELOAD_COMMIT spool mismatch "
                "(got %d, approved %d); pending spool kept"
                % (gate._name, spool_id, gate._shared_preload_spool))
        if gate._shared_pending_spool != spool_id:
            raise gcmd.error(
                "[WARN] NFC[%s]: pending spool changed before commit "
                "(got %s, approved %d); pending spool kept"
                % (gate._name, gate._shared_pending_spool, spool_id))
        gate._shared_clear_pending()
        gate._shared_last_action = (
            "staged spool %d via NEXT_SPOOLID" % spool_id)
        gate._shared_read_deadline = 0.0
        gate._polling = True
        gate.reactor.update_timer(gate._poll_timer, gate.reactor.NOW)
        logger.info(
            "nfc_gate: [%s] PRELOAD_CHECK complete — pending cleared, "
            "polling restarted",
            gate._name)
        if gate._debug >= 3:
            logger.info(
                "nfc_gate: [%s] shared reader ready for next spool — "
                "tap tag on reader to stage",
                gate._name)

    def clear_assigned(self, gcmd):
        gate = self._gate
        spool_id = gcmd.get_int('SPOOL_ID', -1)
        if spool_id <= 0:
            logger.warning(
                "nfc_gate: [%s] PRELOAD_CLEAR_ASSIGNED ignored — "
                "invalid SPOOL_ID=%d",
                gate._name, spool_id)
            gcmd.respond_info(
                "[WARN] NFC[%s]: PRELOAD_CLEAR_ASSIGNED ignored; invalid SPOOL_ID"
                % gate._name)
            gate._shared_last_action = (
                "ignored per-lane clear with invalid spool id")
            return
        expired = gate._shared_expire_pending_if_needed()
        if gate._shared_pending_spool is None:
            logger.info(
                "nfc_gate: [%s] PRELOAD_CLEAR_ASSIGNED — spool %d already "
                "owned by HH; shared pending was already clear%s",
                gate._name, spool_id, " after expiry" if expired else "")
            gate._shared_last_action = (
                "ignored per-lane clear for spool %d; no shared pending"
                % spool_id)
            return
        if gate._shared_pending_spool != spool_id:
            logger.warning(
                "nfc_gate: [%s] PRELOAD_CLEAR_ASSIGNED — HH owns spool %d "
                "but shared pending is spool %d; leaving pending intact",
                gate._name, spool_id, gate._shared_pending_spool)
            gate._shared_last_action = (
                "ignored per-lane clear for spool %d; pending spool is %d"
                % (spool_id, gate._shared_pending_spool))
            return
        if gate._has_per_lane_readers:
            logger.info(
                "nfc_gate: [%s] PRELOAD_CLEAR_ASSIGNED — spool %d already assigned "
                "by per-lane reader; clearing pending (no NEXT_SPOOLID needed)",
                gate._name, spool_id)
        else:
            logger.warning(
                "nfc_gate: [%s] PRELOAD_CLEAR_ASSIGNED — spool %d already assigned "
                "to a gate; possible duplicate load or stale assignment; "
                "skipping NEXT_SPOOLID",
                gate._name, spool_id)
            gcmd.respond_info(
                "[WARN] NFC[%s]: spool %d is already assigned to a gate — "
                "possible duplicate load or stale assignment; "
                "no NEXT_SPOOLID staged" % (gate._name, spool_id))
        gate._shared_clear_pending()
        gate._shared_last_action = (
            "cleared spool %d because HH already had it assigned" % spool_id)
        gate._shared_read_deadline = 0.0
        gate._polling = True
        gate.reactor.update_timer(gate._poll_timer, gate.reactor.NOW)
