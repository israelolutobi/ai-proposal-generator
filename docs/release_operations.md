# Controlled Beta release operations

This is an application runbook, not confirmation of any hosting capability.
The Linux host, PostgreSQL/TLS, ingress, backups, restore process, draining,
probe timing and log collection must be verified by the deployment operator.
Production global AI thresholds remain **UNSELECTED** until separately approved.
Do not enable public registration or seed hosted demo accounts.

## Release sequence

1. Build an immutable Linux artifact using the documented Python 3.13.x runtime
   and the exact pinned dependencies. Do not upgrade them during a release.
2. Run the network-guarded ordinary tests and the explicitly enabled PostgreSQL
   suite against a designated disposable cluster. The launcher never falls back
   to an ordinary `DATABASE_URL`; it also runs the shared release/schema tests.
3. Run `python manage.py collectstatic --noinput` once during build. Retain the
   generated `staticfiles/` directory and manifest in the artifact; do not commit
   it. Build tests must render representative public/login/admin templates.
4. Run `python manage.py validate_deployment` with the approved production
   configuration, and `python manage.py check --deploy`. The staged HSTS policy
   intentionally retains W005/W021; do not suppress them or enable preload just
   to remove the warnings. Configuration validation performs no infrastructure
   probe and does not prove provider credentials are valid.
5. Verify a usable backup, the recovery owner, the rollback artifact and a tested
   restore procedure before any schema-changing release over existing data.
6. Drain traffic and stop incompatible old AI-writing workers before migration.
   Stop alternative writers too. Do not assume an environment flag has changed
   already-running processes; verify fleet-wide adoption of `AI_ENABLED=False`.
7. Run `python manage.py migrate --noinput` exactly once through one coordinated
   release/pre-deploy process against the explicitly approved production DB.
   Never migrate in each worker or run concurrent release migrations. If it
   fails, hold traffic and inspect history/schema; do not fake or silently repair.
8. Run `python manage.py validate_release`. It checks effective configuration,
   connectivity, all required application/framework migrations, managed schema
   columns/key/constraint/index metadata, and representative manifest assets.
   It performs no migration, provider request, seeding, recovery or state repair.
9. Start `gunicorn --config gunicorn.conf.py mysite.wsgi:application`. Startup
   never migrates, collects static assets, seeds or recovers AI requests.
10. Verify `/health/live/`, `/health/ready/` and representative static HTTP assets
    on every new instance before promotion. Supply the approved host/scheme.
11. Perform the non-mutating smoke checks below. Do not generate AI content.
12. Promote traffic only after all gates pass.
13. Observe sanitized stderr logs and `python manage.py ai_status`. Resolve
    accounting/schema warnings before enabling AI or declaring release success.

### Migration 0016

0016 binds historical AIRequest rows to UTC daily/weekly periods and bootstraps
reserved/consumed counters from recorded product-credit evidence. Old AI-writing
code does not update the new counters and must not keep writing across this
migration. The additive schema alone does not make mixed-version AI operation
safe. Use a maintenance/drain window, one migration owner, and a PostgreSQL
rehearsal with old writers stopped. Verify bootstrap bindings/counters in a
quiet database; `ai_status` is a multi-query snapshot, not an automatic repair.

## Health and release checks

`/health/live/` is anonymous GET/HEAD, returns minimal 200 output, and performs
no application database query, migration inspection, template or provider work.
`/health/ready/` is anonymous GET/HEAD and returns generic 200/503 based on
configuration, database and schema readiness. Neither endpoint changes data;
both are non-cacheable. Unsupported methods are rejected, and normal CSRF,
host/HTTPS/proxy/security middleware remains enabled.

Readiness requires all migrations needed by this running code, including Django
auth/session state. It also checks required managed tables/columns, primary and
foreign keys, unique fields, named constraints and indexes. It does not prove
that arbitrary hand-edited constraint expressions, column types or historical
bootstrap contents are correct. The release migration/concurrency tests and
operator bootstrap review remain necessary; do not use `--fake` as a shortcut.

`AI_ENABLED=False`, provider outages and exhausted user/global allowances do not
by themselves make ordinary application readiness fail. Probes never call a
provider or create quota periods. Full static checks belong in `validate_release`
and build/HTTP smoke verification rather than every readiness request.

Readiness is not a total wall-clock deadline. No new DB/provider timeout policy
is introduced here. Verify finite PostgreSQL connection/query/network failure
behavior with the chosen provider and configure probes accordingly. Django
connection maintenance also participates in request handling. A synchronous
worker occupied by AI can delay an HTTP probe: rehearse long mocked requests,
database outage, and draining before choosing restart thresholds. Readiness
failure should remove traffic, not automatically trigger a restart loop.

