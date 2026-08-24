"""UPC Database API client for barcode lookup."""
import requests
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class UPCDatabaseClient:
    """
    Client for upcdatabase.org.

    An API key is REQUIRED, despite what older docs say. Without one the
    endpoint answers HTTP 200 with {"success": false, "error": {"message":
    "Your API Key is invalid..."}} -- so nothing raises, the lookup quietly
    returns nothing, and the log line says "not found". That is how this
    fallback sat dead and unnoticed while UPCitemdb was being rate-limited
    with nothing behind it.

    Free tier is 100 lookups/day, resetting nightly -- the same order as
    UPCitemdb's trial, so running both roughly doubles the daily budget.
    Paid tiers start at $2.50/month for 1,000/day.
    """

    BASE_URL = "https://api.upcdatabase.org/product"

    def __init__(self, api_key: str = ""):
        self.api_key = (api_key or "").strip()

    def lookup_barcode(self, barcode: str) -> Optional[Dict[Any, Any]]:
        """
        Look up a barcode in UPC Database.

        Returns product info if found, None otherwise.
        Note: Free tier has rate limits (~100 requests/day)
        """
        try:
            if not self.api_key:
                logger.info("⏭  UPC Database has no API key configured - skipping "
                            "(set upcdatabase_api_key; the free tier is 100/day)")
                return None

            url = f"{self.BASE_URL}/{barcode}"
            logger.info(f"Looking up barcode in UPC Database: {barcode}")

            response = requests.get(url, params={"apikey": self.api_key}, timeout=10)

            # UPC Database returns 404 if not found
            if response.status_code == 404:
                logger.info(f"❌ Not found in UPC Database: {barcode}")
                return None

            response.raise_for_status()
            data = response.json()

            if data.get('success'):
                # Extract relevant information
                product_info = {
                    'name': data.get('title', 'Unknown Product'),
                    'barcode': barcode,
                    'brand': data.get('brand', ''),
                    'quantity': '',
                    'image_url': '',
                    'categories': data.get('category', ''),
                    'description': data.get('description', '')
                }

                logger.info(f"✅ Found in UPC Database: {product_info['name']}")
                return product_info
            else:
                # Distinguish a real miss from a broken key. Reporting an auth
                # failure as "not found" is what hid this for weeks.
                message = str((data.get("error") or {}).get("message", ""))
                if "api key" in message.lower():
                    logger.error(f"🔑 UPC Database rejected the API key: {message}")
                else:
                    logger.info(f"❌ Not found in UPC Database: {barcode}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"UPC Database API error: {e}")
            return None
