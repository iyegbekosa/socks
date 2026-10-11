from pathlib import Path

import environ

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

SHARED_APPS = [
    "django_tenants",
    "apps.tenancy",
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
]

INSTALLED_APPS = list(SHARED_APPS) + [app for app in TENANT_APPS if app not in SHARED_APPS]

AUTH_USER_MODEL = "accounts.User"

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
    "django.contrib.messages.middleware.MessageMiddleware",
]

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

# Celery (§8.1). Eager mode is for tests only; never the default.
CELERY_BROKER_URL = REDIS_URL or "memory://"
CELERY_RESULT_BACKEND = REDIS_URL or "cache+memory://"
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TIMEZONE = "Africa/Lagos"

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

DATE_FORMAT = "d/m/Y"
DATE_INPUT_FORMATS = ["%d/%m/%Y", "%Y-%m-%d"]

PRIVATE_URL_TTL_SECONDS = 600

STORAGES = {
    # Child-related files: Cloudinary type="private", served via short-lived signed links.
    "default": {
        "BACKEND": "apps.core.storage.TenantCloudinaryStorage",
        "OPTIONS": {"delivery_type": "private"},
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
}


def _sentry_before_send(event, hint):
    from apps.core.request_id import request_id_var, schema_name_var

    tags = event.setdefault("tags", {})
    tags["schema"] = schema_name_var.get()
    tags["request_id"] = request_id_var.get()
    return event


if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(dsn=SENTRY_DSN, send_default_pii=False, before_send=_sentry_before_send)
