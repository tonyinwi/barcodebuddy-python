# Barcode Buddy (Python) - Home Assistant Add-on

Modern Python-based barcode scanner with Grocy integration, OpenFoodFacts automatic product lookup, and multi-scanner support.

![Version](https://img.shields.io/badge/version-2.18.2--beta-blue.svg)
![Fork](https://img.shields.io/badge/fork-fire--and--forget-orange.svg)

> **This is a fork** of [sitaro/barcodebuddy-python](https://github.com/sitaro/barcodebuddy-python),
> pinned to `new-features` @ `e76a458`. One behavioural change: unknown barcodes are
> auto-created rather than queued for a UI decision. See
> [Fork behaviour](#-fork-behaviour-fire-and-forget).

## 🍴 Fork behaviour (fire-and-forget)

Upstream parks an unknown barcode in a **pending queue** and waits for you to resolve
it in the web UI. This fork does not: the scanner must never block on a human. Unknown
barcodes are created in Grocy immediately, and naming/dedup cleanup happens later in
batch, away from the scanning moment.

**What a scan of an unknown barcode does now:**

1. **Look the name up first.** Grocy enforces `UNIQUE` on `products.name`, so a title
   already seen under a different barcode attaches to the *existing* product instead of
   failing the create. Free dedup at intake.
2. **ADD mode** → create the product, attach the barcode, add the scanned quantity.
3. **CONSUME mode** → create the product at **0 stock with `min_stock_amount = 1`**,
   attach the barcode, and book nothing.

### Why CONSUME doesn't go negative

A consume scan of an unknown item means "this is gone and needs reordering." The
obvious encoding is −1 stock. **Grocy cannot represent that.** `ConsumeProduct()`
rejects any amount greater than current stock, unconditionally — there is no setting to
allow it, and inventory-correction routes back through the same check. Grocy's stock is
stock-entry (FIFO lot) based, so a negative balance has no lots to draw down.

So the reorder signal is carried by a **minimum** instead: a product sitting at 0 with a
minimum of 1 is "missing" as far as Grocy is concerned, which is the same mechanism
low-stock reordering already uses.

### ⚙️ Required Grocy setting

Enable **`shopping_list_auto_add_below_min_stock_amount`** (Grocy → Settings → Stock →
*"Add products that are below their minimum stock amount to the shopping list
automatically"*).

Without it the signal reaches Grocy's "missing products" view and **stops there** — it
never reaches the shopping list. Note it fires **on stock transactions**, not on a
timer, so a product created directly at 0-with-a-minimum only appears on the list at the
*next* add or consume.

### Also fixed in this fork

Two upstream routes created a product and then immediately consumed from it, which
always failed for the reason above — a brand-new product has no stock to draw down:

- `/api/create-product`
- `/api/pending/resolve` (`action=create_new`)

Both now create with a reorder point instead. The pending queue itself still exists and
its endpoints still work; the scan path simply no longer feeds it.

## 🎯 Key Features

### 🔢 **Quantity Barcodes** (v2.4.0)
Scan `BBUDDY-Q-X` to set quantity for the next product:
- `BBUDDY-Q-5` → Next scan adds 5 items
- Multiple quantities sum up: `BBUDDY-Q-2` + `BBUDDY-Q-3` = 5

### 🌐 **OpenFoodFacts Integration** (v2.3.0)
Automatic product creation from 2.5M+ products:
- Unknown barcodes → OpenFoodFacts lookup
- Automatic product creation in Grocy
- No manual data entry needed!

### 📱 **Multi-Scanner Support** (v2.2.0)
- Use multiple USB scanners simultaneously
- Automatic hot-plug detection
- Each scanner works independently

### 💻 **Modern Web UI**
- Real-time scan updates
- Manual barcode entry
- Recent scans history
- Status indicators

## 📋 Configuration

```yaml
scanner_device: "/dev/input/event3"
grocy_url: "http://homeassistant.local:9192"
grocy_api_key: "your-api-key-here"
debug: false
```

### Getting Your Grocy API Key

1. Open Grocy
2. Go to **Settings** → **Manage API Keys**
3. Click **Add** to create new key
4. Name it "Barcode Buddy"
5. Copy the key to your add-on configuration

### Scanner Device

Your USB scanner usually appears as `/dev/hidraw0`. To find yours:
- Check logs after starting the add-on
- Look for "Found accessible device" messages

## 🚀 Quick Start

### Basic Usage

1. **Start the add-on**
2. **Open Web UI** (click "Open Web UI" button)
3. **Scan a product barcode**
4. **Done!** Product is added to Grocy

### With Quantity Barcodes

1. Type `BBUDDY-Q-5` in manual input (or scan it)
2. Scan your product barcode
3. → 5 items added to Grocy

## 📖 How It Works

Scanning is **stateful**: a mode barcode switches between ADD and CONSUME, and every
product scan after it applies that mode until you switch again.

```
Scan Barcode
    │
    ├─ Is BBUDDY-Q-X? ──Yes──> Set quantity for next scan
    │
    ├─ Is a mode barcode? ──Yes──> Switch ADD ⇄ CONSUME
    │
    └─ Product barcode
        │
        ▼
   Known to Grocy? (by barcode, then alias)
        │
        ├─ Yes ──> ADD: +qty          CONSUME: −qty
        │
        └─ No
            │
            ▼
       External lookup (OpenFoodFacts → UPC Database)
            │
            ├─ Not found ──> ❓ reported, nothing created
            │
            └─ Found
                │
                ▼
           Name already in Grocy?
                │
                ├─ Yes ──> attach barcode to existing product
                │            └─ ADD: +qty   CONSUME: −qty
                │
                └─ No ───> create product
                             ├─ ADD:     stock = qty
                             └─ CONSUME: stock = 0, min_stock_amount = 1
                                          (flagged for reorder, nothing booked)
```

Everything created this way lands as a **raw product** — whatever name the external
database returned. Cleaning those names up and merging duplicates is a separate batch
step, deliberately not done at scan time.

## 🎨 UI Status Icons

| Icon | Meaning |
|------|---------|
| ➕ | Added to stock |
| ➖ | Removed from stock |
| ✨ | New product created (fork: auto-created, no prompt) |
| ✨ ➖ | Created and flagged for reorder (CONSUME of an unknown) |
| 🔗 | Matched an existing product — by alias, or by name on auto-create |
| 🔢 | Quantity set for next scan |
| ❓ | Barcode not found in Grocy, aliases, or any external database |
| ❌ | Error occurred |

## 🔧 Troubleshooting

### Scanner Not Working

**Check device path:**
- Try `/dev/hidraw0` instead of `/dev/input/event3`
- Check add-on logs for "Found accessible device" messages

**Enable debug mode:**
```yaml
debug: true
```
Restart and check logs for detailed error messages.

### Grocy Connection Issues

**Common problems:**
- Don't include `/api` in Grocy URL
- Use port number: `http://homeassistant.local:9192`
- Verify API key in Grocy settings
- Check Grocy is running and accessible

**302 Redirect errors:**
- System automatically retries after 2 seconds
- Check logs for "Grocy connection successful"

### Products Not Created

**Check:**
1. OpenFoodFacts has the product (search on openfoodfacts.org)
2. Barcode is valid EAN/UPC format
3. Grocy locations exist (add-on uses first available)
4. Debug logs show exact error from Grocy

### A CONSUME scan didn't reduce stock (fork)

**Expected**, if the barcode was unknown. There was nothing to reduce — the product did
not exist. It is created at 0 with a reorder point instead. Look for
`flagged for reorder` in the logs and check Grocy's *Missing products*.

### Reordered items never reach the shopping list (fork)

Enable **`shopping_list_auto_add_below_min_stock_amount`** in Grocy. Without it, items
stop at Grocy's "missing products" view.

If it *is* on and the item still isn't listed, remember the sweep runs **on stock
transactions**, not on a schedule — the next add or consume will pull it in.

### `UNIQUE constraint failed: products.name`

Two barcodes resolved to the same product title. The fork handles this by attaching the
new barcode to the existing product (logged as `🔗 already exists`). Seeing the raw
error in the logs means the create path was reached some other way — check whether the
name differs only by case or whitespace.

## 📝 Version History

Add-on version is **2.18.2-beta**. See **[CHANGELOG.md](CHANGELOG.md)** — that is the
authoritative history for this add-on, and fork changes are listed at the top under
*Fork: fire-and-forget*.

> The repo-root `../CHANGELOG.md` tracks the `main` branch lineage and stops at
> 2.11.1-beta, so it is stale for this add-on. Use the one alongside this README.

The table below is a summary of milestone releases, not every version.

| Version | Key Feature |
|---------|-------------|
| 2.18.2-beta | Current upstream pin for this fork (`e76a458`) |
| 2.4.0 | Quantity Barcodes |
| 2.3.0 | OpenFoodFacts Integration |
| 2.2.0 | Multi-Scanner Support |
| 2.1.0 | USB Scanner via hidraw |
| 2.0.0 | Python Rewrite |

### Fork changes (not upstream)

| Commit | Change |
|--------|--------|
| `8d5c6e3` | Auto-create unknown barcodes; name-collision reuse; reorder point for CONSUME-unknown; fixes create-then-consume in two routes |
| `4d5da2b` | Normalise product ids to `int` across `create_product` / `find_product_by_name` |

## 🏗️ Technical Details

**Built with:**
- Python 3.11 (Alpine Linux)
- Flask 3.0 (Web Framework)
- hidraw (USB Scanner access)
- requests (API communication)

**Architecture:**
- Multi-threaded scanner handling
- Session-based Grocy API client
- Automatic retry logic for API calls
- Dynamic location/quantity unit detection

## 🤝 Support

**Getting Help:**
- Check this README and CHANGELOG
- Enable debug mode and check logs
- Report issues on GitHub — but **check which behaviour you're hitting first**:
  - Anything in [Fork behaviour](#-fork-behaviour-fire-and-forget) (auto-create,
    reorder points, name-collision reuse) belongs on **this fork's** tracker
  - Everything else — scanners, the web UI, modes, quantity barcodes — is upstream's;
    please reproduce on [sitaro/barcodebuddy-python](https://github.com/sitaro/barcodebuddy-python)
    before filing there, so upstream isn't triaging our patches

**Providing Logs:**
1. Set `debug: true`
2. Restart add-on
3. Reproduce issue
4. Copy logs from Home Assistant

## 📜 License

This add-on is provided as-is for Home Assistant users.

## 🙏 Acknowledgments

- **This add-on**: [sitaro/barcodebuddy-python](https://github.com/sitaro/barcodebuddy-python)
  by Mathias Päzolt — this repository is a fork; all original work is theirs
- **Original BarcodeBuddy**: [Forceu/barcodebuddy](https://github.com/Forceu/barcodebuddy)
- **OpenFoodFacts**: [World's largest open food database](https://world.openfoodfacts.org/)
- **Grocy**: [ERP beyond your fridge](https://grocy.info/)

---

Built with ❤️ using [Claude Code](https://claude.com/claude-code)
