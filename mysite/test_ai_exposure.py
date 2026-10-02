"""Global controls with synthetic limits; these values are never Beta policy."""
from datetime import datetime, timedelta, timezone
import io
import json
from unittest.mock import Mock, patch
import uuid

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection, IntegrityError, OperationalError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from mysite import test_ai_control as old_control, test_production_configuration as production
from mysite.configuration import AI_GLOBAL_LIMIT_MAX, ai_configuration, validate_ai_configuration
from proposal_ai import ai_control as control, ai_global, services
from proposal_ai.models import AIQuotaPeriod, AIRequest, JobPost

O, I, L, Q, F = (AIRequest.Operation, AIRequest.Intent, AIRequest.Lifecycle,
                 AIRequest.Quota, AIRequest.Failure)
MOMENT = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
POLICY = dict(APP_ENV="development", AI_ENABLED=True,
              AI_GLOBAL_DAILY_CREDITS=12, AI_GLOBAL_WEEKLY_CREDITS=30)


class AIConfigurationTests(SimpleTestCase):
    def test_development_enabled_by_default_without_global_limits(self):
        self.assertEqual(ai_configuration({}), dict(AI_ENABLED=True,
            AI_GLOBAL_DAILY_CREDITS=None, AI_GLOBAL_WEEKLY_CREDITS=None))

    def test_production_disabled_by_default(self):
        self.assertFalse(ai_configuration({}, production=True)["AI_ENABLED"])

    def test_explicit_development_true_and_false(self):
        for value, expected in (("true", True), ("FALSE", False), (" TrUe ", True)):
            self.assertIs(ai_configuration({"AI_ENABLED": value})["AI_ENABLED"], expected)

    def test_explicit_production_enabled_requires_both_limits(self):
        with self.assertRaises(ImproperlyConfigured):
            ai_configuration({"AI_ENABLED": "true"}, production=True)
        parsed = ai_configuration(dict(AI_ENABLED="true", AI_GLOBAL_DAILY_CREDITS="12",
                                        AI_GLOBAL_WEEKLY_CREDITS="30"), production=True)
        self.assertTrue(parsed["AI_ENABLED"])

    def test_explicit_production_false_may_omit_limits(self):
        self.assertFalse(ai_configuration({"AI_ENABLED": "false"}, production=True)["AI_ENABLED"])

    def test_blank_and_invalid_flags_fail_startup_in_both_modes(self):
        for prod in (True, False):
            for value in ("", " ", "1", "yes", "invalid"):
                with self.subTest(prod=prod, value=value), self.assertRaises(ImproperlyConfigured):
                    ai_configuration({"AI_ENABLED": value}, production=prod)

    def test_missing_limit_pair_rejected_even_when_disabled(self):
        for name in ("AI_GLOBAL_DAILY_CREDITS", "AI_GLOBAL_WEEKLY_CREDITS"):
            with self.assertRaises(ImproperlyConfigured):
                ai_configuration({"AI_ENABLED": "false", name: "1"})

    def test_blank_negative_malformed_and_overflow_limits_rejected(self):
        for value in ("", "-1", "+1", "1.0", "1e2", "１２", "unlimited", str(AI_GLOBAL_LIMIT_MAX + 1)):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                ai_configuration(dict(AI_GLOBAL_DAILY_CREDITS=value, AI_GLOBAL_WEEKLY_CREDITS="5"))

    def test_zero_and_technical_maximum_are_valid_without_ratio_rule(self):
        parsed = ai_configuration(dict(AI_GLOBAL_DAILY_CREDITS=str(AI_GLOBAL_LIMIT_MAX),
                                        AI_GLOBAL_WEEKLY_CREDITS="0"))
        self.assertEqual(parsed["AI_GLOBAL_DAILY_CREDITS"], AI_GLOBAL_LIMIT_MAX)
        self.assertEqual(parsed["AI_GLOBAL_WEEKLY_CREDITS"], 0)

    def test_effective_settings_cannot_use_boolean_or_partial_limits(self):
        for changes in (dict(AI_ENABLED="True"), dict(AI_GLOBAL_DAILY_CREDITS=True),
                        dict(AI_GLOBAL_WEEKLY_CREDITS=None)):
            with self.assertRaises(ImproperlyConfigured):
                validate_ai_configuration({**POLICY, **changes})

    def test_settings_and_deployment_validator_agree_without_database(self):
        parsed = production.load_settings(production.environment(AI_ENABLED="true",
            AI_GLOBAL_DAILY_CREDITS="12", AI_GLOBAL_WEEKLY_CREDITS="30"))
        self.assertTrue(parsed["AI_ENABLED"])
        with production.configured_production(AI_ENABLED="true", AI_GLOBAL_DAILY_CREDITS="12",
                                             AI_GLOBAL_WEEKLY_CREDITS="30"):
            output = io.StringIO()
            with patch("proposal_ai.services.OpenAI") as sdk:
                call_command("validate_deployment", stdout=output)
            sdk.assert_not_called()
            self.assertIn("AI_ENABLED=True", output.getvalue())

    def test_configuration_errors_do_not_echo_supplied_values(self):
        marker = "SYNTHETIC_PRIVATE_CONFIGURATION"
        with self.assertRaises(ImproperlyConfigured) as error:
            ai_configuration({"AI_ENABLED": marker})
        self.assertNotIn(marker, str(error.exception))


