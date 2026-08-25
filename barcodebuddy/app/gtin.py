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


def check_digit_ok(barcode) -> bool:
    """
    Does the GTIN's own check digit validate?

    `is_gtin` only checks LENGTH, which lets a mis-scan or a typo through to
    every provider. UPCitemdb answers such a code with HTTP 400 INVALID_UPC --
    correct of it, but the client recorded that as `error`, so our own bad input
    polluted the provider's error rate. upcdatabase would have called the same
    code a plain miss, which makes the two providers look different for a reason
    that has nothing to do with either.

    ⚠️ DIAGNOSTIC ONLY -- do NOT gate lookups on this. Tested against every real
    barcode in the catalogue: 19 of 20 pass, and the one that fails is
    `04308504`, which RESOLVES CORRECTLY -- USDA returns "SWEET TEA LIQUID WATER
    ENHANCER" for it and echoes the code back exactly. Real barcodes with bad
    check digits exist in the wild and in provider databases, so rejecting them
    would break intake for products that work today. It is useful for flagging a
    probable mis-scan, and nothing more.

    Standard mod-10: pad to 14, weight the 13-digit body alternately 3 and 1
    from the left, and the check digit is (10 - sum mod 10) mod 10.
    """
    digits = [int(c) for c in str(barcode or "") if c.isdigit()]
    if len(digits) not in GTIN_LENGTHS:
        return False
    body, check = digits[:-1], digits[-1]
    body = [0] * (13 - len(body)) + body
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(body))
    return (10 - total % 10) % 10 == check
