"""Opt-in PostgreSQL races on the verified disposable cluster only."""
import threading
from unittest.mock import Mock, patch

from django.db import connections
from django.test import override_settings

from . import postgres_concurrency_tests as pg
from . import test_subscriptions as fixtures
from proposal_ai import ai_control as control, billing
from proposal_ai.models import AIRequest, BillingAccount, CheckoutAttempt, StripeEvent


@override_settings(**fixtures.CONFIG)
class BillingConcurrencyTests(pg.PostgreSQLCase):
    def setUp(self):
        super().setUp()
        self.account = BillingAccount.objects.create(user=self.user, stripe_customer_id="cus_owned")

    def race(self, action):
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker():
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                results.append(action())
            except Exception as error:
                errors.append(error)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        return results

    def test_last_monthly_place_has_one_winner_across_connections(self):
        # Existing successes are outside burst/day boundaries, in this month.
        fixtures.MonthlyAllowanceTests.history(self, 2, at=pg.MOMENT - pg.timedelta(days=1))

        def action():
            try:
                row = self.admit(control.Operation.PROPOSAL_GENERATION).request
                control.mark_dispatch(row)
                control.succeed(row)
                return 200
            except control.ControlError as error:
                return error.status

        results = self.race(action)
        self.assertEqual(results.count(200), 1, results)
        self.assertTrue(all(value in (200, 409, 429) for value in results), results)
        self.assertEqual(AIRequest.objects.filter(lifecycle=AIRequest.Lifecycle.SUCCEEDED).count(), 3)

    def api(self):
        api = Mock()
        api.v1.subscriptions.list.return_value = fixtures.page([])
        api.v1.prices.retrieve.return_value = fixtures.resource(fixtures.PRICE)
        api.v1.billing_portal.configurations.retrieve.return_value = fixtures.resource(fixtures.PORTAL)

        def session(params=None, options=None):
            attempt = CheckoutAttempt.objects.get(account=self.account, resolved=False)
            return fixtures.resource({"id": "cs_test_concurrent", "object": "checkout.session", "livemode": False,
                                      "mode": "subscription", "status": "open", "customer": "cus_owned",
                                      "metadata": {"proposalq_attempt": str(attempt.identity)},
                                      "url": "https://checkout.stripe.com/c/pay/test"})

        api.v1.checkout.sessions.create.side_effect = session
        api.v1.checkout.sessions.retrieve.side_effect = lambda *args: session()
        return api

    def test_simultaneous_checkouts_create_one_remote_session(self):
        api = self.api()
        with patch("proposal_ai.billing.client", return_value=api):
            results = self.race(lambda: billing.checkout(self.user))
        self.assertEqual(results, ["https://checkout.stripe.com/c/pay/test"] * 2)
        api.v1.checkout.sessions.create.assert_called_once()
        self.assertEqual(CheckoutAttempt.objects.count(), 1)

    def test_duplicate_webhooks_commit_one_receipt_and_one_sync(self):
        api = self.api()
        api.v1.subscriptions.list.return_value = fixtures.page([fixtures.subscription(self.account)])
        event = {"id": "evt_concurrent", "livemode": False, "type": "customer.subscription.updated",
                 "data": {"object": {"id": "sub_owned", "customer": "cus_owned", "livemode": False}}}
        with patch("proposal_ai.billing.client", return_value=api):
            self.race(lambda: billing.process_event(event))
        self.assertEqual(StripeEvent.objects.count(), 1)
        api.v1.subscriptions.list.assert_called_once()

    def test_out_of_order_events_fetch_state_after_each_account_lock(self):
        api = self.api()
        api.v1.subscriptions.list.side_effect = [fixtures.page([fixtures.subscription(self.account)]),
                                                fixtures.page([fixtures.subscription(self.account, status="canceled")])]

        def action():
            import uuid
            return billing.process_event({"id": "evt_" + uuid.uuid4().hex, "livemode": False,
                                          "type": "customer.subscription.updated", "data": {"object": {
                                              "id": "sub_owned", "customer": "cus_owned", "livemode": False, "status": "active"}}})

        with patch("proposal_ai.billing.client", return_value=api):
            self.race(action)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "canceled")
        self.assertEqual(StripeEvent.objects.count(), 2)
