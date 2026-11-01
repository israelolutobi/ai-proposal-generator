"""Explicit PostgreSQL integration matrix; never ordinary test discovery.

Use scripts/run_postgres_tests.py against a designated disposable cluster.
Synchronization events arrange tests only; production admission uses the DB.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection, connections, IntegrityError, transaction
from django.test import SimpleTestCase, TransactionTestCase

from proposal_ai import ai_control as control, services
from proposal_ai.models import AIRequest, JobPost, Proposal


O, I, L, Q, F = (AIRequest.Operation, AIRequest.Intent, AIRequest.Lifecycle,
                 AIRequest.Quota, AIRequest.Failure)
MOMENT = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


class PostgreSQLCase(TransactionTestCase):
    def setUp(self):
        self.assertEqual(os.environ.get("PROPOSALQ_POSTGRES_TESTS"), "1")
        self.assertEqual(connection.vendor, "postgresql")
        self.assertEqual(connection.settings_dict["HOST"], "127.0.0.1")
        self.assertEqual(connection.settings_dict["NAME"], "test_proposalq_task4b")
        with connection.cursor() as cursor:
            cursor.execute("SHOW data_directory")
            self.assertEqual(Path(cursor.fetchone()[0]).resolve(),
                             Path(os.environ["PROPOSALQ_TEST_PG_DATA_DIR"]).resolve())
            cursor.execute("SHOW transaction_isolation")
            self.assertEqual(cursor.fetchone()[0], "read committed")
        self.user = get_user_model().objects.create_user(username="pg-matrix-owner")
        self.clock = patch("proposal_ai.ai_control.now", return_value=MOMENT)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        config = patch("proposal_ai.services.check_configuration", return_value=None)
        config.start()
        self.addCleanup(config.stop)
        self.pids = {}
        self.results = {}

    def history(self, count, at, *, dispatched=False):
        AIRequest.objects.bulk_create([
            AIRequest(user=self.user, operation=O.PROFILE_SUMMARY, nonce=uuid.uuid4(),
                      intent=I.GENERATE, submitted_fingerprint="a" * 64,
                      effective_fingerprint="b" * 64, lifecycle=L.SUCCEEDED,
                      quota_state=Q.CONSUMED, quota_units=1, admitted_at=at,
                      dispatch_started_at=at if dispatched else None,
                      completed_at=at, lease_expires_at=at + control.LEASE)
            for _ in range(count)
        ])

    def admit(self, operation=O.PROFILE_SUMMARY, *, user=None, token=None, resource=None):
        user = user or self.user
        token = token or control.issue_nonce(user, operation, resource)
        return control.admit(user, operation, token, {"input": "synthetic"}, "synthetic", resource)

    def telemetry(self, operation=O.PROFILE_SUMMARY, **overrides):
        fields = services.request_telemetry(operation).scalar_fields()
        fields.update(response_model="gpt-5", input_tokens=11, completion_tokens=13,
                      total_tokens=24, provider_latency_ms=7, finish_reason="stop",
                      response_text_characters=17)
        fields.update(overrides)
        return services.AITelemetry(**fields)

    def provider(self, operation=O.PROFILE_SUMMARY, barrier=None):
        def fake():
            self.assertFalse(connections["default"].in_atomic_block)
            if barrier:
                barrier.wait(timeout=10)
            return services.AIServiceResult("Synthetic result", self.telemetry(operation))
        return Mock(side_effect=fake)

    def worker(self, name, function, barrier=None):
        def run():
            connections.close_all()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET statement_timeout='15s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    self.pids[name] = cursor.fetchone()[0]
                if barrier:
                    barrier.wait(timeout=10)
                self.results[name] = ("ok", function())
            except control.ControlError as error:
                self.results[name] = ("control", error)
            except BaseException as error:
                self.results[name] = ("error", type(error).__name__)
            finally:
                connections.close_all()
        return threading.Thread(target=run, name=name)

    def join(self, threads):
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=20)
                self.assertFalse(thread.is_alive(), "A PostgreSQL test worker did not stop.")
        self.assertTrue(all(value[0] != "error" for value in self.results.values()), self.results)
        self.assertEqual(len(set(self.pids.values())), len(self.pids))

    def wait_for_lock(self, waiter, holder, verb):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if waiter in self.pids and holder in self.pids:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT wait_event_type,pg_blocking_pids(pid),split_part(query,' ',1) "
                        "FROM pg_stat_activity WHERE pid=%s", [self.pids[waiter]],
                    )
                    row = cursor.fetchone()
                if row and row[0] == "Lock" and self.pids[holder] in row[1]:
                    self.assertEqual(row[2], verb)
                    return
            time.sleep(0.01)
        self.fail("Expected real PostgreSQL lock contention was not observed.")

    def waiting_admission(self, before=MOMENT, after=None, operation=O.PROFILE_SUMMARY):
        """A captures time, then really waits on B's uncommitted finalization."""
        after = after or before + timedelta(seconds=2)
        b_time = before + timedelta(seconds=1)
        a_captured, b_finalizing, release = (threading.Event() for _ in range(3))
        a_calls = 0

        def clock():
            nonlocal a_calls
            name = threading.current_thread().name
            if name == "A":
                a_calls += 1
                if a_calls == 1:
                    a_captured.set()
                    if not b_finalizing.wait(10):
                        raise AssertionError("B never reached finalization.")
                    return before
                return after
            if name == "B":
                return b_time
            return before - timedelta(seconds=1)

        token = control.issue_nonce(self.user, operation)
        provider = self.provider(operation)
        with patch("proposal_ai.ai_control.now", side_effect=clock):
            b = self.admit(operation).request

            def complete_b():
                control.call_provider(b, provider)

                def persist():
                    b_finalizing.set()
                    if not release.wait(10):
                        raise AssertionError("B finalization was not released.")
                control.succeed(b, persist)

            threads = [self.worker("A", lambda: self.admit(operation, token=token)),
                       self.worker("B", complete_b)]
            try:
                threads[0].start()
                self.assertTrue(a_captured.wait(10))
                threads[1].start()
                self.assertTrue(b_finalizing.wait(10))
                self.wait_for_lock("A", "B", "INSERT")
            finally:
                release.set()
                self.join(threads)
        self.assertEqual(self.results["B"][0], "ok", self.results)
        provider.assert_called_once()
        return self.results["A"]

    def race_admission(self, operations, *, same_nonce=False):
        barrier = threading.Barrier(len(operations))
        tokens = [control.issue_nonce(self.user, op) for op in operations]
        if same_nonce:
            tokens = [tokens[0]] * len(tokens)
        providers = [self.provider(op) for op in operations]

        def attempt(index):
            admitted = self.admit(operations[index], token=tokens[index])
            control.call_provider(admitted.request, providers[index])
            # Keep the winner in flight until all other connections finish.
            return admitted.request
        threads = [self.worker(str(i), lambda i=i: attempt(i), barrier)
                   for i in range(len(operations))]
        for thread in threads:
            thread.start()
        self.join(threads)
        winners = [value[1] for value in self.results.values() if value[0] == "ok"]
        self.assertEqual(len(winners), 1, self.results)
        for kind, value in self.results.values():
            if kind == "control":
                self.assertEqual(value.status, 409)
        self.assertEqual(sum(p.call_count for p in providers), 1)
        self.assertEqual(AIRequest.objects.filter(lifecycle__in=control.ACTIVE).count(), 1)
        return winners[0], tokens

    def dispatched_row(self, operation=O.PROFILE_SUMMARY, resource=None):
        row = self.admit(operation, resource=resource).request
        provider = self.provider(operation)
        control.call_provider(row, provider)
        provider.assert_called_once()
        return row, provider

    def expired_row(self, operation=O.PROFILE_SUMMARY, resource=None):
        row, provider = self.dispatched_row(operation, resource)
        with patch("proposal_ai.ai_control.now", return_value=MOMENT + control.LEASE + timedelta(seconds=1)):
            control.recover_stale(self.user)
        return row, provider


