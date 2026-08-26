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
