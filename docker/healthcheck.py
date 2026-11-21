"""Container liveness probe: exit 0 if /health answers 200 or 503, else 1.

503 means "running but not ready" (e.g. fewer than two distinct keys); the container
is alive and must not be restarted or block the frontend because of it.
"""

import os
import sys
import urllib.error
import urllib.request

url = f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/health"
try:
    urllib.request.urlopen(url, timeout=4)  # noqa: S310 — fixed local URL
except urllib.error.HTTPError as exc:
    sys.exit(0 if exc.code == 503 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
sys.exit(0)
