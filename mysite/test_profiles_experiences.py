from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from proposal_ai.forms import FreelancerProfileForm, WorkExperienceForm
from proposal_ai.models import FreelancerProfile, WorkExperience


User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    DEBUG=False,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class ProfileExperienceTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Unusable passwords; no real credentials or password login needed.
        cls.owner = User.objects.create_user(username="profile-owner")
        cls.other = User.objects.create_user(username="other-owner")

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        client_patch = patch(
            "proposal_ai.views.services.generate_profile_summary",
            side_effect=AssertionError("Form requests must not call AI."),
        )
        self.ai_client = client_patch.start()
        self.addCleanup(client_patch.stop)
        self.addCleanup(self.ai_client.assert_not_called)
        for target in ("extract_job_details", "generate_proposal"):
            service_patch = patch("proposal_ai.views.services." + target,
                                  side_effect=AssertionError("Form requests must not call AI."))
            mocked = service_patch.start()
            self.addCleanup(service_patch.stop)
            self.addCleanup(mocked.assert_not_called)

    def profile_data(self, **changes):
        data = {
            "professional_title": "Backend Developer",
            "profile_summary": "I build Django applications.",
            "preferred_tone": "professional",
        }
        data.update(changes)
        return data

    def experience_data(self, **changes):
        data = {
            "job_title": "Backend Project",
            "company_or_project": "Example Project",
            "tasks": "Built application views.",
            "skills_used": "Python, Django",
            "experience_depth": "Django: two project releases",
        }
        data.update(changes)
        return data

    def stored_data(self):
        return (
            list(FreelancerProfile.objects.order_by("pk").values()),
            list(WorkExperience.objects.order_by("pk").values()),
        )

    def assert_invalid(self, url, data, field, code):
        before = self.stored_data()
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_data(), before)
        errors = response.context["form"].errors.as_data()
        self.assertIn(code, [error.code for error in errors[field]])
        self.assertContains(response, 'role="alert"')
        self.assertContains(response, response.context["form"].fields[field].label + ":")
        for message in response.context["form"].errors[field]:
            self.assertContains(response, escape(message))
        return response