class AdmissionTimeTests(PostgreSQLCase):
    def test_competitor_dispatches_during_database_wait_before_insert(self):
        # An advisory lock is a test-only database synchronization gate. It lets
        # B dispatch outside a transaction while A waits before its first write.
        # The original reproduction separately verifies the active-index wait.
        self.history(2, MOMENT - timedelta(minutes=1), dispatched=True)
        gate = 418301
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid(),pg_advisory_lock(%s)", [gate])
            self.pids["holder"] = cursor.fetchone()[0]
        provider_a = self.provider()
        first_time = True

        def clock():
            nonlocal first_time
            if threading.current_thread().name == "A":
                if first_time:
                    first_time = False
                    return MOMENT
                return MOMENT + timedelta(seconds=2)
            return MOMENT + timedelta(seconds=1)

        def attempt():
            gated = False

            def wait_before_write(execute, sql, params, many, context):
                nonlocal gated
                if sql.startswith("UPDATE") and not gated:
                    gated = True
                    execute("SELECT pg_advisory_xact_lock(%s)", [gate], False, context)
                return execute(sql, params, many, context)
            with connections["default"].execute_wrapper(wait_before_write):
                row = self.admit().request
            control.call_provider(row, provider_a)
        with patch("proposal_ai.ai_control.now", side_effect=clock):
            thread = self.worker("A", attempt)
            try:
                thread.start()
                self.wait_for_lock("A", "holder", "SELECT")
                b, provider_b = self.dispatched_row()
                control.succeed(b)
                provider_b.assert_called_once()
            finally:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [gate])
                self.join([thread])
        self.assertEqual(self.results["A"][0], "control")
        self.assertEqual(self.results["A"][1].status, 429)
        provider_a.assert_not_called()
        self.assertEqual(AIRequest.objects.filter(dispatch_started_at__isnull=False).count(), 3)

    def test_completed_competitor_counts_after_pre_wait_time(self):
        self.history(1, MOMENT - timedelta(minutes=2), dispatched=True)
        self.history(1, MOMENT - timedelta(minutes=1), dispatched=True)
        kind, error = self.waiting_admission()
        self.assertEqual(kind, "control")
        self.assertEqual(error.status, 429)
        self.assertEqual(AIRequest.objects.filter(dispatch_started_at__isnull=False).count(), 3)
        self.assertEqual(AIRequest.objects.filter(lifecycle__in=control.ACTIVE).count(), 0)

    def test_burst_retry_after_uses_refreshed_time(self):
        oldest = MOMENT - timedelta(minutes=2)
        self.history(1, oldest, dispatched=True)
        self.history(1, MOMENT - timedelta(minutes=1), dispatched=True)
        kind, error = self.waiting_admission()
        self.assertEqual(kind, "control")
        self.assertEqual(error.status, 429)
        self.assertEqual(error.retry_after, 478)

    def test_entry_that_expires_during_wait_is_excluded(self):
        self.history(1, MOMENT - timedelta(minutes=10) + timedelta(seconds=1), dispatched=True)
        self.history(1, MOMENT - timedelta(minutes=1), dispatched=True)
        kind, admitted = self.waiting_admission()
        self.assertEqual(kind, "ok")
        self.assertEqual(admitted.request.admitted_at, MOMENT + timedelta(seconds=2))
        self.assertEqual(control.allowance(self.user, MOMENT)[1], 4)

    def test_daily_boundary_assigns_admission_to_new_utc_day(self):
        before = datetime(2026, 10, 8, 23, 59, 59, tzinfo=timezone.utc)
        after = before + timedelta(seconds=2)
        self.history(24, before - timedelta(hours=1))
        kind, admitted = self.waiting_admission(before, after)
        self.assertEqual(kind, "ok")
        row = AIRequest.objects.get(pk=admitted.request.pk)
        self.assertEqual(row.admitted_at, after)
        self.assertEqual(control.allowance(self.user, before)[1], 25)
        self.assertEqual(control.allowance(self.user, after)[1:], (1, 26))
        self.assertEqual(AIRequest.objects.aggregate(total=control.Sum("quota_units"))["total"], 26)

    def test_monday_boundary_assigns_admission_to_new_utc_week(self):
        before = datetime(2026, 10, 11, 23, 59, 59, tzinfo=timezone.utc)
        after = before + timedelta(seconds=2)
        self.history(99, before - timedelta(days=2))
        kind, admitted = self.waiting_admission(before, after)
        self.assertEqual(kind, "ok")
        self.assertEqual(admitted.request.admitted_at, after)
        self.assertEqual(control.allowance(self.user, before)[2], 100)
        self.assertEqual(control.allowance(self.user, after)[1:], (1, 1))
        self.assertEqual(AIRequest.objects.count(), 101)

    def test_full_lease_starts_after_contention(self):
        after = MOMENT + timedelta(minutes=3)
        kind, admitted = self.waiting_admission(after=after)
        self.assertEqual(kind, "ok")
        row = AIRequest.objects.get(pk=admitted.request.pk)
        self.assertEqual(row.admitted_at, after)
        self.assertEqual(row.lease_expires_at, after + control.LEASE)
        self.assertEqual(admitted.request.lease_expires_at, row.lease_expires_at)

    def test_wait_longer_than_provisional_lease_gets_full_new_lease(self):
        after = MOMENT + control.LEASE + timedelta(minutes=1)
        kind, admitted = self.waiting_admission(after=after)
        self.assertEqual(kind, "ok")
        self.assertEqual(admitted.request.lease_expires_at, after + control.LEASE)

    def test_daily_quota_retry_uses_refreshed_time(self):
        self.history(24, MOMENT - timedelta(hours=1))
        kind, error = self.waiting_admission()
        self.assertEqual(kind, "control")
        self.assertEqual(error.status, 429)
        reset = datetime(2026, 10, 8, tzinfo=timezone.utc)
        self.assertEqual(error.metadata["next_reset"], reset.isoformat())
        self.assertEqual(error.retry_after, 43198)
        self.assertEqual(error.metadata["daily_remaining"], 0)
        self.assertEqual(AIRequest.objects.count(), 25)

    def test_weekly_quota_retry_uses_refreshed_time(self):
        self.history(99, MOMENT - timedelta(days=1))
        kind, error = self.waiting_admission()
        self.assertEqual(kind, "control")
        self.assertEqual(error.status, 429)
        reset = datetime(2026, 10, 12, tzinfo=timezone.utc)
        self.assertEqual(error.metadata["next_reset"], reset.isoformat())
        self.assertEqual(error.retry_after, 388798)
        self.assertEqual(error.metadata["weekly_remaining"], 0)
        self.assertEqual(AIRequest.objects.count(), 100)


