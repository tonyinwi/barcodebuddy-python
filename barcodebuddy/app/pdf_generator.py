"""PDF Generator for Quantity Barcodes."""
import io
from dataclasses import dataclass
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.units import inch, mm
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128, qr
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing


def _qr_drawing(text, size):
    """
    A QR fitted to `size` points square, whatever its natural bounds.

    THE BUG THIS FIXES: a QrCodeWidget carries its own bounds -- 90.7pt for
    these payloads -- and adding it to a `Drawing(70, 70)` does not scale it, it
    CLIPS it. A clipped QR is not a small QR, it is an unscannable one, and it
    looks fine on screen until someone points a scanner at a printed sheet.

    Scaling via a transform rather than by setting barWidth/barHeight means this
    stays correct if a longer payload ever changes the module count.
    """
    w = qr.QrCodeWidget(text)
    b = w.getBounds()
    bw, bh = (b[2] - b[0]) or 1, (b[3] - b[1]) or 1
    d = Drawing(size, size, transform=[size / bw, 0, 0, size / bh,
                                       -b[0] * size / bw, -b[1] * size / bh])
    d.add(w)
    return d


def qr_modules(text):
    """
    How many modules across this payload's QR is -- 21 for a version 1, 25 for
    a version 2, and so on.

    Needed because reportlab reports the SAME bounds (90.7pt) whatever the
    version, so the bounds say nothing about how fine the printed modules will
    be. On a full-page card that does not matter. On a half-inch label it is
    the whole question.
    """
    w = qr.QrCodeWidget(str(text))
    # getBounds() is what BUILDS the matrix -- reportlab encodes lazily, and
    # `w.qr.modules` is None until something asks for the geometry. Reading it
    # first returns nothing and falls through to the default below, which is
    # wrong in the safe direction and therefore silent: every payload reports
    # as the worst case and the sheet looks tighter than it is.
    w.getBounds()
    inner = getattr(w, "qr", None)
    mods = getattr(inner, "modules", None)
    return len(mods) if mods else 25          # assume the worse case


# QR requires four modules of clear space on every side. reportlab does NOT
# include one -- a 21x21 code reports exactly 21 modules -- so the drawn square
# is edge-to-edge data. On a full-page card the surrounding white paper is the
# quiet zone by accident. On a label with text 6pt away it is not, and the code
# fails to read for a reason that is invisible on screen.
QUIET_MODULES = 4

# Below this a handheld imager starts struggling, especially with the print
# bleed of an inkjet on matte label stock. Not a hard standard -- good imagers
# manage ~0.25mm -- but it is the point at which a sheet is worth reconsidering
# rather than reprinting twice.
MIN_MODULE_MM = 0.30


@dataclass(frozen=True)
class LabelSheet:
    """
    A die-cut sheet. Measurements are the manufacturer's, in inches.

    Avery publishes margins that are symmetric by construction, and they are
    reproduced here rather than derived: `cols * label_w + (cols-1) * gap_x`
    plus twice the margin must equal the page width exactly, and a test
    asserts it. A quarter-millimetre of drift compounds across four columns
    into labels printed over their own die cuts.
    """
    key: str
    name: str
    page: tuple
    cols: int
    rows: int
    label_w: float
    label_h: float
    margin_x: float
    margin_y: float
    gap_x: float
    gap_y: float = 0.0

    @property
    def per_sheet(self):
        return self.cols * self.rows


LABEL_SHEETS = {
    # 80 up, 4 x 20. The one Tony asked for. Tight for a QR -- see the
    # module-size warning the generator emits.
    "8167": LabelSheet(
        key="8167", name="Avery 8167 / 5167 (return address, 80 up)",
        page=LETTER, cols=4, rows=20,
        label_w=1.75 * inch, label_h=0.5 * inch,
        margin_x=0.28125 * inch, margin_y=0.5 * inch,
        gap_x=0.3125 * inch, gap_y=0.0),
    # 30 up, 3 x 10. Twice the height, so roughly twice the module size --
    # the safe choice if 8167 proves marginal with the gun in hand.
    "5160": LabelSheet(
        key="5160", name="Avery 5160 / 8160 (address, 30 up)",
        page=LETTER, cols=3, rows=10,
        label_w=2.625 * inch, label_h=1.0 * inch,
        margin_x=0.1875 * inch, margin_y=0.5 * inch,
        gap_x=0.125 * inch, gap_y=0.0),
}


