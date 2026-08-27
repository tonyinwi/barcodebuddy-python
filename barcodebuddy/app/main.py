"""Main Flask application."""
from flask import (Flask, render_template, jsonify, request, session,
                   send_file, make_response)
from flask_babel import Babel, gettext
import logging
import sys
import os
import requests
import threading
import base64
from urllib.parse import quote
from config import Config
from grocy import GrocyClient
from scanner import ScannerHandler, device_usb_id
import locations as loc
from alias_client import AliasClient
from pdf_generator import (generate_quantity_barcodes_pdf,
                           generate_location_sheet_pdf,
                           generate_label_sheet_pdf)
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'barcode-buddy-secret-key'  # For session management
app.config['BABEL_DEFAULT_LOCALE'] = 'en'
app.config['BABEL_TRANSLATION_DIRECTORIES'] = 'translations'

# Initialize Babel (will be configured after defining locale_selector)
babel = Babel()

def get_locale():
    """Get the configured language from config."""
    # Simply return the configured language (always set, no auto-detection)
    return config.language

# Load config first (before Babel, since get_locale() uses config)
config = Config()

# Set debug mode
if config.debug:
    logging.getLogger().setLevel(logging.DEBUG)
    app.debug = True

# Initialize Babel with locale selector (after config is loaded)
babel.init_app(app, locale_selector=get_locale)

# Initialize Grocy client
grocy_client = None
if config.has_grocy:
    grocy_client = GrocyClient(config.grocy_url, config.grocy_api_key)
    if grocy_client.test_connection():
        logger.info("✅ Grocy connection successful")
    else:
        logger.warning("⚠️  Grocy connection failed")
        grocy_client = None
else:
    logger.info("ℹ️  No Grocy configuration - running in standalone mode")

# Initialize Paperless Grocy Magic alias client
alias_client = None
if config.has_alias_integration:
    alias_client = AliasClient(config.paperless_grocy_magic_url)
    if alias_client.test_connection():
        logger.info("✅ Paperless Grocy Magic alias integration enabled")
    else:
        logger.warning("⚠️  Paperless Grocy Magic connection failed - alias integration disabled")
        alias_client = None
else:
    logger.info("ℹ️  Alias integration not configured")

# Initialize product database clients
# GTIN lengths that actually exist: EAN-8, UPC-A, EAN-13, ITF-14. Anything else
# is a retailer's internal code, not a global identifier.
GTIN_LENGTHS = (8, 12, 13, 14)


def is_gtin(barcode: str) -> bool:
    """
    Is this a real global trade number, or a shop's own item number?

    Worth checking before any external lookup, because the databases do not
    check. Penzeys bags carry a 5-digit item number; Open Food Facts
    zero-pads 55540 to 00055540, finds a valid EAN-8, and confidently returns
    a Dutch baby food product -- which then gets attached to that barcode
    forever. A wrong answer is worse than no answer: "Unknown 55540" is
    obviously unfinished and gets fixed, while "Bonbebe Fruithapje" looks
    like it worked.

    So non-GTIN codes skip external lookup entirely and are left for a
    source that actually knows them -- see tools/penzeys_backfill.py.
    """
    code = (barcode or "").strip()
    return code.isdigit() and len(code) in GTIN_LENGTHS


def is_product_code(barcode: str) -> bool:
    """
    Could this payload be a product's number at all?

    Deliberately WEAKER than is_gtin(). A scanner reads whatever is printed,
    and packaging carries more than barcodes: recipe QR codes, marketing URLs,
    loyalty codes. Two of those became products --

        HTTPSWWWPSSEASONINGCOMBLOGSRECIPESTAGGEDPRIME-TIME-BUTTERY-BEEF-RUB
        HTTPCONGRANETUGTFTQQ

    -- and one of them was a real jar of Prime Time Buttery Beef Rub whose
    actual UPC we already had.

    The line is NUMERIC, not GTIN-valid. A blanket non-GTIN refusal would make
    Penzeys unscannable: their bags carry 5-digit item numbers, which is why
    is_gtin(), the 1392-SKU catalogue and the penzeys provider all exist. Wild
    Fork prints a 19-digit internal code. Both are real products and both are
    numeric.

    So: digits mean somebody's item number, and we create it -- a source that
    knows those numbers can name it later. Anything else is not a product
    number and never will be, and creating a placeholder for it just makes
    rubbish that has to be found and deleted by hand.
    """
    return (barcode or "").strip().isdigit()


KITCHEN_STACK_URL = "http://172.16.0.138:8099"


