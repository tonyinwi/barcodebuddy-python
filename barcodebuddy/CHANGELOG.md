# Changelog

## Location-flip barcodes: tell a gun which shelf it is at

Scan `BBUDDY-LOC-BIG-PANTRY` at a shelf and everything that gun scans next is
created there. Scan another when you move. Walk away for ten minutes and it
forgets.

**Per gun, not global.** The rule is "10 minutes of no scans on that same gun",
which only means anything if each gun carries its own location -- and it
matches how mode already works, since `resolve_scan_mode()` binds ADD/CONSUME
per device. The stock gun parked in the basement cannot change what the garbage
gun records in the kitchen.

**It expires because a persistent location is dangerous.** A persistent *mode*
is safe: two guns are each bound to one permanently. A location is not. Walk
away, come back tomorrow, and every scan lands in the Spice Cabinet -- silently,
and visible weeks later as a catalogue full of wrong shelves. On expiry it
reverts to `product_presets_location_id` rather than clearing, because Grocy's
`products.location_id` is NOT NULL and something must always be supplied.

**Descriptive payloads, not ids.** `BBUDDY-LOC-OILS-VINEGAR`, not
`BBUDDY-LOC-12`. QR removes any length penalty, a label taped to a shelf should
be readable by whoever is standing in front of it, and ids change across a
database rebuild while names do not. Slugging is deliberately lossy and stable;
two locations that slug identically are an **error**, not a guess, because
silently picking one is how the wrong shelf reaches hundreds of products.

**An unrecognised location code clears the gun's location** rather than leaving
the previous shelf in effect. A rejected scan that quietly kept the old value
would send the next fifty products to the wrong place.

**The PDF sheet is data-driven**, and that is the real change there. The control
sheet hardcodes ADD/CONSUME/quantity, which is fine because those never change.
Locations do: add a shelf in Grocy, regenerate, and it is on the page.
`GET /api/download-location-sheet` builds it from `/objects/locations`.
`GET /api/locations` reports where each gun is and how long it has left.


## The scanner is an input device again

The provider chain, priorities, keys and attempt log all move to the Kitchen
Stack add-on. `lookup_chain()` here is now a single call to Grocy's
external-lookup: Grocy is the resolution authority, its plugin proxies to the
engine, and this add-on scans, asks, creates and stocks.

For one day the chain lived here instead. That inverted the architecture -- the
input device was the authority and the inventory system was its client -- and
Tony called it: the scanner was never meant to own the lookup.

Deleted: upcitemdb.py, upcdatabase.py, openfoodfacts.py, lookup_log.py,
gtin.py, the priority options, and every provider key. `/api/lookup-stats` and
`/api/providers` survive as thin delegations to the engine so existing callers
keep working during the cutover. `is_gtin` stays on the scan path -- garbage
never deserves an HTTP round trip.

Net: this fork is ~600 lines closer to upstream than it was yesterday.


## One number per provider, replacing the list and the enable_ flags

`lookup_order` plus `enable_*` booleans were two ways to say the same thing, and
they could disagree: a provider could be switched on but absent from the order,
or listed in the order but switched off. Both looked exactly like "a provider
that never matches anything", which is the ambiguity that let a dead provider go
unnoticed for weeks in the first place.

Now one field per provider. **1 runs first, 0 is off.**

```
upcdatabase_priority: 1
upcitemdb_priority: 2
openfoodfacts_priority: 0
grocy_lookup_priority: 0
```

A list is unambiguous, which is why it was the first attempt. But the add-on
options screen is a generated form with no drag-to-reorder, so changing a list
means deleting and re-adding entries, while a number is one field to edit.

The trade is that a list made ties impossible and numbers do not, so the
tiebreak is stated rather than left to dictionary iteration order, and the
provider check warns when two providers share a priority instead of resolving it
silently.

The four `enable_*` properties are deleted rather than left in place, because
dead configuration that still reads as live is how this codebase got a provider
enabled with no key.


## An invalid UPC is not a provider failure

UPCitemdb answers a bad check digit with HTTP 400 `INVALID_UPC`. `raise_for_status()`
turned that into an exception and the attempt was recorded as `error` -- so our
own bad input inflated the error rate that the provider-health check watches,
while upcdatabase called the same code a plain miss. Two providers looking
different for a reason that has nothing to do with either.

