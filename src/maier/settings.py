"""Django settings for the Maier project.

Unlike a typical Django project, most of the interesting state here is not
global config but *per working folder*: the SQLite DB lives inside the
folder that was opened (`{folder}/.maier/maier.sqlite3`). `maier.cli`
sets the `MAIER_FOLDER` env var before Django is set up. When nothing has
set it (e.g. `django-admin makemigrations`, or pytest collection before the
per-test fixture folder exists), we fall back to a placeholder directory
under the user cache dir so the settings module can still be imported.
"""

import os
import secrets
from pathlib import Path

import platformdirs

BASE_DIR = Path(__file__).resolve().parent

APP_NAME = "Maier"

# --- Working folder -------------------------------------------------------

_folder_env = os.environ.get("MAIER_FOLDER")
if _folder_env:
    WORKING_FOLDER = Path(_folder_env).expanduser().resolve()
else:
    # Placeholder so settings can be imported without a real folder open
    # (makemigrations, pytest collection, docs tooling, ...).
    WORKING_FOLDER = Path(platformdirs.user_cache_dir(APP_NAME)) / "_no_folder"

MAIER_DIR = WORKING_FOLDER / ".maier"
MAIER_DIR.mkdir(parents=True, exist_ok=True)
(MAIER_DIR / "previews").mkdir(parents=True, exist_ok=True)
(MAIER_DIR / "logs").mkdir(parents=True, exist_ok=True)
(MAIER_DIR / "staticfiles").mkdir(parents=True, exist_ok=True)

# --- Global config (platformdirs, per SPEC §11) ---------------------------

GLOBAL_CONFIG_DIR = Path(platformdirs.user_config_dir(APP_NAME))
GLOBAL_DATA_DIR = Path(platformdirs.user_data_dir(APP_NAME))
GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
GLOBAL_DATA_DIR.mkdir(parents=True, exist_ok=True)


def _get_or_create_secret_key() -> str:
    key_path = GLOBAL_CONFIG_DIR / "secret_key"
    if key_path.exists():
        existing = key_path.read_text().strip()
        if existing:
            return existing
    key = secrets.token_urlsafe(64)
    key_path.write_text(key)
    try:
        key_path.chmod(0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX permission bits
    return key


SECRET_KEY = _get_or_create_secret_key()

DEBUG = os.environ.get("MAIER_DEBUG", "").lower() in ("1", "true", "yes", "on")

ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "maier.core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    # set-status moves files on POST; CSRF protection stops drive-by pages
    # from form-POSTing to 127.0.0.1 even though we only bind localhost.
    "django.middleware.csrf.CsrfViewMiddleware",
]

ROOT_URLCONF = "maier.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "maier.wsgi.application"

# --- Database (SQLite, WAL mode, cache role — SPEC §7/§11) ----------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(MAIER_DIR / "maier.sqlite3"),
        "OPTIONS": {
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"

# Desktop app, single user, no build step: whitenoise serves static files
# straight from each app's static/ dir (WHITENOISE_USE_FINDERS) so there is
# never a collectstatic step for users running `maier open`.
STATIC_URL = "static/"
STATIC_ROOT = MAIER_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = DEBUG
