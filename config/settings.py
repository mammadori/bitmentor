import os
import sys
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .community import COMMUNITY_DIR, community

BASE_DIR = Path(__file__).resolve().parent.parent
# The usual mistakes of a first installation stop here, with what to do, rather than later with a traceback
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY is empty: generate one with `openssl rand -hex 32` and put it in the .env")
if not (COMMUNITY_DIR / "community.yml").is_file():
    raise ImproperlyConfigured(f"COMMUNITY_DIR {COMMUNITY_DIR} has no community.yml: copy examples/community and point "
                               "COMMUNITY_DIR at the copy")
DEBUG = os.environ.get("DEBUG") == "1"
NETWORK = os.environ.get("NETWORK", "signet")
if NETWORK not in ("signet", "mainnet"):
    raise ImproperlyConfigured(f"NETWORK is {NETWORK!r}: it must be signet or mainnet")
if DEBUG and NETWORK == "mainnet":
    raise ImproperlyConfigured("DEBUG cannot be turned on on mainnet")
# The fake explorer (shops/explorer.py): the register asks the site itself whether an on-chain payment arrived, and a
# page of the site pretends to pay. For the tests and practice sites, where no public explorer sees anything; never
# on mainnet, so never in production
FAKE_EXPLORER = os.environ.get("FAKE_EXPLORER") == "1"
if FAKE_EXPLORER and NETWORK == "mainnet":
    raise ImproperlyConfigured("FAKE_EXPLORER cannot be turned on on mainnet")
# Who sees the site: Caddy decides it with caddy/<mode>.caddy. Here we only check that the mode is a real one and that in
# production the register takes real payments.
SITE_MODES = ("development", "maintenance", "production")
SITE_MODE = os.environ.get("SITE_MODE") or "development"
# Since when, YYYY-MM-DD: deploy/mode.sh writes it at every change and /health/ shows it
SITE_MODE_SINCE = os.environ.get("SITE_MODE_SINCE", "")
if SITE_MODE not in SITE_MODES:
    raise ImproperlyConfigured(f"SITE_MODE is {SITE_MODE}: it must be one of {', '.join(SITE_MODES)}")
if SITE_MODE == "production" and NETWORK != "mainnet":
    raise ImproperlyConfigured(f"SITE_MODE production with NETWORK {NETWORK}: in production the register runs on mainnet")
# The addresses that see the site in development (Caddy reads the same variable) and, in every mode, the private part of
# the pages: website/visibility.py
DEV_ALLOWED_IPS = os.environ.get("DEV_ALLOWED_IPS", "")
# Team access (team/, docs/features/team-access.md): on a site under construction the team signs with a Lightning wallet
# and its browser counts as an allowed address. Caddy reads the same variable (Caddyfile, caddy/development.caddy)
TEAM_ACCESS = os.environ.get("TEAM_ACCESS") or "off"
if TEAM_ACCESS not in ("on", "off"):
    raise ImproperlyConfigured(f"TEAM_ACCESS is {TEAM_ACCESS}: it must be on or off")
TEAM_ACCESS = TEAM_ACCESS == "on"
# Who reaches /admin/ is Caddy's rule (Caddyfile, `admin`); here the variable is only read by `manage.py preflight`,
# which says when team access is on and /admin/ is open from anywhere
ADMIN_ALLOWED_IPS = os.environ.get("ADMIN_ALLOWED_IPS", "")
# manage.py demo_data (shops/demo.py) fills a site with made-up shops: it runs only on an empty site in development mode,
# or in maintenance mode when this is set on purpose. Never on mainnet, in production, or beside a real shop or account
DEMO_ALLOWED = os.environ.get("BITMENTOR_DEMO") == "1"
SITE_HOST = os.environ.get("SITE_HOST", "localhost")
# Caddy's too: http behind another proxy that holds TLS (README, "Behind another proxy"). ONION_LOCATION: README, "Onion"
SITE_SCHEME = os.environ.get("SITE_SCHEME", "https")
ONION_LOCATION = os.environ.get("ONION_LOCATION", "")
ALLOWED_HOSTS = [SITE_HOST, "localhost", "127.0.0.1", "web", ".onion"]
CSRF_TRUSTED_ORIGINS = [f"https://{SITE_HOST}"]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = int(os.environ.get("HSTS_SECONDS", 86400))
# Caddy does the redirect to HTTPS, and the onion service must stay on HTTP
SILENCED_SYSTEM_CHECKS = ["security.W008"]
# The site receives no files. With no handlers a file sent with a form is discarded: with Django's own handlers it would end up in /tmp, which is in
# memory, with no size limit. Form fields fit in 1 MiB, the same limit Caddy puts on request bodies.
FILE_UPLOAD_HANDLERS = []
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "website",
    "shops",
    "meetups",
    "team",
]

MIDDLEWARE = [
    "config.middleware.HeadWithoutBody",  # first: it is the last to touch the response
    "django.middleware.security.SecurityMiddleware",
    "config.middleware.SecurityHeaders",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "config.guard.StaffApart",  # right after the accounts: outside /admin/ the staff is a visitor, never a merchant
    "config.temporary.TemporaryPassword",  # after it: a staff member with a temporary password goes to change it
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "website.visibility.VisibilityMiddleware",  # after the accounts: the owner of a certified shop sees the private part
    "config.middleware.PrivateForAccounts",  # last: a page answered to someone signed in is kept by no cache
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [COMMUNITY_DIR / "templates", BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {
        # The meetup tags need no {% load %} in the community's pages, like the includes of the other parts
        "builtins": ["meetups.templatetags.meetups"],
        "context_processors": [
            "django.template.context_processors.request",
            "django.template.context_processors.i18n",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
            "config.community.context",
            "website.visibility.context",
        ],
    },
}]

