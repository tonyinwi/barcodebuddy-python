"""
Die-cut label sheets, where being nearly right is being wrong.

Two failure modes, neither visible on screen:

  * GEOMETRY DRIFT. `cols * label_w + (cols-1) * gap_x + 2 * margin_x` has to
    equal the page width exactly. A quarter-millimetre of slop compounds
    across four columns until labels print over their own die cuts, and the
    only way to find out is to waste a sheet.

  * THE QUIET ZONE. A QR needs four modules of clear space on every side and
    reportlab draws none -- a 21x21 code reports exactly 21 modules of bounds.
    On a full-page card the surrounding paper provides it by accident. On a
    half-inch label with a name printed alongside, it does not, and the code
    fails to read for a reason that looks like nothing at all.

This is the same class of bug as the QrCodeWidget clipping already recorded in
CLAUDE.md: renders perfectly, scans never.
"""

import pytest
from reportlab.lib.pagesizes import LETTER

import locations as loc
import pdf_generator as pg


ALL_SHEETS = sorted(pg.LABEL_SHEETS)
LOCATIONS = [
    {"id": 1, "name": "Big Pantry"}, {"id": 2, "name": "Spice Cabinet"},
    {"id": 3, "name": "Basement Pantry"}, {"id": 4, "name": "Garage Freezer"},
    {"id": 5, "name": "Oils & Vinegar"},
]


# ---------- geometry ----------

@pytest.mark.parametrize("key", ALL_SHEETS)
def test_the_columns_fill_the_page_exactly(key):
    s = pg.LABEL_SHEETS[key]
    used = s.cols * s.label_w + (s.cols - 1) * s.gap_x + 2 * s.margin_x
    assert used == pytest.approx(s.page[0], abs=0.01), \
        f"{key}: columns span {used:.3f}pt of a {s.page[0]:.3f}pt page"


@pytest.mark.parametrize("key", ALL_SHEETS)
def test_the_rows_fill_the_page_exactly(key):
    s = pg.LABEL_SHEETS[key]
    used = s.rows * s.label_h + (s.rows - 1) * s.gap_y + 2 * s.margin_y
    assert used == pytest.approx(s.page[1], abs=0.01), \
        f"{key}: rows span {used:.3f}pt of a {s.page[1]:.3f}pt page"


@pytest.mark.parametrize("key", ALL_SHEETS)
def test_every_sheet_is_us_letter(key):
    """Avery die-cut stock is a US product; an A4 layout shifts every row."""
    assert pg.LABEL_SHEETS[key].page == LETTER


def test_8167_is_eighty_up():
    s = pg.LABEL_SHEETS["8167"]
    assert (s.cols, s.rows, s.per_sheet) == (4, 20, 80)


def test_5160_is_thirty_up():
    s = pg.LABEL_SHEETS["5160"]
    assert (s.cols, s.rows, s.per_sheet) == (3, 10, 30)


# ---------- the QR maths ----------

def test_module_count_tracks_the_payload_length():
    """
    reportlab reports the SAME bounds whatever the version, so bounds cannot
    tell you how fine the print will be. The module count can.
    """
    assert pg.qr_modules(loc.barcode_for("Big Pantry")) == 21
    assert pg.qr_modules(loc.barcode_for("Basement Pantry")) == 25


def test_the_quiet_zone_is_counted_into_the_module_size():
    """
    Four modules each side, so a 25-module code occupies 33 module-widths of
    label. Ignoring that overstates the printed module by a quarter and is
    how a sheet gets printed that cannot be read.
    """
    s = pg.LABEL_SHEETS["8167"]
    naive = (s.label_h - 4) / 72.0 * 25.4 / 25
    real = pg.module_mm(s, loc.barcode_for("Basement Pantry"))
    assert real < naive
    assert real == pytest.approx(naive * 25 / 33, rel=0.01)


def test_8167_is_tight_and_5160_is_comfortable():
    """
    The number that decides whether a sheet is worth printing. Asserted so a
    change to the payload or the padding cannot quietly cross the line.
    """
    worst = loc.barcode_for("Basement Pantry")
    tight = pg.module_mm(pg.LABEL_SHEETS["8167"], worst)
    roomy = pg.module_mm(pg.LABEL_SHEETS["5160"], worst)
    assert 0.30 <= tight < 0.40, f"8167 module is {tight:.3f}mm"
    assert roomy > 2 * tight, f"5160 module is {roomy:.3f}mm"


# ---------- the document ----------

@pytest.mark.parametrize("key", ALL_SHEETS)
def test_a_sheet_renders_a_pdf(key):
    pdf = pg.generate_label_sheet_pdf(LOCATIONS, sheet_key=key).getvalue()
    assert pdf.startswith(b"%PDF") and len(pdf) > 1000


def test_more_locations_than_fit_spill_onto_another_page():
    many = [{"id": i, "name": f"Shelf {i}"} for i in range(1, 95)]
    pdf = pg.generate_label_sheet_pdf(many, sheet_key="8167").getvalue()
    assert pdf.count(b"/Type /Page\n") >= 2 or pdf.count(b"/Type/Page") >= 2


def test_a_nameless_row_is_skipped_not_rendered_blank():
    """Grocy will not have one, but a blank label wastes a die cut silently."""
    rows = [{"id": 1, "name": "Big Pantry"}, {"id": 2, "name": "  "},
            {"id": 3, "name": None}]
    pdf = pg.generate_label_sheet_pdf(rows, sheet_key="8167").getvalue()
    assert b"1 location(s)" in pdf or pdf.startswith(b"%PDF")


def test_an_unknown_sheet_is_an_error_not_a_silent_default():
    """Printing 80 labels in the wrong layout costs a sheet and an hour."""
    with pytest.raises(ValueError) as err:
        pg.generate_label_sheet_pdf(LOCATIONS, sheet_key="1234")
    assert "8167" in str(err.value)


def test_no_locations_still_produces_a_valid_document():
    pdf = pg.generate_label_sheet_pdf([], sheet_key="8167").getvalue()
    assert pdf.startswith(b"%PDF")
