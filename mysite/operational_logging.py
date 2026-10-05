"""Allowlisted production events. Never format messages, arguments or exceptions."""
from datetime import datetime, timezone
import json
import logging

from mysite.operations import FAILURES


def _event(record):
    if record.name == "proposalq.operations":
        if (type(record.msg) is str and record.msg in ("readiness_failed", "release_validation_failed")
                and type(getattr(record, "category", None)) is str and record.category in FAILURES):
            return {"event": record.msg, "category": record.category}
    elif record.name in ("django", "django.request", "django.server"):
        return {"event": "request_failure", "category": "application_error"}
    elif record.name == "django.security" or record.name.startswith("django.security."):
        return {"event": "request_rejected", "category": "security_rejection"}
    return None


class SafeOperationalFilter(logging.Filter):
    def filter(self, record):
        return record.levelno >= logging.WARNING and _event(record) is not None


class SafeOperationalFormatter(logging.Formatter):
    def format(self, record):
        payload = _event(record) or {"event": "operational_failure", "category": "unavailable"}
        payload["timestamp"] = datetime.fromtimestamp(record.created, timezone.utc).isoformat()
        payload["level"] = "ERROR" if record.levelno >= logging.ERROR else "WARNING"
        status = getattr(record, "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            payload["status"] = status
        # No getMessage(), formatException(), request objects or arbitrary extras.
        return json.dumps(payload, sort_keys=True)


def production_logging():
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"safe_operations": {"()": "mysite.operational_logging.SafeOperationalFilter"}},
        "formatters": {"safe_operations": {"()": "mysite.operational_logging.SafeOperationalFormatter"}},
        "handlers": {"safe_operations": {
            "class": "logging.StreamHandler", "stream": "ext://sys.stderr",
            "filters": ["safe_operations"], "formatter": "safe_operations",
        }},
        "loggers": {name: {"handlers": ["safe_operations"], "level": "WARNING", "propagate": False}
                    for name in ("proposalq.operations", "django", "django.server")},
    }
