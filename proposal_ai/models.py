from django.conf import settings
from django.db import models


class FreelancerProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    professional_title = models.CharField(max_length=255)
    main_skills = models.TextField()
    years_experience = models.PositiveIntegerField(null=True, blank=True)
    preferred_tone = models.CharField(max_length=100, default="professional")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.professional_title


class JobPost(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )

    job_title = models.CharField(max_length=255, default="Untitled Job")
    job_description = models.TextField()
    platform = models.CharField(max_length=100, default="Upwork")

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

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Proposal for {self.job_post.job_title}"


class ProposalOutcome(models.Model):
    proposal = models.OneToOneField(
        Proposal,
        on_delete=models.CASCADE
    )

    STATUS_CHOICES = [
        ("no_response", "No Response"),
        ("replied", "Replied"),
        ("interview", "Interview"),
        ("hired", "Hired"),
        ("rejected", "Rejected"),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.proposal} - {self.status}"