class GlobalFixture:
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="global-owner")
        self.other = get_user_model().objects.create_user(username="global-other")
        clock = patch("proposal_ai.ai_control.now", return_value=MOMENT)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        config = patch("proposal_ai.services.check_configuration", return_value=None)
        self.config = config.start()
        self.addCleanup(config.stop)

    def admit(self, operation=O.PROFILE_SUMMARY, user=None, token=None):
        user = user or self.user
        return control.admit(user, operation, token or control.issue_nonce(user, operation),
                             {"title": "Synthetic input"}, "Synthetic effective input")

    def provider(self):
        return Mock(return_value=services.AIServiceResult("Synthetic output", services.AITelemetry(
            provider="openai", input_tokens=11, completion_tokens=13, total_tokens=24)))

    def complete(self, row):
        provider = self.provider()
        control.call_provider(row, provider)
        control.succeed(row)
        provider.assert_called_once()

    def totals(self):
        return list(AIQuotaPeriod.objects.order_by("kind", "period_start").values_list(
            "reserved_credits", "consumed_credits"))

    def reject(self, expected=503, **kwargs):
        with self.assertRaises(control.ControlError) as error:
            self.admit(**kwargs)
        self.assertEqual(error.exception.status, expected)
        return error.exception


@override_settings(**POLICY)
class GlobalAccountingTests(GlobalFixture, TestCase):
    def test_utc_daily_and_monday_week_keys(self):
        moment = datetime(2026, 10, 5, 1, tzinfo=timezone(timedelta(hours=2)))
        daily, weekly = ai_global.period_keys(moment)
        self.assertEqual(str(daily[1]), "2026-10-04")
        self.assertEqual(str(weekly[1]), "2026-09-28")

    def test_all_credit_weights_reserved_then_consumed(self):
        for op, weight in control.CREDITS.items():
            row = self.admit(op).request
            expected = sum(control.CREDITS[o] for o in list(control.CREDITS)[:list(control.CREDITS).index(op)])
            self.assertEqual(self.totals(), [(weight, expected)] * 2)
            self.complete(row)
            self.assertEqual(self.totals(), [(0, expected + weight)] * 2)

    def test_release_restores_both_periods_exactly_once(self):
        row = self.admit().request
        control.fail(row, F.LOCAL_CONFIGURATION, release=True)
        control.fail(row, F.LOCAL_CONFIGURATION, release=True)
        self.assertEqual(self.totals(), [(0, 0)] * 2)
        self.assertEqual(AIRequest.objects.get().quota_state, Q.RELEASED)

    def test_ambiguous_failure_consumes_exactly_once(self):
        row = self.admit().request
        control.mark_dispatch(row)
        control.fail(row, F.TIMEOUT)
        control.fail(row, F.TIMEOUT)
        self.assertEqual(self.totals(), [(0, 1)] * 2)

    def test_provider_rejection_releases_but_keeps_burst_attempt(self):
        for failure in (services.AIAuthenticationError, services.AICapacityError, services.AIRequestError):
            row = self.admit().request
            provider = Mock(side_effect=failure())
            with self.assertRaises(failure):
                control.call_provider(row, provider)
            self.assertEqual(self.totals(), [(0, 0)] * 2)
        self.reject(429)
        self.assertEqual(AIRequest.objects.exclude(dispatch_started_at=None).count(), 3)

    def test_ambiguous_and_invalid_output_failures_consume(self):
        failures = (services.AITimeoutError, services.AIConnectionError, services.AITemporaryError,
                    services.AIResponseError, services.AIIncompleteResponseError, services.AIOversizedResponseError)
        for index, failure in enumerate(failures):
            user = get_user_model().objects.create_user(username=f"failure-{index}")
            row = self.admit(user=user).request
            with self.assertRaises(failure):
                control.call_provider(row, Mock(side_effect=failure()))
            self.assertEqual(self.totals(), [(0, index + 1)] * 2)

    def test_daily_capacity_blocks_other_account_without_reservation(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=1):
            row = self.admit().request
            error = self.reject(429, user=self.other)
            self.assertEqual(error.retry_after, 43200)
            self.assertEqual(error.metadata, {})
            self.assertEqual(self.totals(), [(1, 0)] * 2)
            self.assertEqual(AIRequest.objects.count(), 1)
            self.complete(row)
            self.reject(429, user=self.other)

    def test_weekly_capacity_blocks_independently(self):
        with override_settings(AI_GLOBAL_WEEKLY_CREDITS=1):
            self.complete(self.admit().request)
            error = self.reject(429, user=self.other)
            self.assertEqual(error.retry_after, 388800)

    def test_both_exhausted_use_later_reset(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=1, AI_GLOBAL_WEEKLY_CREDITS=1):
            self.admit()
            self.assertEqual(self.reject(429, user=self.other).retry_after, 388800)

    def test_zero_capacity_has_no_misleading_retry(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=0):
            self.assertIsNone(self.reject(429).retry_after)
            self.assertFalse(AIRequest.objects.exists())
            self.assertFalse(AIQuotaPeriod.objects.exists())

    def test_whole_operation_weight_must_fit(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=2):
            self.reject(429, operation=O.PROPOSAL_GENERATION)
            self.assertFalse(AIRequest.objects.exists())

    def test_recorded_limit_mismatch_fails_closed_without_overwrite(self):
        row = self.admit().request
        control.fail(row, F.LOCAL_CONFIGURATION, release=True)
        with override_settings(AI_GLOBAL_DAILY_CREDITS=13):
            self.reject()
        self.assertEqual(AIQuotaPeriod.objects.get(kind="daily").credit_limit, 12)
        self.assertEqual(self.totals(), [(0, 0)] * 2)

    def test_historical_null_limits_initialize_under_current_admission(self):
        for kind, start in ai_global.period_keys(MOMENT):
            AIQuotaPeriod.objects.create(kind=kind, period_start=start, consumed_credits=2)
        self.admit()
        self.assertEqual(self.totals(), [(1, 2)] * 2)
        self.assertEqual(list(AIQuotaPeriod.objects.order_by("kind").values_list("credit_limit", flat=True)), [12, 30])

    def test_second_period_failure_rolls_back_first_counter_and_ledger(self):
        original = AIQuotaPeriod.objects.filter
        def reject_second(*args, **kwargs):
            query = original(*args, **kwargs)
            if kwargs.get("pk") == AIQuotaPeriod.objects.get(kind="weekly").pk:
                query.update = Mock(side_effect=OperationalError("SYNTHETIC_DATABASE_DETAIL"))
            return query
        for kind, start in ai_global.period_keys(MOMENT):
            AIQuotaPeriod.objects.create(kind=kind, period_start=start)
        with patch.object(AIQuotaPeriod.objects, "filter", side_effect=reject_second):
            self.reject()
        self.assertEqual(self.totals(), [(0, 0)] * 2)
        self.assertFalse(AIRequest.objects.exists())

    def test_counter_underflow_rolls_back_finalization_and_app_write(self):
        row = self.admit().request
        control.mark_dispatch(row)
        AIQuotaPeriod.objects.filter(kind="weekly").update(reserved_credits=0)
        persist = Mock()
        with self.assertRaises(control.ControlError):
            control.succeed(row, persist)
        persist.assert_not_called()
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.IN_FLIGHT, Q.RESERVED))
        self.assertEqual(self.totals(), [(1, 0), (0, 0)])

    def test_missing_binding_blocks_dispatch_without_guessing_refund(self):
        row = self.admit().request
        AIRequest.objects.filter(pk=row.pk).update(global_day_period=None)
        provider = self.provider()
        with self.assertRaises(control.ControlError) as error:
            control.call_provider(row, provider)
        self.assertEqual(error.exception.status, 503)
        provider.assert_not_called()
        self.assertEqual(self.totals(), [(1, 0)] * 2)

    def test_period_references_are_server_controlled_and_protected(self):
        row = self.admit().request
        for name in ("global_day_period", "global_week_period"):
            self.assertFalse(AIRequest._meta.get_field(name).editable)
        with self.assertRaises(ProtectedError):
            row.global_day_period.delete()

    def test_period_constraints_reject_negative_or_invalid_rows(self):
        for fields in (dict(kind="invalid"), dict(reserved_credits=-1),
                       dict(consumed_credits=-1), dict(credit_limit=-1)):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                AIQuotaPeriod.objects.create(kind="daily" if "kind" not in fields else fields["kind"],
                    period_start=MOMENT.date(), **{k: v for k, v in fields.items() if k != "kind"})

    def test_period_identity_is_unique(self):
        self.admit()
        with self.assertRaises(IntegrityError), transaction.atomic():
            AIQuotaPeriod.objects.create(kind="daily", period_start=MOMENT.date())

    def test_account_deletion_does_not_refund_consumed_credits(self):
        self.complete(self.admit().request)
        self.user.delete()
        self.assertEqual(self.totals(), [(0, 1)] * 2)
        self.assertTrue(all(p["warning"] for p in ai_global.status(MOMENT)["periods"]))

    def test_active_account_deletion_does_not_invent_available_capacity(self):
        row = self.admit().request
        self.user.delete()
        with self.assertRaises(control.ControlError):
            control.call_provider(row, self.provider())
        self.assertEqual(self.totals(), [(1, 0)] * 2)

    def test_local_uncapped_mode_remains_available(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=None, AI_GLOBAL_WEEKLY_CREDITS=None):
            row = self.admit().request
            self.assertIsNone(row.global_day_period_id)
            self.complete(row)
            self.assertFalse(AIQuotaPeriod.objects.exists())

    def test_enabling_caps_blocks_unbound_local_request_before_dispatch(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=None, AI_GLOBAL_WEEKLY_CREDITS=None):
            row = self.admit().request
        with self.assertRaises(control.ControlError):
            control.mark_dispatch(row)
        self.assertIsNone(AIRequest.objects.get().dispatch_started_at)

    def test_completed_replay_does_not_charge_again_even_disabled(self):
        token = control.issue_nonce(self.user, O.PROFILE_SUMMARY)
        row = self.admit(token=token).request
        self.complete(row)
        with override_settings(AI_ENABLED=False):
            self.assertTrue(self.admit(token=token).replay)
        self.assertEqual(self.totals(), [(0, 1)] * 2)

    def test_fresh_identical_request_charges_normally(self):
        self.complete(self.admit().request)
        self.complete(self.admit().request)
        self.assertEqual(AIRequest.objects.count(), 2)
        self.assertEqual(self.totals(), [(0, 2)] * 2)

    def test_repeated_success_cannot_charge_or_persist_twice(self):
        row = self.admit().request
        self.complete(row)
        persist = Mock()
        with self.assertRaises(control.ControlError):
            control.succeed(row, persist)
        persist.assert_not_called()
        self.assertEqual(self.totals(), [(0, 1)] * 2)

    def test_disabled_before_admission_creates_nothing(self):
        with override_settings(AI_ENABLED=False):
            self.reject()
        self.assertFalse(AIRequest.objects.exists())
        self.assertFalse(AIQuotaPeriod.objects.exists())

    def test_disabled_after_reservation_releases_exactly_once(self):
        row = self.admit().request
        provider = self.provider()
        with override_settings(AI_ENABLED=False):
            for _ in range(2):
                with self.assertRaises(control.ControlError) as error:
                    control.call_provider(row, provider)
                self.assertEqual(error.exception.status, 503)
        provider.assert_not_called()
        self.assertEqual(self.totals(), [(0, 0)] * 2)
        self.assertEqual(AIRequest.objects.get().failure_category, F.AI_DISABLED)

    def test_disabled_after_dispatch_marker_sdk_guard_releases_and_counts_burst(self):
        row = self.admit().request
        with patch("proposal_ai.services.OpenAI") as sdk:
            def guarded():
                with override_settings(AI_ENABLED=False):
                    return services.generate_profile_summary("Synthetic input")
            with self.assertRaises(control.ControlError):
                control.call_provider(row, guarded)
            sdk.assert_not_called()
        self.assertIsNotNone(AIRequest.objects.get().dispatch_started_at)
        self.assertEqual(self.totals(), [(0, 0)] * 2)

    def test_switch_change_after_paid_work_does_not_refund(self):
        row = self.admit().request
        control.call_provider(row, self.provider())
        with override_settings(AI_ENABLED=False):
            control.succeed(row)
        self.assertEqual(self.totals(), [(0, 1)] * 2)

    def test_missing_key_before_dispatch_releases(self):
        row = self.admit().request
        self.config.side_effect = services.AIConfigurationError()
        with self.assertRaises(services.AIConfigurationError):
            control.call_provider(row, self.provider())
        self.assertIsNone(AIRequest.objects.get().dispatch_started_at)
        self.assertEqual(self.totals(), [(0, 0)] * 2)

    def test_undispatched_stale_inactive_account_recovered_before_admission(self):
        self.admit()
        self.clock.return_value = MOMENT + control.LEASE
        with override_settings(AI_GLOBAL_DAILY_CREDITS=12):
            row = self.admit(user=self.other).request
        self.assertEqual(self.totals(), [(1, 0)] * 2)
        self.assertEqual(AIRequest.objects.get(user=self.user).quota_state, Q.RELEASED)
        self.assertEqual(row.lifecycle, L.RESERVED)

    def test_dispatched_stale_becomes_uncertain_consumed_once(self):
        row = self.admit().request
        control.mark_dispatch(row)
        self.clock.return_value = MOMENT + control.LEASE
        self.assertEqual(control.recover_global_stale(), dict(released=0, consumed=1))
        self.assertEqual(control.recover_global_stale(), dict(released=0, consumed=0))
        self.assertEqual(self.totals(), [(0, 1)] * 2)
        self.assertEqual(AIRequest.objects.get().lifecycle, L.UNCERTAIN)

    def test_later_period_recovery_only_adjusts_original_periods(self):
        row = self.admit().request
        original = (row.global_day_period_id, row.global_week_period_id)
        self.clock.return_value = MOMENT + timedelta(days=8)
        self.admit(user=self.other)
        self.assertEqual(list(AIQuotaPeriod.objects.filter(pk__in=original).values_list(
            "reserved_credits", "consumed_credits")), [(0, 0)] * 2)

    def test_recovery_batch_is_bounded(self):
        self.admit()
        self.admit(user=self.other)
        self.clock.return_value = MOMENT + control.LEASE
        self.assertEqual(control.recover_global_stale(limit=1)["released"], 1)
        self.assertEqual(AIRequest.objects.filter(lifecycle=L.RESERVED).count(), 1)

    def test_late_worker_cannot_write_telemetry_or_application(self):
        row = self.admit().request
        control.mark_dispatch(row)
        self.clock.return_value = MOMENT + control.LEASE
        control.recover_global_stale()
        persist = Mock()
        with self.assertRaises(control.ControlError):
            control.record_telemetry(row, services.AITelemetry(total_tokens=99))
        with self.assertRaises(control.ControlError):
            control.succeed(row, persist)
        persist.assert_not_called()
        self.assertIsNone(AIRequest.objects.get().total_tokens)
        self.assertEqual(self.totals(), [(0, 1)] * 2)

    def test_persistence_failure_preserves_telemetry_and_consumes(self):
        row = self.admit(O.JOB_EXTRACTION).request
        control.call_provider(row, self.provider())
        def persist():
            JobPost.objects.create(user=self.user, job_title="Synthetic", job_description="Synthetic")
            raise ValueError("SYNTHETIC_PRIVATE_PERSISTENCE_ERROR")
        with self.assertRaises(control.ControlError) as error:
            control.succeed(row, persist)
        self.assertNotIn("SYNTHETIC_PRIVATE", str(error.exception))
        self.assertFalse(JobPost.objects.exists())
        row.refresh_from_db()
        self.assertEqual((row.quota_state, row.total_tokens), (Q.CONSUMED, 24))
        self.assertEqual(self.totals(), [(0, 2)] * 2)

    def test_database_error_before_dispatch_has_no_provider_call(self):
        provider = self.provider()
        with patch("proposal_ai.ai_global.lock_current", side_effect=OperationalError("SYNTHETIC_DB")):
            with self.assertRaises(control.ControlError) as error:
                row = self.admit().request
                control.call_provider(row, provider)
        self.assertEqual(error.exception.status, 503)
        provider.assert_not_called()
        self.assertFalse(AIRequest.objects.exists())

    def test_boundary_restart_is_bounded_without_provider_work(self):
        with patch("proposal_ai.ai_global.matches", return_value=False) as mismatch:
            self.reject()
        self.assertEqual(mismatch.call_count, control.ADMISSION_RESTARTS)
        self.assertFalse(AIRequest.objects.exists())
        self.assertFalse(AIQuotaPeriod.objects.exists())

    def test_policy_constants_are_unchanged(self):
        self.assertEqual(tuple(control.CREDITS.values()), (1, 2, 3))
        self.assertEqual((control.DAILY_CREDITS, control.WEEKLY_CREDITS), (25, 100))
        self.assertEqual(tuple(control.BURSTS.values()), ((3, timedelta(minutes=10)),
                         (3, timedelta(minutes=5)), (2, timedelta(minutes=5))))
        self.assertEqual((control.NONCE_MAX_AGE, control.LEASE), (86400, timedelta(minutes=10)))


