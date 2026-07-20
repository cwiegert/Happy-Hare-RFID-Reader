# tests/python/fakes.py
#
# Minimal stand-ins for the Klipper objects nfc_gates code talks to
# (printer.lookup_object('gcode'), printer.get_reactor(), reactor callback
# scheduling). Real Klipper isn't installed in the test environment, and a
# full mock would need to track more Klipper internals than these modules
# actually touch — these fakes cover exactly the surface nfc_gates uses.


class FakeGCode:
    """Records every G-code script the code under test tries to run."""

    def __init__(self):
        self.scripts = []

    def run_script(self, script):
        self.scripts.append(script)


class FakeReactor:
    """Runs registered callbacks immediately instead of deferring them.

    Real Klipper schedules these on the reactor thread; tests care about
    *what* gets dispatched, not the threading, so callbacks fire synchronously
    when register_callback is called.
    """

    def __init__(self):
        self.callbacks = []

    def register_callback(self, callback):
        self.callbacks.append(callback)
        callback(None)


class FakePrinter:
    """Answers lookup_object('gcode') the way nfc_gates code expects."""

    def __init__(self, gcode=None, reactor=None):
        self.gcode = gcode if gcode is not None else FakeGCode()
        self.reactor = reactor if reactor is not None else FakeReactor()
        self._objects = {"gcode": self.gcode}

    def lookup_object(self, name):
        return self._objects[name]

    def get_reactor(self):
        return self.reactor
