# klippy/extras/mmu_nfc_endstop.py
#
# Wrap an existing [nfc_gate laneN] reader as a Happy Hare gear-rail endstop.
#
# This module intentionally does not create any NFC/I2C hardware.  It borrows
# the reader owned by [nfc_gate <name>] and polls it only while Happy Hare is
# performing a homing move against the configured virtual endstop.

import logging

from .mmu_sensors import MmuRunoutHelper


class MmuNfcEndstop:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]

        self._nfc_gate_name = config.get('nfc_gate')
        self._nfc_gate = None

        self.endstop_name = config.get('endstop_name', self.name)
        self.poll_interval = config.getfloat('poll_interval', 0.05, above=0.0)
        self._register_sensor = config.getboolean('register_sensor', True)

        self._filament_present = False
        self.runout_helper = None
        if self._register_sensor:
            self.runout_helper = MmuRunoutHelper(
                self.printer,
                self.endstop_name,
                event_delay=0,
                gcodes={},
                insert_remove_in_print=False,
                button_handler=None,
                switch_pin=None,
            )
            self.get_status = self.runout_helper.get_status
            sensor_obj_name = "filament_switch_sensor %s" % self.endstop_name
            if self.printer.lookup_object(sensor_obj_name, None) is None:
                self.printer.add_object(sensor_obj_name, self)
        else:
            self.get_status = self._get_status

        self._steppers = []
        self._trigger_completion = None
        self._last_trigger_time = None
        self._homing = False
        self._poll_timer = None
        self._last_poll_error = None

        self.printer.register_event_handler("klippy:connect", self._handle_connect)

    def _handle_connect(self):
        nfc_gate = self._get_nfc_gate()
        if not getattr(nfc_gate, '_enabled', True):
            raise self.config.error(
                "mmu_nfc_endstop %s references disabled [nfc_gate %s]"
                % (self.name, self._nfc_gate_name))
        if getattr(nfc_gate, '_reader', None) is None:
            raise self.config.error(
                "mmu_nfc_endstop %s could not find a reader on [nfc_gate %s]"
                % (self.name, self._nfc_gate_name))

        mmu = self.printer.lookup_object('mmu')
        mmu.gear_rail.add_extra_endstop(
            "virtual_endstop:%s" % self.endstop_name,
            self.endstop_name,
            mcu_endstop=self,
        )
        logging.info(
            "MMU: Registered NFC virtual endstop '%s' from [nfc_gate %s]",
            self.endstop_name, self._nfc_gate_name)

    def _get_nfc_gate(self):
        if self._nfc_gate is None:
            self._nfc_gate = self.printer.lookup_object(
                "nfc_gate %s" % self._nfc_gate_name)
        return self._nfc_gate

    def _note_filament_present(self, eventtime, state):
        self._filament_present = bool(state)
        if self.runout_helper is not None:
            self.runout_helper.note_filament_present(eventtime, state)

    def _get_status(self, eventtime=None):
        return {
            "filament_detected": bool(self._filament_present),
            "enabled": True,
            "runout_suspended": False,
        }

    def _poll_event(self, eventtime):
        if not self._homing:
            return self.reactor.NEVER

        uid = None
        try:
            uid = self._get_nfc_gate()._reader.read_tag(
                timeout=self.poll_interval)
            self._last_poll_error = None
        except Exception as e:
            if str(e) != self._last_poll_error:
                self._last_poll_error = str(e)
                logging.exception(
                    "MMU: NFC virtual endstop '%s' poll failed",
                    self.endstop_name)

        self.trigger_handler(eventtime, uid is not None)
        if not self._homing:
            return self.reactor.NEVER
        return self.reactor.monotonic() + 0.001

    def trigger_handler(self, eventtime, state):
        self._note_filament_present(eventtime, state)
        if (self._homing and state and self._trigger_completion is not None
                and self._last_trigger_time is None):
            self._last_trigger_time = eventtime
            self._homing = False
            self._trigger_completion.complete(True)

    # Endstop interface

    def query_endstop(self, print_time):
        if self.runout_helper is not None:
            return self.runout_helper.filament_present
        return self._filament_present

    def setup_pin(self, pin_type, pin_name):
        return self

    def add_stepper(self, stepper):
        if stepper not in self._steppers:
            self._steppers.append(stepper)

    def get_steppers(self):
        return list(self._steppers)

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered):
        self._trigger_completion = self.reactor.completion()
        self._last_trigger_time = None
        self._last_poll_error = None
        self._homing = True
        self._poll_timer = self.reactor.register_timer(
            self._poll_event, self.reactor.NOW)
        return self._trigger_completion

    def home_wait(self, home_end_time):
        self._homing = False
        if self._poll_timer is not None:
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
            self._poll_timer = None
        self._trigger_completion = None

        if self._last_trigger_time is None:
            raise self.printer.command_error(
                "No trigger on %s after full movement" % self.endstop_name)
        return self._last_trigger_time


def load_config_prefix(config):
    return MmuNfcEndstop(config)
