"""Opt-in shared-account concurrency tests on the designated disposable cluster."""
import secrets
import threading

from django.contrib.auth import get_user_model
from django.db import IntegrityError

from mysite.postgres_concurrency_tests import PostgreSQLCase
from proposal_ai.account_security import consume
from proposal_ai.forms import RegistrationForm
from proposal_ai.models import AccountRateLimit, RegisteredEmail


class PostgreSQLAccountTests(PostgreSQLCase):
    def concurrent(self, functions):
        barrier = threading.Barrier(len(functions))
        threads = [self.worker(str(index), function, barrier) for index, function in enumerate(functions)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(set(self.pids.values())), len(functions))
        for result, value in self.results.values():
            self.assertEqual(result, "ok", value)
        return [value for _, value in self.results.values()]

    def test_concurrent_empty_bucket_only_admits_limit_across_connections(self):
        results = self.concurrent([lambda: consume("parallel", "same", 2, 600)] * 4)
        self.assertEqual(results.count(None), 2)
        self.assertEqual(AccountRateLimit.objects.get().attempts, 2)

    def test_case_insensitive_email_race_creates_exactly_one_public_account(self):
        validated = threading.Barrier(2)
        password = secrets.token_urlsafe(32)

        def signup(username, email):
            form = RegistrationForm({"username": username, "email": email,
                                     "password1": password, "password2": password})
            self.assertTrue(form.is_valid(), form.errors)
            validated.wait(timeout=10)
            try:
                form.save()
                return "created"
            except IntegrityError:
                return "duplicate"

        results = self.concurrent([lambda: signup("parallel-one", "Mixed@Example.test"),
                                   lambda: signup("parallel-two", "mixed@example.test")])
        self.assertCountEqual(results, ["created", "duplicate"])
        self.assertEqual(RegisteredEmail.objects.count(), 1)
        self.assertEqual(get_user_model().objects.filter(username__startswith="parallel-").count(), 1)