def module_mm(sheet, text):
    """
    The printed size of one QR module on this sheet, in millimetres.

    This is the number that decides whether a sheet is scannable, and it is
    not guessable: it falls out of the label height, the quiet zone, and the
    payload's QR version together.
    """
    usable = min(sheet.label_h, sheet.label_h) - 4          # 2pt padding each side
    total_modules = qr_modules(text) + 2 * QUIET_MODULES
    return (usable / 72.0) * 25.4 / total_modules


def _create_barcode(barcode_text, barcode_format='code128'):
    """Create a barcode object based on the specified format."""
    if barcode_format == 'qr':
        # QR code
        return qr.QrCodeWidget(barcode_text)
    else:
        # Code128 (default)
        return code128.Code128(
            barcode_text,
            barWidth=1.2,
            barHeight=50,
            humanReadable=True,
            fontSize=10
        )


def _fit_text(c, text, width, font, start, floor=4.0):
    """
    Largest size at or below `start` that fits `width`, or `floor`.

    Shrinking beats truncating: "Basement Pant..." on a shelf label is worse
    than a slightly smaller "Basement Pantry", and these names are chosen by
    the person who has to read them.
    """
    size = start
    while size > floor and c.stringWidth(text, font, size) > width:
        size -= 0.25
    return size


def generate_label_sheet_pdf(locations, sheet_key="8167"):
    """
    Location codes on a die-cut Avery sheet, one label per shelf.

    Laid out from the manufacturer's own measurements, top-left to
    bottom-right, which is the order every label sheet is printed in and the
    order a person peels them off in.

    ⚠️ THE QUIET ZONE IS THE WHOLE DIFFICULTY AT THIS SIZE. A QR needs four
    modules of clear space on every side, and reportlab draws none -- the
    widget's bounds are exactly the data. On a full-page card the surrounding
    paper supplies it by accident; on a 0.5in label with a name printed 6pt
    away it does not, and the code simply fails to read, which is invisible
    until a scanner meets a printed page. So the QR is drawn *inset* by four
    modules and the text starts outside that.

    The quiet zone is kept INSIDE the label rather than borrowed from the
    surrounding sheet. Borrowing it would buy about 12% more module (0.385mm
    against 0.342mm on 8167) and is perfectly valid on the printed sheet --
    but these labels get peeled off and stuck to a shelf, and a shelf can be
    dark wood. A quiet zone that only exists while the label is still on its
    backing paper is a quiet zone that disappears exactly when it is needed.

    The cost is real: on 8167 the module lands around 0.34mm for the longer
    shelf names. That is above what a decent imager needs and below what is
    comfortable, so the generator prints the measured figure on the sheet and
    warns when it drops under MIN_MODULE_MM -- a number on the page beats
    finding out at the shelf.
    """
    import locations as loc

    sheet = LABEL_SHEETS.get(sheet_key)
    if sheet is None:
        raise ValueError(f"unknown label sheet {sheet_key!r}; "
                         f"have {sorted(LABEL_SHEETS)}")

    rows = [r for r in (locations or []) if str(r.get("name") or "").strip()]
    buffer = io.BytesIO()
    page_w, page_h = sheet.page
    c = canvas.Canvas(buffer, pagesize=sheet.page)

    per_page = sheet.per_sheet
    for index, row in enumerate(rows):
        if index and index % per_page == 0:
            c.showPage()
        slot = index % per_page
        col, line = slot % sheet.cols, slot // sheet.cols

        x = sheet.margin_x + col * (sheet.label_w + sheet.gap_x)
        # Measured from the TOP of the page down, because that is how a sheet
        # is described and how it feeds through a printer.
        y_top = page_h - sheet.margin_y - line * (sheet.label_h + sheet.gap_y)
        y = y_top - sheet.label_h

        name = str(row.get("name") or "").strip()
        payload = loc.barcode_for(name)

        pad = 2.0
        box = sheet.label_h - 2 * pad
        modules = qr_modules(payload) + 2 * QUIET_MODULES
        quiet = box * QUIET_MODULES / modules
        qr_size = box - 2 * quiet

        renderPDF.draw(_qr_drawing(payload, qr_size), c,
                       x + pad + quiet, y + pad + quiet)

        text_x = x + pad + box + 4
        text_w = sheet.label_w - (text_x - x) - pad
        name_size = _fit_text(c, name, text_w, "Helvetica-Bold",
                              11 if sheet.label_h > 50 else 7.5)
        c.setFont("Helvetica-Bold", name_size)
        c.drawString(text_x, y + sheet.label_h / 2 + 0.5, name)

        code_size = _fit_text(c, payload, text_w, "Helvetica",
                              7 if sheet.label_h > 50 else 4.6, floor=3.4)
        c.setFont("Helvetica", code_size)
        c.setFillGray(0.45)
        c.drawString(text_x, y + sheet.label_h / 2 - code_size - 1.5, payload)
        c.setFillGray(0)

    if rows:
        worst = min(module_mm(sheet, loc.barcode_for(str(r["name"]).strip()))
                    for r in rows)
        note = (f"{sheet.name} · {len(rows)} location(s) · "
                f"smallest QR module {worst:.2f} mm")
        if worst < MIN_MODULE_MM:
            note += "  ** below %.2f mm -- try 5160 if these do not scan **" % MIN_MODULE_MM
        c.setFont("Helvetica", 5)
        c.setFillGray(0.55)
        # In the bottom margin, which is outside every die cut on both sheets,
        # so it lands on the backing paper and never on a label.
        c.drawString(sheet.margin_x, sheet.margin_y / 2, note)
        c.setFillGray(0)

    c.save()
    buffer.seek(0)
    return buffer