class AccountConcurrencyTests(PostgreSQLCase):
    def test_same_nonce_dispatches_once_and_replays_once(self):
        row, tokens = self.race_admission([O.PROFILE_SUMMARY] * 2, same_nonce=True)
        self.assertEqual(AIRequest.objects.count(), 1)
        control.succeed(row)
        replay = self.admit(token=tokens[0])
        self.assertTrue(replay.replay)
        self.assertEqual(replay.request.pk, row.pk)
        self.assertEqual(control.allowance(self.user, MOMENT)[1:], (1, 1))
        with self.assertRaises(control.ControlError) as rejected:
            control.admit(self.user, O.PROFILE_SUMMARY, tokens[0], {"input": "changed"}, "synthetic")
        self.assertEqual(rejected.exception.status, 409)

    def test_different_nonces_share_one_active_slot(self):
        row, _ = self.race_admission([O.PROFILE_SUMMARY] * 2)
        self.assertEqual(AIRequest.objects.count(), 1)
        control.succeed(row)
        self.assertEqual(control.allowance(self.user, MOMENT)[1:], (1, 1))

    def test_different_operations_share_one_account_slot(self):
        row, _ = self.race_admission([O.PROFILE_SUMMARY, O.JOB_EXTRACTION, O.PROPOSAL_GENERATION])
        self.assertEqual(AIRequest.objects.count(), 1)
        self.assertEqual(row.quota_units, control.CREDITS[row.operation])

    def test_different_users_execute_provider_callbacks_concurrently(self):
        other = get_user_model().objects.create_user(username="pg-independent-owner")
        barrier = threading.Barrier(2)
        providers = [self.provider(barrier=barrier) for _ in range(2)]

        def attempt(user, provider):
            row = self.admit(user=user).request
            control.call_provider(row, provider)
            control.succeed(row)
        threads = [self.worker(str(i), lambda i=i: attempt([self.user, other][i], providers[i]))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        self.join(threads)
        self.assertTrue(all(result[0] == "ok" for result in self.results.values()), self.results)
        self.assertEqual(AIRequest.objects.filter(lifecycle=L.SUCCEEDED).count(), 2)
        self.assertTrue(all(provider.call_count == 1 for provider in providers))

    def test_final_daily_credit_simultaneous_race(self):
        self.history(24, MOMENT - timedelta(hours=1))
        row, _ = self.race_admission([O.PROFILE_SUMMARY] * 2)
        control.succeed(row)
        self.assertEqual(control.allowance(self.user, MOMENT)[1], 25)

    def test_final_weekly_credit_simultaneous_race(self):
        self.history(99, MOMENT - timedelta(days=1))
        row, _ = self.race_admission([O.PROFILE_SUMMARY] * 2)
        control.succeed(row)
        self.assertEqual(control.allowance(self.user, MOMENT)[2], 100)

    def test_partial_unique_constraint_rejects_reserved_and_inflight_conflicts(self):
        first = self.admit().request
        for state in (L.RESERVED, L.IN_FLIGHT):
            with self.subTest(state=state), self.assertRaises(IntegrityError), transaction.atomic():
                AIRequest.objects.create(
                    user=self.user, operation=O.JOB_EXTRACTION, nonce=uuid.uuid4(), intent=I.GENERATE,
                    submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
                    lifecycle=state, quota_state=Q.RESERVED, quota_units=2,
                    admitted_at=MOMENT, lease_expires_at=MOMENT + control.LEASE,
                )
        self.assertEqual(AIRequest.objects.count(), 1)
        self.assertEqual(AIRequest.objects.get().pk, first.pk)

    def test_user_nonce_unique_constraint_survives_terminal_state(self):
        first, _ = self.dispatched_row()
        control.succeed(first)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AIRequest.objects.create(
                user=self.user, operation=O.PROFILE_SUMMARY, nonce=first.nonce, intent=I.GENERATE,
                submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
                quota_units=1, admitted_at=MOMENT, lease_expires_at=MOMENT + control.LEASE,
            )

    def test_operation_credit_constraint_rejects_wrong_weight(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AIRequest.objects.create(
                user=self.user, operation=O.PROFILE_SUMMARY, nonce=uuid.uuid4(), intent=I.GENERATE,
                submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
                quota_units=3, admitted_at=MOMENT, lease_expires_at=MOMENT + control.LEASE,
            )


class RecoveryAndFencingTests(PostgreSQLCase):
    def recover_race(self, dispatched):
        past = MOMENT - control.LEASE - timedelta(seconds=1)
        row = AIRequest.objects.create(
            user=self.user, operation=O.PROFILE_SUMMARY, nonce=uuid.uuid4(), intent=I.GENERATE,
            submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
            lifecycle=L.IN_FLIGHT if dispatched else L.RESERVED, quota_state=Q.RESERVED,
            quota_units=1, admitted_at=past, dispatch_started_at=past if dispatched else None,
            lease_expires_at=past + control.LEASE,
        )
        barrier = threading.Barrier(2)
        threads = [self.worker(str(i), lambda: control.recover_stale(self.user), barrier)
                   for i in range(2)]
        for thread in threads:
            thread.start()
        self.join(threads)
        self.assertTrue(all(result[0] == "ok" for result in self.results.values()))
        row.refresh_from_db()
        self.assertEqual(row.lifecycle, L.UNCERTAIN if dispatched else L.FAILED)
        self.assertEqual(row.quota_state, Q.CONSUMED if dispatched else Q.RELEASED)
        self.assertEqual(row.completed_at, MOMENT)
        self.assertEqual(row.failure_category, F.STALE)
        self.assertEqual(control.allowance(self.user, MOMENT)[1], int(dispatched))
        self.results.clear()
        self.pids.clear()
        self.race_admission([O.PROFILE_SUMMARY] * 2)
        self.assertEqual(control.allowance(self.user, MOMENT)[1], int(dispatched) + 1)

    def test_simultaneous_undispatched_recovery_releases_once(self):
        self.recover_race(False)

    def test_simultaneous_dispatched_recovery_consumes_once(self):
        self.recover_race(True)

    def test_recovery_and_fresh_admission_race(self):
        row, _ = self.dispatched_row()
        expired = MOMENT + control.LEASE + timedelta(seconds=1)
        barrier = threading.Barrier(2)
        with patch("proposal_ai.ai_control.now", return_value=expired):
            threads = [self.worker("recover", lambda: control.recover_stale(self.user), barrier),
                       self.worker("admit", lambda: self.admit(), barrier)]
            for thread in threads:
                thread.start()
            self.join(threads)
        self.assertTrue(all(result[0] == "ok" for result in self.results.values()), self.results)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.UNCERTAIN, Q.CONSUMED))
        self.assertEqual(AIRequest.objects.filter(lifecycle__in=control.ACTIVE).count(), 1)

    def test_late_telemetry_cannot_overwrite_terminal_request(self):
        row, _ = self.expired_row()
        with self.assertRaises(control.ControlError) as rejected:
            control.record_telemetry(row, self.telemetry(input_tokens=999))
        self.assertEqual(rejected.exception.status, 409)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state, row.input_tokens), (L.UNCERTAIN, Q.CONSUMED, 11))

    def test_late_success_cannot_finalize_terminal_request(self):
        row, _ = self.expired_row()
        with self.assertRaises(control.ControlError):
            control.succeed(row)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.UNCERTAIN, Q.CONSUMED))

    def test_late_extraction_cannot_persist_job(self):
        row, _ = self.expired_row(O.JOB_EXTRACTION)
        persist = Mock(side_effect=lambda: JobPost.objects.create(user=self.user, job_description="Synthetic"))
        with self.assertRaises(control.ControlError):
            control.succeed(row, persist)
        persist.assert_not_called()
        self.assertEqual(JobPost.objects.count(), 0)

    def test_late_proposal_cannot_persist_or_confirm_job(self):
        job = JobPost.objects.create(user=self.user, job_description="Synthetic", confirmed_by_user=False)
        row, _ = self.expired_row(O.PROPOSAL_GENERATION, job.pk)

        def persist():
            job.confirmed_by_user = True
            job.save()
            return Proposal.objects.create(user=self.user, job_post=job, final_text="Synthetic")
        callback = Mock(side_effect=persist)
        with self.assertRaises(control.ControlError):
            control.succeed(row, callback)
        callback.assert_not_called()
        job.refresh_from_db()
        self.assertFalse(job.confirmed_by_user)
        self.assertEqual(Proposal.objects.count(), 0)

    def telemetry_stale_race(self, telemetry_first):
        row, _ = self.dispatched_row()
        staged, release = threading.Event(), threading.Event()
        before = MOMENT + control.LEASE - timedelta(seconds=1)
        after = MOMENT + control.LEASE + timedelta(seconds=1)

        def clock():
            return before if threading.current_thread().name == "telemetry" else after

        def telemetry():
            control.record_telemetry(row, self.telemetry(input_tokens=22))

        def stale():
            control.recover_stale(self.user)

        holder_name = "telemetry" if telemetry_first else "stale"
        waiter_name = "stale" if telemetry_first else "telemetry"
        functions = {"telemetry": telemetry, "stale": stale}

        def hold():
            with transaction.atomic():
                functions[holder_name]()
                staged.set()
                if not release.wait(10):
                    raise AssertionError("Ledger transaction was not released.")

        with patch("proposal_ai.ai_control.now", side_effect=clock):
            threads = [self.worker(holder_name, hold), self.worker(waiter_name, functions[waiter_name])]
            try:
                threads[0].start()
                self.assertTrue(staged.wait(10))
                threads[1].start()
                self.wait_for_lock(waiter_name, holder_name, "UPDATE")
            finally:
                release.set()
                self.join(threads)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.UNCERTAIN, Q.CONSUMED))
        self.assertEqual(row.input_tokens, 22 if telemetry_first else 11)
        if not telemetry_first:
            self.assertEqual(self.results["telemetry"][0], "control")
            self.assertEqual(self.results["telemetry"][1].status, 409)

    def test_stale_transition_fences_waiting_telemetry_update(self):
        self.telemetry_stale_race(False)

    def test_accepted_telemetry_survives_following_stale_transition(self):
        self.telemetry_stale_race(True)


