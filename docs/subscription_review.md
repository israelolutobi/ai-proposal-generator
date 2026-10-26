# Subscription feature review

Completed locally on 26 October 2026, on `codex/free-pro-subscriptions`, based on
`codex/seo-sitemap` commit `a46ac7b6885d523a438db5d7a07cc486c0e7a785`.

## Verified results

| Check | Result |
| --- | --- |
| Full Django test suite | **899 passed**: original 818 plus 81 subscription tests |
| Isolated PostgreSQL 18 integration suite | **167 passed**, including 4 new billing races |
| Django system checks | No issues |
| Migration consistency (`makemigrations --check --dry-run`) | No changes detected |
| Dependency compatibility (`pip check`) | No broken requirements |
| Git whitespace check | Passed |
| Pricing desktop and 390px mobile viewport | Rendered; mobile cards stack with no horizontal overflow |

The full suite took 154.838 seconds; the PostgreSQL suite took 75.212 seconds.
All tests blocked external networking and used synthetic Stripe data. A test
with the real pinned Stripe SDK and a fake HTTP transport also verified request
serialization, idempotency headers and the pinned API version.

Initial restricted test runs could not write temporary static assets and caused
cascading test failures. Rerunning with normal local file access resolved them;
no existing regression tests were weakened. The PostgreSQL runner's reference to
the absent `mysite.postgres_phase_b_tests` module was removed, and all available
existing integration modules were retained.

## Review focus

- Additive migration `0019_subscriptions`; no existing data is rewritten.
- Monthly allowance is based on successful ledger results, with an in-flight
  reservation and existing account-wide admission uniqueness. Upgrade and
  downgrade do not reset usage. Calendar months and Stripe billing periods are
  deliberately distinct and disclosed on Pricing.
- Checkout identities are saved before Stripe mutations. Repeated clicks reuse
  a session, and nonterminal subscriptions block duplicate checkout creation.
  Uncertain old attempts require operator reconciliation.
- Webhooks verify the raw-body signature, deduplicate receipts transactionally
  and fetch current Stripe state under an account lock. Only an active, paid,
  current subscription on the configured price grants Pro.
- Stripe credentials/configuration use private environment variables. Billing
  defaults off, and live keys/events/objects are rejected. Existing AI approval
  and spending gates are unchanged.
- User deletion is protected for accounts with billing records, so an operator
  must resolve recurring billing before account deletion.

## Remaining account-specific setup

Follow [the complete subscription setup guide](subscriptions.md): create the
Stripe **test** product/price and portal configuration, add private test keys,
configure the signed webhook, and apply migrations to an approved local/staging
database. Complete a real sandbox Checkout/Portal/failed-renewal test using your
own account before any release approval. No Stripe account credentials were
provided during implementation, so that external end-to-end test is pending.

UK bank connection and payouts are owner-managed in Stripe. Test mode does not
transfer funds. The guide explains UK merchant verification, GBP current-account
details and payout settings. Do not enter bank information into ProposalQ.

No live payments were enabled. No changes were pushed, merged or deployed.
Existing Git stashes, private `.env` and the application database were left
untouched. The isolated PostgreSQL test server and local preview were stopped
after verification. Refund/dispute policy, tax/consumer terms and webhook
monitoring must be reviewed before a separately approved live launch.
