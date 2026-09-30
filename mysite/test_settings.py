import io
import os
from pathlib import Path
import runpy
import secrets
import subprocess
import sys
import unittest
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from dotenv import load_dotenv


SETTINGS_PATH = Path(__file__).with_name("settings.py")


class SettingsTests(unittest.TestCase):
    def load_settings(self, environment, dotenv_text=""):
        # Isolate these checks from real credentials and the developer's .env.
        with patch.dict(os.environ, environment, clear=True), patch(
            "dotenv.load_dotenv",
            side_effect=lambda: load_dotenv(stream=io.StringIO(dotenv_text)),
        ):
            return runpy.run_path(str(SETTINGS_PATH))

    def test_missing_or_blank_secret_key_prevents_startup(self):
        for environment in ({}, {"SECRET_KEY": ""}, {"SECRET_KEY": " \t\n"}):
            with self.subTest(environment_present=bool(environment)):
                with self.assertRaisesRegex(
                    ImproperlyConfigured,
                    "SECRET_KEY must be set in the environment or local .env file",
                ):
                    self.load_settings(environment)

    def test_supplied_secret_key_is_preserved(self):
        secret_key = secrets.token_urlsafe(48)
        settings = self.load_settings({"SECRET_KEY": secret_key})
        self.assertTrue(
            settings["SECRET_KEY"] == secret_key,
            "Settings must preserve the environment's signing key.",
        )

    def test_debug_defaults_off_with_secure_cookies(self):
        settings = self.load_settings({"SECRET_KEY": secrets.token_urlsafe(48)})
        self.assertFalse(settings["DEBUG"])
        self.assertTrue(settings["SESSION_COOKIE_SECURE"])
        self.assertTrue(settings["CSRF_COOKIE_SECURE"])

    def test_explicit_development_debug_enables_http_cookies(self):
        for value in ("True", "true", "TRUE", " True "):
            with self.subTest(debug=value):
                settings = self.load_settings(
                    {"SECRET_KEY": secrets.token_urlsafe(48), "DEBUG": value}
                )
                self.assertTrue(settings["DEBUG"])
                self.assertFalse(settings["SESSION_COOKIE_SECURE"])
                self.assertFalse(settings["CSRF_COOKIE_SECURE"])

    def test_explicit_debug_false_enables_secure_cookies(self):
        for value in ("False", "false", "FALSE", " False "):
            with self.subTest(debug=value):
                settings = self.load_settings(
                    {"SECRET_KEY": secrets.token_urlsafe(48), "DEBUG": value}
                )
                self.assertFalse(settings["DEBUG"])
                self.assertTrue(settings["SESSION_COOKIE_SECURE"])
                self.assertTrue(settings["CSRF_COOKIE_SECURE"])

    def test_non_true_values_do_not_enable_debug(self):
        for value in ("", "1", "yes", "unexpected"):
            with self.subTest(debug=value):
                settings = self.load_settings(
                    {"SECRET_KEY": secrets.token_urlsafe(48), "DEBUG": value}
                )
                self.assertFalse(settings["DEBUG"])

    def test_local_dotenv_can_explicitly_enable_development_debug(self):
        settings = self.load_settings(
            {"SECRET_KEY": secrets.token_urlsafe(48)}, dotenv_text="DEBUG=True\n"
        )
        self.assertTrue(settings["DEBUG"])

    def test_environment_debug_takes_precedence_over_dotenv(self):
        settings = self.load_settings(
            {"SECRET_KEY": secrets.token_urlsafe(48), "DEBUG": "False"},
            dotenv_text="DEBUG=True\n",
        )
        self.assertFalse(settings["DEBUG"])
        self.assertTrue(settings["SESSION_COOKIE_SECURE"])
        self.assertTrue(settings["CSRF_COOKIE_SECURE"])

    def test_manage_check_without_secret_key_fails_without_a_fallback(self):
        environment = os.environ.copy()
        environment.pop("SECRET_KEY", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        script = """
import runpy
import sys
from unittest.mock import patch

sys.argv = ["manage.py", "check"]
with patch("dotenv.load_dotenv", return_value=False):
    runpy.run_path("manage.py", run_name="__main__")
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=SETTINGS_PATH.parent.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "ImproperlyConfigured: SECRET_KEY must be set in the environment "
            "or local .env file.",
            result.stderr,
        )


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    DEBUG=False,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class ProtectedPageRedirectTests(SimpleTestCase):
    def test_anonymous_dashboard_request_redirects_to_existing_login_page(self):
        self.assertEqual(reverse("login"), "/login/")
        with patch(
            "proposal_ai.views.get_openai_client",
            side_effect=AssertionError("This request must not call AI."),
        ):
            response = self.client.get(reverse("dashboard"))
            self.assertRedirects(response, "/login/?next=/dashboard/")
