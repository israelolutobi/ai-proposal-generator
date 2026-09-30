import secrets
from unittest.mock import patch

from django.contrib.auth import SESSION_KEY, get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape
from django.views.debug import SafeExceptionReporterFilter

from proposal_ai.models import FreelancerProfile


User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    DEBUG=False,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class AccountTestCase(TestCase):
    def setUp(self):
        super().setUp()
        ai_client = patch(
            "proposal_ai.views.get_openai_client",
            side_effect=AssertionError("Account requests must not call AI."),
        )
        ai_client.start()
        self.addCleanup(ai_client.stop)

    def registration_data(self, **changes):
        password = secrets.token_urlsafe(32)
        data = {
            "username": "newfreelancer",
            "email": "newfreelancer@example.test",
            "password1": password,
            "password2": password,
        }
        data.update(changes)
        return data

    def assert_passwords_hidden(self, response, *passwords):
        html = response.content.decode()
        for password in passwords:
            self.assertFalse(password in html, "A submitted password appeared in HTML.")
            self.assertFalse(
                escape(password) in html, "An escaped submitted password appeared in HTML."
            )


class RegistrationTests(AccountTestCase):
    def assert_invalid_registration(self, data, field, code):
        count = User.objects.count()
        response = self.client.post(reverse("register"), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), count)
        self.assertNotIn(SESSION_KEY, self.client.session)
        errors = response.context["form"].errors.as_data()
        self.assertIn(code, [error.code for error in errors[field]])
        self.assertContains(response, 'role="alert"')
        for message in response.context["form"].errors[field]:
            self.assertContains(response, escape(message))
        return response

    def test_valid_registration_creates_hashed_user_logs_in_and_redirects_to_profile(self):
        data = self.registration_data()
        with self.assertNoLogs("proposal_ai.views", level="DEBUG"):
            response = self.client.post(reverse("register"), data)
        self.assertRedirects(
            response, reverse("create_freelancer_profile"), fetch_redirect_response=False
        )
        self.assertEqual(User.objects.count(), 1)
        user = User.objects.get(username=data["username"])
        self.assertEqual(user.email, data["email"])
        self.assertTrue(user.check_password(data["password1"]))
        self.assertFalse(user.password == data["password1"], "Password stored without hashing.")
        self.assertEqual(self.client.session[SESSION_KEY], str(user.pk))
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_mismatched_passwords_do_not_create_user(self):
        self.assert_invalid_registration(
            self.registration_data(password2=secrets.token_urlsafe(32)),
            "password2", "password_mismatch",
        )

    def test_common_password_is_rejected_by_configured_validator(self):
        self.assert_invalid_registration(
            self.registration_data(password1="password", password2="password"),
            "password2", "password_too_common",
        )

    def test_short_password_is_rejected_by_configured_validator(self):
        self.assert_invalid_registration(
            self.registration_data(password1="aQ!4", password2="aQ!4"),
            "password2", "password_too_short",
        )

    def test_numeric_password_is_rejected_by_configured_validator(self):
        self.assert_invalid_registration(
            self.registration_data(password1="928374650192", password2="928374650192"),
            "password2", "password_entirely_numeric",
        )

    def test_password_similar_to_username_is_rejected(self):
        self.assert_invalid_registration(
            self.registration_data(password1="newfreelancer", password2="newfreelancer"),
            "password2", "password_too_similar",
        )

    def test_password_validation_receives_the_email_attribute(self):
        email = "distinctive-contact@example.test"
        self.assert_invalid_registration(
            self.registration_data(email=email, password1=email, password2=email),
            "password2", "password_too_similar",
        )

    def test_invalid_email_is_rejected(self):
        self.assert_invalid_registration(
            self.registration_data(email="not-an-email"), "email", "invalid"
        )

    def test_duplicate_username_is_rejected_including_case_variants(self):
        User.objects.create_user(username="existing", email="existing@example.test")
        for username in ("existing", "EXISTING"):
            with self.subTest(username=username):
                self.assert_invalid_registration(
                    self.registration_data(username=username), "username", "unique"
                )

    def test_duplicate_email_is_rejected_including_case_variants(self):
        # Form-level policy only: database-level case-insensitive uniqueness is
        # a separate future decision and is not guaranteed by this test.
        User.objects.create_user(username="existing", email="existing@example.test")
        for email in ("existing@example.test", "EXISTING@EXAMPLE.TEST"):
            with self.subTest(email=email):
                self.assert_invalid_registration(
                    self.registration_data(email=email), "email", "duplicate_email"
                )

    def test_username_uses_django_character_and_length_validation(self):
        for username, code in (("invalid username!", "invalid"), ("x" * 151, "max_length")):
            with self.subTest(code=code):
                self.assert_invalid_registration(
                    self.registration_data(username=username), "username", code
                )

    def test_all_registration_fields_are_required_on_the_server(self):
        for field in ("username", "email", "password1", "password2"):
            for missing in (False, True):
                with self.subTest(field=field, missing=missing):
                    data = self.registration_data()
                    if missing:
                        data.pop(field)
                    else:
                        data[field] = ""
                    self.assert_invalid_registration(data, field, "required")

    def test_get_renders_csrf_protected_form_without_creating_user(self):
        response = self.client.get(reverse("register"), self.registration_data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertFalse(response.context["form"].is_bound)
        self.assertEqual(User.objects.count(), 0)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_invalid_registration_never_renders_or_logs_passwords(self):
        data = self.registration_data(email="bad-email")
        data["password1"] += '<private-test-marker>'
        data["password2"] = data["password1"]
        with self.assertNoLogs("proposal_ai.views", level="DEBUG"):
            response = self.assert_invalid_registration(data, "email", "invalid")
        self.assert_passwords_hidden(response, data["password1"])
        filtered = SafeExceptionReporterFilter().get_post_parameters(response.wsgi_request)
        for name in ("password1", "password2"):
            self.assertEqual(filtered[name], "********************")
        self.assertContains(response, 'value="newfreelancer"')
        self.assertContains(response, 'value="bad-email"')

    def test_retained_username_is_html_escaped(self):
        username = '\"><script>alert(1)</script>'
        response = self.assert_invalid_registration(
            self.registration_data(username=username), "username", "invalid"
        )
        self.assertContains(response, escape(username))
        self.assertNotContains(response, "<script>")


class LoginTests(AccountTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = secrets.token_urlsafe(32)
        cls.user = User.objects.create_user(
            username="returningfreelancer", email="returning@example.test", password=cls.password
        )
        FreelancerProfile.objects.create(user=cls.user, professional_title="Test freelancer")

    def test_valid_login_redirects_to_dashboard_and_authenticated_user_reaches_it(self):
        with self.assertNoLogs("proposal_ai.views", level="DEBUG"):
            response = self.client.post(
                reverse("login"), {"username": self.user.username, "password": self.password}
            )
        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))
        dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertTrue(dashboard.wsgi_request.user.is_authenticated)
        self.assertTemplateUsed(dashboard, "home.html")

    def test_wrong_password_and_unknown_username_have_same_visible_error(self):
        for username in (self.user.username, "unknown-account"):
            with self.subTest(existing=username == self.user.username):
                password = secrets.token_urlsafe(32)
                with self.assertNoLogs("proposal_ai.views", level="DEBUG"):
                    response = self.client.post(
                        reverse("login"), {"username": username, "password": password}
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    list(response.context["form"].non_field_errors()),
                    ["Invalid username or password."],
                )
                self.assertContains(response, "Invalid username or password.")
                self.assertNotIn(SESSION_KEY, self.client.session)
                self.assert_passwords_hidden(response, password)

    def test_inactive_account_uses_the_same_generic_error(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        response = self.client.post(
            reverse("login"), {"username": self.user.username, "password": self.password}
        )
        self.assertContains(response, "Invalid username or password.")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_missing_login_fields_return_required_errors(self):
        response = self.client.post(reverse("login"), {})
        self.assertEqual(response.status_code, 200)
        for field in ("username", "password"):
            self.assertEqual(response.context["form"].errors.as_data()[field][0].code, "required")
        self.assertContains(response, "This field is required.")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_get_does_not_authenticate_even_with_credentials_in_query(self):
        response = self.client.get(
            reverse("login"), {"username": self.user.username, "password": self.password}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertFalse(response.context["form"].is_bound)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assert_passwords_hidden(response, self.password)

    def test_login_password_is_redacted_for_exception_reporting(self):
        password = secrets.token_urlsafe(32)
        response = self.client.post(
            reverse("login"), {"username": self.user.username, "password": password}
        )
        self.assert_passwords_hidden(response, password)
        filtered = SafeExceptionReporterFilter().get_post_parameters(response.wsgi_request)
        self.assertEqual(filtered["password"], "********************")


class AccountSecurityTests(AccountTestCase):
    def csrf_post(self, client, route, data):
        url = reverse(route)
        page = client.get(url, secure=True)
        self.assertEqual(page.status_code, 200)
        return client.post(
            url,
            {**data, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
            secure=True,
            HTTP_REFERER="https://testserver" + url,
        )

    def test_registration_and_login_reject_post_without_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        for route in ("register", "login"):
            with self.subTest(route=route):
                response = client.post(reverse(route), self.registration_data())
                self.assertEqual(response.status_code, 403)
                self.assertEqual(User.objects.count(), 0)
                self.assertNotIn(SESSION_KEY, client.session)

    def test_registration_and_login_work_with_valid_csrf_tokens(self):
        data = self.registration_data()
        client = Client(enforce_csrf_checks=True)
        response = self.csrf_post(client, "register", data)
        self.assertRedirects(
            response, reverse("create_freelancer_profile"), fetch_redirect_response=False
        )
        user = User.objects.get()
        self.assertEqual(client.session[SESSION_KEY], str(user.pk))
        login_client = Client(enforce_csrf_checks=True)
        response = self.csrf_post(
            login_client, "login", {"username": data["username"], "password": data["password1"]}
        )
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(login_client.session[SESSION_KEY], str(user.pk))

    def test_other_http_methods_do_not_create_or_authenticate_accounts(self):
        for route in ("register", "login"):
            for method in ("put", "patch", "delete"):
                with self.subTest(route=route, method=method):
                    response = getattr(self.client, method)(reverse(route), self.registration_data())
                    self.assertEqual(response.status_code, 405)
                    self.assertEqual(User.objects.count(), 0)
                    self.assertNotIn(SESSION_KEY, self.client.session)

    def test_logout_changes_session_only_on_csrf_protected_post(self):
        user = User.objects.create_user(username="logout-test")
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        response = client.get(reverse("logout"), secure=True)
        self.assertRedirects(response, reverse("public_home"), fetch_redirect_response=False)
        self.assertIn(SESSION_KEY, client.session)
        response = client.post(reverse("logout"), secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertIn(SESSION_KEY, client.session)
        page = client.get(reverse("login"), secure=True)
        self.assertEqual(page.status_code, 200)
        response = client.post(
            reverse("logout"), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value},
            secure=True, HTTP_REFERER="https://testserver/login/",
        )
        self.assertRedirects(response, reverse("public_home"), fetch_redirect_response=False)
        self.assertNotIn(SESSION_KEY, client.session)
