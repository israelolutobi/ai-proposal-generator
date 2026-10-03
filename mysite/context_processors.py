"""Expose the effective signup policy without parsing environment values."""
from django.conf import settings


def registration_policy(request):
    return {"registration_enabled": settings.REGISTRATION_ENABLED is True}
