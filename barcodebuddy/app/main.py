"""Main Flask application."""
from flask import Flask, render_template, jsonify, request, session, send_file
from flask_babel import Babel, gettext
import logging
import sys
import os
import requests
from config import Config
from grocy import GrocyClient
from scanner import ScannerHandler
from openfoodfacts import OpenFoodFactsClient
from upcdatabase import UPCDatabaseClient
from alias_client import AliasClient
from pdf_generator import generate_quantity_barcodes_pdf
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
openfoodfacts_client = OpenFoodFactsClient()
upcdatabase_client = UPCDatabaseClient()

# Store recent scans
recent_scans = []

# Store pending barcodes (waiting for user decision)
pending_barcodes = []

# Current quantity for next product scan (reset after each product)
# Starts at 0, defaults to 1 if no quantity barcode scanned
current_quantity = 0.0

# Current mode: 'add' or 'consume'
current_mode = 'add'

def handle_barcode(barcode: str):
    """Handle scanned barcode with automatic product creation."""
    global current_quantity, current_mode

    logger.info(f"📦 Processing barcode: {barcode}")

    scan_result = {
        'barcode': barcode,
        'timestamp': datetime.now().isoformat(),
        'status': 'unknown',
        'message': ''
    }

    # Check if this is a mode switch barcode
    if barcode == config.barcode_add:
        current_mode = 'add'
        scan_result['status'] = 'mode'
        scan_result['message'] = f"➕ Mode: ADD (adding to stock)"
        logger.info(f"➕ Mode switched to: ADD")
        # Store and return
        recent_scans.insert(0, scan_result)
        if len(recent_scans) > 50:
            recent_scans.pop()
        return
    elif barcode == config.barcode_consume:
        current_mode = 'consume'
        scan_result['status'] = 'mode'
        scan_result['message'] = f"➖ Mode: CONSUME (removing from stock)"
        logger.info(f"➖ Mode switched to: CONSUME")
        # Store and return
        recent_scans.insert(0, scan_result)
        if len(recent_scans) > 50:
            recent_scans.pop()
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
        recent_scans.insert(0, scan_result)
        if len(recent_scans) > 50:
            recent_scans.pop()
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
                recent_scans.insert(0, scan_result)
                if len(recent_scans) > 50:
                    recent_scans.pop()
                return

            # If product info wasn't in the barcode response, fetch it separately
            if not product_info:
                product_info = grocy_client.get_product_info(product_id)

            if product_info:
                product_name = product_info.get('name', 'Unknown')
                # Use current quantity, or default to 1 if no quantity barcode was scanned
                amount = current_quantity if current_quantity > 0 else 1.0

                # Add or consume based on current mode
                if current_mode == 'add':
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
                    alias_found = True

                    logger.info(f"🔗 Found product via alias: {product_name} (ID {product_id})")

                    # Use current quantity, or default to 1
                    amount = current_quantity if current_quantity > 0 else 1.0

                    # Add or consume based on current mode
                    if current_mode == 'add':
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

                if grocy_client and not external_product:
                    external_product = grocy_client.external_lookup(barcode)
                    if external_product:
                        database_name = "Grocy lookup plugin"

                if config.enable_openfoodfacts and not external_product:
                    external_product = openfoodfacts_client.lookup_barcode(barcode)
                    if external_product:
                        database_name = "OpenFoodFacts"

                if config.enable_upcdatabase and not external_product:
                    external_product = upcdatabase_client.lookup_barcode(barcode)
                    if external_product:
                        database_name = "UPC Database"

                if external_product:
                    # Step 4: Found in external database - auto-create and stock it.
                    #
                    # FORK PATCH #1 (fire-and-forget). Upstream parks the barcode in
                    # pending_barcodes and waits for a UI decision. We never want the
                    # scanner to block on a human: unknowns are created immediately as
                    # raw products and cleaned up later by tools/grocy_normalize.py +
                    # tools/grocy_review.py in the kitchen-stack repo.
                    product_name = external_product['name']
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
                        min_stock = 0 if current_mode == 'add' else 1
                        # Grocy's own lookup supplies location/unit resolved against
                        # the user's presets; the other sources supply nothing, and
                        # create_product() falls back for those.
                        product_id = grocy_client.create_product(
                            product_name,
                            description=f"Auto-created via Barcode Buddy from {database_name}",
                            min_stock_amount=min_stock,
                            location_id=external_product.get('location_id'),
                            qu_id_purchase=external_product.get('qu_id_purchase'),
                            qu_id_stock=external_product.get('qu_id_stock')
                        )

                    if not product_id:
                        scan_result['status'] = 'error'
                        scan_result['message'] = f"❌ Failed to create: {product_name}"
                        logger.error(f"❌ Could not create product '{product_name}' for barcode {barcode}")
                    else:
                        if not grocy_client.add_barcode_to_product(product_id, barcode):
                            logger.warning(f"Product {product_id} ready but failed to attach barcode {barcode}")

                        if current_mode == 'add':
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
                    # Not found anywhere
                    scan_result['status'] = 'not_found'
                    scan_result['message'] = f"❓ Barcode not found in Grocy, aliases, OpenFoodFacts, or UPC Database"
                    logger.warning(f"❓ Barcode {barcode} not found in any database")
    else:
        scan_result['status'] = 'no_grocy'
        scan_result['message'] = f"📦 Scanned (no Grocy configured)"

    # Store in recent scans (keep last 50)
    recent_scans.insert(0, scan_result)
    if len(recent_scans) > 50:
        recent_scans.pop()

# Initialize scanner (auto-detects all available devices)
scanner = ScannerHandler(None, handle_barcode)
scanner.start()

@app.route('/')
def index():
    """Main page."""
    return render_template('index.html',
                         has_grocy=config.has_grocy,
                         scanner_devices=scanner.active_devices,
                         current_locale=get_locale())

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

    app.run(host='0.0.0.0', port=5000)
