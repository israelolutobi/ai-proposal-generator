from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape
from openai import OpenAIError

from proposal_ai.forms import JobConfirmationForm, JobExtractionForm, JobPasteForm
from proposal_ai.models import FreelancerProfile, JobPost, Proposal, WorkExperience


User = get_user_model()
EDITABLE_FIELDS = (
    "platform", "job_title", "job_description", "budget_type", "hourly_min", "hourly_max",
    "fixed_budget", "experience_level", "project_duration", "hours_per_week", "skills_required",
    "client_location", "proposal_count", "interviewing_count", "invites_sent",
)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    DEBUG=False,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class JobValidationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="job-owner")
        cls.other = User.objects.create_user(username="other-job-owner")
        FreelancerProfile.objects.create(user=cls.owner, professional_title="Django Developer")
        FreelancerProfile.objects.create(user=cls.other, professional_title="OTHER_PRIVATE_CONTEXT")
        WorkExperience.objects.create(user=cls.other, job_title="OTHER_PRIVATE_CONTEXT", tasks="Private tasks")

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.provider = Mock()
        self.output("Mock generated — application")
        client_patch = patch("proposal_ai.views.get_openai_client", return_value=self.provider)
        self.get_client = client_patch.start()
        self.addCleanup(client_patch.stop)
        sdk_patch = patch("proposal_ai.views.OpenAI", side_effect=AssertionError("No live AI client allowed."))
        sdk_patch.start()
        self.addCleanup(sdk_patch.stop)

    def output(self, content):
        self.provider.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    def raw_text(self):
        return "Looking for a Django developer for an hourly project.\n" + "Build reliable application views. " * 20

    def job_data(self, **changes):
        data = {
            "platform": "Upwork", "job_title": "Django Project",
            "job_description": "Build and test Django application views.", "budget_type": "Hourly",
            "hourly_min": "25.00", "hourly_max": "50.00", "fixed_budget": "",
            "experience_level": "Intermediate", "project_duration": "One month",
            "hours_per_week": "Less than 30", "skills_required": "Django, Python",
            "client_location": "UK", "proposal_count": "10 to 15",
            "interviewing_count": "2", "invites_sent": "0",
        }
        data.update(changes)
        return data

    def stored_data(self):
        return (list(JobPost.objects.order_by("pk").values()), list(Proposal.objects.order_by("pk").values()))


