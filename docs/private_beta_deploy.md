# Controlled Beta: Render Free + Neon Free

Deploy the actual Django application as one Render Free Docker web service and
one NEW Neon Free PostgreSQL database. Never create a Render PostgreSQL database,
upload local SQLite, add paid resources, or enter payment information. AI and
registration remain disabled. The public HTTPS home/login pages use Django
authentication to protect application data; this is not a private network/VPN.

## Account setup and secrets

1. Sign in to Neon and create a Free project/database, PostgreSQL 17 preferred,
   in an EU region near Render Frankfurt. Confirm the dashboard says Free and
   does not require payment. Use the **direct** connection URL, not the pooled
   hostname. Keep the URL in a password manager and provider secret UI only.
2. Sign in to Render, authorize this GitHub repository, and create a Blueprint
   from branch `codex/p0-private-beta-deploy` using `render.yaml`. Confirm the
   web service plan is Free and the Blueprint creates **no database**. It is JSON
   formatted YAML, supported by YAML parsers. Automatic deployments are off.
3. Generate a stable production `SECRET_KEY` with a password manager (at least
   64 random characters), save it privately, and enter it and the direct Neon
   `DATABASE_URL` through Render's secret environment UI. Render's default
   generated 256-bit Base64 value is shorter than our 50-character minimum;
   do not use it. Never upload `.env`, place secrets in command arguments, or
   paste them into chat. A first deploy may fail the schema gate until step 4.
4. From the exact approved deployment checkout, run the interactive one-time
   release below. Then manually deploy that commit on Render. Keep owner use
   gated until both health endpoints and smoke checks pass.

## Effective production environment

`render.yaml` sets `APP_ENV=production`, `DEBUG=False`, `AI_ENABLED=False`,
`REGISTRATION_ENABLED=False`, `PORT=10000`, `WEB_CONCURRENCY=2`, explicit proxy
trust, HTTPS redirect, secure cookies via production settings, and staged HSTS
300 seconds without subdomains/preload. It disables dotenv loading. Gunicorn
retains sync workers, one thread, 90-second timeout and 105-second graceful
timeout; Render shutdown grace is 150 seconds. Verify actual resource use and
graceful behavior in the hosted environment before expanding access.

Startup derives `ALLOWED_HOSTS` from Render's exact `RENDER_EXTERNAL_HOSTNAME`
only when no explicit hosts were supplied. Explicit invalid values still fail
validation. `CSRF_TRUSTED_ORIGINS` stays empty for same-origin HTTPS; any later
custom origin must be explicitly approved. Leave `OPENAI_API_KEY`, Gemini keys,
`AI_GLOBAL_DAILY_CREDITS`, `AI_GLOBAL_WEEKLY_CREDITS` and `GUNICORN_CMD_ARGS` absent.
No production AI thresholds are selected while AI is off.

Both startup and the local release require a direct Neon URL, preserve its target
and identity, and enforce `sslmode=verify-full`, the installed certifi CA bundle,
and a 10-second connection timeout. Unrecognized libpq options are rejected;
they cannot override the checked host, database, user or transaction settings.
PostgreSQL 14+ and active TLS are checked before migration or serving traffic.

## Build, one-time migrations and startup

Render Free has no pre-deploy commands, shell or one-off jobs. Migrations therefore
run **once from the owner's local virtual environment**, not during image build,
web startup or in every worker:

```powershell
.\.venv\Scripts\python.exe scripts\local_release.py --create-owner
```

Run in a secure interactive terminal. It prompts for the expected new database
name, Render hostname, and hidden database URL/signing key. The production key
must match Render. Confirm the target with `MIGRATE`. The helper ignores the real
`.env`, creates disposable static output outside the repository without passing
production credentials to its builder, then uses the existing release process:
validate configuration, Django deploy check, verify PostgreSQL/TLS, migrate once,
and validate schema/static readiness. All application migrations through 0016
and Django migrations apply. A failure exits unsuccessfully: do not deploy or
reverse migrations automatically. No local application data is touched.

`--create-owner` runs Django's interactive superuser creation only if no superuser
exists. Enter its password only at the hidden prompt; never in arguments,
environment documentation or logs. Without that flag only migrations run. Do
not create arbitrary testers; registration remains closed.

Only one operator may release/deploy at a time. For future releases against an
existing database, follow the approved backup/drain rules in
`docs/release_operations.md`; stop incompatible writers before migrations.
Free hosting has no coordinated automated release hook. Do not switch automatic
deploys on or deploy schema changes before the administrative release succeeds.

The Docker image pins Python 3.13.5 and installs existing requirements unchanged.
Its allowlisted context excludes secrets, SQLite, virtual environments and Git.
The existing static builder collects/verifies a retained WhiteNoise manifest
using synthetic configuration with all network/database access blocked.
Generated output is never committed. `scripts/start_web.py` then performs
**read-only** production/database/schema/static validation before replacing
itself with the existing Gunicorn command. Any failure prevents startup and
promotion; no migration or collection occurs at startup.

## Health and hosted acceptance

Render's restart health path is `/health/live/`: it does not query PostgreSQL or
OpenAI, so a database outage does not trigger a liveness restart loop. Keep
`/health/ready/` for explicit HTTPS readiness verification after each deployment:
it returns 200 only when production configuration/database/schema are valid.
Render does not use this second endpoint as an ongoing routing gate; after
startup the operator must monitor readiness and restrict use during outages.
Require actual 200 responses, not redirects. Neither response exposes secrets.

Before owner use, verify HTTPS and HTTP redirects; home/login; CSS/logo/admin
assets; both health paths; direct registration rejection and hidden signup links;
protected-page authentication; secure session/CSRF cookies; owner profile and
dashboard; and safe AI-disabled responses. Restart/redeploy normally and verify
the owner account/profile persists in Neon. Do not call OpenAI or run destructive
database-failure experiments. Record the real URL/results; configuration alone
does not establish a successful deployment.

Free services have cold starts/sleep, finite usage/storage and no production SLA.
Check current quotas in both dashboards. Do not add payment methods or upgrade;
if a Free limit stops service, report it. This target cannot promise continuous
availability. Stop before adding any paid AI key, selecting global AI thresholds,
or setting `AI_ENABLED=True`.
