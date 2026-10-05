"""Health/release checks on isolated databases and disposable static output."""
from contextlib import contextmanager
from datetime import timedelta
import io
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import uuid
from unittest.mock import patch

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.db import connection, OperationalError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mysite import operations
from mysite.operational_logging import SafeOperationalFilter, SafeOperationalFormatter
from mysite.test_production_configuration import MANIFEST_STORAGES, configured_production, load_settings, environment
from proposal_ai.models import AIQuotaPeriod, AIRequest


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def no_administrative_actions():
    with (patch.object(MigrationExecutor, "migrate", side_effect=AssertionError("No migrate allowed.")),
          patch.object(MigrationRecorder, "ensure_schema", side_effect=AssertionError("No recorder creation allowed.")),
          patch.object(MigrationRecorder, "record_applied", side_effect=AssertionError("No recorder writes allowed.")),
          patch("proposal_ai.ai_control.recover_global_stale", side_effect=AssertionError("No recovery allowed.")),
          patch("proposal_ai.ai_control.recover_stale", side_effect=AssertionError("No recovery allowed.")),
          patch("proposal_ai.services.OpenAI", side_effect=AssertionError("No provider allowed.")),
          patch("proposal_ai.services.check_configuration", side_effect=AssertionError("No provider preflight allowed."))):
        yield


def counts():
    return {model._meta.db_table: model._default_manager.count()
            for model in apps.get_models(include_auto_created=True)
            if model._meta.managed and not model._meta.proxy}


def assert_read_only(test, function):
    statements = []
    def observe(execute, sql, params, many, context):
        statements.append(sql.lstrip().split(None, 1)[0].upper())
        return execute(sql, params, many, context)
    with connection.execute_wrapper(observe):
        result = function()
    test.assertTrue(statements)
    test.assertTrue(set(statements).issubset({"SELECT", "PRAGMA", "SHOW"}), "Inspection must use only read statements.")
    return result


