"""One provider-neutral Gunicorn contract shared by startup and validation."""
from django.core.exceptions import ImproperlyConfigured

from mysite.configuration import integer


WORKER_TIMEOUT_SECONDS = 90
GRACEFUL_TIMEOUT_SECONDS = 105


def gunicorn_configuration(environment):
    if environment.get("GUNICORN_CMD_ARGS", "").strip():
        raise ImproperlyConfigured("GUNICORN_CMD_ARGS overrides are unsupported; use the repository Gunicorn configuration.")
    configuration = {
        "bind": f"0.0.0.0:{integer(environment, 'PORT', 8000, minimum=1, maximum=65535)}",
        "workers": integer(environment, "WEB_CONCURRENCY", 1, minimum=1),
        "worker_class": "sync",
        "threads": 1,
        "timeout": WORKER_TIMEOUT_SECONDS,
        "graceful_timeout": GRACEFUL_TIMEOUT_SECONDS,
        "reload": False,
        # Django's explicit proxy policy is the sole forwarded-scheme authority.
        # Disable Gunicorn's separate default trust, including on UNIX sockets.
        "secure_scheme_headers": {},
        "forwarded_allow_ips": "",
    }
    validate_runtime(configuration)
    return configuration


def validate_runtime(configuration):
    if (configuration["timeout"] != 90 or configuration["graceful_timeout"] != 105
            or configuration["worker_class"] != "sync" or configuration["threads"] != 1
            or configuration["reload"]):
        raise ImproperlyConfigured("Gunicorn must use the reviewed sync-worker policy: timeout 90, graceful timeout 105, threads 1, reload disabled.")
    if type(configuration["workers"]) is not int or configuration["workers"] < 1:
        raise ImproperlyConfigured("Gunicorn requires a positive worker count.")
    if configuration["secure_scheme_headers"] or configuration["forwarded_allow_ips"]:
        raise ImproperlyConfigured("Gunicorn forwarded-scheme inference must remain disabled; use the explicit Django proxy policy.")


def validate_gunicorn_startup(server):
    # Gunicorn applies CLI arguments after the config file. Check the actual
    # parsed values before workers start, without importing its POSIX runtime.
    names = ("timeout", "graceful_timeout", "worker_class", "threads", "reload",
             "workers", "secure_scheme_headers", "forwarded_allow_ips")
    validate_runtime({name: server.cfg.settings[name].get() for name in names})
