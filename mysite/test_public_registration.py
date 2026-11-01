"""Phase A account protections using synthetic users and blocked provider access."""
from datetime import timedelta
import io
from unittest.mock import patch

from django.contrib.auth import SESSION_KEY, get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import OperationalError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from mysite.configuration import validate_production
from mysite.test_accounts import AccountTestCase
from mysite.test_production_configuration import configured_production, environment, load_settings
from proposal_ai.account_security import consume
from proposal_ai.forms import RegistrationForm
from proposal_ai.models import AccountRateLimit, RegisteredEmail, FreelancerProfile, JobPost, Proposal, WorkExperience


class PublicConfigurationTests(AccountTestCase):
    def test_public_validator_passes_without_connecting_to_infrastructure(self):
        with configured_production(REGISTRATION_ENABLED="True"), patch(
            "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection", side_effect=AssertionError("No DB")
        ):
            output = io.StringIO()
            call_command("validate_deployment", stdout=output)
        self.assertIn("public registration is open", output.getvalue())

    def test_public_configuration_cannot_enable_ai_or_disable_throttling(self):
        for changes in ({"AI_ENABLED": "True", "AI_GLOBAL_DAILY_CREDITS": "10", "AI_GLOBAL_WEEKLY_CREDITS": "20"},
                        {"AUTH_RATE_LIMIT_ENABLED": "False"}, {"DEBUG": "True"}):
            with self.subTest(changes=changes), self.assertRaises(ImproperlyConfigured):
                load_settings(environment(REGISTRATION_ENABLED="True", **changes))

    def test_effective_account_protections_are_required(self):
        for name, value in (("MIDDLEWARE", []), ("AUTH_PASSWORD_VALIDATORS", []), ("AUTH_RATE_LIMIT_ENABLED", False)):
            parsed = load_settings(environment(REGISTRATION_ENABLED="True"))
            parsed[name] = value
            with self.subTest(setting=name), self.assertRaises(ImproperlyConfigured):
                validate_production(parsed)

    def test_release_validator_rejects_ai_even_when_registration_closed(self):
        with configured_production(), override_settings(AI_ENABLED=True, AI_GLOBAL_DAILY_CREDITS=10, AI_GLOBAL_WEEKLY_CREDITS=20):
            with self.assertRaises(ImproperlyConfigured):
                call_command("validate_deployment", stdout=io.StringIO())


@override_settings(APP_ENV="production", REGISTRATION_ENABLED=True, AI_ENABLED=False,
                   AUTH_RATE_LIMIT_ENABLED=True, SECURE_SSL_REDIRECT=False)
class PublicAccountTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)

    def post(self, name, data, client=None, **extra):
        client = client or self.client
        client.get(reverse("login"), secure=True)
        return client.post(reverse(name), {**data, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
                           secure=True, HTTP_REFERER="https://testserver/login/", **extra)

    def test_signup_hashes_password_and_grants_only_ordinary_account(self):
        data = self.registration_data(is_staff="True", is_superuser="True", is_active="False")
        response = self.post("register", data)
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(username=data["username"])
        self.assertTrue(user.check_password(data["password1"]))
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertEqual(self.client.session[SESSION_KEY], str(user.pk))
        self.assertEqual(RegisteredEmail.objects.get().user_id, user.pk)
        self.assertEqual(self.client.get("/admin/", secure=True).status_code, 302)

    def test_invalid_signup_and_missing_csrf_do_not_create_accounts(self):
        data = self.registration_data(password1="password", password2="password")
        self.assertEqual(self.client.post(reverse("register"), data, secure=True).status_code, 403)
        self.assertEqual(self.post("register", data).status_code, 200)
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(RegisteredEmail.objects.exists())

    def test_email_race_rolls_back_losing_user_and_returns_form_error(self):
        first = RegistrationForm(self.registration_data(email="Mixed@Example.test"))
        second = RegistrationForm(self.registration_data(username="second", email="mixed@example.test"))
        self.assertTrue(first.is_valid())
        self.assertTrue(second.is_valid())
        first.save()
        with patch("proposal_ai.views.RegistrationForm", return_value=second):
            response = self.post("register", self.registration_data(username="second"))
        self.assertContains(response, "An account with these details already exists.")
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(RegisteredEmail.objects.count(), 1)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_login_limit_applies_across_sessions_and_forwarded_headers(self):
        for _ in range(10):
            self.assertIsNone(consume("auth-account", "target", 10, 600))
        for client in (self.client, Client(enforce_csrf_checks=True)):
            response = self.post("login", {"username": "TARGET", "password": "synthetic"}, client,
                                 HTTP_X_FORWARDED_FOR="198.51.100.55")
            self.assertEqual(response.status_code, 429)
            self.assertGreater(int(response["Retry-After"]), 0)
            self.assertNotIn(SESSION_KEY, client.session)

    def test_peer_limit_cannot_be_bypassed_by_changing_forwarded_headers(self):
        for _ in range(30):
            consume("auth-peer", "127.0.0.1", 30, 600)
        with patch("proposal_ai.views.LoginForm") as form:
            response = self.post("login", {"username": "other", "password": "synthetic"},
                                 HTTP_X_FORWARDED_FOR="203.0.113.23")
        self.assertEqual(response.status_code, 429)
        self.assertTrue(all(call.kwargs.get("data") is None for call in form.call_args_list))

    def test_signup_global_budget_prevents_new_peer_bucket_creation(self):
        for _ in range(200):
            consume("signup-global", "all", 200, 3600)
        response = self.post("register", self.registration_data(), REMOTE_ADDR="203.0.113.20")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(AccountRateLimit.objects.count(), 1)
        self.assertFalse(get_user_model().objects.exists())

    def test_blocked_signup_peer_cannot_consume_the_global_budget(self):
        for _ in range(20):
            consume("signup-peer", "127.0.0.1", 20, 3600)
        self.assertEqual(self.post("register", self.registration_data()).status_code, 429)
        self.assertEqual(AccountRateLimit.objects.count(), 1)

    def test_missing_csrf_does_not_consume_attempt_budget(self):
        self.assertEqual(self.client.post(reverse("login"), {"username": "target"}, secure=True).status_code, 403)
        self.assertFalse(AccountRateLimit.objects.exists())

    def test_throttle_database_failure_is_fail_closed_and_sanitized(self):
        with patch("proposal_ai.account_security.consume", side_effect=OperationalError("private-marker")):
            response = self.post("login", {"username": "target", "password": "synthetic"})
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, "private-marker", status_code=503)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_counter_expiry_and_retention(self):
        self.assertIsNone(consume("test", "identity", 1, 600))
        self.assertIsNotNone(consume("test", "identity", 1, 600))
        AccountRateLimit.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(consume("test", "identity", 1, 600))
        bucket = AccountRateLimit.objects.get()
        self.assertEqual(bucket.attempts, 1)
        self.assertNotIn("identity", bucket.key)
        AccountRateLimit.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        call_command("prune_account_rate_limits", stdout=io.StringIO())
        self.assertFalse(AccountRateLimit.objects.exists())

    def test_admin_login_and_password_change_are_throttled(self):
        for _ in range(30):
            consume("auth-peer", "127.0.0.1", 30, 600)
        self.client.get("/admin/login/", secure=True)
        response = self.client.post("/admin/login/", {"username": "operator", "password": "synthetic",
                                   "csrfmiddlewaretoken": self.client.cookies["csrftoken"].value},
                                   secure=True, HTTP_REFERER="https://testserver/admin/login/")
        self.assertEqual(response.status_code, 429)
        user = get_user_model().objects.create_user(username="ordinary")
        self.client.force_login(user)
        self.assertEqual(self.post("password_change", {}).status_code, 429)

    def test_registration_closure_stays_database_free(self):
        with override_settings(REGISTRATION_ENABLED=False), patch("proposal_ai.account_security.consume") as limiter:
            response = self.post("register", self.registration_data())
        self.assertEqual(response.status_code, 403)
        limiter.assert_not_called()

    def test_existing_login_logout_and_inactive_accounts(self):
        password = self.registration_data()["password1"]
        user = get_user_model().objects.create_user(username="existing", password=password)
        self.assertEqual(self.post("login", {"username": user.username, "password": password}).status_code, 302)
        self.assertEqual(self.client.session[SESSION_KEY], str(user.pk))
        self.post("logout", {})
        self.assertNotIn(SESSION_KEY, self.client.session)
        user.is_active = False
        user.save(update_fields=["is_active"])
        response = self.post("login", {"username": user.username, "password": password})
        self.assertContains(response, "Invalid username or password.")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_public_user_cannot_read_or_mutate_another_users_resources(self):
        owner = get_user_model().objects.create_user(username="owner")
        outsider = get_user_model().objects.create_user(username="outsider")
        FreelancerProfile.objects.create(user=outsider, professional_title="Own title")
        experience = WorkExperience.objects.create(user=owner, job_title="Private role")
        job = JobPost.objects.create(user=owner, job_title="Private opportunity")
        proposal = Proposal.objects.create(user=owner, job_post=job, final_text="Private proposal")
        self.client.force_login(outsider)
        for name, kwargs in (("edit_work_experience", {"experience_id": experience.pk}),
                             ("confirm_job_features", {"job_post_id": job.pk}),
                             ("confirm_use_proposal", {"proposal_id": proposal.pk}),
                             ("update_outcome", {"proposal_id": proposal.pk})):
            path = reverse(name, kwargs=kwargs)
            with self.subTest(route=name):
                self.assertEqual(self.client.get(path, secure=True).status_code, 404)
                self.client.get(reverse("login"), secure=True)
                response = self.client.post(path, {"csrfmiddlewaretoken": self.client.cookies["csrftoken"].value},
                                            secure=True, HTTP_REFERER="https://testserver/login/")
                self.assertEqual(response.status_code, 404)
        self.assertNotContains(self.client.get(reverse("my_experiences"), secure=True), "Private role")
        proposal.refresh_from_db()
        self.assertEqual(proposal.final_text, "Private proposal")
        self.assertFalse(proposal.used_by_user)
