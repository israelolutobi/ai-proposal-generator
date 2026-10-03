"""Configuration/static checks using synthetic values, no infrastructure access."""
import io
from contextlib import contextmanager
import json
import os
from pathlib import Path
import runpy
import secrets
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command, CommandError
from django.middleware.security import SecurityMiddleware
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse

from mysite.configuration import validate_production
from mysite.runtime import gunicorn_configuration, validate_gunicorn_startup, validate_runtime


ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "mysite" / "settings.py"
MANIFEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}


def environment(production=True, **changes):
    values = {"SECRET_KEY": secrets.token_urlsafe(64)}
    if production:
        values.update(APP_ENV="production", DEBUG="False", ALLOWED_HOSTS="deployment.invalid",
                      DATABASE_URL="postgresql://test_user@127.0.0.1:1/disposable")
    values.update(changes)
    return values


def load_settings(values):
    with patch.dict(os.environ, values, clear=True), patch("dotenv.load_dotenv", return_value=False):
        return runpy.run_path(str(SETTINGS_PATH))


def production_overrides(**changes):
    parsed = load_settings(environment(**changes))
    names = ("APP_ENV", "SECRET_KEY", "DEBUG", "DATABASE_URL", "DATABASES", "ALLOWED_HOSTS",
             "CSRF_TRUSTED_ORIGINS", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE",
             "SECURE_SSL_REDIRECT", "SECURE_HSTS_SECONDS", "SECURE_HSTS_INCLUDE_SUBDOMAINS",
             "SECURE_HSTS_PRELOAD", "SECURE_PROXY_SSL_HEADER", "TRUST_PROXY_HEADERS",
              "DEPLOYMENT_RUNTIME", "AI_ENABLED", "AI_GLOBAL_DAILY_CREDITS", "AI_GLOBAL_WEEKLY_CREDITS",
              "REGISTRATION_ENABLED")
    return {name: parsed[name] for name in names}


@contextmanager
def configured_production(values=None, **changes):
    values = dict(values if values is not None else production_overrides(**changes))
    database = values.pop("DATABASES")
    # These are configuration-only tests. Expose the synthetic database settings
    # to the validator without reconfiguring the ordinary test's ORM connections.
    with override_settings(**values), patch.object(settings, "DATABASES", database):
        yield


