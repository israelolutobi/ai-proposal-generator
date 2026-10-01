from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from proposal_ai.forms import OUTCOME_STATUS_CHOICES, ProposalOutcomeForm, SubmissionConfirmationForm
from proposal_ai.models import (
    FreelancerProfile, JobPost, Proposal, ProposalOutcome, ProposalUseConfirmation,
)
from proposal_ai.platform_config import PLATFORM_ALIASES, PLATFORM_CONFIGS


User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=["testserver"], DEBUG=False,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class LifecycleTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="submission-owner")
        cls.other = User.objects.create_user(username="other-submission-owner")
        FreelancerProfile.objects.create(user=cls.owner, professional_title="Developer")
        FreelancerProfile.objects.create(user=cls.other, professional_title="Other developer")
        cls.job = JobPost.objects.create(
            user=cls.owner, platform="Upwork", job_title="Build an application",
            job_description="Develop a Django application.",
        )
        cls.proposal = Proposal.objects.create(
            user=cls.owner, job_post=cls.job, final_text="Generated cover letter.",
            content_type="cover_letter",
        )
        other_job = JobPost.objects.create(
            user=cls.other, platform="Fiverr", job_title="PRIVATE_OTHER_JOB", job_description="Private",
        )
        cls.other_proposal = Proposal.objects.create(
            user=cls.other, job_post=other_job, final_text="PRIVATE_OTHER_CONTENT",
        )

    def setUp(self):
        self.client.force_login(self.owner)
        for target in ("proposal_ai.views.get_openai_client", "proposal_ai.views.OpenAI"):
            guard = patch(target, side_effect=AssertionError("No external AI calls permitted."))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)

    def submission_url(self, proposal=None):
        return reverse("confirm_use_proposal", args=[(proposal or self.proposal).pk])

    def outcome_url(self, proposal=None):
        return reverse("update_outcome", args=[(proposal or self.proposal).pk])

    def submission_data(self, **changes):
        data = {
            "platform": "Freelancer", "client_name": "Client",
            "job_url": "https://example.com/jobs/42",
            "submitted_proposal_text": "Actual submitted text.\nA second line.",
            "notes": "Submission notes.",
        }
        data.update(changes)
        return data

    def snapshot(self):
        return tuple(list(model.objects.order_by("pk").values()) for model in (
            Proposal, ProposalUseConfirmation, ProposalOutcome, JobPost,
        ))

    def submit(self, **changes):
        response = self.client.post(self.submission_url(), self.submission_data(**changes))
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.proposal.refresh_from_db()
        return ProposalUseConfirmation.objects.get(proposal=self.proposal)

    def invalid_submission(self, data, field):
        before = self.snapshot()
        response = self.client.post(self.submission_url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(field, response.context["form"].errors)
        self.assertEqual(self.snapshot(), before)
        return response

    def invalid_outcome(self, data):
        before = self.snapshot()
        response = self.client.post(self.outcome_url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("outcome_status", response.context["form"].errors)
        self.assertEqual(self.snapshot(), before)
        return response


class SubmissionTests(LifecycleTestCase):
    def test_owner_get_prefills_generated_text_and_platform_without_writes(self):
        before = self.snapshot()
        response = self.client.get(self.submission_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["platform"].value(), "Upwork")
        self.assertEqual(response.context["form"]["submitted_proposal_text"].value(), self.proposal.final_text)
        self.assertEqual(self.snapshot(), before)

    def test_other_user_get_and_post_receive_404(self):
        before = self.snapshot()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.submission_url()).status_code, 404)
        self.assertEqual(self.client.post(self.submission_url(), self.submission_data()).status_code, 404)
        self.assertEqual(self.snapshot(), before)

    def test_valid_submission_persists_state_timestamp_and_separate_actual_text(self):
        start = timezone.now()
        confirmation = self.submit()
        self.assertEqual(confirmation.proposal_id, self.proposal.pk)
        self.assertEqual(confirmation.proposal.user_id, self.owner.pk)
        self.assertTrue(self.proposal.used_by_user)
        self.assertEqual(self.proposal.status, "submitted")
        self.assertGreaterEqual(self.proposal.used_at, start)
        self.assertEqual(self.proposal.used_at, confirmation.confirmed_at)
        self.assertEqual(confirmation.submitted_proposal_text, self.submission_data()["submitted_proposal_text"])
        self.assertEqual(self.proposal.final_text, "Generated cover letter.")
        self.assertEqual(self.proposal.content_type, "cover_letter")
        self.job.refresh_from_db()
        self.assertEqual(self.job.platform, "Upwork")

    def test_client_identifiers_and_lifecycle_fields_are_never_mass_assigned(self):
        confirmation = self.submit(
            proposal=self.other_proposal.pk, proposal_id=self.other_proposal.pk,
            user=self.other.pk, user_id=self.other.pk, status="hired",
            used_by_user=False, used_at="2000-01-01", confirmed_at="2000-01-01",
            content_type="pitch", final_text="Forged generated text",
        )
        self.assertEqual(confirmation.proposal_id, self.proposal.pk)
        self.assertEqual(self.proposal.user_id, self.owner.pk)
        self.assertEqual(self.proposal.status, "submitted")
        self.assertEqual(self.proposal.final_text, "Generated cover letter.")
        self.other_proposal.refresh_from_db()
        self.assertEqual(self.other_proposal.status, "generated")

    def test_required_platform_missing_blank_and_whitespace_rejected(self):
        for value in (None, "", " \n\t"):
            with self.subTest(value=value):
                data = self.submission_data(platform=value)
                if value is None:
                    del data["platform"]
                self.invalid_submission(data, "platform")

    def test_invalid_platform_and_unsafe_platform_schemes_rejected(self):
        for value in ("javascript:alert(1)", "data:text/plain,foo", "file:///tmp/job", "ftp://example.com", "<script>x</script>", "---", "Bad\nPlatform"):
            with self.subTest(value=value):
                self.invalid_submission(self.submission_data(platform=value), "platform")

    def test_model_max_lengths_enforced(self):
        for field in ("platform", "client_name", "job_url"):
            limit = ProposalUseConfirmation._meta.get_field(field).max_length
            value = "x" * (limit + 1)
            if field == "job_url":
                value = "https://example.com/" + value
            with self.subTest(field=field):
                self.invalid_submission(self.submission_data(**{field: value}), field)

    def test_http_and_https_urls_are_accepted_without_fetching(self):
        with patch("socket.create_connection", side_effect=AssertionError("URL must not be fetched")) as network:
            for url in ("http://example.com/job", "https://example.com/job?ref=42#details"):
                with self.subTest(url=url):
                    self.assertEqual(self.submit(job_url=url).job_url, url)
            network.assert_not_called()

    def test_malformed_and_unsafe_urls_are_rejected(self):
        for url in ("not a url", "https://", "https://bad host/job", "javascript:alert(1)", "data:text/html,test", "file:///tmp/job", "ftp://example.com/job"):
            with self.subTest(url=url):
                self.invalid_submission(self.submission_data(job_url=url), "job_url")

    def test_optional_fields_remain_optional_including_actual_submitted_text(self):
        response = self.client.post(self.submission_url(), {"platform": "Direct Client"})
        self.assertEqual(response.status_code, 302)
        confirmation = ProposalUseConfirmation.objects.get(proposal=self.proposal)
        for field in ("client_name", "job_url", "submitted_proposal_text", "notes"):
            self.assertIn(getattr(confirmation, field), (None, ""))

    def test_surrounding_whitespace_trimmed_and_multiline_text_preserved(self):
        confirmation = self.submit(
            platform="  Upwork.com  ", client_name=" Client ",
            job_url=" https://example.com/job ", submitted_proposal_text=" \nFirst\nSecond \t",
            notes=" \nNote\nNext ",
        )
        self.assertEqual(confirmation.platform, "Upwork.com")
        self.assertEqual(confirmation.client_name, "Client")
        self.assertEqual(confirmation.job_url, "https://example.com/job")
        self.assertEqual(confirmation.submitted_proposal_text, "First\nSecond")
        self.assertEqual(confirmation.notes, "Note\nNext")

    def test_repeated_post_updates_one_confirmation_and_preserves_timestamps(self):
        first = self.submit()
        original_used_at = self.proposal.used_at
        second = self.submit(notes="Edited notes")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ProposalUseConfirmation.objects.count(), 1)
        self.assertEqual(second.confirmed_at, first.confirmed_at)
        self.assertEqual(self.proposal.used_at, original_used_at)
        self.assertEqual(second.notes, "Edited notes")

    def test_existing_confirmation_prefills_all_fields_without_writes(self):
        confirmation = self.submit()
        before = self.snapshot()
        response = self.client.get(self.submission_url())
        for name in SubmissionConfirmationForm.Meta.fields:
            self.assertEqual(response.context["form"][name].value(), getattr(confirmation, name))
        self.assertEqual(self.snapshot(), before)

    def test_invalid_edit_preserves_existing_confirmation_and_lifecycle(self):
        self.submit()
        self.invalid_submission(self.submission_data(platform="", notes="Invalid edit"), "platform")

    def test_missing_optional_edit_fields_preserve_saved_values_and_explicit_blank_clears(self):
        confirmation = self.submit()
        response = self.client.post(self.submission_url(), {"platform": "Fiverr"})
        self.assertEqual(response.status_code, 302)
        confirmation.refresh_from_db()
        self.assertEqual(confirmation.notes, "Submission notes.")
        self.assertEqual(confirmation.client_name, "Client")
        self.assertEqual(confirmation.submitted_proposal_text, self.submission_data()["submitted_proposal_text"])
        self.client.post(self.submission_url(), {"platform": "Fiverr", "notes": ""})
        confirmation.refresh_from_db()
        self.assertIn(confirmation.notes, (None, ""))

    def test_invalid_submission_retains_entered_values_and_escapes_html(self):
        html = '</textarea><script>alert("test")</script>'
        response = self.invalid_submission(self.submission_data(job_url="bad url", notes=html, submitted_proposal_text=html), "job_url")
        self.assertContains(response, escape(html))
        self.assertNotContains(response, html)
        self.assertEqual(response.context["form"]["job_url"].value(), "bad url")
        self.assertEqual(response.context["form"]["submitted_proposal_text"].value(), html)

    def test_editing_submission_preserves_saved_valid_outcome(self):
        self.submit()
        outcome = ProposalOutcome.objects.create(proposal=self.proposal, status="hired", notes="Hired notes")
        self.proposal.status = "hired"
        self.proposal.save(update_fields=["status"])
        self.submit(platform="Fiverr")
        self.assertEqual(self.proposal.status, "hired")
        outcome.refresh_from_db()
        self.assertEqual(outcome.notes, "Hired notes")

    def test_confirming_legacy_outcome_status_without_outcome_does_not_erase_history(self):
        Proposal.objects.filter(pk=self.proposal.pk).update(status="hired")
        self.submit()
        self.assertEqual(self.proposal.status, "hired")
        self.assertFalse(ProposalOutcome.objects.filter(proposal=self.proposal).exists())
        before = self.snapshot()
        response = self.client.get(self.outcome_url())
        self.assertEqual(response.context["form"]["outcome_status"].value(), "hired")
        self.assertContains(response, 'value="hired" selected')
        self.assertEqual(self.snapshot(), before)


