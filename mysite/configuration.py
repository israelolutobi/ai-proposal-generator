"""Local configuration validation; never contacts a database or provider."""
from collections.abc import Mapping
import ipaddress
import re
from urllib.parse import urlsplit

import dj_database_url
from django.core.exceptions import ImproperlyConfigured


def environment_mode(environment):
    mode = environment.get("APP_ENV", "development").strip().lower()
    if mode not in {"development", "production"}:
        raise ImproperlyConfigured("APP_ENV must be development or production.")
    return mode


def boolean(environment, name, default):
    if name not in environment:
        return default
    value = environment[name].strip().lower()
    if value not in {"true", "false"}:
        raise ImproperlyConfigured(f"{name} must be True or False.")
    return value == "true"


def integer(environment, name, default, minimum=0, maximum=None):
    value = environment.get(name, str(default)).strip()
    if not re.fullmatch(r"[0-9]+", value):
        raise ImproperlyConfigured(f"{name} must be a nonnegative integer.")
    # Avoid unbounded integer parsing and never echo supplied configuration.
    if len(value) > 10:
        raise ImproperlyConfigured(f"{name} is outside the supported range.")
    number = int(value)
    if number < minimum or (maximum is not None and number > maximum):
        raise ImproperlyConfigured(f"{name} is outside the supported range.")
    return number


def csv_values(value):
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def valid_host(host):
    """Django host syntax: DNS names, IPv4, bracketed IPv6, optional leading dot."""
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
            return True
        except ValueError:
            return False
    name = host.removeprefix(".").removesuffix(".")
    if not name or len(name) > 253:
        return False
    if re.fullmatch(r"[0-9.]+", name):
        try:
            ipaddress.IPv4Address(name)
            return True
        except ValueError:
            return False
    return all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
               for label in name.split("."))


def trusted_origins(value, production=False):
    origins = csv_values(value)
    for origin in origins:
        try:
            parsed = urlsplit(origin)
            hostname = parsed.hostname or ""
            # Django supports explicitly trusted wildcard subdomains.
            hostname = hostname.removeprefix("*.")
            host = f"[{hostname}]" if ":" in hostname else hostname
            port = parsed.port
            valid = (
                parsed.scheme in ({"https"} if production else {"http", "https"})
                and bool(parsed.netloc) and valid_host(host)
                and not hostname.startswith(".")
                and origin.startswith(f"{parsed.scheme}://")
                and not any(character.isspace() for character in origin)
                and parsed.username is None and parsed.password is None
                and not parsed.path and not parsed.query and not parsed.fragment
                and (port is None or 1 <= port <= 65535)
                and not parsed.netloc.endswith(":")
            )
        except ValueError:
            valid = False
        if not valid:
            protocol = "HTTPS" if production else "HTTP or HTTPS"
            raise ImproperlyConfigured(
                f"CSRF_TRUSTED_ORIGINS must contain {protocol} origins without paths or credentials."
            ) from None
    return origins


def database_configuration(value, base_dir, production=False):
    url = (value or "").strip()
    if not url:
        if production:
            raise ImproperlyConfigured("DATABASE_URL is required in production; configure PostgreSQL.")
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": base_dir / "db.sqlite3"}
    try:
        database = dj_database_url.parse(url, conn_max_age=600, conn_health_checks=True)
        if urlsplit(url).fragment:
            raise ValueError
    except ValueError:
        raise ImproperlyConfigured("DATABASE_URL is invalid; check its scheme and URL syntax.") from None
    if production and (database["ENGINE"] != "django.db.backends.postgresql" or not database.get("NAME")):
        raise ImproperlyConfigured("Production DATABASE_URL must configure a named PostgreSQL database.")
    return database


def validate_hsts(seconds, include_subdomains, preload):
    if preload and (not include_subdomains or seconds < 31536000):
        raise ImproperlyConfigured("HSTS preload requires includeSubDomains and at least 31536000 seconds; approve the domain policy before enabling it.")


def validate_production(configuration):
    """Validate the effective Django settings, rather than reparsing environment."""
    def setting(name):
        return configuration[name] if isinstance(configuration, Mapping) else getattr(configuration, name)

    if setting("APP_ENV") != "production":
        return
    if setting("DEBUG"):
        raise ImproperlyConfigured("DEBUG must be False in production.")
    key = setting("SECRET_KEY")
    weak_prefixes = ("django-insecure-", "changeme", "change-me", "replace-me", "your-secret", "placeholder", "example")
    if not isinstance(key, str) or len(key.strip()) < 50 or len(set(key)) < 5 or key.lower().startswith(weak_prefixes):
        raise ImproperlyConfigured("Production SECRET_KEY must be a private random key of at least 50 characters; placeholders are not accepted.")
    database = setting("DATABASES")["default"]
    if not setting("DATABASE_URL") or database["ENGINE"] != "django.db.backends.postgresql" or not database.get("NAME"):
        raise ImproperlyConfigured("Production requires an explicit PostgreSQL DATABASE_URL.")
    hosts = setting("ALLOWED_HOSTS")
    if not hosts or any(not valid_host(host) for host in hosts):
        raise ImproperlyConfigured("Production ALLOWED_HOSTS must contain explicit valid hosts without wildcards, schemes or ports.")
    if not setting("SECURE_SSL_REDIRECT"):
        raise ImproperlyConfigured("SECURE_SSL_REDIRECT must be True in production.")
    if not setting("SESSION_COOKIE_SECURE") or not setting("CSRF_COOKIE_SECURE"):
        raise ImproperlyConfigured("Production session and CSRF cookies must require HTTPS.")
    if setting("SECURE_HSTS_SECONDS") < 1:
        raise ImproperlyConfigured("Production SECURE_HSTS_SECONDS must be positive; begin with the staged 300-second policy.")
    validate_hsts(setting("SECURE_HSTS_SECONDS"), setting("SECURE_HSTS_INCLUDE_SUBDOMAINS"), setting("SECURE_HSTS_PRELOAD"))
    expected_header = ("HTTP_X_FORWARDED_PROTO", "https") if setting("TRUST_PROXY_HEADERS") else None
    if setting("SECURE_PROXY_SSL_HEADER") != expected_header:
        raise ImproperlyConfigured("SECURE_PROXY_SSL_HEADER must match the explicit TRUST_PROXY_HEADERS policy.")
    # Recheck origins from effective settings for the deployment command too.
    trusted_origins(",".join(setting("CSRF_TRUSTED_ORIGINS")), production=True)
