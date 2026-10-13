from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(DEBUG=(bool, False))
# Loads .env into os.environ too, so libpq sees PGHOST and friends.
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", ".localhost"])

CLOUDINARY_URL = env("CLOUDINARY_URL", default="")
CLOUDINARY_ENV_PREFIX = env("CLOUDINARY_ENV_PREFIX", default="dev")
SENTRY_DSN = env("SENTRY_DSN", default="")
REDIS_URL = env("REDIS_URL", default="")
PLATFORM_DOMAIN = env("PLATFORM_DOMAIN", default="localhost")
PLATFORM_URL_TEMPLATE = env("PLATFORM_URL_TEMPLATE", default="http://{subdomain}.localhost:8000")

SHARED_APPS = [
    # apps.tenancy must precede django_tenants: its migrate_schemas command overrides the stock one.
    "apps.tenancy",
    "django_tenants",
    "apps.core",
    # accounts is shared too: AUTH_USER_MODEL must exist in the public schema
    # (auth/admin tables there reference it) and in every school schema.
    "apps.accounts",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
]

TENANT_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "apps.accounts",
    "apps.audit",
    "apps.files",
    "apps.branding",
    "apps.academics",
    "apps.students",
    "apps.imports",
    "apps.results",
    "apps.fees",
    "django_otp",
    "django_otp.plugins.otp_totp",
]

INSTALLED_APPS = list(SHARED_APPS) + [app for app in TENANT_APPS if app not in SHARED_APPS]

# Header carrying the real client IP behind a proxy, e.g. "HTTP_CF_CONNECTING_IP".
# Required in production behind Cloudflare or a proxy, e.g. HTTP_CF_CONNECTING_IP.
CLIENT_IP_HEADER: str | None = env("CLIENT_IP_HEADER", default=None)
# Anti-spraying limit: failed logins per client IP per 900 s (per identifier it is 5).
LOGIN_IP_FAILURE_LIMIT = 50
# Student admission numbers are guessable: the tight 5/900 s limit is per (identifier, IP);
# this looser per-identifier limit (across all IPs) caps distributed guessing.
STUDENT_IDENTIFIER_FAILURE_LIMIT = 20
# Platform-wide cap on valid self-service signups per 24 h (a WARNING is logged when hit).
SIGNUP_GLOBAL_DAILY_LIMIT = env.int("SIGNUP_GLOBAL_DAILY_LIMIT", default=50)

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.IdentifierBackend"]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

TENANT_MODEL = "tenancy.School"
TENANT_DOMAIN_MODEL = "tenancy.Domain"
PUBLIC_SCHEMA_URLCONF = "config.urls_public"
ROOT_URLCONF = "config.urls_tenant"
SHOW_PUBLIC_IF_NO_TENANT_FOUND = True

MIDDLEWARE = [
    "apps.tenancy.middleware.HostTenantMiddleware",
    "apps.core.request_id.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "apps.accounts.middleware.StaffIdleTimeoutMiddleware",
    "apps.accounts.middleware.MfaEnforcementMiddleware",
    "apps.accounts.middleware.ForcePasswordChangeMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_AGE = 14 * 24 * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
if env.bool("SECURE_PROXY_SSL", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
STAFF_IDLE_TIMEOUT_SECONDS = 1800
# Our own per-user limiter (apps/accounts/views/mfa.py) guards TOTP guessing; turn off
# django-otp's per-device delay so a typo does not lock out a correct code for seconds.
OTP_TOTP_THROTTLE_FACTOR = 0
MFA_PENDING_SECONDS = 300
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "home"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.branding.context_processors.branding",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["ENGINE"] = "django_tenants.postgresql_backend"
DATABASE_ROUTERS = ["django_tenants.routers.TenantSyncRouter"]

if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_FUNCTION": "django_tenants.cache.make_key",
        }
    }
else:
    # Local development without Redis.
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            # Prefixes every key with the active schema name (§5.6).
            "KEY_FUNCTION": "django_tenants.cache.make_key",
        }
    }