class FreelancerProfileTests(ProfileExperienceTestCase):
    def test_valid_profile_is_created_for_request_user(self):
        response = self.client.post(reverse("create_freelancer_profile"), self.profile_data())
        self.assertRedirects(response, reverse("add_work_experience"))
        profile = FreelancerProfile.objects.get()
        self.assertEqual(profile.user_id, self.owner.pk)
        self.assertEqual(profile.professional_title, "Backend Developer")
        self.assertEqual(profile.profile_summary, "I build Django applications.")

    def test_existing_profile_is_updated_without_creating_second_profile(self):
        profile = FreelancerProfile.objects.create(user=self.owner, **self.profile_data())
        other_profile = FreelancerProfile.objects.create(
            user=self.other, **self.profile_data(professional_title="Other private profile")
        )
        other_before = FreelancerProfile.objects.filter(pk=other_profile.pk).values().get()
        data = self.profile_data(professional_title="Updated Developer", preferred_tone="technical")
        data.update(user=self.other.pk, user_id=self.other.pk, owner=self.other.pk, id=other_profile.pk)
        response = self.client.post(reverse("create_freelancer_profile"), data)
        self.assertRedirects(response, reverse("add_work_experience"), fetch_redirect_response=False)
        profile.refresh_from_db()
        self.assertEqual(profile.professional_title, "Updated Developer")
        self.assertEqual(profile.preferred_tone, "technical")
        self.assertEqual(profile.user_id, self.owner.pk)
        self.assertEqual(FreelancerProfile.objects.filter(user=self.owner).count(), 1)
        self.assertEqual(FreelancerProfile.objects.count(), 2)
        self.assertEqual(FreelancerProfile.objects.filter(pk=other_profile.pk).values().get(), other_before)

    def test_client_user_or_profile_ids_cannot_create_or_change_another_profile(self):
        other_profile = FreelancerProfile.objects.create(user=self.other, **self.profile_data())
        other_before = FreelancerProfile.objects.filter(pk=other_profile.pk).values().get()
        data = self.profile_data(professional_title="Owner's new profile")
        data.update(user=self.other.pk, user_id=self.other.pk, owner=self.other.pk, id=other_profile.pk)
        response = self.client.post(reverse("create_freelancer_profile"), data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(FreelancerProfile.objects.filter(user=self.owner).exists())
        self.assertEqual(FreelancerProfile.objects.filter(pk=other_profile.pk).values().get(), other_before)

    def test_required_fields_reject_missing_and_blank_values(self):
        for field in ("professional_title", "preferred_tone"):
            for missing in (False, True):
                with self.subTest(field=field, missing=missing):
                    data = self.profile_data()
                    if missing:
                        data.pop(field)
                    else:
                        data[field] = ""
                    self.assert_invalid(reverse("create_freelancer_profile"), data, field, "required")

    def test_whitespace_only_title_is_rejected(self):
        self.assert_invalid(
            reverse("create_freelancer_profile"), self.profile_data(professional_title=" \t\n "),
            "professional_title", "required",
        )

    def test_oversized_title_is_rejected_using_model_limit(self):
        limit = FreelancerProfile._meta.get_field("professional_title").max_length
        self.assert_invalid(
            reverse("create_freelancer_profile"), self.profile_data(professional_title="x" * (limit + 1)),
            "professional_title", "max_length",
        )

    def test_invalid_and_oversized_tones_are_rejected(self):
        for tone in ("unexpected", "x" * 101):
            with self.subTest(tone_length=len(tone)):
                self.assert_invalid(
                    reverse("create_freelancer_profile"), self.profile_data(preferred_tone=tone),
                    "preferred_tone", "invalid_choice",
                )

    def test_all_four_existing_ui_tones_are_accepted(self):
        for tone in ("professional", "friendly", "confident", "technical"):
            with self.subTest(tone=tone):
                response = self.client.post(
                    reverse("create_freelancer_profile"), self.profile_data(preferred_tone=tone)
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(FreelancerProfile.objects.get(user=self.owner).preferred_tone, tone)
                self.assertEqual(FreelancerProfile.objects.count(), 1)

    def test_optional_summary_can_be_omitted_and_text_is_trimmed(self):
        data = self.profile_data(professional_title="  Backend Developer  ")
        data.pop("profile_summary")
        response = self.client.post(reverse("create_freelancer_profile"), data)
        self.assertEqual(response.status_code, 302)
        profile = FreelancerProfile.objects.get()
        self.assertEqual(profile.professional_title, "Backend Developer")
        self.assertFalse(profile.profile_summary)
        response = self.client.post(
            reverse("create_freelancer_profile"), self.profile_data(profile_summary="  Updated summary  ")
        )
        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.profile_summary, "Updated summary")

    def test_invalid_update_leaves_every_stored_field_unchanged(self):
        FreelancerProfile.objects.create(user=self.owner, **self.profile_data())
        self.assert_invalid(
            reverse("create_freelancer_profile"),
            self.profile_data(professional_title="", profile_summary="Must not persist", preferred_tone="friendly"),
            "professional_title", "required",
        )

    def test_optional_summary_can_be_cleared_with_blank_or_whitespace_values(self):
        profile = FreelancerProfile.objects.create(user=self.owner, **self.profile_data())
        for summary in ("", " \t\n "):
            with self.subTest(whitespace=bool(summary)):
                response = self.client.post(
                    reverse("create_freelancer_profile"), self.profile_data(profile_summary=summary)
                )
                self.assertEqual(response.status_code, 302)
                profile.refresh_from_db()
                self.assertFalse(profile.profile_summary)

    def test_get_redisplays_existing_values_without_saving_or_exposing_other_profile(self):
        profile = FreelancerProfile.objects.create(
            user=self.owner, **self.profile_data(preferred_tone="friendly")
        )
        FreelancerProfile.objects.create(
            user=self.other, **self.profile_data(professional_title="Hidden other profile")
        )
        before = self.stored_data()
        response = self.client.get(reverse("create_freelancer_profile"), self.profile_data())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["form"].is_bound)
        self.assertEqual(response.context["form"].instance.pk, profile.pk)
        self.assertContains(response, 'value="Backend Developer"')
        self.assertContains(response, "I build Django applications.")
        self.assertContains(response, 'value="friendly" selected')
        self.assertNotContains(response, "Hidden other profile")
        self.assertEqual(self.stored_data(), before)

    def test_get_new_profile_displays_professional_default_without_saving(self):
        response = self.client.get(reverse("create_freelancer_profile"))
        self.assertContains(response, 'value="professional" selected')
        self.assertEqual(FreelancerProfile.objects.count(), 0)

    def test_next_experiences_redirect_is_preserved_and_other_next_values_are_ignored(self):
        for next_value, destination in (
            ("my_experiences", "my_experiences"),
            ("https://untrusted.example/", "add_work_experience"),
        ):
            with self.subTest(destination=destination):
                response = self.client.post(
                    reverse("create_freelancer_profile"), {**self.profile_data(), "next": next_value}
                )
                self.assertRedirects(response, reverse(destination), fetch_redirect_response=False)

    def test_invalid_form_retains_summary_tone_skills_and_next_with_html_escaping(self):
        html = '</textarea><script>alert("test")</script>'
        data = self.profile_data(
            professional_title="", profile_summary=html, preferred_tone="friendly",
            key_skills=html, next="my_experiences",
        )
        response = self.assert_invalid(
            reverse("create_freelancer_profile"), data, "professional_title", "required"
        )
        self.assertEqual(response.context["form"]["profile_summary"].value(), html)
        self.assertContains(response, escape(html))
        self.assertNotContains(response, html)
        self.assertContains(response, 'value="friendly" selected')
        self.assertContains(response, 'value="my_experiences"')
        for element_id in ("professional_title", "key_skills", "profile_summary",
                           "generate-summary-btn", "skip-summary-btn", "summary-status"):
            self.assertContains(response, 'id="' + element_id + '"')
        self.assertContains(response, reverse("generate_profile_summary"))


