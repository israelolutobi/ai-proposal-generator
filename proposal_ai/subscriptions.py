"""Monthly proposal allowances reuse the durable, replay-protected AI ledger."""
from datetime import timezone as dt_timezone
import math

from django.conf import settings
from django.utils import timezone

from .models import AIRequest, BillingAccount


FREE_LIMIT = 3
PRO_LIMIT = 50


def month_bounds(moment):
    start = moment.astimezone(dt_timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def is_pro(account, moment):
    return bool(account and settings.BILLING_ENABLED and account.status == "active" and account.paid
                and account.price_id == settings.STRIPE_PRO_PRICE_ID
                and account.period_start and account.period_start <= moment
                and account.period_end and moment < account.period_end)


def usage(user, moment=None):
    moment = moment or timezone.now()
    account = BillingAccount.objects.filter(user=user).first()
    start, end = month_bounds(moment)
    rows = AIRequest.objects.filter(user=user, operation=AIRequest.Operation.PROPOSAL_GENERATION,
                                    admitted_at__gte=start, admitted_at__lt=end)
    used = rows.filter(lifecycle=AIRequest.Lifecycle.SUCCEEDED).count()
    pending = rows.filter(lifecycle__in=(AIRequest.Lifecycle.RESERVED, AIRequest.Lifecycle.IN_FLIGHT)).count()
    pro = is_pro(account, moment)
    limit = PRO_LIMIT if pro else FREE_LIMIT
    return {"plan": "Pro" if pro else "Free", "limit": limit, "used": used, "pending": pending,
            "remaining": max(0, limit - used - pending), "reset": end,
            "percent": min(100, round(100 * (used + pending) / limit)), "account": account}


def check_admission(user, row, moment):
    if row.operation != AIRequest.Operation.PROPOSAL_GENERATION:
        return
    from .ai_control import ControlError
    # The AIRequest active-user uniqueness serializes admissions before this read.
    # Billing updates lock only BillingAccount, never AIRequest, avoiding lock cycles.
    current = usage(user, moment)
    if current["used"] + current["pending"] > current["limit"]:
        raise ControlError(
            f"You've reached your {current['plan']} plan's monthly proposal limit. "
            "View Pricing & billing to upgrade or check your reset date.", 429,
            retry_after=max(1, math.ceil((current["reset"] - moment).total_seconds())),
            metadata={"monthly_limit": current["limit"], "next_reset": current["reset"].isoformat()},
        )


def lock_account(user, operation):
    if operation == AIRequest.Operation.PROPOSAL_GENERATION:
        # Acquired before global locks and the final admission timestamp, so
        # waiting for a webhook across a month boundary cannot use an old month.
        BillingAccount.objects.select_for_update().filter(user=user).first()
