"""
A printed label outlives the name it was generated from.

`barcode_for(name)` recomputes the slug from the current name every time,
which is correct right up until somebody renames a shelf in Grocy. Then every
label already stuck to that shelf stops resolving — and a rejected location
code CLEARS the gun by design, so everything scanned afterwards silently lands
on the preset. That is the exact misfiling the feature exists to prevent,
caused by an edit in a web form.

It is not hypothetical: renaming "Basement Pantry" to "Basement - Cleaning
Pantry" changes BBUDDY-LOC-BASEMENT-PANTRY to
BBUDDY-LOC-BASEMENT-CLEANING-PANTRY.

So the slug is persisted in a `slug` userfield on the Grocy location, frozen
the moment it goes on paper, and never changed after.
"""

import locations as loc


def L(name, slug=None, id=1):
    row = {"id": id, "name": name}
    if slug is not None:
        row["userfields"] = {"slug": slug}
    return row


# ---------- which slug wins ----------

def test_a_stored_slug_beats_the_name():
    """The label is authoritative; the display name is free to change."""
    row = L("Basement - Cleaning Pantry", "BASEMENT-PANTRY")
    assert loc.barcode_for_location(row) == "BBUDDY-LOC-BASEMENT-PANTRY"


def test_no_stored_slug_falls_back_to_the_name():
    """Locations that predate the field keep working."""
    assert loc.barcode_for_location(L("Big Pantry")) == "BBUDDY-LOC-BIG-PANTRY"
    assert loc.barcode_for_location(L("Big Pantry", None)) == "BBUDDY-LOC-BIG-PANTRY"


def test_an_empty_stored_slug_is_not_a_slug():
    """A userfield that exists but was never written must not win."""
    assert loc.barcode_for_location(L("Big Pantry", "")) == "BBUDDY-LOC-BIG-PANTRY"


# ---------- the rename, which is the whole point ----------

def test_a_label_printed_before_a_rename_still_resolves():
    rows = [L("Basement - Cleaning Pantry", "BASEMENT-PANTRY", id=9)]
    found, err = loc.resolve("BBUDDY-LOC-BASEMENT-PANTRY", rows)
    assert err is None and found["id"] == 9


def test_the_new_name_does_NOT_resolve_once_a_slug_is_frozen():
    """
    Deliberate. One shelf, one code — if both resolved, two labels would point
    at the same place and the older one would look interchangeable when it is
    the only one anybody has stuck up.
    """
    rows = [L("Basement - Cleaning Pantry", "BASEMENT-PANTRY")]
    found, err = loc.resolve("BBUDDY-LOC-BASEMENT-CLEANING-PANTRY", rows)
    assert found is None and err


# ---------- immutability ----------

def test_freeze_returns_a_slug_only_when_there_is_none():
    assert loc.freeze_slug(L("Big Pantry")) == "BIG-PANTRY"
    assert loc.freeze_slug(L("Big Pantry", "")) == "BIG-PANTRY"


def test_freeze_never_recomputes_an_existing_slug():
    """
    The entire guarantee. A slug that can change is a computed slug with extra
    steps, and would reintroduce the bug this exists to remove.
    """
    row = L("Basement - Cleaning Pantry", "BASEMENT-PANTRY")
    assert loc.freeze_slug(row) == ""


def test_freeze_does_not_correct_a_slug_that_looks_wrong():
    """Even a hand-edited or odd slug is left alone — it may be on a shelf."""
    row = L("Big Pantry", "PANTRY-BIG-OLD")
    assert loc.freeze_slug(row) == ""
    assert loc.barcode_for_location(row) == "BBUDDY-LOC-PANTRY-BIG-OLD"


# ---------- resolve keeps its existing guarantees ----------

def test_ambiguity_is_still_an_error_across_stored_and_computed():
    """Silently picking one is how the wrong shelf reaches hundreds of products."""
    rows = [L("Big Pantry", id=1), L("Somewhere Else", "BIG-PANTRY", id=2)]
    found, err = loc.resolve("BBUDDY-LOC-BIG-PANTRY", rows)
    assert found is None and "ambiguous" in err


def test_a_product_barcode_still_falls_through():
    assert loc.resolve("049000006346", [L("Big Pantry")]) == (None, None)


def test_the_real_eleven_round_trip():
    """The names as they stand in Grocy today, with the slugs now frozen."""
    names = ["Kitchen - Fridge", "Kitchen - Freezer", "Laundry - Fridge",
             "Laundry - Freezer", "Garage - Upright Freezer",
             "Kitchen - Big Pantry", "Kitchen - Small Pantry",
             "Basement - Cleaning Pantry", "Kitchen - Spice Cabinet",
             "Kitchen - Snack Cupboard", "Kitchen - Oils & Vinegar"]
    rows = [L(n, loc.slug(n), id=i) for i, n in enumerate(names)]
    for r in rows:
        code = loc.barcode_for_location(r)
        found, err = loc.resolve(code, rows)
        assert err is None, (r["name"], code, err)
        assert found["id"] == r["id"]
    assert len({loc.stored_slug(r) for r in rows}) == len(names)
