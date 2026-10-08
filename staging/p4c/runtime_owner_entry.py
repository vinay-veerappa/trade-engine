"""The staged runtime-owner plugin shim (P4c T14, canary-owned).

The kit's rendered configs name this module (``plugin_module:
"runtime_owner_entry"``) with ``plugin_paths`` pointing at the staging
folder. It is a two-line shim: it delegates to the client's real
dispatcher, ``scan_engine.runtime_owner_entry.owner_entry``, which
routes each job to the converted T10/T11/T12 owner entries. The client
repository is on ``sys.path`` through the configured
``site_packages`` (scan-engine is installed editable in the live
``.venv313``); the fallback path insert keeps the shim importable even
against a bare interpreter.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CLIENT_SRC = Path(r"C:\Users\vinay\tvDownloadOHLC\src")
if str(_CLIENT_SRC) not in sys.path:
    sys.path.insert(0, str(_CLIENT_SRC))

from scan_engine.runtime_owner_entry import owner_entry  # noqa: F401,E402