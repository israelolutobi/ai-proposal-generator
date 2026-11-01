"""Explicit retention maintenance; never called by startup or readiness."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from proposal_ai.models import AccountRateLimit


class Command(BaseCommand):
    help = "Remove expired account-throttle counters without printing identifiers."

    def handle(self, *args, **options):
        removed, _ = AccountRateLimit.objects.filter(expires_at__lte=timezone.now()).delete()
        self.stdout.write(f"Removed {removed} expired account-throttle counters.")
