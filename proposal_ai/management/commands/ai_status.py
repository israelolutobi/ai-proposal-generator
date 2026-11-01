"""Inspect local AI policy and database accounting without changing either."""
import json
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from proposal_ai import ai_control, ai_global


class Command(BaseCommand):
    help = "Read-only AI policy/accounting snapshot; never calls providers or repairs counters."
    requires_system_checks = []
    requires_migrations_checks = False

    def handle(self, *args, **options):
        try:
            report = ai_global.status(ai_control.now())
        except (DatabaseError, ai_global.CoordinationError):
            raise CommandError("ProposalQ AI status is unavailable; verify database/schema and local configuration.") from None
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        self.stdout.write("This process only: verify every deployed worker has adopted the switch. No state was changed.")
