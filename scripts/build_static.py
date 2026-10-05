"""Collect/verify the image's static artifact without credentials or DB access."""
import argparse
import os
from pathlib import Path
import secrets
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional disposable local verification directory.")
    arguments = parser.parse_args()
    # Build secrets never enter image configuration, files or command arguments.
    os.environ.update(
        DJANGO_SETTINGS_MODULE="mysite.settings", PYTHON_DOTENV_DISABLED="1",
        SECRET_KEY=secrets.token_urlsafe(64), APP_ENV="production", DEBUG="False",
        AI_ENABLED="False", REGISTRATION_ENABLED="False",
        DATABASE_URL="postgresql://static_build@127.0.0.1:1/static_build?sslmode=require",
        ALLOWED_HOSTS="static-build.invalid", CSRF_TRUSTED_ORIGINS="",
        SECURE_SSL_REDIRECT="True", SECURE_HSTS_SECONDS="300",
        SECURE_HSTS_INCLUDE_SUBDOMAINS="False", SECURE_HSTS_PRELOAD="False",
        TRUST_PROXY_HEADERS="False", PORT="8000", WEB_CONCURRENCY="2",
    )
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "AI_GLOBAL_DAILY_CREDITS",
                 "AI_GLOBAL_WEEKLY_CREDITS", "GUNICORN_CMD_ARGS"):
        os.environ.pop(name, None)
    sys.path.insert(0, str(ROOT))

    import django
    from unittest.mock import patch
    from mysite.test_runner import block_external_network

    with (block_external_network(),
          patch("psycopg2.connect", side_effect=AssertionError("Static build must not connect to a database."))):
        django.setup()
        from django.conf import settings
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.staticfiles.storage import staticfiles_storage
        from django.core.management import call_command
        from django.template.loader import render_to_string
        from django.test import Client, RequestFactory
        from mysite.operations import check_static

        if arguments.output:
            settings.STATIC_ROOT = arguments.output.resolve()
        call_command("validate_deployment")
        call_command("check", deploy=True)
        call_command("collectstatic", interactive=False, verbosity=1)
        check_static()
        request = RequestFactory().get("/", secure=True, HTTP_HOST="static-build.invalid")
        request.user = AnonymousUser()
        for template in ("public_home.html", "login.html"):
            html = render_to_string(template, request=request)
            for asset in ("css/style.css", "images/proposallogo.png"):
                if staticfiles_storage.url(asset) not in html:
                    raise RuntimeError("A public template does not reference its collected asset.")
        response = Client().get("/admin/login/", secure=True, HTTP_HOST="static-build.invalid")
        if response.status_code != 200 or staticfiles_storage.url("admin/css/base.css").encode() not in response.content:
            raise RuntimeError("Admin login/static verification failed.")
        print("Static manifest, application templates and admin assets verified without database/provider access.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
