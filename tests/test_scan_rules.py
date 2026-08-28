"""
The rules that decide what becomes a product and what gets counted.

Both have already cost something. `is_product_code` exists because a scanner
reads whatever is printed on a jar, and two QR payloads became products --
one of them a real jar of Prime Time Buttery Beef Rub whose actual UPC was
already in the catalogue. `_tidy` exists because providers return their own
formatting accidents, and it once threw on every single scan.
"""

import grocy
import main


# ---------- what can become a product ----------

def test_a_qr_payload_is_not_a_product_code():
    """Both of these became real products before this guard existed."""
    assert not main.is_product_code(
        "HTTPSWWWPSSEASONINGCOMBLOGSRECIPESTAGGEDPRIME-TIME-BUTTERY-BEEF-RUB")
    assert not main.is_product_code("HTTPCONGRANETUGTFTQQ")


def test_a_five_digit_penzeys_number_IS_a_product_code():
    """
    The line is NUMERIC, not GTIN-valid. A blanket non-GTIN refusal would make
    Penzeys unscannable -- their bags carry 5-digit item numbers, and half the
    spice cabinet comes from there.
    """
    assert main.is_product_code("55540")


def test_a_nineteen_digit_retailer_code_IS_a_product_code():
    """Wild Fork prints one. A real product, and not a GTIN length."""
    assert main.is_product_code("2000000000000123456"[:19])


def test_real_gtins_pass():
    for code in ("049000006346", "8410076472397", "04308504"):
        assert main.is_product_code(code), code


def test_blank_and_none_are_refused_without_throwing():
    assert not main.is_product_code("")
    assert not main.is_product_code(None)
    assert not main.is_product_code("   ")


def test_surrounding_whitespace_does_not_disqualify_a_code():
    """A gun can emit a trailing character; that is not a QR payload."""
    assert main.is_product_code("  55540\n".strip())
    assert main.is_product_code(" 049000006346 ")


# ---------- what gets counted at a shelf ----------

def test_mode_quantity_and_location_scans_are_not_inventory():
    """
    Otherwise "23 items in Spice Cabinet" quietly includes the four times you
    flipped the gun to CONSUME, and the only number worth reading is wrong.
    """
    for status in ("mode", "quantity", "location", "pending", "not_found"):
        assert main._scan_outcome({"status": status}) == "", status


def test_a_created_product_and_a_placeholder_are_counted_apart():
    """That distinction IS the review tally."""
    assert main._scan_outcome(
        {"status": "success", "outcome": "created"}) == "created"
    assert main._scan_outcome(
        {"status": "success", "outcome": "unresolved"}) == "unresolved"


def test_a_success_with_no_outcome_set_counts_as_stocked():
    """A branch that predates the field must not silently stop counting."""
    assert main._scan_outcome({"status": "success"}) == "stocked"


def test_an_error_is_counted_as_an_error():
    assert main._scan_outcome({"status": "error"}) == "error"


# ---------- the picture URL ----------

def test_a_product_with_a_picture_gets_a_proxy_path():
    """
    Relative, so it survives ingress. Not Grocy's own URL: that endpoint wants
    the API key in a header, which an <img> cannot send.
    """
    assert main._picture_url(
        {"id": 50, "picture_file_name": "55540.jpg"}) == "api/picture/50"


def test_a_product_without_one_gets_nothing():
    """No picture is a normal outcome, so this must be empty, not a URL."""
    assert main._picture_url({"id": 50, "picture_file_name": None}) == ""
    assert main._picture_url({"id": 50}) == ""
    assert main._picture_url({"picture_file_name": "x.jpg"}) == ""
    assert main._picture_url(None) == ""


# ---------- tidying provider output ----------

def test_doubled_spaces_are_collapsed():
    """
    Real provider output. This value reaches a shopping list, which somebody
    reads in a shop.
    """
    assert grocy._tidy("Spectrum,  The Hain Celestial Group  Inc.") == \
        "Spectrum, The Hain Celestial Group Inc."


def test_newlines_and_tabs_count_as_whitespace():
    assert grocy._tidy("Mustard\tSeed,\n Brown") == "Mustard Seed, Brown"


