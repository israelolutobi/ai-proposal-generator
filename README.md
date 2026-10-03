# AI Proposal Generator

A Django-based AI tool that helps generate tailored freelance proposal drafts.

## Overview

AI Proposal Generator is a web application designed to help freelancers create more relevant and structured proposal drafts for freelance job opportunities.

The project focuses on combining user profile data, job post information, and AI-assisted text generation to produce proposal drafts that can be reviewed and edited before submission.

This project was built to strengthen skills in Django, backend development, database modelling, template rendering, and AI workflow design.

---

## Features

- Freelancer profile creation
- Job post input workflow
- Proposal draft generation concept
- Django models for storing profile and proposal-related data
- Template-based web pages
- Database migrations
- Separation of project and app logic
- Environment variable support for sensitive configuration

---

## Tech Stack

- Python
- Django
- SQLite
- HTML templates
- OpenAI API concept
- Git & GitHub

---

## Installation

Clone the repository:

```bash
git clone https://github.com/israelolutobi/ai-proposal-generator.git
```

Navigate into the project folder:

```bash
cd ai-proposal-generator
```

Install dependencies:

```bash
pip install -r requirements.txt
```

### Local configuration

For first-time setup, copy `.env.example` to the ignored root `.env` file.
Preserve any existing `.env` and add missing settings rather than overwriting it:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Set your own private `SECRET_KEY` in the environment or `.env`.
The application will not start if it is missing or blank.

`APP_ENV` defaults to `development`; accepted modes are `development` and
`production` (case-insensitive). Invalid or blank modes fail startup. Production
is never inferred from `DEBUG`. Development retains the SQLite fallback when
`DATABASE_URL` is empty and needs no proxy trust.

`DEBUG` defaults to `False`. For development over local HTTP, explicitly set
the following environment variable or add it to `.env`:

```dotenv
DEBUG=True
```

With debug enabled, session and CSRF cookies can be used over local HTTP.
Leave debug disabled in production, where these cookies require HTTPS.
Only `true` enables debug (case-insensitive, with surrounding whitespace ignored).
`False` or a missing value disables debug. All boolean settings accept only
`True`/`False`; invalid or blank values fail startup rather than being guessed.
Process environment variables take precedence over values in `.env`.
Never commit `.env` or secret values. `OPENAI_API_KEY` is needed only for AI
features; it can remain blank for non-AI development and tests.

Run migrations:

```bash
.\.venv\Scripts\python.exe manage.py migrate
```

Start the development server:

```bash
.\.venv\Scripts\python.exe manage.py runserver
```

### Controlled production configuration

The supported runtime contract is Python **3.13.x**, verified locally with
**3.13.5** and the existing pinned requirements. Django 6.0 requires Python 3.12
or later. Select and verify an available Python 3.13 patch on the deployment host;
this repository does not assume a provider's runtime-selection file. Use Linux
for Gunicorn; Windows development continues to use `runserver`. No runtime or
dependency upgrade is part of this change.

Inject production values through the deployment environment. Keep private
credentials outside Git and preserve a stable, randomly generated `SECRET_KEY`
of at least 50 characters. Placeholder/insecure/low-diversity keys are rejected.
The local `.env.example` contains development defaults: copying it alone does
**not** produce a valid production configuration.

| Setting | Controlled production contract |
| --- | --- |
| `APP_ENV` | Explicitly `production` |
| `DEBUG` | `False` (also the default); `True` prevents startup |
| `SECRET_KEY` | Private, stable random signing key; never commit it |
| `DATABASE_URL` | Explicit named PostgreSQL database; no SQLite fallback |
| `ALLOWED_HOSTS` | Explicit deployment DNS/IP hosts, comma-separated; no `*`, URLs or ports |
| `CSRF_TRUSTED_ORIGINS` | Empty for same-origin traffic, or explicit HTTPS origins without paths/credentials; never derived from hosts |
| `SECURE_SSL_REDIRECT` | `True` (production default); disabling it prevents startup |
| `SECURE_HSTS_SECONDS` | `300` initially (production default), then increase after verifying HTTPS |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | `False` initially |
| `SECURE_HSTS_PRELOAD` | `False` initially; never enabled automatically |
| `TRUST_PROXY_HEADERS` | `False` by default; explicitly `True` only under the proxy contract below |
| `OPENAI_API_KEY` | Required for enabled AI operations; not needed by configuration validation/tests |
| `PORT` | Gunicorn binds `0.0.0.0:$PORT`, default `8000`; integer 1–65535 |
| `WEB_CONCURRENCY` | Positive worker count; default one, choose using available CPU/memory |

