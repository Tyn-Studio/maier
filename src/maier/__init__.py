"""Maier -- local-first, folder-centric photo culling.

`__version__` is the single source of truth for the package version:
`pyproject.toml` reads it via `[tool.hatch.version] path = ...` (dynamic
version, no duplicated string to drift), and `core/updates.py` imports it
directly -- this works identically whether Maier is running from an
installed wheel or a PyInstaller bundle (no importlib.metadata lookup,
which a frozen bundle doesn't support the same way).
"""

__version__ = "0.1.10"
