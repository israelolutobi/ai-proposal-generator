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

`DEBUG` defaults to `False`. For development over local HTTP, explicitly set
the following environment variable or add it to `.env`:

```dotenv
DEBUG=True
```

With debug enabled, session and CSRF cookies can be used over local HTTP.
Leave debug disabled in production, where these cookies require HTTPS.
Only `true` enables debug (case-insensitive, with surrounding whitespace ignored).
`False`, missing values, and invalid values leave debug disabled.
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

Migrations `0014_ai_request` and `0015_ai_request_telemetry` must be reviewed and
applied explicitly before using AI features. Existing rows retain NULL telemetry;
no historical usage is invented. Development SQLite uses write-first transactions and fails closed
on lock/busy errors. Production requires one shared database with partial unique
constraints; PostgreSQL admission requires READ COMMITTED isolation. Run the
concurrency tests against the intended production database before release; local
SQLite tests do not prove PostgreSQL behavior.

Admission timestamps and the 10-minute lease start after the reservation INSERT
acquires the active slot. The provisional INSERT timestamps are refreshed inside
the same transaction before burst/quota decisions. A wait spanning midnight or
Monday assigns the new reservation to the UTC period when its slot was acquired.

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

**PRE-BETA deployment review:** the Procfile does not set a Gunicorn timeout.
The installed default is 30 seconds, while the AI client read timeout is 45 seconds
and is not an overall deadline. Align worker/provider deadlines deliberately before
release. This task does not change deployment timeout behavior. Open registration
also permits multiple-account allowance abuse; account controls are not a global
spending ceiling.

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
