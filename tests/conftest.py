"""Points MAIER_FOLDER at a fresh tmp dir before maier.settings is ever
imported. The actual env var assignment happens in `_bootstrap.py`, loaded
even earlier than this conftest via `-p _bootstrap` (see pyproject.toml) --
see that module's docstring for why. Importing it here is just so this is
the obvious place to look.
"""

from _bootstrap import TEST_FOLDER  # noqa: F401