class PlatformAndLegacyTests(LifecycleTestCase):
    def dashboard_proposal(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "PRIVATE_OTHER_JOB")
        return response, response.context["proposals"][0]

    def test_original_platform_and_labels_display_before_submission(self):
        response, proposal = self.dashboard_proposal()
        self.assertEqual(proposal.display_platform, "Upwork")
        self.assertEqual(proposal.application_label, "Proposal")
        self.assertEqual(proposal.generated_heading, "Your Cover Letter")
        self.assertFalse(proposal.is_submitted)
        self.assertEqual(response.context["pending_outcomes"], [])

    def test_corrected_submission_platform_and_labels_survive_reload_everywhere(self):
        self.submit(platform="Fiverr")
        response, proposal = self.dashboard_proposal()
        self.assertEqual(proposal.display_platform, "Fiverr")
        self.assertEqual(proposal.application_label, "Offer")
        self.assertEqual(proposal.submitted_label, "Sent")
        self.assertEqual(proposal.generated_heading, "Your Cover Letter")
        self.assertContains(response, "Fiverr", count=2)
        self.assertEqual(response.context["pending_outcomes"][0].display_platform, "Fiverr")
        response = self.client.get(self.outcome_url())
        self.assertEqual(response.context["application_label"], "Offer")
        self.assertEqual(response.context["content_label"], "Client Message")
        self.assertContains(response, "Fiverr")
        self.job.refresh_from_db()
        self.assertEqual(self.job.platform, "Upwork")

    def test_configured_platform_aliases_and_http_urls_are_supported(self):
        for alias, key in PLATFORM_ALIASES.items():
            with self.subTest(alias=alias):
                form = SubmissionConfirmationForm(data={"platform": alias})
                self.assertTrue(form.is_valid(), form.errors)
                self.submit(platform=alias)
                _, displayed = self.dashboard_proposal()
                self.assertEqual(displayed.platform_key, key)
                self.assertEqual(displayed.application_label, PLATFORM_CONFIGS[key]["application_label"])
        self.submit(platform="https://www.upwork.com/jobs/42")
        _, displayed = self.dashboard_proposal()
        self.assertEqual(displayed.platform_key, "upwork")

    def test_unknown_platform_retains_generic_fallback(self):
        self.submit(platform="New Marketplace")
        _, displayed = self.dashboard_proposal()
        self.assertEqual(displayed.display_platform, "New Marketplace")
        self.assertEqual(displayed.application_label, "Application")
        self.assertEqual(displayed.submitted_label, "Submitted")

    def test_submitted_status_without_confirmation_keeps_original_platform_fallback(self):
        self.proposal.status = "submitted"
        self.proposal.save(update_fields=["status"])
        before = self.snapshot()
        response, displayed = self.dashboard_proposal()
        self.assertTrue(displayed.is_submitted)
        self.assertEqual(displayed.display_platform, "Upwork")
        self.assertIsNone(displayed.submitted_at)
        self.assertEqual(len(response.context["pending_outcomes"]), 1)
        self.assertEqual(self.client.get(self.outcome_url()).status_code, 200)
        self.assertEqual(self.snapshot(), before)

    def test_confirmation_with_inconsistent_generated_state_is_submission_evidence(self):
        confirmation = ProposalUseConfirmation.objects.create(proposal=self.proposal, platform="Fiverr")
        before = self.snapshot()
        response, displayed = self.dashboard_proposal()
        self.assertTrue(displayed.is_submitted)
        self.assertEqual(displayed.submitted_at, confirmation.confirmed_at)
        self.assertEqual(len(response.context["pending_outcomes"]), 1)
        self.assertEqual(self.client.get(self.outcome_url()).status_code, 200)
        self.assertEqual(self.snapshot(), before)
        self.client.post(self.outcome_url(), {"outcome_status": "reply"})
        self.proposal.refresh_from_db()
        self.assertTrue(self.proposal.used_by_user)
        self.assertEqual(self.proposal.used_at, confirmation.confirmed_at)

    def test_legacy_used_status_boolean_and_timestamp_are_submission_evidence(self):
        timestamp = timezone.now() - timedelta(days=10)
        for changes in ({"status": "used"}, {"used_by_user": True}, {"used_at": timestamp}):
            with self.subTest(changes=changes):
                Proposal.objects.filter(pk=self.proposal.pk).update(status="generated", used_by_user=False, used_at=None)
                Proposal.objects.filter(pk=self.proposal.pk).update(**changes)
                before = self.snapshot()
                response, displayed = self.dashboard_proposal()
                self.assertTrue(displayed.is_submitted)
                self.assertEqual(len(response.context["pending_outcomes"]), 1)
                self.assertEqual(self.client.get(self.outcome_url()).status_code, 200)
                self.assertEqual(self.snapshot(), before)

    def test_old_content_type_and_blank_legacy_platform_fall_back_without_rewriting(self):
        Proposal.objects.filter(pk=self.proposal.pk).update(content_type="legacy_letter", status="used")
        confirmation = ProposalUseConfirmation.objects.create(proposal=self.proposal, platform="")
        before = self.snapshot()
        _, displayed = self.dashboard_proposal()
        self.assertEqual(displayed.display_platform, "Upwork")
        self.assertEqual(displayed.generated_heading, "Your Cover Letter")
        self.assertEqual(self.client.get(self.outcome_url()).status_code, 200)
        self.assertEqual(self.snapshot(), before)
        self.submit(platform="Fiverr")
        self.assertEqual(self.proposal.content_type, "legacy_letter")
        self.assertEqual(ProposalUseConfirmation.objects.get(pk=confirmation.pk).platform, "Fiverr")

    def test_legacy_outcome_without_submission_evidence_cannot_be_edited_until_confirmation(self):
        Proposal.objects.filter(pk=self.proposal.pk).update(status="hired")
        outcome = ProposalOutcome.objects.create(proposal=self.proposal, status="hired", notes="Historical")
        before = self.snapshot()
        self.assertRedirects(self.client.get(self.outcome_url()), self.submission_url(), fetch_redirect_response=False)
        self.assertRedirects(self.client.post(self.outcome_url(), {"outcome_status": "rejected"}), self.submission_url(), fetch_redirect_response=False)
        self.assertEqual(self.snapshot(), before)
        self.submit()
        self.assertEqual(self.client.get(self.outcome_url()).status_code, 200)
        outcome.refresh_from_db()
        self.assertEqual(outcome.status, "hired")

    def test_legacy_unsafe_job_url_is_not_rendered_as_a_link(self):
        for url in ("javascript:alert(1)", "data:text/html,test", "file:///tmp/job", "ftp://example.com/job"):
            with self.subTest(url=url):
                confirmation, _ = ProposalUseConfirmation.objects.update_or_create(proposal=self.proposal, defaults={"platform": "Upwork", "job_url": url})
                before = self.snapshot()
                response = self.client.get(self.outcome_url())
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'href="' + url + '"')
                self.assertEqual(response.context["safe_job_url"], "")
                self.assertEqual(self.snapshot(), before)

    def test_missing_actual_text_is_not_presented_as_known_submitted_text(self):
        self.submit(submitted_proposal_text="")
        response = self.client.get(self.outcome_url())
        self.assertContains(response, "No submitted text recorded.")
        self.assertNotContains(response, "Generated cover letter.")
        ProposalUseConfirmation.objects.filter(proposal=self.proposal).delete()
        response = self.client.get(self.outcome_url())
        self.assertContains(response, "submitted text unavailable")
        self.assertContains(response, "Generated cover letter.")


