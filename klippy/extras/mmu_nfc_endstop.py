# klippy/extras/mmu_nfc_endstop.py
#
# Wrap an existing [nfc_gate laneN] reader as a Happy Hare gear-rail endstop.
#
# This module intentionally does not create any NFC/I2C hardware.  It borrows
# the reader owned by [nfc_gate <name>] and polls it only while Happy Hare is
# performing a homing move against the configured virtual endstop.

import logging

from .nfc_gates.happy_hare_compat import (
    create_mmu_runout_helper,
    register_nfc_endstop,
)


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
            self.runout_helper = create_mmu_runout_helper(
                self.printer, self.endstop_name)
            self.get_status = self.runout_helper.get_status
            sensor_obj_name = "filament_switch_sensor %s" % self.endstop_name
            if self.printer.lookup_object(sensor_obj_name, None) is None:
                self.printer.add_object(sensor_obj_name, self)
        else:
            self.get_status = self._get_status

        self._steppers = []
        self._trigger_completion = None
        self._last_trigger_time = None
        self._last_trigger_reactor_time = None
        self._last_home_elapsed = None
        self._home_start_print_time = None
        self._home_start_reactor_time = None
        self._homing = False
        self._triggered = True
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
        try:
            rail_description = register_nfc_endstop(mmu, nfc_gate, self)
        except RuntimeError as e:
            raise self.config.error(
                "mmu_nfc_endstop %s could not register with Happy Hare: %s"
                % (self.name, str(e)))
        logging.info(
            "MMU: Registered NFC virtual endstop '%s' from [nfc_gate %s] on %s",
            self.endstop_name, self._nfc_gate_name, rail_description)

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
        target_info = None
        used_read_target = False
        try:
            reader = self._get_nfc_gate()._reader
            read_target = getattr(reader, 'read_target', None)
            if read_target is not None:
                used_read_target = True
                target_info = read_target(timeout=self.poll_interval)
                if target_info is not None:
                    uid = target_info.get('uid')
            else:
                uid = reader.read_tag(timeout=self.poll_interval)
            self._last_poll_error = None
        except Exception as e:
            if str(e) != self._last_poll_error:
                self._last_poll_error = str(e)
                logging.exception(
                    "MMU: NFC virtual endstop '%s' poll failed",
                    self.endstop_name)

        if uid is not None:
            self._cache_scan_uid(uid, target_info)
        triggered = uid is not None
        self.trigger_handler(self.reactor.monotonic(), triggered)
        if triggered and used_read_target:
            self._release_reader_target()
        if not self._homing:
            return self.reactor.NEVER
        return self.reactor.monotonic() + self.poll_interval

    def _cache_scan_uid(self, uid, target_info=None):
        nfc_gate = self._get_nfc_gate()
        if not getattr(nfc_gate, '_scan_mode', False):
            return
        nfc_gate._scan_continuous_pending_uid = uid
        nfc_gate._scan_continuous_pending_target_info = (
            dict(target_info) if isinstance(target_info, dict) else None)
        nfc_gate._scan_continuous_tag_pending = True
        if getattr(nfc_gate, '_debug', 0) >= 3:
            logging.info(
                "MMU: NFC virtual endstop '%s' cached scan UID %s",
                self.endstop_name, uid)

    def _release_reader_target(self):
        release = getattr(self._get_nfc_gate()._reader,
                          '_release_current_target', None)
        if release is None:
            return
        try:
            release(reason="nfc_virtual_endstop")
        except TypeError:
            release()
        except Exception:
            logging.exception(
                "MMU: NFC virtual endstop '%s' target release failed",
                self.endstop_name)

    def _reactor_to_print_time(self, eventtime):
        mcu = self.printer.lookup_object('mcu', None)
        if mcu is not None:
            try:
                print_time = mcu.estimated_print_time(eventtime)
                if self._home_start_print_time is not None:
                    print_time = max(self._home_start_print_time, print_time)
                return print_time
            except Exception:
                pass
        if (self._home_start_print_time is not None
                and self._home_start_reactor_time is not None):
            return (self._home_start_print_time
                    + max(0.0, eventtime - self._home_start_reactor_time))
        return eventtime

    def trigger_handler(self, eventtime, state):
        self._note_filament_present(eventtime, state)
        if (self._homing and state == self._triggered
                and self._trigger_completion is not None
                and self._last_trigger_time is None):
            self._last_trigger_reactor_time = eventtime
            if self._home_start_reactor_time is not None:
                self._last_home_elapsed = max(
                    0.0, eventtime - self._home_start_reactor_time)
            self._last_trigger_time = self._reactor_to_print_time(eventtime)
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

    def get_last_home_elapsed(self):
        return self._last_home_elapsed

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered):
        self._trigger_completion = self.reactor.completion()
        self._last_trigger_time = None
        self._last_trigger_reactor_time = None
        self._last_home_elapsed = None
        self._last_poll_error = None
        self._home_start_print_time = print_time
        self._home_start_reactor_time = self.reactor.monotonic()
        self._homing = True
        self._triggered = bool(triggered)
        self._poll_timer = self.reactor.register_timer(
            self._poll_event, self.reactor.NOW)
        return self._trigger_completion

    def home_wait(self, home_end_time):
        self._homing = False
        if self._poll_timer is not None:
            self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
            self._poll_timer = None
        self._trigger_completion = None
        self._home_start_print_time = None
        self._home_start_reactor_time = None

        if self._last_trigger_time is None:
            raise self.printer.command_error(
                "No trigger on %s after full movement" % self.endstop_name)
        return self._last_trigger_time


def load_config_prefix(config):
    return MmuNfcEndstop(config)
