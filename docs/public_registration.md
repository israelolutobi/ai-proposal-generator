# Phase A: safe public registration with AI disabled

Target branch: `codex/research-intelligence-reviewed`. This is a proposed release;
this change does not merge, deploy, change billing or mutate the hosted database.

## Release policy and account audit

Production can use either `REGISTRATION_ENABLED=False` (controlled Beta) or
`REGISTRATION_ENABLED=True` (public signup). Startup, `validate_deployment`,
`validate_release`/readiness and the coordinated release script require
`AI_ENABLED=False`. Existing HTTPS/proxy, secret-key, host, PostgreSQL/TLS,
runtime, migration/schema and collected-static checks remain enforced.

Registration uses Django's UserCreationForm and all four configured password
validators. Signup requires an email, validates usernames, hashes passwords and
grants only an active ordinary account. Submitted privilege/ownership fields are
ignored. CSRF covers signup, login, logout and mutations. Password fields are
marked sensitive; passwords are not rendered back into forms. Login uses Django
AuthenticationForm, returns generic unknown/inactive/password errors, rotates
the session and rejects inactive users. Logout requires POST to end the session.
Password changes require the current password and invalidate other sessions.

Profiles, experiences, jobs, proposals, submission confirmations and outcomes
are scoped to `request.user`; foreign record IDs return 404. Existing suites
cover profile/experience selections and submission/outcome mutations; new public
account tests repeat foreign-ID GET/POST isolation. Admin requires staff rights.

Public signup normalizes email to lowercase and reserves it in a new unique
`RegisteredEmail` table in the same transaction as user creation. Racing public
signups cannot create two accounts for that email; the losing transaction rolls
back and returns a form error. Existing emails are checked without rewriting
legacy users. This is NOT a universal uniqueness constraint on `auth_user.email`:
admin/direct ORM edits can bypass reservations; operators must avoid concurrent
provisioning or editing of duplicate emails. Legacy duplicates are not modified.
Exact usernames retain Django's database uniqueness; the existing case-insensitive
username validation is form-level and concurrent differently-cased usernames can
still pass it. Login remains username-based and case-sensitive; this does not
grant another account's permissions or data.

## Abuse controls

Mandatory production middleware counts CSRF-valid POST attempts before password
authentication or signup. It also covers the Django admin login and authenticated
password change. Shared PostgreSQL rows and transactional locks work across
workers and instances. Database failure returns sanitized 503, never an unlimited
fallback. Rate-limit responses return 429 with `Retry-After` and `no-store`.

| Budget | Limit | Interval |
| --- | ---: | --- |
| Signup, whole service | 200 attempts | 1 hour |
| Signup, socket peer | 20 attempts | 1 hour |
| Login/admin/password change, socket peer | 30 attempts | 10 minutes |
| Login/admin username, or password-change user ID | 10 attempts | 10 minutes |

These are attempt limits, including invalid and successful forms. They use an
interval beginning with the first attempt; expiry restores the allowance.
Counters store HMAC digests, counts and expiry, with no passwords/raw IPs or
usernames. There is no per-process cache. Changing SECRET_KEY resets the identity
of counters and existing sessions, so do not rotate it for this release.

The socket peer (`REMOTE_ADDR`) is used; arbitrary `X-Forwarded-For` and similar
headers are ignored. Render ingress can make unrelated visitors share the peer
budget. This deliberately conservative policy can block legitimate users behind
the same ingress/NAT. Before increasing public traffic, verify the actual ingress
header rewriting/isolation contract and add a separately reviewed client-IP
policy if needed. Do not switch to blindly trusting the first forwarded address.

Run `python manage.py prune_account_rate_limits` as periodic operator maintenance
(e.g. daily) after review. It deletes only expired counters. This PR does not
create a scheduled job or paid resource. Startup/readiness never prune or migrate.
Monitor 429/503 counts and database growth using sanitized operational monitoring.

## Remaining protections and product limits

