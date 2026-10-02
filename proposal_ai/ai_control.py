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
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from . import services
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
    # A write is deliberately first: SQLite must not upgrade a stale read snapshot.
    expired = AIRequest.objects.filter(user=user, lifecycle__in=ACTIVE, lease_expires_at__lte=moment)
    expired.filter(dispatch_started_at__isnull=True).update(
        lifecycle=Lifecycle.FAILED, quota_state=Quota.RELEASED,
        completed_at=moment, failure_category=Failure.STALE,
    )
    expired.filter(dispatch_started_at__isnull=False).update(
        lifecycle=Lifecycle.UNCERTAIN, quota_state=Quota.CONSUMED,
        completed_at=moment, failure_category=Failure.STALE,
    )


def recover_stale(user):
    try:
        with transaction.atomic():
            _recover_stale(user, now())
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
        with transaction.atomic():
            _recover_stale(user, moment)
            if connection.vendor == "postgresql":
                # A stale snapshot after waiting on the active-slot constraint
                # could miss recently finalized usage. Require fresh reads.
                with connection.cursor() as cursor:
                    cursor.execute("SHOW transaction_isolation")
                    if cursor.fetchone()[0] != "read committed":
                        raise unavailable()
            existing = AIRequest.objects.filter(user=user, nonce=identity).first()
            if existing:
                return _replay(existing, submitted, effective)
            row = AIRequest.objects.create(
                user=user, operation=operation, nonce=identity, intent=intent,
                submitted_fingerprint=submitted, effective_fingerprint=effective,
                quota_units=CREDITS[operation], admitted_at=moment,
                lease_expires_at=moment + LEASE, job_post_id=resource_id,
            )
            # The INSERT can wait for another request to relinquish its active
            # slot. Its initial timestamps are provisional until that wait ends.
            # Persist one fresh admission time for the lease and all limits below.
            moment = now()
            row.admitted_at = moment
            row.lease_expires_at = moment + LEASE
            row.save(update_fields=["admitted_at", "lease_expires_at"])
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
            return Admission(row)
    except ControlError:
        # A rejected admission rolls back its transaction, including recovery.
        # Commit stale transitions independently so old nonces stay terminal.
        recover_stale(user)
        raise
    except IntegrityError:
        # A competing insert can win PostgreSQL's unique constraint. Re-read only
        # after leaving the failed transaction; never fall through to dispatch.
        try:
            existing = AIRequest.objects.filter(user=user, nonce=identity).first()
            if existing:
                return _replay(existing, submitted, effective)
        except DatabaseError:
            raise unavailable() from None
        raise ControlError("An AI request is already running for your account. Please wait.") from None
    except DatabaseError:
        raise unavailable() from None


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
    moment = now()
    telemetry = telemetry if telemetry is not None else services.request_telemetry(row.operation)
    try:
        changed = _owned_live(row, moment).filter(lifecycle=Lifecycle.RESERVED).update(
            lifecycle=Lifecycle.IN_FLIGHT, dispatch_started_at=moment,
            **_telemetry_fields(telemetry),
        )
        if not changed:
            recover_stale(row.user)
            raise ControlError("This request is no longer active. Start a new generation.")
    except DatabaseError:
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
            _recover_stale(row.user, moment)
            _owned_live(row, moment).update(
                lifecycle=Lifecycle.FAILED, quota_state=Quota.RELEASED if release else Quota.CONSUMED,
                completed_at=moment, failure_category=category,
                **measured,
            )
    except DatabaseError:
        raise unavailable() from None


def record_telemetry(row, telemetry):
    """Short fenced write, committed before any application persistence transaction."""
    measured = _telemetry_fields(telemetry)
    moment = now()
    try:
        changed = _owned_live(row, moment).filter(lifecycle=Lifecycle.IN_FLIGHT).update(**measured)
        if not changed:
            recover_stale(row.user)
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
            changed = _owned_live(row, moment).filter(lifecycle=Lifecycle.IN_FLIGHT).update(
                lifecycle=Lifecycle.SUCCEEDED, quota_state=Quota.CONSUMED,
                completed_at=moment, failure_category="",
            )
            if not changed:
                raise ControlError("This request expired before completion. Start a new generation.")
            result = persist() if persist else None
            if isinstance(result, JobPost):
                AIRequest.objects.filter(pk=row.pk).update(job_post=result)
            elif isinstance(result, Proposal):
                AIRequest.objects.filter(pk=row.pk).update(job_post_id=result.job_post_id, proposal=result)
            return result
    except ControlError:
        recover_stale(row.user)
        raise
    except Exception:
        fail(row, Failure.PERSISTENCE)
        raise ControlError("ProposalQ couldn't save the result. Start a new generation to try again.", 503) from None
