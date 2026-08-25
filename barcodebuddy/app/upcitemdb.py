"""
UPCitemdb, called directly.

This used to reach UPCitemdb through Grocy's `external-lookup`, i.e. through a
PHP plugin -- which meant provider order could never be configurable, because
half the chain lived in another runtime. Same data, one process.

Two endpoints, as the plugin had:
  * no key  -> the free trial endpoint, ~100/day, no signup
  * a key   -> the paid production endpoint, with user_key/key_type headers

The trial endpoint answers HTTP 429 with `code: TOO_FAST` when bursting, which
is a real signal and not an error: the caller should stop rather than spend the
rest of a daily budget on calls that will also be refused.
"""

import logging
from typing import Any, Dict, Optional

import requests

from gtin import same_gtin

logger = logging.getLogger(__name__)

TRIAL_URL = "https://api.upcitemdb.com/prod/trial/lookup"
PROD_URL = "https://api.upcitemdb.com/prod/v1/lookup"


class UPCItemDbClient:
    def __init__(self, api_key: str = ""):
        self.api_key = (api_key or "").strip()
        self.last_outcome = "miss"

    def lookup_barcode(self, barcode: str) -> Optional[Dict[str, Any]]:
        self.last_outcome = "miss"
        try:
            if self.api_key:
                url, headers = PROD_URL, {"user_key": self.api_key,
                                          "key_type": "3scale"}
            else:
                url, headers = TRIAL_URL, {}

            logger.info(f"Looking up barcode in UPCitemdb: {barcode}")
            resp = requests.get(url, params={"upc": barcode},
                                headers=headers, timeout=15)

            if resp.status_code == 429:
                self.last_outcome = "throttled"
                logger.warning(f"⏳ UPCitemdb rate limited on {barcode}")
                return None
            if resp.status_code in (401, 403):
                self.last_outcome = "auth_error"
                logger.error(f"🔑 UPCitemdb rejected the key (HTTP {resp.status_code})")
                return None
            resp.raise_for_status()
            data = resp.json()

            items = data.get("items") or []
            if not items:
                logger.info(f"❌ Not found in UPCitemdb: {barcode}")
                return None
            item = items[0]

            # The provider must be answering about the code we asked for. This
            # is the guard that makes a fuzzy or padding provider safe.
            echoed = item.get("ean") or item.get("upc") or ""
            if echoed and not same_gtin(echoed, barcode):
                self.last_outcome = "echo_reject"
                logger.warning(f"⚠️  UPCitemdb answered for {echoed} when asked about "
                               f"{barcode} - different product, discarding")
                return None

            # A hit must carry a usable name. "success with an empty title" is
            # not a hit -- a blank product name is worse than no answer, because
            # "Unknown <barcode>" looks unfinished and gets fixed while a
            # nameless product is invisible in review.
            name = str(item.get("title") or "").strip()
            if not name:
                self.last_outcome = "no_name"
                logger.info(f"❌ UPCitemdb knows {barcode} but has no title for it")
                return None

            images = item.get("images") or []
            # Prefer an https image with a real extension: UPCitemdb's first
            # entry is often a third-party reseller host that is dead or
            # hotlink-blocked, which is why product pictures silently never
            # arrived. See BACKLOG.md.
            image = ""
            for candidate in images:
                c = str(candidate)
                if c.startswith("https://") and c.lower().rsplit(".", 1)[-1] in (
                        "jpg", "jpeg", "png", "webp"):
                    image = c
                    break
            if not image and images:
                image = str(images[0])

            self.last_outcome = "hit"
            logger.info(f"✅ Found in UPCitemdb: {name}")
            return {
                "name": name,
                "barcode": barcode,
                "brand": str(item.get("brand") or "").strip(),
                "quantity": str(item.get("size") or "").strip(),
                "image_url": image,
                "categories": str(item.get("category") or "").strip(),
                "description": str(item.get("description") or "").strip(),
            }

        except requests.exceptions.RequestException as err:
            self.last_outcome = "error"
            logger.error(f"UPCitemdb API error: {err}")
            return None
