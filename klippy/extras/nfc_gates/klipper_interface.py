# klippy/extras/nfc_gates/klipper_interface.py
#
# Klipper GCode command bridge.
#
# Receives gate change events from the background polling thread and dispatches
# them as GCode macro calls in the reactor (main Klipper) thread.
#
# Threading model
# ───────────────
# NFC polling runs in a background thread.  GCode execution MUST happen in the
# Klipper reactor thread.  reactor.register_callback() is thread-safe in Klipper
# (it uses an internal lock and wakes the reactor's select() loop), so it is
# used here as the inter-thread dispatch mechanism.
#
# GCode macros
# ────────────
# Rather than hardcoding Happy Hare macro names, this module calls user-defined
# GCode macros so the integration can be customised without editing Python code:
#
#   _NFC_SPOOL_CHANGED  GATE=<n>  SPOOL_ID=<id>  UID=<hex>
#   _NFC_SPOOL_REMOVED  GATE=<n>
#   _NFC_TAG_NO_SPOOL   GATE=<n>  UID=<hex>
#
# Define these macros in printer.cfg (see config/nfc_gates_example.cfg).

from .gate_state import EVENT_CHANGED, EVENT_UID_ONLY, EVENT_REMOVED
from .log import logger


class KlipperInterface:
    """
    Bridges the background NFC polling thread to Klipper's GCode system.

    Parameters
    ----------
    printer : Klipper printer object
    reactor : Klipper reactor object
    """

    def __init__(self, printer, reactor):
        self._printer = printer
        self._reactor = reactor

    def dispatch(self, event_type, gate, uid_hex, spool_id):
        """
        Schedule a GCode macro call for the given gate event.

        Safe to call from any thread.  The actual GCode execution happens in
        the reactor thread via register_callback().

        Parameters
        ----------
        event_type : str
            One of EVENT_CHANGED, EVENT_UID_ONLY, EVENT_REMOVED.
        gate : int
            Gate index (0-based).
        uid_hex : str or None
            Tag UID as an 8-character hex string, or None for REMOVED events.
        spool_id : int or None
            Spoolman spool ID, or None if the tag has no spool data.
        """
        # Capture locals for the lambda (Python closure gotcha)
        self._reactor.register_callback(
            lambda e, et=event_type, g=gate, u=uid_hex, s=spool_id:
                self._run_gcode(et, g, u, s))

    def _run_gcode(self, event_type, gate, uid_hex, spool_id):
        """
        Execute the appropriate GCode macro.  Called in the reactor thread.
        """
        gcode = self._printer.lookup_object('gcode')
        try:
            if event_type == EVENT_CHANGED:
                script = "_NFC_SPOOL_CHANGED GATE={} SPOOL_ID={} UID={}".format(
                    gate, spool_id, uid_hex)
                logger.info("nfc_gates: gate %d → spool %d detected (UID %s)",
                             gate, spool_id, uid_hex)

            elif event_type == EVENT_UID_ONLY:
                script = "_NFC_TAG_NO_SPOOL GATE={} UID={}".format(gate, uid_hex)
                logger.info("nfc_gates: gate %d → tag %s (no spool ID in memory)",
                             gate, uid_hex)

            elif event_type == EVENT_REMOVED:
                script = "_NFC_SPOOL_REMOVED GATE={}".format(gate)
                logger.info("nfc_gates: gate %d → spool removed "
                             "(was spool_id=%s)", gate, spool_id)

            else:
                logger.warning("nfc_gates: unknown event type %r", event_type)
                return

            gcode.run_script(script)

        except Exception:
            logger.exception(
                "nfc_gates: GCode dispatch failed for gate %d event %r",
                gate, event_type)