def validate_providers(days=7):
    """
    Delegated: the engine and its evidence live in the Kitchen Stack add-on.
    This survives only so /api/providers keeps answering during the cutover.
    """
    resp = requests.get(f"{KITCHEN_STACK_URL}/api/providers",
                        params={"days": days}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("providers", [])


def log_provider_check():
    """The engine's provider check, echoed at startup for the log's sake."""
    try:
        rows = validate_providers()
    except Exception as err:                                     # noqa: BLE001
        logger.warning(f"provider check unavailable (engine down?): {err}")
        return
    logger.info("🔎 Provider check (from the Kitchen Stack engine):")
    for r in rows:
        state = "ENABLED " if r.get("enabled") else "disabled"
        pr = r.get("priority") or "-"
        logger.info(f"   {pr}. {state} {r.get('provider', '?')}")
        for w in r.get("warnings", []):
            logger.warning(f"      ⚠  {w}")


def penzeys_hierarchy(sku: str, presets: dict):
    """
    Build (or find) the generic parent and the right child for a Penzeys SKU.

    Penzeys sells the same blend as a jar and as a bag -- 551 and 525 of 1392
    SKUs. The bag is bought; the jar is refilled from it. So they are separate
    products with separate pictures, prices, stock and locations, under one
    generic parent that recipes can ask about.

        mustard seed brown        no_own_stock=1  min=0
          |- ... bag              min=1   <- the restock signal
          |- ... jar              min=0   <- tracked, never nags

    THE PARENT IS FOUND THROUGH A SIBLING, NOT BY NAME. Penzeys says "Mustard
    Seed Brown" where a recipe says "brown mustard seed", so the parent gets
    renamed by a human sooner or later. A name lookup would then miss it and
    build a duplicate on the next variant scanned. Any other SKU of the same
    blend that is already a barcode in Grocy points at the real parent, however
    it has since been named.

    Returns (child_product_id, note) or (None, reason).
    """
    try:
        r = requests.get(f"{KITCHEN_STACK_URL}/api/penzeys/blend/{sku}", timeout=10)
        if r.status_code != 200:
            return None, f"blend lookup returned {r.status_code}"
        info = r.json()
    except Exception as err:                                  # noqa: BLE001
        return None, f"blend lookup failed: {err}"

    container = info.get("container") or ""
    if not container:
        # Extracts by the fluid ounce, vanilla by the bean. No container means
        # no jar/bag split to make, so leave it as an ordinary product.
        return None, "no container on this SKU"

    blend = (info.get("blend") or "").strip()
    if not blend:
        return None, "no blend name"

    # Find the parent through a sibling that is already in Grocy.
    parent_id = None
    for sib in info.get("siblings", []):
        existing = grocy_client.find_product_by_barcode(str(sib))
        if not existing:
            continue
        prod = existing.get("product", existing) if isinstance(existing, dict) else {}
        pid = prod.get("parent_product_id")
        if pid:
            parent_id = int(pid)
            logger.info(f"🌳 found the parent via sibling SKU {sib} (product {parent_id})")
            break

    parent_name = blend.lower()
    if parent_id is not None:
        # The parent was found through a sibling, which means it may have been
        # RENAMED by a human -- "Mustard Seed Brown" becomes "brown mustard
        # seed". The child must be named from the parent as it actually is, or
        # the next variant scanned creates "mustard seed brown bag" alongside
        # the existing "brown mustard seed bag": same role, two products, one
        # of them invisible to the person who named the other.
        info_p = grocy_client.get_product_info(parent_id) or {}
        actual = ((info_p.get("product") or info_p).get("name") or "").strip()
        if actual:
            parent_name = actual
    if parent_id is None:
        parent_id = grocy_client.find_product_by_name(parent_name)
    if parent_id is None:
        parent_id = grocy_client.create_product(
            name=parent_name,
            description="Generic parent for a Penzeys blend. Stock lives on the "
                        "jar and bag children; this exists so a recipe asking "
                        "for it can see whether any exists at all.",
            min_stock_amount=0,
            no_own_stock=1,
            location_id=presets.get("location_id"),
            qu_id_purchase=presets.get("qu_id"),
            qu_id_stock=presets.get("qu_id"))
        if parent_id is None:
            return None, "could not create the parent"
        logger.info(f"🌳 created parent {parent_name!r} (product {parent_id})")

    # ONE CHILD PER SIZE, not per container type. A 3/4 cup bag and a 3 cup bag
    # are different products: different price, and Grocy's price history is per
    # product, so collapsing them averages $6 and $18 into a number that means
    # nothing. Stock is worse -- "1 bag" is 4x ambiguous, so "do I have enough"
    # becomes unanswerable. Splitting is the whole reason to have parent/child.
    #
    # Penzeys' own size string reads correctly when appended:
    # "brown mustard seed 3/4 cup bag" is exactly what you would ask for.
    size = (info.get("size") or "").strip()
    child_name = f"{parent_name} {size}" if size else f"{parent_name} {container}"
    child_id = grocy_client.find_product_by_name(child_name)
    if child_id is None:
        child_id = grocy_client.create_product(
            name=child_name,
            description=f"{info.get('size','')} {info.get('weight','')}".strip(),
            # The bag is bought and must reach the shopping list. The jar is
            # refilled from the bag, so a minimum on it would ask you to buy
            # something you never buy.
            min_stock_amount=1 if container == "bag" else 0,
            parent_product_id=parent_id,
            location_id=presets.get("location_id"),
            qu_id_purchase=presets.get("qu_id"),
            qu_id_stock=presets.get("qu_id"))
        if child_id is None:
            return None, "could not create the child"
        logger.info(f"🌳 created child {child_name!r} (product {child_id}) "
                    f"min_stock={1 if container == 'bag' else 0}")
    return child_id, f"{parent_name} / {container}"


def lookup_chain(barcode):
    """
    Ask Grocy. That is the whole function now, on purpose.

    This scanner is an input device. It owns no providers, no keys and no
    ordering: Grocy's external-lookup is the resolution authority, its plugin
    proxies to the Kitchen Stack add-on, and the engine there runs the chain,
    applies the echo and empty-name rules, resolves the product presets, and
    writes the attempt log. For one day the chain lived here instead, which
    inverted the architecture -- the input device was the authority and the
    inventory system was its client.

    is_gtin stays: it costs nothing, keeps garbage off the wire entirely, and
    scanned QR codes never deserve an HTTP round trip.

    Returns (product | None, source_name | None, attempts) -- the same shape
    as before, so the scan path is untouched.
    """
    # NO is_gtin GATE. It moved: the engine now decides per provider, and the
    # local Penzeys catalogue is the one that accepts a non-GTIN, because an
    # exact dict lookup on a retailer's own item number cannot pad or fuzzy
    # match. Keeping a copy here meant #116 never reached the scan path -- a
    # Penzeys bag still landed as "Unknown 55540" while /api/lookup resolved it
    # perfectly. QR codes and URLs are already refused by is_product_code(),
    # so what passes here is numeric and worth asking about.
    if not grocy_client:
        return None, None, []

    product = grocy_client.external_lookup(barcode)
    if not product:
        return None, None, [{"provider": "grocy", "outcome": "miss"}]
    source = product.get("__source") or "Grocy lookup"
    return product, source, [{"provider": "grocy", "outcome": "hit",
                              "source": source}]




# Store recent scans
recent_scans = []

# Store pending barcodes (waiting for user decision)
pending_barcodes = []

# Current quantity for next product scan (reset after each product)
# Starts at 0, defaults to 1 if no quantity barcode scanned
current_quantity = 0.0

# Current mode: 'add' or 'consume'
current_mode = 'add'
# Which shelf each gun is standing at. Per gun, and it forgets after 10
# idle minutes -- see locations.py for why both of those matter.
location_tracker = loc.LocationTracker()
# One gun can be several /dev/hidrawN nodes, so the tracker resolves them to a
# USB id -- the same identity resolve_scan_mode() uses to bind ADD/CONSUME.
location_tracker.usb_resolver = device_usb_id

def notify_webhook(payload: dict):
    """
    POST a scan result to Home Assistant, fire-and-forget.

    FORK PATCH #3. Deliberately cannot affect the scan: it runs on its own
    thread, has a short timeout, and swallows every exception. A scanner that
    stops working because Home Assistant is rebooting would be a far worse bug
    than a missed announcement.

    Everything is sent -- successes, errors, mode switches, quantity barcodes.
    Filtering is HA's job, because that is where the policy is easy to change.
    """
    url = config.ha_webhook_url
    if not url:
        return

    def _post():
        try:
            requests.post(url, json=payload, timeout=3)
        except Exception as e:
            logger.debug(f"Webhook POST failed (ignored): {e}")

    threading.Thread(target=_post, daemon=True).start()


def _picture_url(product: dict) -> str:
    """
    A URL the browser can render for a product Grocy already has a picture of.

    Not Grocy's own URL: that endpoint wants the API key in a header, and an
    <img> tag cannot send one. Putting the key in a query string instead would
    paste a credential into every browser history and log on the network. So
    this add-on proxies it -- the key stays server-side and the browser asks
    for a path.
    """
    name = str((product or {}).get('picture_file_name') or '').strip()
    pid = (product or {}).get('id')
    return f"api/picture/{pid}" if name and pid else ""


def _scan_outcome(scan_result: dict) -> str:
    """
    Narrow a scan to the shelf-side question, or '' if it is not a product scan.

    Mode switches, quantity codes and location codes are not inventory, so they
    must not move the count -- otherwise "23 items in Spice Cabinet" quietly
    includes the four times you flipped the gun to CONSUME.
    """
    status = scan_result.get('status')
    if status == 'error':
        return 'error'
    if status != 'success':
        return ''
    return scan_result.get('outcome') or 'stocked'


def finish_scan(scan_result: dict):
    """Record a scan in recent history and tell Home Assistant about it."""
    recent_scans.insert(0, scan_result)
    if len(recent_scans) > 50:
        recent_scans.pop()
    # One choke point for the per-shelf tally rather than a call in each of the
    # twenty branches that can conclude a scan -- which is how a new branch
    # ends up silently uncounted.
    outcome = _scan_outcome(scan_result)
    if outcome:
        location_tracker.bump(scan_result.get('device'), outcome)
    notify_webhook(scan_result)


def resolve_scan_mode(device: str = None) -> str:
    """
    Decide whether THIS scan means ADD or CONSUME.

    FORK PATCH #2 (per-device scanner mode). The mode is a property of the gun
    you picked up, not a global toggle you have to remember the state of. One
    scanner lives where shopping is unpacked and always means ADD; the other
    lives by the bin and always means CONSUME -- which is the moment
    consumption is unambiguous, because you are throwing the thing away
    precisely because it is empty.

    That retires the stateful toggle and its failure mode: scanning ten items
    in the wrong mode produces ten wrong stock movements and no error.

    Falls back to the global mode when the device is unknown, unbound, or
    cannot be resolved -- so an unconfigured setup behaves exactly as before,
    and the ADD/CONSUME barcodes still work.
    """
    if not device:
        return current_mode

    usb = device_usb_id(device)
    if not usb:
        return current_mode

    if usb == config.scanner_add_device:
        return 'add'
    if usb == config.scanner_consume_device:
        return 'consume'
    return current_mode


def handle_barcode(barcode: str, device: str = None):
    """Handle scanned barcode with automatic product creation."""
    global current_quantity, current_mode

    logger.info(f"📦 Processing barcode: {barcode}")

    # Which gun was this? Decides ADD vs CONSUME for this scan only.
    mode = resolve_scan_mode(device)
    if device and mode != current_mode:
        logger.info(f"🔫 Device {device} is bound to {mode.upper()} (global is {current_mode.upper()})")

    scan_result = {
        'barcode': barcode,
        'timestamp': datetime.now().isoformat(),
        'status': 'unknown',
        'message': '',
        # Structured fields so consumers need not parse the emoji text.
        'mode': mode,
        'product': None,
        'device': device,
        # What this scan amounted to, from the point of view of somebody at a
        # shelf: created / stocked / unresolved / error. Set structurally at
        # each branch rather than inferred from the message, because the
        # message is emoji prose and reading it back is how a rename breaks a
        # counter silently.
        'outcome': '',
        # A URL the browser can put in an <img>. Either this add-on's picture
        # proxy for something Grocy already has, or the provider's own image
        # URL for something just looked up. Never blocks the scan (#150).
        'image': '',
    }

    # Check if this is a mode switch barcode
    if barcode == config.barcode_add:
        current_mode = 'add'
        scan_result['status'] = 'mode'
        scan_result['message'] = f"➕ Mode: ADD (adding to stock)"
        logger.info(f"➕ Mode switched to: ADD")
        # Store and return
        finish_scan(scan_result)
        return
    elif barcode == config.barcode_consume:
        current_mode = 'consume'
        scan_result['status'] = 'mode'
        scan_result['message'] = f"➖ Mode: CONSUME (removing from stock)"
        logger.info(f"➖ Mode switched to: CONSUME")
        # Store and return
        finish_scan(scan_result)
        return

    # Check if this is a location barcode
    if barcode.startswith(loc.PREFIX):
        try:
            location, err = loc.resolve(barcode, grocy_client.get_locations()
                                        if grocy_client else [])
        except Exception as e:                                   # noqa: BLE001
            location, err = None, f"could not read Grocy locations: {e}"
        if location:
            location_tracker.set(device, location["id"], location["name"])
            scan_result['status'] = 'location'
            scan_result['message'] = f"📍 Location: {location['name']}"
            scan_result['location'] = location["name"]
            logger.info(f"📍 {device or 'gun'} is now at: {location['name']} "
                        f"(id {location['id']})")
        else:
            # Never fall back to a guess: an unmatched location code must not
            # quietly leave the previous shelf in effect, because the next
            # fifty scans would land there.
            location_tracker.clear(device)
            scan_result['status'] = 'error'
            scan_result['message'] = f"❌ {err or 'unknown location code'}"
            logger.error(f"📍 location barcode rejected: {barcode} -- {err}")
        finish_scan(scan_result)
        return

    # Check if this is a quantity barcode
    if barcode.startswith(config.barcode_quantity_prefix):
        try:
            quantity_str = barcode[len(config.barcode_quantity_prefix):]
            quantity_to_add = float(quantity_str)
            current_quantity += quantity_to_add
            scan_result['status'] = 'quantity'
            scan_result['message'] = f"🔢 Quantity set to: {current_quantity}"
            logger.info(f"🔢 Quantity updated: {current_quantity} (added {quantity_to_add})")
        except (IndexError, ValueError) as e:
            scan_result['status'] = 'error'
            scan_result['message'] = f"❌ Invalid quantity barcode format"
            logger.error(f"Invalid quantity barcode: {barcode} - {e}")

        # Store scan result and return
        finish_scan(scan_result)
        return

    # A product scan is activity: the idle clock is per gun, not per session.
    # Counted BEFORE the refusal below -- scanning a QR code by mistake still
    # means somebody is standing at the shelf with the gun in their hand, and
    # expiring the shelf underneath them would be wrong.
    location_tracker.touch(device)

    # Not everything printed on a package is a product number. Refuse the rest
    # here rather than manufacturing an "Unknown <payload>" product for it.
    if not is_product_code(barcode):
        scan_result['status'] = 'error'
        scan_result['message'] = "❌ Not a product barcode - looks like a QR code or URL"
        logger.warning(f"🚫 refused, not a product code: {barcode[:60]}")
        finish_scan(scan_result)
        return

    # Regular product barcode handling
    if grocy_client:
        # Step 1: Try to find product in Grocy
        product = grocy_client.find_product_by_barcode(barcode)

        if product:
            # Product exists in Grocy
            logger.debug(f"Product data from Grocy: {product}")

            # Grocy API returns nested structure: {'product': {'id': ...}}
            if 'product' in product and isinstance(product['product'], dict):
                product_id = product['product'].get('id')
                product_name = product['product'].get('name', 'Unknown')
                scan_result['product'] = product_name
                scan_result['outcome'] = 'stocked'
                scan_result['image'] = _picture_url(product['product'])
                # Product info is already included in the response
                product_info = product['product']
            else:
                # Fallback for different API formats
                product_id = product.get('product_id') or product.get('id')
                product_info = None

            if not product_id:
                logger.error(f"No product_id found in response: {product}")
                scan_result['status'] = 'error'
                scan_result['message'] = f"❌ Invalid product data from Grocy"
                # Store and return
                finish_scan(scan_result)
                return

            # Both branches above converge here, which is the only place the id
            # is known to be good. It is kept so the feed can resolve the
            # picture LATER -- see get_scans_recent().
            scan_result['product_id'] = product_id

            # If product info wasn't in the barcode response, fetch it separately
            if not product_info:
                product_info = grocy_client.get_product_info(product_id)

            if product_info:
                product_name = product_info.get('name', 'Unknown')
                scan_result['product'] = product_name
                scan_result['outcome'] = 'stocked'
                scan_result['image'] = _picture_url(product_info)
                # Use current quantity, or default to 1 if no quantity barcode was scanned
                amount = current_quantity if current_quantity > 0 else 1.0

                # Add or consume based on current mode
                if mode == 'add':
                    success = grocy_client.add_product(product_id, amount)
                    action_emoji = "➕"
                    action_text = "Added"
                else:  # consume mode
                    success = grocy_client.consume_product(product_id, amount)
                    action_emoji = "➖"
                    action_text = "Removed"

                if success:
                    quantity_text = f" ({amount}x)" if amount != 1 else ""
                    scan_result['status'] = 'success'
                    scan_result['message'] = f"{action_emoji} {action_text}: {product_name}{quantity_text}"
                    logger.info(f"{action_emoji} {action_text} product: {product_name} (quantity: {amount})")
                    current_quantity = 0.0  # Reset after successful operation
                else:
                    scan_result['status'] = 'error'
                    scan_result['message'] = f"❌ Failed to {action_text.lower()}: {product_name}"
            else:
                scan_result['status'] = 'error'
                scan_result['message'] = f"❌ Error reading product info"
        else:
            # Step 2: Product not in Grocy - check alias system
            alias_found = False
            if alias_client:
                logger.info(f"🔗 Checking Paperless Grocy Magic aliases...")
                alias = alias_client.find_by_barcode(barcode)

                if alias:
                    # Found via alias! Use the Grocy product ID
                    product_id = alias['grocy_product_id']
                    product_name = alias['grocy_product_name']
                    scan_result['product'] = product_name
                    scan_result['outcome'] = 'stocked'
                    alias_found = True

                    logger.info(f"🔗 Found product via alias: {product_name} (ID {product_id})")

                    # Use current quantity, or default to 1
                    amount = current_quantity if current_quantity > 0 else 1.0

                    # Add or consume based on current mode
                    if mode == 'add':
                        success = grocy_client.add_product(product_id, amount)
                        action_emoji = "➕"
                        action_text = "Added"
                    else:  # consume mode
                        success = grocy_client.consume_product(product_id, amount)
                        action_emoji = "➖"
                        action_text = "Removed"

                    if success:
                        quantity_text = f" ({amount}x)" if amount != 1 else ""
                        scan_result['status'] = 'success'
                        scan_result['message'] = f"🔗 {action_emoji} {action_text} via alias: {product_name}{quantity_text}"
                        logger.info(f"🔗 {action_emoji} {action_text} product via alias: {product_name} (quantity: {amount})")
                        current_quantity = 0.0  # Reset after successful operation
                    else:
                        scan_result['status'] = 'error'
                        scan_result['message'] = f"❌ Failed to {action_text.lower()}: {product_name}"

            # Step 3: Not in Grocy, not in aliases - try external databases
            if not alias_found:
                logger.info(f"🔍 Not in aliases, checking external databases...")

                # Try databases in order: Grocy's own lookup plugin → OpenFoodFacts
                # → UPC Database. Only query databases enabled in configuration.
                #
                # Grocy leads deliberately. Its STOCK_BARCODE_LOOKUP_PLUGIN (e.g.
                # UPCitemdb) is US-focused where OpenFoodFacts is weakest, it is
                # tuned in one place rather than duplicated here, and it resolves
                # the product against the user's "presets for new products" so the
                # item lands on the right shelf. It is not a replacement though --
                # it is thin on imported goods, so the others stay as fallbacks.
                external_product = None
                database_name = None

                external_product, database_name, _attempts = lookup_chain(barcode)

                if external_product:
                    # Step 4: Found in external database - auto-create and stock it.
                    #
                    # FORK PATCH #1 (fire-and-forget). Upstream parks the barcode in
                    # pending_barcodes and waits for a UI decision. We never want the
                    # scanner to block on a human: unknowns are created immediately as
                    # raw products and cleaned up later by tools/grocy_normalize.py +
                    # tools/grocy_review.py in the kitchen-stack repo.
                    product_name = external_product['name']
                    scan_result['product'] = product_name
                    scan_result['outcome'] = 'created'
                    # Straight from the provider. Nothing is downloaded here --
                    # the browser fetches it, so a dead CDN costs the scan
                    # nothing at all. Grocy gets its own copy later, out of
                    # band, where a hotlink block can be checked properly (#76).
                    scan_result['image'] = str(
                        external_product.get('image_url') or '').strip()
                    amount = current_quantity if current_quantity > 0 else 1.0

                    # Grocy enforces UNIQUE on products.name, so a title we have already
                    # seen under a different barcode must attach to the existing product
                    # instead of failing the create. Same name = same product; this is
                    # free dedup at intake.
                    # A Penzeys SKU is not one product, it is a container of a
                    # blend. Build (or find) the generic parent and put this
                    # container under it, so the bag carries the restock signal
                    # and the jar -- which is refilled, not bought -- does not.
                    product_id = None
                    reused = False
                    if (external_product.get('__source') or '') == 'penzeys':
                        here = location_tracker.current(device)
                        product_id, note = penzeys_hierarchy(barcode, {
                            "location_id": ((here or {}).get("id")
                                            or external_product.get('location_id')),
                            "qu_id": external_product.get('qu_id_stock'),
                        })
                        if product_id:
                            reused = True     # created or found by the helper
                            product_name = note
                            scan_result['product'] = note
                            logger.info(f"🌳 penzeys: {note}")
                        else:
                            logger.info(f"🌳 penzeys hierarchy skipped: {note} "
                                        "-- falling back to a flat product")

                    if product_id is None:
                        product_id = grocy_client.find_product_by_name(product_name)
                        reused = product_id is not None

                    if reused:
                        logger.info(f"🔗 '{product_name}' already exists (ID {product_id}) - attaching barcode {barcode}")
                    else:
                        # A CONSUME scan of an unknown means "gone and needed". Grocy
                        # cannot hold negative stock -- ConsumeProduct() throws when the
                        # amount exceeds current stock and there is no override setting --
                        # so the reorder signal is expressed as a minimum instead: the
                        # product lands at 0 stock with min_stock_amount 1, which puts it
                        # in GetMissingProducts and onto the shopping list.
                        min_stock = 0 if mode == 'add' else 1
                        # Grocy's own lookup supplies location/unit resolved against
                        # the user's presets; the other sources supply nothing, and
                        # create_product() falls back for those.
                        #
                        # A location scanned on THIS gun wins over the preset: the
                        # person is standing at the shelf and has just said so. When
                        # no location is set, or the gun has been idle 10 minutes,
                        # this is None and the preset applies exactly as before.
                        here = location_tracker.current(device)
                        location_id = ((here or {}).get("id")
                                       or external_product.get('location_id'))
                        if here:
                            logger.info(f"📍 creating in {here['name']} "
                                        f"(scanned location, not the preset)")
                        product_id = grocy_client.create_product(
                            product_name,
                            description=f"Auto-created via Barcode Buddy from {database_name}",
                            min_stock_amount=min_stock,
                            location_id=location_id,
                            qu_id_purchase=external_product.get('qu_id_purchase'),
                            qu_id_stock=external_product.get('qu_id_stock')
                        )

                    if not product_id:
                        scan_result['status'] = 'error'
                        scan_result['message'] = f"❌ Failed to create: {product_name}"
                        logger.error(f"❌ Could not create product '{product_name}' for barcode {barcode}")
                    else:
                        # Brand and lookup source are recorded on the BARCODE, not the
                        # product: the product stays generic so any variant satisfies a
                        # recipe, while each barcode remembers which brand it was.
                        if not grocy_client.add_barcode_to_product(
                                product_id, barcode,
                                brand=external_product.get('brand', ''),
                                source=database_name or '',
                                # The full retail name, kept on the BARCODE. Once
                                # several barcodes share one generic product the
                                # product name no longer says which is which.
                                detail=external_product.get('name', '')):
                            logger.warning(f"Product {product_id} ready but failed to attach barcode {barcode}")

                        if mode == 'add':
                            success = grocy_client.add_product(product_id, amount)
                            if success:
                                quantity_text = f" ({amount}x)" if amount != 1 else ""
                                scan_result['status'] = 'success'
                                scan_result['message'] = f"✨ ➕ Created '{product_name}' from {database_name}{quantity_text}"
                                logger.info(f"✨ Created and added '{product_name}' (ID {product_id}, quantity: {amount})")
                                current_quantity = 0.0
                            else:
                                scan_result['status'] = 'error'
                                scan_result['message'] = f"❌ Created '{product_name}' but failed to add stock"
                        elif reused:
                            # Existing product may hold stock, so a real consume is valid.
                            # If it is already at 0 Grocy rejects this, exactly as it would
                            # for any known product scanned in consume mode.
                            success = grocy_client.consume_product(product_id, amount)
                            if success:
                                quantity_text = f" ({amount}x)" if amount != 1 else ""
                                scan_result['status'] = 'success'
                                scan_result['message'] = f"➖ Removed: {product_name}{quantity_text}"
                                logger.info(f"➖ Consumed '{product_name}' (ID {product_id}, quantity: {amount})")
                                current_quantity = 0.0
                            else:
                                scan_result['status'] = 'error'
                                scan_result['message'] = f"❌ Failed to remove: {product_name} (no stock?)"
                        else:
                            # Brand-new product sits at 0 stock with min_stock_amount 1.
                            # Consuming would throw, and there is nothing to draw down --
                            # the minimum already carries the "needs reorder" signal.
                            scan_result['status'] = 'success'
                            scan_result['message'] = f"✨ ➖ Created '{product_name}' from {database_name} - flagged for reorder"
                            logger.info(f"✨ Created '{product_name}' (ID {product_id}) at 0 stock, min_stock_amount=1 (reorder)")
                            current_quantity = 0.0
                else:
                    # Step 5: Not found anywhere -- create a placeholder anyway.
                    #
                    # FORK PATCH #1 (fire-and-forget), final branch. Upstream stops
                    # here and asks for a product name, which is the last thing in
                    # the flow that blocks on a human standing at the pantry. Some
                    # barcodes are in no database at all -- store brands, local
                    # products, anything without a GS1 listing -- so no amount of
                    # lookup tuning removes this case.
                    #
                    # The name is deliberately useless: naming it properly is the
                    # cleanup pipeline's job, and it was always going to be renamed
                    # anyway. "Unknown <barcode>" is inherently unique, so it cannot
                    # collide on Grocy's UNIQUE products.name the way a real title
                    # can, and it carries the barcode for whoever reviews it later.
                    product_name = f"Unknown {barcode}"
                    scan_result['product'] = product_name
                    # Counted apart from 'created' on purpose: a running tally
                    # of these is the only number that says how much review
                    # this shelf just bought you.
                    scan_result['outcome'] = 'unresolved' 
                    amount = current_quantity if current_quantity > 0 else 1.0

                    product_id = grocy_client.find_product_by_name(product_name)
                    if product_id:
                        logger.info(f"🔗 Placeholder '{product_name}' already exists (ID {product_id})")
                    else:
                        min_stock = 0 if mode == 'add' else 1
                        # The scanned location matters MOST here. An unknown gets
                        # its name fixed later from the review queue -- but nobody
                        # re-walks the house to fix a shelf, so a placeholder
                        # created in the wrong location stays wrong.
                        here = location_tracker.current(device)
                        if here:
                            logger.info(f"📍 placeholder in {here['name']} "
                                        f"(scanned location, not the preset)")
                        product_id = grocy_client.create_product(
                            product_name,
                            description="Auto-created via Barcode Buddy - not found in any database",
                            min_stock_amount=min_stock,
                            location_id=(here or {}).get("id")
                        )

                    if not product_id:
                        scan_result['status'] = 'error'
                        scan_result['message'] = f"❌ Failed to create placeholder for {barcode}"
                        logger.error(f"❌ Could not create placeholder product for barcode {barcode}")
                    else:
                        # No brand to record -- nothing resolved it. Source says so,
                        # which tells the reviewer this name came from nowhere rather
                        # than from a database that got it wrong.
                        grocy_client.add_barcode_to_product(product_id, barcode,
                                                            source='not found')

                        if mode == 'add':
                            if grocy_client.add_product(product_id, amount):
                                quantity_text = f" ({amount}x)" if amount != 1 else ""
                                scan_result['status'] = 'success'
                                scan_result['message'] = f"❓ ➕ Unknown barcode - created placeholder{quantity_text}, needs naming"
                                logger.info(f"❓ Created placeholder '{product_name}' (ID {product_id}, quantity: {amount})")
                                current_quantity = 0.0
                            else:
                                scan_result['status'] = 'error'
                                scan_result['message'] = f"❌ Created placeholder for {barcode} but failed to add stock"
                        else:
                            scan_result['status'] = 'success'
                            scan_result['message'] = f"❓ ➖ Unknown barcode - placeholder flagged for reorder, needs naming"
                            logger.info(f"❓ Created placeholder '{product_name}' (ID {product_id}) at 0 stock, min_stock_amount=1 (reorder)")
                            current_quantity = 0.0
    else:
        scan_result['status'] = 'no_grocy'
        scan_result['message'] = f"📦 Scanned (no Grocy configured)"

    # Store in recent scans (keep last 50)
    finish_scan(scan_result)

# Initialize scanner (auto-detects all available devices)
scanner = ScannerHandler(None, handle_barcode)
scanner.start()


def log_scanner_bindings():
    """
    Report, at startup, what each detected scanner resolves to and which mode
    it is bound to.

    Without this the per-device binding fails silently: if sysfs is not
    readable inside the container, device_usb_id() returns None, every scan
    quietly falls back to the global mode, and nothing anywhere says so. That
    is the same class of silent-wrong-mode bug this feature exists to remove,
    so it should not be possible to hit it without seeing it.
    """
    bound = {config.scanner_add_device: 'ADD', config.scanner_consume_device: 'CONSUME'}
    bound.pop('', None)

    if not scanner.active_devices:
        logger.warning("🔫 No scanner devices detected")
        return

    unresolved = 0
    for dev in scanner.active_devices:
        usb = device_usb_id(dev)
        if usb is None:
            unresolved += 1
            logger.warning(f"🔫 {dev}: USB id unreadable - falls back to global mode")
        else:
            mode = bound.get(usb)
            if mode:
                logger.info(f"🔫 {dev}  {usb}  -> {mode}")
            else:
                logger.info(f"🔫 {dev}  {usb}  -> unbound (global mode)")

    if bound and unresolved == len(scanner.active_devices):
        logger.error(
            "🔫 Per-device modes are configured but NO device could be resolved - "
            "sysfs is likely unreadable in this container. Every scan will use the "
            "global mode."
        )


log_scanner_bindings()

@app.route('/')
def index():
    """Main page."""
    return render_template('index.html',
                         has_grocy=config.has_grocy,
                         scanner_devices=scanner.active_devices,
                         connected_guns=_connected_guns(),
                         current_locale=get_locale())

@app.route('/api/download-location-sheet')
def download_location_sheet():
    """
    A printable sheet of STOCK LOCATION codes, from Grocy's actual locations.

    `?sheet=8167` or `?sheet=5160` lays them out on that Avery die-cut sheet;
    without it you get the original full-page cards.

    ALWAYS FORCES A FRESH FETCH. `get_locations()` is cached for the scan
    path, where somebody is standing waiting for a QR to resolve. Nobody is
    waiting here, and a sheet printed from a stale list is a label for a shelf
    the scanner will reject -- which clears the gun and sends everything after
    it to the preset.
    """
    try:
        rows = grocy_client.get_locations(force=True) if grocy_client else []
        if not rows:
            return jsonify({"error": "no locations available from Grocy"}), 503

        # FREEZE ON PRINT. A slug becomes permanent the moment it goes on
        # paper, so that is when it is written to Grocy -- not at creation
        # (a location can still be renamed freely before anyone prints it)
        # and not on the scan path (which stays read-only). Never overwrites:
        # freeze_slug() returns '' for a location that already has one.
        frozen = 0
        for row in rows:
            new_slug = loc.freeze_slug(row)
            if not new_slug:
                continue
            if grocy_client.set_userfields('locations', row.get('id'),
                                           {'slug': new_slug}):
                row.setdefault('userfields', {})['slug'] = new_slug
                frozen += 1
        if frozen:
            logger.info(f"📍 froze {frozen} location slug(s) at print time")

        sheet = (request.args.get('sheet') or '').strip()
        if sheet:
            buf = generate_label_sheet_pdf(rows, sheet_key=sheet)
            name = f'stock-location-labels-{sheet}.pdf'
        else:
            buf = generate_location_sheet_pdf(
                rows, barcode_format=request.args.get('format', 'qr'))
            name = 'stock-location-codes.pdf'
        # Opened in a tab like the control sheet, not force-downloaded: the
        # point is to look at it, then print it.
        return send_file(buf, mimetype='application/pdf', as_attachment=False,
                         download_name=name)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    except Exception as err:                                     # noqa: BLE001
        logger.error(f"location sheet failed: {err}")
        return jsonify({"error": str(err)[:200]}), 500


def _connected_guns():
    """
    The active devices grouped into PHYSICAL GUNS.

    Listing /dev/hidraw0..3 says "four scanners" to anyone reading it, and
    there are two: each gun exposes several HID interfaces. Grouping by USB id
    is the same identity the mode binding and the location tracker use, so the
    list on screen matches the thing those are actually keyed on.
    """
    guns, unresolved = {}, []
    for dev in scanner.active_devices:
        usb = device_usb_id(dev)
        if usb is None:
            unresolved.append(dev)
            continue
        guns.setdefault(usb, []).append(dev)

    out = []
    for usb, devices in sorted(guns.items()):
        out.append({"usb": usb, "label": config.gun_label(f"usb:{usb}"),
                    "named": f"usb:{usb}" != config.gun_label(f"usb:{usb}"),
                    "devices": sorted(devices)})
    for dev in sorted(unresolved):
        # Named apart rather than hidden: an unreadable USB id means this gun
        # falls back to the global mode and cannot hold a location, which is
        # worth seeing rather than discovering at a shelf.
        out.append({"usb": None, "label": dev, "named": False,
                    "devices": [dev]})
    return out


def _guns_for_ui():
    """
    The tracker snapshot with a human name on each gun.

    The key stays in the payload as `gun` because that is what the clear
    endpoint takes; `label` is only ever for reading.
    """
    out = {}
    for key, value in location_tracker.snapshot().items():
        entry = dict(value)
        entry['gun'] = key
        entry['label'] = config.gun_label(key)
        out[key] = entry
    return out


@app.route('/api/locations')
def locations_state():
    """
    Where each gun currently is, with its remaining idle seconds, plus the
    location codes themselves so a label can be checked against the house.
    """
    try:
        rows = grocy_client.get_locations() if grocy_client else []
    except Exception:                                            # noqa: BLE001
        rows = []
    return jsonify({
        "idle_seconds": location_tracker.idle_seconds,
        "guns": _guns_for_ui(),
        # The STORED slug where there is one -- what a printed label carries.
        "codes": [{"id": r.get("id"), "name": r.get("name"),
                   "barcode": loc.barcode_for_location(r),
                   "frozen": bool(loc.stored_slug(r))} for r in rows],
    })


@app.route('/api/picture/<int:product_id>')
def product_picture(product_id):
    """
    Stream a product's picture out of Grocy.

    Exists so an <img> tag can show it: Grocy wants the API key in a header,
    which a browser cannot attach, and putting it in the query string would
    write a credential into every history and access log on the way.

    Cached for an hour. A product picture does not change, and the scan feed
    re-renders every two seconds -- without this, standing at a shelf means
    re-fetching the same photograph 1800 times an hour.
    """
    if not grocy_client:
        return jsonify({"error": "no grocy client"}), 503
    info = grocy_client.get_product_info(product_id) or {}
    product = info.get('product') if isinstance(info.get('product'), dict) else info
    name = str((product or {}).get('picture_file_name') or '').strip()
    if not name:
        return jsonify({"error": "no picture"}), 404

    seg = quote(base64.b64encode(name.encode()).decode(), safe='')
    try:
        r = requests.get(f"{grocy_client.url}/api/files/productpictures/{seg}",
                         headers={'GROCY-API-KEY': grocy_client.api_key},
                         timeout=10)
    except Exception as err:                                     # noqa: BLE001
        logger.debug(f"picture fetch failed (ignored): {err}")
        return jsonify({"error": "fetch failed"}), 502
    if r.status_code != 200:
        return jsonify({"error": f"grocy said {r.status_code}"}), r.status_code

    resp = make_response(r.content)
    resp.headers['Content-Type'] = r.headers.get('Content-Type', 'image/jpeg')
    resp.headers['Cache-Control'] = 'private, max-age=3600'
    return resp


@app.route('/api/locations/clear', methods=['POST'])
def clear_location():
    """
    Stop scanning into a shelf, without waiting out the 10-minute expiry.

    The expiry exists so a forgotten gun cannot misfile the next cupboard. It
    is a backstop, not a workflow: when you have finished a shelf and are
    walking to the next room, ten minutes of "everything lands in Spice
    Cabinet" is exactly the window this feature exists to close.

    Clearing REVERTS TO THE PRESET, which is the same thing expiry does and
    the same thing a rejected location code does. There is deliberately no
    third state -- `products.location_id` is NOT NULL, so "no location" is not
    something Grocy can hold.
    """
    data = request.get_json(silent=True) or {}
    gun = str(data.get('gun') or '').strip()
    if gun:
        cleared = 1 if location_tracker.clear_key(gun) else 0
    else:
        cleared = location_tracker.clear_all()
    logger.info(f"📍 Location focus cleared ({cleared} gun(s)) — back to the preset")
    return jsonify({"success": True, "cleared": cleared,
                    "guns": _guns_for_ui()})


@app.route('/api/scans')
def get_scans():
    """Get recent scans."""
    return jsonify(recent_scans)


@app.route('/api/scans/recent')
def get_scans_recent():
    """
    The last few scans, wrapped in an OBJECT rather than returned as a bare
    array.

    The shape is the whole point. A consumer that wants the feed as one value
    -- a Home Assistant REST sensor, say -- cannot hoist a top-level array into
    an attribute, so it ends up declaring one sensor per index with a
    `$[0]`, `$[1]`... path. Those warn on EVERY poll whenever the list is
    shorter than the highest index, which after any scanner restart is always,
    until enough things have been scanned. Four sensors, four warnings, every
    ten seconds, saying nothing.

    A dict parses whether it holds nothing or fifty. `/api/scans` keeps
    returning the bare array for what already reads it.
    """
    try:
        n = max(1, min(50, int(request.args.get('n', 6))))
    except (TypeError, ValueError):
        n = 6
    # The picture is resolved HERE, not when the gun beeped. `image` was
    # stamped onto the record at scan time from the product as it was then, so
    # a photograph acquired afterwards -- by the pictures backfill, or by
    # anything that fills a product in later -- never reached a scan already in
    # the list. Blue Goose Field Pea sat in the feed as a placeholder while
    # Grocy held its picture.
    #
    # /api/picture/<id> re-reads Grocy on every request and 404s when there is
    # still no picture, so emitting the URL costs nothing here and the feed
    # heals itself on the next poll. Copy rather than mutate: the record is the
    # history, and it should keep saying what was true at the scan.
    scans = [
        {**s, 'image': f"api/picture/{s['product_id']}"}
        if not s.get('image') and s.get('product_id') else s
        for s in recent_scans[:n]
    ]
    return jsonify({
        'scans': scans,
        'count': len(scans),
        'total': len(recent_scans),
    })

@app.route('/api/scan', methods=['POST'])
def manual_scan():
    """Manual barcode entry."""
    data = request.get_json()
    barcode = data.get('barcode', '').strip()

    if barcode:
        handle_barcode(barcode)
        return jsonify({'success': True})

    return jsonify({'success': False, 'error': 'No barcode provided'}), 400

@app.route('/api/providers')
def providers():
    """The same check the startup log prints, on demand. Spends no quota."""
    try:
        days = int(request.args.get('days', 7))
    except (TypeError, ValueError):
        days = 7
    return jsonify({"days": days, "providers": validate_providers(days=days)})


@app.route('/api/lookup/<barcode>')
def lookup_barcode_readonly(barcode):
    """
    Resolve a barcode WITHOUT touching stock or creating anything.

    `/api/scan` is the only write path and always was; this is the read-only
    twin. It exists for three reasons:

      * retries (relookup) and live scans can share one implementation and one
        provider budget, instead of the retry tool keeping its own chain
      * a lookup fix can be verified by behaviour without writing to the
        household's real inventory -- which was impossible before, and is why an
        empty-title bug had to be confirmed from log timestamps
      * every attempt is logged, so a manual probe contributes to the same
        provider evidence as a real scan

    Returns which provider answered and what each one did on the way.
    """
    code = (barcode or "").strip()
    if not code:
        return jsonify({"error": "no barcode"}), 400
    product, source, attempts = lookup_chain(code)
    return jsonify({
        "barcode": code,
        "found": product is not None,
        "source": source,
        "product": product,
        "attempts": attempts,
    })


@app.route('/api/lookup-stats')
def lookup_stats():
    """Delegated to the Kitchen Stack engine, which owns the log now."""
    try:
        days = int(request.args.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    resp = requests.get(f"{KITCHEN_STACK_URL}/api/lookup-stats",
                        params={"days": days}, timeout=15)
    return jsonify(resp.json()), resp.status_code


@app.route('/api/status')
def status():
    """System status."""
    return jsonify({
        'grocy_configured': config.has_grocy,
        'grocy_connected': grocy_client is not None,
        'scanner_devices': scanner.active_devices,
        # Grouped into PHYSICAL guns as well as raw nodes. One gun is several
        # /dev/hidrawN interfaces -- 4 devices here are 2 guns -- so anything
        # reporting a count needs this one, not the length of the list above.
        # A dashboard saying "4 guns" is simply wrong.
        'guns': _connected_guns(),
        'gun_count': len(_connected_guns()),
        'scanner_active': scanner.running,
        'scan_count': len(recent_scans)
    })

@app.route('/api/create-product', methods=['POST'])
def create_product():
    """Create a new product with barcode and add to stock."""
    global current_quantity, current_mode

    data = request.get_json()
    barcode = data.get('barcode', '').strip()
    product_name = data.get('product_name', '').strip()

    if not barcode or not product_name:
        return jsonify({'success': False, 'error': 'Barcode and product name required'}), 400

    if not grocy_client:
        return jsonify({'success': False, 'error': 'Grocy not configured'}), 400

    try:
        # Create product in Grocy.
        #
        # FORK PATCH #1 (fire-and-forget), same reasoning as the scan path above:
        # a consume of a product that does not exist yet means "gone and needed",
        # and Grocy cannot hold negative stock. Create it at 0 with a reorder
        # point rather than attempting a consume that ConsumeProduct() rejects.
        min_stock = 0 if current_mode == 'add' else 1
        product_id = grocy_client.create_product(
            product_name,
            description=f"Created via Barcode Buddy",
            min_stock_amount=min_stock
        )
        if not product_id:
            return jsonify({'success': False, 'error': 'Failed to create product in Grocy'}), 500

        # Add barcode to product
        if not grocy_client.add_barcode_to_product(product_id, barcode):
            logger.warning(f"Product created but failed to add barcode")

        # Add to stock based on current mode
        amount = current_quantity if current_quantity > 0 else 1.0

        if current_mode == 'add':
            success = grocy_client.add_product(product_id, amount)
            action_text = "Added"
        else:  # consume mode
            # Upstream called consume_product() here, which always failed: a
            # freshly created product has 0 stock, so consuming any amount trips
            # "Amount to be consumed cannot be > current stock amount".
            success = True
            action_text = None  # nothing was booked; the reorder point carries it

        if success:
            # Reset quantity after successful operation
            current_quantity = 0.0

            if action_text is None:
                outcome = "flagged for reorder (0 stock, min 1)"
            else:
                outcome = f"{action_text.lower()} {amount}x to stock"

            # Create scan result
            scan_result = {
                'barcode': barcode,
                'timestamp': __import__('datetime').datetime.now().isoformat(),
                'status': 'success',
                'message': f"✨ Created '{product_name}' and {outcome}"
            }

            # Store in recent scans
            recent_scans.insert(0, scan_result)
            if len(recent_scans) > 50:
                recent_scans.pop()

            logger.info(f"✨ Created product '{product_name}' (ID: {product_id}) and {outcome}")

            return jsonify({'success': True, 'product_id': product_id, 'product_name': product_name})
        else:
            return jsonify({'success': False, 'error': f'Product created but failed to {action_text.lower()} to stock'}), 500

    except Exception as e:
        logger.error(f"Error creating product: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/download-quantity-barcodes')
def download_quantity_barcodes():
    """View PDF with quantity barcodes in browser."""
    try:
        pdf_buffer = generate_quantity_barcodes_pdf(barcode_format=config.barcode_format)
        return send_file(
            pdf_buffer,
            mimetype='application/pdf',
            as_attachment=False
        )
    except Exception as e:
        logger.error(f"Error generating PDF: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/pending')
def get_pending():
    """Get pending barcodes waiting for user decision."""
    return jsonify({
        'success': True,
        'pending': pending_barcodes,
        'count': len(pending_barcodes)
    })

@app.route('/api/available-products')
def get_available_products():
    """
    Get available products from aliases and Grocy.

    Query parameters:
    - search_term: Optional fuzzy matching search term
    - without_barcode: If true, only show products without barcodes (default: false)
    """
    try:
        from difflib import SequenceMatcher

        search_term = request.args.get('search_term', '').strip().lower()
        without_barcode = request.args.get('without_barcode', 'false').lower() == 'true'

        products = []

        # Get products from aliases if available
        if alias_client:
            aliases = alias_client.get_all_aliases()
            if aliases:
                for alias in aliases:
                    has_barcode = alias.get('has_grocy_barcode', False) or len(alias.get('barcodes', [])) > 0

                    # Skip if we only want products without barcodes
                    if without_barcode and has_barcode:
                        continue

                    products.append({
                        'source': 'alias',
                        'id': alias['grocy_product_id'],
                        'name': alias['grocy_product_name'],
                        'alias_name': alias['receipt_name'],
                        'barcodes': alias.get('barcodes', []),
                        'has_barcode': has_barcode,
                        'openfood_name': alias.get('openfood_name')
                    })

        # Also get all products from Grocy
        if grocy_client:
            grocy_products = grocy_client.get_all_products()
            if grocy_products:
                # Add products that aren't already in aliases
                alias_product_ids = {p['id'] for p in products}
                for gp in grocy_products:
                    if gp['id'] not in alias_product_ids:
                        # For Grocy products not in aliases, we assume they don't have barcodes
                        # Include them when filtering for products without barcodes
                        products.append({
                            'source': 'grocy',
                            'id': gp['id'],
                            'name': gp.get('name', 'Unknown'),
                            'alias_name': None,
                            'barcodes': [],
                            'has_barcode': False  # We don't know, assume no
                        })

        # Fuzzy matching if search term provided
        if search_term:
            for product in products:
                # Calculate match score against product name and alias name
                name_score = SequenceMatcher(None, search_term, product['name'].lower()).ratio()
                alias_score = 0
                if product.get('alias_name'):
                    alias_score = SequenceMatcher(None, search_term, product['alias_name'].lower()).ratio()

                # Use the better score
                product['match_score'] = max(name_score, alias_score)

            # Sort by match score (best first), then by name
            products.sort(key=lambda x: (-x['match_score'], x['name'].lower()))
        else:
            # Sort by name only
            products.sort(key=lambda x: x['name'].lower())

        return jsonify({
            'success': True,
            'products': products,
            'count': len(products),
            'search_term': search_term,
            'without_barcode': without_barcode
        })

    except Exception as e:
        logger.error(f"Error fetching available products: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/pending/resolve', methods=['POST'])
def resolve_pending():
    """Resolve a pending barcode - use existing product or create new."""
    global pending_barcodes

    try:
        data = request.get_json()
        barcode = data.get('barcode', '').strip()
        action = data.get('action')  # 'use_existing' or 'create_new'
        product_id = data.get('product_id')  # For use_existing
        product_name = data.get('product_name', '').strip()  # For create_new
        update_name = data.get('update_name', True)  # Whether to update product name (default: True)

        if not barcode or not action:
            return jsonify({'success': False, 'error': 'Missing barcode or action'}), 400

        # Find pending item
        pending_item = next((p for p in pending_barcodes if p['barcode'] == barcode), None)
        if not pending_item:
            return jsonify({'success': False, 'error': 'Pending barcode not found'}), 404

        if action == 'use_existing':
            # Use existing product
            if not product_id:
                return jsonify({'success': False, 'error': 'Missing product_id'}), 400

            product_id = int(product_id)

            # Get current product info
            product_info = grocy_client.get_product_info(product_id)
            if not product_info:
                return jsonify({'success': False, 'error': 'Failed to get product info'}), 500

            old_name = product_info.get('name', 'Unknown')
            new_name = pending_item['product_name']  # Name from OpenFoodFacts/UPC
            name_updated = False

            # Conditionally update product name based on checkbox
            if update_name:
                if grocy_client.update_product_name(product_id, new_name):
                    name_updated = True
                    product_name = new_name
                else:
                    logger.warning(f"Failed to update product name, continuing anyway...")
                    product_name = old_name
            else:
                product_name = old_name
                logger.info(f"Skipping product name update (user choice)")

            # Add barcode to product in Grocy
            if not grocy_client.add_barcode_to_product(product_id, barcode):
                return jsonify({'success': False, 'error': 'Failed to add barcode to product in Grocy'}), 500

            # Add to stock with pending quantity/mode
            amount = pending_item['quantity']
            mode = pending_item['mode']

            if mode == 'add':
                success = grocy_client.add_product(product_id, amount)
                action_text = "Added"
            else:
                success = grocy_client.consume_product(product_id, amount)
                action_text = "Removed"

            if not success:
                return jsonify({'success': False, 'error': f'Failed to {action_text.lower()} to stock'}), 500

            # Remove from pending
            pending_barcodes.remove(pending_item)

            # Log based on whether name was updated
            if name_updated:
                logger.info(f"✅ Resolved pending '{barcode}' → Updated product name: '{old_name}' → '{new_name}' (ID {product_id})")
                success_message = f"✅ Updated '{old_name}' → '{new_name}' and {action_text.lower()} {amount}x"
            else:
                logger.info(f"✅ Resolved pending '{barcode}' → Used existing product '{product_name}' (ID {product_id})")
                success_message = f"✅ Used existing product '{product_name}' and {action_text.lower()} {amount}x"

            logger.info(f"   Added barcode to product and {action_text.lower()} {amount}x to stock")

            return jsonify({
                'success': True,
                'message': success_message,
                'product_id': product_id,
                'product_name': product_name,
                'old_name': old_name,
                'new_name': new_name if name_updated else old_name,
                'name_updated': name_updated
            })

        elif action == 'create_new':
            # Create new product
            if not product_name:
                # Use name from external database
                product_name = pending_item['product_name']

            external_data = pending_item['external_data']
            description = f"{external_data.get('brand', '')} - {external_data.get('quantity', '')}".strip(' -')

            amount = pending_item['quantity']
            mode = pending_item['mode']

            # FORK PATCH #1 (fire-and-forget), same reasoning as the scan path:
            # resolving a pending item in consume mode means "gone and needed",
            # and a product created here has 0 stock. Carry that as a reorder
            # point instead of a consume ConsumeProduct() would reject.
            min_stock = 1 if mode != 'add' else 0
            product_id = grocy_client.create_product(product_name, description,
                                                     min_stock_amount=min_stock)
            if not product_id:
                return jsonify({'success': False, 'error': 'Failed to create product in Grocy'}), 500

            # Add barcode to product
            if not grocy_client.add_barcode_to_product(product_id, barcode):
                return jsonify({'success': False, 'error': 'Product created but failed to add barcode'}), 500

            # Add to stock
            if mode == 'add':
                success = grocy_client.add_product(product_id, amount)
                action_text = "Added"
            else:
                # Upstream consumed here, which always failed on a fresh product.
                success = True
                action_text = "Flagged for reorder"

            if not success:
                return jsonify({'success': False, 'error': f'Product created but failed to {action_text.lower()} to stock'}), 500

            # Remove from pending
            pending_barcodes.remove(pending_item)

            logger.info(f"✨ Resolved pending '{barcode}' → Created new product '{product_name}' (ID {product_id})")

            return jsonify({
                'success': True,
                'message': f"✨ Created new product '{product_name}' and {action_text.lower()} {amount}x",
                'product_id': product_id,
                'product_name': product_name
            })

        else:
            return jsonify({'success': False, 'error': 'Invalid action (must be use_existing or create_new)'}), 400

    except Exception as e:
        logger.error(f"Error resolving pending barcode: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/pending/change-name', methods=['POST'])
def change_product_name():
    """
    Change product name only - does NOT add to stock!
    Used for correcting receipt product names with OpenFoodFacts names.
    """
    global pending_barcodes

    try:
        data = request.get_json()
        barcode = data.get('barcode', '').strip()
        product_id = data.get('product_id')

        if not barcode or not product_id:
            return jsonify({'success': False, 'error': 'Missing barcode or product_id'}), 400

        product_id = int(product_id)

        # Find pending item
        pending_item = next((p for p in pending_barcodes if p['barcode'] == barcode), None)
        if not pending_item:
            return jsonify({'success': False, 'error': 'Pending barcode not found'}), 404

        # Get current product info
        product_info = grocy_client.get_product_info(product_id)
        if not product_info:
            return jsonify({'success': False, 'error': 'Failed to get product info'}), 500

        old_name = product_info.get('name', 'Unknown')
        new_name = pending_item['product_name']  # Name from OpenFoodFacts/UPC

        # Update product name in Grocy
        if not grocy_client.update_product_name(product_id, new_name):
            return jsonify({'success': False, 'error': 'Failed to update product name in Grocy'}), 500

        # Add barcode to product in Grocy
        if not grocy_client.add_barcode_to_product(product_id, barcode):
            return jsonify({'success': False, 'error': 'Name updated but failed to add barcode'}), 500

        # Update alias with OpenFoodFacts name and mark as having Grocy barcode
        if alias_client:
            try:
                # Find alias by product_id (reverse lookup)
                aliases = alias_client.get_all_aliases()
                matching_alias = next((a for a in aliases if a['grocy_product_id'] == product_id), None)

                if matching_alias:
                    receipt_name = matching_alias['receipt_name']
                    # Update OpenFoodFacts name
                    alias_client.update_openfood_name(receipt_name, new_name)
                    # Mark as having Grocy barcode
                    alias_client.mark_barcode_added_to_grocy(receipt_name, barcode)
                    logger.info(f"✅ Updated alias '{receipt_name}' with OpenFood name and barcode flag")
            except Exception as e:
                logger.warning(f"Failed to update alias, continuing: {e}")

        # Remove from pending
        pending_barcodes.remove(pending_item)

        logger.info(f"✏️ Changed product name: '{old_name}' → '{new_name}' (ID {product_id})")
        logger.info(f"   Added barcode {barcode} to product (NO stock change)")

        return jsonify({
            'success': True,
            'message': f"✏️ Renamed '{old_name}' → '{new_name}' and added barcode (no stock change)",
            'product_id': product_id,
            'old_name': old_name,
            'new_name': new_name,
            'barcode': barcode
        })

    except Exception as e:
        logger.error(f"Error changing product name: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    logger.info("🚀 Starting Barcode Buddy (Python)")
    logger.info(f"📱 Scanner: Auto-detecting all available devices")
    logger.info(f"🔗 Grocy: {'✅ Configured' if config.has_grocy else '❌ Not configured'}")
    log_provider_check()

    app.run(host='0.0.0.0', port=5000)
