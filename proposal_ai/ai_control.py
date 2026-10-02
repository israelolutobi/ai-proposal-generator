"""Database-authoritative Beta admission. Never stores AI payloads or calls SDKs.

Transactions start with a write, including on SQLite. The partial unique active
slot serializes same-account admission on a shared database. Provider I/O is
outside these transactions; success claims the still-live request before any
application writes, within the same transaction.
"""
from dataclasses import dataclass
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal
import json
import math
import uuid

from django.core import signing
from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from . import ai_global, services
from .models import AIRequest, JobPost, Proposal


Operation = AIRequest.Operation
Intent = AIRequest.Intent
Lifecycle = AIRequest.Lifecycle
Quota = AIRequest.Quota
Failure = AIRequest.Failure
ACTIVE = (Lifecycle.RESERVED, Lifecycle.IN_FLIGHT)
CREDITS = {Operation.PROFILE_SUMMARY: 1, Operation.JOB_EXTRACTION: 2, Operation.PROPOSAL_GENERATION: 3}
BURSTS = {
    Operation.PROFILE_SUMMARY: (3, timedelta(minutes=10)),
    Operation.JOB_EXTRACTION: (3, timedelta(minutes=5)),
    Operation.PROPOSAL_GENERATION: (2, timedelta(minutes=5)),
}
DAILY_CREDITS = 25
WEEKLY_CREDITS = 100
NONCE_MAX_AGE = 24 * 60 * 60
LEASE = timedelta(minutes=10)
NONCE_SALT = "proposalq.ai-request.v1"
ADMISSION_RESTARTS = 3
STALE_RECOVERY_BATCH = 50


class _RestartAdmission(Exception):
    pass


class _ExpiredDuringAccounting(Exception):
    pass


def now():
    return timezone.now()


class ControlError(Exception):
    def __init__(self, message, status=409, *, retry_after=None, metadata=None):
        super().__init__(message)
        self.user_message = message
        self.status = status
        self.retry_after = retry_after
        self.metadata = metadata or {}


def unavailable():
    return ControlError("ProposalQ request controls are temporarily unavailable. Please try again later.", 503)


def _require_enabled():
    if getattr(settings, "AI_ENABLED", False) is not True:
        raise ControlError(services.AIDisabledError.user_message, 503)


def issue_nonce(user, operation, resource_id=None, intent=Intent.GENERATE):
    if operation not in CREDITS or intent not in Intent.values:
        raise ValueError("Invalid AI operation or intent.")
    return signing.dumps({"user": user.pk, "operation": operation, "resource": resource_id,
                          "intent": intent, "nonce": str(uuid.uuid4())}, salt=NONCE_SALT)


def validate_nonce(token, user, operation, resource_id=None):
    try:
        if not isinstance(token, str) or len(token) > 2048:
            raise ValueError
        data = signing.loads(token, salt=NONCE_SALT, max_age=NONCE_MAX_AGE)
        identity = uuid.UUID(data["nonce"])
        if (identity.version != 4 or data["user"] != user.pk or data["operation"] != operation
                or data["resource"] != resource_id or data["intent"] not in Intent.values):
            raise ValueError
        return identity, data["intent"]
    except (signing.BadSignature, ValueError, TypeError, KeyError, AttributeError):
        raise ControlError("This generation form has expired or is invalid. Reload it before generating.") from None


def _canonical(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if hasattr(value, "values_list"):
        return sorted(value.values_list("pk", flat=True))
    raise TypeError("Unsupported fingerprint value.")


def fingerprint(value, purpose):
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=_canonical)
    return salted_hmac("proposalq.ai-input." + purpose, serialized, algorithm="sha256").hexdigest()


def boundaries(moment):
    utc = moment.astimezone(dt_timezone.utc)
    day = utc.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    return day, week, day + timedelta(days=1), week + timedelta(days=7)


def allowance(user, moment=None):
    moment = moment or now()
    day, week, day_reset, week_reset = boundaries(moment)
    rows = AIRequest.objects.filter(user=user, quota_state__in=(Quota.RESERVED, Quota.CONSUMED))
    daily = rows.filter(admitted_at__gte=day, admitted_at__lt=day_reset).aggregate(total=Sum("quota_units"))["total"] or 0
    weekly = rows.filter(admitted_at__gte=week, admitted_at__lt=week_reset).aggregate(total=Sum("quota_units"))["total"] or 0
    return {"daily_remaining": max(0, DAILY_CREDITS - daily),
            "weekly_remaining": max(0, WEEKLY_CREDITS - weekly),
            "daily_reset": day_reset.isoformat(), "weekly_reset": week_reset.isoformat()}, daily, weekly


