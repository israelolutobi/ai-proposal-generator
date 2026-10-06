"""Explicit one-time Neon migration/owner setup from the approved local checkout."""
import argparse
import getpass
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]


def neon_url(value, expected_database):
    """Require the approved direct Neon database and authenticated TLS."""
    import certifi

    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if (parsed.scheme not in ("postgres", "postgresql")
            or not hostname.endswith(".neon.tech") or "-pooler" in hostname
            or not parsed.username or not parsed.password or parsed.fragment
            or not expected_database or unquote(parsed.path[1:]) != expected_database):
        raise ValueError("Use the approved direct Neon URL and matching database name.")
    options = dict(parse_qsl(parsed.query))
    # Never inherit options that can override the independently checked target,
    # identity or transaction policy through libpq connection parameters.
    if any(name not in {"sslmode", "sslrootcert", "channel_binding", "connect_timeout"}
           for name in options):
        raise ValueError("Unexpected Neon connection options; review them privately.")
    options.update(sslmode="verify-full", sslrootcert=certifi.where(), connect_timeout="10")
    return urlunsplit(parsed._replace(query=urlencode(options)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-owner", action="store_true", help="Create the first superuser interactively if none exists.")
    parser.add_argument("--research-workbook", type=Path, help="Validate/import a private snapshot after migration, with AI disabled.")
    arguments = parser.parse_args()
    # getpass otherwise falls back to echoing input when no secure terminal is
    # available. Refuse that mode instead of risking disclosure.
    if not sys.stdin.isatty():
        print("Run this explicit administrative command in a secure interactive terminal.", file=sys.stderr)
        return 1
    try:
        hostname = input("Approved Render hostname (without https://): ").strip()
        database_name = input("Approved Neon database name: ").strip()
        raw_url = getpass.getpass("Direct Neon connection URL (hidden): ")
        signing_key = getpass.getpass("Production SECRET_KEY already saved in Render (hidden): ")
        from mysite.configuration import valid_host
        if not valid_host(hostname) or hostname.startswith(".") or not hostname.endswith(".onrender.com"):
            raise ValueError("The approved Render hostname is required.")
        url = neon_url(raw_url, database_name)
        environment = {
            "DJANGO_SETTINGS_MODULE": "mysite.settings", "PYTHON_DOTENV_DISABLED": "1",
            "APP_ENV": "production", "DEBUG": "False", "AI_ENABLED": "False",
            "REGISTRATION_ENABLED": "False", "SECRET_KEY": signing_key, "DATABASE_URL": url,
            "ALLOWED_HOSTS": hostname, "CSRF_TRUSTED_ORIGINS": "", "PORT": "10000",
            "WEB_CONCURRENCY": "2", "TRUST_PROXY_HEADERS": "True",
            "SECURE_SSL_REDIRECT": "True", "SECURE_HSTS_SECONDS": "300",
            "SECURE_HSTS_INCLUDE_SUBDOMAINS": "False", "SECURE_HSTS_PRELOAD": "False",
        }
        # Only process-scoped values; never load or rewrite the development .env.
        os.environ.update(environment)
        for name in ("AI_GLOBAL_DAILY_CREDITS", "AI_GLOBAL_WEEKLY_CREDITS",
                     "OPENAI_API_KEY", "GEMINI_API_KEY", "GUNICORN_CMD_ARGS"):
            os.environ.pop(name, None)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory(prefix="proposalq-neon-release-") as output:
            if Path(output).resolve().parent != Path(tempfile.gettempdir()).resolve():
                raise ValueError("Unexpected temporary artifact location.")
            # Do not pass production credentials to the separate static builder.
            build_environment = os.environ.copy()
            for name in ("SECRET_KEY", "DATABASE_URL"):
                build_environment.pop(name, None)
            subprocess.run([sys.executable, "-B", str(ROOT / "scripts/build_static.py"),
                            "--output", output], env=build_environment, check=True)
            import django
            django.setup()
            from django.conf import settings
            from django.contrib.auth import get_user_model
            from django.core.management import call_command
            from scripts.release import run_release
            settings.STATIC_ROOT = Path(output)
            if input("Confirm this is the approved Neon target; type MIGRATE: ") != "MIGRATE":
                print("Administrative release cancelled; no migrations ran.")
                return 1
            run_release()
            if arguments.research_workbook:
                call_command("import_research_workbook", str(arguments.research_workbook), validate_only=True)
                call_command("import_research_workbook", str(arguments.research_workbook))
                from proposal_ai.models import JobPost, ResearchDataset
                from proposal_ai.research_intelligence import build_research_context
                dataset = ResearchDataset.objects.get(active=True)
                print(f"Active research snapshot: {dataset.pk}; cases: {dataset.case_count}.")
                for title, description in (("Python Django developer", "Take over and debug a Django automation codebase"),
                                           ("Wedding photographer", "Portrait photography lighting")):
                    context = build_research_context(JobPost(job_title=title, job_description=description, skills_required=""))
                    print(f"Research smoke: {title}; selected={context.matched_cases}; context_chars={len(context.text)}.")
                call_command("validate_release")
            if arguments.create_owner:
                if get_user_model().objects.filter(is_superuser=True).exists():
                    print("An operator account already exists; no account was created.")
                else:
                    call_command("createsuperuser", interactive=True)
            print("One-time Neon release completed. Manually deploy Render and verify HTTPS readiness before owner use.")
        return 0
    except Exception:
        print("Administrative release failed. Keep deployment gated; inspect configuration/history privately. No automatic rollback was attempted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
