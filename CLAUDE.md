# barcodebuddy-python — fork

Fork of [sitaro/barcodebuddy-python](https://github.com/sitaro/barcodebuddy-python),
pinned to `new-features` @ `e76a458` (2.18.2-beta). It exists for one behavioural
change: **unknown barcodes auto-create instead of queuing for a UI decision**
("fire-and-forget").

**Read first:** [barcodebuddy/CHANGELOG.md](barcodebuddy/CHANGELOG.md) — the fork
section at the top lists every divergence and why. The design record lives in
[kitchen-stack](https://github.com/tonyinwi/kitchen-stack), especially
`barcodebuddy/FORK.md`.

## Branch discipline

- **`fire-and-forget`** is where all fork work goes, and is the repo's **default
  branch** — Home Assistant clones the default branch, and it can't take a `/tree/`
  URL (a `#branch` fragment does work).
- **`main`, `new-features`, `mode_switch` are pristine mirrors of upstream.** Leave
  them alone; that's what makes the pin meaningful.

Note the repo is an **HA add-on repository** — app code is at `barcodebuddy/app/`,
not `app/`.

## Deploying — the sequence matters

```bash
ha store reload                                  # git pull; rebuild alone does NOT
ha apps rebuild c308acc8_barcodebuddy-python
```

**`ha apps rebuild` does not pull.** It rebuilds from the existing clone, so a pushed
fix silently doesn't deploy. Worse, a partially-stale clone looks right: the add-on
once showed the new forked *name* while running pre-fix lookup code.

**Verify by behaviour, not version** — the version string never changes when you push.
Scan a barcode that misses every source (creates nothing) and check the log:

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"barcode":"111111111119"}' http://c308acc8-barcodebuddy-python:5000/api/scan
```

A `Not found via Grocy external lookup` line proves the current build is live.

Also: **a failed rebuild takes the add-on down**, since the old image is removed first.
Builds pull from Docker Hub, which has been unreliable here — see the homelab repo's
DNS notes.

## Behaviour worth not breaking

- **Lookup order is Grocy → OpenFoodFacts → UPC Database**, deliberately. Grocy's own
  plugin (UPCitemdb) is US-focused and supplies the user's presets; the others catch
  imported goods it misses.
- **OpenFoodFacts requires a real User-Agent.** It rejects the `requests` default with
  403, and because the caller only sees `None` that failure reads as "not found".
- **Grocy's presets, not `locations[0]`.** The blind fallback is usually a fridge, so
  shelf-stable goods ended up in cold storage.
- **Nothing emits the `not_found` status any more.** That's what the UI used to render
  a product-name prompt from. Keep it that way.
- Products are tagged `review_status=raw`; brand and lookup source go on the **barcode**.

## Upstream

Fix genuinely upstream bugs here and consider sending them back — several were fixed
in passing (the OFF User-Agent, create-then-consume in two routes). Keep fork-specific
behaviour clearly commented as `FORK PATCH #1` so the divergence stays legible.
