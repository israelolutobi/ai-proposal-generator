"""Explicit recovery for missed test webhooks or uncertain checkout responses."""
import stripe
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from proposal_ai import billing


class Command(BaseCommand):
    help = "Reconcile one billing account from Stripe test mode. Never charges or creates subscriptions."

    def add_arguments(self, parser):
        parser.add_argument("account_id", type=int)

    def handle(self, *args, **options):
        from proposal_ai.models import BillingAccount
        try:
            billing.reconcile(options["account_id"])
        except (billing.BillingError, stripe.StripeError, DatabaseError, BillingAccount.DoesNotExist, ValueError):
            raise CommandError("Test billing reconciliation failed. Review Stripe and retry; no payment was created.") from None
        self.stdout.write(self.style.SUCCESS("Test billing account reconciled. No payment was created."))
