"""
One record per PROVIDER ATTEMPT, not per barcode.

The point is comparing providers on the same input: which one answered, how
fast, how often it was throttled, and how often it handed back a code that was
not the one we asked about. A per-barcode record cannot answer any of that.

Why this ships before the first bulk inventory: that inventory is the best
provider-comparison dataset this household will ever produce -- hundreds of real
barcodes hitting every provider in one burst. Without the log in place first,
that data is simply gone, and the "which provider earns first position" question
stays a guess for another month of ordinary scanning.

Written to /share, NOT the add-on's /data: /data is destroyed when an add-on is
uninstalled, and this log is the evidence behind a purchasing decision. /share
survives that and is included in Home Assistant's backups.

Logging must NEVER break a scan. Every public method swallows its own errors --
a scanner that stops working because the log could not be written would be a
strictly worse trade than losing a record.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

DEFAULT_PATH = "/share/kitchen-stack/lookup_log.jsonl"

# Outcomes. Deliberately finer than hit/miss, because today's two real bugs were
# both invisible under a coarse split: a provider answering success with an empty
# title, and a provider echoing back a different code.
HIT = "hit"                     # a usable product came back
MISS = "miss"                   # provider had nothing
NO_NAME = "no_name"             # answered, but with no usable name -- upcdatabase does this
ECHO_REJECT = "echo_reject"     # answered about a DIFFERENT code than we asked
THROTTLED = "throttled"         # rate limited
AUTH_ERROR = "auth_error"       # key missing, invalid or rejected
ERROR = "error"                 # transport or parse failure
SKIPPED_NON_GTIN = "skipped_non_gtin"
SKIPPED_NO_KEY = "skipped_no_key"


class _Attempt:
    """Times one provider call and records it however the block exits."""

    def __init__(self, log, barcode, provider):
        self._log, self.barcode, self.provider = log, barcode, provider
        self.outcome = MISS
        self.echo_ok = None
        self.detail = None
        self.latency_ms = 0
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.outcome = ERROR
            self.detail = f"{exc_type.__name__}: {exc}"[:120]
        self.latency_ms = int((time.monotonic() - self._t0) * 1000)
        self._log.record(self.barcode, self.provider, self.outcome,
                         self.latency_ms, echo_ok=self.echo_ok, detail=self.detail)
        return False        # never swallow the caller's exception


class LookupLog:
    def __init__(self, path=DEFAULT_PATH, cap=20000):
        self.path = path
        self.cap = cap
        self._lock = threading.Lock()
        self._writes = 0

    def attempt(self, barcode, provider):
        return _Attempt(self, barcode, provider)

    def record(self, barcode, provider, outcome, latency_ms=0,
               echo_ok=None, detail=None):
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "barcode": str(barcode or ""),
            "provider": provider,
            "outcome": outcome,
            "latency_ms": latency_ms,
        }
        if echo_ok is not None:
            row["echo_ok"] = bool(echo_ok)
        if detail:
            row["detail"] = detail
        try:
            with self._lock:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a") as fh:
                    fh.write(json.dumps(row) + "\n")
                self._writes += 1
                # Trimming rewrites the file, so do it rarely rather than per write.
                if self._writes % 500 == 0:
                    self._trim_locked()
        except Exception:                                        # noqa: BLE001
            pass        # a scan must never fail because of the log

    def _trim_locked(self):
        try:
            with open(self.path) as fh:
                lines = fh.readlines()
            if len(lines) <= self.cap:
                return
            with open(self.path, "w") as fh:
                fh.writelines(lines[-self.cap:])
        except Exception:                                        # noqa: BLE001
            pass

    def rows(self, days=30):
        cutoff = time.time() - days * 86400
        out = []
        try:
            with open(self.path) as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                        ts = datetime.fromisoformat(row["ts"]).timestamp()
                        if ts >= cutoff:
                            out.append(row)
                    except Exception:                            # noqa: BLE001
                        continue        # one bad line must not poison the report
        except FileNotFoundError:
            pass
        except Exception:                                        # noqa: BLE001
            pass
        return out

    def stats(self, days=30):
        """
        Per-provider aggregates. Surfaces the data; does NOT reorder anything.

        Automatic reordering on noisy data would shuffle the chain behind your
        back, and an opaque chain is exactly what makes a wrong lookup hard to
        diagnose later.
        """
        rows = self.rows(days)
        by = {}
        for r in rows:
            p = by.setdefault(r.get("provider", "?"), {
                "attempts": 0, "outcomes": {}, "latencies": [], "echo_failures": 0})
            p["attempts"] += 1
            o = r.get("outcome", "?")
            p["outcomes"][o] = p["outcomes"].get(o, 0) + 1
            if isinstance(r.get("latency_ms"), int):
                p["latencies"].append(r["latency_ms"])
            if r.get("echo_ok") is False:
                p["echo_failures"] += 1

        def pct(n, d):
            return round(100.0 * n / d, 1) if d else 0.0

        report = {}
        for name, p in by.items():
            lat = sorted(p["latencies"])
            # Attempts the provider was actually asked to answer -- skips are not
            # the provider's fault and would flatter or punish it unfairly.
            asked = p["attempts"] - sum(p["outcomes"].get(k, 0)
                                        for k in (SKIPPED_NON_GTIN, SKIPPED_NO_KEY))
            report[name] = {
                "attempts": p["attempts"],
                "asked": asked,
                "hits": p["outcomes"].get(HIT, 0),
                "hit_rate_pct": pct(p["outcomes"].get(HIT, 0), asked),
                "throttled": p["outcomes"].get(THROTTLED, 0),
                "echo_failures": p["echo_failures"],
                "median_ms": lat[len(lat) // 2] if lat else None,
                "p95_ms": lat[int(len(lat) * 0.95)] if lat else None,
                "outcomes": p["outcomes"],
            }
        return {"days": days, "records": len(rows), "providers": report}
