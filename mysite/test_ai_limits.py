"""Reviewed Beta budgets; no real provider or user database is used."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from proposal_ai import ai_limits as limits, services
from proposal_ai.forms import (
    JobConfirmationForm, JobExtractionForm, JobPasteForm,
    ProfileSummaryGenerationForm, WorkExperienceForm,
)
from proposal_ai.models import FreelancerProfile, JobPost, Proposal, WorkExperience


def response(text="Generated text", finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish, message=SimpleNamespace(content=text),
    )])


class InputBoundaryTests(SimpleTestCase):
    def test_paste_exactly_at_limit(self):
        self.assertTrue(JobPasteForm({"raw_job_text": "x" * 20000}).is_valid())

    def test_paste_one_over_limit(self):
        form = JobPasteForm({"raw_job_text": "x" * 20001})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors.as_data()["raw_job_text"][0].code, "max_length")

    def test_summary_title_exactly_at_limit(self):
        self.assertTrue(ProfileSummaryGenerationForm({"professional_title": "x" * 255, "key_skills": "Django"}).is_valid())

    def test_summary_title_one_over_limit(self):
        form = ProfileSummaryGenerationForm({"professional_title": "x" * 256, "key_skills": "Django"})
        self.assertFalse(form.is_valid())
        self.assertIn("professional_title", form.errors)

    def test_summary_skills_exactly_at_limit(self):
        self.assertTrue(ProfileSummaryGenerationForm({"professional_title": "Developer", "key_skills": "x" * 2000}).is_valid())

    def test_summary_skills_one_over_limit(self):
        form = ProfileSummaryGenerationForm({"professional_title": "Developer", "key_skills": "x" * 2001})
        self.assertFalse(form.is_valid())
        self.assertIn("key_skills", form.errors)

    def test_outer_whitespace_does_not_use_form_budget(self):
        form = ProfileSummaryGenerationForm({"professional_title": " \n" + "x" * 255 + " \t", "key_skills": " " + "x" * 2000 + " "})
        self.assertTrue(form.is_valid())
        self.assertEqual(len(form.cleaned_data["professional_title"]), 255)
        paste = JobPasteForm({"raw_job_text": " \n" + "x" * 20000 + " \t"})
        self.assertTrue(paste.is_valid())

    def test_unicode_and_non_bmp_are_counted_as_python_characters(self):
        for character in ("界", "😀", "é", "e\u0301"):
            # Combining sequences retain their actual code-point count.
            value = (character * 20000)[:20000]
            with self.subTest(character=character):
                self.assertTrue(JobPasteForm({"raw_job_text": value}).is_valid())
                self.assertFalse(JobPasteForm({"raw_job_text": value + "x"}).is_valid())

    def test_job_description_and_skills_boundaries(self):
        for field, maximum in (("job_description", 20000), ("skills_required", 2000)):
            with self.subTest(field=field):
                data = {"job_title": "Job", "job_description": "Description", field: "x" * maximum}
                self.assertTrue(JobConfirmationForm(data).is_valid())
                data[field] += "x"
                self.assertFalse(JobConfirmationForm(data).is_valid())
                with self.assertRaises(ValidationError):
                    JobExtractionForm.from_json(json.dumps(data))

    def test_stored_profile_field_boundaries(self):
        profile = SimpleNamespace(professional_title="Developer", profile_summary="Summary", preferred_tone="professional")
        for field, (_, maximum) in limits.PROFILE_FIELD_LIMITS.items():
            with self.subTest(field=field):
                setattr(profile, field, "x" * maximum)
                limits.check_fields(profile, limits.PROFILE_FIELD_LIMITS)
                setattr(profile, field, "x" * (maximum + 1))
                with self.assertRaises(ValidationError) as caught:
                    limits.check_fields(profile, limits.PROFILE_FIELD_LIMITS)
                self.assertIn("stored record can remain", str(caught.exception))
                setattr(profile, field, "x")

    def test_experience_field_boundaries(self):
        experience = SimpleNamespace(job_title="Role", company_or_project="Project", tasks="Tasks", skills_used="Skills", experience_depth="Depth")
        for field, (_, maximum) in limits.EXPERIENCE_FIELD_LIMITS.items():
            with self.subTest(field=field):
                setattr(experience, field, "x" * maximum)
                limits.experience_context([experience])
                setattr(experience, field, "x" * (maximum + 1))
                with self.assertRaises(ValidationError):
                    limits.experience_context([experience])
                setattr(experience, field, "x")

    def test_formatted_context_budget_boundaries(self):
        for maximum in (limits.PROFILE_CONTEXT_CHARACTERS, limits.EXPERIENCE_CHARACTERS,
                        limits.EXPERIENCE_CONTEXT_CHARACTERS, limits.JOB_CONTEXT_CHARACTERS):
            with self.subTest(maximum=maximum):
                limits.check_characters("x" * maximum, maximum, "Context")
                with self.assertRaises(ValidationError):
                    limits.check_characters("x" * (maximum + 1), maximum, "Context")

    def test_experience_context_total_exactly_at_limit_and_one_over(self):
        records = [SimpleNamespace(job_title="x", company_or_project="", tasks="x" * 3800,
                                   skills_used="", experience_depth="") for _ in range(5)]
        current = len(limits.experience_context(records))
        records[-1].skills_used = "x" * (20000 - current)
        self.assertEqual(len(limits.experience_context(records)), 20000)
        records[-1].skills_used += "x"
        with self.assertRaises(ValidationError):
            limits.experience_context(records)

    def test_maximum_unicode_extraction_schema_fits_raw_ceiling(self):
        form = JobExtractionForm()
        data = {name: ("😀" * field.max_length if field.max_length else "")
                for name, field in form.fields.items() if name not in ("hourly_min", "hourly_max", "fixed_budget")}
        data.update(hourly_min="999999.99", hourly_max="999999.99", fixed_budget="99999999.99")
        text = json.dumps(data, ensure_ascii=True, indent=4)
        self.assertLess(len(text), limits.EXTRACTION_RESPONSE_CHARACTERS)
        self.assertTrue(JobExtractionForm.from_json(text).is_valid())

    def test_oversized_raw_extraction_rejected_before_json_parser(self):
        with patch("proposal_ai.forms.json.loads") as parser:
            with self.assertRaises(ValidationError):
                JobExtractionForm.from_json("x" * (limits.EXTRACTION_RESPONSE_CHARACTERS + 1))
            parser.assert_not_called()


@override_settings(OPENAI_API_KEY="test-only-not-a-credential")
class ServiceBudgetTests(SimpleTestCase):
    def setUp(self):
        self.sdk = patch("proposal_ai.services.OpenAI", autospec=True)
        self.constructor = self.sdk.start()
        self.addCleanup(self.sdk.stop)
        self.client = MagicMock()
        self.constructor.return_value.__enter__.return_value = self.client
        self.client.chat.completions.create.return_value = response()

    def call(self, operation, text):
        if operation == "summary":
            return services.generate_profile_summary(text)
        if operation == "extraction":
            return services.extract_job_details(text)
        return services.generate_proposal("", text)

    def test_each_request_exactly_at_character_ceiling(self):
        for operation, maximum in limits.REQUEST_CHARACTERS.items():
            with self.subTest(operation=operation):
                self.assertEqual(self.call(operation, "x" * maximum), "Generated text")

    def test_each_request_one_over_rejected_before_client_creation(self):
        for operation, maximum in limits.REQUEST_CHARACTERS.items():
            with self.subTest(operation=operation):
                with self.assertRaises(services.AIInputError):
                    self.call(operation, "x" * (maximum + 1))
        self.constructor.assert_not_called()

    def test_proposal_guard_sums_both_messages_including_spaces(self):
        services.generate_proposal(" " * 10000, "x" * 50000)
        self.constructor.reset_mock()
        with self.assertRaises(services.AIInputError):
            services.generate_proposal(" " * 10001, "x" * 50000)
        self.constructor.assert_not_called()

    def test_utf8_backstop_exactly_at_limit(self):
        self.assertEqual(services.generate_proposal("", "😀" * 32000), "Generated text")

    def test_utf8_backstop_one_byte_over_before_client_creation(self):
        with self.assertRaises(services.AIInputError):
            services.generate_proposal("", "😀" * 32000 + "x")
        self.constructor.assert_not_called()

    def test_invalid_unicode_is_controlled_without_logging_or_client_creation(self):
        with self.assertNoLogs("proposal_ai.services"):
            with self.assertRaises(services.AIInputError):
                services.generate_profile_summary("\ud800")
        self.constructor.assert_not_called()

    def test_all_three_exact_provider_caps(self):
        for operation, expected in (("summary", 2048), ("extraction", 8192), ("proposal", 6144)):
            with self.subTest(operation=operation):
                self.call(operation, "Prompt")
                self.assertEqual(self.client.chat.completions.create.call_args.kwargs["max_completion_tokens"], expected)
                self.assertNotIn("reasoning_effort", self.client.chat.completions.create.call_args.kwargs)

    def test_installed_sdk_serializes_each_cap_on_the_unchanged_endpoint(self):
        for operation, expected in (("summary", 2048), ("extraction", 8192), ("proposal", 6144)):
            seen = []
            def handler(request):
                seen.append(request)
                self.assertEqual(request.url.path, "/v1/chat/completions")
                body = json.loads(request.content)
                self.assertEqual(body["model"], "gpt-5")
                self.assertEqual(body["max_completion_tokens"], expected)
                self.assertNotIn("reasoning_effort", body)
                return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
                    "model": "gpt-5", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Generated text"}}]})
            def client(**kwargs):
                return openai.OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            with self.subTest(operation=operation), patch("proposal_ai.services.OpenAI", side_effect=client):
                self.assertEqual(self.call(operation, "Prompt"), "Generated text")
            self.assertEqual(len(seen), 1)

    def test_length_finish_reason_rejected_for_every_active_operation_without_retry(self):
        self.client.chat.completions.create.return_value = response("Partial content", "length")
        for operation in limits.REQUEST_CHARACTERS:
            with self.subTest(operation=operation):
                self.client.chat.completions.create.reset_mock()
                with self.assertRaises(services.AIResponseError):
                    self.call(operation, "Prompt")
                self.client.chat.completions.create.assert_called_once()

    def test_missing_or_other_finish_reasons_are_not_accepted_as_complete(self):
        for finish in (None, "content_filter", "tool_calls"):
            self.client.chat.completions.create.return_value = response("Partial", finish)
            with self.assertRaises(services.AIResponseError):
                services.generate_proposal("Instructions", "Context")

    def test_visible_output_exactly_at_limits(self):
        for operation, maximum in (("summary", 2000), ("proposal", 8000)):
            self.client.chat.completions.create.return_value = response("😀" * maximum)
            self.assertEqual(len(self.call(operation, "Prompt")), maximum)

    def test_visible_output_one_over_limits(self):
        for operation, maximum in (("summary", 2000), ("proposal", 8000)):
            self.client.chat.completions.create.return_value = response("x" * (maximum + 1))
            with self.assertRaises(services.AIResponseError):
                self.call(operation, "Prompt")

    def test_visible_output_outer_whitespace_normalized(self):
        self.client.chat.completions.create.return_value = response(" \n" + "x" * 2000 + " \t")
        self.assertEqual(len(services.generate_profile_summary("Prompt")), 2000)

    def test_empty_output_still_rejected_for_all_operations(self):
        self.client.chat.completions.create.return_value = response(" \n ")
        for operation in limits.REQUEST_CHARACTERS:
            with self.assertRaises(services.AIResponseError):
                self.call(operation, "Prompt")

    def test_raw_extraction_output_exactly_at_limit_is_not_truncated(self):
        self.client.chat.completions.create.return_value = response("x" * limits.EXTRACTION_RESPONSE_CHARACTERS)
        self.assertEqual(len(services.extract_job_details("Prompt")), limits.EXTRACTION_RESPONSE_CHARACTERS)

    def test_oversized_extraction_padding_is_rejected_before_trim(self):
        self.client.chat.completions.create.return_value = response(" " * limits.EXTRACTION_RESPONSE_CHARACTERS + "{}")
        with self.assertRaises(services.AIResponseError):
            services.extract_job_details("Prompt")


@override_settings(ALLOWED_HOSTS=["testserver"], DEBUG=False, STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class AIBudgetWorkflowTests(TestCase):
    from .ai_test_helpers import SignedAIClient
    client_class = SignedAIClient
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="budget-owner")
        cls.other = get_user_model().objects.create_user(username="budget-other")
        cls.profile = FreelancerProfile.objects.create(user=cls.owner, professional_title="Developer", profile_summary="I build applications.")
        cls.job = JobPost.objects.create(user=cls.owner, job_title="Original", job_description="Original description", raw_job_text="Original raw")

    def setUp(self):
        self.client.force_login(self.owner)
        self.patches = {}
        self.real_services = {name: getattr(services, name) for name in (
            "extract_job_details", "generate_profile_summary", "generate_proposal",
        )}
        for name in ("extract_job_details", "generate_profile_summary", "generate_proposal"):
            patched = patch("proposal_ai.views.services." + name)
            self.patches[name] = patched.start()
            self.addCleanup(patched.stop)
        self.patches["generate_profile_summary"].return_value = "Summary"
        self.patches["generate_proposal"].return_value = "Proposal"
        self.patches["extract_job_details"].return_value = json.dumps(self.job_data())

    def job_data(self, **changes):
        return {"job_title": "Updated", "job_description": "Updated description", **changes}

    def url(self):
        return reverse("confirm_job_features", args=[self.job.pk])

    def records(self):
        return tuple(list(m.objects.order_by("pk").values()) for m in (FreelancerProfile, WorkExperience, JobPost, Proposal))

    def experience(self, **changes):
        return WorkExperience.objects.create(user=self.owner, job_title="Owned evidence", tasks="Built Django applications", **changes)

    def selected_data(self, records):
        return self.job_data(experience_selection_submitted="1", selected_experiences=[str(e.pk) for e in records])

    def assert_proposal_rejected(self, data):
        before = self.records()
        response = self.client.post(self.url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(self.records(), before)
        self.patches["generate_proposal"].assert_not_called()
        return response

    def test_maximum_paste_accepted_before_provider(self):
        raw = "developer hourly " + "x" * (20000 - len("developer hourly "))
        response = self.client.post(reverse("extract_job_features"), {"raw_job_text": raw})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(JobPost.objects.exclude(pk=self.job.pk).get().raw_job_text, raw)
        self.patches["extract_job_details"].assert_called_once()

    def test_overlong_paste_never_scores_calls_provider_or_saves(self):
        raw = "x" * 20001
        before = self.records()
        with patch("proposal_ai.views.validate_job_text") as scoring:
            response = self.client.post(reverse("extract_job_features"), {"raw_job_text": raw})
            scoring.assert_not_called()
        self.assertEqual(response.context["form"]["raw_job_text"].value(), raw)
        self.assertEqual(self.records(), before)
        self.patches["extract_job_details"].assert_not_called()

    def test_continue_anyway_cannot_bypass_paste_limit(self):
        with patch("proposal_ai.views.validate_job_text") as scoring:
            response = self.client.post(reverse("extract_job_features"), {"raw_job_text": "x" * 20001, "continue_anyway": "true"})
            scoring.assert_not_called()
        self.assertTrue(response.context["form"].errors)
        self.patches["extract_job_details"].assert_not_called()

    def test_rejected_paste_is_html_escaped_and_retained(self):
        raw = '<script>alert("x")</script>' + "x" * 20000
        response = self.client.post(reverse("extract_job_features"), {"raw_job_text": raw})
        self.assertContains(response, escape(raw))
        self.assertNotContains(response, raw)

    def test_summary_valid_maximum_inputs_save_nothing(self):
        before = self.records()
        response = self.client.post(reverse("generate_profile_summary"), {"professional_title": "😀" * 255, "key_skills": "x" * 2000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.records(), before)
        self.patches["generate_profile_summary"].assert_called_once()

    def test_summary_invalid_inputs_never_call_provider_or_save(self):
        for data in ({"professional_title": "x" * 256, "key_skills": "Django"},
                     {"professional_title": "Developer", "key_skills": "x" * 2001}):
            before = self.records()
            response = self.client.post(reverse("generate_profile_summary"), data)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.records(), before)
        self.patches["generate_profile_summary"].assert_not_called()

    def test_zero_experiences_keep_existing_behavior(self):
        response = self.client.post(self.url(), self.job_data())
        self.assertEqual(response.status_code, 302)
        self.assertIn("No detailed work experiences were provided.", self.patches["generate_proposal"].call_args.args[1])

    def test_ten_fitting_experiences_are_selected_automatically(self):
        records = [self.experience(company_or_project=f"Project {i}") for i in range(10)]
        get = self.client.get(self.url())
        self.assertEqual(set(get.context["form"].initial["selected_experiences"]), {str(e.pk) for e in records})
        result = self.client.post(self.url(), self.job_data())
        self.assertEqual(result.status_code, 302)
        context = self.patches["generate_proposal"].call_args.args[1]
        for i in range(10):
            self.assertIn(f"Company / Project: Project {i}", context)

    def test_eleven_records_require_explicit_selection_not_newest_ten(self):
        for _ in range(11):
            self.experience()
        get = self.client.get(self.url())
        self.assertTrue(get.context["form"].selection_required)
        self.assertFalse(get.context["form"].initial.get("selected_experiences"))
        self.assert_proposal_rejected(self.job_data())

    def test_eleven_explicit_selections_rejected(self):
        records = [self.experience() for _ in range(11)]
        self.assert_proposal_rejected(self.selected_data(records))

    def test_duplicate_ids_rejected(self):
        record = self.experience()
        self.assert_proposal_rejected(self.selected_data([record, record]))

    def test_equivalent_duplicate_id_spellings_rejected(self):
        record = self.experience()
        self.assert_proposal_rejected(self.job_data(selected_experiences=[str(record.pk), f"0{record.pk}"]))

    def test_nonexistent_id_rejected(self):
        self.assert_proposal_rejected(self.job_data(selected_experiences=["99999999"]))

    def test_cross_user_id_rejected(self):
        record = WorkExperience.objects.create(user=self.other, job_title="Other private evidence", tasks="Private")
        result = self.assert_proposal_rejected(self.selected_data([record]))
        self.assertNotContains(result, "Other private evidence")

    def test_invalid_id_text_is_controlled(self):
        self.assert_proposal_rejected(self.job_data(selected_experiences=["not-an-id"]))

    def test_valid_explicit_subset_includes_only_selected_owned_evidence(self):
        keep = self.experience(company_or_project="CHOSEN_EVIDENCE")
        for _ in range(11):
            self.experience(company_or_project="EXCLUDED_EVIDENCE")
        before = WorkExperience.objects.count()
        result = self.client.post(self.url(), self.selected_data([keep]))
        self.assertEqual(result.status_code, 302)
        context = self.patches["generate_proposal"].call_args.args[1]
        self.assertIn("CHOSEN_EVIDENCE", context)
        self.assertNotIn("EXCLUDED_EVIDENCE", context)
        self.assertEqual(WorkExperience.objects.count(), before)

    def test_explicit_empty_selection_does_not_readd_records(self):
        self.experience()
        result = self.client.post(self.url(), self.selected_data([]))
        self.assertEqual(result.status_code, 302)
        self.assertIn("No detailed work experiences were provided.", self.patches["generate_proposal"].call_args.args[1])

    def test_overlong_experience_remains_stored_but_cannot_be_selected(self):
        record = self.experience(skills_used="x" * 1001)
        self.assert_proposal_rejected(self.selected_data([record]))
        record.refresh_from_db()
        self.assertEqual(len(record.skills_used), 1001)

    def test_long_experience_storage_form_remains_valid(self):
        form = WorkExperienceForm({"job_title": "Role", "tasks": "x" * 5000, "skills_used": "x" * 7000})
        self.assertTrue(form.is_valid())
        record = form.save(commit=False)
        record.user = self.owner
        record.save()
        self.assert_proposal_rejected(self.selected_data([record]))
        record.refresh_from_db()
        self.assertEqual(len(record.tasks), 5000)
        self.assertEqual(len(record.skills_used), 7000)

    def test_total_experience_context_overflow_rejected_without_changes(self):
        records = []
        for _ in range(6):
            record = self.experience()
            record.tasks = "x" * 4000
            record.save()
            records.append(record)
        self.assert_proposal_rejected(self.selected_data(records))

    def test_oversized_stored_profile_rejected_without_changes(self):
        self.profile.profile_summary = "x" * 5001
        self.profile.save()
        self.assert_proposal_rejected(self.job_data())

    def test_maximum_profile_and_job_fields_accepted(self):
        self.profile.professional_title = "x" * 255
        self.profile.profile_summary = "x" * 5000
        self.profile.preferred_tone = "x" * 100
        self.profile.save()
        result = self.client.post(self.url(), self.job_data(job_description="x" * 20000, skills_required="x" * 2000))
        self.assertEqual(result.status_code, 302)

    def test_overlong_confirmed_job_fields_rejected_without_changes(self):
        for name, maximum in (("job_description", 20000), ("skills_required", 2000)):
            self.assert_proposal_rejected(self.job_data(**{name: "x" * (maximum + 1)}))

    def test_oversized_extracted_field_creates_no_job(self):
        self.patches["extract_job_details"].return_value = json.dumps(self.job_data(job_description="x" * 20001))
        before = self.records()
        result = self.client.post(reverse("extract_job_features"), {"raw_job_text": "developer hourly project", "continue_anyway": "true"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.records(), before)

    def test_selection_is_retained_and_escaped_on_other_form_error(self):
        record = self.experience(company_or_project="<script>unsafe</script>")
        result = self.assert_proposal_rejected({**self.selected_data([record]), "job_title": ""})
        self.assertEqual(result.context["form"]["selected_experiences"].value(), [str(record.pk)])
        self.assertContains(result, escape(record.company_or_project))
        self.assertNotContains(result, record.company_or_project)

    def test_component_guards_run_before_generation_even_if_form_limits_fit(self):
        with patch.object(limits, "PROFILE_CONTEXT_CHARACTERS", 1):
            self.assert_proposal_rejected(self.job_data())
        with patch.object(limits, "JOB_CONTEXT_CHARACTERS", 1):
            self.assert_proposal_rejected(self.job_data())

    def test_rejected_provider_outputs_leave_job_lifecycle_and_proposals_unchanged(self):
        for text, finish in (("Partial", "length"), ("x" * 8001, "stop")):
            before = self.records()
            with override_settings(OPENAI_API_KEY="test-only-not-a-credential"), patch(
                "proposal_ai.views.services.generate_proposal", new=self.real_services["generate_proposal"],
            ), patch("proposal_ai.services.OpenAI") as sdk:
                sdk.return_value.__enter__.return_value.chat.completions.create.return_value = response(text, finish)
                result = self.client.post(self.url(), self.job_data())
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.context["form"].errors)
            self.assertEqual(self.records(), before)

    def test_incomplete_or_oversized_extraction_output_creates_no_job(self):
        for text, finish in ((json.dumps(self.job_data()), "length"), ("x" * (limits.EXTRACTION_RESPONSE_CHARACTERS + 1), "stop")):
            before = self.records()
            with override_settings(OPENAI_API_KEY="test-only-not-a-credential"), patch(
                "proposal_ai.views.services.extract_job_details", new=self.real_services["extract_job_details"],
            ), patch("proposal_ai.services.OpenAI") as sdk:
                sdk.return_value.__enter__.return_value.chat.completions.create.return_value = response(text, finish)
                result = self.client.post(reverse("extract_job_features"), {"raw_job_text": "developer hourly", "continue_anyway": "true"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.records(), before)

    def test_incomplete_or_oversized_summary_output_saves_nothing(self):
        for text, finish in (("Partial", "length"), ("x" * 2001, "stop")):
            before = self.records()
            with override_settings(OPENAI_API_KEY="test-only-not-a-credential"), patch(
                "proposal_ai.views.services.generate_profile_summary", new=self.real_services["generate_profile_summary"],
            ), patch("proposal_ai.services.OpenAI") as sdk:
                sdk.return_value.__enter__.return_value.chat.completions.create.return_value = response(text, finish)
                result = self.client.post(reverse("generate_profile_summary"), {"professional_title": "Developer", "key_skills": "Django"})
            self.assertEqual(result.status_code, 500)
            self.assertEqual(self.records(), before)

    def test_final_service_rejection_creates_no_proposal_and_never_constructs_client(self):
        before = self.records()
        with patch("proposal_ai.views.services.generate_proposal", new=self.real_services["generate_proposal"]), patch.object(
            limits, "REQUEST_CHARACTERS", {**limits.REQUEST_CHARACTERS, "proposal": 1},
        ), patch("proposal_ai.services.OpenAI") as sdk:
            result = self.client.post(self.url(), self.job_data())
            sdk.assert_not_called()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.records(), before)

    def test_byte_backstop_rejects_valid_component_sizes_without_partial_writes(self):
        self.profile.profile_summary = "😀" * 5000
        self.profile.save()
        selected = []
        for _ in range(2):
            record = self.experience(skills_used="😀" * 1000, experience_depth="😀" * 2000)
            record.tasks = "😀" * 4000
            record.save()
            selected.append(record)
        before = self.records()
        with patch("proposal_ai.views.services.generate_proposal", new=self.real_services["generate_proposal"]), patch(
            "proposal_ai.services.OpenAI",
        ) as sdk:
            result = self.client.post(self.url(), {**self.selected_data(selected), "job_description": "😀" * 20000})
            sdk.assert_not_called()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.records(), before)

    def test_selected_evidence_normalization_does_not_modify_stored_values(self):
        record = self.experience(skills_used=" \nDjango\t ")
        result = self.client.post(self.url(), self.selected_data([record]))
        self.assertEqual(result.status_code, 302)
        self.assertIn("Skills Used: Django\n", self.patches["generate_proposal"].call_args.args[1])
        record.refresh_from_db()
        self.assertEqual(record.skills_used, " \nDjango\t ")
