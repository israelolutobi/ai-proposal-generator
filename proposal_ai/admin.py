"""Permission-controlled, inspection-only access to the AI request ledger."""
from django.contrib import admin

from .models import AIRequest


@admin.register(AIRequest)
class AIRequestAdmin(admin.ModelAdmin):
    list_display = (
        "operation", "lifecycle", "quota_state", "quota_units", "admitted_at",
        "provider", "requested_model", "response_model", "input_tokens",
        "completion_tokens", "total_tokens", "provider_latency_ms", "finish_reason",
        "failure_category",
    )
    list_filter = ("operation", "lifecycle", "quota_state", "failure_category", "provider", "admitted_at")
    date_hierarchy = "admitted_at"
    ordering = ("-admitted_at", "-pk")
    actions = None
    fields = readonly_fields = (
        "id", "account_id", "operation", "intent", "lifecycle", "quota_state", "quota_units",
        "admitted_at", "dispatch_started_at", "completed_at", "lease_expires_at",
        "failure_category", "job_post_reference", "proposal_reference", "provider", "api_style",
        "requested_model", "response_model", "input_tokens", "completion_tokens",
        "reasoning_tokens", "cached_input_tokens", "total_tokens", "provider_latency_ms",
        "service_tier", "completion_token_cap", "finish_reason", "response_text_characters",
    )

    @admin.display(description="Account ID")
    def account_id(self, obj):
        return obj.user_id

    @admin.display(description="JobPost ID")
    def job_post_reference(self, obj):
        return obj.job_post_id

    @admin.display(description="Proposal ID")
    def proposal_reference(self, obj):
        return obj.proposal_id

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
