# klippy/extras/nfc_gate.py
#
# EMU NFC Gate Reader — Klipper entry point
# Version 1.0.0  |  2026-04-14
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: CC-BY-NC-SA-4.0
#
# Licensed under Creative Commons Attribution-NonCommercial-ShareAlike 4.0
# International. You may not use this file except in compliance with the
# License. Full terms: https://creativecommons.org/licenses/by-nc-sa/4.0/
#
# ─────────────────────────────────────────────────────────────────────────────
# Klipper entry point for [nfc_gate] and [nfc_gate laneN] config sections.
# Per-lane I2C/PN532 path — one PN532 per EBB42 lane board.
#
# All implementation lives in the nfc_gates/ package.
# This file exists only because Klipper maps config section names to filenames
# in klippy/extras/ — [nfc_gate] requires a file called nfc_gate.py here.
#
# Install
# ───────
# Run install.sh — it symlinks this file and the nfc_gates/ package into
# ~/klipper/klippy/extras/ automatically.

__version__ = '1.0.0'

from .nfc_gates.NFC_manager import NFCGate, NFCGateDefaults, _lane_instances

# Tracks which printer object owns the current _lane_instances contents.
# A new Printer is created on every Klipper RESTART, so when this changes
# we know it's a fresh config load and must clear stale entries.
_current_printer = None


def load_config(config):
    # Handles the base [nfc_gate] section — shared defaults only, no hardware.
    global _current_printer
    _current_printer = config.get_printer()
    del _lane_instances[:]
    return NFCGateDefaults(config)


def load_config_prefix(config):
    # Handles [nfc_gate lane0], [nfc_gate lane1], etc.
    global _current_printer
    printer  = config.get_printer()
    if printer is not _current_printer:
        # No base [nfc_gate] section — first lane triggers the reset.
        _current_printer = printer
        del _lane_instances[:]
    defaults = printer.lookup_object('nfc_gate', None)
    gate     = NFCGate(config, defaults)
    # Replace any existing entry for this lane name (guards against Klipper
    # calling load_config_prefix more than once per section in a single run).
    name = config.get_name()
    for i, existing in enumerate(_lane_instances):
        if existing._name == gate._name:
            _lane_instances[i] = gate
            return gate
    _lane_instances.append(gate)
    return gate
