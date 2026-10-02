"""Explicit bounded stale recovery. Separate from read-only status inspection."""
from django.core.management.base import BaseCommand, CommandError
from proposal_ai import ai_control


class Command(BaseCommand):
    help = "Recover a bounded batch of expired AI requests with existing release/consume semantics."
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=ai_control.STALE_RECOVERY_BATCH)

    def handle(self, *args, **options):
        try:
            counts = ai_control.recover_global_stale(options["limit"])
        except (ai_control.ControlError, ValueError):
            raise CommandError("Stale AI recovery was unavailable or the batch limit was invalid. No provider request was made.") from None
        self.stdout.write(f"Recovered: released={counts['released']}, consumed={counts['consumed']}. No provider request was made.")