Production always requires secure session/CSRF cookies. If HSTS preload is
deliberately enabled later, configuration requires `includeSubDomains=True` and
at least one year of HSTS. That is only a consistency check: verify all affected
domains and obtain deployment approval before extending HSTS or submitting to
preload. With the staged defaults, Django intentionally reports `security.W005`
and `security.W021`. They are not suppressed; do not enable broad/preload HSTS
just to remove warnings.

`dj-database-url` retains `CONN_MAX_AGE=600`, connection health checks and supplied
PostgreSQL options. Parsing does not connect or prove TLS: the operator must
select the provider's appropriate TLS/CA/hostname-verification policy and encode
it in the approved database configuration. No provider-specific SSL parameters
are imposed here. Configuration validation cannot prove database availability,
certificate validity or whether migrations have been deployed.

### Trusted proxy and timeout contract

When TLS terminates at a reverse proxy and Gunicorn receives HTTP, explicitly set
`TRUST_PROXY_HEADERS=True`. Django then uses `X-Forwarded-Proto: https`. This is
safe only when direct untrusted access to Gunicorn is blocked and the ingress
strips/overwrites client-supplied forwarded-scheme headers. Django cannot verify
that trust boundary. Without that flag, Django ignores the forwarded scheme;
incorrect proxy configuration can cause an HTTPS redirect loop. Development
must leave the flag false. This does not configure client-IP trust.

Gunicorn's separate forwarded-scheme inference is disabled, including its
default loopback trust, so Django's explicit flag owns that decision. Repository
startup is:

```bash
gunicorn --config gunicorn.conf.py mysite.wsgi:application
```

The shared runtime contract uses sync workers, one thread per worker, reload
disabled, a **90-second worker timeout** and **105-second graceful timeout**.
`WEB_CONCURRENCY` controls the worker count. `GUNICORN_CMD_ARGS` is rejected;
do not override these policies using extra CLI flags or another config file.
The Gunicorn startup hook checks its effective parsed settings too, so CLI
overrides cannot silently weaken the reviewed timeout/worker/proxy policy.
Provider timeouts remain connect 5 seconds and read/write/pool 45 seconds, with
zero retries. Those are phase/inactivity limits, **not a total wall-clock
deadline**; pathological calls can still outlive a worker. Existing uncertain
request accounting/lease recovery remains necessary.

The external reverse proxy/load balancer must allow **at least 120 seconds per
request**. Drain new traffic before terminating an instance and allow more than
105 seconds of infrastructure termination grace for Gunicorn's graceful drain.
Verify the actual ingress/shutdown behavior on the chosen host; application
configuration cannot enforce it. Synchronous AI requests occupy workers.

### Build, release and startup

Use separate deployment phases, from the repository root:

1. **Build:** select the approved runtime, install pinned requirements and run
   regression/PostgreSQL integration tests. With the intended production
   configuration, run `python manage.py validate_deployment` and
   `python manage.py check --deploy`.
2. **Static build:** run `python manage.py collectstatic --noinput` once. WhiteNoise
   uses `CompressedManifestStaticFilesStorage`; retain the resulting ignored
   `staticfiles/` assets and manifest in the release artifact. Never commit them.
3. **Release/pre-deploy:** deliberately run `python manage.py migrate` once against
   the approved production PostgreSQL database, after backup/migration review.
   Coordinate this step across instances; never run it in each worker.
