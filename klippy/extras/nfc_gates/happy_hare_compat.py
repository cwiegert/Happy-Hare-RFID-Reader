# klippy/extras/nfc_gates/happy_hare_compat.py
#
# Happy Hare V3/V4 compatibility helpers used by NFC virtual endstops.


def create_mmu_runout_helper(printer, name):
    """Create the Happy Hare runout helper from the installed layout."""
    try:
        from ..mmu_sensors import MmuRunoutHelper
    except ImportError:
        from ..mmu.mmu_sensor_utils import MmuRunoutHelper
        return MmuRunoutHelper(
            printer,
            name,
            event_delay=0,
            gcodes={},
            insert_remove_in_print=False,
            button_handler=None,
        )

    return MmuRunoutHelper(
        printer,
        name,
        event_delay=0,
        gcodes={},
        insert_remove_in_print=False,
        button_handler=None,
        switch_pin=None,
    )


def register_nfc_endstop(mmu, nfc_gate, endstop):
    """Register an NFC endstop on the Happy Hare rail for its logical gate."""
    gate_number = getattr(nfc_gate, '_gate', None)
    if gate_number is None:
        raise RuntimeError(
            "NFC gate does not expose a logical Happy Hare gate number")

    endstops_by_gate = getattr(mmu, '_nfc_endstops_by_gate', None)
    if endstops_by_gate is None:
        endstops_by_gate = {}
        setattr(mmu, '_nfc_endstops_by_gate', endstops_by_gate)

    existing_for_gate = endstops_by_gate.get(gate_number)
    if existing_for_gate is not None and existing_for_gate is not endstop:
        raise RuntimeError(
            "Happy Hare gate %d is already bound to NFC endstop '%s'"
            % (gate_number, existing_for_gate.endstop_name))

    existing_for_reader = getattr(nfc_gate, '_mmu_nfc_endstop', None)
    if existing_for_reader is not None and existing_for_reader is not endstop:
        raise RuntimeError(
            "NFC reader for Happy Hare gate %d is already bound to endstop '%s'"
            % (gate_number, existing_for_reader.endstop_name))

    drive_for_gate = getattr(mmu, 'drive', None)
    if callable(drive_for_gate):
        drive = drive_for_gate(gate_number)
        gear_stepper = getattr(drive, 'mmu_gear_stepper', None)
        rail = getattr(gear_stepper, 'rail', None)
        if rail is None or not hasattr(rail, 'add_extra_endstop'):
            raise RuntimeError(
                "Happy Hare V4 drive %d does not expose a gear rail"
                % gate_number)

        # V4 explicitly supports pin=None for software implemented endstops.
        rail.add_extra_endstop(
            None,
            endstop.endstop_name,
            mcu_endstop=endstop,
        )
        rail_description = 'V4 drive %d' % gate_number
    else:
        gear_rail = getattr(mmu, 'gear_rail', None)
        if gear_rail is None or not hasattr(gear_rail, 'add_extra_endstop'):
            raise RuntimeError(
                "Happy Hare does not expose a compatible gear rail interface")

        gear_rail.add_extra_endstop(
            "virtual_endstop:%s" % endstop.endstop_name,
            endstop.endstop_name,
            mcu_endstop=endstop,
        )
        rail_description = 'V3 gear rail'

    endstops_by_gate[gate_number] = endstop
    nfc_gate._mmu_nfc_endstop = endstop
    nfc_gate._mmu_nfc_endstop_name = endstop.endstop_name
    return rail_description
