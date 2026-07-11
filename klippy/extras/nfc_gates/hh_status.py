# klippy/extras/nfc_gates/hh_status.py
#
# Small adapter around Happy Hare's mmu.get_status() dict.

GATE_EMPTY = 0
GATE_AVAILABLE = 1
GATE_INBUFFER = 2
FILAMENT_POS_UNLOADED = 0

ACTION_IDLE = 'idle'


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

    def label(self):
        """Return a short human-readable string describing this gate's Happy Hare assignment."""
        if not self.present:
            return "Happy Hare: n/a"
        if self.gate < 0 or (self.gate_count > 0 and self.gate >= self.gate_count):
            return "Happy Hare: unknown"
        if self.active_gate == self.gate and self.filament_pos > 0:
            return "Happy Hare: spool %d  loading (pos %d)" % (self.spool, self.filament_pos)
        if self.assigned:
            return "Happy Hare: spool %d  %s" % (
                self.spool, "available" if self.available else "assigned")
        if self.available:
            return "Happy Hare: found/no spool"
        return "Happy Hare: empty"


class HHFullStatus:
    def __init__(self, present=False, action='', active_gate=-1,
                 filament_pos=FILAMENT_POS_UNLOADED, gate_statuses=None,
                 gate_spool_ids=None):
        self.present = present
        self.action = action
        self.active_gate = active_gate
        self.filament_pos = filament_pos
        self.gate_statuses = gate_statuses or []
        self.gate_spool_ids = gate_spool_ids or []

    @property
    def idle(self):
        return self.action == 'idle'


def _as_int(value, default=-1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def read(printer, gate, eventtime=None):
    """Return parsed Happy Hare status for one gate.

    Missing Happy Hare, missing keys, short lists, and non-integer values all
    degrade to safe defaults so NFC can keep operating without Happy Hare installed.
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


def read_full(printer, eventtime=None):
    """Return parsed Happy Hare status across all gates."""
    mmu = printer.lookup_object('mmu', None)
    if mmu is None:
        return HHFullStatus()

    try:
        status = mmu.get_status(eventtime if eventtime is not None else 0)
    except Exception:
        return HHFullStatus()

    gate_statuses = [_as_int(v, -1) for v in status.get('gate_status', [])]
    gate_spool_ids = [_as_int(v, -1) for v in status.get('gate_spool_id', [])]
    return HHFullStatus(
        present=True,
        action=str(status.get('action', '')).lower(),
        active_gate=_as_int(status.get('gate', -1)),
        filament_pos=_as_int(status.get('filament_pos', 0), 0),
        gate_statuses=gate_statuses,
        gate_spool_ids=gate_spool_ids)


def all_lanes_parked_or_empty(printer, eventtime=None):
    """Return (ok, reason) for read-only scan-jog safety preflight."""
    status = read_full(printer, eventtime)
    if not status.present:
        return False, "Happy Hare status unavailable"

    if status.filament_pos != FILAMENT_POS_UNLOADED:
        return False, "filament is not parked (filament_pos=%d)" % (
            status.filament_pos,)

    if not status.gate_statuses:
        return False, "Happy Hare gate status unavailable"

    for lane, gate_state in enumerate(status.gate_statuses):
        if gate_state not in (GATE_EMPTY, GATE_AVAILABLE, GATE_INBUFFER):
            return False, "lane %d is not parked or empty (status=%d)" % (
                lane, gate_state)

    return True, None
