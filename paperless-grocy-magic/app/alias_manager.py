"""Product Alias Manager for mapping receipt names to Grocy products."""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ProductAlias:
    """Represents a cross-reference mapping: Receipt name ↔ OpenFoodFacts name ↔ Barcode ↔ Grocy product."""

    def __init__(self, receipt_name: str, grocy_product_id: int, grocy_product_name: str,
                 barcodes: List[str] = None, notes: str = "", openfood_name: str = None,
                 has_grocy_barcode: bool = False):
        self.receipt_name = receipt_name.strip().lower()  # Normalize for matching
        self.grocy_product_id = grocy_product_id
        self.grocy_product_name = grocy_product_name
        self.barcodes = barcodes or []  # List of barcodes for this product
        self.notes = notes
        self.openfood_name = openfood_name  # Full name from OpenFoodFacts/UPC (if available)
        self.has_grocy_barcode = has_grocy_barcode  # True if barcode is registered in Grocy

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'receipt_name': self.receipt_name,
            'grocy_product_id': self.grocy_product_id,
            'grocy_product_name': self.grocy_product_name,
            'barcodes': self.barcodes,
            'notes': self.notes,
            'openfood_name': self.openfood_name,
            'has_grocy_barcode': self.has_grocy_barcode
        }

    @staticmethod
    def from_dict(data: dict) -> 'ProductAlias':
        """Create ProductAlias from dictionary."""
        return ProductAlias(
            receipt_name=data['receipt_name'],
            grocy_product_id=data['grocy_product_id'],
            grocy_product_name=data['grocy_product_name'],
            barcodes=data.get('barcodes', []),
            notes=data.get('notes', ''),
            openfood_name=data.get('openfood_name'),
            has_grocy_barcode=data.get('has_grocy_barcode', False)
        )