class WorkExperienceCreateTests(ProfileExperienceTestCase):
    def test_valid_experience_is_created_for_request_user_and_finish_redirect_is_preserved(self):
        data = self.experience_data(user=self.other.pk, user_id=self.other.pk, owner=self.other.pk)
        response = self.client.post(reverse("add_work_experience"), data)
        self.assertRedirects(response, reverse("my_experiences"))
        experience = WorkExperience.objects.get()
        self.assertEqual(experience.user_id, self.owner.pk)
        for field, value in self.experience_data().items():
            self.assertEqual(getattr(experience, field), value)

    def test_save_and_add_another_redirect_is_preserved(self):
        response = self.client.post(
            reverse("add_work_experience"), self.experience_data(add_another="")
        )
        self.assertRedirects(response, reverse("add_work_experience"))
        self.assertEqual(WorkExperience.objects.count(), 1)

    def test_required_fields_reject_missing_blank_and_whitespace_values(self):
        for field in ("job_title", "tasks"):
            for value in (None, "", " \t\n "):
                with self.subTest(field=field, missing=value is None):
                    data = self.experience_data()
                    if value is None:
                        data.pop(field)
                    else:
                        data[field] = value
                    self.assert_invalid(reverse("add_work_experience"), data, field, "required")

    def test_model_max_lengths_are_enforced(self):
        for field in ("job_title", "company_or_project"):
            with self.subTest(field=field):
                limit = WorkExperience._meta.get_field(field).max_length
                self.assert_invalid(
                    reverse("add_work_experience"), self.experience_data(**{field: "x" * (limit + 1)}),
                    field, "max_length",
                )

    def test_optional_fields_can_be_omitted(self):
        data = self.experience_data()
        data.pop("company_or_project")
        data.pop("skills_used")
        data.pop("experience_depth")
        response = self.client.post(reverse("add_work_experience"), data)
        self.assertEqual(response.status_code, 302)
        experience = WorkExperience.objects.get()
        self.assertFalse(experience.company_or_project)
        self.assertFalse(experience.skills_used)
        self.assertFalse(experience.experience_depth)

    def test_optional_fields_accept_blank_and_whitespace_values(self):
        for value in ("", " \t\n "):
            with self.subTest(whitespace=bool(value)):
                data = self.experience_data(company_or_project=value, skills_used=value, experience_depth=value)
                response = self.client.post(reverse("add_work_experience"), data)
                self.assertEqual(response.status_code, 302)
                experience = WorkExperience.objects.latest("pk")
                self.assertFalse(experience.company_or_project)
                self.assertFalse(experience.skills_used)
                self.assertFalse(experience.experience_depth)

    def test_text_fields_are_trimmed_without_inventing_limits_for_text_fields(self):
        data = {field: "  " + value + " \n" for field, value in self.experience_data().items()}
        data["skills_used"] = "  " + "Python " * 1000 + "  "
        response = self.client.post(reverse("add_work_experience"), data)
        self.assertEqual(response.status_code, 302)
        experience = WorkExperience.objects.get()
        for field, value in data.items():
            self.assertEqual(getattr(experience, field), value.strip())

    def test_get_only_displays_unbound_form_even_with_data_in_query(self):
        response = self.client.get(reverse("add_work_experience"), self.experience_data())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["form"].is_bound)
        self.assertEqual(WorkExperience.objects.count(), 0)

    def test_invalid_submission_retains_and_escapes_all_submitted_text(self):
        html = '</textarea><script>alert("test")</script>'
        data = self.experience_data(job_title=html, company_or_project=html, tasks="",
                                    skills_used=html, experience_depth=html)
        response = self.assert_invalid(reverse("add_work_experience"), data, "tasks", "required")
        for field in ("job_title", "company_or_project", "skills_used", "experience_depth"):
            self.assertEqual(response.context["form"][field].value(), html)
        self.assertContains(response, escape(html))
        self.assertNotContains(response, html)


