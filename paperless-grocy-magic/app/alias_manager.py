"""Product Alias Manager for mapping receipt names to Grocy products."""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ProductAlias:
    """Represents a mapping from receipt product name to Grocy product."""

    def __init__(self, receipt_name: str, grocy_product_id: int, grocy_product_name: str, notes: str = ""):
        self.receipt_name = receipt_name.strip().lower()  # Normalize for matching
        self.grocy_product_id = grocy_product_id
        self.grocy_product_name = grocy_product_name
        self.notes = notes

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'receipt_name': self.receipt_name,
            'grocy_product_id': self.grocy_product_id,
            'grocy_product_name': self.grocy_product_name,
            'notes': self.notes
        }

    @staticmethod
    def from_dict(data: dict) -> 'ProductAlias':
        """Create ProductAlias from dictionary."""
        return ProductAlias(
            receipt_name=data['receipt_name'],
            grocy_product_id=data['grocy_product_id'],
            grocy_product_name=data['grocy_product_name'],
            notes=data.get('notes', '')
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

    def add_alias(self, receipt_name: str, grocy_product_id: int, grocy_product_name: str, notes: str = "") -> bool:
        """Add or update an alias."""
        try:
            alias = ProductAlias(receipt_name, grocy_product_id, grocy_product_name, notes)
            self.aliases[alias.receipt_name] = alias
            self._save_aliases()
            logger.info(f"Added alias: '{receipt_name}' → '{grocy_product_name}' (ID {grocy_product_id})")
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

    def list_all_aliases(self) -> List[dict]:
        """Get all aliases as list of dictionaries."""
        return [alias.to_dict() for alias in self.aliases.values()]

    def get_count(self) -> int:
        """Get number of aliases."""
        return len(self.aliases)

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
