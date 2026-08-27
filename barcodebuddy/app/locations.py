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
    """The slug a name WOULD get. See stored_slug() for the one that counts."""
    return PREFIX + slug(name)


def stored_slug(location) -> str:
    """
    The slug persisted on the Grocy location, or '' when it has none yet.

    Lives in the `slug` userfield on `locations`. Grocy returns userfields
    inline on /objects/locations, so reading it costs nothing extra.
    """
    uf = (location or {}).get("userfields") or {}
    return str(uf.get("slug") or "").strip().upper()


def barcode_for_location(location) -> str:
    """
    The barcode for a location: the STORED slug if it has one, else computed.

    This is the function that matters. `barcode_for(name)` recomputes from the
    current name every time, which is fine right up until somebody renames a
    shelf in Grocy -- at which point every label already stuck to that shelf
    stops resolving, and a rejected location code CLEARS the gun by design, so
    everything scanned afterwards silently lands on the preset. Renaming
    "Basement Pantry" to "Basement - Cleaning Pantry" is enough to do it.

    A stored slug makes the printed label authoritative and the display name
    free to change, which is the right way round: the label is the thing you
    cannot edit after the fact.
    """
    return PREFIX + (stored_slug(location) or slug(location.get("name")))


def freeze_slug(location) -> str:
    """
    The slug to persist for a location that has none, or '' if it already has.

    IMMUTABLE ONCE SET: an existing slug is never recomputed, never migrated,
    never "corrected" to match a new name. That is the entire guarantee -- a
    slug that can change is just a computed slug with extra steps.
    """
    return "" if stored_slug(location) else slug(location.get("name"))


class LocationTracker:
    """Remembers which shelf each gun is standing at, and forgets on time."""

    def __init__(self, idle_seconds: int = IDLE_SECONDS):
        self.idle_seconds = idle_seconds
        self._lock = threading.Lock()
        self._by_device = {}        # device -> {"id","name","at","counts"}

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

    # What a product scan can amount to, from the point of view of somebody
    # standing at a shelf. Deliberately narrower than scan_result['status']:
    # the shelf-side question is not "did the HTTP call work" but "how much
    # cleanup did that just buy me".
    OUTCOMES = ("created", "stocked", "unresolved", "error")

    def set(self, device, location_id, location_name):
        """
        Point a gun at a shelf, and START THE COUNT AT ZERO.

        The count exists to answer the only question worth asking mid-shelf --
        is this cupboard done? -- so it has to be per location, not per
        session. Carrying a running total across a new location code would
        make the number meaningless exactly when it is being read.
        """
        with self._lock:
            self._by_device[self._key(device)] = {
                "id": location_id, "name": location_name, "at": time.monotonic(),
                "counts": {k: 0 for k in self.OUTCOMES}}

    def bump(self, device, outcome):
        """
        Record a product scan against the gun's current shelf.

        Also counts as activity, so it resets the idle clock -- scanning is
        exactly what "not idle" means, and a separate touch() call that
        somebody could forget is how the clock ends up expiring mid-shelf.

        Silently does nothing when the gun has no location set: not every scan
        happens during an inventory, and a scan with no shelf is the normal
        case rather than an error.
        """
        if outcome not in self.OUTCOMES:
            return
        with self._lock:
            entry = self._by_device.get(self._key(device))
            if not entry:
                return
            if time.monotonic() - entry["at"] > self.idle_seconds:
                return
            entry["at"] = time.monotonic()
            entry.setdefault("counts", {k: 0 for k in self.OUTCOMES})
            entry["counts"][outcome] = entry["counts"].get(outcome, 0) + 1

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

    def clear_key(self, key):
        """
        Forget a gun's location by its TRACKER KEY rather than a device path.

        The UI only ever sees keys -- `snapshot()` is keyed that way -- and a
        person pressing "done with this shelf" is looking at a card, not
        holding a /dev/hidrawN. Resolving a key back to some device path just
        to hand it to `clear()` would mean guessing which of a gun's several
        nodes to name.

        Returns True if something was actually forgotten, so the caller can
        say so rather than reporting success for a shelf that had already
        expired on its own.
        """
        with self._lock:
            return self._by_device.pop(str(key), None) is not None

    def clear_all(self):
        """Forget every gun. Returns how many were cleared."""
        with self._lock:
            count = len(self._by_device)
            self._by_device.clear()
            return count

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
        """
        For the UI. Reports remaining seconds so a gun can show its clock, and
        the per-outcome counts since this shelf was set.

        The clock is the safety net for the 10-minute expiry: without it on
        screen the expiry is a silent trap rather than a guard, and you find
        out by discovering forty things in Big Pantry.
        """
        now = time.monotonic()
        with self._lock:
            out = {}
            for k, v in self._by_device.items():
                if now - v["at"] > self.idle_seconds:
                    continue
                counts = v.get("counts") or {c: 0 for c in self.OUTCOMES}
                out[k] = {
                    "id": v["id"], "name": v["name"],
                    "expires_in": max(0, int(self.idle_seconds - (now - v["at"]))),
                    "counts": dict(counts),
                    "scanned": sum(counts.values()),
                }
            return out


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

    # Match the STORED slug where there is one, and fall back to the computed
    # name only for locations that predate the field. A label carries the slug
    # it was printed with; the name may have moved on since.
    matches = [l for l in (locations or [])
               if (stored_slug(l) or slug(l.get("name"))) == wanted]
    if not matches:
        return None, f"no Grocy location matches '{wanted}'"
    if len(matches) > 1:
        names = ", ".join(str(m.get("name")) for m in matches)
        return None, (f"'{wanted}' is ambiguous -- {names} all slug the same. "
                      "Rename one in Grocy.")
    return matches[0], None
