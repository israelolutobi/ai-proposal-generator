from django.conf import settings
from django.db import models


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