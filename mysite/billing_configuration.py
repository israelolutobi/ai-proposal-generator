"""Stripe test-mode configuration. Never reads or prints credential values."""
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from .configuration import boolean


API_VERSION = "2026-09-30.endive"  # stripe-python 16.0.0


def billing_configuration(environment, *, production=False):
    values = {"BILLING_ENABLED": boolean(environment, "BILLING_ENABLED", False)}
    for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRO_PRICE_ID",
                 "STRIPE_PORTAL_CONFIGURATION_ID", "BILLING_BASE_URL"):
        values[name] = environment.get(name, "").strip()
    values["STRIPE_API_VERSION"] = API_VERSION
    key = values["STRIPE_SECRET_KEY"]
    if key and not key.startswith("sk_test_"):
        raise ImproperlyConfigured("STRIPE_SECRET_KEY must be a test-mode secret key.")
    if values["BILLING_ENABLED"]:
        for name, prefix in (("STRIPE_SECRET_KEY", "sk_test_"), ("STRIPE_WEBHOOK_SECRET", "whsec_"),
                             ("STRIPE_PRO_PRICE_ID", "price_"), ("STRIPE_PORTAL_CONFIGURATION_ID", "bpc_")):
            if not values[name].startswith(prefix) or len(values[name]) <= len(prefix):
                raise ImproperlyConfigured(f"{name} must be configured for Stripe test mode.")
        try:
            url = urlsplit(values["BILLING_BASE_URL"])
            port = url.port
        except ValueError:
            raise ImproperlyConfigured("BILLING_BASE_URL must be a valid origin.") from None
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or port == 0 or any(character.isspace() for character in values["BILLING_BASE_URL"])
                or url.path not in ("", "/") or url.scheme not in ("http", "https")
                or (url.scheme != "https" and (production or url.hostname not in ("localhost", "127.0.0.1")))):
            raise ImproperlyConfigured("BILLING_BASE_URL must be an HTTPS origin (local HTTP in development only).")
    values["BILLING_BASE_URL"] = values["BILLING_BASE_URL"].rstrip("/")
    return values
