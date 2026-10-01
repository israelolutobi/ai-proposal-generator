"""Exercise older validation/provider regressions with valid request identities.

Every paid POST represents a fresh explicit action in a separate quota week.
Task 3C policy/replay tests use the ordinary Client and their own fixed clocks.
No admission logic is disabled, and the network runner still blocks all I/O.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import resolve
from django.utils import timezone

from proposal_ai import ai_control


class SignedAIClient(Client):
    def post(self, path, data=None, *args, **kwargs):
        route = resolve(path)
        operations = {
            "generate_profile_summary": ai_control.Operation.PROFILE_SUMMARY,
            "extract_job_features": ai_control.Operation.JOB_EXTRACTION,
            "confirm_job_features": ai_control.Operation.PROPOSAL_GENERATION,
        }
        user_id = self.session.get("_auth_user_id")
        if route.url_name not in operations or not user_id:
            return super().post(path, data, *args, **kwargs)
        self.policy_clock = getattr(self, "policy_clock", timezone.now()) + timedelta(days=8)
        with patch("proposal_ai.ai_control.now", return_value=self.policy_clock), override_settings(
            OPENAI_API_KEY="test-only-not-a-credential",
        ):
            user = get_user_model().objects.get(pk=user_id)
            data = (data or {}).copy()
            data.setdefault("ai_nonce", ai_control.issue_nonce(
                user, operations[route.url_name], route.kwargs.get("job_post_id"), ai_control.Intent.REGENERATE,
            ))
            return super().post(path, data, *args, **kwargs)
