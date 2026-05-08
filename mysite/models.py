
# #
# class WorkExperience(models.Model):
#     user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
#     title = models.CharField(max_length=255)
#     description = models.TextField()
#     skills_used = models.TextField(blank=True)
#     result_achieved = models.TextField(blank=True)
#     created_at = models.DateTimeField(auto_now_add=True)
#
#     def __str__(self):
#         return self.title
#
#
# class JobPost(models.Model):
#     user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
#     platform = models.CharField(max_length=100, default="Upwork")
#     job_title = models.CharField(max_length=255)
#     job_description = models.TextField()
#     client_requirements = models.JSONField(default=dict, blank=True)
#     created_at = models.DateTimeField(auto_now_add=True)
#
#     def __str__(self):
#         return self.job_title
#
#
# class Proposal(models.Model):
#     user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
#     job_post = models.ForeignKey(JobPost, on_delete=models.CASCADE)
#     selected_experience = models.ManyToManyField(WorkExperience, blank=True)
#     final_text = models.TextField()
#     ai_score = models.PositiveIntegerField(null=True, blank=True)
#     created_at = models.DateTimeField(auto_now_add=True)
#
#     def __str__(self):
#         return f"Proposal for {self.job_post.job_title}"
#
#
# class ProposalVersion(models.Model):
#     proposal = models.ForeignKey(Proposal, on_delete=models.CASCADE, related_name="versions")
#     version_name = models.CharField(max_length=100, default="Version 1")
#     proposal_text = models.TextField()
#     tone = models.CharField(max_length=100, blank=True)
#     length_type = models.CharField(max_length=100, blank=True)
#     score = models.PositiveIntegerField(null=True, blank=True)
#     created_at = models.DateTimeField(auto_now_add=True)
#
#     def __str__(self):
#         return self.version_name
#
#
# class ProposalOutcome(models.Model):
#     OUTCOME_CHOICES = [
#         ("no_reply", "No Reply"),
#         ("reply", "Reply"),
#         ("interview", "Interview"),
#         ("hired", "Hired"),
#         ("rejected", "Rejected"),
#     ]
#
#     proposal = models.OneToOneField(Proposal, on_delete=models.CASCADE)
#     outcome = models.CharField(max_length=50, choices=OUTCOME_CHOICES, default="no_reply")
#     notes = models.TextField(blank=True)
#     updated_at = models.DateTimeField(auto_now=True)
# #
# #     def __str__(self):
# #         return self.outcome
