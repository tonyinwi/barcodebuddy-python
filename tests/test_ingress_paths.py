"""
The page is served under Home Assistant's ingress prefix, so every URL it
builds must be RELATIVE.

Found live 2026-09-01: one `fetch('/api/locations/set', …)` among nine relative
siblings. Under ingress the app lives at `/api/hassio_ingress/<token>/`, so a
leading slash does not address the add-on -- it addresses the Home Assistant
host, where `/api/…` is Home Assistant's OWN REST API. That answered with HTML,
`r.json()` threw, and the browser reported:

    Could not reach the scanner: SyntaxError: The string did not match the
    expected pattern.

which names neither the path nor the prefix, and reads like the endpoint is
broken when the request never arrived. It also cannot be caught by opening the
page directly on :5000, where there is no prefix and both forms work -- so the
bug is invisible in exactly the environment you would test in.
"""

import pathlib
import re

TEMPLATES = (pathlib.Path(__file__).resolve().parents[1]
             / "barcodebuddy" / "app" / "templates")


def _sources():
    for path in sorted(TEMPLATES.glob("*.html")):
        yield path, path.read_text()


def test_no_fetch_uses_an_absolute_path():
    bad = []
    for path, text in _sources():
        for m in re.finditer(r"""fetch\(\s*['"](/[^'"]*)['"]""", text):
            line = text[:m.start()].count("\n") + 1
            bad.append(f"{path.name}:{line} fetch('{m.group(1)}')")
    assert not bad, (
        "absolute fetch paths escape the ingress prefix and hit Home "
        "Assistant's own API:\n  " + "\n  ".join(bad))


def test_no_form_action_or_src_escapes_the_prefix():
    """
    Same failure, other tags. A `src="/static/…"` or `action="/api/…"` is
    silently wrong under ingress in the same way, and only the fetch case has
    bitten so far.
    """
    bad = []
    for path, text in _sources():
        for m in re.finditer(r"""\b(action|src)\s*=\s*['"](/(?!/)[^'"]*)['"]""", text):
            line = text[:m.start()].count("\n") + 1
            bad.append(f"{path.name}:{line} {m.group(1)}=\"{m.group(2)}\"")
    assert not bad, "absolute paths under ingress:\n  " + "\n  ".join(bad)


def test_the_page_still_calls_the_endpoints_it_needs():
    """
    The rule above is satisfiable by deleting every fetch, so pin that the
    calls the shelf feature depends on are actually present.
    """
    text = (TEMPLATES / "index.html").read_text()
    for needed in ("api/locations/set", "api/locations/clear", "api/locations"):
        assert f"fetch('{needed}'" in text, f"nothing calls {needed}"
