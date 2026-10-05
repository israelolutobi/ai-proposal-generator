"""Release safety gates, using mock connections; no hosted database is touched."""
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from scripts.release import ReleaseError, run_release


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
