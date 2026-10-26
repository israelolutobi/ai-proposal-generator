
"""Stripe test-mode boundary. No client-supplied customer, price or return URLs."""
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlsplit

import stripe
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from .models import BillingAccount, CheckoutAttempt, StripeEvent


TERMINAL = {"canceled", "incomplete_expired"}
EVENT_TYPES = {
    "checkout.session.completed", "checkout.session.expired",
    "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed",
    "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
    "customer.subscription.paused", "customer.subscription.resumed", "customer.deleted",
    "invoice.paid", "invoice.payment_succeeded", "invoice.payment_failed",
    "invoice.payment_action_required", "invoice.finalization_failed", "invoice.marked_uncollectible", "invoice.voided",
}


class BillingError(Exception):
    def __init__(self, message="Billing is temporarily unavailable. Please try again later.", status=503):
        self.user_message = message
        self.status = status
        super().__init__(message)


def client():
    if not settings.BILLING_ENABLED or not settings.STRIPE_SECRET_KEY.startswith("sk_test_"):
        raise BillingError("Test billing is not available yet. Your current account remains available.")
    return stripe.StripeClient(settings.STRIPE_SECRET_KEY, stripe_version=settings.STRIPE_API_VERSION,
                               max_network_retries=0, http_client=stripe.RequestsClient(timeout=8))


def identifier(value):
    return value.get("id") if isinstance(value, dict) else value


def require_test(obj):
    obj = data(obj)
    if not isinstance(obj, dict) or obj.get("livemode") is not False:
        raise BillingError()
    return obj


def data(obj):
    # stripe-python 16 resources deliberately no longer inherit from dict.
    return obj.to_dict() if isinstance(obj, stripe.StripeObject) else obj


def hosted_url(value, host):
    parsed = urlsplit(value or "")
    if parsed.scheme != "https" or parsed.hostname != host or parsed.username or parsed.password or parsed.port:
        raise BillingError()
    return value


def valid_price(price):
    recurring = price.get("recurring") or {}
    return (price.get("id") == settings.STRIPE_PRO_PRICE_ID and price.get("livemode") is False
            and price.get("currency") == "gbp" and price.get("unit_amount") == 999
            and price.get("billing_scheme") == "per_unit" and not price.get("transform_quantity")
            and recurring.get("interval") == "month" and recurring.get("interval_count") == 1
            and recurring.get("usage_type") == "licensed")


def validate_catalog(api):
    price = require_test(api.v1.prices.retrieve(settings.STRIPE_PRO_PRICE_ID))
    if not valid_price(price) or not price.get("active") or price.get("tax_behavior") != "inclusive":
        raise BillingError("The Pro test price is not configured correctly. Please contact support.")
    portal = require_test(api.v1.billing_portal.configurations.retrieve(settings.STRIPE_PORTAL_CONFIGURATION_ID))
    features = portal.get("features") or {}
    cancel = features.get("subscription_cancel") or {}
    if (not portal.get("active") or cancel.get("enabled") is not True or cancel.get("mode") != "at_period_end"
            or (features.get("subscription_update") or {}).get("enabled") is True
            or (features.get("payment_method_update") or {}).get("enabled") is not True
            or (features.get("invoice_history") or {}).get("enabled") is not True):
        raise BillingError("The test billing portal is not configured correctly. Please contact support.")


def _lock(account_id):
    # Write-first also serializes billing on development SQLite.
    BillingAccount.objects.filter(pk=account_id).update(updated_at=F("updated_at"))
    return BillingAccount.objects.select_for_update().get(pk=account_id)


def _subscriptions(api, account):
    result = api.v1.subscriptions.list({"customer": account.stripe_customer_id, "status": "all", "limit": 100,
                                        "expand": ["data.latest_invoice"]})
    rows = [require_test(row) for row in result.auto_paging_iter()]
    for row in rows:
        if identifier(row.get("customer")) != account.stripe_customer_id:
            raise BillingError()
    return rows


