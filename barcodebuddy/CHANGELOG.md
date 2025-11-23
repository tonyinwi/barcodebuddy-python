# Changelog

All notable changes to Barcode Buddy (Python) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.15.1-beta] - 2025-11-23

### Fixed
- **Critical Bug:** Product dropdown in pending barcodes UI was empty
- Added missing `get_all_products()` method to GrocyClient
- The `/api/available-products` endpoint now returns products correctly
- Error: `AttributeError: 'GrocyClient' object has no attribute 'get_all_products'` fixed

### Impact
Without this fix, users could not select existing products when resolving pending barcodes, making the duplicate prevention system unusable.

## [2.15.0-beta] - 2025-11-23

### Added
- **⏸️ Pending Barcodes System** - Manual product selection to prevent duplicates!
- When OpenFoodFacts/UPC finds a product, it's added to "pending" instead of auto-creating
- User can choose: Use existing product OR create new product
- New API endpoints for pending barcode management

### Problem Solved
**Before:** Receipt creates "senfk" → Scan barcode → OpenFoodFacts finds "Senfkörner" → Creates DUPLICATE product

**After:** Receipt creates "senfk" → Scan barcode → OpenFoodFacts finds "Senfkörner" → **Pending (User chooses)** → User selects existing "senfk" → No duplicate!

### How It Works
1. Scan barcode 4012345678901
2. Not in Grocy barcode DB
3. Not in aliases (barcode not linked yet)
4. OpenFoodFacts finds "Senfkörner"
5. **NEW:** Added to pending list (not auto-created!)
6. Scanner shows: "⏸️ Found in OpenFoodFacts: Senfkörner - Check UI"
7. User opens UI → Sees pending barcode
8. User chooses:
   - **Option A:** Use existing "senfk" (ID 123) → Barcode added to product
   - **Option B:** Create new "Senfkörner" (ID 124)
9. Product added to stock ✅

### New API Endpoints
- `GET /api/pending` - Get all pending barcodes
- `GET /api/available-products` - Get products from aliases + Grocy
- `POST /api/pending/resolve` - Resolve pending (use_existing or create_new)

### Workflow Example
```json
// GET /api/pending
{
  "pending": [{
    "barcode": "4012345678901",
    "product_name": "Senfkörner",
    "database": "OpenFoodFacts",
    "quantity": 1.0,
    "mode": "add"
  }]
}

// GET /api/available-products
{
  "products": [{
    "id": 123,
    "name": "senfk",
    "source": "alias",
    "alias_name": "senfk"
  }]
}

// POST /api/pending/resolve
{
  "barcode": "4012345678901",
  "action": "use_existing",
  "product_id": 123
}
→ Adds barcode to "senfk", adds to stock, removes from pending
```

### Prevent Duplicates
- Receipts create aliases automatically
- Barcode scanning finds product in external DB
- Instead of auto-creating duplicate → User selects existing
- Barcode gets linked to existing product
- Next scan → Found via Grocy barcode DB ✅

### Technical Details
- Pending barcodes stored in memory (resets on restart)
- Limited to 20 pending items
- Includes quantity and mode (add/consume) from scan
- Gracefully handles products from both aliases and Grocy

### UI Integration ✅
Complete UI implementation with:
- **Pending Barcodes Card** - Auto-shows when items pending, auto-hides when empty
- **Badge counter** showing number of pending items
- **Product selection dropdown** grouped by source (Aliases vs Grocy)
- **"Use Existing Product" button** - Adds barcode to selected product
- **"Create New Product" button** - Creates new product from external data
- **Auto-refresh** every 2 seconds to update pending list
- **Status indicator** (⏸️) in recent scans for pending items

### JavaScript Functions
- `loadPending()` - Fetch and display pending barcodes
- `loadAvailableProducts()` - Fetch products with caching
- `populateSelects()` - Populate dropdowns with grouped products
- `resolveWithExisting(index, barcode)` - Resolve using selected product
- `resolveWithNew(index, barcode)` - Resolve by creating new product

### Files Modified
- `app/main.py` - Pending system + 3 new API endpoints
- `app/templates/index.html` - Complete UI with pending barcodes section
- `app/config.yaml` - Version 2.15.0-beta
- `app/__init__.py` - Version 2.15.0-beta
- `run.sh` - Version 2.15.0-beta

## [2.14.0-beta] - 2025-11-23

### Added
- **🔗 Paperless Grocy Magic Alias Integration** - Shared product mappings between receipt processing and barcode scanning!
- New config options: `paperless_grocy_magic_url` and `enable_alias_integration`
- New `AliasClient` module for API communication
- Automatic product lookup via aliases before checking external databases

### How It Works
**New Barcode Scanning Flow:**
1. Scan barcode → Check Grocy (existing)
2. **NEW:** Check Paperless Grocy Magic aliases
3. If found via alias → Use Grocy product ID directly
4. If not found → Check OpenFoodFacts/UPC Database (existing)

