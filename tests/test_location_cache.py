"""
Adding a location in Grocy has to reach the scanner, and it did not.

`get_locations()` cached forever. That looked like a stale printed sheet and
was worse: the SCAN PATH resolves location codes against the same list, so a
shelf added in Grocy after the add-on started was invisible to `loc.resolve()`.
Its freshly printed QR came back as *unknown location code* -- and a rejected
code CLEARS the gun by design, so every scan after it landed silently on the
preset shelf.

That is exactly the misfiling the location feature exists to prevent, caused
by the feature itself, and nothing would have raised.

The other half matters as much: a failed fetch must KEEP the old list. Blanking
it on a momentary Grocy blip would make every location code unknown at once,
clearing every gun.
"""

import time

from grocy import GrocyClient


class Recording(GrocyClient):
    """Counts fetches and can be told to fail, without any network."""

    def __init__(self, rows):
        super().__init__("http://grocy", "key")
        self.rows = rows
        self.calls = 0
        self.fail = False

    def _request(self, method, endpoint, retry=True, **kwargs):
        self.calls += 1
        return None if self.fail else list(self.rows)


SHELVES = [{"id": 1, "name": "Big Pantry"}]


def test_the_scan_path_is_served_from_cache():
    """A person is standing there waiting for the QR to resolve."""
    c = Recording(SHELVES)
    for _ in range(5):
        c.get_locations()
    assert c.calls == 1


def test_a_new_shelf_appears_once_the_ttl_passes():
    c = Recording(SHELVES)
    c.LOCATIONS_TTL = 0.1
    assert len(c.get_locations()) == 1
    c.rows = SHELVES + [{"id": 2, "name": "Basement Pantry"}]
    time.sleep(0.15)
    assert [r["name"] for r in c.get_locations()] == ["Big Pantry", "Basement Pantry"]


def test_force_bypasses_the_cache_entirely():
    """
    What printing the label sheet uses. Nobody is waiting, and a sheet printed
    from a stale list is a label for a shelf the scanner will reject.
    """
    c = Recording(SHELVES)
    c.get_locations()
    c.rows = SHELVES + [{"id": 2, "name": "Basement Pantry"}]
    assert len(c.get_locations(force=True)) == 2
    assert c.calls == 2


def test_a_failed_fetch_keeps_the_previous_list():
    """Stale beats empty: an empty list clears every gun at once."""
    c = Recording(SHELVES)
    assert len(c.get_locations()) == 1
    c.fail = True
    assert [r["name"] for r in c.get_locations(force=True)] == ["Big Pantry"]


def test_a_failure_on_the_very_first_call_is_an_empty_list_not_a_crash():
    c = Recording(SHELVES)
    c.fail = True
    assert c.get_locations() == []


def test_a_failed_forced_fetch_does_not_stamp_the_cache_fresh():
    """
    Otherwise a blip during a forced refresh buys the stale list another full
    TTL, and the shelf you just added stays invisible for five more minutes.
    """
    c = Recording(SHELVES)
    c.LOCATIONS_TTL = 0.1
    c.get_locations()
    c.fail = True
    c.get_locations(force=True)
    c.fail = False
    c.rows = SHELVES + [{"id": 2, "name": "Basement Pantry"}]
    time.sleep(0.15)
    assert len(c.get_locations()) == 2