4. **Web startup:** use the Procfile/Gunicorn command above, then perform the
   deployment operator's smoke/readiness checks before routing traffic.

`validate_deployment` checks the effective Django/runtime configuration only. It
does not connect to PostgreSQL/OpenAI, validate provider credentials, collect
assets, apply migrations, or certify infrastructure readiness. Configuration
errors identify the setting without printing its value or credentials.
Migrations, static collection and seeding never run during web-worker startup.
Stop a release if configuration, migration or static build fails. Health/readiness
endpoints and infrastructure rollback/backup procedures remain separate tasks.

AI calls go through `proposal_ai/services.py`. Existing prompts, models and
API styles are preserved. Requests use a 5-second connection timeout and
45-second read/write/pool timeouts (network inactivity limits, not an overall
deadline). SDK retries are disabled; users can retry a failed request.

Run tests with `.\.venv\Scripts\python.exe manage.py test`. The configured
test runner clears AI credentials and blocks networking, so tests need no
OpenAI key. Mock the application service functions in view tests; service tests
use mocked SDK clients or in-memory HTTP transports. Tests run serially;
parallel execution is rejected until worker network guards are implemented.

Beta AI limits are defined in `proposal_ai/ai_limits.py`, separately from storage:
pasted/confirmed descriptions 20,000 Unicode characters, job skills 2,000;
summary title/skills 255/2,000; profile summary sent to AI 5,000. Selected experience
tasks/skills/depth allow 4,000/1,000/2,000 characters; role/company allow 255 each.
Choose up to 10 owned experiences (8,000 characters per formatted record and
20,000 total). All are initially selected only when they fit; otherwise explicitly
choose a subset. Stored records are never deleted or truncated by selection.
Formatted profile/job context ceilings are 6,000/24,000 characters. Final summary,
extraction and proposal requests allow 4,000/24,000/60,000 characters, with a shared
128,000 UTF-8 byte backstop. Fields are outer-trimmed; constructed context and
requests count their exact formatting. Browser counters count Unicode code points;
server validation is authoritative.

Chat completion caps are 2,048/8,192/6,144 tokens for summary/extraction/proposal,
including reasoning. Visible summaries/proposals allow 2,000/8,000 characters.
Only complete (`finish_reason="stop"`) responses are accepted. Extraction raw JSON
is limited to 281,000 characters before parsing: the known schema's 23,310 bounded
string characters may each take 12 characters as escaped non-BMP JSON, plus keys,
budgets and normal formatting. Arbitrarily padded/extra JSON is bounded too.
Over-limit input or incomplete/oversized output fails without partial persistence,
silent truncation or automatic retries. These provisional caps are not exact input
token budgets or measured latency guarantees. Evidence ranking remains separate
work. The unused legacy Responses helper is not an active workflow.

Beta request controls are in `proposal_ai/ai_control.py`, using one durable
`AIRequest` ledger. Summary/extraction/proposal attempts reserve 1/2/3 credits;
both 25 credits per UTC calendar day and 100 per Monday-based UTC week apply.
Burst limits count dispatched attempts: 3 summaries per 10 minutes, 3 extractions
per 5 minutes, 2 proposals per 5 minutes. Only one active operation per account
is permitted. These are Beta allowances, not provider cost estimates.

Forms use signed, server-issued UUID nonces valid for 24 hours. Replaying an
admitted nonce cannot dispatch again; changed submitted/effective input conflicts.
Successful extraction/proposal replays reopen existing records. Summary text is
not stored in the ledger, so a lost summary needs an explicit new generation.
Fresh nonces can regenerate identical content; there is no content-deduplication
window. Errors preserve forms, with 429 for allowance/burst rejection, 409 for
active/replayed/conflicting requests, and 503 for coordination failure.