It is now recorded as `invalid_upc`, and joins the skips excluded from the
`asked` denominator: the provider declined to try, so counting it as a fair
chance to answer understates its hit rate.

**A check-digit gate was written and then deliberately abandoned.** Validating
before calling any provider looked obviously right -- bad codes never reach a
provider, comparison stays symmetric. Tested against every real barcode in the
catalogue first: 19 of 20 pass, and the one that fails, `04308504`, **resolves
correctly** -- USDA returns "SWEET TEA LIQUID WATER ENHANCER" for it and echoes
the code back exactly. Real barcodes with bad check digits exist in the wild and
in provider databases, so the gate would have broken intake for a product that
works today. `check_digit_ok()` survives as a diagnostic for flagging a probable
mis-scan, and is wired to nothing.


## Provider order is configuration, and UPCitemdb is no longer first

`lookup_order` in the add-on options is now the chain. Providers are tried top
to bottom and the first usable answer wins. The chain became a dict of provider
descriptors rather than a stack of if-statements, so adding one is an entry and
reordering one is a UI edit.

**upcdatabase leads now, on measured evidence rather than taste.** UPCitemdb's
trial endpoint throttled **6 of 10** attempts -- from calls being close
together, not a daily cap -- and every throttle falls through to upcdatabase
anyway. Leading with it therefore buys a wasted call before the slow one.
upcdatabase also had the better hit rate over the same window, 50% against 37%.

UPCitemdb is roughly **7x faster** (171ms against 1232ms) and deserves to lead
the moment a paid key removes the throttling. That is one line of configuration
now, which is the whole point.

A provider not listed in `lookup_order` is **never called**, whatever its
`enable_*` switch says. The flags survive as a veto -- an off switch that does
not require editing the order -- and the provider check now reports both halves
of that redundancy explicitly:

  * listed in the order but switched off
  * switched on but missing from the order

Both look identical to "a provider that never matches anything" from the
outside, and that exact ambiguity is what let upcdatabase sit dead for weeks.
The startup log prints the chain in order, numbered, so what you read is the
order that runs.


## UPCitemdb is called directly; the chain finally lives in one runtime

UPCitemdb used to be reached through Grocy's `external-lookup`, i.e. through a
PHP plugin on another host. That is why provider order could never be
configurable: half the chain was somewhere else, in another language, with its
own key. `upcitemdb.py` calls it directly -- trial endpoint with no key, paid
production endpoint with one.

The old path survives as `enable_grocy_lookup`, **off by default**, purely as a
rollback switch.

