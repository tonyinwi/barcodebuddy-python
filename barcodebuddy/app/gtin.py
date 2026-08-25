"""
GTIN predicates, in one place so two providers cannot drift apart on them.

Both rules here were paid for. `is_gtin` exists because Open Food Facts
zero-pads a 5-digit Penzeys code into a valid EAN-8 and confidently returns a
Dutch baby food. `same_gtin` exists because the obvious fix -- demand the
provider echo the code back character-for-character -- rejects upcdatabase.org's
own correct answers, since it returns 049000006346 as 0049000006346.
"""

GTIN_LENGTHS = (8, 12, 13, 14)


def is_gtin(barcode) -> bool:
    """A real global trade number, or a shop's own item number?"""
    code = (barcode or "").strip()
    return code.isdigit() and len(code) in GTIN_LENGTHS


def same_gtin(returned, queried) -> bool:
    """
    Is the code we got back the SAME GTIN we asked about?

    Not a string comparison: GTIN-8/12/13/14 are one number at four widths, so
    compare zero-padded to 14. Still catches a provider handing back a different
    product, which is the whole job. The zero-padding trap is stopped earlier by
    is_gtin() -- a 5-digit code never reaches a provider at all.
    """
    a = "".join(c for c in str(returned or "") if c.isdigit())
    b = "".join(c for c in str(queried or "") if c.isdigit())
    return bool(a) and bool(b) and a.zfill(14) == b.zfill(14)
