"""Controlled Beta access and password/session security, with isolated users only."""
from datetime import timedelta
import secrets
from unittest.mock import patch
import uuid

from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.auth.forms import AdminPasswordChangeForm
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.views.debug import SafeExceptionReporterFilter

from mysite.test_accounts import AccountTestCase
from mysite.test_production_configuration import configured_production
from proposal_ai import ai_control
from proposal_ai.models import AIRequest, FreelancerProfile, JobPost, Proposal


User = get_user_model()


@override_settings(APP_ENV="development", REGISTRATION_ENABLED=False, SECURE_SSL_REDIRECT=False)
class BetaTestCase(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)

    def make_user(self, username="vetted-tester", **changes):
        password = secrets.token_urlsafe(32)
        user = User.objects.create_user(username=username, password=password, **changes)
        FreelancerProfile.objects.create(user=user, professional_title="Synthetic title")
        return user, password

    def post_with_csrf(self, name, data, client=None, **kwargs):
        client = client or self.client
        login_url = reverse("login")
        response = client.get(login_url, secure=True)
        self.assertEqual(response.status_code, 200)
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        return client.post(
            reverse(name, kwargs=kwargs), {**data, "csrfmiddlewaretoken": token},
            secure=True, HTTP_REFERER="https://testserver" + login_url,
        )

    def assert_login_redirect(self, response, path):
        self.assertRedirects(
            response, reverse("login") + "?next=" + path,
            fetch_redirect_response=False,
        )


class RegistrationGateTests(BetaTestCase):
    def test_disabled_get_is_403_with_closed_message_and_login_link(self):
        with patch("proposal_ai.views.RegistrationForm") as form:
            response = self.client.get(reverse("register"), secure=True)
        form.assert_not_called()
        self.assertContains(response, "Beta registration is currently closed.", status_code=403)
        self.assertContains(response, f'href="{reverse("login")}"', status_code=403)
        self.assertNotContains(response, "<form", status_code=403)
        self.assertNotIn("form", response.context)
        self.assertFalse(User.objects.exists())

    def test_disabled_head_is_403_without_signup_content_or_form_construction(self):
        with patch("proposal_ai.views.RegistrationForm") as form:
            response = self.client.head(reverse("register"), secure=True)
        form.assert_not_called()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content, b"")
        self.assertNotIn("form", response.context)

    def test_disabled_valid_csrf_post_never_constructs_form_authenticates_or_logs_in(self):
        data = self.registration_data()
        with (
            patch("proposal_ai.views.RegistrationForm") as form,
            patch("proposal_ai.views.login") as login,
            patch("django.contrib.auth.authenticate") as authenticate,
        ):
            response = self.post_with_csrf("register", data)
        self.assertContains(response, "Beta registration is currently closed.", status_code=403)
        for mocked in (form, login, authenticate):
            mocked.assert_not_called()
        self.assertFalse(User.objects.exists())
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assert_passwords_hidden(response, data["password1"], data["password2"])

    def test_direct_post_cannot_override_policy_or_create_privileged_user(self):
        response = self.post_with_csrf("register", self.registration_data(
            REGISTRATION_ENABLED="True", registration_enabled="true",
            APP_ENV="development", is_staff="true", is_superuser="true",
        ))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.exists())

    def test_query_string_cannot_enable_registration(self):
        response = self.client.get(reverse("register"), {"REGISTRATION_ENABLED": "True"}, secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("form", response.context)

    def test_disabled_post_without_csrf_is_still_rejected_by_middleware(self):
        with patch("proposal_ai.views.register_user") as registration:
            response = self.client.post(reverse("register"), self.registration_data(), secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "CSRF verification failed", status_code=403)
        registration.assert_not_called()
        self.assertFalse(User.objects.exists())

    def test_existing_authenticated_user_cannot_bypass_closed_registration(self):
        user, _ = self.make_user()
        self.client.force_login(user)
        response = self.post_with_csrf("register", self.registration_data())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(self.client.session[SESSION_KEY], str(user.pk))

    def test_passwords_remain_masked_in_exception_reports_for_closed_registration(self):
        data = self.registration_data()
        response = self.post_with_csrf("register", data)
        filtered = SafeExceptionReporterFilter().get_post_parameters(response.wsgi_request)
        self.assertEqual(filtered["password1"], "********************")
        self.assertEqual(filtered["password2"], "********************")

    @override_settings(REGISTRATION_ENABLED="True")
    def test_invalid_effective_policy_does_not_enable_registration(self):
        response = self.post_with_csrf("register", self.registration_data())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.exists())