class OutcomeTests(LifecycleTestCase):
    def test_generated_proposal_cannot_load_or_post_outcome(self):
        before = self.snapshot()
        for method in (self.client.get, self.client.post):
            response = method(self.outcome_url(), {"outcome_status": "hired", "used_by_user": "true", "status": "submitted"})
            self.assertRedirects(response, self.submission_url(), fetch_redirect_response=False)
            self.assertEqual(self.snapshot(), before)

    def test_cross_user_outcome_get_and_post_return_404(self):
        self.submit()
        before = self.snapshot()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.outcome_url()).status_code, 404)
        self.assertEqual(self.client.post(self.outcome_url(), {"outcome_status": "hired"}).status_code, 404)
        self.assertEqual(self.snapshot(), before)

    def test_every_ui_outcome_status_is_valid_and_mirrors_declared_proposal_choice(self):
        self.submit()
        used_at = self.proposal.used_at
        self.assertEqual({value for value, _ in OUTCOME_STATUS_CHOICES}, {"no_response", "reply", "interview", "hired", "rejected"})
        for value, _ in OUTCOME_STATUS_CHOICES:
            with self.subTest(value=value):
                response = self.client.post(self.outcome_url(), {"outcome_status": value, "notes": " Note "})
                self.assertEqual(response.status_code, 302)
                outcome = ProposalOutcome.objects.get(proposal=self.proposal)
                self.assertEqual(outcome.status, value)
                self.assertEqual(outcome.notes, "Note")
                self.proposal.refresh_from_db()
                self.assertEqual(self.proposal.status, value)
                self.assertEqual(self.proposal.used_at, used_at)
                self.assertTrue(self.proposal.used_by_user)
                self.assertEqual(ProposalOutcome.objects.count(), 1)

    def test_arbitrary_blank_missing_and_lifecycle_status_values_rejected(self):
        self.submit()
        for status in (None, "", " ", "unknown", "submitted", "used", "generated", "HIRED", "hired ", "x" * 51):
            with self.subTest(status=status):
                data = {} if status is None else {"outcome_status": status}
                self.invalid_outcome(data)

    def test_owner_get_prefills_saved_outcome_status_and_notes_without_writes(self):
        self.submit()
        ProposalOutcome.objects.create(proposal=self.proposal, status="interview", notes="Existing\nnotes")
        before = self.snapshot()
        response = self.client.get(self.outcome_url())
        self.assertEqual(response.context["form"]["outcome_status"].value(), "interview")
        self.assertEqual(response.context["form"]["notes"].value(), "Existing\nnotes")
        self.assertContains(response, 'value="interview" selected')
        self.assertContains(response, "Existing\nnotes")
        self.assertEqual(self.snapshot(), before)

    def test_valid_edit_and_repeated_posts_update_one_existing_outcome(self):
        self.submit()
        outcome = ProposalOutcome.objects.create(proposal=self.proposal, status="reply", notes="Old")
        for _ in range(2):
            response = self.client.post(self.outcome_url(), {"outcome_status": "hired", "notes": "New"})
            self.assertEqual(response.status_code, 302)
        edited = ProposalOutcome.objects.get(proposal=self.proposal)
        self.assertEqual(edited.pk, outcome.pk)
        self.assertEqual(edited.created_at, outcome.created_at)
        self.assertEqual(edited.notes, "New")
        self.assertEqual(edited.status, "hired")
        self.assertEqual(ProposalOutcome.objects.count(), 1)

    def test_invalid_edit_keeps_saved_outcome_and_proposal_unchanged(self):
        self.submit()
        ProposalOutcome.objects.create(proposal=self.proposal, status="reply", notes="Old")
        self.invalid_outcome({"outcome_status": "forged", "notes": "Invalid edit"})

    def test_outcome_ownership_identifiers_and_forged_status_field_are_ignored(self):
        self.submit()
        self.client.post(self.outcome_url(), {"outcome_status": "reply", "status": "hired", "proposal": self.other_proposal.pk, "proposal_id": self.other_proposal.pk, "user": self.other.pk})
        outcome = ProposalOutcome.objects.get(proposal=self.proposal)
        self.assertEqual(outcome.status, "reply")
        self.assertEqual(outcome.proposal.user_id, self.owner.pk)
        self.assertFalse(ProposalOutcome.objects.filter(proposal=self.other_proposal).exists())

    def test_optional_notes_omission_preserves_existing_notes_and_blank_clears(self):
        self.submit()
        self.client.post(self.outcome_url(), {"outcome_status": "reply"})
        outcome = ProposalOutcome.objects.get(proposal=self.proposal)
        self.assertIn(outcome.notes, (None, ""))
        outcome.notes = "Saved notes"
        outcome.save()
        self.client.post(self.outcome_url(), {"outcome_status": "interview"})
        outcome.refresh_from_db()
        self.assertEqual(outcome.notes, "Saved notes")
        self.client.post(self.outcome_url(), {"outcome_status": "interview", "notes": ""})
        outcome.refresh_from_db()
        self.assertIn(outcome.notes, (None, ""))

    def test_invalid_outcome_retains_submitted_status_and_escaped_notes(self):
        self.submit()
        html = '</textarea><script>alert("test")</script>'
        response = self.invalid_outcome({"outcome_status": "bad", "notes": html})
        self.assertEqual(response.context["form"]["outcome_status"].value(), "bad")
        self.assertEqual(response.context["form"]["notes"].value(), html)
        self.assertContains(response, escape(html))
        self.assertNotContains(response, html)
        self.assertContains(response, 'value="bad" selected disabled')

    def test_old_invalid_outcome_is_displayed_for_correction_without_get_rewrite(self):
        self.submit()
        ProposalOutcome.objects.create(proposal=self.proposal, status="old_unknown", notes="Historic notes")
        before = self.snapshot()
        response = self.client.get(self.outcome_url())
        self.assertEqual(response.context["form"]["outcome_status"].value(), "old_unknown")
        self.assertContains(response, "Historic notes")
        self.assertContains(response, "Unsupported outcome: old_unknown")
        self.assertEqual(self.snapshot(), before)
        self.client.post(self.outcome_url(), {"outcome_status": "no_response"})
        outcome = ProposalOutcome.objects.get(proposal=self.proposal)
        self.assertEqual(outcome.status, "no_response")
        self.assertEqual(outcome.notes, "Historic notes")

    def test_completed_outcome_leaves_pending_list_and_edit_link_remains_available(self):
        self.submit()
        self.client.post(self.outcome_url(), {"outcome_status": "reply"})
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["pending_outcomes"], [])
        self.assertContains(response, "Edit Outcome")
        self.assertContains(response, 'href="' + self.outcome_url() + '"')


