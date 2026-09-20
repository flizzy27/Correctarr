"""Shared setup.

``app.main`` builds its store at import time from ``CONFIG_DIR``. Pointing that
at a temporary directory before anything imports it keeps the test run from
touching a real installation — and makes importing the module safe on a
developer machine, where ``/config`` does not exist.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEMP = Path(tempfile.mkdtemp(prefix="correctarr-tests-"))
os.environ["CONFIG_DIR"] = str(_TEMP)
os.environ.setdefault("AUTH", "on")
os.environ.setdefault("VERSION", "test")
# Keep the language deterministic regardless of the machine running the tests.
os.environ.setdefault("TZ", "Etc/UTC")
