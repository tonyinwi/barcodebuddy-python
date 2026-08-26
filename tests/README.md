# Tests

```bash
python3 -m venv .venv-test && .venv-test/bin/pip install -r requirements-dev.txt
.venv-test/bin/python -m pytest tests/ -q
```

Everything here is a pure function or a class with no I/O. There is no Grocy in
the loop and no scanner: `Config` finds nothing in the environment, so
`grocy_client` stays `None`, and the device monitor finds no devices.

**Why these functions and not others.** Each one has already failed silently in
production, which is the only kind of failure worth a test in a fork this size:

| | What it cost |
|---|---|
| `LocationTracker` keying | Keyed on the device node while `resolve_scan_mode()` keyed on the USB id. One gun is several `/dev/hidrawN` nodes, so a shelf silently stopped applying and products landed on the preset. Nothing errored. |
| `_tidy` | Threw on every scan for days — a missing `import re` — so every new product lost its barcode, and it looked exactly like a missing feature. |
| `is_product_code` | Two QR payloads became products, one of them a real jar whose UPC was already in the catalogue. |
| `_scan_outcome` | Decides the per-shelf count. Counting a mode switch makes the only number worth reading wrong. |

The tests state the reasoning, not just the assertion, because in every case the
correct behaviour looks arbitrary until you know what went wrong.