class RegistrationNavigationTests(BetaTestCase):
    def test_all_public_surfaces_hide_signup_when_closed_and_preserve_login(self):
        for name in ("public_home", "login", "register"):
            with self.subTest(surface=name):
                response = self.client.get(reverse(name), secure=True)
                expected = 403 if name == "register" else 200
                self.assertEqual(response.status_code, expected)
                self.assertNotContains(response, f'href="{reverse("register")}"', status_code=expected)
                self.assertNotContains(response, "Get Started", status_code=expected)
                self.assertContains(response, f'href="{reverse("login")}"', status_code=expected)
                self.assertNotContains(response, f'href="{reverse("password_change")}"', status_code=expected)

    @override_settings(REGISTRATION_ENABLED=True)
    def test_enabled_public_home_and_login_keep_registration_links(self):
        for name in ("public_home", "login"):
            with self.subTest(surface=name):
                response = self.client.get(reverse(name), secure=True)
                self.assertContains(response, f'href="{reverse("register")}"')

    def test_authenticated_navigation_exposes_password_change_without_signup(self):
        user, _ = self.make_user()
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard"), secure=True)
        self.assertContains(response, f'href="{reverse("password_change")}"')
        self.assertNotContains(response, f'href="{reverse("register")}"')

    def test_authenticated_public_home_keeps_dashboard_entry(self):
        user, _ = self.make_user()
        self.client.force_login(user)
        response = self.client.get(reverse("public_home"), secure=True)
        self.assertContains(response, f'href="{reverse("dashboard")}"')
        self.assertNotContains(response, f'href="{reverse("register")}"')


