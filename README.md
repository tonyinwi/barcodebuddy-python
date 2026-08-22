# Barcode Buddy Python — Home Assistant Add-on Repository

Modern Python-based barcode scanner with Grocy integration for Home Assistant.

> ### ⚠️ This is a fork
>
> Upstream is **[sitaro/barcodebuddy-python](https://github.com/sitaro/barcodebuddy-python)**
> by Mathias Päzolt — all the credit for this add-on belongs there. This fork exists
> for one behavioural change: **unknown barcodes are auto-created instead of parked in
> a pending queue** ("fire-and-forget" scanning).
>
> | | |
> |---|---|
> | **Fork branch** | `fire-and-forget` — the only branch with changes |
> | **Pinned to upstream** | `new-features` @ `e76a458` (v2.18.2-beta) |
> | **Mirrors** | `main`, `new-features`, `mode_switch` are untouched copies of upstream |
>
> If you want the original behaviour, use upstream. See
> [the add-on README](barcodebuddy/README.md#-fork-behaviour-fire-and-forget) for what
> changed and why.

## Installation

1. Add this repository to Home Assistant:
   - Go to **Settings** → **Add-ons** → **Add-on Store** (three dots menu) → **Repositories**
   - For the fork: `https://github.com/tonyinwi/barcodebuddy-python`
   - For upstream: `https://github.com/sitaro/barcodebuddy-python`

2. Install the **Barcode Buddy Python** add-on

3. Configure and start the add-on

> **Using the fork's behaviour:** Home Assistant installs from the repository's default
> branch. To run the patched version, point the add-on repository at the
> `fire-and-forget` branch (or make it this fork's default branch). Reload the add-on
> after switching.

## Add-ons in this repository

### Barcode Buddy Python

Simple and clean Python implementation with USB scanner support and Grocy integration.

**Features:**
- ✅ USB Scanner Support
- ✅ Grocy Integration
- ✅ Modern Web UI
- ✅ Manual Entry
- ✅ Real-time Updates
- 🍴 **Fork:** auto-create on unknown barcode, no pending queue

For detailed documentation, see the [add-on README](barcodebuddy/README.md).

### Paperless Grocy Magic

Receipt-to-Grocy tooling. **Unmodified from upstream** — this fork does not touch it.

## Credits

Upstream project and all original work: **Mathias Päzolt**
([sitaro/barcodebuddy-python](https://github.com/sitaro/barcodebuddy-python)).
Original BarcodeBuddy concept: [Forceu/barcodebuddy](https://github.com/Forceu/barcodebuddy).
