from django.conf import settings
from django.db import models

class FreelancerProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )

    professional_title = models.CharField(max_length=255)
    profile_summary = models.TextField(blank=True, null=True)
    preferred_tone = models.CharField(max_length=100, default="professional")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.professional_title

class WorkExperience(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )

    job_title = models.CharField(
        max_length=255
    )

    company_or_project = models.CharField(
        max_length=255,
        blank=True,
        null=True
    )

    tasks = models.TextField()

    skills_used = models.TextField(
        blank=True,
        null=True
    )

    experience_depth = models.TextField(
        blank=True,
        null=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return self.job_title


class JobPost(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )

    raw_job_text = models.TextField(blank=True, null=True)

    platform = models.CharField(max_length=100, blank=True, null=True)
    job_title = models.CharField(max_length=255, default="Untitled Job")
    job_description = models.TextField()

    budget_type = models.CharField(max_length=100, blank=True, null=True)
    hourly_min = models.DecimalField(max_digits=8, decimal_places=2, blank=True, null=True)
    hourly_max = models.DecimalField(max_digits=8, decimal_places=2, blank=True, null=True)

    experience_level = models.CharField(max_length=100, blank=True, null=True)
    project_duration = models.CharField(max_length=100, blank=True, null=True)
    hours_per_week = models.CharField(max_length=100, blank=True, null=True)

    skills_required = models.TextField(blank=True, null=True)
    client_location = models.CharField(max_length=255, blank=True, null=True)

    proposal_count = models.CharField(max_length=100, blank=True, null=True)
    interviewing_count = models.CharField(max_length=100, blank=True, null=True)
    invites_sent = models.CharField(max_length=100, blank=True, null=True)

    confirmed_by_user = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.job_title
class Proposal(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )

    job_post = models.ForeignKey(
        JobPost,
        on_delete=models.CASCADE
    )

    final_text = models.TextField()

    ai_score = models.PositiveIntegerField(null=True, blank=True)

    STATUS_CHOICES = [
        ("generated", "Generated"),
        ("used", "Used"),
        ("reply", "Reply"),
        ("interview", "Interview"),
        ("hired", "Hired"),
        ("rejected", "Rejected"),
        ("no_response", "No Response"),
    ]

    status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES,
        default="generated"
    )

    used_by_user = models.BooleanField(default=False)

    used_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Proposal for {self.job_post.job_title}"


class ProposalOutcome(models.Model):

    proposal = models.OneToOneField(
        Proposal,
        on_delete=models.CASCADE
    )

    status = models.CharField(
        max_length=50
    )

    notes = models.TextField(
        blank=True,
        null=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return f"{self.proposal.job_post.job_title} - {self.status}"

class ProposalUseConfirmation(models.Model):
    proposal = models.OneToOneField(
        Proposal,
        on_delete=models.CASCADE
    )

    platform = models.CharField(max_length=100)
    client_name = models.CharField(max_length=255, blank=True, null=True)
    job_url = models.URLField(blank=True, null=True)
    submitted_proposal_text = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

    confirmed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Used confirmation for {self.proposal}"