@override_settings(REGISTRATION_ENABLED=True)
class EnabledRegistrationTests(BetaTestCase):
    def test_enabled_development_get_renders_signup(self):
        response = self.client.get(reverse("register"), secure=True)
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password1"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')

    def test_enabled_valid_csrf_registration_preserves_hashed_user_login_flow(self):
        data = self.registration_data()
        response = self.post_with_csrf("register", data)
        self.assertRedirects(response, reverse("create_freelancer_profile"), fetch_redirect_response=False)
        user = User.objects.get(username=data["username"])
        self.assertTrue(user.check_password(data["password1"]))
        self.assertNotEqual(user.password, data["password1"])
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(self.client.session[SESSION_KEY], str(user.pk))

    def test_enabled_invalid_registration_keeps_validation_and_hides_password_values(self):
        data = self.registration_data(email="not-an-email", password2=secrets.token_urlsafe(32))
        response = self.post_with_csrf("register", data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.assertIn("password2", response.context["form"].errors)
        self.assertFalse(User.objects.exists())
        self.assert_passwords_hidden(response, data["password1"], data["password2"])

    def test_enabled_case_insensitive_duplicate_username_remains_rejected(self):
        self.make_user(username="duplicate")
        response = self.post_with_csrf("register", self.registration_data(username="DUPLICATE"))
        self.assertIn("username", response.context["form"].errors)
        self.assertEqual(User.objects.count(), 1)

    def test_enabled_case_insensitive_duplicate_email_remains_rejected(self):
        self.make_user(email="vetted@example.test")
        response = self.post_with_csrf("register", self.registration_data(email="VETTED@EXAMPLE.TEST"))
        self.assertIn("email", response.context["form"].errors)
        self.assertEqual(User.objects.count(), 1)

    def test_enabled_registration_still_requires_csrf(self):
        response = self.client.post(reverse("register"), self.registration_data(), secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.exists())


class ExistingAccountTests(BetaTestCase):
    def test_existing_active_user_can_log_in_while_registration_is_closed(self):
        user, password = self.make_user()
        response = self.post_with_csrf("login", {"username": user.username, "password": password})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.session[SESSION_KEY], str(user.pk))

    def test_active_ordinary_user_can_use_dashboard_and_profile(self):
        user, _ = self.make_user()
        self.client.force_login(user)
        for name in ("dashboard", "create_freelancer_profile", "my_experiences"):
            with self.subTest(surface=name):
                response = self.client.get(reverse(name), secure=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.wsgi_request.user.pk, user.pk)
        self.assertFalse(user.is_staff)

    def test_registration_closure_preserves_existing_active_session(self):
        user, _ = self.make_user()
        with override_settings(REGISTRATION_ENABLED=True):
            self.client.force_login(user)
        self.client.get(reverse("register"), secure=True)
        response = self.client.get(reverse("dashboard"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.user.pk, user.pk)

    def test_registration_closure_does_not_modify_existing_user(self):
        user, _ = self.make_user(email="vetted@example.test")
        before = User.objects.filter(pk=user.pk).values().get()
        self.client.get(reverse("register"), secure=True)
        self.post_with_csrf("register", self.registration_data())
        self.assertEqual(User.objects.filter(pk=user.pk).values().get(), before)
        self.assertEqual(User.objects.count(), 1)

    def test_inactive_login_and_unknown_login_share_generic_failure(self):
        user, password = self.make_user(is_active=False)
        for username in (user.username, "absent-account"):
            with self.subTest(existing=username == user.username):
                response = self.post_with_csrf("login", {"username": username, "password": password})
                self.assertContains(response, "Invalid username or password.")
                self.assertNotContains(response, "inactive")
                self.assertNotIn(SESSION_KEY, self.client.session)
                self.assert_passwords_hidden(response, password)

    def test_inactive_account_cannot_use_authenticated_application(self):
        user, _ = self.make_user(is_active=False)
        self.client.force_login(user)
        path = reverse("dashboard")
        self.assert_login_redirect(self.client.get(path, secure=True), path)


class AdminProvisioningAndRevocationTests(BetaTestCase):
    def test_admin_can_provision_an_ordinary_user_with_public_registration_closed(self):
        owner, _ = self.make_user(username="operator", is_staff=True, is_superuser=True)
        self.client.force_login(owner)
        password = secrets.token_urlsafe(32)
        response = self.post_with_csrf("admin:auth_user_add", {
            "username": "manual-beta-tester", "usable_password": "true",
            "password1": password, "password2": password, "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        tester = User.objects.get(username="manual-beta-tester")
        self.assertTrue(tester.is_active)
        self.assertFalse(tester.is_staff)
        self.assertFalse(tester.is_superuser)
        self.assertFalse(tester.groups.exists())
        self.assertFalse(tester.user_permissions.exists())
        self.assertTrue(tester.check_password(password))
        self.client.logout()
        response = self.post_with_csrf("login", {"username": tester.username, "password": password})
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(self.client.get(reverse("dashboard"), secure=True),
                             reverse("create_freelancer_profile"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("create_freelancer_profile"), secure=True).status_code, 200)

    def test_ordinary_tester_cannot_access_admin_or_provision_accounts(self):
        user, _ = self.make_user()
        self.client.force_login(user)
        for path in (reverse("admin:index"), reverse("admin:auth_user_add")):
            with self.subTest(path=path):
                response = self.client.get(path, secure=True)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse("admin:login")))
        response = self.post_with_csrf("admin:auth_user_add", self.registration_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.count(), 1)

    def test_deactivation_blocks_existing_session_and_all_future_ai_entry_points(self):
        user, _ = self.make_user()
        job = JobPost.objects.create(user=user, job_title="Synthetic opportunity")
        self.client.force_login(user)
        user.is_active = False
        user.save(update_fields=["is_active"])
        with patch("proposal_ai.views.ai_control.admit") as admit:
            for name, kwargs in (
                ("dashboard", {}), ("generate_profile_summary", {}),
                ("extract_job_features", {}), ("confirm_job_features", {"job_post_id": job.pk}),
            ):
                with self.subTest(surface=name):
                    path = reverse(name, kwargs=kwargs)
                    response = self.post_with_csrf(name, {}, **kwargs)
                    self.assert_login_redirect(response, path)
            admit.assert_not_called()
        self.assertFalse(AIRequest.objects.exists())

    def test_deactivation_preserves_application_history_and_dispatched_request_state(self):
        user, _ = self.make_user()
        profile = FreelancerProfile.objects.get(user=user)
        job = JobPost.objects.create(user=user, job_title="Synthetic opportunity")
        proposal = Proposal.objects.create(user=user, job_post=job, final_text="Synthetic proposal")
        row = AIRequest.objects.create(
            user=user, operation=AIRequest.Operation.PROFILE_SUMMARY,
            intent=AIRequest.Intent.GENERATE, nonce=uuid.uuid4(),
            submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
            quota_units=1, lifecycle=AIRequest.Lifecycle.IN_FLIGHT,
            dispatch_started_at=timezone.now(), lease_expires_at=timezone.now() + timedelta(minutes=10),
        )
        before = AIRequest.objects.filter(pk=row.pk).values().get()
        user.is_active = False
        user.save(update_fields=["is_active"])
        for model, record in ((FreelancerProfile, profile), (JobPost, job), (Proposal, proposal)):
            self.assertTrue(model.objects.filter(pk=record.pk, user=user).exists())
        self.assertEqual(AIRequest.objects.filter(pk=row.pk).values().get(), before)

    def test_credential_invalidation_prevents_old_session_restoration_after_reactivation(self):
        user, old_password = self.make_user()
        self.client.force_login(user)
        user.is_active = False
        user.save(update_fields=["is_active"])
        form = AdminPasswordChangeForm(user, data={"usable_password": "false"})
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())
        user.is_active = True
        user.save(update_fields=["is_active"])
        # The old browser has not visited while inactive; reactivation still must
        # not restore its previous authenticated session.
        path = reverse("dashboard")
        self.assert_login_redirect(self.client.get(path, secure=True), path)
        self.assertFalse(user.check_password(old_password))
        new_password = secrets.token_urlsafe(32)
        user.set_password(new_password)
        user.save(update_fields=["password"])
        response = self.post_with_csrf("login", {"username": user.username, "password": new_password})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(path, secure=True).status_code, 200)


