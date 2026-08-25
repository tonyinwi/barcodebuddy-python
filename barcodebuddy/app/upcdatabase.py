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

    @staticmethod
    def _same_gtin(returned: str, queried: str) -> bool:
        """
        Is the code we got back the SAME GTIN we asked about?

        Not a string comparison. upcdatabase.org zero-pads UPC-A to EAN-13 --
        ask for 049000006346 and it answers 0049000006346 -- so an exact check
        would reject its own correct answers and silently kill the provider.
        GTIN-8/12/13/14 are one number at four widths, so compare padded to 14.

        This still catches the thing the check is FOR: a provider handing back
        a different product's code. The zero-padding trap that produced
        "Bonbebe Fruithapje" is stopped earlier, by is_gtin() -- 55540 is five
        digits and never reaches any provider.
        """
        a = "".join(c for c in str(returned or "") if c.isdigit())
        b = "".join(c for c in str(queried or "") if c.isdigit())
        return bool(a) and bool(b) and a.zfill(14) == b.zfill(14)

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
                returned = data.get('barcode', '')
                if returned and not self._same_gtin(returned, barcode):
                    logger.warning(f"⚠️  UPC Database answered for {returned} when asked "
                                   f"about {barcode} - different product, discarding")
                    return None

                # "success" is NOT the same as a usable answer. This API returns
                # success:true with title:"" for barcodes it merely knows about --
                # 049000006346 (Coca-Cola) is one. A .get(key, default) does not
                # catch that, because the default only fires when the key is
                # ABSENT, not when it is empty. Left alone, the empty string
                # becomes the product name in Grocy.
                #
                # A blank name is worse than no answer: "Unknown 049000006346"
                # looks unfinished and gets fixed in review, while a nameless
                # product is invisible. So: no name, no hit.
                name = (str(data.get('title') or '').strip()
                        or str(data.get('description') or '').strip())
                if not name:
                    logger.info(f"❌ UPC Database knows {barcode} but has no name for "
                                "it (empty title) - treating as a miss")
                    return None

                product_info = {
                    'name': name,
                    'barcode': barcode,
                    'brand': str(data.get('brand') or '').strip(),
                    'quantity': '',
                    'image_url': '',
                    'categories': str(data.get('category') or '').strip(),
                    'description': str(data.get('description') or '').strip()
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
