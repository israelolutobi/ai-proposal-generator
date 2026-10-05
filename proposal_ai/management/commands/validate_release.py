"""Inspect release state. Deliberately contains no administrative mutations."""
import logging

from django.core.management.base import BaseCommand, CommandError

from mysite.operations import OperationalFailure, check_release


logger = logging.getLogger("proposalq.operations")


class Command(BaseCommand):
    help = "Read-only database, migration/schema and collected-static release validation."
    requires_system_checks = []
    requires_migrations_checks = False

    def handle(self, *args, **options):
        try:
            check_release()
        except OperationalFailure as error:
            logger.error("release_validation_failed", extra={"category": error.category})
            raise CommandError(f"Release validation failed: {error}") from None
        self.stdout.write(self.style.SUCCESS(
            "Release database/schema and collected static assets are ready. No state was changed."
        ))
