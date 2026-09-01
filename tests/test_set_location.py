"""
Pointing a gun at a shelf from the UI, with no barcode to scan.

The printed label stays the fast path at a shelf. This covers what the label
cannot serve -- an unprinted shelf, a code that will not read, or starting an
inventory from the laptop -- and the failure it must not have is the same one
the scanned path guards: a wrong shelf files every product after it in the
wrong room.
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "barcodebuddy" / "app"))

from locations import LocationTracker                            # noqa: E402


def test_set_key_points_a_gun_at_a_shelf():
    t = LocationTracker()
    t.set_key("usb:0581:011a", 7, "Kitchen - Big Pantry")
    snap = t.snapshot()
    assert snap["usb:0581:011a"]["id"] == 7
    assert snap["usb:0581:011a"]["name"] == "Kitchen - Big Pantry"


def test_set_key_zeroes_the_counts_like_a_scanned_code_does():
    """
    The per-shelf count answers *is this cupboard done?*, so it has to restart
    when the shelf changes. Carrying a running total across a new location
    would make the number meaningless exactly when it is being read -- and the
    UI path must not differ from the scanned one here.
    """
    t = LocationTracker()
    t.usb_resolver = lambda dev: "x"
    t.set_key("usb:x", 7, "Big Pantry")
    t.bump("/dev/hidraw0", "created")
    t.bump("/dev/hidraw0", "stocked")
    assert t.snapshot()["usb:x"]["counts"]["created"] == 1, "precondition"

    t.set_key("usb:x", 9, "Cleaning Pantry")
    assert t.snapshot()["usb:x"]["counts"] == {k: 0 for k in LocationTracker.OUTCOMES}


def test_set_key_uses_the_key_VERBATIM_not_a_device_node():
    """
    ONE GUN IS SEVERAL /dev/hidrawN NODES.

    `set()` takes a device and derives the key; the UI already holds the key,
    and resolving it back to a device would pick ONE of a gun's several nodes
    -- reintroducing the exact bug `_key()` exists to prevent, where a location
    set on hidraw0 silently stops applying when the gun emits on hidraw1.
    """
    t = LocationTracker()
    t.set_key("usb:0581:011a", 7, "Big Pantry")
    assert "usb:0581:011a" in t.snapshot()
    assert not any(k.startswith("dev:") for k in t.snapshot())


def test_set_key_and_clear_key_agree_on_the_same_identity():
    """The UI sets and clears the same gun; the two must key alike."""
    t = LocationTracker()
    t.set_key("usb:0581:011a", 7, "Big Pantry")
    assert t.clear_key("usb:0581:011a") is True
    assert t.snapshot() == {}


def test_a_scanned_code_and_the_ui_produce_the_same_state():
    """
    Two ways in, one shape out -- otherwise the shelf card, the expiry and the
    counts all have to know which route set them.
    """
    scanned, typed = LocationTracker(), LocationTracker()
    scanned.usb_resolver = lambda dev: "0581:011a"
    scanned.set("/dev/hidraw0", 7, "Kitchen - Big Pantry")
    typed.set_key("usb:0581:011a", 7, "Kitchen - Big Pantry")

    a = scanned.snapshot()["usb:0581:011a"]
    b = typed.snapshot()["usb:0581:011a"]
    assert a.keys() == b.keys()
    assert (a["id"], a["name"], a["counts"]) == (b["id"], b["name"], b["counts"])
