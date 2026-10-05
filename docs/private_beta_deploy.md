# Private hosted smoke deployment

Target: the actual ProposalQ application, one web instance with two Gunicorn sync
workers and one managed PostgreSQL database. AI and registration stay disabled.
No Redis, Celery or extra AI worker. No account, resource or cost is authorized by
this configuration alone.

Render's shared readiness/restart check conflicts with the approved runbook's
avoidance of database-driven restart loops. Fly service readiness checks remove
routing without restarting the Machine, so this deployment uses Fly instead.

## Owner prerequisites

1. Sign in to Fly.io and approve the web/managed PostgreSQL costs before creating
   resources. Choose an available common region and supported PostgreSQL version
   (17 preferred). No purchase/payment action is performed by these files.
2. Establish the owner-only HTTPS access gateway/VPN and trusted private backend
   boundary. Flycast alone is HTTP, not browser HTTPS. The gateway must enforce
   owner access, overwrite scheme headers and have a timeout at least 120 seconds.
   Keep Django's HTTPS redirects and secure cookies enabled. Do not deploy a
   publicly reachable origin as a shortcut.
3. Create a NEW managed PostgreSQL database with working PostgreSQL TLS and a
   direct connection URL. Verify maintenance/backups with the provider. Never
   upload local SQLite or use the PostgreSQL integration launcher on this DB.

The host app name and region are intentionally not invented in `fly.toml`.
Supply the owner-approved app name via `--app` and select the DB's region for the
web instance. Keep exactly one web Machine; do not accept automatic extra replicas.

## Secrets and effective environment

Use provider secret management for a newly generated private stable `SECRET_KEY`
(at least 50 random characters) and the managed database's `DATABASE_URL`.
Do not put either in command arguments, logs, Git or an image build argument.
Use the provider's secure secret UI/import mechanism. Never upload local `.env`.

`fly.toml` supplies production mode, debug/registration/AI False, port 8000,
two sync workers, HTTPS redirect, HSTS 300 without subdomains/preload, and explicit
proxy trust. Validate the trusted gateway before using that proxy trust.

Set `ALLOWED_HOSTS` in provider configuration to the exact HTTPS gateway hostname
plus `proposalq-private-beta.internal` (the internal probe host). Empty
`CSRF_TRUSTED_ORIGINS` is valid for same-origin access; if needed, use only the
explicit approved HTTPS origin. Leave `OPENAI_API_KEY`, both `AI_GLOBAL_*_CREDITS`
variables and `GUNICORN_CMD_ARGS` absent. Never copy development defaults wholesale.

## Build, release and startup

The Docker image pins Python 3.13.5 and installs existing requirements unchanged.
Its allowlisted context excludes `.env`, SQLite, virtual environments and Git.
`scripts/build_static.py` uses an ephemeral in-memory signing key and synthetic
unreachable database configuration. Networking/database access is blocked while
it validates settings, collects static files, verifies the manifest and renders
public/login/admin templates. Production credentials are never needed by build.

The image retains collected assets; generated output is not committed. Startup
is exactly the existing Gunicorn command. It does not migrate or collect assets.

One operator runs one deployment at a time; prohibit overlapping manual/CI deploys.
Fly's separate release command runs `scripts/release.py`, which validates the
production configuration, requires AI/registration off, verifies PostgreSQL 14+
and an active TLS connection, runs `migrate --noinput` once, then `validate_release`.
Any failure stops that deploy. Do not fake, reverse or repair migrations blindly.
All application migrations through 0016 and required Django migrations apply.
If an existing database is involved, follow the backup/drain rules in the release
runbook before migration; incompatible old writers must be stopped.

After owner prerequisites, use the approved app with `fly deploy --app APP_NAME
--flycast --ha=false`. Before deployment, verify the app has NO public IPv4/IPv6
addresses; Flycast does not negate an existing public address. Keep the private
gateway closed to users until every release and smoke check passes. A later
manual redeploy reuses the same managed DB and stable signing key.

Readiness is the routing gate; liveness is monitoring only. Internal probes send
the explicit allowed host/scheme and must receive a real 200, not a redirect.
Verify both over HTTPS at the actual gateway too. No probe calls AI. The host
sends SIGTERM with 150-second grace; Gunicorn retains 90/105 seconds, sync workers
and one thread. Fly grace is best-effort: actual graceful draining and gateway
timeouts still require hosted verification. Inspect logs without exposing payloads.

## Owner account and smoke acceptance

Use an interactive console attached to the approved app:
`fly ssh console --app APP_NAME --pty -C "python manage.py createsuperuser"`.
The owner enters username/email/password; the password must never be supplied as
a command argument or printed. Provision only the owner, not arbitrary testers.
The owner logs in and can use `/change-password/`, create/edit a profile and use
the dashboard. Registration stays closed.

Verify real HTTPS/redirects, home/login, CSS/logo/admin assets, both health paths,
register rejection, protected-route redirects, production cookies and owner
access. Verify the owner-created profile/account persists through a normal
restart/redeploy. Do not create synthetic jobs/proposals or call OpenAI. Inspect
`showmigrations` and `ai_status` in the private operator console without dumping
private records. Stop before adding any provider key, credit ceilings or enabling
AI. Real hosted results must be recorded before claiming deployment success.