class PasswordChangeTests(BetaTestCase):
    def setUp(self):
        super().setUp()
        self.user, self.old_password = self.make_user()

    def password_data(self, **changes):
        new_password = secrets.token_urlsafe(32)
        return {"old_password": self.old_password, "new_password1": new_password,
                "new_password2": new_password, **changes}

    def change(self, data=None):
        self.client.force_login(self.user)
        return self.post_with_csrf("password_change", data or self.password_data())

    def assert_unchanged(self):
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))

    def test_anonymous_get_requires_login(self):
        path = reverse("password_change")
        self.assert_login_redirect(self.client.get(path, secure=True), path)

    def test_anonymous_valid_csrf_post_requires_login_and_cannot_change_password(self):
        path = reverse("password_change")
        self.assert_login_redirect(self.post_with_csrf("password_change", self.password_data()), path)
        self.assert_unchanged()

    def test_anonymous_completion_page_requires_login(self):
        path = reverse("password_change_done")
        self.assert_login_redirect(self.client.get(path, secure=True), path)

    def test_authenticated_get_renders_current_and_new_password_fields(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("password_change"), secure=True)
        self.assertTemplateUsed(response, "password_change.html")
        for name in ("old_password", "new_password1", "new_password2", "csrfmiddlewaretoken"):
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, f'action="{reverse("password_change")}"')

    def test_authenticated_post_requires_csrf(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("password_change"), self.password_data(), secure=True)
        self.assertEqual(response.status_code, 403)
        self.assert_unchanged()

    def test_wrong_current_password_fails_without_reflecting_passwords(self):
        data = self.password_data(old_password=secrets.token_urlsafe(32))
        response = self.change(data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("old_password", response.context["form"].errors)
        self.assert_passwords_hidden(response, *data.values())
        self.assert_unchanged()

    def test_missing_current_password_fails(self):
        response = self.change(self.password_data(old_password=""))
        self.assertIn("old_password", response.context["form"].errors)
        self.assert_unchanged()

    def test_mismatched_new_passwords_fail(self):
        response = self.change(self.password_data(new_password2=secrets.token_urlsafe(32)))
        self.assertIn("new_password2", response.context["form"].errors)
        self.assert_unchanged()

    def test_missing_new_password_fails(self):
        response = self.change(self.password_data(new_password1="", new_password2=""))
        self.assertIn("new_password1", response.context["form"].errors)
        self.assert_unchanged()

    def test_configured_password_validators_reject_weak_new_passwords(self):
        for password, code in (("password", "password_too_common"), ("aQ!4", "password_too_short"),
                               ("928374650192", "password_entirely_numeric"),
                               (self.user.username, "password_too_similar")):
            with self.subTest(validator=code):
                response = self.change(self.password_data(new_password1=password, new_password2=password))
                errors = response.context["form"].errors.as_data()["new_password2"]
                self.assertIn(code, [error.code for error in errors])
                self.assert_unchanged()

    def test_success_hashes_password_and_keeps_current_session_authenticated(self):
        data = self.password_data()
        response = self.change(data)
        self.assertRedirects(response, reverse("password_change_done"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.password, data["new_password1"])
        self.assertTrue(self.user.check_password(data["new_password1"]))
        self.assertFalse(self.user.check_password(self.old_password))
        response = self.client.get(reverse("dashboard"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.user.pk, self.user.pk)

    def test_old_password_no_longer_authenticates_and_new_password_does(self):
        data = self.password_data()
        self.change(data)
        self.client.logout()
        response = self.post_with_csrf("login", {"username": self.user.username, "password": self.old_password})
        self.assertContains(response, "Invalid username or password.")
        self.assertNotIn(SESSION_KEY, self.client.session)
        response = self.post_with_csrf("login", {"username": self.user.username, "password": data["new_password1"]})
        self.assertEqual(response.status_code, 302)

    def test_other_session_with_previous_auth_hash_loses_authentication(self):
        other_browser = Client(enforce_csrf_checks=True)
        other_browser.force_login(self.user)
        self.assertEqual(other_browser.get(reverse("dashboard"), secure=True).status_code, 200)
        self.change()
        path = reverse("dashboard")
        self.assert_login_redirect(other_browser.get(path, secure=True), path)
        self.assertNotIn(SESSION_KEY, other_browser.session)
        self.assertEqual(self.client.get(path, secure=True).status_code, 200)

    def test_success_renders_completion_without_password_values(self):
        data = self.password_data()
        self.change(data)
        response = self.client.get(reverse("password_change_done"), secure=True)
        self.assertTemplateUsed(response, "password_change_done.html")
        self.assertContains(response, "Your password has been changed.")
        self.assert_passwords_hidden(response, *data.values())

    def test_standard_sensitive_parameter_filter_masks_all_password_change_fields(self):
        data = self.password_data(old_password=secrets.token_urlsafe(32))
        response = self.change(data)
        filtered = SafeExceptionReporterFilter().get_post_parameters(response.wsgi_request)
        for name in data:
            self.assertEqual(filtered[name], "********************")

    def test_password_change_does_not_deliberately_log_submitted_values(self):
        with self.assertNoLogs("proposal_ai", level="DEBUG"), self.assertNoLogs("django", level="INFO"):
            response = self.change()
        self.assertEqual(response.status_code, 302)

    def test_get_parameters_cannot_change_password_or_render_submitted_values(self):
        data = self.password_data()
        self.client.force_login(self.user)
        response = self.client.get(reverse("password_change"), data, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assert_unchanged()
        self.assert_passwords_hidden(response, *data.values())

    def test_client_supplied_user_identity_cannot_change_another_account_password(self):
        other, other_password = self.make_user(username="other-tester")
        data = self.password_data(user_id=other.pk, username=other.username)
        response = self.change(data)
        self.assertEqual(response.status_code, 302)
        other.refresh_from_db()
        self.assertTrue(other.check_password(other_password))


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class ProductionAccessTests(SimpleTestCase):
    def assert_closed_without_infrastructure(self, **changes):
        with (
            configured_production(ALLOWED_HOSTS="testserver", **changes),
            patch("django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
                  side_effect=AssertionError("Access-policy validation must not connect to a database.")),
            patch("proposal_ai.services.OpenAI",
                  side_effect=AssertionError("Access-policy validation must not construct a provider client.")),
            patch("proposal_ai.views.RegistrationForm") as form,
        ):
            client = Client(enforce_csrf_checks=True)
            response = client.get(reverse("register"), secure=True)
            self.assertContains(response, "Beta registration is currently closed.", status_code=403)
            response = client.get(reverse("login"), secure=True)
            self.assertEqual(response.status_code, 200)
            token = client.cookies[settings.CSRF_COOKIE_NAME].value
            password = secrets.token_urlsafe(32)
            response = client.post(reverse("register"), {
                "username": "unapproved", "email": "unapproved@example.test",
                "password1": password, "password2": password, "csrfmiddlewaretoken": token,
            }, secure=True, HTTP_REFERER="https://testserver/login/")
            self.assertEqual(response.status_code, 403)
            form.assert_not_called()

    def test_production_defaults_closed_and_login_remains_available_without_database_or_provider(self):
        self.assert_closed_without_infrastructure()

    def test_explicit_production_false_remains_closed_without_database_or_provider(self):
        self.assert_closed_without_infrastructure(REGISTRATION_ENABLED="False")


class ClosedRegistrationAIRegressionTests(BetaTestCase):
    def setUp(self):
        super().setUp()
        self.user, _ = self.make_user()
        self.client.force_login(self.user)

    def summary(self):
        return self.post_with_csrf("generate_profile_summary", {
            "professional_title": "Synthetic specialist", "key_skills": "Synthetic skill",
            "ai_nonce": ai_control.issue_nonce(self.user, AIRequest.Operation.PROFILE_SUMMARY),
        })

    @override_settings(AI_ENABLED=False)
    def test_registration_closure_does_not_bypass_global_ai_kill_switch(self):
        response = self.summary()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(AIRequest.objects.exists())

    @override_settings(AI_ENABLED=True, AI_GLOBAL_DAILY_CREDITS=0, AI_GLOBAL_WEEKLY_CREDITS=0)
    def test_registration_closure_does_not_bypass_global_quota(self):
        response = self.summary()
        self.assertEqual(response.status_code, 429)
        self.assertFalse(AIRequest.objects.exists())

    @override_settings(AI_ENABLED=True, AI_GLOBAL_DAILY_CREDITS=None, AI_GLOBAL_WEEKLY_CREDITS=None)
    def test_registration_closure_does_not_bypass_per_user_quota(self):
        moment = timezone.now()
        for _ in range(25):
            AIRequest.objects.create(
                user=self.user, operation=AIRequest.Operation.PROFILE_SUMMARY,
                intent=AIRequest.Intent.GENERATE, nonce=uuid.uuid4(),
                submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
                quota_units=1, lifecycle=AIRequest.Lifecycle.SUCCEEDED, quota_state=AIRequest.Quota.CONSUMED,
                admitted_at=moment, completed_at=moment, lease_expires_at=moment,
            )
        response = self.summary()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(AIRequest.objects.count(), 25)
