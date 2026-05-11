# klippy/extras/nfc_gates/klipper_interface.py
#
# EMU NFC Gate Reader — reactor-thread GCode macro dispatcher
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Receives gate change events and dispatches them as GCode macro calls in the
# Klipper reactor thread.
#
# Macros called (define in printer.cfg / nfc_macros.cfg):
#
#   _NFC_SPOOL_CHANGED  GATE=<n>  SPOOL_ID=<id>  UID=<hex>  [AUTO_CREATED=1]
#   _NFC_SPOOL_CHANGED  GATE=<n>  [NAME=<str>]  [MATERIAL=<str>]  [COLOR=<hex>]  [TEMP=<int>]  UID=<hex>
#   _NFC_SPOOL_REMOVED  GATE=<n>
#   _NFC_TAG_NO_SPOOL   GATE=<n>  UID=<hex>

import re

from .gate_state import (DIRECT_METADATA_SPOOL,
                         EVENT_CHANGED, EVENT_UID_ONLY, EVENT_REMOVED)
from .log import logger


class KlipperInterface:
    def __init__(self, printer, reactor, debug=2):
        self._printer = printer
        self._reactor = reactor
        self._debug = debug

    def dispatch(self, event_type, gate, uid_hex, spool_id, meta=None,
                 auto_created=False):
        """Schedule a GCode macro call for the given gate event."""
        self._reactor.register_callback(
            lambda e, et=event_type, g=gate, u=uid_hex, s=spool_id, m=meta,
                   ac=auto_created:
                self._run_gcode(et, g, u, s, m, ac))

    @staticmethod
    def _macro_value(value):
        value = str(value or '').strip()
        value = re.sub(r'\s+', '_', value)
        return re.sub(r'[^A-Za-z0-9_#.+-]', '', value)

    def _metadata_name(self, meta):
        meta = meta or {}
        base = meta.get('material_detail') or meta.get('material')
        prefix = meta.get('brand') or meta.get('vendor') or meta.get('tag_format')
        base = self._macro_value(base)
        prefix = self._macro_value(prefix)
        if prefix and prefix.lower() == 'bambu_lab':
            prefix = 'Bambu'
        if prefix and base and not base.lower().startswith(prefix.lower()):
            return "{}_{}".format(prefix, base)
        return base

    def _run_gcode(self, event_type, gate, uid_hex, spool_id, meta=None,
                   auto_created=False):
        gcode = self._printer.lookup_object('gcode')
        try:
            if event_type == EVENT_CHANGED:
                if spool_id is not None:
                    script = "_NFC_SPOOL_CHANGED GATE={} SPOOL_ID={} UID={}{}".format(
                        gate, spool_id, uid_hex,
                        " AUTO_CREATED=1" if auto_created else "")
                    logger.info("nfc_gates: gate %d → spool %d detected (UID %s%s)",
                                 gate, spool_id, uid_hex,
                                 " [auto-created]" if auto_created else "")
                else:
                    name     = self._metadata_name(meta or {})
                    material = self._macro_value((meta or {}).get('material', ''))
                    color    = self._macro_value((meta or {}).get('color_hex', ''))
                    temp     = (meta or {}).get('min_temp')
                    parts = ['_NFC_SPOOL_CHANGED', 'GATE={}'.format(gate)]
                    if name:
                        parts.append('NAME={}'.format(name))
                    if material:
                        parts.append('MATERIAL={}'.format(material))
                    if color:
                        parts.append('COLOR={}'.format(color))
                    if temp is not None:
                        parts.append('TEMP={}'.format(int(temp)))
                    parts.append('UID={}'.format(uid_hex))
                    script = ' '.join(parts)
                    logger.info("nfc_gates: gate %d → tag %s metadata-only "
                                "(name=%s material=%s color=%s temp=%s)",
                                gate, uid_hex, name, material, color, temp)
            elif event_type == EVENT_UID_ONLY:
                script = "_NFC_TAG_NO_SPOOL GATE={} UID={}".format(gate, uid_hex)
                logger.info("nfc_gates: gate %d → tag %s (no spool ID in Spoolman)",
                             gate, uid_hex)
            elif event_type == EVENT_REMOVED:
                script = "_NFC_SPOOL_REMOVED GATE={}".format(gate)
                logger.info("nfc_gates: gate %d → spool removed (was spool_id=%s)",
                             gate, spool_id)
            else:
                logger.warning("nfc_gates: unknown event type %r", event_type)
                return
            if self._debug >= 3:
                logger.info("nfc_gates: dispatching GCode: %s", script)
            gcode.run_script(script)
            if self._debug >= 3:
                logger.info("nfc_gates: dispatched GCode OK: %s", script)
        except Exception:
            logger.exception("nfc_gates: GCode dispatch failed for gate %d event %r",
                              gate, event_type)