class JobPasteExtractionTests(JobValidationTestCase):
    def post_extraction(self, content, **post_changes):
        self.output(content)
        return self.client.post(reverse("extract_job_features"), {"raw_job_text": self.raw_text(), **post_changes})

    def assert_extraction_failure(self, content):
        before = self.stored_data()
        response = self.post_extraction(content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_data(), before)
        self.assertContains(response, escape("We couldn't extract valid job details."))
        self.assertEqual(response.context["form"]["raw_job_text"].value(), self.raw_text())
        self.assertContains(response, escape(self.raw_text()))
        return response

    def test_valid_extraction_saves_validated_owned_job_and_retains_multiline_text(self):
        raw = " \n" + self.raw_text() + " \t"
        response = self.post_extraction(json.dumps(self.job_data()), raw_job_text=raw)
        job = JobPost.objects.get()
        self.assertRedirects(response, reverse("confirm_job_features", args=[job.pk]))
        self.assertEqual(job.user_id, self.owner.pk)
        self.assertEqual(job.raw_job_text, raw.strip())
        self.assertIn("\n", job.raw_job_text)
        self.assertEqual(job.hourly_min, Decimal("25.00"))
        self.assertFalse(job.confirmed_by_user)
        self.assertEqual(Proposal.objects.count(), 0)
        self.provider.chat.completions.create.assert_called_once()
        self.assertEqual(self.provider.chat.completions.create.call_args.kwargs["model"], "gpt-5")

    def test_empty_missing_and_whitespace_paste_rejected_even_with_warning_bypass(self):
        for value in (None, "", " \t\n "):
            for bypass in (False, True):
                with self.subTest(missing=value is None, bypass=bypass):
                    data = {} if value is None else {"raw_job_text": value}
                    if bypass:
                        data["continue_anyway"] = "true"
                    response = self.client.post(reverse("extract_job_features"), data)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context["form"].errors.as_data()["raw_job_text"][0].code, "required")
                    self.assertEqual(JobPost.objects.count(), 0)
                    self.get_client.assert_not_called()

    def test_get_with_text_displays_unbound_form_without_persistence_or_ai(self):
        response = self.client.get(reverse("extract_job_features"), {"raw_job_text": self.raw_text()})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["form"].is_bound)
        self.assertEqual(JobPost.objects.count(), 0)
        self.get_client.assert_not_called()

    def test_completeness_warning_does_not_call_ai_or_save_and_continue_still_works(self):
        raw = "A short opportunity"
        response = self.client.post(reverse("extract_job_features"), {"raw_job_text": raw})
        self.assertContains(response, "Job Validation Report")
        self.assertContains(response, "Continue Anyway")
        self.assertEqual(response.context["form"]["raw_job_text"].value(), raw)
        self.assertEqual(JobPost.objects.count(), 0)
        self.get_client.assert_not_called()
        self.output(json.dumps(self.job_data()))
        response = self.client.post(reverse("extract_job_features"), {"raw_job_text": raw, "continue_anyway": "true"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(JobPost.objects.get().raw_job_text, raw)

    def test_paste_form_has_no_invented_length_limit(self):
        form = JobPasteForm({"raw_job_text": "Developer project\n" + "legitimate detail " * 5000})
        self.assertTrue(form.is_valid())
        self.assertIsNone(form.fields["raw_job_text"].max_length)
        self.get_client.assert_not_called()

    def test_missing_optional_extracted_fields_are_safely_optional(self):
        response = self.post_extraction(json.dumps({"job_title": "Title", "job_description": "Description"}))
        self.assertEqual(response.status_code, 302)
        job = JobPost.objects.get()
        for field in set(EDITABLE_FIELDS) - {"job_title", "job_description"}:
            self.assertIn(getattr(job, field), (None, ""))

    def test_optional_null_extracted_fields_are_accepted(self):
        data = {field: None for field in EDITABLE_FIELDS}
        data.update(job_title="Title", job_description="Description")
        self.assertEqual(self.post_extraction(json.dumps(data)).status_code, 302)

    def test_extracted_ownership_raw_text_and_confirmation_flags_are_ignored(self):
        data = self.job_data(user=self.other.pk, user_id=self.other.pk, owner=self.other.pk,
                             raw_job_text="replaced", confirmed_by_user=True)
        self.assertEqual(self.post_extraction(json.dumps(data), user=self.other.pk).status_code, 302)
        job = JobPost.objects.get()
        self.assertEqual(job.user_id, self.owner.pk)
        self.assertEqual(job.raw_job_text, self.raw_text().strip())
        self.assertFalse(job.confirmed_by_user)

    def test_malformed_json_is_rejected_without_rendering_raw_output(self):
        marker = 'PRIVATE_PROVIDER_DETAIL </textarea><script>alert("test")</script>'
        response = self.assert_extraction_failure(marker)
        self.assertNotContains(response, "PRIVATE_PROVIDER_DETAIL")
        self.assertNotContains(response, "<script>")

    def test_all_non_object_json_roots_are_rejected(self):
        for value in ([], "text", 12, 1.25, True, False, None):
            with self.subTest(root_type=type(value).__name__):
                self.assert_extraction_failure(json.dumps(value))

    def test_duplicate_json_fields_are_rejected_instead_of_silently_overwritten(self):
        self.assert_extraction_failure(
            '{"job_title":"First","job_title":"Second","job_description":"Description"}'
        )

    def test_excessively_nested_json_fails_cleanly(self):
        self.assert_extraction_failure("[" * 2000 + "0" + "]" * 2000)

    def test_empty_or_non_text_output_is_rejected(self):
        for value in (None, "", " \n ", [], 12, True):
            with self.subTest(output_type=type(value).__name__):
                self.assert_extraction_failure(value)

    def test_missing_blank_whitespace_or_null_required_ai_fields_never_use_fake_defaults(self):
        for field in ("job_title", "job_description"):
            for value in (None, "", " \t ", "missing"):
                with self.subTest(field=field, value_kind="missing" if value == "missing" else "empty"):
                    data = self.job_data()
                    if value == "missing":
                        data.pop(field)
                    else:
                        data[field] = value
                    self.assert_extraction_failure(json.dumps(data))

    def test_wrong_types_for_every_text_field_are_rejected(self):
        fields = set(EDITABLE_FIELDS) - {"hourly_min", "hourly_max", "fixed_budget"}
        for field in sorted(fields):
            for value in ([], {}, 15, True):
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.assert_extraction_failure(json.dumps(self.job_data(**{field: value})))

    def test_wrong_budget_types_invalid_decimals_and_precision_are_rejected(self):
        for field in ("hourly_min", "hourly_max", "fixed_budget"):
            for value in ([], {}, True, False, "not-a-number", "$500", "NaN", "Infinity", "12.345", "1e100"):
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.assert_extraction_failure(json.dumps(self.job_data(**{field: value})))

    def test_nonstandard_json_numbers_are_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            self.assert_extraction_failure('{"job_title":"Title","job_description":"Description","fixed_budget":' + value + '}')

    def test_negative_budgets_and_reversed_hourly_range_are_rejected(self):
        for field in ("hourly_min", "hourly_max", "fixed_budget"):
            with self.subTest(field=field):
                self.assert_extraction_failure(json.dumps(self.job_data(**{field: "-1"})))
        self.assert_extraction_failure(json.dumps(self.job_data(hourly_min="50", hourly_max="25")))

    def test_model_text_max_lengths_are_enforced_before_saving(self):
        for field in EDITABLE_FIELDS:
            limit = JobPost._meta.get_field(field).max_length
            if limit:
                with self.subTest(field=field):
                    self.assert_extraction_failure(json.dumps(self.job_data(**{field: "x" * (limit + 1)})))

    def test_json_numeric_budgets_and_zero_values_preserve_decimal_precision(self):
        content = json.dumps(self.job_data(hourly_min=0, hourly_max=0, fixed_budget=125.25))
        self.assertEqual(self.post_extraction(content).status_code, 302)
        job = JobPost.objects.get()
        self.assertEqual(job.hourly_min, Decimal("0.00"))
        self.assertEqual(job.hourly_max, Decimal("0.00"))
        self.assertEqual(job.fixed_budget, Decimal("125.25"))

    def test_mocked_provider_errors_are_controlled_and_preserve_original_text(self):
        for error in (TimeoutError("PRIVATE_PROVIDER_DETAIL"), OpenAIError("PRIVATE_PROVIDER_DETAIL"),
                      RuntimeError("PRIVATE_PROVIDER_DETAIL")):
            with self.subTest(error_type=type(error).__name__):
                self.provider.chat.completions.create.side_effect = error
                response = self.client.post(reverse("extract_job_features"), {"raw_job_text": self.raw_text()})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, escape("We couldn't extract valid job details."))
                self.assertNotContains(response, "PRIVATE_PROVIDER_DETAIL")
                self.assertEqual(response.context["form"]["raw_job_text"].value(), self.raw_text())
                self.assertEqual(JobPost.objects.count(), 0)

    def test_malformed_provider_response_structure_is_controlled(self):
        for response_data in (None, SimpleNamespace(choices=[]), SimpleNamespace(choices=[SimpleNamespace()])):
            self.provider.chat.completions.create.return_value = response_data
            response = self.client.post(reverse("extract_job_features"), {"raw_job_text": self.raw_text()})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, escape("We couldn't extract valid job details."))
            self.assertEqual(JobPost.objects.count(), 0)

    def test_failed_extraction_escapes_retained_raw_html_and_keeps_continue_button(self):
        raw = '</textarea><script>alert("test")</script>'
        response = self.post_extraction("invalid JSON", raw_job_text=raw, continue_anyway="true")
        self.assertContains(response, escape(raw))
        self.assertNotContains(response, raw)
        self.assertContains(response, "Continue Anyway")
        self.assertEqual(JobPost.objects.count(), 0)


