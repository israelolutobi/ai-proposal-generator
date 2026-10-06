"""Run opt-in integration tests on an explicitly designated disposable cluster.

Never inherits DATABASE_URL or reads .env to choose the test connection.
This script creates/destroys Django's test database, not the server cluster.
"""
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import unquote, urlsplit


class TestConfigurationError(Exception):
    pass


def configuration():
    if os.environ.get("PROPOSALQ_POSTGRES_TESTS") != "1":
        raise TestConfigurationError("Set PROPOSALQ_POSTGRES_TESTS=1 to explicitly enable these tests.")
    url = os.environ.get("PROPOSALQ_POSTGRES_TEST_DATABASE_URL", "")
    if not url:
        raise TestConfigurationError("PROPOSALQ_POSTGRES_TEST_DATABASE_URL is required; DATABASE_URL is never used as a fallback.")
    expected_directory = os.environ.get("PROPOSALQ_TEST_PG_DATA_DIR", "")
    if not expected_directory:
        raise TestConfigurationError("PROPOSALQ_TEST_PG_DATA_DIR must identify the disposable cluster.")
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme in ("postgres", "postgresql") and parsed.hostname == "127.0.0.1"
                 and parsed.port is not None and parsed.username
                 and parsed.path == "/proposalq_task4b" and not parsed.query and not parsed.fragment)
        if not valid:
            raise ValueError
        params = {"host": "127.0.0.1", "port": parsed.port,
                  "user": unquote(parsed.username), "dbname": "proposalq_task4b", "connect_timeout": 5}
        if parsed.password is not None:
            params["password"] = unquote(parsed.password)
    except ValueError:
        raise TestConfigurationError("Use an explicit loopback port and the disposable proposalq_task4b database; URL options are not accepted.") from None
    return url, expected_directory, params


def verify_cluster(expected_directory, params):
    import psycopg2
    try:
        db = psycopg2.connect(**params)
    except psycopg2.Error:
        raise TestConfigurationError("Cannot connect to the explicitly designated disposable PostgreSQL cluster.") from None
    try:
        with db.cursor() as cursor:
            cursor.execute("SHOW data_directory")
            if Path(cursor.fetchone()[0]).resolve() != Path(expected_directory).resolve():
                raise TestConfigurationError("Cluster identity does not match the designated disposable data directory.")
            cursor.execute("SHOW server_version_num")
            if int(cursor.fetchone()[0]) < 140000:
                raise TestConfigurationError("Django 6.0 requires PostgreSQL 14 or later.")
            cursor.execute("SHOW transaction_isolation")
            if cursor.fetchone()[0] != "read committed":
                raise TestConfigurationError("The disposable cluster must use READ COMMITTED.")
            cursor.execute("SELECT datname FROM pg_database WHERE NOT datistemplate AND datname NOT IN (%s,%s,%s)",
                           ["postgres", "proposalq_task4b", "test_proposalq_task4b"])
            if cursor.fetchall():
                raise TestConfigurationError("Additional databases exist; refusing a potentially shared cluster.")
    except psycopg2.Error:
        raise TestConfigurationError("Unable to verify the disposable cluster identity and isolation.") from None
    finally:
        db.close()


def main():
    try:
        url, directory, params = configuration()
        verify_cluster(directory, params)
    except TestConfigurationError as error:
        print("PostgreSQL test configuration error: " + str(error), file=sys.stderr)
        return 2
    os.environ.update(DATABASE_URL=url, SECRET_KEY=secrets.token_urlsafe(64), DEBUG="False",
                      OPENAI_API_KEY="", GEMINI_API_KEY="", DJANGO_SETTINGS_MODULE="mysite.settings",
                      PYTHONDONTWRITEBYTECODE="1", ALLOWED_HOSTS="localhost,127.0.0.1",
                      APP_ENV="development", AI_ENABLED="True", PYTHON_DOTENV_DISABLED="1")
    # Individual global-control tests explicitly opt into synthetic ceilings.
    # Ordinary DATABASE_URL/.env/production AI policy never selects test state.
    os.environ.pop("AI_GLOBAL_DAILY_CREDITS", None)
    os.environ.pop("AI_GLOBAL_WEEKLY_CREDITS", None)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import django
    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    if settings.TEST_RUNNER != "mysite.test_runner.NoNetworkDiscoverRunner":
        raise TestConfigurationError("The network-blocking test runner must remain active.")
    labels = ["mysite.postgres_admission_tests", "mysite.postgres_concurrency_tests",
              "mysite.postgres_global_exposure_tests", "mysite.postgres_research_tests",
              "mysite.test_research_intelligence", "mysite.test_release_operations"]
    call_command("test", *labels, verbosity=2, interactive=False)
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.exit(main())