class WorkExperienceEditTests(ProfileExperienceTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        data = {"job_title": "Owned role", "tasks": "Owned tasks", "experience_depth": "Two releases"}
        cls.experience = WorkExperience.objects.create(user=cls.owner, **data)
        cls.other_experience = WorkExperience.objects.create(user=cls.other, **{**data, "job_title": "Hidden role"})

    def edit_url(self):
        return reverse("edit_work_experience", args=[self.experience.pk])

    def test_owner_get_loads_existing_values_without_saving(self):
        before = self.stored_data()
        response = self.client.get(self.edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].instance.pk, self.experience.pk)
        self.assertContains(response, 'value="Owned role"')
        self.assertContains(response, "Owned tasks")
        self.assertContains(response, "Two releases")
        self.assertEqual(self.stored_data(), before)

    def test_owner_updates_existing_record_without_reassigning_or_changing_other_records(self):
        other_before = WorkExperience.objects.filter(pk=self.other_experience.pk).values().get()
        data = self.experience_data(user=self.other.pk, user_id=self.other.pk, owner=self.other.pk,
                                    id=self.other_experience.pk)
        response = self.client.post(self.edit_url(), data)
        self.assertRedirects(response, reverse("my_experiences"))
        self.experience.refresh_from_db()
        for field, value in self.experience_data().items():
            self.assertEqual(getattr(self.experience, field), value)
        self.assertEqual(self.experience.user_id, self.owner.pk)
        self.assertEqual(WorkExperience.objects.count(), 2)
        self.assertEqual(WorkExperience.objects.filter(pk=self.other_experience.pk).values().get(), other_before)

    def test_invalid_update_preserves_all_stored_fields_and_redisplays_submitted_values(self):
        data = self.experience_data(job_title="Changed title", tasks="", company_or_project="Changed company")
        response = self.assert_invalid(self.edit_url(), data, "tasks", "required")
        self.assertContains(response, 'value="Changed title"')
        self.assertContains(response, 'value="Changed company"')
        self.assertEqual(response.context["form"]["tasks"].value(), "")

    def test_oversized_update_leaves_stored_data_unchanged(self):
        self.assert_invalid(
            self.edit_url(), self.experience_data(job_title="x" * 256), "job_title", "max_length"
        )

    def test_edit_validation_failure_escapes_submitted_html(self):
        html = '</textarea><script>alert("test")</script>'
        response = self.assert_invalid(
            self.edit_url(), self.experience_data(job_title=html, tasks=""), "tasks", "required"
        )
        self.assertContains(response, escape(html))
        self.assertNotContains(response, html)

    def test_another_user_cannot_get_or_post_the_record(self):
        self.client.force_login(self.other)
        before = self.stored_data()
        self.assertEqual(self.client.get(self.edit_url()).status_code, 404)
        self.assertEqual(self.client.post(self.edit_url(), self.experience_data()).status_code, 404)
        self.assertEqual(self.stored_data(), before)

    def test_nonexistent_experience_returns_404_for_get_and_post(self):
        url = reverse("edit_work_experience", args=[self.other_experience.pk + 1000])
        before = self.stored_data()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, self.experience_data()).status_code, 404)
        self.assertEqual(self.stored_data(), before)

    def test_listing_shows_only_owner_records_and_does_not_save(self):
        before = self.stored_data()
        response = self.client.get(reverse("my_experiences"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["experiences"].values_list("pk", flat=True)), [self.experience.pk])
        self.assertContains(response, "Owned role")
        self.assertNotContains(response, "Hidden role")
        self.assertEqual(self.stored_data(), before)


