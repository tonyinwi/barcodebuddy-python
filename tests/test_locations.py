"""
The location tracker, which is this fork's subtlest module.

It has already produced one latent bug of exactly the kind that never raises:
the tracker keyed on the DEVICE NODE while `resolve_scan_mode()` keyed on the
USB id. One gun presents as several `/dev/hidrawN` nodes -- 0581:011a is both
hidraw0 and hidraw1 here -- so a location set while the gun emitted on one node
silently stopped applying the moment it emitted on the other, and everything
landed on the preset shelf. Nothing errored. It only happened not to bite
because that day's scans all came through one node.

That is the whole reason these tests exist: every failure this module can have
is silent, and shows up as products on the wrong shelf a week later.
"""

import time

import locations as loc


def _tracker(idle=600, usb="0581:011a"):
    t = loc.LocationTracker(idle_seconds=idle)
    t.usb_resolver = lambda device: usb
    return t


# ---------- one gun is several device nodes ----------

def test_one_gun_on_two_nodes_is_one_gun():
    t = _tracker()
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    assert t.current("/dev/hidraw1")["name"] == "Spice Cabinet"


def test_two_different_guns_are_kept_apart():
    t = loc.LocationTracker()
    t.usb_resolver = lambda d: {"/dev/hidraw0": "aaaa:1111"}.get(d, "bbbb:2222")
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    t.set("/dev/hidraw9", 4, "Big Pantry")
    assert t.current("/dev/hidraw0")["name"] == "Spice Cabinet"
    assert t.current("/dev/hidraw9")["name"] == "Big Pantry"


def test_an_unresolvable_device_still_gets_state():
    """Better a shared key than dropping the feature where sysfs is unreadable."""
    t = loc.LocationTracker()
    t.usb_resolver = lambda d: None
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    assert t.current("/dev/hidraw0")["name"] == "Spice Cabinet"


def test_a_resolver_that_throws_does_not_break_scanning():
    t = loc.LocationTracker()
    def boom(device):
        raise OSError("sysfs went away")
    t.usb_resolver = boom
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    assert t.current("/dev/hidraw0")["name"] == "Spice Cabinet"


# ---------- expiry, which is a guard and not a nuisance ----------

def test_a_stale_location_expires():
    t = _tracker(idle=1)
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    time.sleep(1.2)
    assert t.current("/dev/hidraw0") is None


def test_a_scan_counts_as_activity():
    """
    The clock is per gun and resets on use, so a long shelf does not expire
    mid-count. bump() does the touching, so there is no separate call to
    forget.
    """
    t = _tracker(idle=2)
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    time.sleep(1.2)
    t.bump("/dev/hidraw0", "stocked")
    time.sleep(1.2)
    assert t.current("/dev/hidraw0") is not None


def test_clear_removes_the_entry_entirely():
    """
    A rejected location code CLEARS the gun rather than leaving the previous
    shelf in effect -- otherwise the next fifty products go to the old one.
    """
    t = _tracker()
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    t.clear("/dev/hidraw1")                      # the other node, same gun
    assert t.current("/dev/hidraw0") is None
    assert t.snapshot() == {}


# ---------- the per-shelf count ----------

def test_counts_start_at_zero_and_add_up():
    t = _tracker()
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    for outcome in ("stocked", "stocked", "created", "unresolved", "error"):
        t.bump("/dev/hidraw0", outcome)
    snap = t.snapshot()["usb:0581:011a"]
    assert snap["scanned"] == 5
    assert snap["counts"] == {"created": 1, "stocked": 2, "unresolved": 1, "error": 1}


def test_a_new_shelf_resets_the_count():
    """
    The number answers "is this cupboard done", so it has to be per location.
    Carrying a total across shelves makes it meaningless exactly when it is
    being read.
    """
    t = _tracker()
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    t.bump("/dev/hidraw0", "stocked")
    t.set("/dev/hidraw0", 4, "Big Pantry")
    snap = t.snapshot()["usb:0581:011a"]
    assert snap["scanned"] == 0 and snap["name"] == "Big Pantry"


def test_an_unknown_outcome_is_ignored():
    t = _tracker()
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    t.bump("/dev/hidraw0", "banana")
    assert t.snapshot()["usb:0581:011a"]["scanned"] == 0


def test_a_scan_with_no_shelf_set_is_a_no_op():
    """Not every scan happens during an inventory. That is normal, not an error."""
    t = _tracker()
    t.bump("/dev/hidraw0", "stocked")
    assert t.snapshot() == {}


def test_an_expired_gun_leaves_the_snapshot():
    t = _tracker(idle=1)
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    time.sleep(1.2)
    assert t.snapshot() == {}


def test_the_snapshot_reports_a_countdown():
    t = _tracker(idle=600)
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    assert 590 <= t.snapshot()["usb:0581:011a"]["expires_in"] <= 600


# ---------- the barcodes themselves ----------

def test_a_slug_is_descriptive_not_numeric():
    """QR removes the length penalty, and a label taped to a shelf gets read."""
    assert loc.barcode_for("Oils & Vinegar") == "BBUDDY-LOC-OILS-VINEGAR"
    assert loc.barcode_for("Big Pantry") == "BBUDDY-LOC-BIG-PANTRY"


def test_resolve_matches_a_location():
    rows = [{"id": 3, "name": "Spice Cabinet"}, {"id": 4, "name": "Big Pantry"}]
    found, err = loc.resolve("BBUDDY-LOC-SPICE-CABINET", rows)
    assert err is None and found["id"] == 3


def test_a_non_location_barcode_is_not_an_error():
    """A product barcode must fall through, not be rejected."""
    found, err = loc.resolve("049000006346", [{"id": 3, "name": "Spice Cabinet"}])
    assert found is None and err is None


def test_an_unknown_slug_is_an_error():
    found, err = loc.resolve("BBUDDY-LOC-ATTIC", [{"id": 3, "name": "Spice Cabinet"}])
    assert found is None and err


def test_an_AMBIGUOUS_slug_is_an_error_and_never_a_guess():
    """Silently picking one is how the wrong shelf reaches hundreds of products."""
    rows = [{"id": 3, "name": "Big Pantry"}, {"id": 9, "name": "Big  Pantry"}]
    found, err = loc.resolve("BBUDDY-LOC-BIG-PANTRY", rows)
    assert found is None and err
