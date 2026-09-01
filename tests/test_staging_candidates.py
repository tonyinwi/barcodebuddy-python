"""
Offering the shopping-list lines a scan might be.

155 rows on the list are text the household typed into AnyList. A scan produces
a specific branded name and a staging line is a generic human phrase --
`MiraLAX` against `Magic eraser · Walmart`, `Tide` against `Dawn powerwash ·
Walmart` -- so an exact match fires for almost nothing, and a fuzzy match that
ACTED on its own would be the wrong-join failure this project refuses
everywhere else.

Hence: ranked, offered, tapped. The person is holding the item, which is the
cheapest moment the question will ever be asked and the only one where the
answer is certain.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "barcodebuddy" / "app"))

import main                                                       # noqa: E402


class _Grocy:
    def __init__(self, notes):
        self.rows = [{"id": 100 + i, "note": n, "shopping_list_id": 1}
                     for i, n in enumerate(notes)]

    def free_text_rows(self, list_id=1):
        return self.rows


def _with(notes):
    main.grocy_client = _Grocy(notes)


def test_it_offers_the_line_that_shares_a_word():
    _with(["Dawn powerwash · Walmart", "Toilet paper · Walmart", "Coffee"])
    hits = main.staging_candidates("Dawn Platinum Powerwash Dish Spray")
    assert hits and "Dawn powerwash" in hits[0]["note"]


def test_it_offers_NOTHING_when_nothing_shares_a_word():
    """
    An ordinary scan must look exactly as it did. A block that appears on every
    scan saying "no matches" is the alarm-about-nothing this project keeps
    deleting.
    """
    _with(["Toilet paper · Walmart", "Coffee"])
    assert main.staging_candidates("MiraLAX") == []


def test_the_store_suffix_is_not_matched_on():
    """
    ` · Walmart` is the importer's, not the item's. Matching on it would offer
    every Walmart line for every Walmart product.
    """
    _with(["Bar soap · Walmart", "Kleenex · Walmart"])
    assert main.staging_candidates("Walmart Great Value Bleach") == []


def test_packaging_words_do_not_create_a_match():
    """`oz`, `ct`, `large` are on half the lines and mean nothing."""
    _with(["Hefty Slider Jumbo 2.5 Gal · Walmart"])
    assert main.staging_candidates("Blue Diamond Almonds, 6 oz") == []


def test_more_shared_words_ranks_higher():
    _with(["Glad Press'N Seal · Walmart", "Glad Cling Wrap 300 sq ft · Walmart"])
    hits = main.staging_candidates("Glad Cling Wrap Multipurpose")
    assert "Cling Wrap" in hits[0]["note"], "the two-word overlap should lead"


def test_at_most_three_are_offered():
    _with([f"coffee thing {i} · Walmart" for i in range(9)])
    assert len(main.staging_candidates("Coffee")) <= 3


def test_no_grocy_client_is_silent_not_an_error():
    """A scan must never fail because a diagnostic could not run."""
    main.grocy_client = None
    assert main.staging_candidates("anything") == []