class ProfileExperienceSecurityTests(ProfileExperienceTestCase):
    def routes(self, experience):
        return (
            (reverse("create_freelancer_profile"), self.profile_data()),
            (reverse("add_work_experience"), self.experience_data()),
            (reverse("edit_work_experience", args=[experience.pk]), self.experience_data()),
        )

    def test_forms_expose_only_the_explicit_editable_fields(self):
        self.assertEqual(set(FreelancerProfileForm().fields),
                         {"professional_title", "profile_summary", "preferred_tone"})
        self.assertEqual(set(WorkExperienceForm().fields),
                         {"job_title", "company_or_project", "tasks", "skills_used", "experience_depth"})

    def test_anonymous_users_are_redirected_to_real_login_without_writes(self):
        experience = WorkExperience.objects.create(user=self.owner, **self.experience_data())
        client = Client()
        before = self.stored_data()
        for url, data in self.routes(experience):
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    response = client.get(url) if method == "get" else client.post(url, data)
                    self.assertRedirects(response, reverse("login") + "?next=" + url,
                                         fetch_redirect_response=False)
        response = client.get(reverse("my_experiences"))
        self.assertRedirects(response, reverse("login") + "?next=" + reverse("my_experiences"),
                             fetch_redirect_response=False)
        self.assertEqual(self.stored_data(), before)

    def test_csrf_middleware_tokens_and_rejection_remain_enabled(self):
        self.assertIn("django.middleware.csrf.CsrfViewMiddleware", settings.MIDDLEWARE)
        experience = WorkExperience.objects.create(user=self.owner, **self.experience_data())
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        before = self.stored_data()
        for url, data in self.routes(experience):
            with self.subTest(url=url):
                self.assertContains(client.get(url, secure=True), 'name="csrfmiddlewaretoken"')
                response = client.post(url, data, secure=True)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stored_data(), before)

    def test_valid_csrf_protected_posts_can_create_and_edit(self):
        experience = WorkExperience.objects.create(user=self.owner, **self.experience_data())
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        for url, data in self.routes(experience):
            with self.subTest(url=url):
                self.assertEqual(client.get(url, secure=True).status_code, 200)
                response = client.post(
                    url, {**data, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
                    secure=True, HTTP_REFERER="https://testserver" + url,
                )
                self.assertEqual(response.status_code, 302)
        self.assertEqual(FreelancerProfile.objects.filter(user=self.owner).count(), 1)
        self.assertEqual(WorkExperience.objects.filter(user=self.owner).count(), 2)

    def test_other_methods_cannot_change_profile_or_experience_data(self):
        experience = WorkExperience.objects.create(user=self.owner, **self.experience_data())
        before = self.stored_data()
        for url, data in self.routes(experience):
            for method in ("put", "patch", "delete"):
                with self.subTest(url=url, method=method):
                    self.assertEqual(getattr(self.client, method)(url, data).status_code, 405)
        self.assertEqual(self.stored_data(), before)

    def test_profile_summary_get_or_missing_input_never_calls_ai_or_saves(self):
        url = reverse("generate_profile_summary")
        before = self.stored_data()
        self.assertEqual(self.client.get(url).status_code, 405)
        for data in ({}, {"professional_title": "Developer"}, {"key_skills": "Django"}):
            self.assertEqual(self.client.post(url, data).status_code, 400)
        self.assertEqual(self.stored_data(), before)

    def test_mocked_profile_summary_is_saved_only_by_explicit_valid_profile_post(self):
        client = Mock(return_value="Example generated summary.")
        before = self.stored_data()
        with patch("proposal_ai.views.services.generate_profile_summary", new=client):
            response = self.client.post(reverse("generate_profile_summary"),
                                        {"professional_title": "Developer", "key_skills": "Django"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"summary": "Example generated summary."})
        self.assertEqual(self.stored_data(), before)
        client.assert_called_once()
        self.assertIn("Professional title:\nDeveloper", client.call_args.args[0])
        response = self.client.post(
            reverse("create_freelancer_profile"), self.profile_data(profile_summary=response.json()["summary"])
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FreelancerProfile.objects.get(user=self.owner).profile_summary,
                         "Example generated summary.")
