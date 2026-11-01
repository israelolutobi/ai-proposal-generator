# ProposalQ subscriptions: review and Stripe test setup

This feature starts from production branch `codex/seo-sitemap` at
`a46ac7b6885d523a438db5d7a07cc486c0e7a785`. Work is isolated on
`codex/free-pro-subscriptions`. No production merge, deployment, live payment,
bank connection or change to an existing Git stash is part of this work.

## Architecture audit and implementation

- Django 6.0.4, Python 3.13, built-in Django users and session authentication.
  Existing profiles, experience, jobs, proposals and account safeguards remain.
- AI generation goes through `proposal_ai/views.py`, `ai_control.py` and
  `services.py`. The durable `AIRequest` ledger already provides signed nonces,
  replay protection, per-account active-request uniqueness, stale recovery,
  daily/weekly spending allowances, bursts and global spending ceilings.
- Monthly proposal accounting extends that ledger; a separate mutable usage
  counter would risk disagreeing with the saved generation result. The existing
  fenced success transaction saves a proposal and its successful ledger state
  together. Deleting a proposal does not refund its ledger usage.
- `subscriptions.py` enforces 3 Free / 50 Pro places per UTC calendar month at
  admission, before any provider work. Successful requests count; reserved and
  in-flight requests temporarily reserve a place. Failures and stale uncertain
  requests without a saved result do not consume monthly places. Their provider
  spending still follows the existing conservative credit rules.
- Regenerating with a new nonce consumes a place. Replaying a successful nonce
  does not. Summaries and job extraction are outside the proposal allowance.
  Existing daily/weekly and burst/global controls apply to both plans.
- The month is the request's final admission month, after database lock waits.
  A generation spanning midnight belongs to the month it started in. UTC avoids
  London daylight-saving ambiguity. No monthly reset job is required.
- Upgrade changes the current month's limit from 3 to 50 without resetting its
  usage. Downgrade keeps usage too. Unused places do not roll over. Stripe bills
  monthly from signup; its renewal date can differ from the calendar reset.
- `billing.py` handles Checkout, the Billing Portal and reconciliation.
  `billing_views.py` exposes `/pricing/`, authenticated POST checkout/portal
  actions, the informational return page and the signed webhook endpoint.
- Migration `0019_subscriptions` only adds `BillingAccount`, `CheckoutAttempt`
  and `StripeEvent` tables. It does not rewrite users or existing data, invent
  subscriptions or reset AI history. Existing users are Free automatically.
  Existing successful ledger rows in the current month count towards the limit.
- Billing records are inspection-only in Django admin, using normal model view
  permissions. BillingAccount IDs shown there are used by the recovery command.
  User deletion is protected once a billing account exists, to avoid orphaning
  recurring payments; an operator must resolve billing before account deletion.

## State, replay and charge safeguards

Pro requires an `active` subscription with exactly one configured GBP £9.99
monthly price, quantity one, an expanded latest invoice in `paid` status, no
paused payment collection, and a current, unexpired Stripe item billing period.
`past_due`, `unpaid`, `incomplete`, `incomplete_expired`, `paused`, `trialing` and
`canceled` use Free limits. Trial subscriptions are not offered. Scheduled
cancellation keeps Pro through the paid period; payment recovery restores Pro.
Saved product data remains accessible after downgrade.

Stripe events must pass SDK signature verification against the **raw body**,
with a five-minute timestamp tolerance. Only the webhook is CSRF exempt.
Event IDs are unique and receipts commit in the same transaction as state.
Temporary database/Stripe errors return 503 without a receipt, allowing retries.
Unknown customers cannot attach themselves using email or event metadata.

Each relevant webhook fetches current subscriptions from the authenticated
Stripe API while holding the account lock. Older/out-of-order events therefore
cannot restore canceled access or remove a newer subscription. Multiple ongoing
subscriptions owned by this app produce a conflict and fail closed for Pro;
review them in Stripe and cancel unwanted duplicates before reconciliation.
Duplicate external subscriptions are not automatically charged, canceled or
refunded by this application.