def _recover_stale(user, moment):
    # Write first on SQLite and claim the ledger row before any period lock.
    expired = AIRequest.objects.filter(user=user, lifecycle__in=ACTIVE, lease_expires_at__lte=moment)
    expired.update(lease_expires_at=F("lease_expires_at"))
    counts = {"released": 0, "consumed": 0}
    for row in expired.order_by("pk"):
        dispatched = row.dispatch_started_at is not None
        quota = Quota.CONSUMED if dispatched else Quota.RELEASED
        changed = expired.filter(pk=row.pk, quota_state=Quota.RESERVED).update(
            lifecycle=Lifecycle.UNCERTAIN if dispatched else Lifecycle.FAILED,
            quota_state=quota, completed_at=moment, failure_category=Failure.STALE,
        )
        if changed:
            ai_global.finish_reservation(row, quota)
            counts["consumed" if dispatched else "released"] += 1
    return counts


def recover_stale(user, moment=None):
    try:
        with transaction.atomic():
            return _recover_stale(user, moment or now())
    except (DatabaseError, ai_global.CoordinationError):
        raise unavailable() from None


def recover_global_stale(limit=STALE_RECOVERY_BATCH, moment=None):
    """Bounded cross-user recovery, one ledger-first transaction per account.

    Never invoke while retaining a new admission's period locks.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Recovery batch must be between 1 and 1000.")
    moment = moment or now()
    try:
        users = list(AIRequest.objects.filter(lifecycle__in=ACTIVE, lease_expires_at__lte=moment)
                     .order_by("lease_expires_at", "pk").values_list("user_id", flat=True)[:limit])
        counts = {"released": 0, "consumed": 0}
        for user_id in users:
            result = recover_stale(user_id, moment)
            for name in counts:
                counts[name] += result[name]
        return counts
    except DatabaseError:
        raise unavailable() from None


@dataclass(frozen=True)
class Admission:
    request: AIRequest
    replay: bool = False


def _replay(row, submitted, effective):
    if not (constant_time_compare(row.submitted_fingerprint, submitted)
            and constant_time_compare(row.effective_fingerprint, effective)):
        raise ControlError("This request was already submitted with different inputs. Start a new generation.")
    if row.lifecycle in ACTIVE:
        raise ControlError("An AI request is already running for your account. Please wait.")
    if row.lifecycle == Lifecycle.SUCCEEDED:
        return Admission(row, replay=True)
    if row.lifecycle == Lifecycle.UNCERTAIN:
        raise ControlError("The previous request could not be confirmed. Start an explicit new generation to try again.")
    raise ControlError("The previous request did not complete. Start an explicit new generation to try again.")


def reject_invalid_replay(user, operation, token, resource_id=None):
    """An admitted identity cannot be reused to correct/change invalid inputs."""
    try:
        identity, intent = validate_nonce(token, user, operation, resource_id)
    except ControlError:
        return  # Ordinary invalid forms remain correctable before admission.
    try:
        if AIRequest.objects.filter(user=user, nonce=identity).exists():
            raise ControlError("This request was already admitted. Start a new generation to change its inputs.")
    except DatabaseError:
        raise unavailable() from None


def admit(user, operation, token, submitted_input, effective_input, resource_id=None):
    identity, intent = validate_nonce(token, user, operation, resource_id)
    moment = now()
    try:
        submitted = fingerprint(submitted_input, "submitted")
        effective = fingerprint(effective_input, "effective")
    except DatabaseError:
        raise unavailable() from None
    try:
        if not connection.features.supports_partial_indexes:
            raise unavailable()
        _require_read_committed()
        existing = AIRequest.objects.filter(user=user, nonce=identity).first()
        if existing:
            recover_stale(user, moment)
            existing.refresh_from_db()
            return _replay(existing, submitted, effective)
        _require_enabled()
        configured = ai_global.limits()
        # Old-period recovery commits separately, before new-period acquisition.
        recover_stale(user, moment)
        if configured[0] is not None:
            recover_global_stale(moment=moment)
        for attempt in range(ADMISSION_RESTARTS):
            try:
                with transaction.atomic():
                    _require_read_committed()
                    _require_enabled()
                    # First application write establishes the account-wide slot.
                    row = AIRequest.objects.create(
                        user=user, operation=operation, nonce=identity, intent=intent,
                        submitted_fingerprint=submitted, effective_fingerprint=effective,
                        quota_units=CREDITS[operation], admitted_at=moment,
                        lease_expires_at=moment + LEASE, job_post_id=resource_id,
                    )
                    moment = now()
                    periods = None
                    if configured[0] is not None:
                        periods = ai_global.lock_current(moment)
                        moment = now()
                        if not ai_global.matches(periods, moment):
                            raise _RestartAdmission()
                    # One final application time after ALL contention, never DB NOW().
                    row.admitted_at = moment
                    row.lease_expires_at = moment + LEASE
                    row.save(update_fields=["admitted_at", "lease_expires_at"])
                    _require_enabled()
                    _check_user_limits(user, row, moment)
                    if periods is not None:
                        ai_global.reserve(row, periods, configured, moment)
                    _require_enabled()
                    return Admission(row)
            except _RestartAdmission:
                # Only database admission restarts. No provider work has started.
                continue
        raise unavailable()
    except ai_global.CapacityError as error:
        raise ControlError(services.AIDisabledError.user_message, 429, retry_after=error.retry_after) from None
    except ai_global.CoordinationError:
        raise unavailable() from None
    except ControlError:
        # Recovery has its own ledger-first transaction and cannot retain new locks.
        recover_stale(user)
        raise
    except IntegrityError:
        try:
            existing = AIRequest.objects.filter(user=user, nonce=identity).first()
            if existing:
                return _replay(existing, submitted, effective)
        except DatabaseError:
            raise unavailable() from None
        raise ControlError("An AI request is already running for your account. Please wait.") from None
    except DatabaseError:
        raise unavailable() from None


def _require_read_committed():
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            if cursor.fetchone()[0] != "read committed":
                raise unavailable()


def _check_user_limits(user, row, moment):
    operation = row.operation
    maximum, window = BURSTS[operation]
    attempts = AIRequest.objects.filter(
        user=user, operation=operation, dispatch_started_at__gt=moment - window,
        dispatch_started_at__lte=moment,
    ).order_by("dispatch_started_at")
    if attempts.count() >= maximum:
        retry = max(1, math.ceil((attempts.first().dispatch_started_at + window - moment).total_seconds()))
        raise ControlError("Please wait before generating again.", 429, retry_after=retry)
    metadata, daily, weekly = allowance(user, moment)
    day, week, day_reset, week_reset = boundaries(moment)
    resets = []
    if daily > DAILY_CREDITS:
        resets.append(day_reset)
    if weekly > WEEKLY_CREDITS:
        resets.append(week_reset)
    if resets:
        # Remove the tentative reservation from displayed allowance too.
        metadata["daily_remaining"] = max(0, DAILY_CREDITS - daily + row.quota_units)
        metadata["weekly_remaining"] = max(0, WEEKLY_CREDITS - weekly + row.quota_units)
        reset = max(resets)
        metadata["next_reset"] = reset.isoformat()
        raise ControlError("Your ProposalQ allowance is insufficient. It resets at " + reset.strftime("%Y-%m-%d %H:%M UTC") + ".",
                           429, retry_after=max(1, math.ceil((reset - moment).total_seconds())), metadata=metadata)


def _owned_live(row, moment):
    return AIRequest.objects.filter(pk=row.pk, user_id=row.user_id, nonce=row.nonce,
                                    lifecycle__in=ACTIVE, lease_expires_at__gt=moment)


def _telemetry_fields(telemetry):
    if type(telemetry) is not services.AITelemetry:
        raise TypeError("Invalid AI telemetry.")
    # Revalidate at the persistence boundary, copying only approved scalar fields.
    measured = services.AITelemetry(**telemetry.scalar_fields()).scalar_fields()
    # An absent optional snapshot must not erase the known dispatch configuration.
    identity = ("provider", "api_style", "requested_model", "completion_token_cap")
    return {name: value for name, value in measured.items() if value is not None or name not in identity}


def mark_dispatch(row, telemetry=None):
    if getattr(settings, "AI_ENABLED", False) is not True:
        fail(row, Failure.AI_DISABLED, release=True)
        _require_enabled()
    moment = now()
    telemetry = telemetry if telemetry is not None else services.request_telemetry(row.operation)
    try:
        owned = AIRequest.objects.get(pk=row.pk, user_id=row.user_id, nonce=row.nonce)
        ai_global.check_dispatch_bindings(owned)
        changed = _owned_live(row, moment).filter(lifecycle=Lifecycle.RESERVED, quota_state=Quota.RESERVED).update(
            lifecycle=Lifecycle.IN_FLIGHT, dispatch_started_at=moment,
            **_telemetry_fields(telemetry),
        )
        if not changed:
            recover_stale(row.user_id)
            raise ControlError("This request is no longer active. Start a new generation.")
    except (DatabaseError, AIRequest.DoesNotExist, ai_global.CoordinationError):
        try:
            fail(row, Failure.COORDINATION, release=True)
        except ControlError:
            pass  # Retained reservation remains bounded and will expire safely.
        raise unavailable() from None


def fail(row, category, *, release=False, telemetry=None):
    if category not in Failure.values:
        category = Failure.UNEXPECTED
    moment = now()
    measured = _telemetry_fields(telemetry) if telemetry is not None else {}
    try:
        with transaction.atomic():
            _recover_stale(row.user_id, moment)
            quota = Quota.RELEASED if release else Quota.CONSUMED
            changed = _owned_live(row, moment).filter(quota_state=Quota.RESERVED).update(
                lifecycle=Lifecycle.FAILED, quota_state=Quota.RELEASED if release else Quota.CONSUMED,
                completed_at=moment, failure_category=category,
                **measured,
            )
            if changed:
                owned = AIRequest.objects.get(pk=row.pk)
                if ai_global.finish_reservation(owned, quota) and owned.lease_expires_at <= now():
                    raise _ExpiredDuringAccounting()
    except _ExpiredDuringAccounting:
        recover_stale(row.user_id)
    except (DatabaseError, ai_global.CoordinationError):
        raise unavailable() from None


def record_telemetry(row, telemetry):
    """Short fenced write, committed before any application persistence transaction."""
    measured = _telemetry_fields(telemetry)
    moment = now()
    try:
        changed = _owned_live(row, moment).filter(lifecycle=Lifecycle.IN_FLIGHT).update(**measured)
        if not changed:
            recover_stale(row.user_id)
            raise ControlError("This request expired before completion. Start a new generation.")
    except DatabaseError:
        try:
            # Paid work may have happened. Never release or retry after this write failure.
            fail(row, Failure.COORDINATION)
        except ControlError:
            pass  # If the database stays unavailable, normal stale recovery applies.
        raise unavailable() from None


def call_provider(row, function):
    requested = services.request_telemetry(row.operation)
    try:
        services.check_configuration()
    except services.AIDisabledError:
        fail(row, Failure.AI_DISABLED, release=True, telemetry=requested)
        raise ControlError(services.AIDisabledError.user_message, 503) from None
    except services.AIConfigurationError:
        fail(row, Failure.LOCAL_CONFIGURATION, release=True, telemetry=requested)
        raise
    mark_dispatch(row, requested)
    try:
        result = function()
        if type(result) is not services.AIServiceResult:
            raise services.AIResponseError()
    except services.AIError as error:
        release = isinstance(error, (services.AIConfigurationError, services.AICapacityError, services.AIInputError))
        fail(row, error.category, release=release, telemetry=error.telemetry)
        if isinstance(error, services.AIDisabledError):
            raise ControlError(error.user_message, 503) from None
        raise
    except Exception:
        fail(row, Failure.UNEXPECTED)
        raise ControlError("ProposalQ couldn't complete this request. Start a new generation to try again.", 503) from None
    record_telemetry(row, result.telemetry)
    return result


def succeed(row, persist=None):
    moment = now()
    try:
        with transaction.atomic():
            # This write owns/fences the ledger row before application persistence.
            # Recovery cannot pass it until this transaction commits or rolls back.
            changed = _owned_live(row, moment).filter(lifecycle=Lifecycle.IN_FLIGHT, quota_state=Quota.RESERVED).update(
                lifecycle=Lifecycle.SUCCEEDED, quota_state=Quota.CONSUMED,
                completed_at=moment, failure_category="",
            )
            if not changed:
                raise ControlError("This request expired before completion. Start a new generation.")
            owned = AIRequest.objects.get(pk=row.pk)
            if ai_global.finish_reservation(owned, Quota.CONSUMED) and owned.lease_expires_at <= now():
                raise ControlError("This request expired before completion. Start a new generation.")
            result = persist() if persist else None
            if isinstance(result, JobPost):
                AIRequest.objects.filter(pk=row.pk).update(job_post=result)
            elif isinstance(result, Proposal):
                AIRequest.objects.filter(pk=row.pk).update(job_post_id=result.job_post_id, proposal=result)
            return result
    except ControlError:
        recover_stale(row.user_id)
        raise
    except Exception:
        fail(row, Failure.PERSISTENCE)
        raise ControlError("ProposalQ couldn't save the result. Start a new generation to try again.", 503) from None
