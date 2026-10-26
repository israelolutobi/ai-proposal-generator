"""Subscription tests use real Stripe resource shapes and signed, fake events.

All provider calls are mocked. The ordinary runner blocks networking as a backstop.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone as dt_timezone
import hashlib
import hmac
import io
import json
import time
from types import SimpleNamespace
import uuid
from unittest.mock import Mock, patch

import stripe
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command, CommandError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from mysite.billing_configuration import API_VERSION, billing_configuration
from proposal_ai import ai_control as control, billing, subscriptions
from proposal_ai.models import AIRequest, BillingAccount, CheckoutAttempt, StripeEvent


NOW = datetime(2026, 10, 15, 12, tzinfo=dt_timezone.utc)
CONFIG = {
    "BILLING_ENABLED": True, "STRIPE_SECRET_KEY": "sk_test_synthetic_only",
    "STRIPE_WEBHOOK_SECRET": "whsec_synthetic_only", "STRIPE_PRO_PRICE_ID": "price_pro",
    "STRIPE_PORTAL_CONFIGURATION_ID": "bpc_test", "STRIPE_API_VERSION": API_VERSION,
    "BILLING_BASE_URL": "https://proposalq.example", "ALLOWED_HOSTS": ["testserver"],
    "STORAGES": {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                 "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
}
PRICE = {"id": "price_pro", "object": "price", "livemode": False, "active": True,
         "currency": "gbp", "unit_amount": 999, "billing_scheme": "per_unit", "tax_behavior": "inclusive",
         "recurring": {"interval": "month", "interval_count": 1, "usage_type": "licensed"}}
PORTAL = {"id": "bpc_test", "object": "billing_portal.configuration", "livemode": False, "active": True,
          "features": {"subscription_cancel": {"enabled": True, "mode": "at_period_end"},
                       "subscription_update": {"enabled": False}, "payment_method_update": {"enabled": True},
                       "invoice_history": {"enabled": True}}}
O, L, Q, F = AIRequest.Operation, AIRequest.Lifecycle, AIRequest.Quota, AIRequest.Failure


def resource(values):
    return stripe.StripeObject.construct_from(deepcopy(values), "sk_test_synthetic_only")


def page(rows):
    return SimpleNamespace(auto_paging_iter=lambda: iter(resource(row) for row in rows))


def subscription(account, **changes):
    result = {"id": "sub_owned", "object": "subscription", "livemode": False, "created": 100,
              "customer": account.stripe_customer_id, "status": "active", "cancel_at_period_end": False,
              "metadata": {"proposalq_account": str(account.identity)},
              "latest_invoice": {"id": "in_test", "object": "invoice", "livemode": False,
                                 "status": "paid", "customer": account.stripe_customer_id},
              "items": {"data": [{"price": deepcopy(PRICE), "quantity": 1,
                                  "current_period_start": int((NOW - timedelta(days=5)).timestamp()),
                                  "current_period_end": int((NOW + timedelta(days=25)).timestamp())}], "has_more": False}}
    result.update(changes)
    return result


class BillingConfigurationTests(SimpleTestCase):
    def environment(self, **changes):
        env = {name: str(value) for name, value in CONFIG.items() if name not in ("STORAGES", "ALLOWED_HOSTS")}
        env.update(changes)
        return env

    def test_disabled_by_default_without_credentials(self):
        self.assertFalse(billing_configuration({})["BILLING_ENABLED"])

    def test_test_configuration_and_pinned_api_version(self):
        result = billing_configuration(self.environment(), production=True)
        self.assertTrue(result["BILLING_ENABLED"])
        self.assertEqual(result["STRIPE_API_VERSION"], API_VERSION)

    def test_live_restricted_and_unknown_keys_rejected_even_disabled(self):
        for key in ("sk_live_forbidden", "rk_live_forbidden", "rk_test_unsupported", "arbitrary"):
            with self.subTest(key_type=key.split("_")[0]), self.assertRaises(ImproperlyConfigured):
                billing_configuration({"STRIPE_SECRET_KEY": key})

    def test_missing_configuration_rejected_without_leaking_secrets(self):
        for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRO_PRICE_ID", "STRIPE_PORTAL_CONFIGURATION_ID"):
            env = self.environment(**{name: ""})
            with self.subTest(name=name), self.assertRaises(ImproperlyConfigured) as error:
                billing_configuration(env)
            self.assertIn(name, str(error.exception))
            self.assertNotIn("synthetic_only", str(error.exception))

    def test_billing_flag_is_strict(self):
        for value in ("", "1", "yes"):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                billing_configuration({"BILLING_ENABLED": value})

    def test_untrusted_return_origins_rejected(self):
        for url in ("https://user:secret@example.com", "https://example.com/path", "https://example.com?q=1",
                    "https://example.com#part", "javascript:alert(1)", "http://example.com", "",
                    "http://localhost:invalid", "https://example.com:0", "https://exam\nple.com"):
            with self.subTest(url=url), self.assertRaises(ImproperlyConfigured):
                billing_configuration(self.environment(BILLING_BASE_URL=url))

    def test_local_http_allowed_only_in_development(self):
        env = self.environment(BILLING_BASE_URL="http://127.0.0.1:8000/")
        self.assertEqual(billing_configuration(env)["BILLING_BASE_URL"], "http://127.0.0.1:8000")
        with self.assertRaises(ImproperlyConfigured):
            billing_configuration(env, production=True)


@override_settings(**CONFIG)
class MonthlyAllowanceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="quota-owner")
        self.clock = patch("proposal_ai.ai_control.now", return_value=NOW)
        self.clock_mock = self.clock.start()
        self.addCleanup(self.clock.stop)

    def history(self, count, at=NOW - timedelta(days=1), lifecycle=L.SUCCEEDED, **changes):
        values = dict(user=self.user, operation=O.PROPOSAL_GENERATION, intent=AIRequest.Intent.GENERATE,
                      quota_units=3, lifecycle=lifecycle, quota_state=Q.CONSUMED,
                      submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
                      admitted_at=at, lease_expires_at=at + control.LEASE)
        values.update(changes)
        return AIRequest.objects.bulk_create([AIRequest(nonce=uuid.uuid4(), **values) for _ in range(count)])

    def pro(self, **changes):
        values = dict(user=self.user, status="active", paid=True, price_id="price_pro",
                      period_start=NOW - timedelta(days=5), period_end=NOW + timedelta(days=25))
        values.update(changes)
        return BillingAccount.objects.create(**values)

    def admit(self, operation=O.PROPOSAL_GENERATION, token=None):
        return control.admit(self.user, operation, token or control.issue_nonce(self.user, operation), {"input": "test"}, "test")

    def test_existing_user_is_free_without_new_account_row(self):
        current = subscriptions.usage(self.user, NOW)
        self.assertEqual((current["plan"], current["limit"], current["remaining"]), ("Free", 3, 3))
        self.assertFalse(BillingAccount.objects.exists())

    def test_third_free_proposal_is_admitted_and_fourth_blocked(self):
        self.history(2)
        row = self.admit().request
        control.mark_dispatch(row)
        control.succeed(row)
        with self.assertRaises(control.ControlError) as error:
            self.admit()
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(AIRequest.objects.count(), 3)

    def test_fiftieth_pro_is_admitted_and_fifty_first_blocked(self):
        self.pro()
        self.history(49, at=NOW - timedelta(days=10))
        row = self.admit().request
        control.mark_dispatch(row)
        control.succeed(row)
        with self.assertRaises(control.ControlError) as error:
            self.admit()
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(AIRequest.objects.count(), 50)

    def test_replay_after_limit_returns_same_result_without_extra_usage(self):
        self.history(2)
        token = control.issue_nonce(self.user, O.PROPOSAL_GENERATION)
        row = self.admit(token=token).request
        control.mark_dispatch(row)
        control.succeed(row)
        self.assertTrue(self.admit(token=token).replay)
        self.assertEqual(subscriptions.usage(self.user, NOW)["used"], 3)

    def test_failures_and_uncertain_requests_do_not_consume_monthly_places(self):
        self.history(1)
        self.history(1, lifecycle=L.FAILED)
        self.history(1, lifecycle=L.UNCERTAIN)
        self.assertEqual(subscriptions.usage(self.user, NOW)["used"], 1)
        self.admit()

    def test_pending_reservation_counts_and_failure_releases_monthly_place(self):
        self.history(2)
        row = self.admit().request
        self.assertEqual(subscriptions.usage(self.user, NOW)["remaining"], 0)
        control.fail(row, F.LOCAL_CONFIGURATION, release=True)
        self.assertEqual(subscriptions.usage(self.user, NOW)["remaining"], 1)
        self.admit()

    def test_failed_persistence_does_not_consume_monthly_place(self):
        row = self.admit().request
        control.mark_dispatch(row)
        with self.assertRaises(control.ControlError):
            control.succeed(row, lambda: (_ for _ in ()).throw(DatabaseError("private")))
        self.assertEqual(subscriptions.usage(self.user, NOW)["used"], 0)

    def test_saved_result_deletion_does_not_restore_allowance(self):
        self.history(3, proposal=None)
        with self.assertRaises(control.ControlError):
            self.admit()

    def test_utc_month_reset_and_previous_usage_are_preserved(self):
        self.history(3, at=datetime(2026, 9, 30, 23, 59, tzinfo=dt_timezone.utc))
        self.admit()
        self.assertEqual(AIRequest.objects.count(), 4)

    def test_month_is_chosen_after_waiting_for_billing_lock(self):
        self.history(3)
        next_month = datetime(2026, 11, 1, tzinfo=dt_timezone.utc)
        with patch("proposal_ai.subscriptions.lock_account", side_effect=lambda *args: setattr(self.clock_mock, "return_value", next_month)):
            row = self.admit().request
        self.assertEqual(row.admitted_at, next_month)
        self.assertEqual(subscriptions.usage(self.user, next_month)["pending"], 1)

    def test_december_and_leap_year_boundaries(self):
        for date, end in ((datetime(2026, 12, 31, tzinfo=dt_timezone.utc), datetime(2027, 1, 1, tzinfo=dt_timezone.utc)),
                          (datetime(2028, 2, 29, tzinfo=dt_timezone.utc), datetime(2028, 3, 1, tzinfo=dt_timezone.utc))):
            self.assertEqual(subscriptions.month_bounds(date)[1], end)

    def test_local_timezone_does_not_reset_early(self):
        moment = datetime(2026, 11, 1, 0, 30, tzinfo=dt_timezone(timedelta(hours=1)))
        self.assertEqual(subscriptions.month_bounds(moment)[0].month, 10)

    def test_upgrade_keeps_current_month_usage(self):
        self.history(3)
        self.pro()
        self.assertEqual(subscriptions.usage(self.user, NOW)["remaining"], 47)
        self.admit()

    def test_downgrade_keeps_usage_and_saved_records(self):
        self.history(6)
        self.pro(status="canceled")
        self.assertEqual(subscriptions.usage(self.user, NOW)["remaining"], 0)
        with self.assertRaises(control.ControlError):
            self.admit()
        self.assertEqual(AIRequest.objects.count(), 6)

    def test_only_active_paid_current_subscription_grants_pro(self):
        account = self.pro()
        for status in ("past_due", "unpaid", "incomplete", "incomplete_expired", "canceled", "paused", "trialing", "conflict"):
            account.status = status
            self.assertFalse(subscriptions.is_pro(account, NOW), status)

    def test_expiry_missing_period_unpaid_wrong_price_fail_closed(self):
        account = self.pro()
        for changes in ({"period_end": NOW}, {"period_start": NOW + timedelta(seconds=1)},
                        {"period_end": None}, {"paid": False}, {"price_id": "price_other"}):
            with self.subTest(changes=changes):
                current = deepcopy(account)
                for key, value in changes.items():
                    setattr(current, key, value)
                self.assertFalse(subscriptions.is_pro(current, NOW))

    def test_scheduled_cancellation_retains_pro_until_end(self):
        account = self.pro(cancel_at_period_end=True)
        self.assertTrue(subscriptions.is_pro(account, NOW))
        self.assertFalse(subscriptions.is_pro(account, account.period_end))

    def test_billing_disabled_cannot_grant_pro(self):
        account = self.pro()
        with override_settings(BILLING_ENABLED=False):
            self.assertFalse(subscriptions.is_pro(account, NOW))

    def test_summary_and_extraction_do_not_use_monthly_proposal_limit(self):
        self.history(3)
        row = self.admit(O.PROFILE_SUMMARY).request
        control.fail(row, F.LOCAL_CONFIGURATION, release=True)
        self.admit(O.JOB_EXTRACTION)

    def test_existing_daily_weekly_and_burst_controls_still_apply_on_pro(self):
        self.pro()
        self.history(9, at=NOW - timedelta(hours=1))
        with self.assertRaises(control.ControlError) as error:
            self.admit()
        self.assertIn("allowance", error.exception.user_message)

    def test_user_cannot_use_another_users_subscription(self):
        other = get_user_model().objects.create_user(username="other-quota-owner")
        self.pro(user=other)
        self.history(3)
        with self.assertRaises(control.ControlError):
            self.admit()


@override_settings(**CONFIG)
class BillingFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="billing-owner")
        self.account = BillingAccount.objects.create(user=self.user, stripe_customer_id="cus_owned")
        self.api = Mock()
        self.api.v1.subscriptions.list.return_value = page([])
        self.api.v1.prices.retrieve.return_value = resource(PRICE)
        self.api.v1.billing_portal.configurations.retrieve.return_value = resource(PORTAL)
        self.api.v1.billing_portal.sessions.create.return_value = resource({"url": "https://billing.stripe.com/p/session"})
        self.api.v1.checkout.sessions.create.side_effect = self.session
        self.client_patch = patch("proposal_ai.billing.client", return_value=self.api)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def session(self, params, options):
        return resource({"id": "cs_test_owned", "object": "checkout.session", "livemode": False,
                         "customer": params["customer"], "mode": "subscription", "status": "open",
                         "metadata": params["metadata"], "url": "https://checkout.stripe.com/c/pay/test"})

    def event(self, event_type="customer.subscription.updated", **changes):
        event = {"id": "evt_" + uuid.uuid4().hex, "object": "event", "livemode": False, "type": event_type,
                 "data": {"object": {"id": "sub_owned", "customer": "cus_owned", "livemode": False}}}
        event.update(changes)
        return event

    def current_subscription(self, **changes):
        row = subscription(self.account, **changes)
        self.api.v1.subscriptions.list.return_value = page([row])
        return row

    def test_checkout_uses_fixed_price_customer_and_urls(self):
        url = billing.checkout(self.user)
        self.assertEqual(url, "https://checkout.stripe.com/c/pay/test")
        params = self.api.v1.checkout.sessions.create.call_args.args[0]
        self.assertEqual(params["customer"], "cus_owned")
        self.assertEqual(params["line_items"], [{"price": "price_pro", "quantity": 1}])
        self.assertEqual(params["success_url"], "https://proposalq.example/billing/return/")
        self.assertEqual(params["payment_method_types"], ["card"])
        self.assertFalse(params["allow_promotion_codes"])
        self.account.refresh_from_db()
        self.assertFalse(self.account.paid)

    def test_real_sdk_checkout_serialization_against_fake_http_transport(self):
        from urllib.parse import parse_qs
        self.client_patch.stop()
        calls = []

        def transport(http, method, url, headers, post_data=None):
            calls.append((method, url, headers, post_data))
            if "/subscriptions?" in url:
                response = {"object": "list", "url": "/v1/subscriptions", "has_more": False, "data": []}
            elif url.endswith("/prices/price_pro"):
                response = PRICE
            elif url.endswith("/billing_portal/configurations/bpc_test"):
                response = PORTAL
            elif url.endswith("/checkout/sessions"):
                params = parse_qs(post_data)
                self.assertEqual(params["line_items[0][price]"], ["price_pro"])
                self.assertEqual(params["customer"], ["cus_owned"])
                self.assertEqual(params["mode"], ["subscription"])
                response = {"object": "checkout.session", "id": "cs_test_http", "livemode": False,
                            "customer": "cus_owned", "mode": "subscription", "status": "open",
                            "metadata": {"proposalq_attempt": params["metadata[proposalq_attempt]"][0]},
                            "url": "https://checkout.stripe.com/c/pay/test"}
            else:
                raise AssertionError("Unexpected Stripe endpoint.")
            return json.dumps(response).encode(), 200, {"request-id": "req_fake"}

        with patch("stripe.RequestsClient.request", autospec=True, side_effect=transport):
            self.assertEqual(billing.checkout(self.user), "https://checkout.stripe.com/c/pay/test")
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(call[2]["Stripe-Version"] == API_VERSION for call in calls))
        self.assertIn("Idempotency-Key", calls[-1][2])

    def test_repeated_clicks_reuse_existing_open_session(self):
        billing.checkout(self.user)
        first = self.api.v1.checkout.sessions.create.return_value
        attempt = CheckoutAttempt.objects.get()
        self.api.v1.checkout.sessions.retrieve.return_value = self.session(
            {"customer": "cus_owned", "metadata": {"proposalq_attempt": str(attempt.identity)}}, {})
        billing.checkout(self.user)
        self.api.v1.checkout.sessions.create.assert_called_once()
        self.assertEqual(CheckoutAttempt.objects.count(), 1)

    def test_uncertain_create_retries_same_durable_key(self):
        self.api.v1.checkout.sessions.create.side_effect = stripe.APIConnectionError("private-provider-error")
        with self.assertRaises(stripe.APIConnectionError):
            billing.checkout(self.user)
        first_key = self.api.v1.checkout.sessions.create.call_args.kwargs["options"]["idempotency_key"]
        self.assertFalse(CheckoutAttempt.objects.get().resolved)
        self.api.v1.checkout.sessions.create.side_effect = self.session
        billing.checkout(self.user)
        self.assertEqual(first_key, self.api.v1.checkout.sessions.create.call_args.kwargs["options"]["idempotency_key"])

    def test_old_unknown_attempt_does_not_reuse_expired_idempotency_guarantee(self):
        attempt = CheckoutAttempt.objects.create(account=self.account)
        CheckoutAttempt.objects.filter(pk=attempt.pk).update(created_at=NOW - timedelta(days=2))
        with self.assertRaises(billing.BillingError):
            billing.checkout(self.user)
        self.api.v1.checkout.sessions.create.assert_not_called()

    def test_existing_nonterminal_subscription_blocks_checkout_despite_webhook_lag(self):
        for status in ("active", "trialing", "past_due", "unpaid", "paused", "incomplete"):
            self.current_subscription(status=status)
            with self.subTest(status=status), self.assertRaises(billing.BillingError) as error:
                billing.checkout(self.user)
            self.assertEqual(error.exception.status, 409)
        self.api.v1.checkout.sessions.create.assert_not_called()

    def test_wrong_amount_currency_recurrence_quantity_and_live_catalog_blocked(self):
        for changes in ({"unit_amount": 1000}, {"currency": "usd"}, {"livemode": True},
                        {"tax_behavior": "exclusive"}, {"active": False}, {"recurring": {"interval": "year"}}):
            self.api.v1.prices.retrieve.return_value = resource({**PRICE, **changes})
            with self.subTest(changes=changes), self.assertRaises(billing.BillingError):
                billing.checkout(self.user)
        self.api.v1.checkout.sessions.create.assert_not_called()

    def test_portal_must_allow_period_end_cancellation_and_disallow_plan_switches(self):
        for feature, changes in (("subscription_cancel", {"enabled": False}),
                                 ("subscription_cancel", {"enabled": True, "mode": "immediately"}),
                                 ("subscription_update", {"enabled": True}),
                                 ("payment_method_update", {"enabled": False})):
            portal = deepcopy(PORTAL)
            portal["features"][feature] = changes
            self.api.v1.billing_portal.configurations.retrieve.return_value = resource(portal)
            with self.subTest(feature=feature), self.assertRaises(billing.BillingError):
                billing.checkout(self.user)

    def test_expired_session_resolves_before_new_checkout_can_start(self):
        billing.checkout(self.user)
        attempt = CheckoutAttempt.objects.get()
        session = billing.data(self.session({"customer": "cus_owned", "metadata": {"proposalq_attempt": str(attempt.identity)}}, {}))
        session["status"] = "expired"
        self.api.v1.checkout.sessions.retrieve.return_value = resource(session)
        with self.assertRaises(billing.BillingError):
            billing.checkout(self.user)
        attempt.refresh_from_db()
        self.assertTrue(attempt.resolved)

    def test_complete_session_does_not_open_second_checkout(self):
        attempt = CheckoutAttempt.objects.create(account=self.account, stripe_session_id="cs_test_owned")
        session = billing.data(self.session({"customer": "cus_owned", "metadata": {"proposalq_attempt": str(attempt.identity)}}, {}))
        session["status"] = "complete"
        self.api.v1.checkout.sessions.retrieve.return_value = resource(session)
        with self.assertRaises(billing.BillingError):
            billing.checkout(self.user)
        self.api.v1.checkout.sessions.create.assert_not_called()

    def test_customer_create_is_idempotent_and_uses_no_email_identity(self):
        self.account.stripe_customer_id = None
        self.account.save()
        self.api.v1.customers.create.return_value = resource({"id": "cus_new", "livemode": False})
        billing.checkout(self.user)
        params = self.api.v1.customers.create.call_args
        self.assertEqual(params.kwargs["options"]["idempotency_key"], f"proposalq-customer-{self.account.identity}")
        self.assertNotIn("email", params.args[0])

    def test_portal_is_bound_to_signed_in_users_customer(self):
        self.assertEqual(billing.portal(self.user), "https://billing.stripe.com/p/session")
        params = self.api.v1.billing_portal.sessions.create.call_args.args[0]
        self.assertEqual(params["customer"], "cus_owned")
        self.assertEqual(params["configuration"], "bpc_test")

    def test_customerless_portal_rejected(self):
        other = get_user_model().objects.create_user(username="no-billing-customer")
        with self.assertRaises(billing.BillingError):
            billing.portal(other)

    def test_redirect_host_injection_rejected(self):
        for value in ("https://checkout.stripe.com.evil.test/path", "http://checkout.stripe.com/path",
                      "https://user@checkout.stripe.com/path", "https://evil.test/path", "javascript:alert(1)"):
            with self.subTest(value=value), self.assertRaises(billing.BillingError):
                billing.hosted_url(value, "checkout.stripe.com")

    def test_subscription_state_is_read_from_stripe_and_not_event_snapshot(self):
        self.current_subscription(status="past_due")
        event = self.event()
        event["data"]["object"]["status"] = "active"
        billing.process_event(resource(event))
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "past_due")

    def test_duplicate_event_processed_once(self):
        self.current_subscription()
        event = self.event()
        billing.process_event(event)
        billing.process_event(event)
        self.api.v1.subscriptions.list.assert_called_once()
        self.assertEqual(StripeEvent.objects.count(), 1)

    def test_failed_webhook_rolls_back_state_and_receipt_for_retry(self):
        self.api.v1.subscriptions.list.side_effect = stripe.APIConnectionError("private")
        event = self.event()
        with self.assertRaises(stripe.APIConnectionError):
            billing.process_event(event)
        self.assertFalse(StripeEvent.objects.exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "none")
        self.api.v1.subscriptions.list.side_effect = None
        self.current_subscription()
        billing.process_event(event)
        self.assertEqual(StripeEvent.objects.count(), 1)

    def test_database_failure_does_not_acknowledge_event(self):
        self.current_subscription()
        with patch.object(BillingAccount, "save", side_effect=DatabaseError("private")):
            with self.assertRaises(DatabaseError):
                billing.process_event(self.event())
        self.assertFalse(StripeEvent.objects.exists())

    def test_active_paid_subscription_has_verified_pro_entitlement(self):
        self.current_subscription()
        billing.process_event(self.event())
        self.account.refresh_from_db()
        self.assertTrue(subscriptions.is_pro(self.account, NOW))

    def test_invoice_failure_removes_entitlement_and_payment_recovery_restores_it(self):
        self.current_subscription(status="past_due")
        billing.process_event(self.event("invoice.payment_failed"))
        self.account.refresh_from_db()
        self.assertFalse(subscriptions.is_pro(self.account, NOW))
        self.current_subscription()
        billing.process_event(self.event("invoice.paid"))
        self.account.refresh_from_db()
        self.assertTrue(subscriptions.is_pro(self.account, NOW))

    def test_unpaid_latest_invoice_never_grants_pro_even_if_status_active(self):
        row = subscription(self.account)
        row["latest_invoice"]["status"] = "open"
        self.api.v1.subscriptions.list.return_value = page([row])
        billing.process_event(self.event())
        self.account.refresh_from_db()
        self.assertFalse(self.account.paid)

    def test_wrong_price_quantity_and_pause_collection_do_not_grant_pro(self):
        for kind in ("price", "quantity", "pause"):
            row = subscription(self.account)
            if kind == "price":
                row["items"]["data"][0]["price"]["id"] = "price_other"
            elif kind == "quantity":
                row["items"]["data"][0]["quantity"] = 2
            else:
                row["pause_collection"] = {"behavior": "void"}
            self.api.v1.subscriptions.list.return_value = page([row])
            billing.process_event(self.event())
            self.account.refresh_from_db()
            self.assertFalse(self.account.paid, kind)

    def test_old_canceled_event_cannot_remove_new_subscription(self):
        old = subscription(self.account, id="sub_old", status="canceled", created=50)
        new = subscription(self.account, id="sub_new", created=200)
        self.api.v1.subscriptions.list.return_value = page([old, new])
        billing.process_event(self.event("customer.subscription.deleted"))
        self.account.refresh_from_db()
        self.assertEqual(self.account.stripe_subscription_id, "sub_new")
        self.assertTrue(subscriptions.is_pro(self.account, NOW))

    def test_duplicate_remote_subscriptions_fail_closed(self):
        self.api.v1.subscriptions.list.return_value = page([subscription(self.account), subscription(self.account, id="sub_duplicate")])
        billing.process_event(self.event())
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "conflict")
        self.assertFalse(self.account.paid)

    def test_unowned_subscription_metadata_never_grants_pro(self):
        self.current_subscription(metadata={"proposalq_account": "another-account"})
        billing.process_event(self.event())
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "none")

    def test_unknown_customer_is_ignored_without_linking_by_email_or_metadata(self):
        event = self.event()
        event["data"]["object"]["customer"] = "cus_unknown"
        billing.process_event(event)
        self.api.v1.subscriptions.list.assert_not_called()
        self.assertEqual(StripeEvent.objects.get().outcome, "ignored")

    def test_live_event_or_live_fetched_subscription_is_rejected(self):
        for event, row in ((self.event(livemode=True), subscription(self.account)),
                           (self.event(), subscription(self.account, livemode=True))):
            self.api.v1.subscriptions.list.return_value = page([row])
            with self.assertRaises(billing.BillingError):
                billing.process_event(event)
        self.assertFalse(StripeEvent.objects.exists())

    def test_customer_deletion_revokes_access_and_blocks_repurchase(self):
        event = self.event("customer.deleted")
        event["data"]["object"]["id"] = "cus_owned"
        billing.process_event(event)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "customer_deleted")
        with self.assertRaises(billing.BillingError):
            billing.checkout(self.user)

    def test_unique_pending_attempt_and_customer_constraints(self):
        CheckoutAttempt.objects.create(account=self.account)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CheckoutAttempt.objects.create(account=self.account)
        other = get_user_model().objects.create_user(username="duplicate-customer")
        with self.assertRaises(IntegrityError), transaction.atomic():
            BillingAccount.objects.create(user=other, stripe_customer_id="cus_owned")

    def test_account_deletion_cannot_orphan_subscription(self):
        with self.assertRaises(ProtectedError):
            self.user.delete()

    def test_operator_reconciliation_recovers_uncertain_checkout_without_payment(self):
        attempt = CheckoutAttempt.objects.create(account=self.account)
        self.current_subscription()
        session = billing.data(self.session({"customer": "cus_owned", "metadata": {"proposalq_attempt": str(attempt.identity)}}, {}))
        session["status"] = "complete"
        self.api.v1.checkout.sessions.list.return_value = page([session])
        output = io.StringIO()
        call_command("reconcile_billing", self.account.pk, stdout=output)
        attempt.refresh_from_db()
        self.assertTrue(attempt.resolved)
        self.api.v1.checkout.sessions.create.assert_not_called()
        self.assertIn("No payment was created", output.getvalue())

    def test_operator_error_does_not_leak_provider_error(self):
        self.api.v1.subscriptions.list.side_effect = stripe.APIConnectionError("private-provider-details")
        with self.assertRaises(CommandError) as error:
            call_command("reconcile_billing", self.account.pk)
        self.assertNotIn("private-provider-details", str(error.exception))


@override_settings(**CONFIG)
class BillingViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="billing-view")

    def signed(self, payload, timestamp=None):
        body = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = int(time.time()) if timestamp is None else timestamp
        signature = hmac.new(CONFIG["STRIPE_WEBHOOK_SECRET"].encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
        return body, f"t={timestamp},v1={signature}"

    def event(self, **changes):
        event = {"id": "evt_signed", "object": "event", "livemode": False,
                 "type": "customer.subscription.updated", "data": {"object": {"id": "sub_unknown", "customer": "cus_unknown", "livemode": False}}}
        event.update(changes)
        return event

    def test_public_pricing_is_accessible_without_creating_billing_records(self):
        response = self.client.get(reverse("pricing"))
        self.assertContains(response, "£9.99")
        self.assertContains(response, "3 AI-generated proposals")
        self.assertContains(response, "50 AI-generated proposals")
        self.assertFalse(BillingAccount.objects.exists())
        self.assertIn("no-store", response["Cache-Control"])

    def test_free_user_sees_usage_and_upgrade(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("pricing"))
        self.assertContains(response, "0 of 3 proposals generated")
        self.assertContains(response, reverse("billing_checkout"))

    def test_billing_disabled_hides_checkout_and_does_not_call_stripe(self):
        with override_settings(BILLING_ENABLED=False):
            response = self.client.get(reverse("pricing"))
        self.assertContains(response, "Test checkout coming soon")

    def test_login_required_for_billing_actions_and_return(self):
        for name in ("billing_checkout", "billing_portal", "billing_return"):
            self.assertEqual(self.client.post(reverse(name)).status_code, 302)

    def test_state_changes_reject_get(self):
        self.client.force_login(self.user)
        for name in ("billing_checkout", "billing_portal"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 405)

    def test_checkout_and_portal_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for name in ("billing_checkout", "billing_portal"):
            self.assertEqual(client.post(reverse(name)).status_code, 403)

    def test_client_supplied_ids_and_price_are_not_passed_to_service(self):
        self.client.force_login(self.user)
        with patch("proposal_ai.billing.checkout", return_value="https://checkout.stripe.com/c/pay/test") as checkout:
            response = self.client.post(reverse("billing_checkout"), {"customer": "cus_other", "price": "price_other", "return_url": "https://evil.test"})
        self.assertEqual(response.status_code, 302)
        checkout.assert_called_once_with(self.user)

    def test_checkout_return_cannot_activate_subscription(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("billing_return"), {"session_id": "cs_test_fake", "paid": "true", "plan": "pro"})
        self.assertContains(response, "Pro activates after Stripe confirms payment")
        self.assertEqual(response.context["usage"]["plan"], "Free")
        self.assertFalse(BillingAccount.objects.exists())

    def test_provider_error_is_generic_and_not_cached(self):
        self.client.force_login(self.user)
        with patch("proposal_ai.billing.checkout", side_effect=stripe.APIConnectionError("private-provider-marker")):
            response = self.client.post(reverse("billing_checkout"))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"private-provider-marker", response.content)
        self.assertIn("no-store", response["Cache-Control"])

    def test_valid_signed_event_works_without_browser_csrf(self):
        body, signature = self.signed(self.event())
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("stripe_webhook"), body, content_type="application/json", HTTP_STRIPE_SIGNATURE=signature)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StripeEvent.objects.count(), 1)

    def test_unsigned_bad_signature_and_mutated_payload_rejected(self):
        body, signature = self.signed(self.event())
        for payload, header in ((body, ""), (body, "t=1,v1=bad"), (body + b" ", signature)):
            response = self.client.post(reverse("stripe_webhook"), payload, content_type="application/json", HTTP_STRIPE_SIGNATURE=header)
            self.assertEqual(response.status_code, 400)
        self.assertFalse(StripeEvent.objects.exists())

    def test_old_signature_rejected(self):
        body, signature = self.signed(self.event(), int(time.time()) - 301)
        response = self.client.post(reverse("stripe_webhook"), body, content_type="application/json", HTTP_STRIPE_SIGNATURE=signature)
        self.assertEqual(response.status_code, 400)

    def test_duplicate_signed_event_acknowledged_once(self):
        body, signature = self.signed(self.event())
        for _ in range(2):
            self.assertEqual(self.client.post(reverse("stripe_webhook"), body, content_type="application/json", HTTP_STRIPE_SIGNATURE=signature).status_code, 200)
        self.assertEqual(StripeEvent.objects.count(), 1)

    def test_live_signed_event_rejected(self):
        body, signature = self.signed(self.event(livemode=True))
        response = self.client.post(reverse("stripe_webhook"), body, content_type="application/json", HTTP_STRIPE_SIGNATURE=signature)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(StripeEvent.objects.exists())

    def test_temporary_failure_returns_retryable_status(self):
        body, signature = self.signed(self.event())
        with patch("proposal_ai.billing.process_event", side_effect=DatabaseError("private-db-marker")):
            response = self.client.post(reverse("stripe_webhook"), body, content_type="application/json", HTTP_STRIPE_SIGNATURE=signature)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"private-db-marker", response.content)

    def test_oversize_payload_rejected_before_signature_processing(self):
        response = self.client.post(reverse("stripe_webhook"), b"x" * 1_000_001, content_type="application/json")
        self.assertEqual(response.status_code, 413)

    def test_webhook_accepts_only_post(self):
        self.assertEqual(self.client.get(reverse("stripe_webhook")).status_code, 405)


@override_settings(**CONFIG)
class StripeSdkBoundaryTests(SimpleTestCase):
    def test_client_uses_pinned_version_test_key_timeout_and_no_retries(self):
        with patch("stripe.StripeClient") as constructor, patch("stripe.RequestsClient") as http:
            billing.client()
        self.assertEqual(constructor.call_args.args, ("sk_test_synthetic_only",))
        self.assertEqual(constructor.call_args.kwargs["stripe_version"], API_VERSION)
        self.assertEqual(constructor.call_args.kwargs["max_network_retries"], 0)
        http.assert_called_once_with(timeout=8)

    def test_disabled_or_live_key_never_constructs_sdk_client(self):
        for changes in ({"BILLING_ENABLED": False}, {"STRIPE_SECRET_KEY": "sk_live_forbidden"}):
            with override_settings(**changes), patch("stripe.StripeClient") as constructor:
                with self.assertRaises(billing.BillingError):
                    billing.client()
                constructor.assert_not_called()

    def test_sdk_resource_conversion_preserves_nested_values(self):
        self.assertEqual(billing.require_test(resource(PRICE)), PRICE)

    def test_billing_admin_is_inspection_only(self):
        from django.contrib import admin
        for model in (BillingAccount, CheckoutAttempt, StripeEvent):
            registered = admin.site._registry[model]
            self.assertFalse(registered.has_add_permission(None))
            self.assertFalse(registered.has_change_permission(None))
            self.assertFalse(registered.has_delete_permission(None))
            self.assertIsNone(registered.actions)
