"""Authenticated CSRF-protected billing actions and one signature-only webhook."""
import stripe
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST, require_safe

from . import billing, subscriptions


def _pricing(request, *, error=None, status=200, returned=False):
    context = {"billing_enabled": settings.BILLING_ENABLED, "billing_error": error, "checkout_returned": returned}
    if request.user.is_authenticated:
        try:
            current = subscriptions.usage(request.user)
        except DatabaseError:
            return HttpResponse("Billing is temporarily unavailable. Please try again later.", status=503)
        account = current["account"]
        context.update(usage=current, billing_account=account,
                       can_manage=bool(account and account.stripe_customer_id and account.status != "customer_deleted"))
    return render(request, "pricing.html", context, status=status)


@never_cache
@require_safe
def pricing(request):
    return _pricing(request)


def _action(request, function):
    try:
        return redirect(function(request.user))
    except billing.BillingError as error:
        return _pricing(request, error=error.user_message, status=error.status)
    except (stripe.StripeError, DatabaseError, ValueError):
        return _pricing(request, error="Billing is temporarily unavailable. Please try again later.", status=503)


@never_cache
@login_required
@require_POST
@sensitive_variables()
def checkout(request):
    return _action(request, billing.checkout)


@never_cache
@login_required
@require_POST
@sensitive_variables()
def portal(request):
    return _action(request, billing.portal)


@never_cache
@login_required
@require_GET
def checkout_return(request):
    return _pricing(request, returned=True)


@csrf_exempt
@never_cache
@require_POST
@sensitive_variables()
def webhook(request):
    if not settings.BILLING_ENABLED:
        return HttpResponse("Billing unavailable.", status=503)
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > 1_000_000:
            return HttpResponse(status=413)
        body = request.body
        if len(body) > 1_000_000:
            return HttpResponse(status=413)
        event = stripe.Webhook.construct_event(body, request.headers.get("Stripe-Signature", ""),
                                               settings.STRIPE_WEBHOOK_SECRET, tolerance=300)
    except (ValueError, stripe.SignatureVerificationError, RequestDataTooBig):
        return HttpResponse("Invalid webhook.", status=400)
    try:
        billing.process_event(event)
    except (ValueError, KeyError, TypeError):
        return HttpResponse("Invalid event.", status=400)
    except (billing.BillingError, stripe.StripeError, DatabaseError):
        # No event receipt is committed on failure; Stripe must retry delivery.
        return HttpResponse("Webhook temporarily unavailable.", status=503)
    return HttpResponse(status=200)
