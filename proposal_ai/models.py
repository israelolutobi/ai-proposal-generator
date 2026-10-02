from django.conf import settings
from django.db import models
from django.utils import timezone


class AIQuotaPeriod(models.Model):
    """Durable product-credit accounting; period dates are UTC, not browser dates."""
    class Kind(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"

    kind = models.CharField(max_length=6, choices=Kind.choices, editable=False)
    period_start = models.DateField(editable=False)
    credit_limit = models.BigIntegerField(null=True, blank=True, editable=False)
    reserved_credits = models.BigIntegerField(default=0, editable=False)
    consumed_credits = models.BigIntegerField(default=0, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("kind", "period_start"), name="ai_quota_period_unique"),
            models.CheckConstraint(condition=models.Q(kind__in=("daily", "weekly")), name="ai_quota_period_kind"),
            models.CheckConstraint(condition=models.Q(reserved_credits__gte=0), name="ai_quota_reserved_nonnegative"),
            models.CheckConstraint(condition=models.Q(consumed_credits__gte=0), name="ai_quota_consumed_nonnegative"),
            models.CheckConstraint(condition=models.Q(credit_limit__isnull=True) | models.Q(credit_limit__gte=0), name="ai_quota_limit_nonnegative"),
        ]

    def __str__(self):
        return f"{self.kind}: {self.period_start} UTC"


class AIRequest(models.Model):
    """Payload-free request ledger; allowances are not provider cost estimates."""

    class Operation(models.TextChoices):
        PROFILE_SUMMARY = "profile_summary", "Profile summary"
        JOB_EXTRACTION = "job_extraction", "Job extraction"
        PROPOSAL_GENERATION = "proposal_generation", "Proposal generation"

    class Intent(models.TextChoices):
        GENERATE = "generate", "Generate"
        REGENERATE = "regenerate", "Regenerate"

    class Lifecycle(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        IN_FLIGHT = "in_flight", "In flight"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        UNCERTAIN = "uncertain", "Uncertain"

    class Quota(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        CONSUMED = "consumed", "Consumed"
        RELEASED = "released", "Released"

    class Failure(models.TextChoices):
        AI_DISABLED = "ai_disabled", "AI disabled before provider execution"
        LOCAL_CONFIGURATION = "local_configuration", "Local configuration"
        CONFIGURATION = "configuration", "Provider configuration rejection"
        AUTHENTICATION = "authentication", "Provider authentication rejection"
        CAPACITY = "capacity", "Provider capacity rejection"
        INVALID_REQUEST = "invalid_request", "Provider request rejection"
        INPUT_LIMIT = "input_limit", "Input validation"
        TIMEOUT = "timeout", "Timeout"
        CONNECTION = "connection", "Connection"
        TEMPORARY = "temporary", "Temporary failure"
        INVALID_RESPONSE = "invalid_response", "Invalid response"
        INCOMPLETE_RESPONSE = "incomplete_response", "Incomplete response"
        OVERSIZED_RESPONSE = "oversized_response", "Oversized response"
        PERSISTENCE = "persistence", "Persistence failure"
        COORDINATION = "coordination", "Coordination failure"
        STALE = "stale", "Expired request"
        UNEXPECTED = "unexpected", "Unexpected failure"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    operation = models.CharField(max_length=24, choices=Operation.choices)
    nonce = models.UUIDField()
    intent = models.CharField(max_length=12, choices=Intent.choices)
    submitted_fingerprint = models.CharField(max_length=64)
    effective_fingerprint = models.CharField(max_length=64)
    lifecycle = models.CharField(max_length=12, choices=Lifecycle.choices, default=Lifecycle.RESERVED)
    quota_state = models.CharField(max_length=8, choices=Quota.choices, default=Quota.RESERVED)
    quota_units = models.PositiveSmallIntegerField(editable=False)
    admitted_at = models.DateTimeField(default=timezone.now, editable=False)
    dispatch_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField()
    failure_category = models.CharField(max_length=24, choices=Failure.choices, blank=True)
    job_post = models.ForeignKey("JobPost", null=True, blank=True, on_delete=models.SET_NULL)
    proposal = models.ForeignKey("Proposal", null=True, blank=True, on_delete=models.SET_NULL)
    global_day_period = models.ForeignKey(AIQuotaPeriod, null=True, blank=True, editable=False,
                                         on_delete=models.PROTECT, related_name="daily_requests")
    global_week_period = models.ForeignKey(AIQuotaPeriod, null=True, blank=True, editable=False,
                                          on_delete=models.PROTECT, related_name="weekly_requests")

    # Provider evidence is independent of quota units and future monetary estimates.
    # NULL means unknown; zero is reserved for an actual reported/measured zero.
    provider = models.CharField(max_length=24, null=True, blank=True)
    api_style = models.CharField(max_length=24, null=True, blank=True)
    requested_model = models.CharField(max_length=200, null=True, blank=True)
    response_model = models.CharField(max_length=200, null=True, blank=True)
    input_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    reasoning_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    cached_input_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    total_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    provider_latency_ms = models.PositiveBigIntegerField(null=True, blank=True)
    service_tier = models.CharField(max_length=16, null=True, blank=True)
    completion_token_cap = models.PositiveBigIntegerField(null=True, blank=True)
    finish_reason = models.CharField(max_length=16, null=True, blank=True)
    response_text_characters = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("user", "nonce"), name="ai_request_user_nonce_unique"),
            models.UniqueConstraint(
                fields=("user",), condition=models.Q(lifecycle__in=("reserved", "in_flight")),
                name="ai_request_one_active_user",
            ),
            models.CheckConstraint(
                condition=(models.Q(operation="profile_summary", quota_units=1)
                           | models.Q(operation="job_extraction", quota_units=2)
                           | models.Q(operation="proposal_generation", quota_units=3)),
                name="ai_request_operation_units",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "admitted_at"), name="ai_request_user_admitted"),
            models.Index(fields=("user", "operation", "dispatch_started_at"), name="ai_request_user_burst"),
            models.Index(fields=("lifecycle", "lease_expires_at"), name="ai_request_stale"),
        ]


class FreelancerProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )

    professional_title = models.CharField(
        max_length=255
    )

    profile_summary = models.TextField(
        blank=True,
        null=True,
    )

    preferred_tone = models.CharField(
        max_length=100,
        default="professional",
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    def __str__(self):
        return self.professional_title


class WorkExperience(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )

    job_title = models.CharField(
        max_length=255
    )

    company_or_project = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    tasks = models.TextField()

    skills_used = models.TextField(
        blank=True,
        null=True,
    )

    experience_depth = models.TextField(
        blank=True,
        null=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return self.job_title


class JobPost(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )

    raw_job_text = models.TextField(
        blank=True,
        null=True,
    )

    platform = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    job_title = models.CharField(
        max_length=255,
        default="Untitled Job",
    )

    job_description = models.TextField()

    budget_type = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    hourly_min = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        blank=True,
        null=True,
    )

    hourly_max = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        blank=True,
        null=True,
    )

    fixed_budget = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
    )

    experience_level = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    project_duration = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    hours_per_week = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    skills_required = models.TextField(
        blank=True,
        null=True,
    )

    client_location = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    proposal_count = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    interviewing_count = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    invites_sent = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    confirmed_by_user = models.BooleanField(
        default=False
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return self.job_title


class Proposal(models.Model):

    # ---------------------------------------------------------
    # TYPE OF WRITTEN CONTENT GENERATED FOR THIS APPLICATION
    # ---------------------------------------------------------
    #
    # Different freelance platforms use different terminology.
    #
    # Examples:
    # Upwork       -> Cover Letter
    # Some sites   -> Proposal Text
    # Direct work  -> Pitch
    #
    # The Proposal model remains the broader application record,
    # while content_type tells ProposalIQ what the generated
    # written component actually represents.
    # ---------------------------------------------------------

    CONTENT_TYPE_CHOICES = [
        (
            "cover_letter",
            "Cover Letter",
        ),
        (
            "proposal_text",
            "Proposal Text",
        ),
        (
            "application_message",
            "Application Message",
        ),
        (
            "pitch",
            "Pitch",
        ),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )

    job_post = models.ForeignKey(
        JobPost,
        on_delete=models.CASCADE,
    )

    # Keep the existing database field name for now.
    #
    # Conceptually, this stores the generated written component:
    # cover letter, proposal text, pitch, etc.
    #
    # Renaming this field would create unnecessary migration work
    # at this stage of the beta.
    final_text = models.TextField()

    content_type = models.CharField(
        max_length=50,
        choices=CONTENT_TYPE_CHOICES,
        default="proposal_text",
    )

    ai_score = models.PositiveIntegerField(
        null=True,
        blank=True,
    )

    STATUS_CHOICES = [
        (
            "generated",
            "Generated",
        ),

        # New preferred terminology.
        (
            "submitted",
            "Submitted",
        ),

        # Retained temporarily for backwards compatibility with
        # existing ProposalIQ records and code.
        (
            "used",
            "Used",
        ),

        (
            "reply",
            "Reply",
        ),
        (
            "interview",
            "Interview",
        ),
        (
            "hired",
            "Hired",
        ),
        (
            "rejected",
            "Rejected",
        ),
        (
            "no_response",
            "No Response",
        ),
    ]

    status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES,
        default="generated",
    )

    # Legacy field retained for now so existing code/data does
    # not break. We will migrate the user-facing concept from
    # "used" to "submitted" in the views/templates.
    used_by_user = models.BooleanField(
        default=False
    )

    # Legacy timestamp retained for backwards compatibility.
    used_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return (
            f"Application for "
            f"{self.job_post.job_title}"
        )


class ProposalOutcome(models.Model):
    proposal = models.OneToOneField(
        Proposal,
        on_delete=models.CASCADE,
    )

    status = models.CharField(
        max_length=50
    )

    notes = models.TextField(
        blank=True,
        null=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return (
            f"{self.proposal.job_post.job_title} "
            f"- {self.status}"
        )


class ProposalUseConfirmation(models.Model):

    # This model currently stores submission details.
    #
    # The class name is kept temporarily because renaming a Django
    # model during an active beta creates unnecessary migration risk.
    #
    # In the user interface we will refer to this concept as a
    # Submission rather than "Use Confirmation".

    proposal = models.OneToOneField(
        Proposal,
        on_delete=models.CASCADE,
    )

    platform = models.CharField(
        max_length=100
    )

    client_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    job_url = models.URLField(
        blank=True,
        null=True,
    )

    # Keep the existing database field name for now.
    # It can contain the final cover letter, proposal text,
    # pitch or other platform-specific written content.
    submitted_proposal_text = models.TextField(
        blank=True,
        null=True,
    )

    notes = models.TextField(
        blank=True,
        null=True,
    )

    confirmed_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return (
            f"Submission confirmation for "
            f"{self.proposal}"
        )