def generate_location_sheet_pdf(locations, barcode_format='qr'):
    """
    One QR per Grocy location, on shelf-sized cards.

    These are STOCK LOCATION codes, not kitchen ones. Grocy's locations cover
    the whole house -- laundry fridge, garage freezer, basement pantry -- and
    calling the sheet "kitchen" made it look like it only applied to one room.
    Nothing about the payload changed: `BBUDDY-LOC-<SLUG>` is already printed
    on labels that are stuck to shelves, and renaming a prefix to improve a
    title would strand every one of them.

    DATA-DRIVEN on purpose. The control sheet above hardcodes its barcode list,
    which is fine for ADD/CONSUME/quantity because those never change. Locations
    do: add a shelf in Grocy, regenerate, and it is on the page. A hardcoded
    list would quietly stop matching the house, and the failure would show up as
    products on the wrong shelf weeks later.

    Defaults to QR. The payload is descriptive -- BBUDDY-LOC-BIG-PANTRY -- so
    the person standing in front of a taped-up label can read what it is, and QR
    removes any reason to prefer a short numeric code.
    """
    import locations as loc

    buffer = io.BytesIO()
    # LETTER, not A4: these are printed here, on US paper, and the Avery
    # sheets alongside them are a US Letter product. An A4 layout on a Letter
    # printer silently shifts everything up the page.
    c = canvas.Canvas(buffer, pagesize=LETTER)
    width, height = LETTER

    c.setFont("Helvetica-Bold", 20)
    c.drawString(50, height - 50, "Stock Location Codes")
    c.setFont("Helvetica", 11)
    c.drawString(50, height - 70,
                 "Scan at a shelf. Everything that gun scans next is created there.")
    c.drawString(50, height - 85,
                 "Forgets after 10 minutes without a scan, and falls back to the preset.")

    cols, card_w, card_h = 2, (width - 100) / 2, 150
    x0, y = 50, height - 120
    for idx, row in enumerate(locations or []):
        name = str(row.get("name") or "")
        payload = loc.barcode_for(name)
        col = idx % cols
        x = x0 + col * card_w
        if col == 0 and idx:
            y -= card_h
        if y < 120:                       # new page before we run off the bottom
            c.showPage()
            y = height - 120
            c.setFont("Helvetica-Bold", 20)
            c.drawString(50, height - 50, "Stock Location Codes (cont.)")

        if barcode_format == 'qr':
            renderPDF.draw(_qr_drawing(payload, 90), c, x, y - 95)
        else:
            _create_barcode(payload, barcode_format).drawOn(c, x, y - 60)

        c.setFont("Helvetica-Bold", 13)
        c.drawString(x + 100, y - 30, name)
        c.setFont("Helvetica", 8)
        c.drawString(x + 100, y - 46, payload)

    c.save()
    buffer.seek(0)
    return buffer


