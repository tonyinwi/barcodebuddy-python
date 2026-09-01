"""
A bin scan of something already at zero stock.

Found live 2026-09-01: Tony scanned a pile of items into the bin and got
`❌ Failed to remove: floss picks (no stock?)` for products that were **already
on the shopping list**, put there the previous evening. The message was false
twice over -- it called a correct outcome an error, and it hid the one fact
worth knowing.

BINNED IS DEMAND. `CLAUDE.md`'s own flow diagram has said so since it was drawn
(`BINGUN --> "binned = demand"`), but the implementation only ever called
consume, which cannot express demand for something you have none of:
`ConsumeProduct()` throws unconditionally when amount > stock, there is no
setting, and a FAILED consume is not a stock transaction -- so
`shopping_list_auto_add_below_min_stock_amount` never fires either.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "barcodebuddy" / "app"))


class FakeGrocy:
    """Only the three calls the bin branch makes."""

    def __init__(self, on_list=False, list_accepts=True):
        self._on_list = on_list
        self._accepts = list_accepts
        self.added = []

    def consume_product(self, product_id, amount=1.0):
        return False                      # always the zero-stock case here

    def shopping_list_has(self, product_id, list_id=1):
        return self._on_list

    def add_to_shopping_list(self, product_id, amount=1.0, list_id=1):
        if not self._accepts:
            return False
        self.added.append((product_id, amount))
        self._on_list = True
        return True


def _bin_scan(grocy, product_id=402, name="floss picks"):
    """The branch under test, transcribed from main.py."""
    if grocy.consume_product(product_id, 1):
        return "success", f"➖ Removed: {name}"
    if grocy.shopping_list_has(product_id):
        return "success", f"🛒 {name} — already on your list"
    if grocy.add_to_shopping_list(product_id):
        return "success", f"🛒 ➖ {name} — added to your list"
    return "error", f"❌ {name}: no stock to remove, and could not reach the shopping list"


def test_already_on_the_list_is_a_SUCCESS_and_says_so():
    """
    The exact case that produced three red crosses. `floss picks` and
    `livfresh toothpaste gel` had shopping-list rows created 2026-08-31
    19:50:30 -- a day before the bin run.
    """
    g = FakeGrocy(on_list=True)
    status, message = _bin_scan(g)
    assert status == "success"
    assert "already on your list" in message
    assert g.added == [], "it is already there; adding again would bump the amount"


def test_not_on_the_list_gets_added():
    """
    The gap a minimum cannot close. A product at `min_stock_amount = 0` --
    a refill jar, an untracked food, anything deliberately not reordered --
    never reaches the list by auto-add, so binning it did nothing at all.
    """
    g = FakeGrocy(on_list=False)
    status, message = _bin_scan(g)
    assert status == "success"
    assert "added to your list" in message
    assert g.added == [(402, 1.0)]


def test_only_a_real_failure_is_reported_as_one():
    g = FakeGrocy(on_list=False, list_accepts=False)
    status, message = _bin_scan(g)
    assert status == "error"
    assert "could not reach the shopping list" in message


def test_the_branch_in_main_py_matches_this_shape():
    """
    The transcription above is only worth something if it still matches. Pins
    the three outcomes by their message text, which is what a person at a bin
    actually reads.
    """
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "barcodebuddy" / "app" / "main.py").read_text()
    for phrase in ("already on your list", "added to your list",
                   "could not reach the shopping list",
                   "shopping_list_has", "add_to_shopping_list"):
        assert phrase in src, f"main.py no longer contains {phrase!r}"
    assert "Failed to remove:" not in src, "the false message is back"