class DisabledServiceTests(SimpleTestCase):
    @override_settings(AI_ENABLED=False)
    def test_all_service_paths_stop_before_sdk_construction(self):
        with patch("proposal_ai.services.OpenAI") as sdk:
            for function, args in ((services.generate_profile_summary, ("Synthetic",)),
                (services.extract_job_details, ("Synthetic",)),
                (services.generate_proposal, ("Synthetic", "Synthetic")),
                (services.generate_freelancer_profile_summary, ("Developer", "Python"))):
                with self.subTest(function=function.__name__), self.assertRaises(services.AIDisabledError):
                    function(*args)
            sdk.assert_not_called()


@override_settings(**POLICY, **old_control.UI_SETTINGS)
class GlobalHTTPTests(TestCase):
    setUp = old_control.WorkflowTests.setUp
    token = old_control.WorkflowTests.token
    post = old_control.WorkflowTests.post

    def setUp(self):
        old_control.WorkflowTests.setUp(self)
        config = patch("proposal_ai.services.check_configuration", return_value=None)
        config.start()
        self.addCleanup(config.stop)

    def test_all_operations_disabled_return_503_without_provider(self):
        with override_settings(AI_ENABLED=False):
            for op in control.CREDITS:
                self.assertEqual(self.post(op).status_code, 503)
        self.assertFalse(AIRequest.objects.exists())
        for provider in self.providers.values():
            provider.assert_not_called()

    def test_saved_pages_remain_accessible_while_disabled(self):
        with override_settings(AI_ENABLED=False):
            for route, args in (("dashboard", []), ("extract_job_features", []),
                ("create_freelancer_profile", []), ("confirm_job_features", [self.job.pk])):
                self.assertEqual(self.client.get(reverse(route, args=args)).status_code, 200)

    def test_successful_extraction_replay_while_disabled_reuses_job(self):
        token = self.token(O.JOB_EXTRACTION)
        first = self.post(O.JOB_EXTRACTION, token)
        self.assertEqual(first.status_code, 302)
        with override_settings(AI_ENABLED=False):
            replay = self.post(O.JOB_EXTRACTION, token)
        self.assertEqual(replay.url, first.url)
        self.assertEqual(AIRequest.objects.count(), 1)
        self.providers["extract_job_details"].assert_called_once()

    def test_successful_proposal_replay_while_disabled_reuses_result(self):
        token = self.token(O.PROPOSAL_GENERATION)
        self.assertEqual(self.post(O.PROPOSAL_GENERATION, token).status_code, 302)
        with override_settings(AI_ENABLED=False):
            self.assertEqual(self.post(O.PROPOSAL_GENERATION, token).status_code, 302)
        self.assertEqual(AIRequest.objects.count(), 1)
        self.providers["generate_proposal"].assert_called_once()

    def test_summary_replay_keeps_lost_output_behavior_while_disabled(self):
        token = self.token(O.PROFILE_SUMMARY)
        self.assertEqual(self.post(O.PROFILE_SUMMARY, token).status_code, 200)
        with override_settings(AI_ENABLED=False):
            self.assertEqual(self.post(O.PROFILE_SUMMARY, token).status_code, 409)
        self.providers["generate_profile_summary"].assert_called_once()

    def test_fresh_regeneration_disabled(self):
        self.post(O.PROPOSAL_GENERATION)
        with override_settings(AI_ENABLED=False):
            self.assertEqual(self.post(O.PROPOSAL_GENERATION,
                self.token(O.PROPOSAL_GENERATION, I.REGENERATE)).status_code, 503)

    def test_global_rejection_preserves_input_nonce_and_has_safe_retry(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=1):
            self.post(O.PROFILE_SUMMARY)
            token = self.token(O.JOB_EXTRACTION)
            response = self.post(O.JOB_EXTRACTION, token)
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)
        control.validate_nonce(response.context["ai_nonce"], self.user, O.JOB_EXTRACTION)
        self.assertEqual(response.context["form"]["raw_job_text"].value(), "PRIVATE_PASTE developer hourly project")
        self.assertNotContains(response, "global_daily", status_code=429)
        self.providers["extract_job_details"].assert_not_called()

    def test_zero_limit_no_retry_header(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=0):
            result = self.post(O.PROFILE_SUMMARY)
        self.assertEqual(result.status_code, 429)
        self.assertNotIn("Retry-After", result)

    def test_continue_anyway_still_cannot_bypass_input_maximum(self):
        response = self.post(O.JOB_EXTRACTION, raw_job_text="x" * 20001)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AIRequest.objects.exists())
        self.assertFalse(AIQuotaPeriod.objects.exists())