class AtomicityAndSecurityTests(LifecycleTestCase):
    def test_submission_proposal_save_failure_rolls_back_new_confirmation(self):
        before = self.snapshot()
        with patch.object(Proposal, "save", side_effect=IntegrityError("Mock write failure")):
            with self.assertRaises(IntegrityError):
                self.client.post(self.submission_url(), self.submission_data())
        self.assertEqual(self.snapshot(), before)

    def test_submission_proposal_save_failure_rolls_back_confirmation_edit(self):
        self.submit()
        before = self.snapshot()
        with patch.object(Proposal, "save", side_effect=IntegrityError("Mock write failure")):
            with self.assertRaises(IntegrityError):
                self.client.post(self.submission_url(), self.submission_data(platform="Fiverr"))
        self.assertEqual(self.snapshot(), before)

    def test_confirmation_save_failure_leaves_proposal_unchanged(self):
        before = self.snapshot()
        with patch.object(ProposalUseConfirmation, "save", side_effect=IntegrityError("Mock write failure")):
            with self.assertRaises(IntegrityError):
                self.client.post(self.submission_url(), self.submission_data())
        self.assertEqual(self.snapshot(), before)

    def test_outcome_proposal_save_failure_rolls_back_new_outcome(self):
        self.submit()
        before = self.snapshot()
        with patch.object(Proposal, "save", side_effect=IntegrityError("Mock write failure")):
            with self.assertRaises(IntegrityError):
                self.client.post(self.outcome_url(), {"outcome_status": "hired"})
        self.assertEqual(self.snapshot(), before)

    def test_outcome_proposal_save_failure_rolls_back_existing_outcome_edit(self):
        self.submit()
        ProposalOutcome.objects.create(proposal=self.proposal, status="reply", notes="Old")
        before = self.snapshot()
        with patch.object(Proposal, "save", side_effect=IntegrityError("Mock write failure")):
            with self.assertRaises(IntegrityError):
                self.client.post(self.outcome_url(), {"outcome_status": "hired", "notes": "New"})
        self.assertEqual(self.snapshot(), before)

    def test_outcome_save_failure_leaves_proposal_unchanged(self):
        self.submit()
        before = self.snapshot()
        with patch.object(ProposalOutcome, "save", side_effect=IntegrityError("Mock write failure")):
            with self.assertRaises(IntegrityError):
                self.client.post(self.outcome_url(), {"outcome_status": "hired"})
        self.assertEqual(self.snapshot(), before)

    def test_both_forms_exclude_all_ownership_and_lifecycle_fields(self):
        self.assertEqual(set(SubmissionConfirmationForm().fields), {"platform", "client_name", "job_url", "submitted_proposal_text", "notes"})
        self.assertEqual(set(ProposalOutcomeForm().fields), {"outcome_status", "notes"})

    def test_anonymous_requests_redirect_to_login_without_writes(self):
        self.client.logout()
        before = self.snapshot()
        for url in (self.submission_url(), self.outcome_url()):
            for method in (self.client.get, self.client.post):
                response = method(url)
                self.assertRedirects(response, reverse("login") + "?next=" + url, fetch_redirect_response=False)
        self.assertEqual(self.snapshot(), before)

    def test_nonexistent_ids_return_404_for_get_and_post(self):
        before = self.snapshot()
        for name in ("confirm_use_proposal", "update_outcome"):
            url = reverse(name, args=[999999])
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, self.submission_data()).status_code, 404)
        self.assertEqual(self.snapshot(), before)

    def test_csrf_remains_enabled_on_submission_and_outcome_forms(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        before = self.snapshot()
        response = client.get(self.submission_url())
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertEqual(client.post(self.submission_url(), self.submission_data()).status_code, 403)
        self.assertEqual(self.snapshot(), before)
        token = client.cookies["csrftoken"].value
        response = client.post(self.submission_url(), self.submission_data(), HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 302)
        response = client.get(self.outcome_url())
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        before = self.snapshot()
        self.assertEqual(client.post(self.outcome_url(), {"outcome_status": "hired"}).status_code, 403)
        self.assertEqual(self.snapshot(), before)
        response = client.post(self.outcome_url(), {"outcome_status": "hired"}, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
        self.assertEqual(response.status_code, 302)

    def test_dashboard_submission_and_outcome_text_remain_escaped(self):
        html = '<script>alert("test")</script>'
        self.submit(client_name=html, submitted_proposal_text=html, notes=html)
        self.client.post(self.outcome_url(), {"outcome_status": "reply", "notes": html})
        for url in (self.submission_url(), self.outcome_url()):
            response = self.client.get(url)
            self.assertContains(response, escape(html))
            self.assertNotContains(response, html)
        JobPost.objects.filter(pk=self.job.pk).update(platform=html)
        ProposalUseConfirmation.objects.filter(proposal=self.proposal).update(platform=html)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, escape(html))
        self.assertNotContains(response, html)

    def test_get_and_head_requests_never_persist(self):
        self.submit()
        ProposalOutcome.objects.create(proposal=self.proposal, status="reply", notes="Notes")
        before = self.snapshot()
        for url in (self.submission_url(), self.outcome_url(), reverse("dashboard")):
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.head(url).status_code, 200)
            self.assertEqual(self.snapshot(), before)

    def test_other_http_methods_do_not_change_records(self):
        self.submit()
        before = self.snapshot()
        for url in (self.submission_url(), self.outcome_url(), reverse("dashboard")):
            for method in ("put", "patch", "delete"):
                self.assertEqual(getattr(self.client, method)(url, {}).status_code, 405)
        self.assertEqual(self.snapshot(), before)