# Celery (§8.1). Without Redis there is no worker, so local dev runs tasks inline.
CELERY_BROKER_URL = REDIS_URL or "memory://"
CELERY_RESULT_BACKEND = REDIS_URL or "cache+memory://"
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TIMEZONE = "Africa/Lagos"
CELERY_BEAT_SCHEDULE = {
    "fees-reverify": {
        "task": "apps.fees.tasks.reverify_all_schools",
        "schedule": 900.0,
        "options": {"queue": "payments"},
    },
}
# Workers keep Django's JSON LOGGING (see the setup_logging receiver in config/celery.py).
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
if not DEBUG and not REDIS_URL:
    # Without a broker every task would run inline in the web request (eager mode), silently.
    raise ImproperlyConfigured("REDIS_URL is required when DEBUG is off (Celery broker and cache).")
if not REDIS_URL:
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True

# Staff invite links expire after 72 hours (also the password-reset token lifetime).
PASSWORD_RESET_TIMEOUT = 259200

if DEBUG:
    EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
else:
    EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_TIMEOUT = 10
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@build2learn.local")

# Dotted path of the SMS backend. Console in development; unset in production until the
# real provider lands (P5), when send_sms raises ImproperlyConfigured.
PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY", default="")
PAYMENT_GATEWAY = env(
    "PAYMENT_GATEWAY",
    default="apps.fees.gateways.fake.FakeGateway"
    if DEBUG
    else "apps.fees.gateways.paystack.PaystackGateway",
)
PAYMENT_PLACEHOLDER_EMAIL_DOMAIN = env("PAYMENT_PLACEHOLDER_EMAIL_DOMAIN", default="build2learn.ng")
SMS_BACKEND = env("SMS_BACKEND", default="apps.core.sms.ConsoleSmsBackend" if DEBUG else None)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

FORMAT_MODULE_PATH = ["config.formats"]
DATE_FORMAT = "d/m/Y"
DATE_INPUT_FORMATS = ["%d/%m/%Y", "%Y-%m-%d"]

PRIVATE_URL_TTL_SECONDS = 600

STORAGES = {
    # Child-related files: Cloudinary type="authenticated" (not "private": derived versions
    # of private assets are public), served via short-lived signed links.
    "default": {
        "BACKEND": "apps.core.storage.TenantCloudinaryStorage",
        "OPTIONS": {"delivery_type": "authenticated"},
    },
    # School branding only (logos): plain public delivery.
    "public": {
        "BACKEND": "apps.core.storage.TenantCloudinaryStorage",
        "OPTIONS": {"delivery_type": "upload"},
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "EXCEPTION_HANDLER": "apps.core.errors.api_exception_handler",
}

LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "context": {"()": "apps.core.logging.ContextFilter"},
        "redact": {"()": "apps.core.logging.RedactFilter"},
    },
    "formatters": {
        "json": {
            "()": "apps.core.logging.JsonFormatter",
            "format": (
                "%(asctime)s %(levelname)s %(name)s %(message)s "
                "%(request_id)s %(schema)s %(user_id)s %(role)s"
            ),
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["context", "redact"],
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        # httpx logs every request URL at INFO; Paystack URLs carry account numbers and
        # payment references (bank resolve, verify). Keep only warnings and errors.
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
    },
}


def _sentry_before_send(event, hint):
    from apps.core.request_id import request_id_var, schema_name_var

    tags = event.setdefault("tags", {})
    tags["schema"] = schema_name_var.get()
    tags["request_id"] = request_id_var.get()
    # Request bodies, query strings and cookies can carry children's data (DOB,
    # guardian contacts, search terms) and passwords: never send them.
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("data", "query_string", "cookies"):
            request.pop(key, None)
    return event


if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        send_default_pii=False,
        # Frame locals would carry children's data (DOB, guardian contacts) into events.
        include_local_variables=False,
        max_request_body_size="never",
        before_send=_sentry_before_send,
    )