class JobConfirmationTests(JobValidationTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.job = JobPost.objects.create(user=cls.owner, raw_job_text="Original pasted text",
                                        job_title="Original title", job_description="Original description")
        cls.other_job = JobPost.objects.create(user=cls.other, job_title="Other private title",
                                              job_description="Other private description")

    def confirm_url(self, pk=None):
        return reverse("confirm_job_features", args=[pk if pk is not None else self.job.pk])

    def assert_invalid_confirmation(self, data, field):
        before = self.stored_data()
        response = self.client.post(self.confirm_url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_data(), before)
        self.assertIn(field, response.context["form"].errors)
        self.assertContains(response, 'role="alert"')
        for message in response.context["form"].errors[field]:
            self.assertContains(response, escape(message))
        self.get_client.assert_not_called()
        self.provider.chat.completions.create.assert_not_called()
        return response

    def test_owner_get_loads_existing_values_without_save_or_provider_call(self):
        before = self.stored_data()
        response = self.client.get(self.confirm_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="Original title"')
        self.assertContains(response, "Original description")
        self.assertFalse(response.context["form"].is_bound)
        self.assertEqual(self.stored_data(), before)
        self.get_client.assert_not_called()

    def test_valid_confirmation_updates_only_owner_job_and_generates_existing_application_workflow(self):
        other_before = JobPost.objects.filter(pk=self.other_job.pk).values().get()
        data = self.job_data(user=self.other.pk, user_id=self.other.pk, owner=self.other.pk,
                             id=self.other_job.pk, raw_job_text="Changed raw text", confirmed_by_user=False)
        response = self.client.post(self.confirm_url(), data)
        self.assertRedirects(response, reverse("dashboard"))
        self.job.refresh_from_db()
        self.assertEqual(self.job.user_id, self.owner.pk)
        self.assertEqual(self.job.raw_job_text, "Original pasted text")
        self.assertTrue(self.job.confirmed_by_user)
        self.assertEqual(self.job.job_title, data["job_title"])
        self.assertEqual(self.job.hourly_min, Decimal("25.00"))
        self.assertEqual(JobPost.objects.filter(pk=self.other_job.pk).values().get(), other_before)
        proposal = Proposal.objects.get()
        self.assertEqual(proposal.user_id, self.owner.pk)
        self.assertEqual(proposal.job_post_id, self.job.pk)
        self.assertEqual(proposal.content_type, "cover_letter")
        self.assertEqual(proposal.status, "generated")
        self.assertEqual(proposal.final_text, "Mock generated - application")
        call = self.provider.chat.completions.create.call_args.kwargs
        self.assertEqual(call["model"], "gpt-5")
        self.assertEqual([message["role"] for message in call["messages"]], ["system", "user"])
        self.assertIn(data["job_description"], call["messages"][1]["content"])
        self.assertNotIn("OTHER_PRIVATE_CONTEXT", call["messages"][1]["content"])

    def test_another_user_cannot_get_or_post_job_by_id(self):
        before = self.stored_data()
        self.assertEqual(self.client.get(self.confirm_url(self.other_job.pk)).status_code, 404)
        self.assertEqual(self.client.post(self.confirm_url(self.other_job.pk), self.job_data()).status_code, 404)
        self.assertEqual(self.stored_data(), before)
        self.get_client.assert_not_called()

    def test_nonexistent_job_returns_404_without_generation(self):
        url = self.confirm_url(self.other_job.pk + 1000)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, self.job_data()).status_code, 404)
        self.get_client.assert_not_called()

    def test_missing_profile_preserves_existing_redirect_without_saving_or_generation(self):
        FreelancerProfile.objects.filter(user=self.owner).delete()
        before = self.stored_data()
        response = self.client.post(self.confirm_url(), self.job_data())
        self.assertRedirects(response, reverse("create_freelancer_profile"))
        self.assertEqual(self.stored_data(), before)
        self.get_client.assert_not_called()

    def test_required_fields_reject_missing_blank_and_whitespace(self):
        for field in ("job_title", "job_description"):
            for value in (None, "", " \t\n "):
                with self.subTest(field=field, missing=value is None):
                    data = self.job_data()
                    if value is None:
                        data.pop(field)
                    else:
                        data[field] = value
                    self.assert_invalid_confirmation(data, field)

    def test_invalid_nonfinite_or_overprecise_decimals_never_save_or_generate(self):
        for field in ("hourly_min", "hourly_max", "fixed_budget"):
            for value in ("bad", "$25", "NaN", "Infinity", "12.345", "1e100"):
                with self.subTest(field=field, value=value):
                    self.assert_invalid_confirmation(self.job_data(**{field: value}), field)

    def test_negative_budgets_never_save_or_generate(self):
        for field in ("hourly_min", "hourly_max", "fixed_budget"):
            with self.subTest(field=field):
                self.assert_invalid_confirmation(self.job_data(**{field: "-0.01"}), field)

    def test_reversed_hourly_range_never_saves_or_generates(self):
        self.assert_invalid_confirmation(self.job_data(hourly_min="50", hourly_max="25"), "hourly_max")

    def test_model_text_max_lengths_never_save_or_generate(self):
        for field in EDITABLE_FIELDS:
            limit = JobPost._meta.get_field(field).max_length
            if limit:
                with self.subTest(field=field):
                    self.assert_invalid_confirmation(self.job_data(**{field: "x" * (limit + 1)}), field)

    def test_free_text_schema_accepts_unknown_platforms_budget_labels_and_text_counts(self):
        # JobPost has no choice fields. Do not invent a restrictive enum or
        # turn textual counts such as "10 to 15" into numeric fields.
        self.assertFalse(any(JobPost._meta.get_field(field).choices for field in EDITABLE_FIELDS))
        data = self.job_data(platform="New Marketplace", budget_type="Another stated budget", proposal_count="10 to 15")
        response = self.client.post(self.confirm_url(), data)
        self.assertEqual(response.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.platform, data["platform"])
        self.assertEqual(self.job.budget_type, data["budget_type"])
        self.assertEqual(Proposal.objects.get().content_type, "application_message")

    def test_optional_fields_can_be_omitted_and_required_text_is_trimmed(self):
        response = self.client.post(self.confirm_url(), {"job_title": "  Title  ", "job_description": "  Description  "})
        self.assertEqual(response.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.job_title, "Title")
        self.assertEqual(self.job.job_description, "Description")
        self.assertIsNone(self.job.hourly_min)
        self.assertIsNone(self.job.fixed_budget)

    def test_zero_and_equal_hourly_ranges_are_valid_and_zero_redisplays_on_get(self):
        response = self.client.post(self.confirm_url(), self.job_data(hourly_min="0", hourly_max="0", fixed_budget="0"))
        self.assertEqual(response.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.fixed_budget, Decimal("0.00"))
        response = self.client.get(self.confirm_url())
        self.assertContains(response, 'value="0.00"')

    def test_invalid_form_retains_submitted_values_and_escapes_html(self):
        html = '</textarea><script>alert("test")</script>'
        response = self.assert_invalid_confirmation(self.job_data(job_description=html, hourly_min="bad"), "hourly_min")
        self.assertEqual(response.context["form"]["job_description"].value(), html)
        self.assertContains(response, escape(html))
        self.assertNotContains(response, html)
        self.assertContains(response, 'value="bad"')

    def test_provider_failure_does_not_save_validated_job_or_proposal(self):
        before = self.stored_data()
        self.provider.chat.completions.create.side_effect = OpenAIError("Mock generation failure")
        with self.assertRaises(OpenAIError):
            self.client.post(self.confirm_url(), self.job_data())
        self.assertEqual(self.stored_data(), before)

    def test_proposal_save_failure_rolls_back_the_confirmed_job_write(self):
        before = self.stored_data()
        with patch("proposal_ai.views.Proposal.objects.create", side_effect=IntegrityError("Mock database failure")):
            with self.assertRaises(IntegrityError):
                self.client.post(self.confirm_url(), self.job_data())
        self.assertEqual(self.stored_data(), before)

    def test_external_generation_occurs_before_any_confirmation_write(self):
        before = self.stored_data()
        response = self.provider.chat.completions.create.return_value
        def verify_before_call(**kwargs):
            self.assertEqual(self.stored_data(), before)
            return response
        self.provider.chat.completions.create.side_effect = verify_before_call
        self.assertEqual(self.client.post(self.confirm_url(), self.job_data()).status_code, 302)