class AliasManager:
    """Manages product name aliases for receipt matching."""

    def __init__(self, alias_file_path: str = '/data/product_aliases.json'):
        self.alias_file = Path(alias_file_path)
        self.aliases: Dict[str, ProductAlias] = {}  # Key: normalized receipt name
        self._load_aliases()

    def _load_aliases(self):
        """Load aliases from JSON file."""
        try:
            if self.alias_file.exists():
                with open(self.alias_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for alias_data in data.get('aliases', []):
                        alias = ProductAlias.from_dict(alias_data)
                        self.aliases[alias.receipt_name] = alias
                logger.info(f"Loaded {len(self.aliases)} product aliases from {self.alias_file}")
            else:
                logger.info(f"No alias file found at {self.alias_file}, starting with empty aliases")
        except Exception as e:
            logger.error(f"Error loading aliases: {e}")
            self.aliases = {}

    def _save_aliases(self):
        """Save aliases to JSON file."""
        try:
            # Ensure directory exists
            self.alias_file.parent.mkdir(parents=True, exist_ok=True)

            data = {
                'aliases': [alias.to_dict() for alias in self.aliases.values()]
            }

            with open(self.alias_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            logger.info(f"Saved {len(self.aliases)} aliases to {self.alias_file}")
        except Exception as e:
            logger.error(f"Error saving aliases: {e}")

    def add_alias(self, receipt_name: str, grocy_product_id: int, grocy_product_name: str,
                  barcodes: List[str] = None, notes: str = "") -> bool:
        """Add or update an alias."""
        try:
            alias = ProductAlias(receipt_name, grocy_product_id, grocy_product_name, barcodes, notes)
            self.aliases[alias.receipt_name] = alias
            self._save_aliases()
            barcode_info = f" with {len(alias.barcodes)} barcode(s)" if alias.barcodes else ""
            logger.info(f"Added alias: '{receipt_name}' → '{grocy_product_name}' (ID {grocy_product_id}){barcode_info}")
            return True
        except Exception as e:
            logger.error(f"Error adding alias: {e}")
            return False

    def remove_alias(self, receipt_name: str) -> bool:
        """Remove an alias by receipt name."""
        try:
            normalized_name = receipt_name.strip().lower()
            if normalized_name in self.aliases:
                del self.aliases[normalized_name]
                self._save_aliases()
                logger.info(f"Removed alias for '{receipt_name}'")
                return True
            else:
                logger.warning(f"No alias found for '{receipt_name}'")
                return False
        except Exception as e:
            logger.error(f"Error removing alias: {e}")
            return False

    def get_alias(self, receipt_name: str) -> Optional[ProductAlias]:
        """Get alias for a receipt product name (case-insensitive)."""
        normalized_name = receipt_name.strip().lower()
        return self.aliases.get(normalized_name)

    def find_grocy_product(self, receipt_name: str) -> Optional[Tuple[int, str]]:
        """
        Find Grocy product ID and name for a receipt product name.
        Returns (product_id, product_name) or None if no alias found.
        """
        alias = self.get_alias(receipt_name)
        if alias:
            return (alias.grocy_product_id, alias.grocy_product_name)
        return None

    def find_by_barcode(self, barcode: str) -> Optional[ProductAlias]:
        """
        Find alias by barcode.
        Returns ProductAlias or None if no alias has this barcode.
        """
        barcode = barcode.strip()
        for alias in self.aliases.values():
            if barcode in alias.barcodes:
                logger.info(f"Found alias by barcode '{barcode}': {alias.grocy_product_name} (ID {alias.grocy_product_id})")
                return alias
        return None

    def add_barcode_to_alias(self, receipt_name: str, barcode: str) -> bool:
        """
        Add a barcode to an existing alias.
        Returns True if successful, False if alias not found or error.
        """
        try:
            alias = self.get_alias(receipt_name)
            if not alias:
                logger.warning(f"Cannot add barcode: alias '{receipt_name}' not found")
                return False

            barcode = barcode.strip()
            if barcode not in alias.barcodes:
                alias.barcodes.append(barcode)
                self._save_aliases()
                logger.info(f"Added barcode '{barcode}' to alias '{receipt_name}'")
                return True
            else:
                logger.warning(f"Barcode '{barcode}' already exists for alias '{receipt_name}'")
                return False
        except Exception as e:
            logger.error(f"Error adding barcode to alias: {e}")
            return False

    def list_all_aliases(self) -> List[dict]:
        """Get all aliases as list of dictionaries."""
        return [alias.to_dict() for alias in self.aliases.values()]

    def get_count(self) -> int:
        """Get number of aliases."""
        return len(self.aliases)

    def update_openfood_name(self, receipt_name: str, openfood_name: str) -> bool:
        """
        Update the OpenFoodFacts name for an alias.
        Used when barcode is scanned and external database provides a better name.
        """
        try:
            alias = self.get_alias(receipt_name)
            if not alias:
                logger.warning(f"Cannot update OpenFood name: alias '{receipt_name}' not found")
                return False

            alias.openfood_name = openfood_name
            self._save_aliases()
            logger.info(f"Updated OpenFood name for '{receipt_name}' to '{openfood_name}'")
            return True
        except Exception as e:
            logger.error(f"Error updating OpenFood name: {e}")
            return False

    def mark_barcode_added_to_grocy(self, receipt_name: str, barcode: str = None) -> bool:
        """
        Mark that a barcode has been added to Grocy for this alias.
        This indicates the product is fully set up (has barcode in Grocy).
        """
        try:
            alias = self.get_alias(receipt_name)
            if not alias:
                logger.warning(f"Cannot mark barcode: alias '{receipt_name}' not found")
                return False

            alias.has_grocy_barcode = True
            if barcode and barcode not in alias.barcodes:
                alias.barcodes.append(barcode)
            self._save_aliases()
            logger.info(f"Marked '{receipt_name}' as having Grocy barcode")
            return True
        except Exception as e:
            logger.error(f"Error marking barcode: {e}")
            return False

    def get_aliases_without_grocy_barcode(self) -> List[ProductAlias]:
        """
        Get all aliases that don't have a barcode registered in Grocy yet.
        Used for showing products that still need barcode assignment.
        """
        return [alias for alias in self.aliases.values() if not alias.has_grocy_barcode]

    def get_alias_by_product_id(self, product_id: int) -> Optional[ProductAlias]:
        """
        Find alias by Grocy product ID.
        Useful for reverse lookups.
        """
        for alias in self.aliases.values():
            if alias.grocy_product_id == product_id:
                return alias
        return None

    def clear_all(self) -> bool:
        """Remove all aliases (use with caution!)."""
        try:
            self.aliases = {}
            self._save_aliases()
            logger.warning("Cleared all product aliases")
            return True
        except Exception as e:
            logger.error(f"Error clearing aliases: {e}")
            return False
