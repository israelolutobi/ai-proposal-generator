"""Atomic period accounting subordinate to the AIRequest lifecycle.

Callers must first own an AIRequest write in their short transaction. Period
locks follow in (kind, UTC start, primary key) order. No provider I/O lives here.
"""
from datetime import datetime, time, timedelta, timezone as dt_timezone
import math

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import F, Q, Sum

from mysite.configuration import validate_ai_configuration
from .models import AIQuotaPeriod, AIRequest


class CoordinationError(Exception):
    """No raw database/configuration details cross the control boundary."""


class CapacityError(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


def limits():
    try:
        validate_ai_configuration(settings, production=settings.APP_ENV == "production")
    except ImproperlyConfigured:
        raise CoordinationError() from None
    return settings.AI_GLOBAL_DAILY_CREDITS, settings.AI_GLOBAL_WEEKLY_CREDITS


def period_keys(moment):
    utc_day = moment.astimezone(dt_timezone.utc).date()
    return ((AIQuotaPeriod.Kind.DAILY, utc_day),
            (AIQuotaPeriod.Kind.WEEKLY, utc_day - timedelta(days=utc_day.weekday())))


def _lock(ids):
    rows = list(AIQuotaPeriod.objects.filter(pk__in=ids).order_by(
        "kind", "period_start", "pk").select_for_update())
    if len(rows) != 2:
        raise CoordinationError()
    return rows


def lock_current(moment):
    ids = [AIQuotaPeriod.objects.get_or_create(kind=kind, period_start=start)[0].pk
           for kind, start in period_keys(moment)]
    return _lock(ids)


def matches(periods, moment):
    return tuple((row.kind, row.period_start) for row in periods) == period_keys(moment)


def reserve(row, periods, configured, moment):
    """Caller holds the active slot and both period locks until commit."""
    blocked = []
    for period, credit_limit in zip(periods, configured, strict=True):
        if period.credit_limit is None:
            period.credit_limit = credit_limit
            period.save(update_fields=["credit_limit"])
        elif period.credit_limit != credit_limit:
            raise CoordinationError()
        if period.reserved_credits + period.consumed_credits + row.quota_units > credit_limit:
            reset = datetime.combine(period.period_start, time.min, dt_timezone.utc)
            reset += timedelta(days=1 if period.kind == AIQuotaPeriod.Kind.DAILY else 7)
            blocked.append((credit_limit, reset))
    if blocked:
        retry = None if any(limit == 0 for limit, _ in blocked) else max(
            1, math.ceil((max(reset for _, reset in blocked) - moment).total_seconds()))
        raise CapacityError(retry)
    for period in periods:
        AIQuotaPeriod.objects.filter(pk=period.pk).update(reserved_credits=F("reserved_credits") + row.quota_units)
    row.global_day_period_id, row.global_week_period_id = (period.pk for period in periods)
    row.save(update_fields=["global_day_period", "global_week_period"])


def _bound(row):
    ids = (row.global_day_period_id, row.global_week_period_id)
    if not any(ids):
        if settings.APP_ENV == "production" or limits()[0] is not None:
            raise CoordinationError()
        return None
    if not all(ids) or ids[0] == ids[1]:
        raise CoordinationError()
    return ids


def finish_reservation(row, quota_state):
    """Only invoke after a winning reserved -> terminal ledger write.

Counters and that write roll back together. Deleted/missing evidence never
causes a guessed refund. Accounting still runs when AI is subsequently disabled.
"""
    ids = _bound(row)
    if ids is None:
        return False
    periods = _lock(ids)
    if not matches(periods, row.admitted_at):
        raise CoordinationError()
    for period in periods:
        changes = {"reserved_credits": F("reserved_credits") - row.quota_units}
        if quota_state == AIRequest.Quota.CONSUMED:
            changes["consumed_credits"] = F("consumed_credits") + row.quota_units
        elif quota_state != AIRequest.Quota.RELEASED:
            raise CoordinationError()
        changed = AIQuotaPeriod.objects.filter(pk=period.pk, reserved_credits__gte=row.quota_units).update(**changes)
        if changed != 1:
            raise CoordinationError()
    return True


def check_dispatch_bindings(row):
    ids = _bound(row)
    if ids is None:
        return
    periods = list(AIQuotaPeriod.objects.filter(pk__in=ids).order_by("kind", "period_start", "pk"))
    if len(periods) != 2 or not matches(periods, row.admitted_at):
        raise CoordinationError()
    configured = limits()
    if configured[0] is not None and any(period.credit_limit != limit
                                       for period, limit in zip(periods, configured, strict=True)):
        raise CoordinationError()


def status(moment):
    """Read-only, payload-free operator snapshot. Never reconciles downwards."""
    configured = limits()
    report = {"app_env": settings.APP_ENV, "ai_enabled": settings.AI_ENABLED,
              "global_enforcement": configured[0] is not None, "periods": []}
    for (kind, start), limit in zip(period_keys(moment), configured, strict=True):
        period = AIQuotaPeriod.objects.filter(kind=kind, period_start=start).first()
        reserved = period.reserved_credits if period else 0
        consumed = period.consumed_credits if period else 0
        reset = datetime.combine(start, time.min, dt_timezone.utc) + timedelta(
            days=1 if kind == AIQuotaPeriod.Kind.DAILY else 7)
        warning = None
        if period:
            field = "global_day_period" if kind == AIQuotaPeriod.Kind.DAILY else "global_week_period"
            totals = {state: AIRequest.objects.filter(**{field: period}, quota_state=state).aggregate(
                total=Sum("quota_units"))["total"] or 0 for state in (AIRequest.Quota.RESERVED, AIRequest.Quota.CONSUMED)}
            if (totals[AIRequest.Quota.RESERVED], totals[AIRequest.Quota.CONSUMED]) != (reserved, consumed):
                warning = "Counter/ledger discrepancy; inspect retained evidence. No counters were changed."
            elif limit is not None and period.credit_limit is not None and period.credit_limit != limit:
                warning = "Recorded period policy differs from this process configuration."
        report["periods"].append({"kind": kind, "period_start": str(start), "configured_limit": limit,
            "recorded_limit": period.credit_limit if period else None, "reserved": reserved,
            "consumed": consumed, "remaining": max(0, limit - reserved - consumed) if limit is not None else None,
            "reset": reset.isoformat(), "warning": warning})
    report["expired_active_requests"] = AIRequest.objects.filter(
        lifecycle__in=(AIRequest.Lifecycle.RESERVED, AIRequest.Lifecycle.IN_FLIGHT), lease_expires_at__lte=moment).count()
    report["unbound_active_requests"] = AIRequest.objects.filter(
        lifecycle__in=(AIRequest.Lifecycle.RESERVED, AIRequest.Lifecycle.IN_FLIGHT),
        ).filter(Q(global_day_period__isnull=True) | Q(global_week_period__isnull=True)).count()
    return report
