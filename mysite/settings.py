"""
Django settings for mysite project.
"""

from pathlib import Path
import os

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from mysite.configuration import (
    ai_configuration, boolean, csv_values, database_configuration, environment_mode, integer,
    trusted_origins, validate_hsts, validate_production,
)
from mysite.runtime import gunicorn_configuration


load_dotenv()


BASE_DIR = Path(__file__).resolve().parent.parent


# SECURITY SETTINGS

APP_ENV = environment_mode(os.environ)
IS_PRODUCTION = APP_ENV == "production"
globals().update(ai_configuration(os.environ, production=IS_PRODUCTION))
REGISTRATION_ENABLED = boolean(os.environ, "REGISTRATION_ENABLED", not IS_PRODUCTION)

SECRET_KEY = os.getenv("SECRET_KEY")

if not SECRET_KEY or not SECRET_KEY.strip():
    raise ImproperlyConfigured(
        "SECRET_KEY must be set in the environment or local .env file."
    )

DEBUG = boolean(os.environ, "DEBUG", False)

ALLOWED_HOSTS = csv_values(os.getenv("ALLOWED_HOSTS", "" if IS_PRODUCTION else "localhost,127.0.0.1"))

CSRF_TRUSTED_ORIGINS = trusted_origins(os.getenv("CSRF_TRUSTED_ORIGINS", ""), production=IS_PRODUCTION)


# APPLICATIONS

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    "proposal_ai",
]


# MIDDLEWARE

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


ROOT_URLCONF = "mysite.urls"


# TEMPLATES

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            BASE_DIR / "templates",
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "mysite.context_processors.registration_policy",
            ],
        },
    },
]


WSGI_APPLICATION = "mysite.wsgi.application"


# DATABASE

DATABASE_URL = os.getenv("DATABASE_URL")

DATABASES = {"default": database_configuration(DATABASE_URL, BASE_DIR, production=IS_PRODUCTION)}


# PASSWORD VALIDATION

LOGIN_URL = "login"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# INTERNATIONALIZATION

LANGUAGE_CODE = "en-us"

TIME_ZONE = "Europe/London"

USE_I18N = True

USE_TZ = True


# STATIC FILES

STATIC_URL = "/static/"

STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}


# HTTPS / PROXY SETTINGS

TRUST_PROXY_HEADERS = boolean(os.environ, "TRUST_PROXY_HEADERS", False)
if TRUST_PROXY_HEADERS and not IS_PRODUCTION:
    raise ImproperlyConfigured("TRUST_PROXY_HEADERS is supported only in explicit production mode.")
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if TRUST_PROXY_HEADERS else None

SECURE_SSL_REDIRECT = boolean(os.environ, "SECURE_SSL_REDIRECT", IS_PRODUCTION)
SECURE_HSTS_SECONDS = integer(os.environ, "SECURE_HSTS_SECONDS", 300 if IS_PRODUCTION else 0, maximum=2**31 - 1)
SECURE_HSTS_INCLUDE_SUBDOMAINS = boolean(os.environ, "SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = boolean(os.environ, "SECURE_HSTS_PRELOAD", False)
validate_hsts(SECURE_HSTS_SECONDS, SECURE_HSTS_INCLUDE_SUBDOMAINS, SECURE_HSTS_PRELOAD)

SESSION_COOKIE_SECURE = IS_PRODUCTION or not DEBUG
CSRF_COOKIE_SECURE = IS_PRODUCTION or not DEBUG

# This is the same configuration consumed by gunicorn.conf.py, without I/O.
DEPLOYMENT_RUNTIME = gunicorn_configuration(os.environ)
validate_production(globals())


# DEFAULT PRIMARY KEY FIELD

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Ordinary Django tests must never spend API credit or access external services.
TEST_RUNNER = "mysite.test_runner.NoNetworkDiscoverRunner"