def test_none_and_blank_survive():
    """
    _tidy is called on optional fields on every scan path. It once threw --
    a missing `import re` -- and every new product lost its barcode silently
    for days.
    """
    assert grocy._tidy(None) == ""
    assert grocy._tidy("") == ""
    assert grocy._tidy("   ") == ""


def test_a_blank_value_produces_no_userfield_at_all():
    """A blank overwrite is still a write, and would erase a known brand."""
    assert grocy._barcode_userfields("", "") == {}
    assert grocy._barcode_userfields("  ", "penzeys") == {"source": "penzeys"}
    assert grocy._barcode_userfields("Penzeys", "penzeys") == {
        "brand": "Penzeys", "source": "penzeys"}


# ---------- the feed resolves pictures LATE ----------

def _recent(payload):
    """Call /api/scans/recent against a hand-placed history."""
    main.recent_scans[:] = payload
    with main.app.test_client() as c:
        return c.get("/api/scans/recent?n=6").get_json()["scans"]


def test_a_picture_acquired_after_the_scan_still_reaches_the_feed():
    """
    The bug this pins: `image` used to be stamped onto the record when the gun
    beeped, so a photograph the backfill fetched an hour later never appeared
    -- the feed showed a placeholder for a product Grocy had a picture of, and
    only a rescan could fix it.

    The endpoint emits the proxy path whenever it knows the product id. The
    proxy re-reads Grocy and 404s while there is still no picture, so this is
    cheap and self-healing rather than optimistic.
    """
    got = _recent([{"barcode": "793888658370", "product": "Blue Goose Field Pea",
                    "product_id": 144, "image": ""}])
    assert got[0]["image"] == "api/picture/144"


def test_an_image_the_scan_already_found_is_left_alone():
    """A provider's own URL is absolute and must not be replaced by the proxy."""
    got = _recent([{"product_id": 7, "image": "https://example.test/a.jpg"}])
    assert got[0]["image"] == "https://example.test/a.jpg"


def test_a_scan_with_no_product_gets_no_url():
    """A mode switch or an unresolved barcode has no product to photograph."""
    got = _recent([{"barcode": "55540", "image": ""}])
    assert got[0]["image"] == ""


def test_the_stored_history_is_not_mutated():
    """
    The record is what was true at the scan. Resolving into a copy keeps the
    history honest and stops the value being frozen again on the first read.
    """
    history = [{"product_id": 144, "image": ""}]
    _recent(history)
    assert history[0]["image"] == ""


# ---------- every scanned product is tracked ----------

def test_no_creation_path_leaves_a_product_without_a_reorder_point():
    """
    ADD mode used to create products with min_stock_amount 0, at four separate
    sites (`0 if mode == 'add' else 1`). A product with no minimum can never
    reach GetMissingProducts, so it is carried forever and never asked for --
    which is not tracking, it is a list. One evening's intake made 34 of them.

    Tony, 2026-08-27: "i do not care to track it if it doesn't have a minimum."

    Source-level because the four sites sit deep inside handle_barcode's
    branching and a behavioural test would have to reproduce the whole scan
    path. The thing worth pinning is that the mode-dependent form does not come
    back -- it read as deliberate and was easy to miss in review.
    """
    import inspect
    import pathlib

    src = pathlib.Path(inspect.getsourcefile(main)).read_text()
    offenders = [ln.strip() for ln in src.splitlines()
                 if "min_stock" in ln and "mode" in ln and "=" in ln
                 and not ln.strip().startswith("#")]
    assert not offenders, f"mode-dependent minimum is back: {offenders}"


def test_the_penzeys_refill_exceptions_are_deliberate_and_survive():
    """
    The blanket rule has exactly two exceptions, both load-bearing:

      * the generic PARENT -- no_own_stock is a database CHECK, so it can never
        hold stock; a minimum on it would put the generic name on the shopping
        list instead of the container you actually buy
      * the JAR child -- it is refilled from the bag, so a minimum would ask you
        to buy something you never buy

    Pinned because "every product gets a minimum" is exactly the kind of tidy
    rule that would quietly delete them.
    """
    import inspect

    src = inspect.getsource(main.penzeys_hierarchy)
    assert "min_stock_amount=0," in src, "the generic parent must stay at 0"
    assert 'min_stock_amount=1 if container == "bag" else 0' in src, \
        "the jar must stay at 0 and the bag at 1"
