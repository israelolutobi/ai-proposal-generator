"""Shared, fail-closed account throttling. No passwords or raw identifiers stored."""
from datetime import timedelta
import ipaddress
import math
import unicodedata

from django.conf import settings
from django.db import DatabaseError, transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import AccountRateLimit


class RateLimited(Exception):
    def __init__(self, retry):
        self.retry = retry


def consume(scope, identifier, limit, seconds):
    key = salted_hmac("proposalq.account-rate." + scope, identifier, algorithm="sha256").hexdigest()
    with transaction.atomic():
        moment = timezone.now()
        bucket, _ = AccountRateLimit.objects.select_for_update().get_or_create(
            key=key, defaults={"expires_at": moment + timedelta(seconds=seconds)},
        )
        if bucket.expires_at <= moment:
            bucket.attempts = 0
            bucket.expires_at = moment + timedelta(seconds=seconds)
        if bucket.attempts >= limit:
            return max(1, math.ceil((bucket.expires_at - moment).total_seconds()))
        bucket.attempts += 1
        bucket.save(update_fields=["attempts", "expires_at"])
    return None


class AccountRateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if (request.method != "POST" or settings.APP_ENV != "production"
                or settings.AUTH_RATE_LIMIT_ENABLED is not True):
            return None
        name = request.resolver_match.url_name
        if name not in {"register", "login", "password_change"}:
            return None
        if name == "register" and settings.REGISTRATION_ENABLED is not True:
            return None
        if name == "password_change" and not request.user.is_authenticated:
            return None
        # Never trust user-supplied forwarding headers as an abuse identity.
        # Behind Render this is a conservative shared ingress bucket. Account
        # limits and a global signup cap still apply across ingress addresses.
        try:
            peer = str(ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")))
        except ValueError:
            peer = "unknown"
        username = str(request.user.pk) if name == "password_change" else request.POST.get("username", "")
        username = unicodedata.normalize("NFKC", username[:150]).casefold()
        if name == "register":
            budgets = [("signup-global", "all", 200, 3600), ("signup-peer", peer, 20, 3600)]
        else:
            budgets = [("auth-peer", peer, 30, 600), ("auth-account", username, 10, 600)]
        try:
            # Roll back earlier budgets if any later budget rejects. An already
            # blocked peer must not exhaust the entire service's signup budget.
            with transaction.atomic():
                for scope, identifier, limit, seconds in budgets:
                    retry = consume(scope, identifier, limit, seconds)
                    if retry:
                        raise RateLimited(retry)
        except RateLimited as error:
            response = HttpResponse("Too many account attempts. Please try again later.", status=429)
            response["Retry-After"] = str(error.retry)
            response["Cache-Control"] = "no-store"
            return response
        except DatabaseError:
            response = HttpResponse("Account access is temporarily unavailable. Please try again later.", status=503)
            response["Retry-After"] = "60"
            response["Cache-Control"] = "no-store"
            return response
        return None
