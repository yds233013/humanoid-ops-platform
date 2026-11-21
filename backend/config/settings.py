"""Settings. Everything environment-specific comes from environment variables (12-factor).

Two database identities:
  * DB_OWNER_USER  - owns the schema, runs migrations (never used by the running app)
  * DB_APP_USER    - runtime role: no DDL, no UPDATE/DELETE on audit, cannot disable triggers
DJANGO_DB_IDENTITY selects which one this process connects as ("owner" or "app").
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]
if not DEBUG:  # behind the host's HTTPS proxy (hosted demo)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "rest_framework",
    "platform_core",
    "accounts",
    "fleet",
    "planning",
    "readiness",
    "execution",
    "operations",
    "capacity",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "platform_core.middleware.RequestIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/fleet/"

DB_APP_ROLE = env("DB_APP_USER", "ops_app")
_identity = env("DJANGO_DB_IDENTITY", "app")
_user = env("DB_OWNER_USER", "ops_owner") if _identity == "owner" else DB_APP_ROLE
_password = (
    env("DB_OWNER_PASSWORD", "ops_owner_dev")
    if _identity == "owner"
    else env("DB_APP_PASSWORD", "ops_app_dev")
)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "humanoid_ops"),
        "USER": _user,
        "PASSWORD": _password,
        "HOST": env("DB_HOST", "localhost"),
        "PORT": env("DB_PORT", "5440"),
        "ATOMIC_REQUESTS": False,
        "OPTIONS": {"options": "-c lock_timeout=3000 -c statement_timeout=10000"},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_PASSWORD_VALIDATORS = [{"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"}]

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_AGE = 12 * 60 * 60

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Los_Angeles"
USE_TZ = True

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "EXCEPTION_HANDLER": "platform_core.api.exception_handler",
    "UNAUTHENTICATED_USER": None,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