Credits are released for known local pre-dispatch failures and definitive provider
authentication, capacity or invalid-request rejections. Dispatched rejections still
count toward burst limits. Timeouts, ambiguous connection/server failures, invalid
outputs and failed local persistence consume credits. No automatic retry occurs.
Active leases last 10 minutes; undispatched stale reservations release credits,
dispatched stale requests become uncertain/consumed. Late workers cannot persist
after losing their request state. The ledger stores hashes and references, never
prompts, private inputs or generated text. Automatic deletion is not implemented;
telemetry retention duration remains a separate pre-Beta/privacy-policy decision.

AIRequest also records nullable, privacy-safe provider telemetry: requested and
reported model identity, Chat Completions token counts, optional reasoning/cached
counts, service tier, completion cap, finish reason and monotonic provider-call
latency in integer milliseconds. Missing or malformed optional usage stays unknown
(NULL); genuinely reported zero stays zero. Completion tokens already include
reasoning tokens. Provider-call latency excludes admission, client construction,
application persistence and rendering. `response_text_characters` counts Python
characters in the raw returned completion text, including rejected responses when
available; extraction counts returned JSON text, not visible prose. No completion
text, provider request IDs, raw responses or errors are stored as telemetry.

Services return immutable validated-value/scalar-telemetry objects. Conditional
ledger writes preserve received provider evidence before application persistence,
including when saving a JobPost/Proposal fails; expired/terminal workers cannot
overwrite telemetry or persist results. Staff with AIRequest view permission can
inspect the ledger in Django admin; adding, changing, deleting and bulk mutation
are disabled, and nonces/fingerprints are excluded from its display.

Measured provider usage is separate from product quota credits. Monetary estimates,
pricing snapshots and billing calculations are not implemented. Requests without
reported usage cannot be treated as zero cost. The ledger counts admitted requests,
not every rejected form/allowance attempt; killed workers may leave usage unknown.

Migrations `0014_ai_request`, `0015_ai_request_telemetry` and
`0016_ai_global_exposure` must be reviewed and
applied explicitly before using AI features. Existing rows retain NULL telemetry;
no historical usage is invented. Development SQLite uses write-first transactions and fails closed
on lock/busy errors. Production requires one shared database with partial unique
constraints; PostgreSQL admission requires READ COMMITTED isolation. Run the
concurrency tests against the intended production database before release; local
SQLite tests do not prove PostgreSQL behavior.
SQLite contention may reject both competitors with controlled failures and no
dispatch; local concurrency tests verify this fail-closed outcome as well as a
single winner. PostgreSQL integration tests require a winner when capacity permits.

Admission timestamps and the full 10-minute lease start after the reservation
INSERT acquires the active slot and, with global controls enabled, after both
global period locks are acquired. The provisional INSERT timestamps are refreshed
inside the same transaction before all burst/quota decisions. A wait spanning
midnight or Monday rolls back/restarts admission against the final UTC periods;
database-only restarts are bounded and never retry provider work.

`AI_ENABLED` is a server-side availability switch. Development defaults to True;
explicit production defaults to False. Only True/False (case-insensitive) are
accepted; supplied blank/invalid values fail startup. Disabled AI returns a safe
503 for new generation, while saved product areas and completed extraction/proposal
replays remain available. Summary replay retains its existing lost-output behavior.
Checks run before admission, after lock waits, immediately before dispatch and at
the SDK boundary, including the unused Responses helper. A disabled undispatched
reservation releases capacity; already-started paid work is not cancelled/refunded.

`AI_GLOBAL_DAILY_CREDITS` and `AI_GLOBAL_WEEKLY_CREDITS` provide separate service-wide
product-credit ceilings. **Actual production thresholds remain unselected and
require human approval.** Both are required when production AI is enabled; both
may be omitted in development or with production AI disabled. If either is supplied,
both must be valid decimal integers from 0 through 2147483647. Zero admits no new
paid work, and does not promise a reset retry. Negative, blank, malformed and
unlimited sentinel values are rejected. There is no required daily/weekly ratio.
Local omission disables only global enforcement; existing account controls remain.
Product credits do not guarantee a monetary spend ceiling.