class PersistenceAndFailureTests(PostgreSQLCase):
    def inspect_from_separate_connection(self, row):
        thread = self.worker("observer", lambda: AIRequest.objects.get(pk=row.pk))
        thread.start()
        self.join([thread])
        result = self.results["observer"][1]
        self.assertEqual(result.lifecycle, L.IN_FLIGHT)
        self.assertEqual((result.input_tokens, result.provider_latency_ms), (11, 7))

    def test_extraction_success_commits_job_and_ledger_together(self):
        token = control.issue_nonce(self.user, O.JOB_EXTRACTION)
        row = self.admit(O.JOB_EXTRACTION, token=token).request
        provider = self.provider(O.JOB_EXTRACTION)
        control.call_provider(row, provider)
        self.inspect_from_separate_connection(row)

        def persist():
            self.assertTrue(connection.in_atomic_block)
            return JobPost.objects.create(user=self.user, job_title="Synthetic", job_description="Synthetic")
        job = control.succeed(row, persist)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state, row.job_post_id), (L.SUCCEEDED, Q.CONSUMED, job.pk))
        self.assertEqual(row.quota_units, 2)
        replay = self.admit(O.JOB_EXTRACTION, token=token)
        self.assertTrue(replay.replay)
        self.assertEqual(replay.request.job_post_id, job.pk)
        self.assertEqual(JobPost.objects.count(), 1)
        self.assertEqual(AIRequest.objects.count(), 1)
        provider.assert_called_once()

    def test_proposal_success_commits_job_proposal_and_ledger_together(self):
        job = JobPost.objects.create(user=self.user, job_description="Original", confirmed_by_user=False)
        token = control.issue_nonce(self.user, O.PROPOSAL_GENERATION, job.pk)
        row = self.admit(O.PROPOSAL_GENERATION, token=token, resource=job.pk).request
        provider = self.provider(O.PROPOSAL_GENERATION)
        control.call_provider(row, provider)
        self.inspect_from_separate_connection(row)

        def persist():
            self.assertTrue(connection.in_atomic_block)
            job.job_description = "Confirmed"
            job.confirmed_by_user = True
            job.save()
            return Proposal.objects.create(user=self.user, job_post=job, final_text="Synthetic")
        proposal = control.succeed(row, persist)
        row.refresh_from_db()
        job.refresh_from_db()
        self.assertTrue(job.confirmed_by_user)
        self.assertEqual(job.job_description, "Confirmed")
        self.assertEqual((row.lifecycle, row.quota_state, row.proposal_id), (L.SUCCEEDED, Q.CONSUMED, proposal.pk))
        self.assertEqual(row.quota_units, 3)
        replay = self.admit(O.PROPOSAL_GENERATION, token=token, resource=job.pk)
        self.assertTrue(replay.replay)
        self.assertEqual(replay.request.proposal_id, proposal.pk)
        self.assertEqual(Proposal.objects.count(), 1)
        self.assertEqual(AIRequest.objects.count(), 1)
        provider.assert_called_once()

    def test_extraction_persistence_failure_rolls_back_job_preserves_telemetry(self):
        row, provider = self.dispatched_row(O.JOB_EXTRACTION)

        def fail_after_write():
            JobPost.objects.create(user=self.user, job_description="Synthetic")
            raise RuntimeError("Synthetic persistence failure")
        with self.assertRaises(control.ControlError) as rejected:
            control.succeed(row, fail_after_write)
        self.assertEqual(rejected.exception.status, 503)
        self.assertEqual(JobPost.objects.count(), 0)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state, row.failure_category), (L.FAILED, Q.CONSUMED, F.PERSISTENCE))
        self.assertEqual((row.input_tokens, row.total_tokens, row.provider_latency_ms), (11, 24, 7))
        provider.assert_called_once()

    def test_proposal_persistence_failure_rolls_back_every_application_write(self):
        job = JobPost.objects.create(user=self.user, job_description="Original", confirmed_by_user=False)
        row, provider = self.dispatched_row(O.PROPOSAL_GENERATION, job.pk)

        def fail_after_writes():
            job.job_description = "Changed"
            job.confirmed_by_user = True
            job.save()
            Proposal.objects.create(user=self.user, job_post=job, final_text="Synthetic")
            raise RuntimeError("Synthetic persistence failure")
        with self.assertRaises(control.ControlError):
            control.succeed(row, fail_after_writes)
        job.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(job.job_description, "Original")
        self.assertFalse(job.confirmed_by_user)
        self.assertEqual(Proposal.objects.count(), 0)
        self.assertEqual((row.lifecycle, row.quota_state, row.input_tokens), (L.FAILED, Q.CONSUMED, 11))
        provider.assert_called_once()

    def test_database_failure_before_dispatch_fails_closed(self):
        provider = self.provider()

        def database_fault(execute, sql, params, many, context):
            if sql.startswith("UPDATE"):
                return execute("SELECT 1 / 0", None, False, context)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(database_fault), self.assertRaises(control.ControlError) as rejected:
            row = self.admit().request
            control.call_provider(row, provider)
        self.assertEqual(rejected.exception.status, 503)
        provider.assert_not_called()
        self.assertEqual(AIRequest.objects.count(), 0)

    def test_unsupported_actual_postgresql_isolation_fails_before_dispatch(self):
        provider = self.provider()
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        try:
            with self.assertRaises(control.ControlError) as rejected:
                row = self.admit().request
                control.call_provider(row, provider)
            self.assertEqual(rejected.exception.status, 503)
            provider.assert_not_called()
            self.assertEqual(AIRequest.objects.count(), 0)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ COMMITTED")

    def test_definitive_authentication_rejection_releases_but_counts_dispatch(self):
        row = self.admit().request
        provider = Mock(side_effect=services.AIAuthenticationError())
        with self.assertRaises(services.AIAuthenticationError):
            control.call_provider(row, provider)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.FAILED, Q.RELEASED))
        self.assertIsNotNone(row.dispatch_started_at)
        self.assertEqual(control.allowance(self.user, MOMENT)[1:], (0, 0))
        provider.assert_called_once()

    def test_timeout_consumes_with_unknown_usage(self):
        row = self.admit().request
        provider = Mock(side_effect=services.AITimeoutError())
        with self.assertRaises(services.AITimeoutError):
            control.call_provider(row, provider)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.FAILED, Q.CONSUMED))
        self.assertIsNone(row.total_tokens)
        self.assertEqual(control.allowance(self.user, MOMENT)[1:], (1, 1))
        provider.assert_called_once()


