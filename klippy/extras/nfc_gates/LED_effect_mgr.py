# klippy/extras/nfc_gates/LED_effect_mgr.py
#
# Contract boundary between NFC reader events and Happy Hare LED effects.

from .log import logger

EVENT_SCAN_START = 'scan_start'
EVENT_SCAN_REASSERT = 'scan_reassert'
EVENT_TAG_READ = 'tag_read'
EVENT_SPOOL_READY = 'spool_ready'
EVENT_UNRESOLVED = 'unresolved'
EVENT_AUTO_CREATE = 'auto_create'
EVENT_REWIND = 'rewind'
EVENT_WARNING = 'warning'
EVENT_LED_TEST = 'led_test'
EVENT_RELEASE = 'release'


def lane_effect_name(base_effect, gate):
    """Return HH's per-gate effect name for a lane reader."""
    base = (base_effect or '').strip()
    if not base:
        return ''
    return "%s_exit_%d" % (base, int(gate))


def shared_effect_name(base_effect, led_unit='unit0', segment='exit',
                       mcu_index=None):
    """Return HH's effect name for a shared-reader LED event.

    shared_led_segment=gate keeps the legacy per-gate naming convention.
    Other segments use the whole-chain HH name: {unit}_{base}_{segment}.
    """
    base = (base_effect or '').strip()
    if not base:
        return ''
    segment = (segment or 'exit').strip().lower()
    if segment == 'gate':
        if mcu_index is None:
            return base
        return lane_effect_name(base, mcu_index)
    return "%s_%s_%s" % ((led_unit or 'unit0'), base, segment)


def led_unit_index(led_unit):
    """Return HH's numeric UNIT value from NFC's unit label."""
    if led_unit is None:
        return None
    value = str(led_unit).strip().lower()
    if not value:
        return None
    if value.startswith('unit'):
        value = value[4:]
    try:
        return int(value)
    except Exception:
        return None


def is_happy_hare_v4(printer):
    """Return whether the installed Happy Hare exposes the V4 drive API."""
    if printer is None:
        return False
    mmu = printer.lookup_object('mmu', None)
    return callable(getattr(mmu, 'drive', None))


def hh_led_script(effect_name, duration=None, gate=None, unit=None,
                  direct_effect=False):
    """Build the appropriate Happy Hare command for an NFC LED effect."""
    effect = (effect_name or '').strip()
    if not effect:
        return ''
    if direct_effect:
        # V4's MMU_SET_LED validates only configured operation effects. NFC's
        # generated [mmu_led_effect] instances must therefore be addressed
        # directly by their full generated name.
        return "_MMU_SET_LED_EFFECT EFFECT=%s REPLACE=1" % effect
    parts = ["MMU_SET_LED"]
    if unit is not None:
        try:
            parts.append("UNIT=%d" % int(unit))
        except Exception:
            pass
    if gate is not None:
        try:
            parts.append("GATE=%d" % int(gate))
        except Exception:
            pass
    parts.append("EXIT_EFFECT=%s" % effect)
    if duration is not None:
        try:
            duration = float(duration)
        except Exception:
            duration = 0.0
        if duration > 0.0:
            parts.append("DURATION=%.2f" % duration)
    return " ".join(parts)


class LEDResult:
    def __init__(self, ok, effect='', script='', error=None, event=''):
        self.ok = ok
        self.effect = effect
        self.script = script
        self.error = error
        self.event = event