class ConfigurationTests(unittest.TestCase):
    def rejected(self, values, name):
        with self.assertRaisesRegex(ImproperlyConfigured, name):
            load_settings(values)

    def test_mode_defaults_to_development(self):
        parsed = load_settings(environment(False))
        self.assertEqual(parsed["APP_ENV"], "development")
        self.assertFalse(parsed["DEBUG"])

    def test_explicit_development_mode(self):
        parsed = load_settings(environment(False, APP_ENV="development", DEBUG="True"))
        self.assertFalse(parsed["IS_PRODUCTION"])
        self.assertTrue(parsed["DEBUG"])

    def test_explicit_production_mode(self):
        self.assertTrue(load_settings(environment())["IS_PRODUCTION"])

    def test_mode_is_case_insensitive_and_trimmed(self):
        self.assertEqual(load_settings(environment(APP_ENV=" Production "))["APP_ENV"], "production")

    def test_invalid_mode_is_rejected(self):
        for mode in ("", "staging", "prod", "unexpected"):
            with self.subTest(mode=mode):
                self.rejected(environment(APP_ENV=mode), "APP_ENV")

    def test_debug_false_does_not_imply_production(self):
        parsed = load_settings(environment(False, DEBUG="False"))
        self.assertEqual(parsed["APP_ENV"], "development")
        self.assertEqual(parsed["DATABASES"]["default"]["ENGINE"], "django.db.backends.sqlite3")

    def test_development_sqlite_fallback(self):
        parsed = load_settings(environment(False, DEBUG="True"))
        self.assertEqual(parsed["DATABASES"]["default"]["NAME"], ROOT / "db.sqlite3")

    def test_blank_development_database_url_uses_sqlite(self):
        for url in ("", " \t"):
            with self.subTest(blank_url=True):
                parsed = load_settings(environment(False, DATABASE_URL=url))
                self.assertEqual(parsed["DATABASES"]["default"]["ENGINE"], "django.db.backends.sqlite3")

    def test_production_missing_database_is_rejected(self):
        values = environment()
        values.pop("DATABASE_URL")
        self.rejected(values, "DATABASE_URL is required")

    def test_production_blank_database_is_rejected(self):
        for url in ("", " \t"):
            with self.subTest(blank_url=True):
                self.rejected(environment(DATABASE_URL=url), "DATABASE_URL is required")

    def test_production_sqlite_is_rejected(self):
        self.rejected(environment(DATABASE_URL="sqlite:///:memory:"), "PostgreSQL")

    def test_production_other_backend_is_rejected(self):
        self.rejected(environment(DATABASE_URL="mysql://test_user@127.0.0.1/disposable"), "PostgreSQL")

    def test_unknown_database_scheme_is_rejected(self):
        self.rejected(environment(DATABASE_URL="unsupported://test_user@127.0.0.1/disposable"), "DATABASE_URL is invalid")

    def test_postgresql_aliases_and_health_checks(self):
        for scheme in ("postgres", "postgresql", "pgsql"):
            with self.subTest(scheme=scheme):
                parsed = load_settings(environment(DATABASE_URL=f"{scheme}://test_user@127.0.0.1:1/disposable"))
                database = parsed["DATABASES"]["default"]
                self.assertEqual(database["ENGINE"], "django.db.backends.postgresql")
                self.assertEqual(database["CONN_MAX_AGE"], 600)
                self.assertTrue(database["CONN_HEALTH_CHECKS"])

    def test_production_database_requires_name(self):
        self.rejected(environment(DATABASE_URL="postgresql://test_user@127.0.0.1/"), "named PostgreSQL")

    def test_database_tls_options_are_preserved(self):
        parsed = load_settings(environment(DATABASE_URL="postgresql://test_user@127.0.0.1/disposable?sslmode=verify-full&sslrootcert=/operator/ca.pem"))
        self.assertEqual(parsed["DATABASES"]["default"]["OPTIONS"],
                         {"sslmode": "verify-full", "sslrootcert": "/operator/ca.pem"})

    def test_database_parse_failure_does_not_expose_password(self):
        password = secrets.token_urlsafe(24)
        with self.assertRaises(ImproperlyConfigured) as captured:
            load_settings(environment(DATABASE_URL=f"postgresql://test_user:{password}@127.0.0.1:invalid/disposable"))
        self.assertTrue(password not in str(captured.exception), "Configuration errors must omit credentials.")
        self.assertTrue(captured.exception.__suppress_context__)

    def test_unknown_scheme_error_does_not_echo_url(self):
        marker = secrets.token_hex(16)
        with self.assertRaises(ImproperlyConfigured) as captured:
            load_settings(environment(DATABASE_URL=f"{marker}://test_user@127.0.0.1/disposable"))
        self.assertTrue(marker not in str(captured.exception), "Unknown schemes must not be echoed.")

    def test_database_url_fragment_is_rejected(self):
        self.rejected(environment(DATABASE_URL="postgresql://test_user@127.0.0.1/disposable#ignored"), "DATABASE_URL is invalid")

    def test_production_debug_true_is_rejected(self):
        self.rejected(environment(DEBUG="True"), "DEBUG must be False")

    def test_production_debug_false_is_accepted(self):
        self.assertFalse(load_settings(environment(DEBUG=" fAlSe "))["DEBUG"])

    def test_production_debug_defaults_false(self):
        values = environment()
        values.pop("DEBUG")
        self.assertFalse(load_settings(values)["DEBUG"])

    def test_invalid_debug_is_rejected(self):
        self.rejected(environment(DEBUG="yes"), "DEBUG must be True or False")

    def test_required_secret_key_behavior_remains(self):
        for key in (None, "", " \t"):
            values = environment()
            if key is None:
                values.pop("SECRET_KEY")
            else:
                values["SECRET_KEY"] = key
            with self.subTest(key_present=key is not None):
                self.rejected(values, "SECRET_KEY must be set")

    def test_short_production_key_is_rejected(self):
        self.rejected(environment(SECRET_KEY=secrets.token_urlsafe(16)), "Production SECRET_KEY")

    def test_low_diversity_key_is_rejected(self):
        self.rejected(environment(SECRET_KEY="a" * 64), "Production SECRET_KEY")

    def test_insecure_or_placeholder_key_is_rejected(self):
        for prefix in ("django-insecure-", "change-me", "replace-me", "your-secret", "placeholder", "example"):
            with self.subTest(prefix=prefix):
                self.rejected(environment(SECRET_KEY=prefix + secrets.token_urlsafe(64)), "Production SECRET_KEY")

    def test_strong_key_is_preserved(self):
        values = environment()
        self.assertTrue(load_settings(values)["SECRET_KEY"] == values["SECRET_KEY"], "Signing keys must be preserved.")

    def test_key_error_does_not_expose_key(self):
        key = secrets.token_urlsafe(12)
        with self.assertRaises(ImproperlyConfigured) as captured:
            load_settings(environment(SECRET_KEY=key))
        self.assertTrue(key not in str(captured.exception), "Configuration errors must omit signing keys.")

    def test_development_key_requirement_does_not_gain_production_length_rule(self):
        self.assertFalse(load_settings(environment(False, SECRET_KEY=secrets.token_urlsafe(16)))["IS_PRODUCTION"])

    def test_development_hosts_keep_localhost_defaults(self):
        self.assertEqual(load_settings(environment(False))["ALLOWED_HOSTS"], ["localhost", "127.0.0.1"])

    def test_production_missing_or_blank_hosts_are_rejected(self):
        for value in (None, "", " , "):
            values = environment()
            if value is None:
                values.pop("ALLOWED_HOSTS")
            else:
                values["ALLOWED_HOSTS"] = value
            with self.subTest(hosts_present=value is not None):
                self.rejected(values, "ALLOWED_HOSTS")

    def test_wildcard_hosts_are_rejected(self):
        for hosts in ("*", "deployment.invalid,*", "*.deployment.invalid"):
            with self.subTest(hosts=hosts):
                self.rejected(environment(ALLOWED_HOSTS=hosts), "ALLOWED_HOSTS")

    def test_malformed_hosts_are_rejected(self):
        for hosts in ("https://deployment.invalid", "deployment.invalid:443", "deployment.invalid/path",
                      "user@deployment.invalid", "bad..invalid", "-bad.invalid", "bad_.invalid", "999.999.999.999"):
            with self.subTest(hosts=hosts):
                self.rejected(environment(ALLOWED_HOSTS=hosts), "ALLOWED_HOSTS")

    def test_explicit_dns_and_ip_hosts_are_accepted(self):
        parsed = load_settings(environment(ALLOWED_HOSTS=" deployment.invalid , .subdomain.invalid , 127.0.0.1, [::1]"))
        self.assertEqual(parsed["ALLOWED_HOSTS"], ["deployment.invalid", ".subdomain.invalid", "127.0.0.1", "[::1]"])

    def test_empty_csrf_origins_are_valid_for_same_origin(self):
        self.assertEqual(load_settings(environment(CSRF_TRUSTED_ORIGINS=""))["CSRF_TRUSTED_ORIGINS"], [])

    def test_valid_https_csrf_origins(self):
        origins = "https://deployment.invalid,https://other.invalid:8443"
        self.assertEqual(load_settings(environment(CSRF_TRUSTED_ORIGINS=origins))["CSRF_TRUSTED_ORIGINS"], origins.split(","))

    def test_csrf_origins_not_derived_from_hosts(self):
        self.assertEqual(load_settings(environment(ALLOWED_HOSTS="deployment.invalid,other.invalid"))["CSRF_TRUSTED_ORIGINS"], [])

    def test_malformed_csrf_origins_are_rejected(self):
        for origin in ("deployment.invalid", "https://deployment.invalid/path", "https://deployment.invalid/",
                       "https://deployment.invalid?query=1", "https://deployment.invalid#fragment",
                       "https://user@deployment.invalid", "ftp://deployment.invalid", "https://*",
                       "https://.deployment.invalid", "https://deployment.invalid:", "https://a\nb.invalid"):
            with self.subTest(origin=origin):
                self.rejected(environment(CSRF_TRUSTED_ORIGINS=origin), "CSRF_TRUSTED_ORIGINS")

    def test_invalid_csrf_port_is_rejected(self):
        for port in ("bad", "0", "65536"):
            with self.subTest(port=port):
                self.rejected(environment(CSRF_TRUSTED_ORIGINS=f"https://deployment.invalid:{port}"), "CSRF_TRUSTED_ORIGINS")

    def test_production_http_csrf_origin_is_rejected(self):
        self.rejected(environment(CSRF_TRUSTED_ORIGINS="http://deployment.invalid"), "HTTPS origins")

    def test_development_http_csrf_origin_is_accepted(self):
        self.assertEqual(load_settings(environment(False, CSRF_TRUSTED_ORIGINS="http://localhost:8000"))["CSRF_TRUSTED_ORIGINS"], ["http://localhost:8000"])

    def test_csrf_explicit_wildcard_subdomain_and_ipv6_syntax(self):
        origins = "https://*.deployment.invalid,https://[::1]:8443"
        self.assertEqual(load_settings(environment(CSRF_TRUSTED_ORIGINS=origins))["CSRF_TRUSTED_ORIGINS"], origins.split(","))

    def test_production_redirect_defaults_on(self):
        self.assertTrue(load_settings(environment())["SECURE_SSL_REDIRECT"])

    def test_production_redirect_cannot_be_disabled(self):
        self.rejected(environment(SECURE_SSL_REDIRECT="False"), "SECURE_SSL_REDIRECT must be True")

    def test_development_http_configuration(self):
        parsed = load_settings(environment(False, DEBUG="True"))
        for name in ("SECURE_SSL_REDIRECT", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE"):
            self.assertFalse(parsed[name])
        self.assertEqual(parsed["SECURE_HSTS_SECONDS"], 0)

    def test_production_cookies_are_secure(self):
        parsed = load_settings(environment())
        self.assertTrue(parsed["SESSION_COOKIE_SECURE"])
        self.assertTrue(parsed["CSRF_COOKIE_SECURE"])

    def test_staged_hsts_defaults(self):
        parsed = load_settings(environment())
        self.assertEqual(parsed["SECURE_HSTS_SECONDS"], 300)
        self.assertFalse(parsed["SECURE_HSTS_INCLUDE_SUBDOMAINS"])
        self.assertFalse(parsed["SECURE_HSTS_PRELOAD"])

    def test_hsts_duration_can_be_explicitly_increased(self):
        self.assertEqual(load_settings(environment(SECURE_HSTS_SECONDS="86400"))["SECURE_HSTS_SECONDS"], 86400)

    def test_zero_production_hsts_is_rejected(self):
        self.rejected(environment(SECURE_HSTS_SECONDS="0"), "SECURE_HSTS_SECONDS must be positive")

    def test_invalid_hsts_integer_is_rejected(self):
        for value in ("", "-1", "1.5", "300s", "True", "2147483648", "9" * 5000):
            with self.subTest(kind="invalid integer"):
                self.rejected(environment(SECURE_HSTS_SECONDS=value), "SECURE_HSTS_SECONDS")

    def test_security_booleans_are_strict(self):
        for name in ("SECURE_SSL_REDIRECT", "SECURE_HSTS_INCLUDE_SUBDOMAINS", "SECURE_HSTS_PRELOAD", "TRUST_PROXY_HEADERS"):
            for value in ("", "1", "yes", "unexpected"):
                with self.subTest(setting=name, value=value):
                    self.rejected(environment(**{name: value}), name)

    def test_preload_requires_deliberate_domain_policy(self):
        for changes in ({"SECURE_HSTS_PRELOAD": "True"},
                        {"SECURE_HSTS_PRELOAD": "True", "SECURE_HSTS_INCLUDE_SUBDOMAINS": "True"}):
            with self.subTest(policy=changes):
                self.rejected(environment(**changes), "HSTS preload requires")

    def test_explicit_complete_preload_configuration_is_accepted(self):
        parsed = load_settings(environment(SECURE_HSTS_PRELOAD="True", SECURE_HSTS_INCLUDE_SUBDOMAINS="True", SECURE_HSTS_SECONDS="31536000"))
        self.assertTrue(parsed["SECURE_HSTS_PRELOAD"])

    def test_proxy_trust_defaults_off_in_production(self):
        parsed = load_settings(environment())
        self.assertFalse(parsed["TRUST_PROXY_HEADERS"])
        self.assertIsNone(parsed["SECURE_PROXY_SSL_HEADER"])

    def test_proxy_trust_is_explicit_and_case_insensitive(self):
        parsed = load_settings(environment(TRUST_PROXY_HEADERS=" tRuE "))
        self.assertEqual(parsed["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https"))

    def test_explicit_false_proxy_trust(self):
        self.assertIsNone(load_settings(environment(TRUST_PROXY_HEADERS="False"))["SECURE_PROXY_SSL_HEADER"])

    def test_development_proxy_trust_defaults_off(self):
        self.assertIsNone(load_settings(environment(False, DEBUG="True"))["SECURE_PROXY_SSL_HEADER"])

    def test_development_proxy_trust_is_rejected(self):
        self.rejected(environment(False, TRUST_PROXY_HEADERS="True"), "only in explicit production mode")

    def test_proxy_and_insecure_redirect_combination_is_rejected(self):
        self.rejected(environment(TRUST_PROXY_HEADERS="True", SECURE_SSL_REDIRECT="False"), "SECURE_SSL_REDIRECT")

    def test_effective_proxy_setting_must_match_flag(self):
        parsed = load_settings(environment())
        parsed["SECURE_PROXY_SSL_HEADER"] = ("HTTP_X_FORWARDED_PROTO", "https")
        with self.assertRaisesRegex(ImproperlyConfigured, "must match"):
            validate_production(parsed)


class RegistrationConfigurationTests(unittest.TestCase):
    def parsed(self, production=False, **changes):
        return load_settings(environment(production, **changes))

    def rejected(self, production=False, **changes):
        with self.assertRaisesRegex(ImproperlyConfigured, "REGISTRATION_ENABLED"):
            self.parsed(production, **changes)

    def test_development_missing_enables_registration(self):
        self.assertTrue(self.parsed()["REGISTRATION_ENABLED"])

    def test_development_true_enables_registration(self):
        self.assertTrue(self.parsed(REGISTRATION_ENABLED="True")["REGISTRATION_ENABLED"])

    def test_development_false_disables_registration(self):
        self.assertFalse(self.parsed(REGISTRATION_ENABLED="False")["REGISTRATION_ENABLED"])

    def test_development_case_and_whitespace(self):
        for value, expected in ((" tRuE ", True), ("\tFaLsE\n", False)):
            with self.subTest(value=value):
                self.assertIs(self.parsed(REGISTRATION_ENABLED=value)["REGISTRATION_ENABLED"], expected)

    def test_development_blank_is_rejected(self):
        for value in ("", " \t"):
            with self.subTest(blank=True):
                self.rejected(REGISTRATION_ENABLED=value)

    def test_development_invalid_is_rejected(self):
        for value in ("yes", "1", "0", "enabled", "unexpected"):
            with self.subTest(value=value):
                self.rejected(REGISTRATION_ENABLED=value)

    def test_production_missing_disables_registration(self):
        self.assertFalse(self.parsed(True)["REGISTRATION_ENABLED"])

    def test_production_false_disables_registration(self):
        self.assertFalse(self.parsed(True, REGISTRATION_ENABLED="False")["REGISTRATION_ENABLED"])

    def test_production_false_case_and_whitespace(self):
        self.assertFalse(self.parsed(True, REGISTRATION_ENABLED=" fAlSe ")["REGISTRATION_ENABLED"])

    def test_production_true_is_rejected(self):
        self.rejected(True, REGISTRATION_ENABLED="True")

    def test_production_true_case_and_whitespace_is_rejected(self):
        self.rejected(True, REGISTRATION_ENABLED=" tRuE ")

    def test_production_blank_is_rejected(self):
        for value in ("", " \t"):
            with self.subTest(blank=True):
                self.rejected(True, REGISTRATION_ENABLED=value)

    def test_production_invalid_is_rejected(self):
        for value in ("yes", "1", "0", "enabled", "unexpected"):
            with self.subTest(value=value):
                self.rejected(True, REGISTRATION_ENABLED=value)

    def test_development_debug_false_does_not_close_registration(self):
        parsed = self.parsed(DEBUG="False")
        self.assertEqual(parsed["APP_ENV"], "development")
        self.assertTrue(parsed["REGISTRATION_ENABLED"])

    def test_database_and_https_do_not_infer_production(self):
        parsed = self.parsed(DATABASE_URL="postgresql://test_user@127.0.0.1:1/disposable",
                             SECURE_SSL_REDIRECT="True")
        self.assertEqual(parsed["APP_ENV"], "development")
        self.assertTrue(parsed["REGISTRATION_ENABLED"])

    def test_effective_production_policy_cannot_enable_registration(self):
        parsed = self.parsed(True)
        parsed["REGISTRATION_ENABLED"] = True
        with self.assertRaisesRegex(ImproperlyConfigured, "REGISTRATION_ENABLED"):
            validate_production(parsed)

    def test_effective_policy_rejects_non_boolean_values(self):
        for value in (None, "False", 0, 1):
            parsed = self.parsed()
            parsed["REGISTRATION_ENABLED"] = value
            with self.subTest(value=value):
                with self.assertRaisesRegex(ImproperlyConfigured, "REGISTRATION_ENABLED"):
                    validate_production(parsed)

    def test_registration_error_does_not_echo_supplied_value(self):
        marker = secrets.token_urlsafe(32)
        with self.assertRaises(ImproperlyConfigured) as captured:
            self.parsed(True, REGISTRATION_ENABLED=marker)
        self.assertNotIn(marker, str(captured.exception))


class RuntimeTests(unittest.TestCase):
    def test_explicit_worker_and_graceful_timeouts(self):
        parsed = gunicorn_configuration({})
        self.assertEqual(parsed["timeout"], 90)
        self.assertEqual(parsed["graceful_timeout"], 105)

    def test_sync_workers_and_no_reload(self):
        parsed = gunicorn_configuration({})
        self.assertEqual(parsed["worker_class"], "sync")
        self.assertEqual(parsed["threads"], 1)
        self.assertFalse(parsed["reload"])

    def test_worker_count_default_and_override(self):
        self.assertEqual(gunicorn_configuration({})["workers"], 1)
        self.assertEqual(gunicorn_configuration({"WEB_CONCURRENCY": "3"})["workers"], 3)

    def test_invalid_worker_counts_are_rejected(self):
        for value in ("0", "-1", "1.5", "", "many"):
            with self.subTest(workers=value):
                with self.assertRaisesRegex(ImproperlyConfigured, "WEB_CONCURRENCY"):
                    gunicorn_configuration({"WEB_CONCURRENCY": value})

    def test_port_controls_hosted_binding(self):
        self.assertEqual(gunicorn_configuration({"PORT": "43210"})["bind"], "0.0.0.0:43210")

    def test_default_binding_is_explicit(self):
        self.assertEqual(gunicorn_configuration({})["bind"], "0.0.0.0:8000")

    def test_invalid_ports_are_rejected(self):
        for value in ("0", "65536", "-1", "8000.0", "", "invalid"):
            with self.subTest(port=value):
                with self.assertRaisesRegex(ImproperlyConfigured, "PORT"):
                    gunicorn_configuration({"PORT": value})

    def test_gunicorn_environment_overrides_cannot_bypass_policy(self):
        for arguments in ("--timeout 30", "--timeout invalid", "--reload", "--threads 8", "--graceful-timeout 0"):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ImproperlyConfigured, "GUNICORN_CMD_ARGS overrides"):
                    gunicorn_configuration({"GUNICORN_CMD_ARGS": arguments})

    def test_invalid_effective_timeout_is_rejected(self):
        for name, value in (("timeout", 30), ("graceful_timeout", 30), ("timeout", 0), ("reload", True)):
            parsed = gunicorn_configuration({})
            parsed[name] = value
            with self.subTest(setting=name, value=value):
                with self.assertRaisesRegex(ImproperlyConfigured, "reviewed sync-worker policy"):
                    validate_runtime(parsed)

    def test_gunicorn_does_not_independently_trust_forwarded_scheme(self):
        parsed = gunicorn_configuration({"FORWARDED_ALLOW_IPS": "*"})
        self.assertEqual(parsed["forwarded_allow_ips"], "")
        self.assertEqual(parsed["secure_scheme_headers"], {})

    def test_config_file_executes_same_runtime_contract(self):
        values = {"PORT": "43210", "WEB_CONCURRENCY": "2"}
        with patch.dict(os.environ, values, clear=True), patch("dotenv.load_dotenv", return_value=False):
            loaded = runpy.run_path(str(ROOT / "gunicorn.conf.py"))
        for name, value in gunicorn_configuration(values).items():
            self.assertEqual(loaded[name], value)

    def test_procfile_only_starts_web_application_with_config(self):
        self.assertEqual((ROOT / "Procfile").read_text().strip(),
                         "web: gunicorn --config gunicorn.conf.py mysite.wsgi:application")

    def server(self, **changes):
        configuration = gunicorn_configuration({})
        configuration.update(changes)
        return SimpleNamespace(cfg=SimpleNamespace(settings={
            name: SimpleNamespace(get=lambda value=value: value)
            for name, value in configuration.items()
        }))

    def test_startup_hook_accepts_gunicorn_parsed_values(self):
        validate_gunicorn_startup(self.server(forwarded_allow_ips=[]))

    def test_startup_hook_rejects_cli_timeout_or_worker_override(self):
        for changes in ({"timeout": 30}, {"graceful_timeout": 30}, {"threads": 2}, {"workers": 0}):
            with self.subTest(changes=changes):
                with self.assertRaises(ImproperlyConfigured):
                    validate_gunicorn_startup(self.server(**changes))

    def test_startup_hook_rejects_gunicorn_proxy_trust_override(self):
        for changes in ({"forwarded_allow_ips": ["*"]}, {"secure_scheme_headers": {"X-FORWARDED-PROTO": "https"}}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ImproperlyConfigured, "forwarded-scheme inference"):
                    validate_gunicorn_startup(self.server(**changes))

    def test_config_file_installs_effective_startup_check(self):
        with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv", return_value=False):
            loaded = runpy.run_path(str(ROOT / "gunicorn.conf.py"))
        self.assertIs(loaded["on_starting"], validate_gunicorn_startup)


