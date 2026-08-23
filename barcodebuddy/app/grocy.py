"""Grocy API client."""
import requests
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


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
            'brand': '',
            'quantity': '',
            'image_url': result.get('__image_url', ''),
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
                       qu_id_stock=None) -> Optional[int]:
        """
        Create a new product in Grocy.

        min_stock_amount sets the reorder point. A product at 0 stock with a
        minimum of 1 is counted by Grocy's GetMissingProducts and can be
        auto-added to the shopping list, which is how "gone and needed" is
        expressed -- Grocy cannot hold negative stock.

        location_id / qu_id_* override the fallbacks below. Pass the values from
        external_lookup(): Grocy resolves those against the user's "presets for
        new products", whereas the fallbacks here just take whatever location and
        unit happen to sort first, which is usually the wrong shelf.

        Returns the product ID if successful, None otherwise.
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
            'min_stock_amount': min_stock_amount
        }
        result = self._request('POST', 'objects/products', json=data)
        if result and 'created_object_id' in result:
            # Grocy returns created_object_id as a string but reports ids as ints
            # everywhere else. Normalise so callers can compare the result of this
            # against find_product_by_name() without a type mismatch.
            product_id = int(result['created_object_id'])
            logger.info(f"✅ Created product in Grocy: {name} (ID: {product_id})")
            return product_id
        return None

    def add_barcode_to_product(self, product_id: int, barcode: str) -> bool:
        """
        Add a barcode to an existing product.

        Returns True if successful, False otherwise.
        """
        data = {
            'product_id': product_id,
            'barcode': barcode
        }
        result = self._request('POST', 'objects/product_barcodes', json=data)
        if result:
            logger.info(f"✅ Added barcode {barcode} to product {product_id}")
            return True
        return False

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
