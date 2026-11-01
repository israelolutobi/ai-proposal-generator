
"""Stripe test/live configuration. Never reads or prints credential values."""
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from .configuration import boolean


API_VERSION = "2026-09-30.endive"  # stripe-python 16.0.0


def billing_configuration(environment, *, production=False):
    mode = environment.get("STRIPE_MODE", "test").strip().lower()

    if mode not in ("test", "live"):
        raise ImproperlyConfigured(
            "STRIPE_MODE must be either 'test' or 'live'."
        )

    values = {
        "BILLING_ENABLED": boolean(environment, "BILLING_ENABLED", False),
        "STRIPE_MODE": mode,
    }

    for name in (
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRO_PRICE_ID",
        "STRIPE_PORTAL_CONFIGURATION_ID",
        "BILLING_BASE_URL",
    ):
        values[name] = environment.get(name, "").strip()

    values["STRIPE_API_VERSION"] = API_VERSION

    # Checkout stays restricted unless public access is explicitly enabled.
    values["STRIPE_CHECKOUT_RESTRICTED"] = boolean(
        environment, "STRIPE_CHECKOUT_RESTRICTED", True
    )

    raw_ids = environment.get("STRIPE_CHECKOUT_ALLOWED_USER_IDS", "").strip()
    ids = [item.strip() for item in raw_ids.split(",") if item.strip()]

    if any(not item.isdecimal() or int(item) <= 0 for item in ids):
        raise ImproperlyConfigured(
            "STRIPE_CHECKOUT_ALLOWED_USER_IDS must contain positive integer IDs."
        )

    values["STRIPE_CHECKOUT_ALLOWED_USER_IDS"] = frozenset(
        int(item) for item in ids
    )

    key = values["STRIPE_SECRET_KEY"]

    # Standard and restricted Stripe API keys are supported.
    # Keys must always match the configured Stripe environment.
    allowed_prefixes = (
        f"sk_{mode}_",
        f"rk_{mode}_",
    )

    if key and not key.startswith(allowed_prefixes):
        raise ImproperlyConfigured(
            "STRIPE_SECRET_KEY does not match the configured STRIPE_MODE."
        )

    if values["BILLING_ENABLED"]:
        # A nonempty, correctly prefixed API key is required
        # whenever billing is enabled.
        if not any(
            key.startswith(prefix) and len(key) > len(prefix)
            for prefix in allowed_prefixes
        ):
            raise ImproperlyConfigured(
                f"STRIPE_SECRET_KEY must be configured for Stripe {mode} mode."
            )

        for name, prefix in (
            ("STRIPE_WEBHOOK_SECRET", "whsec_"),
            ("STRIPE_PRO_PRICE_ID", "price_"),
            ("STRIPE_PORTAL_CONFIGURATION_ID", "bpc_"),
        ):
            if (
                not values[name].startswith(prefix)
                or len(values[name]) <= len(prefix)
            ):
                raise ImproperlyConfigured(
                    f"{name} must be configured for Stripe {mode} mode."
                )

        try:
            url = urlsplit(values["BILLING_BASE_URL"])
            port = url.port
        except ValueError:
            raise ImproperlyConfigured(
                "BILLING_BASE_URL must be a valid origin."
            ) from None

        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or port == 0
            or any(
                character.isspace()
                for character in values["BILLING_BASE_URL"]
            )
            or url.path not in ("", "/")
            or url.scheme not in ("http", "https")
            or (
                url.scheme != "https"
                and (
                    production
                    or url.hostname not in ("localhost", "127.0.0.1")
                )
            )
        ):
            raise ImproperlyConfigured(
                "BILLING_BASE_URL must be an HTTPS origin "
                "(local HTTP in development only)."
            )

    values["BILLING_BASE_URL"] = values["BILLING_BASE_URL"].rstrip("/")

    return values
