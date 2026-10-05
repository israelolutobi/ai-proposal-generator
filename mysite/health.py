"""Minimal health responses; normal host/HTTPS/security middleware still applies."""
import logging

from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from mysite.operations import OperationalFailure, check_readiness


logger = logging.getLogger("proposalq.operations")


def _response(request, body, status=200):
    response = HttpResponse("" if request.method == "HEAD" else body, status=status,
                            content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@require_safe
def live(request):
    return _response(request, "ok\n")


@never_cache
@require_safe
def ready(request):
    try:
        check_readiness()
    except OperationalFailure as error:
        logger.warning("readiness_failed", extra={"category": error.category})
        return _response(request, "not ready\n", 503)
    return _response(request, "ok\n")