Email ownership is not verified. There is no verification/resend flow, CAPTCHA,
MFA or self-service password reset. Signup grants immediate account access; email
must not be treated as proof of identity or used for privileged entitlements.
Email/username duplicate validation can reveal account existence. Rate limits
reduce enumeration/credential stuffing but do not prevent distributed abuse or
deliberate temporary account lockout. A distributed attacker can exhaust the
global signup cap. [Render edge DDoS protection](https://render.com/articles/how-render-handles-ddos-attacks)
does not replace these application rate limits.

AI endpoints retain their kill switch and provider-boundary checks; public
accounts cannot generate summaries, extract jobs or generate proposals while
AI is disabled. Manual profiles, experiences and saved-data access still work.
Production AI activation requires a separate review of the newly public user
population and entitlements. No API credential is added, changed or printed.

## Exact Render settings

The documented existing hostname is `proposalq-beta.onrender.com`. If the actual
service has a different hostname or approved custom domain, replace the hostname
and matching HTTPS origin together; do not use wildcards.

| Variable | Public release value |
| --- | --- |
| `APP_ENV` | `production` |
| `DEBUG` | `False` |
| `REGISTRATION_ENABLED` | `True` |
| `AI_ENABLED` | `False` |
| `AUTH_RATE_LIMIT_ENABLED` | `True` |
| `ALLOWED_HOSTS` | `proposalq-beta.onrender.com` |
| `CSRF_TRUSTED_ORIGINS` | `https://proposalq-beta.onrender.com` |
| `TRUST_PROXY_HEADERS` | `True` |
| `SECURE_SSL_REDIRECT` | `True` |
| `SECURE_HSTS_SECONDS` | `300` |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | `False` |
| `SECURE_HSTS_PRELOAD` | `False` |
| `PORT` | `10000` |
| `WEB_CONCURRENCY` | `2` |
| `PYTHON_DOTENV_DISABLED` | `1` |
| `SECRET_KEY` | Keep the existing private random key; never paste it into reports |
| `DATABASE_URL` | Keep the existing direct authenticated Neon PostgreSQL URL |
| `GUNICORN_CMD_ARGS` | Unset/empty |
| `AI_GLOBAL_DAILY_CREDITS`, `AI_GLOBAL_WEEKLY_CREDITS` | Leave both unset, or preserve an existing valid pair; these do not enable AI |
| Provider API credentials | Leave unchanged; no new credentials are needed |

The start wrapper preserves the direct Neon target and enforces `sslmode=verify-full`
with certifi CA verification. Session/CSRF secure cookies follow production mode.
The staged HSTS policy deliberately leaves Django `security.W005` and
`security.W021` warnings visible; do not enable subdomain/preload policy merely
to suppress them. Render branch must be `codex/research-intelligence-reviewed`
after this PR is reviewed and merged by the operator. Automatic deployments stay
off; plan/billing, region, shutdown and health paths are unchanged. The Blueprint
continues to default registration to False so importing it does not open signup.

## Operator release sequence (not executed by this task)

1. Review the PR, backups and remaining limitations; keep registration False and
   AI False while applying the reviewed release.
2. Use the existing coordinated `scripts/local_release.py` procedure against the
   approved direct Neon target. It intentionally migrates with signup closed.
   Migration `0018_account_security` creates only new security tables/indexes;
   it does not backfill or update existing users. Never migrate at worker startup.
3. Build the reviewed image and collect static assets. After migrations, use the
   settings above for the manually approved public release. Do not change billing.
4. Confirm startup succeeds through `validate_deployment`, authenticated database
   TLS and read-only `validate_release`; independently verify `/health/ready/`
   returns 200 (Render retains the existing `/health/live/` health path).
5. Perform an approved synthetic signup/login/logout smoke, check ordinary
   privileges, protected routes/foreign IDs, secure cookies/CSRF, static assets,
   and that all three AI operations refuse generation. Do not use real paid calls.
6. Roll back access by setting `REGISTRATION_ENABLED=False` and manually restarting
   all workers, keeping AI False. Existing accounts/sessions remain usable.
   Keep migration 0018 applied: old code can ignore these additive tables.

## Automated validation

See the PR for executed test counts. Ordinary Django tests use the existing
network-blocking runner and empty provider credentials, including AI regression
tests with synthetic switches/mocks. PostgreSQL tests run only on an explicitly
identified disposable loopback cluster and prove shared-counter admission and
case-insensitive email arbitration across separate database connections. No
hosted database, Render configuration, billing or provider credential was touched.
