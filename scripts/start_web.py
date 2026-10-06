"""Read-only release gate followed by the existing Gunicorn command."""
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit


def render_hostname(environment):
    """Use Render's service-specific hostname only when no hosts were supplied."""
    from mysite.configuration import valid_host

    if "ALLOWED_HOSTS" in environment:
        return
    hostname = environment.get("RENDER_EXTERNAL_HOSTNAME", "")
    if (environment.get("RENDER") != "true" or not valid_host(hostname)
            or hostname.startswith(".") or not hostname.endswith(".onrender.com")):
        raise ValueError("An explicit deployment hostname is required.")
    environment["ALLOWED_HOSTS"] = hostname


def render_database(environment):
    """Apply the same direct Neon target and CA-verified TLS policy as release."""
    from scripts.local_release import neon_url

    value = environment.get("DATABASE_URL", "")
    database_name = unquote(urlsplit(value).path[1:])
    environment["DATABASE_URL"] = neon_url(value, database_name)


def start_web():
    from django.conf import settings
    from django.core.management import call_command
    from scripts.release import verify_database

    call_command("validate_deployment")
    if settings.AI_ENABLED or settings.REGISTRATION_ENABLED:
        raise ValueError("Controlled Beta startup requires AI and registration disabled.")
    verify_database()
    call_command("validate_release")
    # No migrations/collection occur here. exec gives Gunicorn the platform's
    # SIGTERM directly rather than leaving a shell/Python supervisor in front.
    os.execvp("gunicorn", ["gunicorn", "--config", "gunicorn.conf.py", "mysite.wsgi:application"])


def main():
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        render_hostname(os.environ)
        render_database(os.environ)
        import django
        django.setup()
        start_web()
    except Exception:
        print("Startup release validation failed; Gunicorn was not started. Inspect sanitized events and configuration privately.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
