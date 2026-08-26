"""
Put the app on the path.

`barcodebuddy/app` is the container's working directory, so its modules import
each other flatly (`import locations as loc`). Tests run from the repo root, so
the directory has to be added rather than the package imported -- turning it
into a package instead would change how the running add-on imports itself,
which is not a change worth making for tests.

Importing `main` is safe but not free: it builds a Flask app, reads Config from
the environment (finding nothing, so `grocy_client` stays None), and starts the
scanner's device-monitor DAEMON THREAD, which finds no devices on a laptop and
retries quietly. No network calls, and the server itself only runs under
`__main__`. Tests that only need pure helpers import the smaller modules
instead.
"""

import pathlib
import sys

APP = pathlib.Path(__file__).resolve().parents[1] / "barcodebuddy" / "app"
sys.path.insert(0, str(APP))