**The plugin stays installed, and must.** Grocy's "presets for new products"
are read by the barcode-lookup plugin and by *nothing else* on the API path --
that is the only reason scans land in Big Pantry instead of whichever location
sorts first. Anything else calling `external-lookup` (Basil, Grocy's own UI)
still needs it.

So this add-on now resolves those presets itself, the same way the plugin did:
`product_presets_location_id` and `product_presets_qu_id`, each falling back to
the first location / first quantity unit when unset (`-1`). Measured here: the
location preset is 7, the quantity preset is unset, and the first-unit fallback
gives 2 -- exactly what the plugin was returning. The presets are applied with
`setdefault`, so a provider that supplies its own values keeps them, and they
are cached because user settings change roughly never and this is on the scan
path.

`gtin.py` holds `is_gtin` and `same_gtin` in one place so two providers cannot
drift apart on rules that were both expensive to establish.

The new client carries the lessons the others taught: a hit must have a
non-empty title, the returned `ean`/`upc` must be the same GTIN that was asked
for, HTTP 429 is `throttled` rather than an error, and images prefer the first
**https** URL with a real extension -- UPCitemdb's first entry is often a
third-party reseller host that is dead or hotlink-blocked, which is a known
reason product pictures silently never arrived.


## Validate the providers at startup, and say so in the log

Every start now prints what is actually wired up and whether it is actually
working, and `GET /api/providers` answers the same question on demand.

**It spends no lookup quota.** A synthetic ping would have been worse than
useless: upcdatabase.org sat dead for weeks answering HTTP 200 with
`success:false`, so a reachability probe would have reported "fine". The check
uses two free sources instead -- static configuration, and what the attempt log
says each provider actually did over the last seven days.

The static half catches the failure seen here for real: a provider **enabled
with an empty key**, which is skipped silently on every single scan and looks
exactly like a provider that simply never matches anything.

The evidence half catches the slower rot. `0 hits in 200 attempts over 7d` is
the line that shouts; so are a rejected key, repeated throttling, and any echo
mismatch, which means a provider handed back a different product than the one
asked about.

Sample output:

```
🔎 Provider check (no lookups spent):
   ENABLED  upcitemdb-via-grocy    last 7d: 6 asked, 2 hit (33.3%), median 171ms
   disabled openfoodfacts          no recent attempts
   ENABLED  upcdatabase            last 7d: 5 asked, 2 hit (40.0%), median 1239ms
   disabled usda                   no recent attempts   [key stored, provider intentionally NOT in the chain]
```


## A read-only lookup route, and one chain behind both callers

`GET /api/lookup/<barcode>` resolves a barcode without touching stock or
creating anything. `/api/scan` remains the only write path.

The provider chain is extracted into `lookup_chain()` and both callers now go
through it. Two copies of a chain is the fragmentation this work exists to
remove, and it is also how a retry silently starts behaving differently from a
live scan -- the retry tool currently keeps its own, which is why the same
barcode can resolve one way when scanned and another way when retried.

Every provider in the chain was already read-only: the Grocy path uses
`external-lookup?add=false` precisely so resolution and creation stay separate.

Three things this unlocks:

  * retries and live scans share one implementation and one provider budget
  * a lookup fix can be verified by *behaviour* without writing to the
    household's real inventory. That was impossible before -- confirming an
    empty-title bug meant correlating git push times against Docker build lines,
    because every path into this add-on wrote to Grocy
  * a manual probe contributes to the same provider evidence as a real scan,
    since the route logs attempts like any other caller

The response reports which provider answered and what each one did on the way,
so a miss can be attributed rather than guessed at.


## Log every lookup attempt, so provider order can be decided on evidence

One record per provider *attempt*, not per barcode -- the whole point is
comparing providers on the same input. `/api/lookup-stats?days=30` returns
per-provider attempts, hit rate, median and p95 latency, throttle count and
echo failures.

This ships **before** the first bulk pantry inventory rather than after it.
That inventory is the best provider-comparison dataset this household will ever
produce: hundreds of real barcodes hitting every provider in one burst. Without
the log in place first that data is simply gone, and "which provider earns first
position" stays a guess for another month of ordinary scanning.

Outcomes are finer than hit/miss on purpose, because both of this fork's real
lookup bugs were invisible under a coarse split: a provider answering
`success: true` with an empty title, and a provider echoing back a zero-padded
code. `no_name` and `echo_reject` are now distinct from `miss`, and
`UPCDatabaseClient` reports `last_outcome` so the caller can log *why*.

A non-GTIN barcode records ONE `skipped_non_gtin` row for the chain rather than
one per provider -- charging each provider a miss for a lookup nobody performed
would distort every hit rate the decision depends on. Skips are excluded from
the `asked` denominator for the same reason.

Written to `/share`, not the add-on's `/data`: `/data` is destroyed when an
add-on is uninstalled, and this log is the evidence behind a purchasing
decision. `/share` survives that and rides along in Home Assistant backups.

Logging can never break a scan. Every write swallows its own errors, and the
timing wrapper records an exception as `error` and then re-raises it.

The stats endpoint deliberately does **not** reorder anything. Automatic tuning
would shuffle providers on noisy data behind your back, and an opaque chain is
exactly what makes a wrong lookup hard to diagnose months later.


## Renamed to "Kitchen Scanner"; version now bumps per deploy; build.yaml removed

The add-on card and sidebar now read **Kitchen Scanner** / **Scanner**. The
`slug` is deliberately unchanged -- Home Assistant identifies an installed
add-on by slug, so renaming that would orphan the install and its config.

`version:` must now be bumped on **every** deploy. This add-on builds from a
git branch, so without a bump the version string is byte-identical before and
after a push, and a stale clone looks exactly like a fresh one. Proving a
deploy had landed previously meant correlating the git push time against
`store.git` and Docker build lines in the supervisor log. Now `ha addons info`
answers it, and HA shows "update available" when the branch moves.

`build.yaml` is **deleted**. It declared `build_from: python:3.11-alpine` for
every arch and was dead configuration: the Dockerfile hardcodes
`FROM python:3.11-alpine` and never reads `ARG BUILD_FROM`, so the Supervisor's
`--build-arg BUILD_FROM=...` was consumed by nothing. The value also failed the
Supervisor's image-reference regex -- a bare `name:tag` has no registry/org
path -- so every rebuild logged two warnings about a file that changed nothing.
The base image lives in the Dockerfile.


## upcdatabase.org: "success" is not a hit, and the echo check must normalise GTINs

Two bugs found live on 2026-08-25, the moment a real API key made this provider
reachable for the first time.

**An empty title became a product name.** `data.get('title', 'Unknown Product')`
looks defensive but is not: the default only fires when the key is *absent*.
upcdatabase.org answers `success: true` with `title: ""` for barcodes it merely
knows about -- `049000006346` (Coca-Cola) is one -- so the name passed to Grocy
was the empty string. A blank product name is worse than no answer at all:
`Unknown 049000006346` looks unfinished and gets fixed in review, while a
nameless product is invisible. A hit must now carry a non-empty name (title,
falling back to description) or it is treated as a miss.

**The returned code is zero-padded, so an exact echo check would be wrong.**
Ask about `049000006346` and this API answers `0049000006346`; ask about
`016291441187` and it answers `0016291441187`. A character-for-character
comparison -- which is what the kitchen-stack roadmap specified -- would reject
both, including the perfectly good *Coriander Seed / Morton & Bassett* result,
and silently disable the provider. `_same_gtin()` compares zero-padded to 14
digits instead, which still catches a provider substituting a different
product. The padding trap that produced "Bonbebe Fruithapje" is stopped earlier
by `is_gtin()`: `55540` is five digits and never reaches a provider.


All notable changes to Barcode Buddy (Python) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

# Fork: fire-and-forget

Changes below this heading are **not upstream**. They belong to the
[tonyinwi/barcodebuddy-python](https://github.com/tonyinwi/barcodebuddy-python)
fork, branched from `sitaro/barcodebuddy-python@e76a458` (2.18.2-beta). Upstream's
own history resumes at the next divider.

## [fork] - 2026-08-23

### Added
- **Brand and lookup source recorded on the barcode.** Each scanned barcode now
  stores the brand it resolved to, plus which database answered, as `brand` /
  `source` userfields on the `product_barcodes` row. Brand is deliberately a
  *barcode* attribute, not a product one: the generic product stays the source of
  truth so any variant satisfies a recipe, while each barcode remembers what was
  actually on the shelf. New `set_userfields()` helper; `add_barcode_to_product()`
  gained `brand` / `source` arguments. Userfield failures are logged, never fatal
  to a scan.
- **Grocy's own lookup plugin leads the lookup chain** (`ad770cc`). Order is now
  Grocy → OpenFoodFacts → UPC Database, called with `add=false` so Grocy resolves
  without creating; this add-on still does its own create, which is what handles
  name collisions and reorder points. Grocy is US-focused and thin on imported
  goods, so the other two remain fallbacks rather than being replaced.
- **Auto-create on unknown barcode** (`8d5c6e3`) — the fork's reason to exist.
  Unknown barcodes are created immediately instead of parking in a pending queue.
  The name is looked up first, since Grocy enforces `UNIQUE` on `products.name`,
  so a title already seen under a different barcode attaches to the existing
  product. Free dedup at intake.

### Fixed
- **Products no longer land in the wrong location** (`6d22700`). Location and
  quantity unit came from whichever row sorted first, which on a typical setup is
  a fridge — so shelf-stable goods were created in cold storage. Both now read
  Grocy's "presets for new products", falling back to the old behaviour only when
  a preset is unset.
- **OpenFoodFacts lookups always failed** (`ad770cc`). OFF rejects the requests
  library's default User-Agent with HTTP 403, and because the caller only sees
  `None`, every failure was reported as "barcode not found in any database". Now
  sends an identifying User-Agent.
- **Create-then-consume always failed** (`8d5c6e3`) in `/api/create-product` and
  `/api/pending/resolve`. Both created a product then immediately consumed from
  it, which Grocy rejects: a new product has 0 stock and `ConsumeProduct()`
  refuses any amount above current stock. A consume of an unknown now records a
  reorder point instead of booking a consume.
- **Product ids were inconsistently typed** (`4d5da2b`). `create_product()`
  returned a string while `find_product_by_name()` returned an int, and the scan
  path compares the two to decide whether it is reusing a product.

### Changed
- Add-on and repository renamed to identify the fork, so it is distinguishable
  from upstream in the add-on store (`db71827`). Slug deliberately unchanged —
  Home Assistant keys installs by slug, so renaming it orphans an existing
  install and its configuration.
- READMEs rewritten to document fork behaviour, the required Grocy setting, and
  the deploy sequence (`82dcf82`).

### Notes
- **Requires** `shopping_list_auto_add_below_min_stock_amount` enabled in Grocy,
  or the reorder signal stops at "missing products" and never reaches the
  shopping list.
- **Deploying needs `ha store reload` before `ha apps rebuild`** — rebuild alone
  does not pull new commits. See `README.md`.
- The `brand` / `source` / `preferred` userfields must exist on
  `product_barcodes`; `tools/grocy_normalize.py --setup` in the kitchen-stack
  repo creates them.
- Brand via the Grocy lookup path additionally needs the UPCitemdb plugin to emit
  `__brand` — a kitchen-stack change deployed separately to Grocy itself.

---

## [2.18.2-beta] - 2025-11-23

### Fixed
- **"Namen ändern" button error** - Fixed "Error: Failed to update product name in Grocy"
- Grocy API `update_product_name()` now sends complete product object (required by Grocy PUT API)
- Previously only sent the `name` field, which Grocy rejected

## [2.18.1-beta] - 2025-11-23

### Fixed
- **Empty product dropdown** when scanning barcodes after receipt processing
- API filter logic now correctly includes Grocy products when `without_barcode=true`
- Previously all Grocy products were incorrectly skipped when filtering for products without barcodes

## [2.18.0-beta] - 2025-11-23

### Added
- **🔗 Cross-Reference Table (Kreuztabelle)** - Complete product lifecycle tracking
  - Maps: Receipt name ↔ OpenFoodFacts name ↔ Barcode ↔ Grocy product
  - Tracks `openfood_name` from external databases
  - Tracks `has_grocy_barcode` flag for barcode registration status
  - Enables intelligent matching in future receipts

- **🎯 Fuzzy Matching for Post-Receipt Scanning** - Smart product suggestions
  - When scanning barcodes after receipt processing, system suggests best matches
  - Uses difflib SequenceMatcher with 60% threshold for auto-selection
  - Shows match percentage for transparency (>70%)
  - Only shows products WITHOUT barcodes (prevents duplicate assignments)

- **✏️ "Namen ändern" Button** - Update product name WITHOUT stock addition
  - Orange button to distinguish from stock operations
  - Updates product name with OpenFoodFacts/UPC name
  - Adds barcode to Grocy product
  - Updates cross-reference table with `openfood_name`
  - **Does NOT add to stock** (key difference from other buttons)
  - Perfect for correcting receipt product names

- **🔍 Product Filter** - Show only products needing barcodes
  - Dropdown lists filtered to products without `has_grocy_barcode=true`
  - Grouped display: "📝 From Receipts (ohne Barcode)" vs "📦 From Grocy"
  - Reduces clutter, focuses on products needing attention

### Changed
- **Enhanced Receipt Processing** - Uses cross-reference table
  - When processing receipts, checks cross-table for known items
  - If product has `openfood_name`, uses that for display/logging
  - Example: Receipt shows "senfk" → Logs show "Senfkörner" (from previous correction)
  - Provides better product identification across multiple receipts

- **Scanner Auto-Mode** - Documented existing intelligent behavior
  - Known products (in Grocy or cross-table) → Direct stock addition
  - Unknown products (from OpenFoodFacts/UPC) → Pending queue for user decision
  - Prevents duplicate product creation

### Complete Workflow Example
1. **Process receipt** → Creates "senfk" product in Grocy
2. **Scan barcode** → OpenFoodFacts returns "Senfkörner"
3. **Fuzzy match** → Auto-selects "senfk" product (best match)
4. **Click "Namen ändern"** → Updates product name to "Senfkörner", adds barcode to Grocy
5. **Cross-table updated** → `openfood_name="Senfkörner"`, `has_grocy_barcode=true`
6. **Next receipt with "senfk"** → Logs show "Senfkörner", uses correct name

### Technical Details
- New API endpoint: `POST /api/pending/change-name` for name-only updates
- Enhanced API endpoint: `GET /api/available-products?search_term=X&without_barcode=true`
- Extended ProductAlias with `openfood_name` and `has_grocy_barcode` fields
- New AliasManager methods: `update_openfood_name()`, `mark_barcode_added_to_grocy()`, `get_aliases_without_grocy_barcode()`
- New GrocyClient method: `update_product_name()`

## [2.17.0-beta] - 2025-11-23

### Added
- **Optional Product Name Update** - Checkbox to choose whether to update product name or keep it
- Two scanning modes via checkbox:
  - **Mode 1 (✓ Checked):** Correct receipt names - "senfk" → "Senfkörner"
  - **Mode 2 (☐ Unchecked):** Normal scanning - Keep existing name, just add barcode

### How It Works
Each pending item now has a checkbox:
```
☑ Update product name to "Senfkörner"
```

**Checkbox CHECKED (default):**
- Updates product name with OpenFoodFacts/UPC name
- Perfect for correcting receipt abbreviations/typos
- Example: "senfk" becomes "Senfkörner"

**Checkbox UNCHECKED:**
- Keeps existing product name
- Just adds barcode and updates stock
- Perfect for products with good names already

### Example Workflows

**Workflow 1: Correct Receipt Name**
1. Receipt creates "senfk" (abbreviated)
2. Scan barcode → OpenFoodFacts finds "Senfkörner"
3. Checkbox: ✓ "Update product name to 'Senfkörner'" (default)
4. Click "Use Existing Product"
5. Result: Product renamed to "Senfkörner" ✅

**Workflow 2: Normal Scanning**
1. Product "Butter" exists (good name already)
2. Scan new barcode → OpenFoodFacts finds "Butter Kerrygold"
3. Checkbox: ☐ Uncheck (don't want to rename)
4. Click "Use Existing Product"
5. Result: Product stays "Butter", barcode added ✅

### Benefits
- ✅ Flexibility: Choose per-item whether to update name
- ✅ Default checked: Corrects receipt names automatically
- ✅ Easy to disable: Just uncheck for normal scanning
- ✅ Solves "two modes" requirement

## [2.16.0-beta] - 2025-11-23

### Added
- **Automatic Product Name Upgrade** - When using existing product, name is updated with OpenFoodFacts/UPC name
- New method: `update_product_name()` in GrocyClient

### How It Works
**Before:**
- Receipt creates: "senfk" (ID 123) - short name from receipt
- Scan barcode → "Use Existing" → Product stays "senfk"

**After:**
- Receipt creates: "senfk" (ID 123) - short name from receipt
- Scan barcode → OpenFoodFacts finds "Senfkörner"
- "Use Existing" → **Product name updated to "Senfkörner"** ✅
- Best of both worlds: Auto-create from receipts + Upgrade with proper names!

### Example Flow
1. Process receipt → Creates product "senfk" with alias
2. Scan barcode 4012345678901
3. OpenFoodFacts finds "Senfkörner"
4. UI shows pending: "Found in OpenFoodFacts: Senfkörner"
5. Select existing product "senfk" from dropdown
6. Click "Use Existing Product"
7. **Product renamed:** "senfk" → "Senfkörner"
8. Barcode added to product
9. Stock updated
10. Future scans find product directly in Grocy ✅

### Benefits
- No more abbreviated/typo names from receipts
- Clean, proper product names from external databases
- Maintains single product (no duplicates)
- Barcode linking works perfectly

## [2.15.2-beta] - 2025-11-23

### Fixed
- **UX Issue:** Auto-refresh now pauses when user interacts with pending items
- Dropdown selection no longer interrupted by 2-second auto-refresh
- Users can now comfortably select products without the list resetting

### Technical Details
- `loadPending()` skips refresh when:
  - Product dropdown has focus
  - Product dropdown has selected value
  - Use Existing/Create New buttons have focus
- Improves user experience significantly

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
