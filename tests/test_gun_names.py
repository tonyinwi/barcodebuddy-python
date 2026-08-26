"""
Naming the guns, and ending a shelf early.

One physical gun is several `/dev/hidrawN` nodes -- here 0581:011a is hidraw0
and hidraw1, and a YuRiot 0461:4d86 is hidraw2 and hidraw3. Every identity in
this fork is therefore keyed on the USB id, and names have to be too: naming a
node would name the same gun twice and come undone at the next replug, because
node numbering shifts.
"""

import config as config_mod
import locations as loc


def _config(**options):
    c = config_mod.Config.__new__(config_mod.Config)
    c._config = options
    return c


# ---------- parsing ----------

def test_names_are_keyed_on_the_usb_id():
    c = _config(scanner_names="0581:011a = Pantry gun")
    assert c.gun_label("usb:0581:011a") == "Pantry gun"


def test_both_nodes_of_one_gun_get_the_same_name():
    """The point of keying on the USB id rather than the device path."""
    c = _config(scanner_names="0581:011a = Pantry gun")
    tracker = loc.LocationTracker()
    tracker.usb_resolver = lambda d: "0581:011a"       # hidraw0 and hidraw1
    assert c.gun_label(tracker._key("/dev/hidraw0")) == "Pantry gun"
    assert c.gun_label(tracker._key("/dev/hidraw1")) == "Pantry gun"


def test_several_guns_on_separate_lines():
    c = _config(scanner_names="0581:011a = Pantry gun\n0461:4d86 = YuRiot")
    assert c.gun_label("usb:0581:011a") == "Pantry gun"
    assert c.gun_label("usb:0461:4d86") == "YuRiot"


def test_commas_work_too():
    c = _config(scanner_names="0581:011a = A, 0461:4d86 = B")
    assert c.gun_label("usb:0581:011a") == "A"
    assert c.gun_label("usb:0461:4d86") == "B"


def test_case_and_spacing_do_not_matter():
    c = _config(scanner_names="  0581:011A   =   Pantry gun  ")
    assert c.gun_label("usb:0581:011a") == "Pantry gun"


def test_an_unnamed_gun_falls_back_to_its_key():
    """
    Never blank. An empty label on a card is worse than an ugly one -- you
    cannot tell which gun the card belongs to at all.
    """
    c = _config(scanner_names="0581:011a = Pantry gun")
    assert c.gun_label("usb:0461:4d86") == "usb:0461:4d86"
    assert _config().gun_label("usb:0581:011a") == "usb:0581:011a"


def test_junk_lines_are_ignored_not_fatal():
    """A typo in an option must not take the scanner offline."""
    c = _config(scanner_names="nonsense\n\n0581:011a = Pantry gun\n= nameless\nx =")
    assert c.scanner_names == {"0581:011a": "Pantry gun"}


def test_an_unresolvable_device_key_can_also_be_named():
    """`dev:` keys happen where sysfs is unreadable; naming still works."""
    c = _config(scanner_names="dev:/dev/hidraw0 = The odd one")
    assert c.gun_label("dev:/dev/hidraw0") == "The odd one"


# ---------- ending a shelf early ----------

def _tracker(usb="0581:011a"):
    t = loc.LocationTracker()
    t.usb_resolver = lambda d: usb
    return t


def test_clearing_by_key_forgets_the_shelf():
    """
    The UI only ever sees keys. Resolving one back to some device path just to
    call clear() would mean guessing which of a gun's nodes to name.
    """
    t = _tracker()
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    assert t.clear_key("usb:0581:011a") is True
    assert t.current("/dev/hidraw0") is None
    assert t.snapshot() == {}


def test_clearing_an_unknown_key_reports_that_nothing_happened():
    """So the caller can say so rather than claiming success."""
    assert _tracker().clear_key("usb:dead:beef") is False


def test_clearing_one_gun_leaves_the_other_alone():
    t = loc.LocationTracker()
    t.usb_resolver = lambda d: {"/dev/hidraw0": "0581:011a"}.get(d, "0461:4d86")
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    t.set("/dev/hidraw2", 4, "Big Pantry")
    t.clear_key("usb:0581:011a")
    assert list(t.snapshot()) == ["usb:0461:4d86"]


def test_clear_all_reports_how_many():
    t = loc.LocationTracker()
    t.usb_resolver = lambda d: {"/dev/hidraw0": "0581:011a"}.get(d, "0461:4d86")
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    t.set("/dev/hidraw2", 4, "Big Pantry")
    assert t.clear_all() == 2
    assert t.snapshot() == {}


def test_a_cleared_gun_starts_counting_again_from_zero():
    """Clearing reverts to the preset; the next shelf is a fresh count."""
    t = _tracker()
    t.set("/dev/hidraw0", 3, "Spice Cabinet")
    t.bump("/dev/hidraw0", "stocked")
    t.clear_key("usb:0581:011a")
    t.set("/dev/hidraw0", 4, "Big Pantry")
    assert t.snapshot()["usb:0581:011a"]["scanned"] == 0
