"""Opt-in PostgreSQL reproduction; excluded from ordinary test*.py discovery.

Run this module explicitly with PROPOSALQ_POSTGRES_TESTS=1, DATABASE_URL
pointing to an isolated task cluster, and PROPOSALQ_TEST_PG_DATA_DIR naming
that cluster's data directory. Never use a shared or production database.
"""
import json
import os
from pathlib import Path
import threading
import time
import unittest
import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from mysite.ai_test_helpers import service_result
from proposal_ai import ai_control as control
from proposal_ai.models import AIRequest


@unittest.skipUnless(
    os.environ.get("PROPOSALQ_POSTGRES_TESTS") == "1",
    "Requires an explicitly enabled disposable PostgreSQL cluster.",
)
class CapturedTimeAdmissionTests(TransactionTestCase):
    """Real separate connections and an observed PostgreSQL transaction wait."""

    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.assertEqual(connection.settings_dict["HOST"], "127.0.0.1")
        self.assertTrue(connection.settings_dict["NAME"].startswith("test_proposalq_task4b"))
        expected = os.environ.get("PROPOSALQ_TEST_PG_DATA_DIR")
        self.assertTrue(expected, "An isolated cluster identity is required.")
        with connection.cursor() as cursor:
            cursor.execute("SHOW data_directory")
            self.assertEqual(Path(cursor.fetchone()[0]).resolve(), Path(expected).resolve())
            cursor.execute("SHOW transaction_isolation")
            self.assertEqual(cursor.fetchone()[0], "read committed")
        self.user = get_user_model().objects.create_user(username="captured-time-owner")

    def test_waiting_admission_counts_dispatch_completed_after_captured_time(self):
        operation = AIRequest.Operation.PROFILE_SUMMARY
        captured = timezone.now()
        dispatched = captured + timedelta(seconds=1)
        resumed = captured + timedelta(seconds=2)
        maximum, window = control.BURSTS[operation]
        self.assertEqual(maximum, 3)
        for minutes in (2, 1):
            prior = captured - timedelta(minutes=minutes)
            AIRequest.objects.create(
                user=self.user, operation=operation, nonce=uuid.uuid4(),
                intent=AIRequest.Intent.GENERATE,
                submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
                lifecycle=AIRequest.Lifecycle.SUCCEEDED,
                quota_state=AIRequest.Quota.CONSUMED, quota_units=1,
                admitted_at=prior, dispatch_started_at=prior,
                completed_at=prior, lease_expires_at=prior + control.LEASE,
            )
        tokens = {name: control.issue_nonce(self.user, operation) for name in ("A", "B")}
        a_captured = threading.Event()
        b_finalizing = threading.Event()
        release_b = threading.Event()
        backend_pids = {}
        outcomes = {}
        a_clock_calls = 0

        def clock():
            nonlocal a_clock_calls
            name = threading.current_thread().name
            if name == "task4b-A":
                a_clock_calls += 1
                if a_clock_calls == 1:
                    a_captured.set()
                    if not b_finalizing.wait(10):
                        raise AssertionError("B did not reach its persistence transaction.")
                    return captured
                return resumed
            if name == "task4b-B":
                return dispatched
            return captured - timedelta(seconds=1)

        def fake_provider():
            if connections["default"].in_atomic_block:
                raise AssertionError("Provider callback ran inside a transaction.")
            return service_result("Synthetic summary")

        providers = {name: Mock(side_effect=fake_provider) for name in ("A", "B")}

        def connect(name):
            connections.close_all()
            user = get_user_model().objects.get(pk=self.user.pk)
            with connections["default"].cursor() as cursor:
                cursor.execute("SET statement_timeout = '15s'")
                cursor.execute("SELECT pg_backend_pid()")
                backend_pids[name] = cursor.fetchone()[0]
            return user

        def attempt_a():
            try:
                user = connect("A")
                admission = control.admit(user, operation, tokens["A"], {"input": "synthetic"}, "synthetic")
                control.call_provider(admission.request, providers["A"])
                control.succeed(admission.request)
                outcomes["A"] = {"status": 200}
            except control.ControlError as error:
                outcomes["A"] = {"status": error.status}
            except BaseException as error:
                outcomes["A"] = {"error_type": type(error).__name__}
            finally:
                connections.close_all()

        def complete_b(row):
            try:
                connect("B")
                control.call_provider(row, providers["B"])

                def persist():
                    # succeed() has already updated B to terminal, but that write
                    # is uncommitted. A's partial-unique INSERT must wait for it.
                    b_finalizing.set()
                    if not release_b.wait(10):
                        raise AssertionError("B finalization was not released.")

                control.succeed(row, persist=persist)
                outcomes["B"] = {"status": 200}
            except BaseException as error:
                outcomes["B"] = {"error_type": type(error).__name__}
            finally:
                connections.close_all()

        blocking_observed = False
        with patch("proposal_ai.ai_control.now", side_effect=clock), patch(
            "proposal_ai.services.check_configuration", return_value=None,
        ):
            b = control.admit(self.user, operation, tokens["B"], {"input": "synthetic"}, "synthetic")
            threads = [threading.Thread(target=attempt_a, name="task4b-A"),
                       threading.Thread(target=complete_b, args=(b.request,), name="task4b-B")]
            try:
                threads[0].start()
                self.assertTrue(a_captured.wait(10))
                threads[1].start()
                self.assertTrue(b_finalizing.wait(10))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT wait_event_type, pg_blocking_pids(pid), "
                            "split_part(query, ' ', 1) FROM pg_stat_activity WHERE pid=%s",
                            [backend_pids["A"]],
                        )
                        activity = cursor.fetchone()
                    if activity and activity[0] == "Lock" and backend_pids["B"] in activity[1]:
                        self.assertEqual(activity[2], "INSERT")
                        blocking_observed = True
                        break
                    time.sleep(0.02)
            finally:
                release_b.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(timeout=20)
                        self.assertFalse(thread.is_alive(), "A database worker did not stop.")

        self.assertTrue(blocking_observed, "No real PostgreSQL unique-index wait was observed.")
        self.assertEqual(len(set(backend_pids.values())), 2)
        self.assertEqual(outcomes.get("B"), {"status": 200}, outcomes)
        self.assertEqual(providers["B"].call_count, 1)
        attempts = AIRequest.objects.filter(
            user=self.user, operation=operation,
            dispatch_started_at__gt=resumed - window,
            dispatch_started_at__lte=resumed,
        ).count()
        evidence = {
            "postgresql_unique_insert_wait_observed": blocking_observed,
            "separate_worker_connections": len(set(backend_pids.values())),
            "captured_at": captured.isoformat(), "b_dispatched_at": dispatched.isoformat(),
            "a_resumed_at": resumed.isoformat(), "outcomes": outcomes,
            "a_provider_calls": providers["A"].call_count,
            "b_provider_calls": providers["B"].call_count,
            "actual_dispatched_attempts_in_window": attempts,
            "configured_burst_maximum": maximum,
        }
        print("TASK4B_RACE_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
        self.assertEqual(outcomes.get("A"), {"status": 429}, evidence)
        self.assertEqual(providers["A"].call_count, 0)
        self.assertEqual(attempts, maximum)
