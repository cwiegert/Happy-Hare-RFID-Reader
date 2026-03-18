# klippy/extras/nfc_gates/spoolman_client.py
#
# Spoolman API client — looks up a spool record by NFC tag UID.
#
# Integration model (UID lookup)
# ───────────────────────────────────────────
# Tags are never written to.  Each tag's factory UID is registered in
# Spoolman by setting a custom extra field (default key: "rfid") to the
# tag's UID string.  When the reader detects a tag it reads only the UID
# (the fastest possible NFC operation), then this client queries the
# Spoolman REST API to find which spool record carries that UID.
#
# Spoolman extra fields
# ─────────────────────
# Spoolman stores arbitrary key-value metadata on each spool in a JSON
# dict called "extra".  You configure which extra fields exist in the
# Spoolman web UI:
#
#   Settings → Extra fields → Spool → Add field
#     Field name:  rfid        (or whatever spoolman_rfid_key is set to)
#     Field type:  Text
#
# Then on each spool record set the "rfid" field to the tag's UID string
# exactly as the reader reports it (uppercase hex, no separators):
#   e.g.  04A23BC1D45E80
#
# The stored value may optionally contain colons, hyphens, or spaces —
# this client normalises both sides before comparing.
#
# API endpoint
# ────────────
# GET {spoolman_url}/api/v1/spool
#
# Returns a JSON array of all spool objects.  Each object has an "extra"
# dict (may be null or absent for spools created before the field was
# added).  This client filters in Python; no server-side filtering is
# needed, so it works with all Spoolman versions that have the /spool
# endpoint (v0.14+).
#
# For a typical home collection (50–300 spools) the response is a few KB
# and the lookup completes in well under 100 ms on a local network.
#
# Caching
# ───────
# The result of a successful lookup is cached by UID for cache_ttl seconds
# (default 300 s = 5 min).  Polls that see the same tag within the TTL do
# not make a network request.  Set cache_ttl=0 to disable caching.

import json
import time

from .log import logger

try:
    # Python 3
    import urllib.request as _urllib_request
    import urllib.error   as _urllib_error
except ImportError:
    # Micropython / very old Python 2 — should not reach here in Klipper
    raise ImportError("spoolman_client requires Python 3")


class SpoolmanClient:
    """
    Queries the Spoolman REST API to resolve a tag UID to a spool ID.

    Parameters
    ----------
    base_url : str
        Root URL of the Spoolman instance, e.g. "http://192.168.1.50:7912".
        Trailing slash is stripped automatically.
    rfid_key : str
        Name of the extra field that holds the tag UID on each spool record.
        Default: "rfid".  Must match the field name you created in the
        Spoolman Settings → Extra fields → Spool panel.
    timeout : float
        HTTP request timeout in seconds.  Default: 5.0.
    cache_ttl : float
        Seconds to cache a successful UID → spool_id mapping.  Set to 0
        to disable.  Default: 300.
    debug : int
        0 = silent, 1 = warnings only, 2 = full trace.
    """

    def __init__(self, base_url, rfid_key='rfid',
                 timeout=5.0, cache_ttl=300.0, debug=1):
        self._base_url  = base_url.rstrip('/')
        self._rfid_key  = rfid_key
        self._timeout   = timeout
        self._cache_ttl = cache_ttl
        self._debug     = debug

        # UID → (spool_id, expiry_monotonic)
        self._cache = {}

    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise_uid(uid_str):
        """
        Strip surrounding quotes, separators, and uppercase so that
        e.g. '"04:a2:3b"' == "04A23B".
        """
        return (uid_str.strip('"\'')
                       .upper()
                       .replace(':', '')
                       .replace('-', '')
                       .replace(' ', ''))

    # ─────────────────────────────────────────────────────────────────────────

    def lookup_spool_by_uid(self, uid_hex):
        """
        Return the Spoolman spool ID whose extra[rfid_key] matches uid_hex,
        or None if not found or if the API request fails.

        Parameters
        ----------
        uid_hex : str
            Tag UID as returned by read_tag() — uppercase hex, no separators.

        Returns
        -------
        int or None
        """
        uid_norm = self._normalise_uid(uid_hex)

        # ── Cache hit ─────────────────────────────────────────────────────────
        if self._cache_ttl > 0 and uid_norm in self._cache:
            spool_id, expiry = self._cache[uid_norm]
            if time.monotonic() < expiry:
                if self._debug >= 2:
                    logger.debug(
                        "spoolman: cache hit uid=%s → spool_id=%s", uid_hex, spool_id)
                return spool_id
            # Expired — remove stale entry
            del self._cache[uid_norm]

        # ── API request ───────────────────────────────────────────────────────
        url = '{}/api/v1/spool'.format(self._base_url)
        if self._debug >= 2:
            logger.debug("spoolman: GET %s (looking for uid=%s, key=%s)",
                          url, uid_hex, self._rfid_key)
        try:
            req = _urllib_request.Request(
                url,
                headers={'Accept': 'application/json',
                         'User-Agent': 'klipper-nfc-gates/1.0'})
            with _urllib_request.urlopen(req, timeout=self._timeout) as resp:
                spools = json.loads(resp.read().decode('utf-8'))
        except _urllib_error.URLError as e:
            logger.warning("spoolman: request failed (%s): %s", url, e)
            return None
        except Exception as e:
            logger.warning("spoolman: unexpected error querying %s: %s", url, e)
            return None

        if not isinstance(spools, list):
            logger.warning("spoolman: unexpected response type %s from %s",
                            type(spools).__name__, url)
            return None

        # ── Search ────────────────────────────────────────────────────────────
        spool_id = None
        for spool in spools:
            extra = spool.get('extra') or {}
            stored_raw = extra.get(self._rfid_key)
            if not stored_raw:
                continue
            stored_cleaned = str(stored_raw).strip('"\'')
            stored_norm = self._normalise_uid(stored_cleaned)
  #          stored_norm = self._normalise_uid(str(stored_raw))
            if stored_norm == uid_norm:
                raw_id = spool.get('id')
                if raw_id is not None:
                    spool_id = int(raw_id)
                break

        if self._debug >= 1:
            if spool_id is not None:
                logger.info("spoolman: uid=%s → spool_id=%d", uid_hex, spool_id)
            else:
                logger.info(
                    "spoolman: uid=%s not found in %d spool records "
                    "(check the '%s' extra field in Spoolman)",
                    uid_hex, len(spools), self._rfid_key)

        # ── Cache store ───────────────────────────────────────────────────────
        if self._cache_ttl > 0 and spool_id is not None:
            self._cache[uid_norm] = (spool_id, time.monotonic() + self._cache_ttl)

        return spool_id

    def clear_cache(self):
        """Flush all cached UID → spool_id mappings."""
        self._cache.clear()
        if self._debug >= 2:
            logger.debug("spoolman: cache cleared")