@override_settings(**POLICY)
class OperatorTests(GlobalFixture, TestCase):
    def test_status_does_not_create_periods_or_recover_stale_records(self):
        output = io.StringIO()
        call_command("ai_status", stdout=output)
        self.assertFalse(AIQuotaPeriod.objects.exists())
        self.admit()
        self.clock.return_value = MOMENT + control.LEASE
        before = list(AIRequest.objects.values())
        counters = self.totals()
        call_command("ai_status", stdout=output)
        self.assertEqual(before, list(AIRequest.objects.values()))
        self.assertEqual(self.totals(), counters)
        self.assertNotIn("fingerprint", output.getvalue())
        self.assertNotIn("nonce", output.getvalue())
        self.assertNotIn("Synthetic input", output.getvalue())

    def test_recovery_command_is_separate_bounded_mutation(self):
        self.admit()
        self.admit(user=self.other)
        self.clock.return_value = MOMENT + control.LEASE
        call_command("recover_ai_requests", limit=1, stdout=io.StringIO())
        self.assertEqual(AIRequest.objects.filter(quota_state=Q.RELEASED).count(), 1)

    def test_period_admin_has_no_mutation_paths_even_for_superuser(self):
        request = RequestFactory().get("/admin/")
        request.user = get_user_model().objects.create_superuser("operator", password="synthetic-test-password")
        inspector = admin.site._registry[AIQuotaPeriod]
        self.assertTrue(inspector.has_view_permission(request))
        self.assertFalse(inspector.has_add_permission(request))
        self.assertFalse(inspector.has_change_permission(request))
        self.assertFalse(inspector.has_delete_permission(request))
        self.assertEqual(inspector.get_actions(request), {})

    def test_period_admin_requires_model_permission(self):
        request = RequestFactory().get("/admin/")
        request.user = self.user
        self.assertFalse(admin.site._registry[AIQuotaPeriod].has_view_permission(request))

    def test_period_schema_contains_only_accounting_scalars(self):
        self.assertEqual({f.name for f in AIQuotaPeriod._meta.fields},
            {"id", "kind", "period_start", "credit_limit", "reserved_credits", "consumed_credits"})


