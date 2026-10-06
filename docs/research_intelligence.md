# ProposalQ research intelligence

The private master workbook is an operator input. It is never committed, served,
or opened during a customer request. Importing a snapshot creates structured
PostgreSQL tables for cases, requirements, notes and indexed opportunity terms.

Every confirmed proposal generation queries the currently active snapshot with
the validated JobPost fields before prompt-budget validation and quota admission.
SQL ranks positive term overlap, breaking ties by case key, and returns at most
eight cases. Title/skill tokens precede description tokens (maximum 64).
Outcome and record completeness never affect relevance. This lexical MVP may
retrieve broad adjacent matches and miss synonyms; replace the retrieval module
later without changing ingestion or generation controls.

The internal context is under 4,000 characters. It contains closed categories and
counts for routes, outcomes, coverage, source fidelity, observations, hypotheses
and recorded confounds. No arbitrary research prose, names, raw historical
proposals, participant identifiers or historical freelancer claims enter the
prompt. The stored structured observations and hypotheses remain separate with
confidence and non-causal cautions. Outcomes remain observational, never evidence
of causation or customer success probabilities. Exact enriched provider messages
are validated and fingerprinted by the existing quota/idempotency machinery.
A missing active snapshot or unavailable research database returns a safe 503
before reservation/provider dispatch. An active snapshot with no relevant cases
still allows generation using the current freelancer/job facts.

## Import contract

Required sheets: Cases, Job Breakdown, Proposal Breakdown, Requirement Coverage,
Research Notes. Headers must be in physical row 3. Required headers, unique case
IDs, exactly one job/proposal breakdown per case, child references, note types,
numbers and model field limits are checked. Formulas in imported tables are
rejected; other source sheets (including raw Evidence) are ignored. Private
structured text stays in operator-only database tables. Participant labels are
hashed for linkage, not sent to customers. Source SHA-256 identifies snapshots.

Imports use one transaction and a PostgreSQL advisory lock; a partial unique
constraint allows only one active snapshot. The old snapshot remains active on
failure. Exact reimports are harmless and never reactivate a retired snapshot.
New snapshots are visible to the next generation without a restart or code edit.
In-flight generations retain the context they already retrieved. Reusing a nonce
after a snapshot change can correctly conflict because the effective prompt has
changed; submit with the newly issued nonce. No AI request is automatically retried.

Validate only (rolls back all import writes):

    python manage.py import_research_workbook /private/master.xlsx --validate-only

Import on the explicitly configured development PostgreSQL database:

    python manage.py import_research_workbook /private/master.xlsx

## Secure Neon release

Use the reviewed checkout and the existing release process. Check backup/restore
and stop incompatible writers before changes to an existing database. Render
must retain AI_ENABLED=False, registration disabled and automatic deploys off.
Never add provider keys or select global credit thresholds for this release.

    python -B scripts/local_release.py --research-workbook /private/master.xlsx

Add --create-owner only for initial setup if needed. The helper prompts for the
approved direct Neon URL and production signing key without echo, applies Django
migrations once through scripts.release, validates schema/static readiness,
validates/imports the snapshot, and prints sanitized counts and retrieval smoke
results. Production imports require PostgreSQL and disabled AI. Secrets are not
written to files, command arguments, source, or logs. Use the database name
proposalq_beta and hostname proposalq-beta.onrender.com for this beta target.

After successful migration/import, select the reviewed branch/commit in Render
and manually deploy. Verify exact-commit deployment, health live/ready GET and
HEAD, HTTPS redirect, home/login, closed registration, protected routes, static
assets and owner login. Paid generation is excluded from hosted smoke checks.
Rollback keeps the additive research schema and compatible code; do not reverse
AI-ledger or research migrations as a routine rollback.

## Verification

Ordinary tests run with network access blocked and provider credentials cleared.
The synthetic AI switch exists only inside that test guard; deployed AI stays off.
The PostgreSQL launcher verifies a designated loopback disposable cluster and
runs existing admission/concurrency/global-exposure/release suites plus research
retrieval/import and concurrent snapshot activation tests. Workflow tests run the
actual view, retrieval, budget/admission, service and save boundary against a
mocked SDK; the exact provider prompt fingerprint, telemetry, charged quota and
idempotent replay are asserted. The real private workbook is imported only in
explicit operator/development verification, never as a regression fixture.