def generate_quantity_barcodes_pdf(barcode_format='code128'):
    """Generate a PDF with quantity barcodes (3-10, 20, 30)."""
    # Create PDF buffer
    buffer = io.BytesIO()

    # Create canvas (A4 size)
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # Title
    c.setFont("Helvetica-Bold", 20)
    format_name = "QR Codes" if barcode_format == 'qr' else "Code128 Barcodes"
    c.drawString(50, height - 50, f"Barcode Buddy - Control {format_name}")

    c.setFont("Helvetica", 12)
    c.drawString(50, height - 70, "Scan these barcodes to control Barcode Buddy")

    # Layout settings
    left_margin = 50
    right_column_x = width / 2 + 20
    barcode_height = 50 if barcode_format == 'code128' else 70  # QR codes need more space

    # Section 1: Mode Barcodes (ADD/CONSUME)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 110, "Mode Control:")

    mode_y = height - 140

    # ADD mode barcode
    try:
        c.setFont("Helvetica-Bold", 14)
        c.drawString(left_margin, mode_y + 10, "➕ ADD Mode")

        barcode_obj = _create_barcode("BBUDDY-ADD", barcode_format)

        if barcode_format == 'qr':
            renderPDF.draw(_qr_drawing("BBUDDY-ADD", barcode_height),
                           c, left_margin, mode_y - barcode_height - 5)
        else:
            barcode_obj.drawOn(c, left_margin, mode_y - barcode_height - 5)

    except Exception as e:
        c.setFont("Helvetica", 8)
        c.drawString(left_margin, mode_y, f"Error: {str(e)[:50]}")

    # CONSUME mode barcode
    try:
        c.setFont("Helvetica-Bold", 14)
        c.drawString(right_column_x, mode_y + 10, "➖ CONSUME Mode")

        barcode_obj = _create_barcode("BBUDDY-CONSUME", barcode_format)

        if barcode_format == 'qr':
            renderPDF.draw(_qr_drawing("BBUDDY-CONSUME", barcode_height),
                           c, right_column_x, mode_y - barcode_height - 5)
        else:
            barcode_obj.drawOn(c, right_column_x, mode_y - barcode_height - 5)

    except Exception as e:
        c.setFont("Helvetica", 8)
        c.drawString(right_column_x, mode_y, f"Error: {str(e)[:50]}")

    # Section 2: Quantity Barcodes
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, mode_y - 100, "Quantity Control:")

    # Define quantities to generate
    quantities = [3, 4, 5, 6, 7, 8, 9, 10, 20, 30]

    # Layout settings for quantities
    start_y = mode_y - 130
    spacing = 100 if barcode_format == 'qr' else 90  # QR codes need more spacing

    # Generate barcodes
    for idx, qty in enumerate(quantities):
        barcode_text = f"BBUDDY-Q-{qty}"

        # Determine position (2 columns)
        col = idx % 2
        row = idx // 2

        x_pos = left_margin if col == 0 else right_column_x
        y_pos = start_y - (row * spacing)

        try:
            # Add label above barcode
            c.setFont("Helvetica-Bold", 14)
            c.drawString(x_pos, y_pos + 10, f"Quantity: {qty}")

            # Create barcode
            barcode_obj = _create_barcode(barcode_text, barcode_format)

            # Draw barcode
            if barcode_format == 'qr':
                renderPDF.draw(_qr_drawing(barcode_text, barcode_height),
                               c, x_pos, y_pos - barcode_height - 5)
            else:
                barcode_obj.drawOn(c, x_pos, y_pos - barcode_height - 5)

        except Exception as e:
            # Fallback: just draw text if barcode generation fails
            c.setFont("Helvetica", 10)
            c.drawString(x_pos, y_pos, f"Error: {barcode_text}")
            c.setFont("Helvetica", 8)
            c.drawString(x_pos, y_pos - 15, f"{str(e)[:50]}")

    # Footer
    c.setFont("Helvetica-Oblique", 10)
    c.drawString(50, 30, f"Generated by Barcode Buddy (Python) - Format: {format_name}")

    # Save PDF
    c.save()

    # Return buffer
    buffer.seek(0)
    return buffer