if os.environ.get("POSTGRES_HOST"):
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ["POSTGRES_HOST"],
        "NAME": os.environ.get("POSTGRES_DB", "bitmentor"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
    }}
else:
    # In a test container /app is read-only: SQLITE_PATH moves it elsewhere
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.environ.get("SQLITE_PATH", BASE_DIR / "db.sqlite3")}}

# On file, shared by all the workers: the request limits and the pages kept for everybody must be the same in each
CACHES = {"default": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
                      "LOCATION": Path(tempfile.gettempdir()) / "bitmentor-cache", "OPTIONS": {"MAX_ENTRIES": 10000}}}
if sys.argv[1:2] == ["test"]:
    # One cache per test process: the file cache in /tmp is shared, and with --parallel (or two test runs at once)
    # a test reads the pages and the limits another process has just written
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    # A fast hasher in the tests: the real PBKDF2 makes every sign-in cost a second on a small machine. The tests of the
    # hasher upgrade set their own list with override_settings
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher", "django.contrib.auth.hashers.PBKDF2PasswordHasher"]
# Seconds /shops.json and the list under the map are kept, the same for everybody (shops/public_cache.py). Off in the
# tests, which change shops and switches between two requests; the tests of the cache turn it on
PUBLIC_LIST_SECONDS = 0 if sys.argv[1:2] == ["test"] else 60

# Django and the libraries say only their errors; the site's own apps also say their warnings (an address the guardian
# cannot reach, a mail not sent), which an operator wants to find in the logs
LOGGING = {"version": 1, "disable_existing_loggers": False,
           # A path that opens something (the token of a team invite) is never written whole: config/quiet.py
           "filters": {"quiet_paths": {"()": "config.quiet.QuietPaths"}},
           "handlers": {"console": {"class": "logging.StreamHandler", "level": "WARNING", "filters": ["quiet_paths"]}},
           "root": {"handlers": ["console"], "level": "ERROR"},
           "loggers": {app: {"level": "WARNING"} for app in ("config", "shops", "website", "meetups", "team")}}

LANGUAGES = [(code, name) for code, name in community()["languages"].items()]
LANGUAGE_CODE = LANGUAGES[0][0]
TEST_RUNNER = "config.test_runner.Runner"  # after every test the language goes back to the default
LOCALE_PATHS = [COMMUNITY_DIR / "locale", BASE_DIR / "locale"]
# Our own date formats where Django's read oddly (config/formats/<language>/formats.py)
FORMAT_MODULE_PATH = ["config.formats"]
USE_I18N = True
USE_TZ = True
TIME_ZONE = community().get("timezone", "UTC")

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticroot"
STATICFILES_DIRS = [BASE_DIR / "static"]
# Names with the fingerprint (base.3f2a….css): WhiteNoise serves them with a one-year cache, and the name changes with every
# version. The originals stay, for whoever cites them by hand. The url() inside the CSS are not rewritten: Leaflet cites images it lacks.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "config.storage.FingerprintedStaticFiles"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Absolute links are always https://SITE_HOST. The one exception is the five-minute try of the README (DEBUG, so never
# on mainnet, and no SITE_HOST): the site is then the one `runserver` serves, and the sign-in links must open there
PUBLIC_URL = f"https://{SITE_HOST}" if os.environ.get("SITE_HOST") or not DEBUG else "http://localhost:8000"
# Version of the software and, if a release image adds the community, of the site: /health/ shows them
BITMENTOR_VERSION = os.environ.get("BITMENTOR_VERSION", "")
SITE_VERSION = os.environ.get("SITE_VERSION", "")
# Where the source of the version this site runs is (AGPLv3, section 13): the upstream repository by default
SOURCE_URL = os.environ.get("SOURCE_URL", "https://github.com/mammadori/bitmentor")
# The community's own code, where the software asks for it: the dotted name of a module (config/hooks.py). Empty here;
# a community sets it in a settings module of its own, never in `community.yml` or in the `.env`
COMMUNITY_HOOKS = ""
# At most one change of a shop's Lightning address by its owner in this many days (shops/address_change.py)
LIGHTNING_CHANGE_DAYS = int(os.environ.get("LIGHTNING_CHANGE_DAYS", "7"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
TELEGRAM_MODERATION_CHAT = os.environ.get("TELEGRAM_MODERATION_CHAT", "")
# The OAuth2 token of the OpenStreetMap account that signs the edits of manage.py osm_upload (docs/features/openstreetmap.md).
# Only that command reads it; without it nothing is ever sent to OpenStreetMap
OSM_TOKEN = os.environ.get("OSM_TOKEN", "")
NOSTR_SECRET_KEY = os.environ.get("NOSTR_SECRET_KEY", "")
NOSTR_RELAYS = [relay for relay in os.environ.get("NOSTR_RELAYS", "").split(",") if relay]
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = True
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@localhost")
# On the console the login link would end up in the logs: on mainnet, with no mail server, sending fails and says so.
# On a test network the console is how a test site works without a mail server (docs/configuration.md#the-network)
EMAIL_REAL_BACKEND = f"django.core.mail.backends.{'smtp' if EMAIL_HOST or NETWORK == 'mainnet' else 'console'}.EmailBackend"
# Every email goes through the one that counts them and keeps the provider's monthly quota (config/mailbudget.py)
EMAIL_BACKEND = "config.mailbudget.CountingBackend"
