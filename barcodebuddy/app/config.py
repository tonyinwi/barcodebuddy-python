"""Configuration management for Home Assistant Add-on."""
import json
import os
from typing import Optional


class Config:
    """Load and manage add-on configuration."""

    def __init__(self):
        self.config_path = "/data/options.json"
        self._config = self._load_config()

    def _load_config(self) -> dict:
        """Load configuration from Home Assistant."""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                return json.load(f)
        return {}

    @property
    def grocy_url(self) -> Optional[str]:
        """Get Grocy URL."""
        url = self._config.get('grocy_url', '').strip()
        return url if url else None

    @property
    def grocy_api_key(self) -> Optional[str]:
        """Get Grocy API key."""
        key = self._config.get('grocy_api_key', '').strip()
        return key if key else None

    @property
    def debug(self) -> bool:
        """Get debug mode."""
        return self._config.get('debug', False)

    @property
    def has_grocy(self) -> bool:
        """Check if Grocy is configured."""
        return self.grocy_url is not None and self.grocy_api_key is not None

    @property
    def barcode_add(self) -> str:
        """Get ADD mode barcode."""
        return self._config.get('barcode_add', 'BBUDDY-ADD')

    @property
    def barcode_consume(self) -> str:
        """Get CONSUME mode barcode."""
        return self._config.get('barcode_consume', 'BBUDDY-CONSUME')

    @property
    def barcode_quantity_prefix(self) -> str:
        """Get quantity barcode prefix."""
        return self._config.get('barcode_quantity_prefix', 'BBUDDY-Q-')

    @property
    def enable_openfoodfacts(self) -> bool:
        """Check if OpenFoodFacts database is enabled."""
        return self._config.get('enable_openfoodfacts', True)

    @property
    def ha_webhook_url(self) -> str:
        """
        Home Assistant webhook to POST each scan result to. Empty = disabled.

        Barcode Buddy is a standalone Flask app with no HA integration, so
        nothing downstream can react to a scan. This is the one wire out:
        HA decides what a scan means (announce it, notify, log it) rather
        than that policy living in here.
        """
        return str(self._config.get('ha_webhook_url', '') or '').strip()

    @property
    def upcdatabase_api_key(self) -> str:
        """
        upcdatabase.org API key. Empty means the lookup is skipped entirely
        rather than firing a request that comes back 200-but-failed.
        Free tier is 100/day; get one at https://upcdatabase.org/api
        """
        return str(self._config.get('upcdatabase_api_key', '') or '').strip()

    @property
    def scanner_add_device(self) -> str:
        """
        USB "vendor:product" of the gun that always means ADD, e.g. "0581:011a".

        Binding the mode to a physical scanner removes the stateful ADD/CONSUME
        toggle, which is the failure that corrupts data silently: scan ten items
        in the wrong mode and you get ten wrong movements with no error. The gun
        you pick up IS the mode.

        Empty means no binding -- that device falls back to the global mode.
        """
        return str(self._config.get('scanner_add_device', '') or '').strip().lower()

    @property
    def scanner_consume_device(self) -> str:
        """USB "vendor:product" of the gun that always means CONSUME."""
        return str(self._config.get('scanner_consume_device', '') or '').strip().lower()

    @property
    def usda_api_key(self) -> str:
        """
        USDA FoodData Central key. STORED, NOT YET USED.

        There is deliberately no USDA provider in the chain: measured against
        nine real barcodes it hit 2, both already resolved by a provider twice
        as fast, and added zero coverage. The key lives here so it is in one
        place when that decision is revisited, and so the outstanding fuzzy-risk
        probe can be finished -- `foods/search?query=<upc>` is a text search and
        can return *a* food for anything.
        """
        return str(self._config.get('usda_api_key', '') or '').strip()

    @property
    def lookup_log_path(self) -> str:
        """
        Where the per-attempt lookup log is written.

        /share, not /data: an add-on's /data is destroyed on uninstall, and this
        log is the evidence behind "is a paid provider worth it". /share also
        rides along in Home Assistant's backups.
        """
        return str(self._config.get('lookup_log_path', '')
                   or '/share/kitchen-stack/lookup_log.jsonl')

    @property
    def enable_upcdatabase(self) -> bool:
        """Check if UPC Database is enabled."""
        return self._config.get('enable_upcdatabase', True)

    @property
    def language(self) -> str:
        """Get configured language."""
        lang = self._config.get('language', 'en').strip()
        # Validate language code
        if lang in ['en', 'de', 'fr', 'es']:
            return lang
        return 'en'  # Default fallback

    @property
    def barcode_format(self) -> str:
        """Get barcode format for PDF generation."""
        fmt = self._config.get('barcode_format', 'code128').strip().lower()
        # Validate format
        if fmt in ['code128', 'qr']:
            return fmt
        return 'code128'  # Default fallback

    @property
    def paperless_grocy_magic_url(self) -> Optional[str]:
        """Get Paperless Grocy Magic URL."""
        url = self._config.get('paperless_grocy_magic_url', '').strip()
        return url if url else None

    @property
    def enable_alias_integration(self) -> bool:
        """Check if alias integration is enabled."""
        return self._config.get('enable_alias_integration', True)

    @property
    def has_alias_integration(self) -> bool:
        """Check if alias integration is fully configured."""
        return (self.paperless_grocy_magic_url is not None and
                self.enable_alias_integration)
