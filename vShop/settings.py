from pathlib import Path
from decouple import config, Csv
from celery.schedules import crontab

# ─── Paths ────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent

# ─── Core ────────────────────────────────────────────────────────────────────────
SECRET_KEY = config("SECRET_KEY", default="django-insecure-g2=%j((j))&yrac9_$asxx&kf(-+tvxl)4hm7uqhv((gc(b%uy")
DEBUG = config("DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())

# ─── Applications ───────────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "billing_engine",
    "frontend",
    "django_celery_beat",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "vShop.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "vShop.wsgi.application"

# ─── Database ────────────────────────────────────────────────────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# ─── Auth ────────────────────────────────────────────────────────────────────────
AUTH_USER_MODEL = "billing_engine.CustomUser"
LOGIN_URL = "frontend:login"
LOGIN_REDIRECT_URL = "frontend:index"
LOGOUT_REDIRECT_URL = "frontend:login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ─── Internationalisation ────────────────────────────────────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ─── Static & Media ──────────────────────────────────────────────────────────────────────
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "protected_media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── Redis / Cache ──────────────────────────────────────────────────────────────────────
REDIS_URL = config("REDIS_URL", default="redis://redis:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "SOCKET_CONNECT_TIMEOUT": 5,
            "SOCKET_TIMEOUT": 5,
            "IGNORE_EXCEPTIONS": True,
        },
        "KEY_PREFIX": "vshop",
    }
}

SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_CACHE_ALIAS = "default"

# ─── Celery ────────────────────────────────────────────────────────────────────────
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = config("REDIS_URL", default="redis://redis:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_SOFT_TIME_LIMIT = 300
CELERY_TASK_TIME_LIMIT = 360

# ─── Celery Beat ───────────────────────────────────────────────────────────────────────
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_BEAT_SCHEDULE = {
    "sync-edge-traffic-every-10min": {
        "task": "billing_engine.tasks.sync_all_edge_traffic",
        "schedule": crontab(minute="*/10"),
        "options": {"expires": 540},
    },
}

# ─── Protected Media ─────────────────────────────────────────────────────────────────────
PROTECTED_MEDIA_ROOT = BASE_DIR / "protected_media"

# ─── Logging ────────────────────────────────────────────────────────────────────────
# Creates two log files in BASE_DIR/logs/:
#   xui_topup.log  — every XUI_ prefixed line from xui_client.py (DEBUG+)
#   django.log     — general WARNING+ from all other Django/app loggers
# Both also print to console (stdout) so docker logs picks them up.
_LOGS_DIR = BASE_DIR / "logs"
_LOGS_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
    },
    "handlers": {
        # Prints to stdout — captured by `docker logs` or gunicorn
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        # Dedicated log for every 3x-ui API interaction (XUI_ prefixed lines)
        "xui_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(_LOGS_DIR / "xui_topup.log"),
            "maxBytes": 10 * 1024 * 1024,  # 10 MB
            "backupCount": 5,
            "formatter": "verbose",
        },
        # General application log
        "app_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(_LOGS_DIR / "django.log"),
            "maxBytes": 10 * 1024 * 1024,  # 10 MB
            "backupCount": 5,
            "formatter": "verbose",
        },
    },
    "loggers": {
        # Captures all XUI_ lines from xui_client.py at DEBUG level
        "billing_engine.xui_client": {
            "handlers": ["console", "xui_file"],
            "level": "DEBUG",
            "propagate": False,
        },
        # Captures top-up flow logs from views.py
        "frontend.views": {
            "handlers": ["console", "xui_file"],
            "level": "DEBUG",
            "propagate": False,
        },
        # General app-level logger
        "billing_engine": {
            "handlers": ["console", "app_file"],
            "level": "WARNING",
            "propagate": False,
        },
        # Django internals
        "django": {
            "handlers": ["console", "app_file"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
