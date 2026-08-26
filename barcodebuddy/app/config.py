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
    def scanner_names(self) -> dict:
        """
        Friendly names for the guns, keyed by USB id.

        `0581:011a = Pantry gun` per line, or comma separated.

        KEYED ON THE USB ID, NOT THE DEVICE NODE, because that is the identity
        everything else here already uses: one physical gun presents SEVERAL
        /dev/hidrawN nodes -- 0581:011a is hidraw0 and hidraw1, 0461:4d86 is
        hidraw2 and hidraw3 -- and the numbering shifts when things are
        replugged. Naming a node would mean naming the same gun twice and
        having it come undone at the next reboot.

        ⚠️ TWO IDENTICAL GUNS WOULD SHARE ONE NAME. `device_usb_id()` reads
        vendor:product out of sysfs and nothing more, so two of the same model
        are indistinguishable -- and that is not a limitation of naming, it is
        already true of `resolve_scan_mode()` and of the per-gun location. The
        two here are different models, so it does not arise. If a second
        identical gun ever appears, HID_UNIQ (a serial, which the YuRiot has
        and the other does not) or HID_PHYS (the USB port path, which changes
        when replugged) would be where to look.
        """
        raw = str(self._config.get('scanner_names', '') or '')
        names = {}
        for line in raw.replace(',', '\n').splitlines():
            if '=' not in line:
                continue
            usb, _, label = line.partition('=')
            usb, label = usb.strip().lower(), label.strip()
            if usb and label:
                names[usb] = label
        return names

    def gun_label(self, key: str) -> str:
        """
        Turn a tracker key into something a person recognises.

        The keys are `usb:0581:011a` or `dev:/dev/hidraw0` -- correct, stable,
        and meaningless to somebody holding one of two guns wondering which
        card on screen is theirs.
        """
        key = str(key or '')
        if key.startswith('usb:'):
            return self.scanner_names.get(key[4:], key)
        return self.scanner_names.get(key, key)

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
