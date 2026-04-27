# klippy/extras/nfc_gates/hh_status.py
#
# Small adapter around Happy Hare's mmu.get_status() dict.


class HHGateStatus:
    def __init__(self, present=False, gate=-1, spool=-1, status=0,
                 action='', active_gate=-1, filament_pos=0, gate_count=0):
        self.present = present
        self.gate = gate
        self.spool = spool
        self.status = status
        self.action = action
        self.active_gate = active_gate
        self.filament_pos = filament_pos
        self.gate_count = gate_count

    @property
    def assigned(self):
        return self.spool > 0

    @property
    def available(self):
        return self.status >= 1

    @property
    def idle(self):
        return self.action == 'idle'


def _as_int(value, default=-1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def read(printer, gate, eventtime=None):
    """Return parsed HH status for one gate.

    Missing Happy Hare, missing keys, short lists, and non-integer values all
    degrade to safe defaults so NFC can keep operating without HH installed.
    """
    mmu = printer.lookup_object('mmu', None)
    if mmu is None:
        return HHGateStatus(gate=gate)

    try:
        status = mmu.get_status(eventtime if eventtime is not None else 0)
    except Exception:
        return HHGateStatus(gate=gate)

    gate_spool_ids = status.get('gate_spool_id', [])
    gate_statuses = status.get('gate_status', [])
    gate_count = len(gate_spool_ids)

    if gate < 0 or gate >= gate_count:
        return HHGateStatus(
            present=True,
            gate=gate,
            action=str(status.get('action', '')).lower(),
            active_gate=_as_int(status.get('gate', -1)),
            filament_pos=_as_int(status.get('filament_pos', 0), 0),
            gate_count=gate_count)

    gate_state = 0
    if gate < len(gate_statuses):
        gate_state = _as_int(gate_statuses[gate], -1)

    return HHGateStatus(
        present=True,
        gate=gate,
        spool=_as_int(gate_spool_ids[gate]),
        status=gate_state,
        action=str(status.get('action', '')).lower(),
        active_gate=_as_int(status.get('gate', -1)),
        filament_pos=_as_int(status.get('filament_pos', 0), 0),
        gate_count=gate_count)