**Integration Benefits:**
- Receipts create auto-aliases → Barcodes can be added to those aliases
- Single source of truth: `/share/paperless-grocy-magic/product_aliases.json`
- Products from receipts and scanned barcodes map to same Grocy product
- No duplicate products for receipt items vs scanned items!

### Example Workflow
```
1. Receipt processed: "Vorderhaxe" → Creates alias + Grocy product ID 123
2. Scan barcode 4012345678901 → Not in Grocy barcode DB
3. Check alias API → Not found yet
4. Create product from OpenFoodFacts → Add to Grocy
5. User adds barcode to "vorderhaxe" alias via Paperless Grocy Magic UI
6. Next scan of 4012345678901 → Found via alias → Use product ID 123!
```

### Configuration
```yaml
# In Barcode Buddy add-on config:
paperless_grocy_magic_url: "http://localhost:5002"  # Or via ingress
enable_alias_integration: true

# In Paperless Grocy Magic config:
alias_storage_location: "shared"  # Use /share for cross-addon access
```

### Technical Details
- Added `alias_client.py` with API methods
- Integrated alias check between Grocy and external database lookups
- Connection test at startup
- Detailed logging with 🔗 emoji for alias operations
- Graceful degradation if Paperless Grocy Magic unavailable

### Files Changed
- `config.yaml` - New configuration options
- `app/config.py` - New properties for alias integration
- `app/alias_client.py` - **NEW** API client module
- `app/main.py` - Integrated alias check in barcode flow

## [2.13.1-beta] - 2025-11-22

### Changed
- Barcode format option now displays as dropdown list in Home Assistant UI
- Improved configuration UX (prevents typos in barcode_format setting)

## [2.13.0-beta] - 2025-11-22

