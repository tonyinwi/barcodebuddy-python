"""Client for Paperless Grocy Magic Alias API."""
import logging
import requests
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class AliasClient:
    """Client for interacting with Paperless Grocy Magic alias API."""

    def __init__(self, base_url: str):
        """
        Initialize alias client.

        Args:
            base_url: Base URL of Paperless Grocy Magic (e.g., http://localhost:5002)
        """
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json'
        })

    def test_connection(self) -> bool:
        """Test connection to Paperless Grocy Magic API."""
        try:
            response = self.session.get(f"{self.base_url}/api/status", timeout=5)
            if response.status_code == 200:
                logger.info("✅ Paperless Grocy Magic connection successful")
                return True
            else:
                logger.warning(f"⚠️  Paperless Grocy Magic returned status {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️  Paperless Grocy Magic connection failed: {e}")
            return False

    def find_by_barcode(self, barcode: str) -> Optional[Dict]:
        """
        Find alias by barcode.

        Args:
            barcode: Barcode to search for

        Returns:
            Alias dict if found, None otherwise
            Example: {
                'receipt_name': 'vorderhaxe',
                'grocy_product_id': 123,
                'grocy_product_name': 'Schweinshaxe gegart',
                'barcodes': ['4012345678901'],
                'notes': 'Auto-created from REWE receipt'
            }
        """
        try:
            response = self.session.get(
                f"{self.base_url}/api/aliases/barcode/{barcode}",
                timeout=5
            )

            if response.status_code == 200:
                data = response.json()
                if data.get('success') and data.get('found'):
                    alias = data['alias']
                    logger.info(f"🔗 Found alias via barcode '{barcode}': {alias['grocy_product_name']} (ID {alias['grocy_product_id']})")
                    return alias
                else:
                    logger.debug(f"No alias found for barcode '{barcode}'")
                    return None
            else:
                logger.warning(f"Alias API returned status {response.status_code} for barcode '{barcode}'")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"Error querying alias API for barcode '{barcode}': {e}")
            return None

    def add_barcode_to_alias(self, receipt_name: str, barcode: str) -> bool:
        """
        Add barcode to existing alias.

        Args:
            receipt_name: Name of the receipt item (alias key)
            barcode: Barcode to add

        Returns:
            True if successful, False otherwise
        """
        try:
            response = self.session.put(
                f"{self.base_url}/api/aliases/{receipt_name}/barcode",
                json={"barcode": barcode},
                timeout=5
            )

            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    logger.info(f"🔗 Added barcode '{barcode}' to alias '{receipt_name}'")
                    return True
                else:
                    logger.warning(f"Failed to add barcode to alias: {data.get('error')}")
                    return False
            elif response.status_code == 404:
                logger.debug(f"Alias '{receipt_name}' not found")
                return False
            elif response.status_code == 409:
                # Barcode already used by another product
                data = response.json()
                logger.warning(f"Barcode conflict: {data.get('error')}")
                return False
            else:
                logger.warning(f"Alias API returned status {response.status_code}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Error adding barcode to alias: {e}")
            return False

    def get_all_aliases(self) -> Optional[list]:
        """
        Get all aliases (for debugging/UI purposes).

        Returns:
            List of alias dicts, or None on error
        """
        try:
            response = self.session.get(
                f"{self.base_url}/api/aliases",
                timeout=5
            )

            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    return data.get('aliases', [])
                else:
                    return None
            else:
                logger.warning(f"Alias API returned status {response.status_code}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching aliases: {e}")
            return None
