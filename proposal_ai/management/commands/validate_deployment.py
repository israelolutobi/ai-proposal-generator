"""Validate configuration only; deliberately performs no infrastructure probes."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from mysite.configuration import validate_production
from mysite.runtime import validate_runtime


class Command(BaseCommand):
    help = "Validate explicit production configuration without database or provider access."
    requires_system_checks = []
    requires_migrations_checks = False

    def handle(self, *args, **options):
        if settings.APP_ENV != "production":
            raise CommandError("Set APP_ENV=production before deployment validation.")
        validate_production(settings)
        validate_runtime(settings.DEPLOYMENT_RUNTIME)
        self.stdout.write(f"AI_ENABLED={settings.AI_ENABLED}; global limits configured={settings.AI_GLOBAL_DAILY_CREDITS is not None}. This is local process configuration only.")
        self.stdout.write(self.style.SUCCESS("Production configuration is valid. No database or provider connection was made."))
        self.stdout.write("Confirm PostgreSQL TLS, ingress isolation/header rewriting, upstream timeout >=120s and shutdown draining with the deployment operator.")
        self.stdout.write("Review the HSTS rollout policy and Django deployment warnings without suppressing them.")