### Added
- **Configurable Barcode Format**: Choose between Code128 or QR codes for PDF
- New config option: \`barcode_format\` (code128/qr)
- QR codes can be scanned with smartphones
- PDF title and footer show selected format


### Security
- **Improved Security Rating**: Removed unnecessary privileged permissions
- Removed `full_access: true` (was granting full host access)
- Removed `apparmor: false` (now uses AppArmor protection)
- Removed `host_ipc: true` and `host_pid: true` (unnecessary host access)
- Removed privileged capabilities `SYS_ADMIN` and `SYS_RAWIO`
- Scanner access still works via device mapping and udev

## [2.12.5-beta] - 2025-11-22

### Changed
- PDF now opens in new browser tab instead of current tab

## [2.12.4-beta] - 2025-11-22

### Removed
- Quantity barcodes for 1 and 2 (not commonly used)

### Changed
- PDF now contains quantity barcodes: 3-10, 20, 30

## [2.12.3-beta] - 2025-11-22

### Changed
- PDF now opens in browser instead of forcing download

## [2.12.2-beta] - 2025-11-22

### Added
- **Mode Barcodes in PDF**: ADD and CONSUME mode barcodes now included in PDF
- PDF organized into sections: Mode Control and Quantity Control

## [2.12.1-beta] - 2025-11-22

### Fixed
- **PDF Generation**: Fixed barcode rendering error by using reportlab's built-in barcode support
- Removed external python-barcode and Pillow dependencies

## [2.12.0-beta] - 2025-11-22

### Added
- **PDF Download**: Generate PDF with quantity barcodes (1-9, 10, 20, 30)
- New button in UI to download printable quantity barcodes
- PDF generator using reportlab and python-barcode libraries
- Code128 barcode format for quantity codes (BBUDDY-Q-X)
- Multi-language support for PDF download button (en/de/fr/es)

## [2.11.1-beta] - 2025-11-22

### Fixed
- Language schema now uses `select()` instead of `list()` for proper dropdown rendering in HA

## [2.11.0-beta] - 2025-11-22

### Changed
- **Simplified Language Selection**: Removed "Auto Detect" option
- Language now configured via dropdown in add-on settings (en/de/fr/es)
- Default language set to German (de)
- Removed language auto-detection code (Accept-Language, Supervisor API)
- Removed `/api/language` and `/api/debug-language` endpoints

### Removed
- Auto-detection of language from browser or Home Assistant Core
- Session-based language switching

## [2.10.6-beta] - 2025-11-22

### Changed
- Try `/supervisor/info` endpoint instead of `/core/info` for language detection
- Added logging for Ingress headers and Accept-Language
- Version bump to force fresh build in Home Assistant

## [2.10.5-beta] - 2025-11-22

### Fixed
- **Critical**: Added `hassio_api: true` to config.yaml to grant Supervisor API access
- Fixes 403 Forbidden error when accessing Home Assistant Core language settings
- Language auto-detection from HA Core now has proper permissions

## [2.10.4-beta] - 2025-11-22

### Fixed
- **Critical**: Fixed initialization order bug where Babel locale_selector was configured before Config was loaded
- Language auto-detection now works correctly with Home Assistant Core language setting
- Debug logging for language detection now appears properly

### Added
- Debug endpoint `/api/debug-language` for troubleshooting language detection

## [2.10.3-beta] - 2025-11-22

### Added
- Auto-detect now uses Home Assistant Core language setting (via Supervisor API)
- Language dropdown in add-on configuration dialog

### Changed
- Language detection priority: Config → Session → HA Core → Browser
- "Auto Detect" respects Home Assistant user language preference
- Improved integration with Home Assistant language settings

## [2.10.2-beta] - 2025-11-22

### Changed
- Language switcher UI: Buttons replaced with dropdown menu
- Dropdown shows full language names (English, Deutsch, Français, Español)
- Added globe emoji (🌍) as visual indicator
- More compact and cleaner design

## [2.10.1-beta] - 2025-11-22

### Added
- `language` configuration option to force a specific language (for debugging)
- Config option accepts: `en`, `de`, `fr`, `es` (empty = auto-detect)

### Changed
- Language detection priority: Config > Session > Browser auto-detect
- Config language setting overrides all other language selections

## [2.10.0-beta] - 2025-11-22

### Added
- **Multi-Language Support**: UI now available in English, German, French, and Spanish
- Flask-Babel integration for internationalization (i18n)
- Language switcher in UI (EN/DE/FR/ES buttons)
- Automatic language detection from browser settings
- Language preference saved in session

### Changed
- All UI text now translatable
- Dynamic language switching without configuration changes

## [2.9.5-beta] - 2025-11-22

### Changed
- Reorganized config.yaml with clear sections (Grocy, Barcode Config, Product Databases, Debug)
- Improved readability of add-on configuration options

## [2.9.4-beta] - 2025-11-22

### Removed
- **EAN-Search.org** database integration (requires paid API key - 401 Unauthorized errors)
- `enable_eansearch` configuration option

### Changed
- Database lookup now only uses OpenFoodFacts and UPC Database (both free)
- "Not found" message updated to reflect available databases

## [2.9.3-beta] - 2025-11-22

### Fixed
- CHANGELOG.md now in correct location for Home Assistant add-on directory
- Fixes "No changelog found for add-on" message in Home Assistant update dialog

## [2.9.2-beta] - 2025-11-22

### Added
- **Configurable Product Databases**: Enable/disable individual online databases via add-on configuration
- Configuration options: `enable_openfoodfacts`, `enable_eansearch`, `enable_upcdatabase`
- All databases enabled by default for maximum barcode coverage

### Changed
- Database queries now respect configuration settings (only enabled databases are queried)
- Improved efficiency by skipping disabled databases

## [2.9.1-beta] - 2025-11-22

### Added
- **EAN-Search.org** database integration (free, no API key needed)
- **UPC Database** integration (free tier, ~100 requests/day)
- Multi-database lookup chain for better barcode coverage

### Changed
- Lookup order: Grocy → OpenFoodFacts → EAN-Search → UPC Database
- UI shows which database product was found in (e.g., "Created from EAN-Search")
- "Not found" message now lists all 4 databases

## [2.9.0-beta] - 2025-11-22

### Changed
- Prepared new_features branch for next development cycle
- Version bump for future features

## [2.8.0] - 2025-11-22

### Added
- **Product Creation UI**: Create products from unknown barcodes directly in the web interface
- **Mode Switching**: Toggle between Add/Consume modes with special barcodes (BBUDDY-ADD / BBUDDY-CONSUME)
- Configurable special barcode texts in add-on configuration
- Input field appears for unknown barcodes to enter product name manually

### Changed
- **Auto-detection** of all scanner devices (hidraw and input/event)
- Active scanner devices now displayed in UI
- Removed unused scanner_device configuration option
- Enhanced UI responsiveness

### Fixed
- Auto-refresh pauses while typing product name
- Product creation refresh issues resolved
- Button state management during product creation

## [2.7.3-beta] - 2025-11-22

### Removed
- Removed unused scanner_device configuration option (fully automatic now)

### Changed
- Startup log message updated to reflect auto-detection

## [2.7.2-beta] - 2025-11-22

### Fixed
- Auto-refresh blocking product name input field
- Allow refresh when input is disabled (creation in progress)

## [2.7.1-beta] - 2025-11-22

### Fixed
- Product creation refresh timing issues

## [2.7.0-beta] - 2025-11-22

### Added
- **Product Creation from UI**: When barcode not found, show input field for product name
- Automatic product creation in Grocy with barcode association
- Automatic stock addition after product creation

## [2.6.2-beta] - 2025-11-22

### Fixed
- UI scanner device display now shows actual active devices instead of config value

## [2.6.1-beta] - 2025-11-22

### Added
- Configurable special barcode texts (barcode_add, barcode_consume, barcode_quantity_prefix)

## [2.6.0] - 2025-11-22

### Added
- MIT License
- Initial stable release with all beta features

## [2.5.0] - 2025-11-22

### Added
- **Mode Switching**: BBUDDY-ADD and BBUDDY-CONSUME barcodes
- Persistent mode state (add/consume)
- Mode indicator (🔄) in UI

## [2.4.3] - 2025-11-22

### Fixed
- Quantity calculation off-by-one error (was adding 11 instead of 10)
- Quantity now starts at 0, defaults to 1 if no quantity barcode scanned

## [2.4.2] - 2025-11-22

### Fixed
- Nested product structure handling from Grocy API

## [2.4.1] - 2025-11-22

### Fixed
- Grocy API compatibility issues

## [2.4.0] - 2025-11-20

### Added
- **Quantity Barcodes**: Scan `BBUDDY-Q-X` to set quantity for next product
- Multiple quantity barcodes are automatically summed
- UI shows quantity in parentheses: "Added: Product (3x)"
- Special 🔢 icon for quantity barcode scans

### Changed
- Quantity resets to 1 after successful product addition

### Example
```
1. Scan BBUDDY-Q-3 → "🔢 Quantity set to: 3"
2. Scan BBUDDY-Q-2 → "🔢 Quantity set to: 5"
3. Scan product → "✅ Added: Chester (5x)"
```

## [2.3.0] - 2025-11-20

### Added
- **OpenFoodFacts Integration**: Automatic product lookup and creation
- Unknown barcodes are automatically looked up in OpenFoodFacts database
- Products are created in Grocy with information from OpenFoodFacts
- Dynamic location and quantity unit ID detection from Grocy

### Changed
- Enhanced workflow: Grocy search → OpenFoodFacts lookup → Create product → Add to stock
- Better error messages showing exact Grocy API responses

### Fixed
- Product creation compatibility with different Grocy versions
- NOT NULL constraint errors by querying available locations
- Handling of 400/404 responses from Grocy API

## [2.2.0] - 2025-11-20

### Added
- **Multi-Scanner Support**: Automatic detection and simultaneous use of multiple USB scanners
- Hot-plug detection: New scanners are detected automatically every 5 seconds
- Each scanner works independently with its own buffer
- Support for up to 20 hidraw devices

### Changed
- Scanner handler now uses threading for concurrent device monitoring
- Improved device detection and error handling

## [2.1.0] - 2025-11-20

### Added
- **USB Scanner Support via hidraw**: Switched from evdev to hidraw devices
- HID report parsing for keyboard emulation mode
- Support for `/dev/hidraw0-4` devices

### Fixed
- Scanner device permission issues with `/dev/input/event*`
- Kernel kbd handler conflicts resolved by using hidraw

## [2.0.0] - 2025-11-20

### Added
- **Complete Python Rewrite**: Rebuilt from scratch in Python/Flask
- Modern web UI with real-time updates
- Grocy API integration
- Home Assistant Add-on architecture
- Multi-architecture Docker support (armhf, armv7, aarch64, amd64, i386)
- Scanner device configuration
- Debug mode with detailed logging

### Removed
- Legacy PHP/bash implementation

---

## Version History Summary

| Version | Date | Key Feature |
|---------|------|-------------|
| 2.9.1-beta | 2025-11-22 | EAN-Search & UPC Database |
| 2.8.0 | 2025-11-22 | Product Creation UI & Mode Switching |
| 2.7.0-beta | 2025-11-22 | Manual Product Creation |
| 2.6.0 | 2025-11-22 | Stable Release |
| 2.5.0 | 2025-11-22 | Mode Switching (Add/Consume) |
| 2.4.0 | 2025-11-20 | Quantity Barcodes (BBUDDY-Q-X) |
| 2.3.0 | 2025-11-20 | OpenFoodFacts Integration |
| 2.2.0 | 2025-11-20 | Multi-Scanner Support |
| 2.1.0 | 2025-11-20 | USB Scanner via hidraw |
| 2.0.0 | 2025-11-20 | Python Rewrite |

---

## Migration Notes

### From v1.x (PHP/Bash) to v2.0.0+

The Python version is a complete rewrite with:
- New configuration format (config.yaml)
- Different device paths (/dev/hidraw vs /dev/input)
- Modern web interface
- Better error handling
- OpenFoodFacts integration built-in

### Configuration Changes
- Scanner device now uses hidraw: `/dev/hidraw0` instead of `/dev/input/event3`
- Grocy URL should not include `/api` suffix
- API keys are validated on startup with automatic retry

---

## Contributing

Built with ❤️ using [Claude Code](https://claude.com/claude-code)
