"""One coordinated private-smoke release; never invoked by web workers."""
import os
from pathlib import Path
import sys


class ReleaseError(Exception):
    """Fixed operator messages only; database exceptions must never be printed."""


def verify_database():
    """Inspect PostgreSQL/TLS before any administrative mutation."""
    from django.db import connection
    if connection.vendor != "postgresql":
        raise ReleaseError("Release migrations require PostgreSQL; SQLite is forbidden.")
    connection.ensure_connection()
    if not connection.connection.info.ssl_in_use:
        raise ReleaseError("PostgreSQL TLS must be active before release migrations.")
    if connection.pg_version < 140000:
        raise ReleaseError("Release requires PostgreSQL 14 or later.")
    print("PostgreSQL version floor and encrypted connection verified.")


def run_release():
    from django.conf import settings
    from django.core.management import call_command

    call_command("validate_deployment")
    if settings.AI_ENABLED or settings.REGISTRATION_ENABLED:
        raise ReleaseError("Private smoke release requires AI and registration disabled.")
    call_command("check", deploy=True)
    verify_database()
    call_command("migrate", interactive=False)
    call_command("validate_release")


def main():
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        import django
        django.setup()
        run_release()
    except ReleaseError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # Driver/settings exceptions can embed credentials. Preserve failure
        # status without copying their payload into platform release logs.
        print("Release failed; traffic must remain gated. Inspect sanitized operational events and configuration privately.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