Checkout uses saved random customer/attempt identities and Stripe idempotency
keys. An unresolved checkout is reused; any nonterminal subscription on that
customer blocks a new checkout, including when its webhook is delayed. A timeout
keeps the attempt for retry. Unknown attempts older than 23 hours require
reconciliation instead of relying on Stripe's 24-hour idempotency retention.
An expired session is resolved before a new attempt can begin. Avoid creating
subscriptions manually or through payment links for the app's customers.

The browser supplies no price, customer, subscription or redirect URL. Checkout
and Portal need login, POST and CSRF protection. Return URLs use a configured
origin; outbound redirects are restricted to Stripe's hosted Checkout/Portal.
Pricing and billing responses are private and non-cacheable. No card details,
bank details, raw event payloads or Stripe error messages are stored by ProposalQ.

## Connect your Stripe test account

1. Sign into your own [Stripe Dashboard](https://dashboard.stripe.com/) and
   select **test mode / a sandbox**. Do not supply a live key. This application
   rejects keys other than `sk_test_...`, including when billing is disabled;
   it also rejects live API objects and events. There is no live-mode switch.
2. In that same test environment, create a **ProposalQ Pro** product with one
   recurring price: **GBP 9.99 every month**, fixed per-unit licensed pricing,
   **tax inclusive**, active, no trial. Copy its `price_...` ID. The application
   checks the price before starting Checkout. Quantity is always one, promotion
   codes are disabled and automatic tax is off for this test implementation.
3. Configure the [test Customer Portal](https://dashboard.stripe.com/test/settings/billing/portal):
   enable payment-method updates, invoice history and cancellation **at the end
   of the billing period**. Disable plan switching, quantity changes, promotion
   codes and retention discounts. Save and copy the `bpc_...` configuration ID
   (available via Stripe's portal configuration API if the Dashboard omits it).
   Add ProposalQ branding and your support contact. Checkout validates the
   required portal capabilities before allowing a purchase.
4. Add the following to your private local environment / ignored `.env`.
   Do not overwrite existing settings or put secrets in Git, chat or screenshots:

   ```dotenv
   BILLING_ENABLED=True
   STRIPE_SECRET_KEY=sk_test_your_private_test_key
   STRIPE_WEBHOOK_SECRET=whsec_your_endpoint_signing_secret
   STRIPE_PRO_PRICE_ID=price_your_test_monthly_price
   STRIPE_PORTAL_CONFIGURATION_ID=bpc_your_test_portal_configuration
   BILLING_BASE_URL=http://127.0.0.1:8000
   ```

   The return origin must be HTTPS outside local development. Credentials,
   customer, price, portal configuration and signing secret must all belong to
   the same Stripe test environment. Hosted Checkout needs no publishable key
   or browser Stripe SDK. `BILLING_ENABLED` defaults to False. Billing does not
   enable AI or change existing Phase B/global-spending approval settings.
5. Install the pinned requirements in your development environment. Back up
   your database, review migration 0019, then explicitly run on your **local or
   approved staging database**:

   ```powershell
   .\.venv\Scripts\python.exe manage.py migrate
   .\.venv\Scripts\python.exe manage.py check
   .\.venv\Scripts\python.exe manage.py runserver
   ```

   Do not run these migration instructions against production without approval.
   The migration has been exercised by isolated test databases during development.
   Normal readiness/release schema inspection discovers the new tables
   automatically. Collect static assets before any subsequently approved release.
6. For local testing, install/login to the official Stripe CLI yourself, then:

   ```powershell
   stripe listen --forward-to http://127.0.0.1:8000/billing/webhook/
   ```

   Copy the listener's `whsec_...` into the private environment and restart Django.
   A CLI listener secret differs from a Dashboard endpoint secret. CLI fixtures
   for unrelated test customers are safely ignored; use a real test Checkout
   through ProposalQ to verify account linking.
7. For a separately approved HTTPS staging host, register the snapshot webhook
   URL `https://YOUR-STAGING-HOST/billing/webhook/` with API version
   **2026-09-30.endive**, matching pinned `stripe==16.0.0`. Use its signing secret
   instead of the CLI listener secret. Subscribe to:

   ```text
   checkout.session.completed
   checkout.session.expired
   checkout.session.async_payment_succeeded
   checkout.session.async_payment_failed
   customer.subscription.created
   customer.subscription.updated
   customer.subscription.deleted
   customer.subscription.paused
   customer.subscription.resumed
   customer.deleted
   invoice.paid
   invoice.payment_succeeded
   invoice.payment_failed
   invoice.payment_action_required
   invoice.finalization_failed
   invoice.marked_uncollectible
   invoice.voided
   ```

## Test acceptance and operations

Run `python manage.py test` for the full regression suite. The custom runner
blocks networking and clears AI/Stripe credentials; billing tests use synthetic
test configuration, real SDK resource shapes, fake HTTP responses and signed
webhook bodies. No test can charge a card or invoke a paid AI provider.

On an explicitly designated disposable PostgreSQL cluster, use the existing
`scripts/run_postgres_tests.py` procedure. Its opt-in billing tests cover
simultaneous last-place admission, checkout clicks and webhook deliveries.
SQLite passing results alone do not prove production PostgreSQL concurrency.

The production-branch PostgreSQL runner also referenced
`mysite.postgres_phase_b_tests`, which is absent from that branch. That stale
entry was removed; all existing available integration modules remain selected,
and the four billing concurrency tests were added.

In your Stripe sandbox, finish the following manual acceptance checks after
credentials are configured:

- Sign in to ProposalQ, verify Free usage, and exhaust the three proposal places.
- Start Upgrade, close it and reopen; confirm it reuses the same open session.
- Complete Checkout with Stripe's `4242 4242 4242 4242` test card, a future expiry
  and test CVC. Pro must activate only after a successful webhook, with remaining
  allowance equal to 50 minus already-used places.
- Resend the event from Stripe; confirm no duplicate receipt or usage reset.
- Open Manage billing, update the test card, inspect invoices and cancel renewal.
  Verify Pro remains until period end, then Free with saved data intact.
- Use Stripe Billing test clocks / test cards to simulate failed renewal and
  payment recovery. Free limits must apply while payment needs attention.
- Verify expired subscriptions downgrade even if a renewal webhook is delayed.
- Send a bad webhook signature; expect 400. Temporarily fail Stripe/database
  reconciliation; expect 503 and a successful later retry.

Monitor failed event deliveries in Stripe Workbench. If an event was missed or a
checkout response was lost, find the account ID in Django admin and run:

```powershell
.\.venv\Scripts\python.exe manage.py reconcile_billing ACCOUNT_ID
```

This fetches authoritative **test** state and recovers an existing session by its
attempt metadata. It never creates a checkout/subscription, charges, cancels or
refunds anything. If an old unresolved attempt has no matching session, review
Stripe manually; the command deliberately leaves it blocked rather than risking
a duplicate payment. Resolving that exceptional condition needs operator review.

## UK bank payouts: owner setup for a future approved launch

Test mode moves no money and cannot pay your bank. ProposalQ is the merchant
selling its own subscription, so a Stripe Connect platform is not needed.

When you choose to prepare your merchant account, complete Stripe's business
verification with your actual UK business/sole-trader details and support
information. As account Owner/Admin, open **Settings → Business → Bank accounts
and currencies**, choose GBP and add a UK current account whose holder name,
account number and sort code match your bank statement. Enter bank information
directly in Stripe; ProposalQ does not need it. Stripe may require additional
verification. Choose a payout schedule in Stripe and check the Dashboard's
estimated availability and payout dates; timing depends on your account and bank.

These account-owner steps do not authorize enabling live payments in this
application. Live mode requires a separately reviewed code change (live keys
are currently rejected), live products/webhooks, appropriate tax/consumer terms,
operational acceptance and your explicit approval before deployment. Refunds and
disputes are handled by an operator in Stripe; a refund alone does not cancel
the subscription. Decide that policy before a live launch. Protect Stripe with
two-factor authentication and keep API/webhook keys in private environment storage.

Official references: [Stripe account setup](https://docs.stripe.com/get-started/account/set-up),
[UK bank payout setup](https://support.stripe.com/questions/add-a-bank-account-for-payouts?locale=en-GB),
[test payments](https://docs.stripe.com/testing),
[portal configuration](https://docs.stripe.com/customer-management/configure-portal),
[webhook verification and retries](https://docs.stripe.com/webhooks),
[idempotency retention](https://docs.stripe.com/api/idempotent_requests).