def checkout(user):
    api = client()
    # Persist identities BEFORE remote mutations. A rollback or timeout cannot
    # invent a fresh idempotency key on the next request.
    account, _ = BillingAccount.objects.get_or_create(user=user)
    with transaction.atomic():
        account = _lock(account.pk)
        if account.status == "customer_deleted":
            raise BillingError("This billing account needs support before another checkout.", 409)
        if not account.stripe_customer_id:
            customer = require_test(api.v1.customers.create(
                {"metadata": {"proposalq_account": str(account.identity)}},
                options={"idempotency_key": f"proposalq-customer-{account.identity}"},
            ))
            account.stripe_customer_id = customer["id"]
            account.save(update_fields=["stripe_customer_id", "updated_at"])
    with transaction.atomic():
        account = _lock(account.pk)
        attempt, _ = CheckoutAttempt.objects.get_or_create(account=account, resolved=False)
    # The persisted attempt survives every error below. Keep the account lock
    # through Stripe I/O so competing clicks and webhook reconciliation serialize.
    # No AI ledger/global-period lock is acquired here.
    expired = False
    with transaction.atomic():
        account = _lock(account.pk)
        attempt = CheckoutAttempt.objects.get(pk=attempt.pk)
        if attempt.resolved:
            raise BillingError("Checkout has already finished. Refresh Pricing & billing.", 409)
        if any(row.get("status") not in TERMINAL for row in _subscriptions(api, account)):
            raise BillingError("You already have a subscription. Use Manage billing to update or cancel it.", 409)
        validate_catalog(api)
        if attempt.stripe_session_id:
            session = api.v1.checkout.sessions.retrieve(attempt.stripe_session_id)
        else:
            if timezone.now() - attempt.created_at >= timedelta(hours=23):
                raise BillingError("An earlier checkout needs reconciliation. Please contact support.", 409)
            session = api.v1.checkout.sessions.create({
                "mode": "subscription", "customer": account.stripe_customer_id,
                "client_reference_id": str(account.identity),
                "line_items": [{"price": settings.STRIPE_PRO_PRICE_ID, "quantity": 1}],
                "subscription_data": {"metadata": {"proposalq_account": str(account.identity)}},
                "metadata": {"proposalq_attempt": str(attempt.identity)},
                "success_url": settings.BILLING_BASE_URL + reverse("billing_return"),
                "cancel_url": settings.BILLING_BASE_URL + reverse("pricing") + "?checkout=cancelled",
                "allow_promotion_codes": False, "automatic_tax": {"enabled": False},
            }, options={"idempotency_key": f"proposalq-checkout-{attempt.identity}"})
        session = require_test(session)
        if (identifier(session.get("customer")) != account.stripe_customer_id or session.get("mode") != "subscription"
                or (session.get("metadata") or {}).get("proposalq_attempt") != str(attempt.identity)):
            raise BillingError()
        attempt.stripe_session_id = session["id"]
        if session.get("status") == "expired":
            attempt.resolved = True
            expired = True
        elif session.get("status") != "open":
            raise BillingError("Checkout is being confirmed. Refresh Pricing & billing in a moment.", 409)
        attempt.save(update_fields=["stripe_session_id", "resolved"])
        url = None if expired else hosted_url(session.get("url"), "checkout.stripe.com")
    if expired:
        raise BillingError("Your previous checkout expired. Select Upgrade again to start a new one.", 409)
    return url


def portal(user):
    api = client()
    account = BillingAccount.objects.filter(user=user).first()
    if not account or not account.stripe_customer_id or account.status == "customer_deleted":
        raise BillingError("There is no billing account to manage yet.", 409)
    session = api.v1.billing_portal.sessions.create({
        "customer": account.stripe_customer_id, "configuration": settings.STRIPE_PORTAL_CONFIGURATION_ID,
        "return_url": settings.BILLING_BASE_URL + reverse("pricing"),
    })
    return hosted_url(data(session).get("url"), "billing.stripe.com")


def _timestamp(value):
    if type(value) is not int:
        return None
    return datetime.fromtimestamp(value, dt_timezone.utc)


