"""Opt-in global-accounting matrix on the designated disposable PostgreSQL.

Events/barriers arrange tests only; application coordination uses database rows.
Every provider is fake and the existing network-blocking runner remains active.
"""
from datetime import datetime, timedelta, timezone
import threading
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection, IntegrityError, OperationalError, transaction
from django.test import override_settings

from mysite import postgres_concurrency_tests as existing, test_ai_exposure as ordinary
from proposal_ai import ai_control as control, ai_global, services
from proposal_ai.models import AIQuotaPeriod, AIRequest, JobPost, Proposal

O, L, Q, F = AIRequest.Operation, AIRequest.Lifecycle, AIRequest.Quota, AIRequest.Failure
MOMENT = existing.MOMENT
POLICY = ordinary.POLICY


@override_settings(**POLICY)
class GlobalPostgreSQLTests(existing.PostgreSQLCase):
    def setUp(self):
        super().setUp()
        self.other = get_user_model().objects.create_user(username="pg-global-other")

    def totals(self):
        return list(AIQuotaPeriod.objects.order_by("kind", "period_start").values_list(
            "reserved_credits", "consumed_credits"))

    def race(self, operations, users=None, same_nonce=False):
        users = users or [self.user, self.other]
        barrier = threading.Barrier(len(operations))
        tokens = [control.issue_nonce(user, op) for user, op in zip(users, operations, strict=True)]
        if same_nonce:
            tokens = [tokens[0]] * len(tokens)
        providers = [self.provider(op) for op in operations]
        def attempt(index):
            row = self.admit(operations[index], user=users[index], token=tokens[index]).request
            control.call_provider(row, providers[index])
            return row
        threads = [self.worker(str(i), lambda i=i: attempt(i), barrier) for i in range(len(operations))]
        for thread in threads:
            thread.start()
        self.join(threads)
        return [result[1] for result in self.results.values() if result[0] == "ok"], providers, tokens

    def final_credit_race(self, daily=1, weekly=30):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=daily, AI_GLOBAL_WEEKLY_CREDITS=weekly):
            winners, providers, _ = self.race([O.PROFILE_SUMMARY] * 2)
        self.assertEqual(len(winners), 1, self.results)
        self.assertEqual(sum(p.call_count for p in providers), 1)
        self.assertEqual(self.totals(), [(1, 0)] * 2)
        self.assertEqual([result[1].status for result in self.results.values() if result[0] == "control"], [429])

    def locked_admission(self, before=MOMENT, after=None, *, disable=False):
        """B holds a real period row lock while A acquires its user slot."""
        after = after or before + timedelta(seconds=2)
        daily = AIQuotaPeriod.objects.get_or_create(kind="daily", period_start=before.date(),
                                                    defaults={"credit_limit": 12})[0]
        ready, release = threading.Event(), threading.Event()
        calls = 0
        def clock():
            nonlocal calls
            if threading.current_thread().name == "A":
                calls += 1
                return before if calls <= 2 else after
            return before
        def hold():
            with transaction.atomic():
                AIQuotaPeriod.objects.select_for_update().get(pk=daily.pk)
                ready.set()
                self.assertTrue(release.wait(10))
        provider = self.provider()
        def attempt():
            row = self.admit().request
            control.call_provider(row, provider)
            return row
        threads = [self.worker("B", hold), self.worker("A", attempt)]
        with patch("proposal_ai.ai_control.now", side_effect=clock):
            try:
                threads[0].start()
                self.assertTrue(ready.wait(10))
                threads[1].start()
                self.wait_for_lock("A", "B", "SELECT")
                if disable:
                    with override_settings(AI_ENABLED=False):
                        release.set()
                        self.join(threads)
                else:
                    release.set()
                    self.join(threads)
            finally:
                release.set()
                for thread in threads:
                    if thread.is_alive():
                        thread.join(20)
        self.assertEqual(self.results["B"][0], "ok", self.results)
        return self.results["A"], provider

    def test_final_daily_global_credit_race(self):
        self.final_credit_race()

    def test_final_weekly_global_credit_race(self):
        self.final_credit_race(daily=12, weekly=1)

    def test_three_workers_different_weights_cannot_overspend(self):
        third = get_user_model().objects.create_user(username="pg-global-third")
        with override_settings(AI_GLOBAL_DAILY_CREDITS=3):
            winners, providers, _ = self.race(list(control.CREDITS), [self.user, self.other, third])
        weights = sum(row.quota_units for row in winners)
        self.assertLessEqual(weights, 3)
        self.assertEqual(sum(p.call_count for p in providers), len(winners))
        self.assertEqual(self.totals(), [(weights, 0)] * 2)

    def test_concurrent_first_period_creation_is_unique(self):
        winners, providers, _ = self.race([O.PROFILE_SUMMARY] * 2)
        self.assertEqual(len(winners), 2, self.results)
        self.assertEqual(AIQuotaPeriod.objects.count(), 2)
        self.assertEqual(self.totals(), [(2, 0)] * 2)
        self.assertEqual(sum(p.call_count for p in providers), 2)

    def test_different_users_provider_execution_can_overlap_without_locks(self):
        barrier = threading.Barrier(2)
        providers = [self.provider(barrier=barrier), self.provider(barrier=barrier)]
        def attempt(index):
            row = self.admit(user=[self.user, self.other][index]).request
            control.call_provider(row, providers[index])
            control.succeed(row)
        threads = [self.worker(str(i), lambda i=i: attempt(i)) for i in range(2)]
        for thread in threads:
            thread.start()
        self.join(threads)
        self.assertEqual([r[0] for r in self.results.values()], ["ok", "ok"])
        self.assertEqual(self.totals(), [(0, 2)] * 2)

    def test_global_row_lock_refreshes_admission_and_full_lease(self):
        after = MOMENT + timedelta(minutes=4)
        result, provider = self.locked_admission(after=after)
        self.assertEqual(result[0], "ok", self.results)
        self.assertEqual((result[1].admitted_at, result[1].lease_expires_at), (after, after + control.LEASE))
        provider.assert_called_once()

    def test_global_wait_crossing_midnight_restarts_into_new_day(self):
        before = datetime(2026, 10, 7, 23, 59, 59, tzinfo=timezone.utc)
        after = before + timedelta(seconds=2)
        result, provider = self.locked_admission(before, after)
        self.assertEqual(result[0], "ok", self.results)
        self.assertEqual(result[1].global_day_period.period_start, after.date())
        self.assertEqual(AIQuotaPeriod.objects.get(kind="daily", period_start=before.date()).reserved_credits, 0)
        self.assertEqual(result[1].lease_expires_at, after + control.LEASE)
        provider.assert_called_once()

    def test_global_wait_crossing_monday_restarts_both_periods(self):
        before = datetime(2026, 10, 4, 23, 59, 59, tzinfo=timezone.utc)
        result, provider = self.locked_admission(before, before + timedelta(seconds=2))
        self.assertEqual(result[0], "ok", self.results)
        row = result[1]
        self.assertEqual(row.global_week_period.period_start, datetime(2026, 10, 5).date())
        self.assertEqual(row.global_week_period.reserved_credits, 1)
        self.assertFalse(AIQuotaPeriod.objects.filter(kind="weekly", period_start=datetime(2026, 9, 28).date()).exists())
        provider.assert_called_once()

    def test_kill_switch_changed_while_waiting_rolls_back_admission(self):
        result, provider = self.locked_admission(disable=True)
        self.assertEqual((result[0], result[1].status), ("control", 503))
        provider.assert_not_called()
        self.assertFalse(AIRequest.objects.exists())
        self.assertEqual(self.totals(), [(0, 0)])

    def test_current_limit_conflict_fails_closed(self):
        row = self.admit().request
        control.fail(row, F.LOCAL_CONFIGURATION, release=True)
        with override_settings(AI_GLOBAL_WEEKLY_CREDITS=31):
            with self.assertRaises(control.ControlError) as error:
                self.admit(user=self.other)
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(AIQuotaPeriod.objects.get(kind="weekly").credit_limit, 30)
        self.assertEqual(self.totals(), [(0, 0)] * 2)

    def test_competitor_completion_after_user_slot_wait_still_counts_burst_with_global_locks(self):
        self.history(2, MOMENT - timedelta(minutes=1), dispatched=True)
        result = self.waiting_admission()
        self.assertEqual((result[0], result[1].status), ("control", 429))
        self.assertEqual(self.totals(), [(0, 1)] * 2)

    def test_burst_expiry_during_global_lock_wait_uses_final_time(self):
        self.history(3, MOMENT - timedelta(minutes=10) + timedelta(seconds=1), dispatched=True)
        result, provider = self.locked_admission(after=MOMENT + timedelta(seconds=2))
        self.assertEqual(result[0], "ok", self.results)
        provider.assert_called_once()

    def finalization_wait(self, consume):
        """B owns ledger -> periods; A's other-account admission really waits."""
        with override_settings(AI_GLOBAL_DAILY_CREDITS=1):
            row = self.admit().request
            b_provider = self.provider()
            if consume:
                control.call_provider(row, b_provider)
            ready, release = threading.Event(), threading.Event()
            original = ai_global.finish_reservation
            a_calls = 0
            after = MOMENT + timedelta(seconds=2)
            def clock():
                nonlocal a_calls
                if threading.current_thread().name == "A":
                    a_calls += 1
                    return MOMENT if a_calls <= 2 else after
                return MOMENT + timedelta(seconds=1)
            def finish(owned, quota):
                result = original(owned, quota)
                if threading.current_thread().name == "B":
                    ready.set()
                    self.assertTrue(release.wait(10))
                return result
            a_provider = self.provider()
            def attempt():
                new = self.admit(user=self.other).request
                control.call_provider(new, a_provider)
                return new
            def complete():
                if consume:
                    control.succeed(row)
                else:
                    control.fail(row, F.LOCAL_CONFIGURATION, release=True)
            threads = [self.worker("B", complete), self.worker("A", attempt)]
            with patch("proposal_ai.ai_global.finish_reservation", side_effect=finish), \
                    patch("proposal_ai.ai_control.now", side_effect=clock):
                try:
                    threads[0].start()
                    self.assertTrue(ready.wait(10))
                    threads[1].start()
                    self.wait_for_lock("A", "B", "SELECT")
                finally:
                    release.set()
                    self.join(threads)
            self.assertEqual(self.results["B"][0], "ok", self.results)
            if consume:
                self.assertEqual((self.results["A"][0], self.results["A"][1].status), ("control", 429))
                self.assertEqual(self.results["A"][1].retry_after, 43198)
                a_provider.assert_not_called()
                b_provider.assert_called_once()
                self.assertEqual(self.totals(), [(0, 1)] * 2)
            else:
                self.assertEqual(self.results["A"][0], "ok", self.results)
                self.assertEqual(self.results["A"][1].admitted_at, after)
                a_provider.assert_called_once()
                self.assertEqual(self.totals(), [(1, 0)] * 2)

    def test_competitor_consumption_during_lock_wait_blocks_final_global_credit(self):
        self.finalization_wait(True)

    def test_competitor_release_during_lock_wait_restores_final_global_credit(self):
        self.finalization_wait(False)

    def test_same_nonce_race_creates_one_global_reservation(self):
        winners, providers, _ = self.race([O.PROFILE_SUMMARY] * 2, [self.user] * 2, same_nonce=True)
        self.assertEqual(len(winners), 1, self.results)
        self.assertEqual(AIRequest.objects.count(), 1)
        self.assertEqual(sum(p.call_count for p in providers), 1)
        self.assertEqual(self.totals(), [(1, 0)] * 2)

    def test_account_wide_slot_remains_for_different_operations(self):
        winners, providers, _ = self.race([O.PROFILE_SUMMARY, O.JOB_EXTRACTION], [self.user] * 2)
        self.assertEqual(len(winners), 1, self.results)
        self.assertEqual(sum(p.call_count for p in providers), 1)
        self.assertEqual(self.totals(), [(winners[0].quota_units, 0)] * 2)

    def test_two_releases_and_admission_race_adjust_once(self):
        row = self.admit().request
        barrier = threading.Barrier(3)
        threads = [self.worker("release-1", lambda: control.fail(row, F.LOCAL_CONFIGURATION, release=True), barrier),
                   self.worker("release-2", lambda: control.fail(row, F.LOCAL_CONFIGURATION, release=True), barrier),
                   self.worker("new", lambda: self.admit(user=self.other), barrier)]
        for thread in threads:
            thread.start()
        self.join(threads)
        self.assertTrue(all(r[0] == "ok" for r in self.results.values()), self.results)
        self.assertEqual(self.totals(), [(1, 0)] * 2)

    def test_release_restores_final_global_credit(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=1):
            row = self.admit().request
            control.fail(row, F.LOCAL_CONFIGURATION, release=True)
            control.fail(row, F.LOCAL_CONFIGURATION, release=True)
            self.admit(user=self.other)
        self.assertEqual(self.totals(), [(1, 0)] * 2)

    def recovery_race(self, dispatched):
        row = self.admit().request
        if dispatched:
            control.mark_dispatch(row)
        with patch("proposal_ai.ai_control.now", return_value=MOMENT + control.LEASE):
            barrier = threading.Barrier(2)
            threads = [self.worker(str(i), control.recover_global_stale, barrier) for i in range(2)]
            for thread in threads:
                thread.start()
            self.join(threads)
        row.refresh_from_db()
        self.assertEqual(row.quota_state, Q.CONSUMED if dispatched else Q.RELEASED)
        self.assertEqual(row.lifecycle, L.UNCERTAIN if dispatched else L.FAILED)
        name = "consumed" if dispatched else "released"
        self.assertEqual(sum(r[1][name] for r in self.results.values()), 1)
        self.assertEqual(self.totals(), [(0, 1 if dispatched else 0)] * 2)

    def test_two_recovery_workers_release_undispatched_once(self):
        self.recovery_race(False)

    def test_two_recovery_workers_consume_dispatched_once(self):
        self.recovery_race(True)

    def test_later_period_recovery_uses_original_bindings(self):
        row = self.admit().request
        control.mark_dispatch(row)
        original = (row.global_day_period_id, row.global_week_period_id)
        with patch("proposal_ai.ai_control.now", return_value=MOMENT + timedelta(days=8)):
            self.admit(user=self.other)
        self.assertEqual(list(AIQuotaPeriod.objects.filter(pk__in=original).values_list(
            "reserved_credits", "consumed_credits")), [(0, 1)] * 2)
        self.assertEqual(AIQuotaPeriod.objects.count(), 4)

    def test_late_worker_cannot_persist_telemetry_job_or_proposal(self):
        row, provider = self.dispatched_row()
        with patch("proposal_ai.ai_control.now", return_value=MOMENT + control.LEASE):
            control.recover_global_stale()
            with self.assertRaises(control.ControlError):
                control.record_telemetry(row, self.telemetry(total_tokens=99))
            for persist in (Mock(side_effect=lambda: JobPost.objects.create(user=self.user, job_title="Late")),
                            Mock(side_effect=lambda: Proposal.objects.create())):
                with self.assertRaises(control.ControlError):
                    control.succeed(row, persist)
                persist.assert_not_called()
        self.assertFalse(JobPost.objects.exists())
        self.assertFalse(Proposal.objects.exists())
        row.refresh_from_db()
        self.assertEqual(row.total_tokens, 24)
        self.assertEqual(self.totals(), [(0, 1)] * 2)
        provider.assert_called_once()

    def test_telemetry_and_stale_race_keeps_terminal_state_and_counters(self):
        row, _ = self.dispatched_row()
        barrier = threading.Barrier(2)
        with patch("proposal_ai.ai_control.now", return_value=MOMENT + control.LEASE):
            threads = [self.worker("telemetry", lambda: control.record_telemetry(row, self.telemetry(total_tokens=99)), barrier),
                       self.worker("stale", control.recover_global_stale, barrier)]
            for thread in threads:
                thread.start()
            self.join(threads)
        self.assertEqual(self.results["telemetry"][0], "control")
        self.assertEqual(self.totals(), [(0, 1)] * 2)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.total_tokens), (L.UNCERTAIN, 24))

    def test_expiring_while_waiting_for_finalization_period_locks_fences_application(self):
        row, _ = self.dispatched_row()
        ready, release = threading.Event(), threading.Event()
        late = MOMENT + control.LEASE
        persist = Mock()
        clock_calls = 0
        def clock():
            nonlocal clock_calls
            if threading.current_thread().name == "A":
                clock_calls += 1
                return MOMENT if clock_calls == 1 else late
            return MOMENT
        def hold():
            with transaction.atomic():
                AIQuotaPeriod.objects.select_for_update().get(pk=row.global_day_period_id)
                ready.set()
                self.assertTrue(release.wait(10))
        threads = [self.worker("B", hold), self.worker("A", lambda: control.succeed(row, persist))]
        with patch("proposal_ai.ai_control.now", side_effect=clock):
            try:
                threads[0].start()
                self.assertTrue(ready.wait(10))
                threads[1].start()
                self.wait_for_lock("A", "B", "SELECT")
            finally:
                release.set()
                self.join(threads)
        self.assertEqual(self.results["A"][0], "control")
        persist.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.lifecycle, L.UNCERTAIN)
        self.assertEqual(self.totals(), [(0, 1)] * 2)

    def test_extraction_success_coordinates_app_and_global_ledger(self):
        row, provider = self.dispatched_row(O.JOB_EXTRACTION)
        job = control.succeed(row, lambda: JobPost.objects.create(user=self.user, job_title="Synthetic", job_description="Synthetic"))
        row.refresh_from_db()
        self.assertEqual((row.job_post_id, row.lifecycle, row.quota_state), (job.pk, L.SUCCEEDED, Q.CONSUMED))
        self.assertEqual(self.totals(), [(0, 2)] * 2)
        provider.assert_called_once()

    def test_proposal_success_coordinates_application_and_global_ledger(self):
        job = JobPost.objects.create(user=self.user, job_title="Synthetic", job_description="Synthetic")
        row, provider = self.dispatched_row(O.PROPOSAL_GENERATION, job.pk)
        def persist():
            job.confirmed_by_user = True
            job.save()
            return Proposal.objects.create(user=self.user, job_post=job, final_text="Synthetic output")
        proposal = control.succeed(row, persist)
        row.refresh_from_db()
        self.assertEqual((row.proposal_id, row.job_post_id), (proposal.pk, job.pk))
        self.assertEqual(self.totals(), [(0, 3)] * 2)
        provider.assert_called_once()

    def test_application_failure_retains_telemetry_and_consumes_without_retry(self):
        row, provider = self.dispatched_row(O.JOB_EXTRACTION)
        def persist():
            JobPost.objects.create(user=self.user, job_title="Synthetic", job_description="Synthetic")
            raise IntegrityError("SYNTHETIC_DATABASE_DETAIL")
        with self.assertRaises(control.ControlError):
            control.succeed(row, persist)
        self.assertFalse(JobPost.objects.exists())
        row.refresh_from_db()
        self.assertEqual((row.total_tokens, row.quota_state), (24, Q.CONSUMED))
        self.assertEqual(self.totals(), [(0, 2)] * 2)
        provider.assert_called_once()

    def test_database_failure_before_dispatch_rolls_back_all_reservations(self):
        provider = self.provider()
        with patch("proposal_ai.ai_global.lock_current", side_effect=OperationalError("SYNTHETIC_DB")):
            with self.assertRaises(control.ControlError) as error:
                row = self.admit().request
                control.call_provider(row, provider)
        self.assertEqual(error.exception.status, 503)
        provider.assert_not_called()
        self.assertFalse(AIRequest.objects.exists())
        self.assertFalse(AIQuotaPeriod.objects.exists())

    def test_disabled_before_admission_no_database_reservation(self):
        with override_settings(AI_ENABLED=False), self.assertRaises(control.ControlError) as error:
            self.admit()
        self.assertEqual(error.exception.status, 503)
        self.assertFalse(AIRequest.objects.exists())

    def test_disabled_after_reservation_releases_both_without_provider(self):
        row = self.admit().request
        provider = self.provider()
        with override_settings(AI_ENABLED=False), self.assertRaises(control.ControlError) as error:
            control.call_provider(row, provider)
        self.assertEqual(error.exception.status, 503)
        provider.assert_not_called()
        self.assertEqual(self.totals(), [(0, 0)] * 2)

    def test_completed_replay_disabled_does_not_duplicate_global_charge(self):
        token = control.issue_nonce(self.user, O.PROFILE_SUMMARY)
        row = self.admit(token=token).request
        provider = self.provider()
        control.call_provider(row, provider)
        control.succeed(row)
        with override_settings(AI_ENABLED=False):
            self.assertTrue(self.admit(token=token).replay)
        provider.assert_called_once()
        self.assertEqual(self.totals(), [(0, 1)] * 2)

    def test_global_permits_but_per_user_rejects_no_global_charge(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=100, AI_GLOBAL_WEEKLY_CREDITS=200):
            self.history(25, MOMENT - timedelta(hours=1))
            with self.assertRaises(control.ControlError) as error:
                self.admit()
        self.assertEqual(error.exception.status, 429)
        self.assertFalse(AIQuotaPeriod.objects.exists())

    def test_per_user_permits_but_global_rejects(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=0), self.assertRaises(control.ControlError) as error:
            self.admit()
        self.assertEqual(error.exception.status, 429)
        self.assertFalse(AIRequest.objects.exists())

    def test_per_user_final_credit_race_with_global_limits(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=100, AI_GLOBAL_WEEKLY_CREDITS=200):
            self.history(24, MOMENT - timedelta(hours=1))
            winners, providers, _ = self.race([O.PROFILE_SUMMARY] * 2, [self.user] * 2)
        self.assertEqual(len(winners), 1, self.results)
        self.assertEqual(sum(p.call_count for p in providers), 1)
        self.assertEqual(self.totals(), [(1, 0)] * 2)

    def test_per_user_final_weekly_credit_race_with_global_limits(self):
        with override_settings(AI_GLOBAL_DAILY_CREDITS=100, AI_GLOBAL_WEEKLY_CREDITS=200):
            self.history(99, MOMENT - timedelta(days=1))
            winners, providers, _ = self.race([O.PROFILE_SUMMARY] * 2, [self.user] * 2)
        self.assertEqual(len(winners), 1, self.results)
        self.assertEqual(sum(p.call_count for p in providers), 1)
        self.assertEqual(self.totals(), [(1, 0)] * 2)

    def test_unsupported_isolation_still_blocks_before_provider(self):
        provider = self.provider()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            with self.assertRaises(control.ControlError) as error:
                row = self.admit().request
                control.call_provider(row, provider)
            self.assertEqual(error.exception.status, 503)
            provider.assert_not_called()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ COMMITTED")

    def test_cross_period_recovery_and_admission_do_not_invert_lock_order(self):
        row = self.admit().request
        control.mark_dispatch(row)
        barrier = threading.Barrier(2)
        with patch("proposal_ai.ai_control.now", return_value=MOMENT + timedelta(days=8)):
            threads = [self.worker("recover", control.recover_global_stale, barrier),
                       self.worker("admit", lambda: self.admit(user=self.other), barrier)]
            for thread in threads:
                thread.start()
            self.join(threads)
        self.assertTrue(all(result[0] == "ok" for result in self.results.values()), self.results)
        self.assertEqual(self.totals(), [(0, 1), (1, 0), (0, 1), (1, 0)])

    def test_postgres_historical_bootstrap_preserves_existing_constraints(self):
        ordinary.GlobalTransactionTests.test_historical_bootstrap_preserves_evidence(self)
