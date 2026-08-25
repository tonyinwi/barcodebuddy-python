"""
Per-gun current location, for a room-by-room inventory.

Scan a location QR at a shelf, and everything that gun scans afterwards is
created there. Scan a different one when you move. Walk away for ten minutes
and it forgets.

WHY PER GUN, not global: the expiry rule is "10 minutes of no scans on that
same gun", which only means anything if each gun carries its own location. It
also matches how mode already works -- `resolve_scan_mode()` binds ADD/CONSUME
per device -- so the stock gun parked in the basement cannot change what the
garbage gun records in the kitchen.

WHY IT EXPIRES AT ALL: a persistent *mode* is safe, because two guns are each
bound to one permanently. A persistent *location* is not. Walk away, come back
tomorrow, and every scan lands in the Spice Cabinet -- silently, and visible
weeks later as a catalogue full of wrong shelves.

WHY IT REVERTS RATHER THAN CLEARS: Grocy's products.location_id is NOT NULL, so
something must always be supplied. On expiry that is the user's
`product_presets_location_id`, which is what every scan used before this
existed.

Payloads are DESCRIPTIVE, not numeric: `BBUDDY-LOC-BIG-PANTRY`, not
`BBUDDY-LOC-7`. QR removes any length penalty, and a label taped to a shelf
should be readable by the person standing in front of it. Ids also change
across a database rebuild; names do not.
"""

import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

PREFIX = "BBUDDY-LOC-"
IDLE_SECONDS = 10 * 60


def slug(name: str) -> str:
    """
    'Oils & Vinegar' -> 'OILS-VINEGAR'.

    Deliberately lossy and deliberately stable: uppercase, non-alphanumerics
    collapse to a single dash, edges trimmed. Two locations that slug the same
    are a naming problem to fix in Grocy, and `resolve()` says so rather than
    silently picking one.
    """
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(name or "")).strip("-").upper()
    return re.sub(r"-{2,}", "-", s)


def barcode_for(name: str) -> str:
    return PREFIX + slug(name)


class LocationTracker:
    """Remembers which shelf each gun is standing at, and forgets on time."""

    def __init__(self, idle_seconds: int = IDLE_SECONDS):
        self.idle_seconds = idle_seconds
        self._lock = threading.Lock()
        self._by_device = {}        # device -> {"id","name","at"}

    # Resolves a device node to its USB id. Injected so this module stays
    # testable without the scanner package.
    usb_resolver = None

    def _key(self, device):
        """
        Key on the USB id, NOT the device node.

        One physical gun presents as several /dev/hidrawN nodes -- this setup
        shows 0581:011a on both hidraw0 and hidraw1. Keying on the node means
        setting a location while the gun emits on hidraw0, then losing it the
        moment it emits on hidraw1: the location silently stops applying and
        everything lands on the preset shelf.

        `resolve_scan_mode()` already keys on the USB id for exactly this
        reason. This did not, which was a latent bug -- today's scans all
        happened to come through one node.

        An unresolvable device still gets state under a shared key: better than
        dropping the feature on a setup where sysfs is unreadable.
        """
        if not device:
            return "(unknown device)"
        usb = None
        if self.usb_resolver:
            try:
                usb = self.usb_resolver(device)
            except Exception:                                    # noqa: BLE001
                usb = None
        return f"usb:{usb}" if usb else f"dev:{device}"

    def set(self, device, location_id, location_name):
        with self._lock:
            self._by_device[self._key(device)] = {
                "id": location_id, "name": location_name, "at": time.monotonic()}

    def clear(self, device):
        """
        Forget this gun's location entirely.

        Used when a location code is rejected: leaving the previous shelf in
        effect would send the next fifty products there. Storing a None-valued
        entry instead would fall back correctly but leave junk in the UI, so the
        entry is removed rather than blanked.
        """
        with self._lock:
            self._by_device.pop(self._key(device), None)

    def touch(self, device):
        """A product scan counts as activity: the idle clock is per gun."""
        with self._lock:
            entry = self._by_device.get(self._key(device))
            if entry:
                entry["at"] = time.monotonic()

    def current(self, device):
        """The gun's location, or None once it has gone stale."""
        with self._lock:
            key = self._key(device)
            entry = self._by_device.get(key)
            if not entry:
                return None
            if time.monotonic() - entry["at"] > self.idle_seconds:
                del self._by_device[key]
                logger.info(f"📍 {key} idle {self.idle_seconds // 60}min -- "
                            f"location '{entry['name']}' expired, back to the preset")
                return None
            return entry

    def snapshot(self):
        """For the UI. Reports remaining seconds so a gun can show its clock."""
        now = time.monotonic()
        with self._lock:
            return {k: {"id": v["id"], "name": v["name"],
                        "expires_in": max(0, int(self.idle_seconds - (now - v["at"])))}
                    for k, v in self._by_device.items()
                    if now - v["at"] <= self.idle_seconds}


def resolve(barcode, locations):
    """
    Match a scanned BBUDDY-LOC-* payload to a Grocy location.

    `locations` is Grocy's /objects/locations. Returns (location | None, error).
    An ambiguous slug is an error rather than a guess -- picking one silently
    is how the wrong shelf ends up on hundreds of products.
    """
    if not str(barcode or "").startswith(PREFIX):
        return None, None
    wanted = str(barcode)[len(PREFIX):].strip().upper()
    if not wanted:
        return None, "empty location code"

    matches = [l for l in (locations or []) if slug(l.get("name")) == wanted]
    if not matches:
        return None, f"no Grocy location matches '{wanted}'"
    if len(matches) > 1:
        names = ", ".join(str(m.get("name")) for m in matches)
        return None, (f"'{wanted}' is ambiguous -- {names} all slug the same. "
                      "Rename one in Grocy.")
    return matches[0], None