@override_settings(**POLICY)
class GlobalTransactionTests(GlobalFixture, TransactionTestCase):
    def test_provider_calls_are_outside_admission_transactions(self):
        row = self.admit().request
        def provider():
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(self.totals(), [(1, 0)] * 2)
            return services.AIServiceResult("Synthetic", services.AITelemetry())
        control.call_provider(row, provider)
        control.succeed(row)

    def test_historical_bootstrap_preserves_evidence(self):
        executor = MigrationExecutor(connection)
        final_targets = executor.loader.graph.leaf_nodes()
        before = [("proposal_ai", "0015_ai_request_telemetry")]
        after = [("proposal_ai", "0016_ai_global_exposure")]
        try:
            executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            Request = apps.get_model("proposal_ai", "AIRequest")
            User = apps.get_model("auth", "User")
            expected = {}
            records = []
            for index, (op, units, quota, instant) in enumerate((
                (O.PROFILE_SUMMARY, 1, Q.RESERVED, MOMENT),
                (O.JOB_EXTRACTION, 2, Q.CONSUMED, MOMENT),
                (O.PROPOSAL_GENERATION, 3, Q.RELEASED, MOMENT),
                (O.PROPOSAL_GENERATION, 3, Q.CONSUMED, datetime(2026, 10, 4, 23, 59, tzinfo=timezone.utc)),
                (O.PROFILE_SUMMARY, 1, Q.CONSUMED, datetime(2026, 10, 5, tzinfo=timezone.utc)),
            )):
                user = User.objects.create(username=f"historical-{index}")
                row = Request.objects.create(user_id=user.pk, operation=op, quota_units=units,
                    quota_state=quota, lifecycle=L.RESERVED if quota == Q.RESERVED else L.SUCCEEDED,
                    nonce=uuid.uuid4(), intent=I.GENERATE, submitted_fingerprint="a" * 64,
                    effective_fingerprint="b" * 64, admitted_at=instant, lease_expires_at=instant + control.LEASE,
                    total_tokens=42, response_model="synthetic-model")
                records.append((row.pk, quota, row.lifecycle, row.admitted_at))
                for kind, start in ai_global.period_keys(instant):
                    totals = expected.setdefault((kind, start), [0, 0])
                    if quota != Q.RELEASED:
                        totals[0 if quota == Q.RESERVED else 1] += units
            with connection.cursor() as cursor:
                old_constraints = connection.introspection.get_constraints(cursor, "proposal_ai_airequest")
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            actual = {(p.kind, p.period_start): [p.reserved_credits, p.consumed_credits]
                      for p in AIQuotaPeriod.objects.all()}
            self.assertEqual(actual, expected)
            self.assertFalse(AIQuotaPeriod.objects.exclude(credit_limit=None).exists())
            for pk, quota, lifecycle, instant in records:
                row = AIRequest.objects.get(pk=pk)
                self.assertEqual((row.quota_state, row.lifecycle, row.admitted_at), (quota, lifecycle, instant))
                self.assertEqual((row.total_tokens, row.response_model), (42, "synthetic-model"))
                self.assertEqual((row.global_day_period.kind, row.global_day_period.period_start), ai_global.period_keys(instant)[0])
                self.assertEqual((row.global_week_period.kind, row.global_week_period.period_start), ai_global.period_keys(instant)[1])
            with connection.cursor() as cursor:
                new_constraints = connection.introspection.get_constraints(cursor, "proposal_ai_airequest")
            for name, definition in old_constraints.items():
                if definition["index"] or definition["unique"] or definition["check"]:
                    self.assertEqual(new_constraints[name], definition)
        finally:
            MigrationExecutor(connection).migrate(final_targets)
