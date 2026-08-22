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

    def get_default_location_id(self) -> int:
        """Get the first available location ID from Grocy."""
        result = self._request('GET', 'objects/locations')
        if result and len(result) > 0:
            location_id = result[0].get('id', 1)
            logger.debug(f"Using location ID: {location_id}")
            return location_id
        return 1  # Fallback to 1

    def get_default_quantity_unit_id(self) -> int:
        """Get the first available quantity unit ID from Grocy."""
        result = self._request('GET', 'objects/quantity_units')
        if result and len(result) > 0:
            qu_id = result[0].get('id', 1)
            logger.debug(f"Using quantity unit ID: {qu_id}")
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
                       min_stock_amount: float = 0) -> Optional[int]:
        """
        Create a new product in Grocy.

        min_stock_amount sets the reorder point. A product at 0 stock with a
        minimum of 1 is counted by Grocy's GetMissingProducts and can be
        auto-added to the shopping list, which is how "gone and needed" is
        expressed -- Grocy cannot hold negative stock.

        Returns the product ID if successful, None otherwise.
        """
        # Get default IDs from Grocy
        location_id = self.get_default_location_id()
        qu_id = self.get_default_quantity_unit_id()

        # Required fields for product creation
        data = {
            'name': name,
            'description': description,
            'location_id': location_id,
            'qu_id_purchase': qu_id,
            'qu_id_stock': qu_id,
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