class DeploymentBehaviorTests(SimpleTestCase):
    def test_untrusted_forwarded_scheme_does_not_bypass_redirect(self):
        with configured_production():
            request = RequestFactory().get("/login/", HTTP_HOST="deployment.invalid", HTTP_X_FORWARDED_PROTO="https")
            self.assertFalse(request.is_secure())
            response = SecurityMiddleware(lambda request: HttpResponse("ok"))(request)
            self.assertEqual(response.status_code, 301)
            self.assertEqual(response["Location"], "https://deployment.invalid/login/")

    def test_explicit_trusted_proxy_prevents_redirect_loop(self):
        with configured_production(TRUST_PROXY_HEADERS="True"):
            request = RequestFactory().get("/login/", HTTP_HOST="deployment.invalid", HTTP_X_FORWARDED_PROTO="https")
            self.assertTrue(request.is_secure())
            response = SecurityMiddleware(lambda request: HttpResponse("ok"))(request)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Strict-Transport-Security"], "max-age=300")

    def test_http_scheme_from_trusted_proxy_still_redirects(self):
        with configured_production(TRUST_PROXY_HEADERS="True"):
            request = RequestFactory().get("/", HTTP_HOST="deployment.invalid", HTTP_X_FORWARDED_PROTO="http")
            response = SecurityMiddleware(lambda request: HttpResponse("ok"))(request)
            self.assertEqual(response.status_code, 301)

    def test_development_http_does_not_redirect(self):
        parsed = load_settings(environment(False, DEBUG="True"))
        with override_settings(SECURE_SSL_REDIRECT=parsed["SECURE_SSL_REDIRECT"], SECURE_PROXY_SSL_HEADER=parsed["SECURE_PROXY_SSL_HEADER"]):
            response = SecurityMiddleware(lambda request: HttpResponse("ok"))(RequestFactory().get("/"))
            self.assertEqual(response.status_code, 200)

    def test_deployment_validator_requires_production_mode(self):
        with override_settings(APP_ENV="development"):
            with self.assertRaisesRegex(CommandError, "APP_ENV=production"):
                call_command("validate_deployment", stdout=io.StringIO())

    def test_validator_performs_no_database_or_provider_connection(self):
        output = io.StringIO()
        with (configured_production(),
              patch("django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection", side_effect=AssertionError("No database probe allowed.")),
              patch("proposal_ai.services.OpenAI", side_effect=AssertionError("No provider probe allowed."))):
            call_command("validate_deployment", stdout=output)
        self.assertIn("Production configuration is valid", output.getvalue())
        self.assertIn("REGISTRATION_ENABLED=False; controlled-Beta registration is closed", output.getvalue())

    def test_validator_rejects_unsafe_effective_registration_policy(self):
        with configured_production(), override_settings(REGISTRATION_ENABLED=True):
            with self.assertRaisesRegex(ImproperlyConfigured, "REGISTRATION_ENABLED"):
                call_command("validate_deployment", stdout=io.StringIO())

    def test_validator_output_omits_credentials(self):
        secret = secrets.token_urlsafe(64)
        password = secrets.token_urlsafe(24)
        output = io.StringIO()
        with configured_production(SECRET_KEY=secret, DATABASE_URL=f"postgresql://test_user:{password}@127.0.0.1:1/disposable"):
            call_command("validate_deployment", stdout=output)
        self.assertTrue(secret not in output.getvalue() and password not in output.getvalue(), "Validator output must omit credentials.")

    def test_validator_checks_effective_settings_not_reparsed_environment(self):
        with configured_production(), override_settings(DEBUG=True):
            with self.assertRaisesRegex(ImproperlyConfigured, "DEBUG must be False"):
                call_command("validate_deployment", stdout=io.StringIO())

    def test_validator_rejects_unsafe_effective_runtime(self):
        parsed = production_overrides()
        parsed["DEPLOYMENT_RUNTIME"]["timeout"] = 30
        with configured_production(parsed):
            with self.assertRaisesRegex(ImproperlyConfigured, "reviewed sync-worker policy"):
                call_command("validate_deployment", stdout=io.StringIO())

    def test_deploy_checks_retain_only_staged_hsts_warnings(self):
        with configured_production():
            findings = run_checks(include_deployment_checks=True)
        self.assertEqual({finding.id for finding in findings}, {"security.W005", "security.W021"})


class ProductionStaticTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.output_directory = tempfile.TemporaryDirectory(prefix="proposalq-static-tests-")
        cls.addClassCleanup(cls.output_directory.cleanup)
        cls.static_root = Path(cls.output_directory.name)
        cls.configuration = override_settings(
            STATIC_ROOT=cls.static_root, STORAGES=MANIFEST_STORAGES, DEBUG=False,
            SECURE_SSL_REDIRECT=True, SECURE_PROXY_SSL_HEADER=None,
            ALLOWED_HOSTS=["deployment.invalid"],
        )
        cls.configuration.enable()
        cls.addClassCleanup(cls.configuration.disable)
        call_command("collectstatic", interactive=False, verbosity=0, stdout=io.StringIO())

    def test_manifest_is_produced_in_disposable_output(self):
        manifest = json.loads((self.static_root / "staticfiles.json").read_text())
        self.assertIn("css/style.css", manifest["paths"])
        self.assertIn("admin/css/base.css", manifest["paths"])
        self.assertFalse(self.static_root.is_relative_to(ROOT))

    def test_application_css_resolves_to_existing_hashed_asset(self):
        name = staticfiles_storage.stored_name("css/style.css")
        self.assertNotEqual(name, "css/style.css")
        self.assertTrue((self.static_root / name).is_file())
        self.assertEqual(staticfiles_storage.url("css/style.css"), "/static/" + name)

    def test_application_logo_and_admin_assets_resolve(self):
        for asset in ("images/proposallogo.png", "admin/css/base.css", "admin/js/core.js"):
            with self.subTest(asset=asset):
                name = staticfiles_storage.stored_name(asset)
                self.assertTrue((self.static_root / name).is_file())

    def test_public_and_login_templates_render_with_manifest(self):
        request = RequestFactory().get("/", secure=True, HTTP_HOST="deployment.invalid")
        request.user = AnonymousUser()
        for template in ("public_home.html", "login.html", "register.html"):
            with self.subTest(template=template):
                html = render_to_string(template, request=request)
                self.assertIn(staticfiles_storage.url("css/style.css"), html)
                self.assertIn(staticfiles_storage.url("images/proposallogo.png"), html)

    def test_admin_login_renders_with_manifest_assets(self):
        response = self.client.get(reverse("admin:login"), secure=True, HTTP_HOST="deployment.invalid")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, staticfiles_storage.url("admin/css/base.css"))

    def test_whitenoise_serves_collected_application_css(self):
        response = self.client.get(staticfiles_storage.url("css/style.css"), secure=True, HTTP_HOST="deployment.invalid")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"].split(";")[0], "text/css")
            self.assertIn("utf-8", response["Content-Type"])
        finally:
            response.close()
