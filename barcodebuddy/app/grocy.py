"""Grocy API client."""
import re
import time
import requests
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


def _tidy(value: str) -> str:
    """
    Collapse whitespace. Providers return things like
    "Spectrum,  The Hain Celestial Group  Inc." with doubled spaces, and a
    field that is going to be read by a human on a shopping list should not
    carry the provider's formatting accidents.
    """
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _barcode_userfields(brand: str, source: str) -> dict:
    """Only send fields that have a value -- a blank overwrite is still a write."""
    fields = {}
    if _tidy(brand):
        fields['brand'] = _tidy(brand)
    if _tidy(source):
        fields['source'] = _tidy(source)
    return fields


class GrocyClient:
    """Client for Grocy API."""

    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip('/')
        self.api_key = api_key
        self.headers = {
            'GROCY-API-KEY': api_key,
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
        # Use a session to persist cookies/connection
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def _request(self, method: str, endpoint: str, retry: bool = True, **kwargs) -> Optional[Dict[Any, Any]]:
        """Make API request with automatic retry on redirect."""
        import time
        url = f"{self.url}/api/{endpoint.lstrip('/')}"
        logger.debug(f"Grocy API call: {method} {url}")
        try:
            # Don't follow redirects - API should respond directly
            response = self.session.request(
                method,
                url,
                timeout=10,
                allow_redirects=False,
                **kwargs
            )
            logger.debug(f"Grocy response: Status {response.status_code}, Content-Type: {response.headers.get('Content-Type', 'unknown')}")

            # Check for redirects (means auth failed or session issue)
            if response.status_code in (301, 302, 303, 307, 308):
                if retry:
                    logger.warning(f"Grocy redirect detected, retrying in 1 second...")
                    time.sleep(1)
                    # Retry without further retries to avoid infinite loop
                    return self._request(method, endpoint, retry=False, **kwargs)
                else:
                    logger.error(f"Grocy returned redirect (status {response.status_code}) - API key may be invalid")
                    logger.error(f"Redirect location: {response.headers.get('Location', 'unknown')}")
                    return None

            # Handle 400 Bad Request
            if response.status_code == 400:
                error_text = response.text[:500] if response.text else "No error message"
                logger.error(f"Grocy returned 400 for endpoint: {endpoint}")
                logger.error(f"Response: {error_text}")
                return None

            response.raise_for_status()

            if not response.text:
                logger.warning("Grocy returned empty response")
                return {}

            return response.json()
        except requests.exceptions.JSONDecodeError as e:
            logger.error(f"Grocy API returned invalid JSON: {e}")
            logger.error(f"Response text: {response.text[:200]}")  # First 200 chars
            return None
        except requests.exceptions.HTTPError as e:
            # 404 means not found, which is expected for unknown barcodes
            if e.response.status_code == 404:
                logger.info(f"Grocy returned 404 (not found) for: {endpoint}")
            else:
                logger.error(f"Grocy API HTTP error: {e}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Grocy API request failed: {e}")
            return None

    def test_connection(self) -> bool:
        """Test Grocy connection with retry logic."""
        import time

        # First attempt
        result = self._request('GET', 'system/info')
        if result is not None:
            return True

        # If first attempt failed with redirect, wait and retry
        logger.info("First connection attempt failed, retrying in 2 seconds...")
        time.sleep(2)

        result = self._request('GET', 'system/info')
        return result is not None

    def find_product_by_barcode(self, barcode: str) -> Optional[Dict[Any, Any]]:
        """Find product by barcode."""
        result = self._request('GET', f'stock/products/by-barcode/{barcode}')
        return result

    def external_lookup(self, barcode: str) -> Optional[Dict[Any, Any]]:
        """
        Look up an unknown barcode via Grocy's own external lookup plugin.

        This delegates to whatever STOCK_BARCODE_LOOKUP_PLUGIN is configured in
        Grocy (e.g. UPCitemdb), which means lookup behaviour is tuned in one
        place -- Grocy -- rather than duplicated in this add-on.

        add=false so Grocy resolves the barcode WITHOUT creating anything: this
        add-on does its own create so it can handle name collisions and set a
        reorder point. (add=true would also throw on a duplicate product name.)

        Returns a dict in the same shape as the other lookup clients, with the
        Grocy-native product fields carried alongside. Grocy resolves those
        against the user's "presets for new products", so they are more correct
        than this client's first-location/first-unit fallbacks.

        Returns None when the plugin finds nothing (Grocy returns null) or when
        no lookup plugin is configured.
        """
        result = self._request('GET',
                               f'stock/barcodes/external-lookup/{barcode}?add=false')
        if not result or not isinstance(result, dict) or not result.get('name'):
            logger.info(f"❌ Not found via Grocy external lookup: {barcode}")
            return None

        logger.info(f"✅ Found via Grocy external lookup: {result['name']}")
        return {
            'name': result['name'],
            'barcode': barcode,
            # __brand is a non-standard key added by the UPCitemdb plugin in the
            # kitchen-stack repo. Grocy passes plugin output through untouched on
            # add=false, so it survives; absent with any other lookup plugin.
            'brand': result.get('__brand', ''),
            'quantity': '',
            'image_url': result.get('__image_url', ''),
            # Which provider actually answered, carried through from the
            # Kitchen Stack engine via the plugin. Without this the barcode's
            # source userfield says "Grocy lookup", which is the messenger,
            # not the source.
            '__source': result.get('__source', ''),
            # Grocy-native fields, already resolved against the user's presets.
            'location_id': result.get('location_id'),
            'qu_id_purchase': result.get('qu_id_purchase'),
            'qu_id_stock': result.get('qu_id_stock'),
        }

    def add_product(self, product_id: int, amount: float = 1.0) -> bool:
        """Add product to stock."""
        data = {
            'amount': amount,
            'transaction_type': 'purchase'
        }
        result = self._request('POST', f'stock/products/{product_id}/add', json=data)
        return result is not None

    def consume_product(self, product_id: int, amount: float = 1.0) -> bool:
        """Consume product from stock."""
        data = {
            'amount': amount,
            'transaction_type': 'consume'
        }
        result = self._request('POST', f'stock/products/{product_id}/consume', json=data)
        return result is not None

    def get_product_info(self, product_id: int) -> Optional[Dict[Any, Any]]:
        """Get product information."""
        return self._request('GET', f'objects/products/{product_id}')

    def get_all_products(self) -> Optional[list]:
        """Get all products from Grocy."""
        result = self._request('GET', 'objects/products')
        if result is not None:
            return result if isinstance(result, list) else []
        return None

    def _get_preset(self, setting: str) -> Optional[int]:
        """
        Read one of Grocy's "presets for new products" user settings.

        Grocy returns these as strings, and uses -1 to mean "not set".
        Returns the id as an int, or None when unset/unavailable.
        """
        settings = self._request('GET', 'user/settings')
        if not settings or not isinstance(settings, dict):
            return None
        try:
            value = int(settings.get(setting, -1))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def get_product_presets(self) -> dict:
        """
        Grocy's "presets for new products", resolved the way the lookup plugin
        resolved them.

        This matters more than it looks. Nothing on Grocy's API path consults
        these presets -- they are read by the barcode-lookup plugin and nowhere
        else, which is the only reason scans land in Big Pantry rather than
        whichever location sorts first. Calling UPCitemdb directly means this
        add-on must do that resolution itself or every scan lands on the wrong
        shelf.

        A preset of -1 means "unset", and falls back to the first location /
        first quantity unit exactly as the plugin did. Cached: user settings
        change roughly never, and this is on the scan path.
        """
        if getattr(self, "_presets", None) is not None:
            return self._presets

        def setting(key):
            try:
                r = self._request('GET', f'user/settings/{key}')
                return (r or {}).get('value')
            except Exception:                                    # noqa: BLE001
                return None

        loc = setting('product_presets_location_id')
        qu = setting('product_presets_qu_id')
        try:
            loc = int(loc)
        except (TypeError, ValueError):
            loc = -1
        try:
            qu = int(qu)
        except (TypeError, ValueError):
            qu = -1

        if loc == -1:
            loc = self.get_default_location_id()
        if qu == -1:
            qu = self.get_default_quantity_unit_id()

        self._presets = {"location_id": loc, "qu_id_purchase": qu,
                         "qu_id_stock": qu}
        logger.info(f"📍 Product presets resolved: location={loc} qu={qu}")
        return self._presets

    # Long enough to keep the scan path off a round trip, short enough that
    # adding a shelf in Grocy takes effect while you are still at the printer.
    LOCATIONS_TTL = 300

    def get_locations(self, force: bool = False) -> list:
        """
        Grocy's locations, cached with a TTL.

        The cache is here because this sits on the SCAN PATH: a location QR has
        to resolve without a round trip that somebody is standing there waiting
        for. It was previously cached forever, which was a real bug and a
        nastier one than "the printed sheet is stale".

        A location added in Grocy after the add-on started was invisible to
        `loc.resolve()`, so its freshly printed QR came back as *unknown
        location code* -- and a rejected code CLEARS the gun by design, so
        every following scan silently landed on the preset shelf. That is
        precisely the misfiling this whole feature exists to prevent, produced
        by the feature itself.

        `force=True` for anything human-initiated, like generating the label
        sheet: nobody is waiting on the scan path there, and a sheet must never
        be printed from a stale list.

        A FAILED FETCH KEEPS THE OLD LIST. Emptying it on a Grocy blip would
        make every location code unknown at once, which clears every gun --
        turning a momentary network problem into a shelf full of misfiled
        products. Stale beats empty here.
        """
        now = time.monotonic()
        fresh = (getattr(self, "_locations_cache", None) is not None
                 and now - getattr(self, "_locations_cached_at", 0) < self.LOCATIONS_TTL)
        if force or not fresh:
            fetched = self._request('GET', 'objects/locations')
            if fetched is not None:
                self._locations_cache = fetched
                self._locations_cached_at = now
        return getattr(self, "_locations_cache", None) or []

    def get_default_location_id(self) -> int:
        """
        Location for newly created products.

        Prefers Grocy's own "presets for new products" setting. Falling back to
        whichever location sorts first is almost always wrong -- on a typical
        setup that is a fridge, so shelf-stable goods land in cold storage.
        """
        preset = self._get_preset('product_presets_location_id')
        if preset is not None:
            logger.debug(f"Using preset location ID: {preset}")
            return preset

        result = self._request('GET', 'objects/locations')
        if result and len(result) > 0:
            location_id = result[0].get('id', 1)
            logger.debug(f"No location preset set; falling back to {location_id}")
            return location_id
        return 1  # Fallback to 1

    def get_default_quantity_unit_id(self) -> int:
        """
        Quantity unit for newly created products.

        Prefers Grocy's "presets for new products" setting, as above.
        """
        preset = self._get_preset('product_presets_qu_id')
        if preset is not None:
            logger.debug(f"Using preset quantity unit ID: {preset}")
            return preset

        result = self._request('GET', 'objects/quantity_units')
        if result and len(result) > 0:
            qu_id = result[0].get('id', 1)
            logger.debug(f"No quantity unit preset set; falling back to {qu_id}")
            return qu_id
        return 1  # Fallback to 1

    def find_product_group_id(self, name: str) -> Optional[int]:
        """
        A product group id by name, cached for the process.

        Looked up rather than configured, because a group id is exactly the kind
        of number that drifts: it differs between this Grocy and a restored
        backup, and a stale id in config fails silently -- Grocy accepts an
        integer matching no group, so products land in a group nobody can see.
        A name is stable, and a miss is visible.

        Returns None when the group does not exist, which is not an error: the
        caller omits the field and Grocy leaves the product ungrouped, exactly as
        before. Creating the group is a deliberate act, not a side effect of a
        scan.
        """
        cache = getattr(self, "_group_id_cache", None)
        if cache is None:
            cache = self._group_id_cache = {}
        key = (name or "").strip().lower()
        if key in cache:
            return cache[key]
        groups = self._request('GET', 'objects/product_groups') or []
        found = None
        for g in groups:
            if str(g.get('name', '')).strip().lower() == key:
                found = int(g['id'])
                break
        cache[key] = found
        if found is None:
            logger.warning(f"No product group named '{name}' in Grocy -- "
                           f"placeholders will be left ungrouped")
        return found

    def find_product_by_name(self, name: str) -> Optional[int]:
        """
        Find a product by its exact name (case-insensitive).

        Grocy enforces UNIQUE on products.name, so a name collision on create
        is not an error to retry but a signal that the product already exists.

        Returns the product ID if found, None otherwise.
        """
        products = self.get_all_products()
        if not products:
            return None

        target = name.strip().lower()
        for product in products:
            if str(product.get('name', '')).strip().lower() == target:
                product_id = product.get('id')
                return int(product_id) if product_id is not None else None
        return None

    def create_product(self, name: str, description: str = "",
                       min_stock_amount: float = 0,
                       location_id=None, qu_id_purchase=None,
                       qu_id_stock=None,
                       default_best_before_days: int = -1,
                       parent_product_id=None,
                       no_own_stock: int = 0,
                       product_group_id=None) -> Optional[int]:
        """
        Create a new product in Grocy.

        min_stock_amount sets the reorder point. A product at 0 stock with a
        minimum of 1 is counted by Grocy's GetMissingProducts and can be
        auto-added to the shopping list, which is how "gone and needed" is
        expressed -- Grocy cannot hold negative stock.

        default_best_before_days is -1, Grocy's sentinel for "never expires"
        (it stores 2999-12-31). Grocy reads this field as days-from-today, so
        its own default of 0 does not mean "untracked" -- it means DUE TODAY.
        Leaving it at 0 made every scanned item land already expiring, which
        lit the expiring-products sensor permanently and said nothing useful.
        Whether a pantry staple needs replacing is answered by
        min_stock_amount, not by a date.

        location_id / qu_id_* override the fallbacks below. Pass the values from
        external_lookup(): Grocy resolves those against the user's "presets for
        new products", whereas the fallbacks here just take whatever location and
        unit happen to sort first, which is usually the wrong shelf.

        Returns the product ID if successful, None otherwise.

        parent_product_id / no_own_stock build a generic parent with its
        variants underneath. no_own_stock is a database CHECK, not a hint:
        Grocy refuses to add stock to such a product outright, which is what
        guarantees stock lands on a child and never on the generic. A parent
        holding stock while its children were empty would make recipe
        resolution claim you own something you do not.

        """
        # Fall back to the first location / unit only when the caller has nothing
        # better. Anything from Grocy's own lookup is preferred.
        if location_id is None:
            location_id = self.get_default_location_id()
        if qu_id_purchase is None or qu_id_stock is None:
            default_qu_id = self.get_default_quantity_unit_id()
            qu_id_purchase = qu_id_purchase if qu_id_purchase is not None else default_qu_id
            qu_id_stock = qu_id_stock if qu_id_stock is not None else default_qu_id

        # Required fields for product creation
        data = {
            'name': name,
            'description': description,
            'location_id': location_id,
            'qu_id_purchase': qu_id_purchase,
            'qu_id_stock': qu_id_stock,
            'min_stock_amount': min_stock_amount,
            'default_best_before_days': default_best_before_days
        }
        if product_group_id is not None:
            data['product_group_id'] = product_group_id
        if parent_product_id is not None:
            data['parent_product_id'] = parent_product_id
        if no_own_stock:
            data['no_own_stock'] = 1
        result = self._request('POST', 'objects/products', json=data)
        if result and 'created_object_id' in result:
            # Grocy returns created_object_id as a string but reports ids as ints
            # everywhere else. Normalise so callers can compare the result of this
            # against find_product_by_name() without a type mismatch.
            product_id = int(result['created_object_id'])
            logger.info(f"✅ Created product in Grocy: {name} (ID: {product_id})")

            # Tag it for the cleanup pipeline. The name here is whatever the
            # lookup returned -- a raw title, not how a recipe would name it --
            # so it needs review before it is trustworthy. Marking this
            # explicitly makes the state visible in Grocy's UI instead of being
            # inferred from a blank field.
            #
            # tools/grocy_normalize.py in kitchen-stack treats both "" and "raw"
            # as unreviewed; do not narrow that without changing this.
            self.set_userfields('products', product_id, {'review_status': 'raw'})
            return product_id
        return None

    def set_userfields(self, entity: str, object_id, fields: Dict[str, Any]) -> bool:
        """
        Set userfield values on a Grocy object.

        Silently no-ops on an empty payload. Userfields must already exist in
        Grocy (created by tools/grocy_normalize.py --setup in kitchen-stack); if
        one does not, Grocy rejects the whole call, so this logs and moves on
        rather than failing the scan around it.
        """
        fields = {k: v for k, v in fields.items() if v not in (None, '')}
        if not fields:
            return True

        result = self._request('PUT', f'userfields/{entity}/{object_id}', json=fields)
        if result is not None:
            logger.info(f"✅ Set userfields on {entity}/{object_id}: {fields}")
            return True
        logger.warning(f"Could not set userfields on {entity}/{object_id}: {fields}")
        return False

    def add_barcode_to_product(self, product_id: int, barcode: str,
                               brand: str = '', source: str = '',
                               detail: str = '') -> bool:
        """
        Add a barcode to an existing product.

        brand and source are stored as userfields on the barcode row, not on the
        product. Brand is a barcode attribute in this data model: the generic
        product stays the source of truth so any variant satisfies a recipe,
        while each scanned barcode records which brand it actually was.

        `detail` is the full name the lookup returned, before normalisation.
        Once several barcodes share one generic product -- two brands of coconut
        oil, say -- the product name no longer says which is which, and the
        original retail name is the only record of what was actually on the
        shelf. Without it, "I clearly bought two for some reason" has no answer.

        It is written to the barcode's NATIVE `note` column rather than a
        userfield. Same place, one less field to define, and Grocy already
        renders it as the Note column in the barcode table on the product page --
        so it is visible where somebody would look for it, without any setup.

        Returns True if the barcode was added. Userfield failures are logged but
        do not fail the call -- the barcode mapping is what matters.
        """
        data = {
            'product_id': product_id,
            'barcode': barcode
        }
        if _tidy(detail):
            data['note'] = _tidy(detail)
        result = self._request('POST', 'objects/product_barcodes', json=data)
        if not result:
            return False

        logger.info(f"✅ Added barcode {barcode} to product {product_id}")

        barcode_id = result.get('created_object_id')
        if barcode_id and (brand or source):
            self.set_userfields('product_barcodes', barcode_id,
                                _barcode_userfields(brand, source))
        return True

    def update_product_name(self, product_id: int, new_name: str) -> bool:
        """
        Update the name of an existing product.

        Returns True if successful, False otherwise.
        """
        # First, get the current product data
        product = self.get_product_info(product_id)
        if not product:
            logger.error(f"Failed to get product {product_id} for name update")
            return False

        # Update the name field
        product['name'] = new_name

        # Send the complete product back to Grocy
        result = self._request('PUT', f'objects/products/{product_id}', json=product)
        if result:
            logger.info(f"✅ Updated product {product_id} name to '{new_name}'")
            return True
        return False
