"""Release safety gates, using mock connections; no hosted database is touched."""
import io
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from scripts.release import ReleaseError, run_release
from scripts.local_release import neon_url
from scripts.start_web import render_database, render_hostname, start_web


@override_settings(AI_ENABLED=False, REGISTRATION_ENABLED=False)
class PrivateReleaseTests(SimpleTestCase):
    def setUp(self):
        self.database = Mock(vendor="postgresql", pg_version=170000)
        self.database.connection.info.ssl_in_use = True
        self.commands = patch("django.core.management.call_command").start()
        self.addCleanup(patch.stopall)
        patch("django.db.connection", self.database).start()

    def command_names(self):
        return [call.args[0] for call in self.commands.call_args_list]

    def test_success_runs_one_migration_then_release_validation(self):
        run_release()
        self.assertEqual(self.command_names(), ["validate_deployment", "check", "migrate", "validate_release"])
        self.commands.assert_any_call("migrate", interactive=False)

    def test_configuration_failure_prevents_connection_and_migration(self):
        self.commands.side_effect = RuntimeError("Configuration rejected.")
        with self.assertRaises(RuntimeError):
            run_release()
        self.database.ensure_connection.assert_not_called()
        self.assertNotIn("migrate", self.command_names())

    def test_enabled_ai_prevents_release(self):
        with override_settings(AI_ENABLED=True), self.assertRaises(ReleaseError):
            run_release()
        self.database.ensure_connection.assert_not_called()
        self.assertNotIn("migrate", self.command_names())

    def test_open_registration_prevents_release(self):
        with override_settings(REGISTRATION_ENABLED=True), self.assertRaises(ReleaseError):
            run_release()
        self.database.ensure_connection.assert_not_called()

    def test_sqlite_is_rejected_before_connecting(self):
        self.database.vendor = "sqlite"
        with self.assertRaises(ReleaseError):
            run_release()
        self.database.ensure_connection.assert_not_called()
        self.assertNotIn("migrate", self.command_names())

    def test_unencrypted_connection_prevents_migration(self):
        self.database.connection.info.ssl_in_use = False
        with self.assertRaises(ReleaseError):
            run_release()
        self.assertNotIn("migrate", self.command_names())

    def test_unsupported_postgresql_prevents_migration(self):
        self.database.pg_version = 130000
        with self.assertRaises(ReleaseError):
            run_release()
        self.assertNotIn("migrate", self.command_names())

    def test_failed_migration_never_validates_release(self):
        def command(name, **kwargs):
            if name == "migrate":
                raise RuntimeError("Migration failed.")
        self.commands.side_effect = command
        with self.assertRaises(RuntimeError):
            run_release()
        self.assertNotIn("validate_release", self.command_names())


@override_settings(AI_ENABLED=False, REGISTRATION_ENABLED=False)
class StartupGateTests(SimpleTestCase):
    def setUp(self):
        self.commands = patch("django.core.management.call_command").start()
        self.database = patch("scripts.release.verify_database").start()
        self.execute = patch("scripts.start_web.os.execvp").start()
        self.addCleanup(patch.stopall)

    def test_startup_validates_without_running_migrations_or_collectstatic(self):
        start_web()
        self.assertEqual([call.args[0] for call in self.commands.call_args_list],
                         ["validate_deployment", "validate_release"])
        self.database.assert_called_once_with()
        self.execute.assert_called_once_with(
            "gunicorn", ["gunicorn", "--config", "gunicorn.conf.py", "mysite.wsgi:application"])

    def test_failed_configuration_prevents_database_and_start(self):
        self.commands.side_effect = RuntimeError("Invalid configuration")
        with self.assertRaises(RuntimeError):
            start_web()
        self.database.assert_not_called()
        self.execute.assert_not_called()

    def test_failed_database_prevents_start(self):
        self.database.side_effect = ReleaseError("Database unavailable")
        with self.assertRaises(ReleaseError):
            start_web()
        self.execute.assert_not_called()

    def test_failed_schema_or_static_readiness_prevents_start(self):
        def command(name):
            if name == "validate_release":
                raise RuntimeError("Release incomplete")
        self.commands.side_effect = command
        with self.assertRaises(RuntimeError):
            start_web()
        self.execute.assert_not_called()

    def test_enabled_ai_prevents_start(self):
        with override_settings(AI_ENABLED=True), self.assertRaises(ValueError):
            start_web()
        self.database.assert_not_called()
        self.execute.assert_not_called()

    def test_open_registration_prevents_start(self):
        with override_settings(REGISTRATION_ENABLED=True), self.assertRaises(ValueError):
            start_web()
        self.database.assert_not_called()
        self.execute.assert_not_called()


