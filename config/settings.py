import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# Lightweight .env loader (KEY=VALUE per line, # comments ok). Existing
# environment variables win over .env values. No external dependency.
_envfile = BASE_DIR / ".env"
if _envfile.exists():
    for _line in _envfile.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
        os.environ.setdefault(_k, _v)

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

# SECRET_KEY must come from the environment. The "dev-insecure-change-me"
# fallback is only tolerated while DEBUG is on; any non-DEBUG run with a
# missing (or fallback) key fails loudly instead of silently signing
# sessions / password-reset tokens / signed unsubscribe URLs with a known,
# public secret.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY == "dev-insecure-change-me":
    if not DEBUG:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set (and not the dev placeholder) "
            "when DJANGO_DEBUG is disabled."
        )
    SECRET_KEY = "dev-insecure-change-me"

# Sentry: zero-effort error monitoring. Init is a no-op if SENTRY_DSN is not
# set, so dev/CI/local don't need to know about Sentry at all. To enable in
# prod: heroku config:set SENTRY_DSN=https://... — restart picks it up.
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN and not DEBUG:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            integrations=[DjangoIntegration()],
            traces_sample_rate=0.1,
            send_default_pii=False,  # never PII, even in error context
            environment=os.environ.get("SENTRY_ENV", "production"),
        )
    except ImportError:
        pass  # sentry-sdk not yet installed locally — fine, we're not DEBUG anyway
# In production never fall back to a wildcard allow-list — force explicit
# hosts. The wildcard is only a local-dev convenience (DEBUG on).
_hosts_raw = os.environ.get("DJANGO_ALLOWED_HOSTS", "*" if DEBUG else "")
ALLOWED_HOSTS = [h.strip() for h in _hosts_raw.split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "pharmacypulse",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *(["whitenoise.middleware.WhiteNoiseMiddleware"]
      if __import__("importlib").util.find_spec("whitenoise") else []),
    # GeoBlock runs early — before sessions/auth — so non-US IPs never touch
    # the auth or DB layers. Disabled by default; set GEO_BLOCK_ENABLED=1 in
    # prod to turn on. Crawler User-Agents are whitelisted (SEO).
    "pharmacypulse.middleware.GeoBlockMiddleware",
    "pharmacypulse.middleware.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "pharmacypulse" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "pharmacypulse.context_processors.app_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}
if os.environ.get("DATABASE_URL"):
    try:
        import dj_database_url
        DATABASES["default"] = dj_database_url.parse(
            os.environ["DATABASE_URL"], conn_max_age=600, conn_health_checks=True,
        )
    except ImportError:
        pass

AUTH_USER_MODEL = "pharmacypulse.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", "English"),
    ("es", "Español"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
# Match the Benmore prod cookie name so the topbar's ppSetLang() page-reload
# pattern works seamlessly with Django's LocaleMiddleware.
LANGUAGE_COOKIE_NAME = "lang"
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "pharmacypulse" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
if __import__("importlib").util.find_spec("whitenoise"):
    STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    # 1-year HSTS; preload-eligible. Bump only after confirming no subdomain
    # ever needs to serve plain HTTP (preload is essentially permanent).
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Cache backend.
#
# Prod runs gunicorn with multiple worker PROCESSES (WEB_CONCURRENCY=4 on the
# current dyno), so a per-process LocMemCache is NOT shared: every worker keeps
# its own copy, which means the homepage aggregates (5 min), insights
# aggregates (6 h), and — worst of all — the per-IP ipapi.co geolocation lookup
# ("cached 24h") all get recomputed independently per worker. A visitor whose
# first few requests round-robin across cold workers triggers a fresh external
# geo HTTP call + full-table aggregate each time, which is the source of the
# 9-23s first-load spikes.
#
# DatabaseCache stores entries in Postgres, so all workers (and dyno restarts)
# share one warm cache. No extra addon needed. Created in the release phase via
# `manage.py createcachetable` (see Procfile). Local/dev/test (no DATABASE_URL)
# stay on LocMem so they don't need the cache table.
if os.environ.get("DATABASE_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "pp_cache",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "pp-default",
        }
    }