class LivenessTests(SimpleTestCase):
    def test_anonymous_get_returns_minimal_200(self):
        response = self.client.get(reverse("health_live"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok\n")

    def test_head_has_no_body(self):
        response = self.client.head(reverse("health_live"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")

    def test_responses_are_not_cacheable(self):
        self.assertIn("no-store", self.client.get(reverse("health_live"))["Cache-Control"])

    def test_unsupported_methods_are_rejected(self):
        for method in ("post", "put", "patch", "delete", "options"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(reverse("health_live"))
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response["Allow"], "GET, HEAD")
                self.assertIn("no-store", response["Cache-Control"])

    def test_no_database_or_migration_inspection(self):
        with (patch("django.db.backends.base.base.BaseDatabaseWrapper.cursor", side_effect=AssertionError("No database allowed.")),
              patch("mysite.operations.MigrationExecutor", side_effect=AssertionError("No migration inspection allowed.")),
              patch("mysite.health.check_readiness", side_effect=AssertionError("No readiness check allowed."))):
            self.assertEqual(self.client.get(reverse("health_live")).status_code, 200)

    def test_no_provider_recovery_or_recorder_mutation(self):
        with no_administrative_actions():
            self.assertEqual(self.client.get(reverse("health_live")).status_code, 200)

    def test_no_templates_or_static_inspection(self):
        with (patch("django.template.loader.render_to_string", side_effect=AssertionError("No rendering allowed.")),
              patch("mysite.operations.check_static", side_effect=AssertionError("No static inspection allowed."))):
            self.assertEqual(self.client.get(reverse("health_live")).status_code, 200)

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_https_redirect_is_preserved(self):
        self.assertEqual(self.client.get(reverse("health_live")).status_code, 301)
        self.assertEqual(self.client.get(reverse("health_live"), secure=True).status_code, 200)

    @override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
    def test_host_validation_is_preserved(self):
        self.assertEqual(self.client.get(reverse("health_live"), HTTP_HOST="untrusted.invalid").status_code, 400)

    def test_csrf_middleware_is_not_disabled(self):
        self.assertEqual(Client(enforce_csrf_checks=True).post(reverse("health_live")).status_code, 403)

    def test_session_cookie_does_not_trigger_user_lookup(self):
        self.client.cookies[settings.SESSION_COOKIE_NAME] = "synthetic-session-cookie"
        with patch("django.db.backends.base.base.BaseDatabaseWrapper.cursor", side_effect=AssertionError("No database allowed.")):
            self.assertEqual(self.client.get(reverse("health_live")).status_code, 200)


class ReadinessTests(TestCase):
    def get(self):
        return self.client.get(reverse("health_ready"))

    def test_complete_schema_returns_200(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok\n")

    def test_head_has_no_body(self):
        response = self.client.head(reverse("health_ready"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")

    def test_success_is_not_cacheable(self):
        self.assertIn("no-store", self.get()["Cache-Control"])

    def test_failure_is_not_cacheable(self):
        with patch.object(connection, "cursor", side_effect=OperationalError("private-database-marker")):
            response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertIn("no-store", response["Cache-Control"])

    def test_database_failure_is_private(self):
        with patch.object(connection, "cursor", side_effect=OperationalError("private-database-marker")):
            response = self.get()
        self.assertEqual(response.content, b"not ready\n")
        self.assertNotContains(response, "private-database-marker", status_code=503)

    def test_failure_head_is_empty_and_not_cacheable(self):
        with patch.object(connection, "cursor", side_effect=OperationalError("private-database-marker")):
            response = self.client.head(reverse("health_ready"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, b"")
        self.assertIn("no-store", response["Cache-Control"])

    def test_unexpected_schema_inspection_error_is_private(self):
        with patch.object(connection.introspection, "get_table_description", side_effect=RuntimeError("private-schema-marker")):
            response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, b"not ready\n")

    def test_pending_application_migration(self):
        MigrationRecorder(connection).migration_qs.filter(app="proposal_ai", name="0016_ai_global_exposure").delete()
        self.assertEqual(self.get().status_code, 503)

    def test_pending_framework_migration(self):
        MigrationRecorder(connection).migration_qs.filter(app="sessions", name="0001_initial").delete()
        self.assertEqual(self.get().status_code, 503)

    def test_inconsistent_history(self):
        MigrationRecorder(connection).migration_qs.filter(app="proposal_ai", name="0014_ai_request").delete()
        with self.assertLogs("proposalq.operations", level="WARNING") as logs:
            response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(logs.records[0].category, "migration_history_invalid")
        self.assertEqual(response.content, b"not ready\n")

    def test_migration_conflict_is_not_ready(self):
        with patch("django.db.migrations.loader.MigrationLoader.detect_conflicts", return_value={"proposal_ai": ["synthetic"]}):
            self.assertEqual(self.get().status_code, 503)

    @override_settings(APP_ENV="unexpected")
    def test_invalid_effective_configuration(self):
        self.assertEqual(self.get().status_code, 503)

    @override_settings(AI_ENABLED=False)
    def test_ai_disabled_does_not_disable_ordinary_readiness(self):
        self.assertEqual(self.get().status_code, 200)

    @override_settings(OPENAI_API_KEY="")
    def test_missing_provider_key_does_not_disable_readiness(self):
        with no_administrative_actions():
            self.assertEqual(self.get().status_code, 200)

    def test_read_only_sql_and_no_administrative_calls(self):
        before = counts()
        migrations_before = MigrationRecorder(connection).migration_qs.count()
        with no_administrative_actions():
            response = assert_read_only(self, self.get)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(counts(), before)
        self.assertEqual(MigrationRecorder(connection).migration_qs.count(), migrations_before)

    def test_no_static_or_template_work_in_probe(self):
        with (patch("mysite.operations.check_static", side_effect=AssertionError("Static is release-only.")),
              patch("django.template.loader.render_to_string", side_effect=AssertionError("No templates allowed."))):
            self.assertEqual(self.get().status_code, 200)

    def test_does_not_recover_expired_request(self):
        user = get_user_model().objects.create_user(username="readiness-owner")
        row = AIRequest.objects.create(
            user=user, operation=AIRequest.Operation.PROFILE_SUMMARY, nonce=uuid.uuid4(),
            intent=AIRequest.Intent.GENERATE, submitted_fingerprint="a" * 64,
            effective_fingerprint="b" * 64, lifecycle=AIRequest.Lifecycle.RESERVED,
            quota_state=AIRequest.Quota.RESERVED, quota_units=1,
            admitted_at=timezone.now() - timedelta(hours=1), lease_expires_at=timezone.now() - timedelta(minutes=1),
        )
        with no_administrative_actions():
            self.assertEqual(self.get().status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.lifecycle, AIRequest.Lifecycle.RESERVED)
        self.assertEqual(row.quota_state, AIRequest.Quota.RESERVED)

    @override_settings(AI_GLOBAL_DAILY_CREDITS=0, AI_GLOBAL_WEEKLY_CREDITS=0)
    def test_exhausted_global_allowance_does_not_disable_readiness(self):
        AIQuotaPeriod.objects.create(kind=AIQuotaPeriod.Kind.DAILY, period_start=timezone.now().date(), credit_limit=0)
        self.assertEqual(self.get().status_code, 200)

    def test_exhausted_user_allowance_does_not_disable_readiness(self):
        user = get_user_model().objects.create_user(username="readiness-quota-owner")
        moment = timezone.now()
        AIRequest.objects.bulk_create([
            AIRequest(user=user, operation=AIRequest.Operation.PROFILE_SUMMARY, nonce=uuid.uuid4(),
                      intent=AIRequest.Intent.GENERATE, submitted_fingerprint="a" * 64,
                      effective_fingerprint="b" * 64, lifecycle=AIRequest.Lifecycle.SUCCEEDED,
                      quota_state=AIRequest.Quota.CONSUMED, quota_units=1, admitted_at=moment,
                      dispatch_started_at=moment, lease_expires_at=moment + timedelta(minutes=10))
            for _ in range(25)
        ])
        self.assertEqual(self.get().status_code, 200)

    def test_failure_category_is_logged_without_raw_error(self):
        with (patch.object(connection, "cursor", side_effect=OperationalError("private-database-marker")),
              self.assertLogs("proposalq.operations", level="WARNING") as logs):
            self.get()
        self.assertEqual(logs.records[0].category, "database_unavailable")
        self.assertNotIn("private-database-marker", " ".join(logs.output))

    def test_unsupported_method_does_not_inspect_database(self):
        with patch("mysite.health.check_readiness", side_effect=AssertionError("No POST inspection.")):
            self.assertEqual(self.client.post(reverse("health_ready")).status_code, 405)


class PhysicalSchemaTests(TransactionTestCase):
    def command_fails(self):
        with self.assertRaises(CommandError):
            call_command("validate_release", stdout=io.StringIO())

    def test_faked_schema_missing_ai_table(self):
        with connection.schema_editor() as editor:
            editor.delete_model(AIRequest)
        try:
            self.assertEqual(self.client.get(reverse("health_ready")).status_code, 503)
            self.command_fails()
        finally:
            with connection.schema_editor() as editor:
                editor.create_model(AIRequest)

    def test_faked_telemetry_column_missing(self):
        quote = connection.ops.quote_name
        table = quote(AIRequest._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {table} RENAME COLUMN provider_latency_ms TO absent_provider_latency_ms")
        try:
            self.assertEqual(self.client.get(reverse("health_ready")).status_code, 503)
            self.command_fails()
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"ALTER TABLE {table} RENAME COLUMN absent_provider_latency_ms TO provider_latency_ms")

    def test_faked_global_constraint_missing(self):
        constraint = next(item for item in AIQuotaPeriod._meta.constraints if item.name == "ai_quota_period_unique")
        # SQLite rebuilds from the supplied model state for table constraints.
        remaining = [item for item in AIQuotaPeriod._meta.constraints if item != constraint]
        with patch.object(AIQuotaPeriod._meta, "constraints", remaining):
            with connection.schema_editor() as editor:
                editor.remove_constraint(AIQuotaPeriod, constraint)
        try:
            self.assertEqual(self.client.get(reverse("health_ready")).status_code, 503)
        finally:
            with connection.schema_editor() as editor:
                editor.add_constraint(AIQuotaPeriod, constraint)

    def test_active_user_partial_uniqueness_missing(self):
        constraint = next(item for item in AIRequest._meta.constraints if item.name == "ai_request_one_active_user")
        with connection.schema_editor() as editor:
            editor.remove_constraint(AIRequest, constraint)
        try:
            self.assertEqual(self.client.get(reverse("health_ready")).status_code, 503)
        finally:
            with connection.schema_editor() as editor:
                editor.add_constraint(AIRequest, constraint)

    def test_required_ai_index_missing(self):
        index = next(item for item in AIRequest._meta.indexes if item.name == "ai_request_user_burst")
        with connection.schema_editor() as editor:
            editor.remove_index(AIRequest, index)
        try:
            self.assertEqual(self.client.get(reverse("health_ready")).status_code, 503)
        finally:
            with connection.schema_editor() as editor:
                editor.add_index(AIRequest, index)

    def test_absent_migration_recorder_is_not_created(self):
        recorder = MigrationRecorder(connection)
        saved = list(recorder.migration_qs.values("app", "name", "applied"))
        with connection.schema_editor() as editor:
            editor.delete_model(recorder.Migration)
        try:
            with no_administrative_actions():
                self.assertEqual(self.client.get(reverse("health_ready")).status_code, 503)
            self.assertNotIn("django_migrations", connection.introspection.table_names())
        finally:
            with connection.schema_editor() as editor:
                editor.create_model(recorder.Migration)
            recorder.migration_qs.bulk_create([recorder.Migration(**values) for values in saved])


class ReleaseValidationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.output = tempfile.TemporaryDirectory(prefix="proposalq-release-tests-")
        cls.addClassCleanup(cls.output.cleanup)
        cls.static_root = Path(cls.output.name)
        cls.configuration = override_settings(STATIC_ROOT=cls.static_root, STORAGES=MANIFEST_STORAGES)
        cls.configuration.enable()
        cls.addClassCleanup(cls.configuration.disable)
        call_command("collectstatic", interactive=False, verbosity=0, stdout=io.StringIO())

    def run_command(self):
        output = io.StringIO()
        call_command("validate_release", stdout=output)
        return output.getvalue()

    def test_valid_database_and_collected_static_succeed(self):
        self.assertIn("No state was changed", self.run_command())

    def test_no_database_mutation_or_administrative_actions(self):
        before = counts()
        migrations_before = MigrationRecorder(connection).migration_qs.count()
        with no_administrative_actions():
            assert_read_only(self, self.run_command)
        self.assertEqual(counts(), before)
        self.assertEqual(MigrationRecorder(connection).migration_qs.count(), migrations_before)

    def test_static_files_are_not_modified(self):
        before = {path.relative_to(self.static_root): (path.stat().st_size, path.stat().st_mtime_ns)
                  for path in self.static_root.rglob("*") if path.is_file()}
        self.run_command()
        after = {path.relative_to(self.static_root): (path.stat().st_size, path.stat().st_mtime_ns)
                 for path in self.static_root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_database_failure_is_controlled_and_private(self):
        with patch.object(connection, "cursor", side_effect=OperationalError("private-database-marker")):
            with self.assertRaises(CommandError) as error:
                self.run_command()
        self.assertIn("database", str(error.exception))
        self.assertNotIn("private-database-marker", str(error.exception))

    def test_pending_migration_rejects_release(self):
        MigrationRecorder(connection).migration_qs.filter(app="proposal_ai", name="0016_ai_global_exposure").delete()
        with self.assertRaisesRegex(CommandError, "unapplied"):
            self.run_command()

    def test_missing_manifest_rejects_release(self):
        with tempfile.TemporaryDirectory(prefix="proposalq-release-empty-") as empty:
            with override_settings(STATIC_ROOT=empty):
                with self.assertRaisesRegex(CommandError, "manifest"):
                    self.run_command()

    def test_malformed_manifest_rejects_release(self):
        manifest = self.static_root / "staticfiles.json"
        original = manifest.read_bytes()
        try:
            manifest.write_text("not-json")
            with self.assertRaises(CommandError):
                self.run_command()
        finally:
            manifest.write_bytes(original)

    def test_missing_application_asset_rejects_release(self):
        self.missing_asset("css/style.css")

    def test_invalid_manifest_structure_is_controlled_and_private(self):
        manifest = self.static_root / "staticfiles.json"
        original = manifest.read_bytes()
        try:
            manifest.write_text('["private-manifest-marker"]')
            with self.assertRaises(CommandError) as error:
                self.run_command()
            self.assertNotIn("private-manifest-marker", str(error.exception))
        finally:
            manifest.write_bytes(original)

    def test_missing_logo_rejects_release(self):
        self.missing_asset("images/proposallogo.png")

    def test_missing_admin_asset_rejects_release(self):
        self.missing_asset("admin/js/core.js")

    def missing_asset(self, name):
        manifest = json.loads((self.static_root / "staticfiles.json").read_text())
        asset = self.static_root / manifest["paths"][name]
        original = asset.read_bytes()
        asset.unlink()
        try:
            with self.assertRaises(CommandError):
                self.run_command()
        finally:
            asset.write_bytes(original)

    def test_ai_disabled_does_not_prevent_release_validation(self):
        with override_settings(AI_ENABLED=False):
            self.assertIn("ready", self.run_command())

    def test_invalid_configuration_fails_before_database_access(self):
        with (override_settings(APP_ENV="unexpected"),
              patch.object(connection, "cursor", side_effect=AssertionError("Configuration should fail first."))):
            with self.assertRaisesRegex(CommandError, "configuration"):
                self.run_command()

    def test_missing_manifest_entry_rejects_release(self):
        manifest = self.static_root / "staticfiles.json"
        original = manifest.read_bytes()
        try:
            data = json.loads(original)
            data["paths"].pop("css/style.css")
            manifest.write_text(json.dumps(data))
            with self.assertRaises(CommandError):
                self.run_command()
        finally:
            manifest.write_bytes(original)

    def test_unsupported_storage_is_not_contacted(self):
        with (override_settings(STORAGES={"staticfiles": {"BACKEND": "untrusted.remote.Storage"}}),
              patch("mysite.operations.storages.create_storage", side_effect=AssertionError("No remote storage allowed."))):
            with self.assertRaises(CommandError):
                self.run_command()

    def test_configuration_validator_remains_database_independent(self):
        with (configured_production(),
              patch.object(connection, "cursor", side_effect=AssertionError("No database allowed.")),
              no_administrative_actions()):
            call_command("validate_deployment", stdout=io.StringIO())

    def test_failure_is_logged_with_safe_category(self):
        with tempfile.TemporaryDirectory(prefix="proposalq-release-empty-") as empty:
            with override_settings(STATIC_ROOT=empty), self.assertLogs("proposalq.operations", level="ERROR") as logs:
                with self.assertRaises(CommandError):
                    self.run_command()
        self.assertEqual(logs.records[0].category, "static_unavailable")


class OperationalLoggingTests(SimpleTestCase):
    def record(self, name="proposalq.operations", message="readiness_failed", category="database_unavailable"):
        record = logging.LogRecord(name, logging.ERROR, "private-path-marker", 1, message, (), None)
        record.category = category
        return record

    def test_allowlisted_operational_event(self):
        record = self.record()
        self.assertTrue(SafeOperationalFilter().filter(record))
        payload = json.loads(SafeOperationalFormatter().format(record))
        self.assertEqual(payload["event"], "readiness_failed")
        self.assertEqual(payload["category"], "database_unavailable")
        self.assertEqual(set(payload), {"event", "category", "timestamp", "level"})

    def test_arbitrary_message_is_rejected(self):
        self.assertFalse(SafeOperationalFilter().filter(self.record(message="private-payload-marker")))

    def test_arbitrary_category_is_rejected(self):
        self.assertFalse(SafeOperationalFilter().filter(self.record(category="private-payload-marker")))

    def test_arbitrary_logger_is_rejected(self):
        self.assertFalse(SafeOperationalFilter().filter(self.record(name="untrusted.logger")))

    def test_low_severity_events_are_not_emitted(self):
        record = self.record()
        record.levelno = logging.INFO
        self.assertFalse(SafeOperationalFilter().filter(record))

    def test_payload_arguments_exceptions_and_extras_are_not_formatted(self):
        class PrivateValue:
            def __str__(self):
                raise AssertionError("Private content must not be formatted.")
        record = self.record()
        record.args = (PrivateValue(),)
        record.exc_info = (ValueError, ValueError("private-error-marker"), None)
        record.stack_info = "private-stack-marker"
        record.request = PrivateValue()
        record.password = "private-password-marker"
        record.nonce = "private-nonce-marker"
        record.fingerprint = "private-fingerprint-marker"
        output = SafeOperationalFormatter().format(record)
        self.assertNotIn("private", output)

    def test_django_exception_output_is_reduced_to_safe_category(self):
        record = self.record(name="django.request", message="private-error-marker")
        record.exc_info = (ValueError, ValueError("private-error-marker"), None)
        record.status_code = 500
        payload = json.loads(SafeOperationalFormatter().format(record))
        self.assertEqual(payload["category"], "application_error")
        self.assertEqual(payload["status"], 500)
        self.assertNotIn("private", json.dumps(payload))

    def test_security_details_are_not_emitted(self):
        record = self.record(name="django.security.csrf", message="private-csrf-marker")
        payload = json.loads(SafeOperationalFormatter().format(record))
        self.assertEqual(payload["category"], "security_rejection")
        self.assertNotIn("private", json.dumps(payload))

    def test_production_enables_explicit_logging(self):
        parsed = load_settings(environment())
        self.assertEqual(parsed["LOGGING"]["handlers"]["safe_operations"]["stream"], "ext://sys.stderr")
        self.assertFalse(parsed["LOGGING"]["disable_existing_loggers"])

    def test_development_keeps_existing_logging_configuration(self):
        self.assertNotIn("LOGGING", load_settings(environment(False)))

    def test_actual_production_output_and_sdk_suppression(self):
        values = dict(os.environ)
        values.update(APP_ENV="production", DEBUG="False", SECRET_KEY=secrets.token_urlsafe(64),
                      DATABASE_URL="postgresql://test_user@127.0.0.1:1/disposable", ALLOWED_HOSTS="deployment.invalid",
                      AI_ENABLED="False", REGISTRATION_ENABLED="False", PYTHON_DOTENV_DISABLED="1",
                      DJANGO_SETTINGS_MODULE="mysite.settings", PYTHONDONTWRITEBYTECODE="1",
                      TRUST_PROXY_HEADERS="False", SECURE_SSL_REDIRECT="True", SECURE_HSTS_SECONDS="300",
                      SECURE_HSTS_INCLUDE_SUBDOMAINS="False", SECURE_HSTS_PRELOAD="False")
        for name in ("AI_GLOBAL_DAILY_CREDITS", "AI_GLOBAL_WEEKLY_CREDITS", "GUNICORN_CMD_ARGS"):
            values.pop(name, None)
        script = """
import django, logging
from mysite.test_runner import block_external_network
with block_external_network():
    django.setup()
    from proposal_ai import services
    logging.getLogger('proposalq.operations').error('readiness_failed', extra={'category':'schema_incomplete','password':'private-payload-marker'})
    logging.getLogger('django.request').error('private-payload-marker', exc_info=ValueError('private-error-marker'))
    for name in ('openai._base_client','openai._response'):
        logging.getLogger(name).error('private-sdk-payload-marker')
"""
        result = subprocess.run([sys.executable, "-B", "-c", script], env=values, cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, "Isolated production logging process failed.")
        self.assertNotIn("private", result.stdout + result.stderr)
        events = [json.loads(line) for line in result.stderr.splitlines()]
        self.assertEqual([event["category"] for event in events], ["schema_incomplete", "application_error"])