class RenderHostTests(SimpleTestCase):
    def test_platform_hostname_is_used_when_hosts_are_missing(self):
        environment = {"RENDER": "true", "RENDER_EXTERNAL_HOSTNAME": "proposalq-beta.onrender.com"}
        render_hostname(environment)
        self.assertEqual(environment["ALLOWED_HOSTS"], "proposalq-beta.onrender.com")

    def test_explicit_hosts_are_preserved_including_invalid_blank(self):
        for hosts in ("approved.example", ""):
            with self.subTest(hosts=hosts):
                environment = {"ALLOWED_HOSTS": hosts}
                render_hostname(environment)
                self.assertEqual(environment["ALLOWED_HOSTS"], hosts)

    def test_missing_or_untrusted_platform_hostname_fails_closed(self):
        for environment in ({}, {"RENDER_EXTERNAL_HOSTNAME": "beta.onrender.com"},
                            {"RENDER": "true", "RENDER_EXTERNAL_HOSTNAME": ".onrender.com"},
                            {"RENDER": "true", "RENDER_EXTERNAL_HOSTNAME": "https://beta.onrender.com"},
                            {"RENDER": "true", "RENDER_EXTERNAL_HOSTNAME": "beta.example"}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                render_hostname(environment)


class NeonAdministrationTests(SimpleTestCase):
    # Synthetic credentials only. No test connects to a database or provider.
    direct_url = "postgresql://operator:synthetic-test-password@ep-example.eu-central-1.aws.neon.tech/neondb"

    def test_direct_target_is_preserved_and_tls_is_authenticated(self):
        import certifi
        result = urlsplit(neon_url(self.direct_url + "?sslmode=require&channel_binding=require", "neondb"))
        original = urlsplit(self.direct_url)
        self.assertEqual((result.netloc, result.path), (original.netloc, original.path))
        self.assertEqual(parse_qs(result.query), {
            "sslmode": ["verify-full"], "sslrootcert": [certifi.where()],
            "channel_binding": ["require"], "connect_timeout": ["10"],
        })

    def test_render_and_local_release_apply_identical_tls_policy(self):
        environment = {"DATABASE_URL": self.direct_url}
        render_database(environment)
        self.assertEqual(environment["DATABASE_URL"], neon_url(self.direct_url, "neondb"))

    def test_mismatched_database_is_rejected(self):
        with self.assertRaises(ValueError):
            neon_url(self.direct_url, "different_database")

    def test_pooler_sqlite_non_neon_and_missing_credentials_are_rejected(self):
        for value in (self.direct_url.replace("ep-example", "ep-example-pooler"),
                      "sqlite:///db.sqlite3", self.direct_url.replace("neon.tech", "example.com"),
                      self.direct_url.replace(":synthetic-test-password", "")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                neon_url(value, "neondb")

    def test_target_identity_or_transaction_overrides_are_rejected(self):
        for option in ("host=another.neon.tech", "dbname=other", "user=other",
                       "options=-c%20default_transaction_isolation%3Dserializable"):
            with self.subTest(option=option), self.assertRaises(ValueError):
                neon_url(self.direct_url + "?" + option, "neondb")

    def test_administration_refuses_noninteractive_secret_input(self):
        from scripts.local_release import main
        with (patch("sys.argv", ["local_release.py"]), patch("sys.stdin.isatty", return_value=False),
              patch("getpass.getpass") as prompt, patch("subprocess.run") as build,
              patch("sys.stderr", io.StringIO())):
            self.assertEqual(main(), 1)
        prompt.assert_not_called()
        build.assert_not_called()