LOGIN_URL = "/login"
LOGIN_REDIRECT_URL = "/home"
LOGOUT_REDIRECT_URL = "/"

# Benmore: session_duration: 30d
SESSION_COOKIE_AGE = int(timedelta(days=30).total_seconds())
SESSION_COOKIE_NAME = "pp_session"
SESSION_SAVE_EVERY_REQUEST = True
# Don't drop the cookie when the browser closes — users complained that PWA /
# mobile reopens required re-login. Combined with SESSION_SAVE_EVERY_REQUEST
# this gives a rolling 30-day session.
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
# SMTP relay config (e.g. Resend at smtp.resend.com:587 with user="resend"
# and password=<resend API key>). Set DJANGO_EMAIL_BACKEND to
# "django.core.mail.backends.smtp.EmailBackend" to activate.
EMAIL_HOST          = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT          = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER     = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS       = os.environ.get("EMAIL_USE_TLS", "true").lower() in ("1", "true", "yes")
EMAIL_USE_SSL       = os.environ.get("EMAIL_USE_SSL", "false").lower() in ("1", "true", "yes")
DEFAULT_FROM_EMAIL  = os.environ.get("DEFAULT_FROM_EMAIL", "no-reply@pharmacypulse.co")
# Resend audience sync. Set both vars on the dyno to enable; unset = no-op.
RESEND_API_KEY      = os.environ.get("RESEND_API_KEY", "")
RESEND_AUDIENCE_ID  = os.environ.get("RESEND_AUDIENCE_ID", "")

# --------- PharmacyPulse-specific settings (Benmore app.yaml) ---------
BRAND = {
    "primary": "#008F82",
    "site_name": "PharmacyPulse",
    "font": "Plus Jakarta Sans",
    "description": (
        "Find, review, and compare pharmacies. Real reviews from real patients — "
        "stock confidence, wait times, and service ratings. Community-powered "
        "pharmacy transparency."
    ),
    "url": "https://pharmacypulse.co",
    "security_contact": "security@pharmacypulse.co",
    "external_id": "BEN-156",
    "csp": {
        "script_src": "https://maps.googleapis.com https://maps.gstatic.com",
        "connect_src": "https://maps.googleapis.com https://maps.gstatic.com",
        "img_src": ("https://maps.googleapis.com https://maps.gstatic.com "
                     "https://*.ggpht.com https://*.google.com https://*.gstatic.com"),
    },
}

# Benmore role → resource:action scopes (replicated verbatim)
ROLE_SCOPES = {
    "admin": "*",
    "pharmacist": (
        "pharmacies:* reviews:* pharmacy_claims:* pharmacy_hours:* "
        "pharmacy_services:* drug_shortages:read activity_log:* "
        "pharmacy_orgs:* pharmacy_team_members:* review_responses:*"
    ),
    "user": (
        "pharmacies:read reviews:* drug_shortages:read pharmacy_claims:write "
        "pharmacy_services:read pharmacy_hours:read activity_log:read"
    ),
}

# External APIs — keys come from environment. Empty string means unavailable;
# affected flows return a friendly error surface.
GOOGLE_PLACES_KEY = os.environ.get("GOOGLE_PLACES_KEY", "")
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
FACEBOOK_OAUTH_APP_ID = os.environ.get("FACEBOOK_OAUTH_APP_ID", "")
FACEBOOK_OAUTH_APP_SECRET = os.environ.get("FACEBOOK_OAUTH_APP_SECRET", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SITE_URL = os.environ.get("SITE_URL", "http://localhost:8765")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")

# Geo-block: if enabled, non-US IPs see a 451 page. Crawlers + private IPs +
# stripe-webhook + admin are whitelisted. Toggle via env var so dev/staging
# stay open and prod can be flipped without a deploy.
GEO_BLOCK_ENABLED = os.environ.get("GEO_BLOCK_ENABLED", "0") == "1"