class JobWorkflowSecurityTests(JobValidationTestCase):
    def test_forms_expose_only_editable_job_fields(self):
        self.assertEqual(set(JobConfirmationForm().fields), set(EDITABLE_FIELDS))
        self.assertEqual(set(JobExtractionForm().fields), set(EDITABLE_FIELDS))
        self.assertEqual(set(JobPasteForm().fields), {"raw_job_text"})

    def test_anonymous_get_and_post_redirect_to_real_login_without_writes_or_ai(self):
        job = JobPost.objects.create(user=self.owner, job_title="Title", job_description="Description")
        client = Client()
        before = self.stored_data()
        for url in (reverse("extract_job_features"), reverse("confirm_job_features", args=[job.pk])):
            for method in ("get", "post"):
                response = getattr(client, method)(url)
                self.assertRedirects(response, reverse("login") + "?next=" + url, fetch_redirect_response=False)
        self.assertEqual(self.stored_data(), before)
        self.get_client.assert_not_called()

    def test_csrf_tokens_middleware_and_rejection_remain_enabled(self):
        self.assertIn("django.middleware.csrf.CsrfViewMiddleware", settings.MIDDLEWARE)
        job = JobPost.objects.create(user=self.owner, job_title="Title", job_description="Description")
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        before = self.stored_data()
        for url in (reverse("extract_job_features"), reverse("confirm_job_features", args=[job.pk])):
            self.assertContains(client.get(url, secure=True), 'name="csrfmiddlewaretoken"')
            self.assertEqual(client.post(url, self.job_data(), secure=True).status_code, 403)
        self.assertEqual(self.stored_data(), before)
        self.get_client.assert_not_called()

    def test_valid_csrf_posts_complete_the_existing_extraction_confirmation_flow(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        url = reverse("extract_job_features")
        client.get(url, secure=True)
        self.output(json.dumps(self.job_data()))
        response = client.post(url, {"raw_job_text": self.raw_text(), "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
                               secure=True, HTTP_REFERER="https://testserver" + url)
        self.assertEqual(response.status_code, 302)
        job = JobPost.objects.get()
        url = reverse("confirm_job_features", args=[job.pk])
        client.get(url, secure=True)
        self.output("Mock application")
        response = client.post(url, {**self.job_data(), "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
                               secure=True, HTTP_REFERER="https://testserver" + url)
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(Proposal.objects.count(), 1)

    def test_other_http_methods_do_not_persist_or_call_ai(self):
        job = JobPost.objects.create(user=self.owner, job_title="Title", job_description="Description")
        before = self.stored_data()
        for url in (reverse("extract_job_features"), reverse("confirm_job_features", args=[job.pk])):
            for method in ("put", "patch", "delete"):
                self.assertEqual(getattr(self.client, method)(url, self.job_data()).status_code, 405)
        self.assertEqual(self.stored_data(), before)
        self.get_client.assert_not_called()
