"""Set CULLER_FOLDER before culler.settings can possibly be imported.

pytest-django forces an import of the settings module inside its
`pytest_load_initial_conftests` hookimpl, which runs *before* pytest loads
any conftest.py (pytest's own conftest-loading hookimpl is `trylast`).
Settings module-level constants (working folder, DB path) are computed once
at that first import, so setting the env var from conftest.py fixtures/hooks
is too late.

`-p <module>` plugins are imported during argument preparsing, which happens
even earlier than `pytest_load_initial_conftests` -- see the `addopts` /
`pythonpath` entries in pyproject.toml that load this module by name.
"""

import os
import tempfile
from pathlib import Path

TEST_FOLDER = Path(tempfile.mkdtemp(prefix="culler-pytest-"))
os.environ["CULLER_FOLDER"] = str(TEST_FOLDER)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "culler.settings")