`AIQuotaPeriod` holds durable reserved/consumed totals for UTC days and Monday-based
UTC weeks, with immutable request bindings on the authoritative AIRequest ledger.
Admission claims the ledger first, then locks periods in `(kind, period_start, pk)`
order. Both reservations are committed together. Success/ambiguous paid failures
consume; definitive releasable failures and undispatched stale requests release.
Lifecycle and counter transitions commit together, exactly once. No provider I/O
occurs inside these transactions. Recorded non-NULL period limits must match worker
configuration; conflicts fail closed without overwriting policy. Changing a current
period's limit is not an automatic live configuration operation.

Deploy 0016 as an operator/release migration with old AI-writing workers stopped,
before enabling new workers. It binds historical requests using their recorded
UTC admission time and aggregates only recorded reserved/consumed credits; released
requests add no credits. It preserves lifecycle/telemetry and leaves historical
limits NULL, initialized under lock only if a period becomes current for admission.
Do not run migrations during worker startup. Back up and review this bootstrap
before a real deployment; development/test rehearsals do not migrate real data.

For read-only operator inspection, run `python manage.py ai_status`. It reports this
process's policy, current period totals/reset times and safe ledger discrepancy
warnings; it never creates periods, repairs counters, recovers requests or calls
providers. Period admin is inspection-only under normal staff/model permissions.
Automatic admission recovery sweeps at most 50 expired accounts, each in a separate
ledger-first transaction before new-period locking. Explicit recovery is a separate
mutating command: `python manage.py recover_ai_requests --limit 50` (1–1000).
Recovery always adjusts the original bound periods, never the recovery period,
and never redispatches. Current/active periods and request bindings must be retained;
formal retention remains separate. Account/ledger deletion does not guess refunds
or lower durable counters; a discrepancy requires operator investigation.

An environment switch is process-local, not a dynamic distributed toggle. To stop
AI fleet-wide, set `AI_ENABLED=False`, replace/restart every web worker/replica,
verify each adopted configuration (including read-only status), and drain existing
in-flight work. Do not remove the API key as an operational shutdown mechanism.

PostgreSQL integration tests are separate from ordinary `manage.py test` discovery.
Use a dedicated disposable PostgreSQL cluster with a `proposalq_task4b` database
on an explicit loopback port. Set `PROPOSALQ_POSTGRES_TESTS=1`,
`PROPOSALQ_POSTGRES_TEST_DATABASE_URL` to that test-only connection, and
`PROPOSALQ_TEST_PG_DATA_DIR` to its server data directory, then run:

```powershell
.\.venv\Scripts\python.exe -B scripts/run_postgres_tests.py
```

The launcher verifies the cluster identity, rejects additional databases, requires
READ COMMITTED, and creates/destroys `test_proposalq_task4b`. It never chooses a
connection from `.env` or ordinary `DATABASE_URL`. Providers remain mocked and the
network guard stays active. CI can use an isolated PostgreSQL service published on
a loopback port; require this suite before deployment promotion. Cluster setup and
cleanup remain explicit operator steps. No CI infrastructure is added here.

## Controlled Beta access

The initial hosted Beta is for approximately ten manually vetted ordinary Django
users. Public registration is closed. No invitation system, separate AI
entitlement, email verification or public password-reset service is introduced.

`APP_ENV` remains the authoritative environment mode. In development,
`REGISTRATION_ENABLED` defaults to `True`; an explicit `False` closes signup for
local testing. In production it defaults to `False`, and explicit `True` is a
configuration error during controlled Beta. Values are case-insensitive and
trimmed, but blank or values other than `True`/`False` fail startup. Closed
registration returns HTTP 403 for GET, HEAD and valid-CSRF POST, with a login
link. Public signup links disappear. Existing active users can still log in and
use their saved data. `validate_deployment` reports the effective closed policy
without inspecting users, connecting to the database or calling a provider.

### Provisioning and onboarding

Use the protected Django admin and Django's password mechanisms:

1. Verify the tester through the approved external contact process.
2. Create an ordinary user in the admin and set a strong initial password.
3. Add the vetted email address on the user edit page if required.
4. Confirm `is_active=True`, `is_staff=False`, `is_superuser=False`, and no
   administrative permissions or permission-bearing groups.
5. Deliver onboarding credentials through an appropriately protected external
   channel. Keep plaintext credentials out of logs, documentation and source.
6. The tester logs in and uses **Change password** in the authenticated navigation
   (`/change-password/`) to replace the onboarding password.
7. The tester completes their ProposalQ profile.

Do not use `createsuperuser` for Beta testers. Do not run `seed_demo_data` in hosted
Beta. Testers need no staff privileges. Only approved operators should be able to
provision users through admin.

**Before production AI is enabled, review the actual production User population.**
Every active account must be intentionally approved for controlled Beta. Local
database accounts do not establish the production population. Review historical
and operator accounts too; do not automate deletion to perform this review.

All active authenticated accounts receive the existing AI policy when globally
enabled. Registration closure does not replace `AI_ENABLED`, per-user quotas,
burst limits, global ceilings or idempotency. Production global AI thresholds
remain **UNSELECTED** until separately approved; this task selects no values.

### Password changes, recovery and revocation

The authenticated password-change page requires the current password, CSRF and
Django password validation. It preserves the changing browser's session; other
sessions carrying the previous authentication hash lose authentication when
subsequently checked. Passwords are hashed and are not rendered back into forms.
It does not send email or provide public password recovery.

For recovery, an approved operator verifies the tester through the external
contact process and uses the admin's supported password-change mechanism to set
fresh credentials. Deliver them through the protected channel, then have the
tester change the replacement password after login. Do not send plaintext
passwords through application logs or add credentials to shell command arguments.

For revocation, set `User.is_active=False` and also invalidate the existing
credentials using Django's supported admin password mechanisms (for example,
disable password-based authentication). Deactivation blocks future authentication
and subsequent protected requests, including future AI admission. Credential
invalidation prevents an old browser session from becoming authenticated again
if the account is later reactivated. Existing database session rows may remain;
do not rely on deactivation alone to permanently invalidate their authentication
hash. Reactivation must use freshly controlled credentials and operator review.

Do not delete the user to revoke access: owned application records and AI history
must remain. Already-dispatched AI work is not retroactively cancelled; existing
accounting, stale recovery and fencing remain authoritative. A request already
authenticated before deactivation may finish.

### Hosted access protection release gates

Before internet-accessible Beta, trusted ingress/platform protection must cover
at least `/login/` and `/admin/login/`. Establish trustworthy client-IP handling
before using IP limits, and never trust arbitrary client-supplied
`X-Forwarded-For`. Protect admin through an appropriate operator-only
ingress/authentication boundary where supported. These are infrastructure release
requirements, not application throttling implemented here. The selected
infrastructure and its actual protection remain **UNKNOWN** until verified.

**PRE-BETA deployment review:** verify the selected production PostgreSQL database,
TLS/proxy isolation, upstream timeouts and graceful draining against the contract
above. Gunicorn now uses the reviewed 90/105-second policy; provider inactivity
timeouts still do not create an overall deadline. Keep production registration
closed and review the active account population; account controls are not a global
spending ceiling. The global controls above require approved limits and verified
fleet-wide configuration before hosted AI Beta. Health checks, backups, pricing
and telemetry retention remain separate reviewed tasks.

---

## Project Status

Active development.

This project is currently being developed as a portfolio project focused on applied AI, freelance workflow automation, and backend web development.

---

## Future Improvements

- Improve proposal generation logic
- Add user authentication
- Add proposal editing and saving
- Improve UI styling
- Add OpenAI API integration safeguards
- Add testing
- Deploy a live demo
- Add screenshots and usage examples

---

## Notes

This project is intended as a portfolio and learning project. Sensitive configuration such as API keys and environment variables are excluded from version control.