def synchronize(api, account):
    """Fetch current Stripe state UNDER account lock; old events cannot roll back it."""
    rows = [row for row in _subscriptions(api, account)
            if (row.get("metadata") or {}).get("proposalq_account") == str(account.identity)]
    ongoing = [row for row in rows if row.get("status") not in TERMINAL]
    account.paid = False
    account.price_id = ""
    account.period_start = account.period_end = None
    account.cancel_at_period_end = False
    account.stripe_subscription_id = None
    account.status = "none"
    if len(ongoing) > 1:
        account.status = "conflict"
    elif rows:
        row = ongoing[0] if ongoing else max(rows, key=lambda value: value.get("created", 0))
        account.stripe_subscription_id = row["id"]
        account.status = row.get("status", "unknown")

        # Recognise both Stripe cancellation representations:
        # - cancel_at_period_end=True
        # - cancel_at set to the subscription's period-end timestamp
        # Keep the flag False for an already terminated subscription.
        items = (row.get("items") or {}).get("data") or []
        scheduled_at = _timestamp(row.get("cancel_at"))
        period_end = (
            _timestamp(items[0].get("current_period_end"))
            if len(items) == 1 else None
        )
        account.cancel_at_period_end = (
            row.get("status") not in TERMINAL
            and (
                row.get("cancel_at_period_end") is True
                or (
                    scheduled_at is not None
                    and period_end is not None
                    and scheduled_at == period_end
                )
            )
        )

        if len(items) == 1 and not (row.get("items") or {}).get("has_more"):
            item = items[0]
            price = item.get("price") or {}
            invoice = row.get("latest_invoice") or {}
            account.price_id = identifier(price) or ""
            account.period_start = _timestamp(item.get("current_period_start"))
            account.period_end = _timestamp(item.get("current_period_end"))
            account.paid = bool(valid_price(price) and item.get("quantity") == 1
                                and not row.get("pause_collection") and isinstance(invoice, dict)
                                and invoice.get("status") == "paid" and invoice.get("livemode") is False
                                and identifier(invoice.get("customer")) == account.stripe_customer_id)
    account.synced_at = timezone.now()
    account.save()
    return account


def process_event(event):
    event = require_test(event)
    event_id, event_type = event.get("id"), event.get("type")
    if not isinstance(event_id, str) or not event_id.startswith("evt_") or len(event_id) > 255:
        raise ValueError("Invalid event identity.")
    if not isinstance(event_type, str) or len(event_type) > 100:
        raise ValueError("Invalid event type.")
    if StripeEvent.objects.filter(event_id=event_id).exists():
        return
    if event_type not in EVENT_TYPES:
        return
    obj = event["data"]["object"]
    customer_id = obj.get("id") if event_type == "customer.deleted" else identifier(obj.get("customer"))
    account_id = BillingAccount.objects.filter(stripe_customer_id=customer_id).values_list("pk", flat=True).first() if customer_id else None
    with transaction.atomic():
        account = _lock(account_id) if account_id else None
        if StripeEvent.objects.filter(event_id=event_id).exists():
            return
        if account:
            require_test(obj)
            if event_type == "customer.deleted":
                account.paid = False
                account.status = "customer_deleted"
                account.synced_at = timezone.now()
                account.save(update_fields=["paid", "status", "synced_at", "updated_at"])
            else:
                synchronize(client(), account)
            # Only resolve an owned, known session. A success redirect does nothing.
            if event_type.startswith("checkout.session.") and obj.get("status") in ("complete", "expired"):
                CheckoutAttempt.objects.filter(account=account, stripe_session_id=obj.get("id")).update(resolved=True)
        StripeEvent.objects.create(event_id=event_id, event_type=event_type, outcome="updated" if account else "ignored")


def reconcile(account_id):
    """Operator recovery using authenticated test Stripe API, never browser data."""
    api = client()
    with transaction.atomic():
        account = _lock(account_id)
        if not account.stripe_customer_id or account.status == "customer_deleted":
            raise BillingError("This account has no recoverable Stripe test customer.", 409)
        synchronize(api, account)
        attempts = CheckoutAttempt.objects.filter(account=account, resolved=False)
        for attempt in attempts:
            sessions = api.v1.checkout.sessions.list({"customer": account.stripe_customer_id, "limit": 100})
            matches = [data(row) for row in sessions.auto_paging_iter()
                       if (data(row).get("metadata") or {}).get("proposalq_attempt") == str(attempt.identity)]
            if len(matches) > 1:
                raise BillingError("Multiple sessions require manual review in Stripe.", 409)
            if matches:
                session = require_test(matches[0])
                attempt.stripe_session_id = session["id"]
                attempt.resolved = session.get("status") in ("complete", "expired")
                attempt.save(update_fields=["stripe_session_id", "resolved"])
        return account