class LEDEffectManager:
    """Small adapter for NFC's temporary LED requests.

    HH remains the owner of steady-state LED output.  NFC uses this manager to
    request short-lived effects and to release control back to HH.
    """
    def __init__(self, printer, reactor=None, runner=None, name='nfc',
                 console=None):
        self.printer = printer
        self.reactor = reactor
        self.runner = runner
        self.name = name
        self.console = console

    def _v4_effect_registry(self):
        """Return NFC's printer-scoped registry of directly started V4 effects."""
        registry = getattr(self.printer, '_nfc_v4_led_effects', None)
        if registry is None:
            registry = {'next_token': 0, 'targets': {}}
            setattr(self.printer, '_nfc_v4_led_effects', registry)
        return registry

    def _remember_v4_effect(self, target, effect):
        registry = self._v4_effect_registry()
        registry['next_token'] += 1
        token = registry['next_token']
        registry['targets'][target] = (effect, token)
        return token

    def _release_v4_effect_after(self, target, effect, token, duration):
        if self.reactor is None or duration is None:
            return
        try:
            duration = float(duration)
        except Exception:
            duration = 0.0
        if duration <= 0.0:
            return

        def _release(eventtime, _target=target, _effect=effect, _token=token):
            registry = self._v4_effect_registry()
            if registry['targets'].get(_target) != (_effect, _token):
                return self.reactor.NEVER
            registry['targets'].pop(_target, None)
            script = (
                "_MMU_SET_LED_EFFECT EFFECT=%s STOP=1\n"
                "MMU_GATE_MAP QUIET=1" % _effect)
            self._run_effect_script(script, event=EVENT_RELEASE,
                                    log_failure=False)
            return self.reactor.NEVER

        self._register_timer(_release, duration)

    def _gcode(self):
        if self.printer is None:
            return None
        return self.printer.lookup_object('gcode', None)

    def _run_script(self, script):
        if self.runner is not None:
            return self.runner(script) is not False
        gcode = self._gcode()
        if gcode is None:
            raise RuntimeError("gcode object unavailable")
        gcode.run_script(script)
        return True

    def _run_async(self, callback):
        if self.reactor is not None and hasattr(self.reactor, 'register_async_callback'):
            self.reactor.register_async_callback(callback)
            return True
        return False

    def _register_timer(self, callback, delay):
        if self.reactor is None:
            return None
        when = self.reactor.monotonic() + delay
        try:
            return self.reactor.register_timer(callback, when)
        except TypeError:
            timer = self.reactor.register_timer(callback)
            self.reactor.update_timer(timer, when)
            return timer

    def _run_effect_script(self, script, event='', log_failure=True):
        if not script:
            return False
        try:
            if self._run_script(script):
                return True
        except Exception as e:
            if log_failure:
                logger.warning("[%s]: LED %s transport failed: %s",
                               self.name, event or 'named', e)
        return False

    def play_named(self, effect_name, replace=True,
                   async_dispatch=False, log_failure=True, event='',
                   duration=None, gate=None, unit=None, display_effect=None,
                   target=None):
        effect = (effect_name or '').strip()
        display_effect = (display_effect or effect).strip()
        v4_direct = is_happy_hare_v4(self.printer)
        script = hh_led_script(
            display_effect if v4_direct else effect,
            duration=duration, gate=gate, unit=unit,
            direct_effect=v4_direct)
        if not script:
            return LEDResult(False, effect=display_effect, script='',
                             error=ValueError("missing LED effect"),
                             event=event)
        try:
            target = target or display_effect

            def _started():
                if v4_direct:
                    token = self._remember_v4_effect(target, display_effect)
                    self._release_v4_effect_after(
                        target, display_effect, token, duration)

            if async_dispatch and self._run_async(
                    lambda et, _s=script:
                    self._run_effect_script(
                        _s, event=event, log_failure=log_failure) and _started()):
                logger.info("[%s]: LED %s effect %s scheduled",
                            self.name, event or 'named', display_effect)
            else:
                if not self._run_effect_script(
                        script, event=event, log_failure=log_failure):
                    raise RuntimeError("LED transport failed")
                _started()
                logger.info("[%s]: LED %s effect %s",
                            self.name, event or 'named', display_effect)
            return LEDResult(True, effect=display_effect, script=script,
                             event=event)
        except Exception as e:
            if log_failure:
                logger.warning("[%s]: LED %s effect %s failed: %s",
                               self.name, event or 'named', display_effect, e)
            return LEDResult(False, effect=display_effect, script=script,
                             error=e, event=event)

    def play_lane(self, base_effect, gate, replace=True,
                  async_dispatch=False, log_failure=True, event='',
                  duration=None):
        base = (base_effect or '').strip()
        return self.play_named(
            base,
            replace=replace, async_dispatch=async_dispatch,
            log_failure=log_failure, event=event, duration=duration,
            gate=gate, display_effect=lane_effect_name(base, gate),
            target='lane:%d' % int(gate))

    def play_shared(self, base_effect, led_unit='unit0', segment='exit',
                    mcu_index=None, replace=True,
                    async_dispatch=False, log_failure=True, event='',
                    duration=None):
        base = (base_effect or '').strip()
        segment = (segment or 'exit').strip().lower()
        gate = mcu_index if segment == 'gate' else None
        unit = None if segment == 'gate' else led_unit_index(led_unit)
        target = 'shared:%s:%s:%s' % (led_unit, segment, mcu_index)
        return self.play_named(
            base,
            replace=replace, async_dispatch=async_dispatch,
            log_failure=log_failure, event=event, duration=duration,
            gate=gate, unit=unit,
            display_effect=shared_effect_name(
                base, led_unit, segment, mcu_index),
            target=target)

    def play_scan_start(self, base_effect, gate, **kwargs):
        return self.play_lane_event(EVENT_SCAN_START, base_effect, gate, **kwargs)

    def play_scan_reassert(self, base_effect, gate, **kwargs):
        return self.play_lane_event(EVENT_SCAN_REASSERT, base_effect, gate, **kwargs)

    def play_tag_read(self, base_effect, gate, **kwargs):
        return self.play_lane_event(EVENT_TAG_READ, base_effect, gate, **kwargs)

    def play_rewind(self, base_effect, gate, **kwargs):
        return self.play_lane_event(EVENT_REWIND, base_effect, gate, **kwargs)

    def play_unresolved(self, base_effect, gate, **kwargs):
        return self.play_lane_event(EVENT_UNRESOLVED, base_effect, gate, **kwargs)

    def play_auto_create(self, base_effect, gate, **kwargs):
        return self.play_lane_event(EVENT_AUTO_CREATE, base_effect, gate, **kwargs)

    def play_led_test(self, base_effect, gate, **kwargs):
        return self.play_lane_event(EVENT_LED_TEST, base_effect, gate, **kwargs)

    def play_lane_event(self, event, base_effect, gate, replace=True,
                        async_dispatch=False, log_failure=True,
                        duration=None):
        return self.play_lane(
            base_effect, gate, replace=replace,
            async_dispatch=async_dispatch, log_failure=log_failure,
            event=event, duration=duration)

    def play_shared_tag_read(self, base_effect, **kwargs):
        return self.play_shared_event(EVENT_TAG_READ, base_effect, **kwargs)

    def play_shared_spool_ready(self, base_effect, **kwargs):
        return self.play_shared_event(EVENT_SPOOL_READY, base_effect, **kwargs)

    def play_shared_unresolved(self, base_effect, **kwargs):
        return self.play_shared_event(EVENT_UNRESOLVED, base_effect, **kwargs)

    def play_shared_auto_create(self, base_effect, **kwargs):
        return self.play_shared_event(EVENT_AUTO_CREATE, base_effect, **kwargs)

    def play_shared_warning(self, base_effect, **kwargs):
        return self.play_shared_event(EVENT_WARNING, base_effect, **kwargs)

    def play_shared_event(self, event, base_effect, led_unit='unit0',
                          segment='exit', mcu_index=None, replace=True,
                          async_dispatch=False,
                          log_failure=True, duration=None):
        return self.play_shared(
            base_effect, led_unit=led_unit, segment=segment,
            mcu_index=mcu_index, replace=replace,
            async_dispatch=async_dispatch, log_failure=log_failure,
            event=event, duration=duration)

    def schedule_lane_test_cycles(self, base_effect, gate, cycles=2,
                                  duration=2.0, gap=0.15,
                                  restore_delay=0.1,
                                  skip_restore=None):
        """Run a visible lane LED test sequence and restore HH ownership."""
        try:
            cycles = int(cycles)
        except Exception:
            cycles = 2
        cycles = max(1, cycles)
        cycle_stride = duration + gap
        for cycle in range(cycles):
            start_delay = cycle * cycle_stride
            if cycle > 0:
                self._schedule_lane_event(
                    EVENT_LED_TEST, base_effect, gate, start_delay)
        self._schedule_release((cycles - 1) * cycle_stride + duration
                               + restore_delay, skip_restore=skip_restore)

    def _schedule_lane_event(self, event, base_effect, gate, delay,
                             duration=None):
        if not base_effect or delay <= 0:
            return

        def _run(eventtime, _event=event, _effect=base_effect,
                 _gate=gate, _duration=duration):
            self.play_lane_event(
                _event, _effect, _gate,
                async_dispatch=True, log_failure=False, duration=_duration)
            return self.reactor.NEVER

        self._register_timer(_run, delay)

    def _schedule_release(self, delay, skip_restore=None):
        if delay <= 0:
            return

        def _run(eventtime):
            if skip_restore is not None and skip_restore():
                logger.info("[%s]: LED release skipped", self.name)
                return self.reactor.NEVER
            self.release(async_dispatch=True)
            return self.reactor.NEVER

        self._register_timer(_run, delay)

    def release(self, async_dispatch=False):
        if is_happy_hare_v4(self.printer):
            registry = self._v4_effect_registry()
            effects = sorted(set(
                effect for effect, _token in registry['targets'].values()))
            registry['targets'].clear()
            stop_scripts = [
                "_MMU_SET_LED_EFFECT EFFECT=%s STOP=1" % effect
                for effect in effects]
            stop_scripts.append("MMU_GATE_MAP QUIET=1")
            script = "\n".join(stop_scripts)
        else:
            script = "MMU_GATE_MAP QUIET=1"
        try:
            if async_dispatch and self._run_async(
                    lambda et, _s=script: self._run_script(_s)):
                logger.info("[%s]: LED control release scheduled", self.name)
            else:
                self._run_script(script)
                logger.info("[%s]: LED control released", self.name)
            return LEDResult(True, script=script, event=EVENT_RELEASE)
        except Exception as e:
            logger.warning("[%s]: LED release failed: %s", self.name, e)
            return LEDResult(False, script=script, error=e, event=EVENT_RELEASE)