The reviewed worker timeout remains 90 seconds and graceful timeout 105 seconds.
The external request timeout must be at least 120 seconds. Infrastructure shutdown
grace must exceed 105 seconds with margin after SIGTERM, and traffic must drain
before termination. Confirm signal delivery to Gunicorn and actual platform
behavior. Graceful shutdown cannot guarantee completion of every provider call.

## AI status and stale recovery

`python manage.py ai_status` is read-only. It reports this process's policy,
period accounting and expired/unbound active requests. Verify every replica's
configuration; one invocation does not establish fleet-wide shutdown.

`python manage.py recover_ai_requests --limit 50` is an explicit mutating command,
not a health or startup action. Run only after the required AI schema exists.
It handles expired leases: known-undispatched work releases; dispatched work
becomes uncertain/consumed. It updates the original bound quota periods and
never calls a provider or creates JobPost/Proposal content. AI may stay disabled.

After a deployment/worker incident, inspect status, allow leases to expire, run
bounded recovery, and inspect again. Repeat batches if more stale work remains;
the supported limit is 1–1000 but start at 50. Earlier accounts can commit before
a later batch error; reruns are safe. If errors or discrepancies persist, stop
and inspect retained evidence rather than reducing counters. Concurrent recovery
is database-coordinated, but one operator/scheduled owner is simpler initially.
Admission already performs bounded recovery; no queue is required. A platform
schedule can be approved later. Never automatically retry provider work.

## Backups, restore and rollback

The provisional recommendation is automated encrypted daily PostgreSQL backups,
at least 14 days retention, a pre-schema-release backup, an isolated restore
rehearsal and a named recovery owner. These are recommendations requiring
operator approval, not confirmed infrastructure. Document accepted data-loss
and recovery-time targets; require PITR if daily recovery cannot satisfy them.
Restrict backup access, monitor failures, and protect credentials separately.
Backups contain auth/session and private application data, not merely telemetry.
Deletion may not remove older backup copies immediately; retention remains a
separate reviewed policy, not an application cleanup feature in this task.

Restore a backup into a disposable isolated PostgreSQL target first. Never
overwrite production as a rehearsal. Keep AI disabled, restrict access and block
provider/email execution. Verify migration history, table counts, auth and
application access, references, constraints, ledger bindings and quota-period
consistency without dumping usernames, hashes or private content to logs.

An older backup can lose AI requests, dispatch markers, telemetry, consumed
credits and nonce identities. Signed forms may remain valid while their request
row is missing, and a restored "undispatched" row might have dispatched after
the backup. Restoring a database cannot undo provider work. **Keep AI disabled
until uncertain accounting and nonce/session cutover are reconciled and approved.**
Do not blindly run recovery on restored dispatch evidence, guess refunds, invent
missing measurements, rotate SECRET_KEY casually, or regenerate missing content
automatically. Re-review restored accounts/revocations/credentials too. If no
surviving evidence exists, the owner must approve a conservative restart plan
before re-enablement; waiting out affected quota/nonce windows may be required.

Prefer roll-forward or a tested compatible application rollback while retaining
additive schema. Reversing 0014–0016 destroys request, telemetry or accounting
evidence and is not routine rollback. SQL compatibility does not make older
code safe: revisions predating global controls/closed signup can bypass policy.
An old revision may not honor the AI switch. Keep traffic gated until a compatible
artifact is verified. Database restore is a separate data-loss incident process.

## Non-mutating smoke checks and incident actions

Verify HTTP-to-HTTPS behavior, approved host, health GET/HEAD, public home/login,
closed registration (403), protected-route login redirects, security headers,
application CSS/logo and an admin asset. Initial anonymous checks need no new
user. An approved operator/tester may check an existing authenticated session;
do not create synthetic profiles/jobs/proposals or perform paid AI generation.

Trusted ingress protection for `/login/` and `/admin/login/`, trustworthy client
IP handling, backend isolation, header rewriting, TLS and an operator admin
boundary remain release gates. Do not trust arbitrary forwarded headers. Review
the actual approved production user population before enabling AI; local users
do not prove that review. No extra entitlement or public signup is introduced.

For failed releases/migrations, hold traffic and preserve evidence. For DB
outage, readiness returns 503; restore connectivity and inspect expired AI work.
For provider outage/runaway exposure, disable AI across the fleet and inspect
status while ordinary product areas remain available. Do not casually edit
current-period thresholds: immutable period policy can reject a mismatch.
For compromised tester/operator accounts, use supported deactivation/credential
invalidation and platform ingress controls while preserving history. All actual
platform restrictions, backups and restore actions require operator support.

Production operational stderr events include only bounded category, event,
timestamp, severity and an optional numeric HTTP status. Messages, exception
payloads, bodies, private content, credentials, session/CSRF values, nonces and
fingerprints are not formatted. Existing provider payload suppression stays in
place. `ai_status` and explicit recovery summaries are restricted operator tools;
public probes expose only minimal availability. Log collection/retention must
be verified separately. Task 4G pricing/retention/reporting is not implemented.
