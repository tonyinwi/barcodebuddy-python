"""Main Flask application."""
from flask import Flask, render_template, jsonify, request, session, send_file
from flask_babel import Babel, gettext
import logging
import sys
import os
import requests
import threading
from config import Config
from grocy import GrocyClient
from scanner import ScannerHandler, device_usb_id
import locations as loc
from alias_client import AliasClient
from pdf_generator import generate_quantity_barcodes_pdf, generate_location_sheet_pdf
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
    if not is_gtin(barcode):
        logger.info(f"⏭  {barcode} is not a GTIN ({len(barcode)} digits) - "
                    "skipping external lookup")
        return None, None, [{"provider": "(chain)",
                             "outcome": "skipped_non_gtin", "latency_ms": 0}]
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


def finish_scan(scan_result: dict):
    """Record a scan in recent history and tell Home Assistant about it."""
    recent_scans.insert(0, scan_result)
    if len(recent_scans) > 50:
        recent_scans.pop()
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
        'device': device
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
    location_tracker.touch(device)

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

            # If product info wasn't in the barcode response, fetch it separately
            if not product_info:
                product_info = grocy_client.get_product_info(product_id)

            if product_info:
                product_name = product_info.get('name', 'Unknown')
                scan_result['product'] = product_name
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
                    amount = current_quantity if current_quantity > 0 else 1.0

                    # Grocy enforces UNIQUE on products.name, so a title we have already
                    # seen under a different barcode must attach to the existing product
                    # instead of failing the create. Same name = same product; this is
                    # free dedup at intake.
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
                                source=database_name or ''):
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
                    amount = current_quantity if current_quantity > 0 else 1.0

                    product_id = grocy_client.find_product_by_name(product_name)
                    if product_id:
                        logger.info(f"🔗 Placeholder '{product_name}' already exists (ID {product_id})")
                    else:
                        min_stock = 0 if mode == 'add' else 1
                        product_id = grocy_client.create_product(
                            product_name,
                            description="Auto-created via Barcode Buddy - not found in any database",
                            min_stock_amount=min_stock
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
                         current_locale=get_locale())

@app.route('/api/download-location-sheet')
def download_location_sheet():
    """A printable QR sheet, generated from Grocy's ACTUAL locations."""
    try:
        rows = grocy_client.get_locations() if grocy_client else []
        if not rows:
            return jsonify({"error": "no locations available from Grocy"}), 503
        fmt = request.args.get('format', 'qr')
        buf = generate_location_sheet_pdf(rows, barcode_format=fmt)
        # Opened in a tab like the control sheet, not force-downloaded: the
        # point is to look at it, then print it.
        return send_file(buf, mimetype='application/pdf', as_attachment=False,
                         download_name='kitchen-location-codes.pdf')
    except Exception as err:                                     # noqa: BLE001
        logger.error(f"location sheet failed: {err}")
        return jsonify({"error": str(err)[:200]}), 500


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
        "guns": location_tracker.snapshot(),
        "codes": [{"id": r.get("id"), "name": r.get("name"),
                   "barcode": loc.barcode_for(r.get("name"))} for r in rows],
    })


@app.route('/api/scans')
def get_scans():
    """Get recent scans."""
    return jsonify(recent_scans)

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