class PostgreSQLLauncherTests(SimpleTestCase):
    def refuse(self, values, message):
        env = os.environ.copy()
        for key in ("PROPOSALQ_POSTGRES_TESTS", "PROPOSALQ_POSTGRES_TEST_DATABASE_URL", "PROPOSALQ_TEST_PG_DATA_DIR"):
            env.pop(key, None)
        env.update(values)
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_postgres_tests.py"
        result = subprocess.run([sys.executable, "-B", str(script)], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn(message, result.stderr)

    def test_explicit_opt_in_is_required(self):
        self.refuse({}, "PROPOSALQ_POSTGRES_TESTS=1")

    def test_regular_database_url_is_never_a_fallback(self):
        self.refuse({"PROPOSALQ_POSTGRES_TESTS": "1"}, "PROPOSALQ_POSTGRES_TEST_DATABASE_URL is required")

    def test_cluster_identity_is_required_before_connecting(self):
        self.refuse({"PROPOSALQ_POSTGRES_TESTS": "1", "PROPOSALQ_POSTGRES_TEST_DATABASE_URL": "postgresql://tester@127.0.0.1:5432/proposalq_task4b"},
                    "PROPOSALQ_TEST_PG_DATA_DIR must identify")

    def test_non_loopback_database_is_rejected_before_connecting(self):
        self.refuse({"PROPOSALQ_POSTGRES_TESTS": "1", "PROPOSALQ_POSTGRES_TEST_DATABASE_URL": "postgresql://tester@invalid.example:5432/proposalq_task4b",
                     "PROPOSALQ_TEST_PG_DATA_DIR": "disposable"}, "Use an explicit loopback port")
