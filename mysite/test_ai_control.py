"""Beta controls use isolated databases and fake providers, never live AI."""
from .ai_test_helpers import service_result
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace
import uuid
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import DatabaseError, IntegrityError, OperationalError, connection, connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from proposal_ai import ai_control as control, services
from proposal_ai.models import AIRequest, FreelancerProfile, JobPost, Proposal, WorkExperience


O, I, L, Q, F = (AIRequest.Operation, AIRequest.Intent, AIRequest.Lifecycle, AIRequest.Quota, AIRequest.Failure)
MOMENT = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
FAKE_KEY = "test-only-not-a-credential"
UI_SETTINGS = {
    "OPENAI_API_KEY": FAKE_KEY, "ALLOWED_HOSTS": ["testserver"], "DEBUG": False,
    "STORAGES": {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                 "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
}


class NonceTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=7)

    def token(self, **kwargs):
        return control.issue_nonce(self.user, O.PROFILE_SUMMARY, **kwargs)

    def test_signed_nonce_contains_random_v4_identity_and_intent(self):
        token = self.token(intent=I.REGENERATE)
        identity, intent = control.validate_nonce(token, self.user, O.PROFILE_SUMMARY)
        self.assertEqual(identity.version, 4)
        self.assertEqual(intent, I.REGENERATE)
        self.assertNotEqual(token, self.token(intent=I.REGENERATE))

    def test_nonce_expiration_at_exact_24_hours(self):
        with patch("django.core.signing.time.time", return_value=1000):
            token = self.token()
        with patch("django.core.signing.time.time", return_value=1000 + 86400):
            control.validate_nonce(token, self.user, O.PROFILE_SUMMARY)
        with patch("django.core.signing.time.time", return_value=1001 + 86400):
            with self.assertRaises(control.ControlError):
                control.validate_nonce(token, self.user, O.PROFILE_SUMMARY)

    def test_invalid_unsigned_and_tampered_nonces_rejected(self):
        for token in ("", str(uuid.uuid4()), self.token() + "x", None):
            with self.subTest(token_type=type(token).__name__), self.assertRaises(control.ControlError):
                control.validate_nonce(token, self.user, O.PROFILE_SUMMARY)

    def test_user_and_operation_and_resource_are_bound(self):
        token = control.issue_nonce(self.user, O.PROPOSAL_GENERATION, 4)
        for user, operation, resource in ((SimpleNamespace(pk=8), O.PROPOSAL_GENERATION, 4),
                                           (self.user, O.JOB_EXTRACTION, 4),
                                           (self.user, O.PROPOSAL_GENERATION, 5)):
            with self.subTest(resource=resource), self.assertRaises(control.ControlError):
                control.validate_nonce(token, user, operation, resource)

    def test_signed_invalid_intent_is_rejected(self):
        data = {"user": 7, "operation": O.PROFILE_SUMMARY, "resource": None,
                "nonce": str(uuid.uuid4()), "intent": "arbitrary"}
        with self.assertRaises(control.ControlError):
            control.validate_nonce(signing.dumps(data, salt=control.NONCE_SALT), self.user, O.PROFILE_SUMMARY)

    def test_fingerprints_are_canonical_keyed_and_separated(self):
        value = {"title": "PRIVATE_TITLE", "skills": ["Python"]}
        first = control.fingerprint(value, "submitted")
        self.assertEqual(first, control.fingerprint(dict(reversed(list(value.items()))), "submitted"))
        self.assertNotEqual(first, control.fingerprint(value, "effective"))
        with override_settings(SECRET_KEY="test-only-alternative-signing-material"):
            self.assertNotEqual(first, control.fingerprint(value, "submitted"))
        self.assertEqual(len(first), 64)
        self.assertNotIn("PRIVATE_TITLE", first)

    def test_utc_day_and_monday_week_boundaries_ignore_local_timezone(self):
        local = datetime(2026, 10, 5, 0, 30, tzinfo=timezone(timedelta(hours=1)))
        day, week, day_reset, week_reset = control.boundaries(local)
        self.assertEqual(day, datetime(2026, 10, 4, tzinfo=timezone.utc))
        self.assertEqual(week, datetime(2026, 9, 28, tzinfo=timezone.utc))
        self.assertEqual(day_reset, week_reset)


@override_settings(OPENAI_API_KEY=FAKE_KEY)
class ControlTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="control-owner")
        self.other = get_user_model().objects.create_user(username="control-other")
        self.clock = patch("proposal_ai.ai_control.now", return_value=MOMENT)
        self.clock_mock = self.clock.start()
        self.addCleanup(self.clock.stop)

    def admit(self, operation=O.PROFILE_SUMMARY, *, user=None, token=None, submitted=None, effective=None):
        user = user or self.user
        token = token or control.issue_nonce(user, operation)
        return control.admit(user, operation, token, submitted or {"text": "test"}, effective or "context")

    def history(self, count, *, user=None, operation=O.PROFILE_SUMMARY, moment=None, quota=Q.CONSUMED):
        moment = moment or MOMENT - timedelta(hours=1)
        return AIRequest.objects.bulk_create([AIRequest(
            user=user or self.user, operation=operation, intent=I.GENERATE,
            nonce=uuid.uuid4(), submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
            lifecycle=L.FAILED, quota_state=quota, quota_units=control.CREDITS[operation],
            admitted_at=moment, dispatch_started_at=moment, completed_at=moment,
            lease_expires_at=moment + control.LEASE,
        ) for _ in range(count)])

    def assert_control(self, function, status):
        with self.assertRaises(control.ControlError) as raised:
            function()
        self.assertEqual(raised.exception.status, status)
        return raised.exception

    def test_credit_weights_and_immediate_reservation(self):
        for operation, units in control.CREDITS.items():
            with self.subTest(operation=operation):
                row = self.admit(operation).request
                self.assertEqual(row.quota_units, units)
                self.assertEqual(row.quota_state, Q.RESERVED)
                self.assertEqual(control.allowance(self.user)[0]["daily_remaining"], 25 - units)
                control.fail(row, F.LOCAL_CONFIGURATION, release=True)

    def test_below_exact_and_one_over_each_burst_limit(self):
        for operation, (maximum, window) in control.BURSTS.items():
            with self.subTest(operation=operation):
                AIRequest.objects.all().delete()
                for _ in range(maximum):
                    row = self.admit(operation).request
                    control.mark_dispatch(row)
                    control.fail(row, F.CAPACITY, release=True)
                before = AIRequest.objects.count()
                error = self.assert_control(lambda: self.admit(operation), 429)
                self.assertEqual(error.retry_after, int(window.total_seconds()))
                self.assertEqual(AIRequest.objects.count(), before)
                self.assertEqual(control.allowance(self.user)[0]["daily_remaining"], 25)

    def test_burst_expiry_at_exact_window_restores_access(self):
        self.history(3, moment=MOMENT - timedelta(minutes=10), quota=Q.RELEASED)
        self.admit()

    def test_user_and_operation_burst_limits_are_independent(self):
        self.history(3, moment=MOMENT - timedelta(seconds=10), quota=Q.RELEASED)
        row = self.admit(O.JOB_EXTRACTION).request
        control.fail(row, F.LOCAL_CONFIGURATION, release=True)
        self.admit(user=self.other)

    def test_daily_exact_boundary_accepted_then_next_rejected(self):
        self.history(24)
        row = self.admit().request
        control.mark_dispatch(row)
        control.succeed(row)
        error = self.assert_control(self.admit, 429)
        self.assertEqual(error.metadata["daily_remaining"], 0)
        self.assertEqual(AIRequest.objects.count(), 25)

    def test_daily_remaining_must_cover_full_operation_weight(self):
        self.history(23)
        self.assert_control(lambda: self.admit(O.PROPOSAL_GENERATION), 429)
        self.assertEqual(control.allowance(self.user)[0]["daily_remaining"], 2)

    def test_weekly_exact_boundary_accepted_then_rejected(self):
        self.history(99, moment=MOMENT - timedelta(days=1))
        row = self.admit().request
        control.mark_dispatch(row)
        control.succeed(row)
        error = self.assert_control(self.admit, 429)
        self.assertEqual(error.metadata["weekly_remaining"], 0)
        self.assertEqual(error.metadata["daily_remaining"], 24)

    def test_both_quota_ceilings_are_checked(self):
        self.history(100, moment=MOMENT - timedelta(days=1))
        self.history(25)
        error = self.assert_control(self.admit, 429)
        self.assertEqual(error.metadata["daily_remaining"], 0)
        self.assertEqual(error.metadata["weekly_remaining"], 0)
        self.assertEqual(error.metadata["next_reset"], control.boundaries(MOMENT)[3].isoformat())

    def test_utc_day_reset_and_week_reset(self):
        self.history(25)
        self.clock_mock.return_value = MOMENT.replace(hour=0) + timedelta(days=1)
        row = self.admit().request
        control.fail(row, F.LOCAL_CONFIGURATION, release=True)
        self.history(100)
        self.clock_mock.return_value = control.boundaries(MOMENT)[3]
        self.admit()

    def test_other_user_quota_unaffected(self):
        self.history(25)
        self.admit(user=self.other)

    def test_active_slot_blocks_all_operations_and_different_jobs(self):
        self.admit(O.JOB_EXTRACTION)
        for operation in control.CREDITS:
            with self.subTest(operation=operation):
                self.assert_control(lambda: self.admit(operation), 409)
        self.assertEqual(AIRequest.objects.count(), 1)

    def test_database_enforces_identity_and_active_slot(self):
        row = self.admit().request
        for nonce in (row.nonce, uuid.uuid4()):
            with self.subTest(same_nonce=nonce == row.nonce):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    AIRequest.objects.create(user=self.user, nonce=nonce, operation=O.PROFILE_SUMMARY,
                        intent=I.GENERATE, quota_units=1, submitted_fingerprint="a" * 64,
                        effective_fingerprint="b" * 64, lease_expires_at=MOMENT + control.LEASE)

    def test_database_rejects_changed_operation_credit_weight(self):
        row = self.admit().request
        with self.assertRaises(IntegrityError), transaction.atomic():
            AIRequest.objects.filter(pk=row.pk).update(quota_units=2)

    def test_same_active_nonce_replay_and_changed_inputs_conflict(self):
        token = control.issue_nonce(self.user, O.PROFILE_SUMMARY)
        self.admit(token=token)
        self.assert_control(lambda: self.admit(token=token), 409)
        self.assert_control(lambda: self.admit(token=token, submitted={"text": "changed"}), 409)
        self.assert_control(lambda: self.admit(token=token, effective="changed"), 409)
        self.assertEqual(AIRequest.objects.count(), 1)

    def test_completed_replay_returns_existing_request_without_more_credits(self):
        token = control.issue_nonce(self.user, O.PROFILE_SUMMARY)
        row = self.admit(token=token).request
        control.mark_dispatch(row)
        control.succeed(row)
        replay = self.admit(token=token)
        self.assertTrue(replay.replay)
        self.assertEqual(replay.request.pk, row.pk)
        self.assertEqual(control.allowance(self.user)[0]["daily_remaining"], 24)

    def test_failed_and_uncertain_nonces_cannot_redispatch(self):
        for state in (L.FAILED, L.UNCERTAIN):
            with self.subTest(state=state):
                token = control.issue_nonce(self.user, O.PROFILE_SUMMARY)
                row = self.admit(token=token).request
                AIRequest.objects.filter(pk=row.pk).update(lifecycle=state, quota_state=Q.CONSUMED)
                self.assert_control(lambda: self.admit(token=token), 409)

    def test_fresh_nonce_identical_content_can_regenerate(self):
        row = self.admit().request
        control.mark_dispatch(row)
        control.succeed(row)
        fresh = control.issue_nonce(self.user, O.PROFILE_SUMMARY, intent=I.REGENERATE)
        second = self.admit(token=fresh).request
        self.assertEqual(second.intent, I.REGENERATE)
        self.assertNotEqual(row.pk, second.pk)

    def test_undispatched_stale_request_releases_and_old_nonce_stays_terminal(self):
        token = control.issue_nonce(self.user, O.PROFILE_SUMMARY)
        row = self.admit(token=token).request
        self.clock_mock.return_value = MOMENT + control.LEASE
        self.assert_control(lambda: self.admit(token=token), 409)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.FAILED, Q.RELEASED))
        self.admit()

    def test_dispatched_stale_request_consumes_and_new_request_can_start(self):
        row = self.admit().request
        control.mark_dispatch(row)
        self.clock_mock.return_value = MOMENT + control.LEASE
        self.admit(O.JOB_EXTRACTION)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.UNCERTAIN, Q.CONSUMED))

    def test_expired_reservation_cannot_dispatch(self):
        row = self.admit().request
        self.clock_mock.return_value = MOMENT + control.LEASE
        self.assert_control(lambda: control.mark_dispatch(row), 409)
        row.refresh_from_db()
        self.assertIsNone(row.dispatch_started_at)
        self.assertEqual(row.quota_state, Q.RELEASED)

    def test_dispatch_transition_cannot_repeat(self):
        row = self.admit().request
        control.mark_dispatch(row)
        self.assert_control(lambda: control.mark_dispatch(row), 409)

    def test_local_missing_configuration_releases_without_dispatch(self):
        row = self.admit().request
        provider = Mock()
        with override_settings(OPENAI_API_KEY=""), patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            with self.assertRaises(services.AIConfigurationError):
                control.call_provider(row, provider)
        provider.assert_not_called()
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.FAILED, Q.RELEASED))
        self.assertIsNone(row.dispatch_started_at)

    def test_definitive_provider_rejections_release_but_count_burst(self):
        for cls in (services.AIConfigurationError, services.AICapacityError, services.AIRequestError):
            with self.subTest(error=cls.__name__):
                row = self.admit().request
                with self.assertRaises(cls):
                    control.call_provider(row, Mock(side_effect=cls()))
                row.refresh_from_db()
                self.assertEqual((row.lifecycle, row.quota_state), (L.FAILED, Q.RELEASED))
                self.assertIsNotNone(row.dispatch_started_at)
        self.assert_control(self.admit, 429)
        self.assertEqual(control.allowance(self.user)[0]["daily_remaining"], 25)

    def test_ambiguous_and_output_failures_consume(self):
        for cls in (services.AITimeoutError, services.AIConnectionError, services.AITemporaryError, services.AIResponseError):
            with self.subTest(error=cls.__name__):
                AIRequest.objects.all().delete()
                row = self.admit().request
                with self.assertRaises(cls):
                    control.call_provider(row, Mock(side_effect=cls()))
                row.refresh_from_db()
                self.assertEqual((row.lifecycle, row.quota_state), (L.FAILED, Q.CONSUMED))

    def test_stale_rejection_does_not_refund_uncertain_dispatch(self):
        row = self.admit().request
        control.mark_dispatch(row)
        self.clock_mock.return_value = MOMENT + control.LEASE
        control.fail(row, F.CAPACITY, release=True)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.UNCERTAIN, Q.CONSUMED))

    def test_database_admission_failure_is_closed(self):
        with patch("proposal_ai.ai_control._recover_stale", side_effect=OperationalError("private database detail")):
            error = self.assert_control(self.admit, 503)
        self.assertNotIn("private", error.user_message)
        self.assertEqual(AIRequest.objects.count(), 0)

    def test_fingerprint_database_failure_is_closed(self):
        with patch("proposal_ai.ai_control.fingerprint", side_effect=OperationalError("private detail")):
            self.assert_control(self.admit, 503)
        self.assertEqual(AIRequest.objects.count(), 0)

    def test_backend_without_partial_constraints_fails_closed(self):
        with patch.object(connections["default"].features, "supports_partial_indexes", False):
            self.assert_control(self.admit, 503)
        self.assertEqual(AIRequest.objects.count(), 0)

    def test_postgresql_non_read_committed_isolation_fails_closed(self):
        cursor = Mock()
        cursor.fetchone.return_value = ["repeatable read"]
        context = Mock()
        context.__enter__ = Mock(return_value=cursor)
        context.__exit__ = Mock(return_value=False)
        # Exercise the safety branch only; this is not a PostgreSQL lock test.
        with patch("proposal_ai.ai_control._recover_stale"), patch.object(
            connections["default"], "vendor", "postgresql",
        ), patch.object(connections["default"], "cursor", return_value=context):
            self.assert_control(self.admit, 503)
        self.assertEqual(AIRequest.objects.count(), 0)

    def test_database_dispatch_failure_releases_without_provider(self):
        row = self.admit().request
        provider = Mock()
        with patch("proposal_ai.ai_control._owned_live", side_effect=OperationalError("private")):
            self.assert_control(lambda: control.call_provider(row, provider), 503)
        provider.assert_not_called()
        # If coordination remains unavailable, recovery handles the reservation.
        self.clock_mock.return_value = MOMENT + control.LEASE
        control.recover_stale(self.user)
        row.refresh_from_db()
        self.assertEqual(row.quota_state, Q.RELEASED)

    def test_success_fencing_blocks_expired_worker_callback(self):
        row = self.admit().request
        control.mark_dispatch(row)
        self.clock_mock.return_value = MOMENT + control.LEASE
        persist = Mock()
        self.assert_control(lambda: control.succeed(row, persist), 409)
        persist.assert_not_called()
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.UNCERTAIN, Q.CONSUMED))

    def test_ledger_has_no_payload_columns_and_safe_failure_category(self):
        fields = {field.name for field in AIRequest._meta.fields}
        self.assertFalse(fields & {"prompt", "api_key", "job_description", "profile_text", "experience_text", "final_text", "summary"})
        row = self.admit(submitted={"text": "PRIVATE_INPUT"}, effective="PRIVATE_CONTEXT").request
        control.fail(row, "PRIVATE_RAW_EXCEPTION", release=True)
        row.refresh_from_db()
        self.assertEqual(row.failure_category, F.UNEXPECTED)
        self.assertNotIn("PRIVATE", str(AIRequest.objects.values().get()))


@override_settings(**UI_SETTINGS)
class WorkflowTests(TestCase):
    def setUp(self):
        from .test_research_intelligence import seed_research
        seed_research()
        self.user = get_user_model().objects.create_user(username="workflow-owner")
        self.other = get_user_model().objects.create_user(username="workflow-other")
        self.profile = FreelancerProfile.objects.create(user=self.user, professional_title="Developer", profile_summary="PRIVATE_PROFILE")
        self.job = JobPost.objects.create(user=self.user, job_title="Original", job_description="Original job")
        self.experience = WorkExperience.objects.create(user=self.user, job_title="Role", tasks="PRIVATE_EXPERIENCE")
        self.client.force_login(self.user)
        self.clock = patch("proposal_ai.ai_control.now", return_value=MOMENT)
        self.clock_mock = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.providers = {}
        self.real_services = {name: getattr(services, name) for name in (
            "generate_profile_summary", "extract_job_details", "generate_proposal",
        )}
        for name, result in (("generate_profile_summary", "PRIVATE_SUMMARY"),
                             ("extract_job_details", json.dumps({"job_title": "Extracted", "job_description": "PRIVATE_JOB"})),
                             ("generate_proposal", "PRIVATE_PROPOSAL")):
            mocked = patch("proposal_ai.views.services." + name, return_value=service_result(result))
            self.providers[name] = mocked.start()
            self.addCleanup(mocked.stop)

    def test_research_pipeline_fingerprints_exact_provider_prompt_and_replays_once(self):
        from proposal_ai.models import ResearchDataset, ResearchCase, ResearchTerm
        dataset = ResearchDataset.objects.get(active=True)
        case = ResearchCase.objects.create(dataset=dataset, case_key="C001", domain_niche="Django Python",
                                           outcome="Hired", application_route="Cold", strong_features="PRIVATE_RESEARCH_PERSON")
        ResearchTerm.objects.create(research_case=case, token="django")
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="Safe proposal."))],
            model=services.CHAT_MODEL,
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=3, total_tokens=23))
        self.providers["generate_proposal"].side_effect = self.real_services["generate_proposal"]
        token = self.token(O.PROPOSAL_GENERATION)
        with patch("proposal_ai.services._create_client", return_value=client):
            response = self.post(O.PROPOSAL_GENERATION, token, job_title="Django developer")
            self.assertEqual(response.status_code, 302)
            replay = self.post(O.PROPOSAL_GENERATION, token, job_title="Django developer")
            self.assertEqual(replay.status_code, 302)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertIn("INTERNAL RESEARCH DECISION SUPPORT", messages[1]["content"])
        self.assertIn('"selected_cases":1', messages[1]["content"])
        self.assertNotIn("PRIVATE_RESEARCH_PERSON", messages[1]["content"])
        row = AIRequest.objects.get(operation=O.PROPOSAL_GENERATION)
        self.assertEqual(row.effective_fingerprint, control.fingerprint(messages, "effective"))
        self.assertEqual(row.quota_state, Q.CONSUMED)
        self.assertEqual(row.lifecycle, L.SUCCEEDED)
        self.assertEqual(row.input_tokens, 20)
        self.assertEqual(Proposal.objects.get().final_text, "Safe proposal.")

    def test_missing_research_stops_before_reservation_and_provider(self):
        from proposal_ai.models import ResearchDataset
        ResearchDataset.objects.update(active=False)
        response = self.post(O.PROPOSAL_GENERATION)
        self.assertEqual(response.status_code, 503)
        self.providers["generate_proposal"].assert_not_called()
        self.assertFalse(AIRequest.objects.exists())
        self.assertFalse(Proposal.objects.exists())

    def token(self, operation, intent=I.GENERATE):
        resource = self.job.pk if operation == O.PROPOSAL_GENERATION else None
        return control.issue_nonce(self.user, operation, resource, intent)

    def post(self, operation, token=None, **changes):
        data = {"ai_nonce": token if token is not None else self.token(operation)}
        if operation == O.PROFILE_SUMMARY:
            url = reverse("generate_profile_summary")
            data.update(professional_title="Developer", key_skills="Python")
        elif operation == O.JOB_EXTRACTION:
            url = reverse("extract_job_features")
            data.update(raw_job_text="PRIVATE_PASTE developer hourly project", continue_anyway="true")
        else:
            url = reverse("confirm_job_features", args=[self.job.pk])
            data.update(job_title="Updated", job_description="PRIVATE_JOB", experience_selection_submitted="1", selected_experiences=[str(self.experience.pk)])
        data.update(changes)
        return self.client.post(url, data)

    def test_all_four_surfaces_have_valid_server_issued_nonces(self):
        cases = (("create_freelancer_profile", [], O.PROFILE_SUMMARY, "summary_nonce"),
                 ("dashboard", [], O.JOB_EXTRACTION, "ai_nonce"),
                 ("extract_job_features", [], O.JOB_EXTRACTION, "ai_nonce"),
                 ("confirm_job_features", [self.job.pk], O.PROPOSAL_GENERATION, "ai_nonce"))
        for route, args, operation, key in cases:
            with self.subTest(route=route):
                page = self.client.get(reverse(route, args=args))
                resource = self.job.pk if args else None
                control.validate_nonce(page.context[key], self.user, operation, resource)
                self.assertContains(page, 'name="ai_nonce"')
                self.assertContains(page, 'name="csrfmiddlewaretoken"')

    def test_summary_ajax_sends_nonce_and_retains_accessible_busy_state(self):
        page = self.client.get(reverse("create_freelancer_profile"))
        self.assertContains(page, 'requestBody.append("ai_nonce", summaryNonce.value)')
        self.assertContains(page, 'generateButton.disabled = true')
        self.assertContains(page, 'aria-live')
        self.assertContains(page, 'X-ProposalQ-Next-Nonce')

    def test_standalone_extraction_preserves_submitter_before_disabling(self):
        page = self.client.get(reverse("extract_job_features"))
        self.assertContains(page, 'event.submitter')
        self.assertContains(page, 'value.name = submitter.name')
        self.assertContains(page, 'value.value = submitter.value')
        self.assertContains(page, 'button.disabled = true')
        self.assertContains(page, 'pageshow')

    def test_missing_nonce_rejected_before_provider(self):
        result = self.post(O.PROFILE_SUMMARY, "")
        self.assertEqual(result.status_code, 409)
        self.providers["generate_profile_summary"].assert_not_called()
        self.assertEqual(AIRequest.objects.count(), 0)

    def test_invalid_form_can_correct_using_same_unadmitted_nonce(self):
        token = self.token(O.PROFILE_SUMMARY)
        self.assertEqual(self.post(O.PROFILE_SUMMARY, token, professional_title="").status_code, 400)
        self.assertEqual(AIRequest.objects.count(), 0)
        self.assertEqual(self.post(O.PROFILE_SUMMARY, token).status_code, 200)
        self.providers["generate_profile_summary"].assert_called_once()

    def test_changed_invalid_inputs_under_admitted_nonce_are_409(self):
        cases = ((O.PROFILE_SUMMARY, {"professional_title": ""}),
                 (O.JOB_EXTRACTION, {"raw_job_text": ""}),
                 (O.PROPOSAL_GENERATION, {"job_description": ""}))
        for operation, changes in cases:
            with self.subTest(operation=operation):
                token = self.token(operation)
                self.post(operation, token)
                count = AIRequest.objects.count()
                self.assertEqual(self.post(operation, token, **changes).status_code, 409)
                self.assertEqual(AIRequest.objects.count(), count)

    def test_extraction_warning_preserves_nonce_and_continue_anyway(self):
        token = self.token(O.JOB_EXTRACTION)
        url = reverse("extract_job_features")
        page = self.client.post(url, {"ai_nonce": token, "raw_job_text": "short"})
        self.assertEqual(page.context["ai_nonce"], token)
        self.assertEqual(AIRequest.objects.count(), 0)
        self.assertContains(page, 'name="continue_anyway"')
        result = self.client.post(url, {"ai_nonce": token, "raw_job_text": "short", "continue_anyway": "true"})
        self.assertEqual(result.status_code, 302)

    def test_continue_anyway_cannot_bypass_paste_maximum(self):
        result = self.post(O.JOB_EXTRACTION, raw_job_text="x" * 20001)
        self.assertEqual(result.status_code, 200)
        self.providers["extract_job_details"].assert_not_called()
        self.assertEqual(AIRequest.objects.count(), 0)

    def test_extraction_replay_reopens_original_job_without_duplicate(self):
        token = self.token(O.JOB_EXTRACTION)
        first = self.post(O.JOB_EXTRACTION, token)
        second = self.post(O.JOB_EXTRACTION, token)
        self.assertEqual(first.url, second.url)
        self.assertEqual(JobPost.objects.count(), 2)
        self.assertEqual(AIRequest.objects.count(), 1)
        self.providers["extract_job_details"].assert_called_once()

    def test_proposal_replay_does_not_duplicate_or_charge_twice(self):
        token = self.token(O.PROPOSAL_GENERATION)
        self.assertEqual(self.post(O.PROPOSAL_GENERATION, token).status_code, 302)
        self.assertEqual(self.post(O.PROPOSAL_GENERATION, token).status_code, 302)
        self.assertEqual(Proposal.objects.count(), 1)
        self.assertEqual(AIRequest.objects.get().quota_units, 3)
        self.providers["generate_proposal"].assert_called_once()

    def test_summary_replay_does_not_store_or_reconstruct_output(self):
        token = self.token(O.PROFILE_SUMMARY)
        self.assertEqual(self.post(O.PROFILE_SUMMARY, token).json(), {"summary": "PRIVATE_SUMMARY"})
        second = self.post(O.PROFILE_SUMMARY, token)
        self.assertEqual(second.status_code, 409)
        self.assertIn("not stored", second.json()["error"])
        self.assertNotIn("PRIVATE_SUMMARY", str(AIRequest.objects.values().get()))
        self.assertIn("X-ProposalQ-Next-Nonce", second)
        self.providers["generate_profile_summary"].assert_called_once()

    def test_changed_submitted_or_effective_input_gives_409(self):
        token = self.token(O.PROPOSAL_GENERATION)
        self.post(O.PROPOSAL_GENERATION, token)
        self.assertEqual(self.post(O.PROPOSAL_GENERATION, token, job_title="Changed").status_code, 409)
        self.profile.profile_summary = "Changed profile"
        self.profile.save()
        self.assertEqual(self.post(O.PROPOSAL_GENERATION, token).status_code, 409)
        self.providers["generate_proposal"].assert_called_once()

    def test_fresh_explicit_regeneration_of_identical_proposal_allowed(self):
        self.assertEqual(self.post(O.PROPOSAL_GENERATION).status_code, 302)
        page = self.client.get(reverse("confirm_job_features", args=[self.job.pk]) + "?regenerate=1")
        token = page.context["ai_nonce"]
        self.assertEqual(control.validate_nonce(token, self.user, O.PROPOSAL_GENERATION, self.job.pk)[1], I.REGENERATE)
        self.assertEqual(self.post(O.PROPOSAL_GENERATION, token).status_code, 302)
        self.assertEqual(Proposal.objects.count(), 2)
        self.assertEqual(sum(AIRequest.objects.values_list("quota_units", flat=True)), 6)

    def test_failed_nonce_cannot_retry_but_fresh_nonce_can(self):
        token = self.token(O.PROFILE_SUMMARY)
        self.providers["generate_profile_summary"].side_effect = services.AICapacityError()
        self.assertEqual(self.post(O.PROFILE_SUMMARY, token).status_code, 500)
        self.assertEqual(self.post(O.PROFILE_SUMMARY, token).status_code, 409)
        self.providers["generate_profile_summary"].side_effect = None
        self.assertEqual(self.post(O.PROFILE_SUMMARY, self.token(O.PROFILE_SUMMARY, I.REGENERATE)).status_code, 200)
        self.assertEqual(self.providers["generate_profile_summary"].call_count, 2)

    def test_quota_rejection_before_provider_has_429_reset_metadata(self):
        for _ in range(25):
            AIRequest.objects.create(user=self.user, operation=O.PROFILE_SUMMARY, intent=I.GENERATE, nonce=uuid.uuid4(),
                submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64, lifecycle=L.SUCCEEDED,
                quota_state=Q.CONSUMED, quota_units=1, admitted_at=MOMENT, lease_expires_at=MOMENT)
        result = self.post(O.JOB_EXTRACTION)
        self.assertEqual(result.status_code, 429)
        self.assertIn("Retry-After", result)
        self.assertContains(result, "UTC", status_code=429)
        self.assertEqual(result.context["form"]["raw_job_text"].value(), "PRIVATE_PASTE developer hourly project")
        self.providers["extract_job_details"].assert_not_called()

    def test_burst_rejections_after_released_failures_stop_provider(self):
        self.providers["generate_profile_summary"].side_effect = services.AICapacityError()
        for _ in range(3):
            self.assertEqual(self.post(O.PROFILE_SUMMARY).status_code, 500)
        result = self.post(O.PROFILE_SUMMARY)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result["Retry-After"], "600")
        self.assertEqual(self.providers["generate_profile_summary"].call_count, 3)
        self.assertEqual(control.allowance(self.user)[0]["daily_remaining"], 25)

    def test_cross_operation_active_request_is_409(self):
        control.admit(self.user, O.PROFILE_SUMMARY, self.token(O.PROFILE_SUMMARY), {}, "context")
        self.assertEqual(self.post(O.JOB_EXTRACTION).status_code, 409)
        self.providers["extract_job_details"].assert_not_called()

    def test_database_coordination_failure_is_safe_503(self):
        with patch("proposal_ai.ai_control._recover_stale", side_effect=OperationalError("PRIVATE_DATABASE")):
            result = self.post(O.PROFILE_SUMMARY)
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("PRIVATE_DATABASE", result.content.decode())
        self.providers["generate_profile_summary"].assert_not_called()

    def test_missing_local_config_releases_without_provider(self):
        with override_settings(OPENAI_API_KEY=""), patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            self.assertEqual(self.post(O.PROFILE_SUMMARY).status_code, 500)
        row = AIRequest.objects.get()
        self.assertEqual(row.quota_state, Q.RELEASED)
        self.assertIsNone(row.dispatch_started_at)
        self.providers["generate_profile_summary"].assert_not_called()

    def test_malformed_extraction_consumes_and_creates_no_job(self):
        self.providers["extract_job_details"].return_value = service_result("malformed")
        self.assertEqual(self.post(O.JOB_EXTRACTION).status_code, 200)
        self.assertEqual(JobPost.objects.count(), 1)
        row = AIRequest.objects.get()
        self.assertEqual((row.lifecycle, row.quota_state, row.failure_category), (L.FAILED, Q.CONSUMED, F.INVALID_RESPONSE))

    def test_late_extraction_worker_cannot_save(self):
        def expire(prompt):
            self.clock_mock.return_value = MOMENT + control.LEASE
            control.recover_stale(self.user)
            return service_result(json.dumps({"job_title": "Extracted", "job_description": "Description"}))
        self.providers["extract_job_details"].side_effect = expire
        self.assertEqual(self.post(O.JOB_EXTRACTION).status_code, 409)
        self.assertEqual(JobPost.objects.count(), 1)
        self.assertEqual(AIRequest.objects.get().lifecycle, L.UNCERTAIN)

    def test_late_proposal_worker_cannot_save(self):
        def expire(*args):
            self.clock_mock.return_value = MOMENT + control.LEASE
            control.recover_stale(self.user)
            return service_result("Generated")
        self.providers["generate_proposal"].side_effect = expire
        self.assertEqual(self.post(O.PROPOSAL_GENERATION).status_code, 409)
        self.assertEqual(Proposal.objects.count(), 0)
        self.job.refresh_from_db()
        self.assertFalse(self.job.confirmed_by_user)
        self.assertEqual(self.job.job_title, "Original")

    def test_proposal_save_failure_rolls_back_and_consumes(self):
        with patch("proposal_ai.views.Proposal.objects.create", side_effect=IntegrityError("PRIVATE_FAILURE")):
            result = self.post(O.PROPOSAL_GENERATION)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(Proposal.objects.count(), 0)
        self.job.refresh_from_db()
        self.assertEqual((self.job.job_title, self.job.confirmed_by_user), ("Original", False))
        row = AIRequest.objects.get()
        self.assertEqual((row.lifecycle, row.quota_state, row.failure_category), (L.FAILED, Q.CONSUMED, F.PERSISTENCE))

    def test_extraction_save_failure_rolls_back_and_consumes(self):
        with patch("proposal_ai.views.JobPost.save", side_effect=IntegrityError("PRIVATE_FAILURE")):
            result = self.post(O.JOB_EXTRACTION)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(JobPost.objects.count(), 1)
        self.assertEqual(AIRequest.objects.get().quota_state, Q.CONSUMED)

    def test_success_ledger_and_application_references_are_coordinated(self):
        self.post(O.JOB_EXTRACTION)
        extraction = AIRequest.objects.get(operation=O.JOB_EXTRACTION)
        self.assertEqual(extraction.lifecycle, L.SUCCEEDED)
        self.assertEqual(extraction.job_post.job_title, "Extracted")
        self.post(O.PROPOSAL_GENERATION)
        proposal = AIRequest.objects.get(operation=O.PROPOSAL_GENERATION)
        self.assertEqual(proposal.lifecycle, L.SUCCEEDED)
        self.assertEqual(proposal.proposal.job_post_id, self.job.pk)
        ledger = str(list(AIRequest.objects.values()))
        for private in ("PRIVATE_PROFILE", "PRIVATE_JOB", "PRIVATE_EXPERIENCE", "PRIVATE_PROPOSAL", "PRIVATE_SUMMARY", "PRIVATE_PASTE"):
            self.assertNotIn(private, ledger)

    def test_actual_incomplete_and_oversized_outputs_consume_without_persistence(self):
        examples = (
            (O.PROFILE_SUMMARY, "generate_profile_summary", "short", "length"),
            (O.PROFILE_SUMMARY, "generate_profile_summary", "x" * 2001, "stop"),
            (O.JOB_EXTRACTION, "extract_job_details", "x" * 281001, "stop"),
            (O.PROPOSAL_GENERATION, "generate_proposal", "short", "length"),
            (O.PROPOSAL_GENERATION, "generate_proposal", "x" * 8001, "stop"),
        )
        for index, (operation, name, text, finish) in enumerate(examples):
            with self.subTest(operation=operation, finish=finish):
                self.clock_mock.return_value = MOMENT + timedelta(minutes=15 * index)
                fake_response = SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason=finish, message=SimpleNamespace(content=text),
                )])
                with patch("proposal_ai.views.services." + name, new=self.real_services[name]), patch("proposal_ai.services.OpenAI") as sdk:
                    sdk.return_value.__enter__.return_value.chat.completions.create.return_value = fake_response
                    result = self.post(operation)
                self.assertEqual(result.status_code, 500 if operation == O.PROFILE_SUMMARY else 200)
                row = AIRequest.objects.latest("pk")
                self.assertEqual((row.lifecycle, row.quota_state, row.failure_category), (L.FAILED, Q.CONSUMED, F.INCOMPLETE_RESPONSE if finish == "length" else F.OVERSIZED_RESPONSE))
                self.assertEqual(JobPost.objects.count(), 1)
                self.assertEqual(Proposal.objects.count(), 0)

    def test_ledger_reference_failure_rolls_back_extracted_record(self):
        original_filter = AIRequest.objects.filter
        def fail_reference(*args, **kwargs):
            if set(kwargs) == {"pk"}:
                raise IntegrityError("PRIVATE_REFERENCE_FAILURE")
            return original_filter(*args, **kwargs)
        with patch("proposal_ai.ai_control.AIRequest.objects.filter", side_effect=fail_reference):
            result = self.post(O.JOB_EXTRACTION)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(JobPost.objects.count(), 1)
        self.assertEqual((AIRequest.objects.get().lifecycle, AIRequest.objects.get().quota_state), (L.FAILED, Q.CONSUMED))

    def test_uncertain_nonce_after_client_disconnect_cannot_redispatch(self):
        token = self.token(O.PROFILE_SUMMARY)
        row = control.admit(self.user, O.PROFILE_SUMMARY, token,
            {"professional_title": "Developer", "key_skills": "Python"}, "context").request
        control.mark_dispatch(row)
        self.clock_mock.return_value = MOMENT + control.LEASE
        control.recover_stale(self.user)
        with self.assertRaises(control.ControlError):
            control.admit(self.user, O.PROFILE_SUMMARY, token,
                {"professional_title": "Developer", "key_skills": "Python"}, "context")
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.UNCERTAIN, Q.CONSUMED))
        self.providers["generate_profile_summary"].assert_not_called()

    def test_provider_rejections_release_extraction_and_proposal_allowance(self):
        cases = ((O.JOB_EXTRACTION, "extract_job_details", services.AICapacityError),
                 (O.JOB_EXTRACTION, "extract_job_details", services.AIConfigurationError),
                 (O.JOB_EXTRACTION, "extract_job_details", services.AIRequestError),
                 (O.PROPOSAL_GENERATION, "generate_proposal", services.AIConfigurationError),
                 (O.PROPOSAL_GENERATION, "generate_proposal", services.AIRequestError))
        for index, (operation, name, cls) in enumerate(cases):
            with self.subTest(operation=operation, failure=cls.__name__):
                self.clock_mock.return_value = MOMENT + timedelta(minutes=15 * index)
                self.providers[name].side_effect = cls()
                self.assertEqual(self.post(operation).status_code, 200)
                row = AIRequest.objects.latest("pk")
                self.assertEqual((row.lifecycle, row.quota_state), (L.FAILED, Q.RELEASED))
                self.assertIsNotNone(row.dispatch_started_at)
        self.assertEqual(control.allowance(self.user)[0]["daily_remaining"], 25)
        self.assertEqual(JobPost.objects.count(), 1)
        self.assertEqual(Proposal.objects.count(), 0)

    def test_nonce_wrong_user_cannot_dispatch(self):
        token = control.issue_nonce(self.other, O.JOB_EXTRACTION)
        self.assertEqual(self.post(O.JOB_EXTRACTION, token).status_code, 409)
        self.providers["extract_job_details"].assert_not_called()

    def test_cross_user_job_ownership_precedes_request_admission(self):
        job = JobPost.objects.create(user=self.other, job_title="Hidden", job_description="Hidden")
        token = control.issue_nonce(self.user, O.PROPOSAL_GENERATION, job.pk)
        result = self.client.post(reverse("confirm_job_features", args=[job.pk]), {
            "ai_nonce": token, "job_title": "Changed", "job_description": "Changed",
        })
        self.assertEqual(result.status_code, 404)
        self.assertEqual(AIRequest.objects.count(), 0)
        self.providers["generate_proposal"].assert_not_called()

    def test_unmocked_provider_network_fails_even_behind_request_controls(self):
        from mysite.test_runner import ExternalNetworkBlocked
        with patch("proposal_ai.views.services.generate_profile_summary", new=self.real_services["generate_profile_summary"]):
            with self.assertRaises(ExternalNetworkBlocked):
                self.post(O.PROFILE_SUMMARY)


@override_settings(OPENAI_API_KEY=FAKE_KEY)
class ConcurrencyTests(TransactionTestCase):
    """Separate connections test SQLite safety, not PostgreSQL lock semantics."""
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="race-owner")

    def race(self, operations, *, same_nonce=False, complete=False):
        barrier = threading.Barrier(2)
        tokens = [control.issue_nonce(self.user, operation) for operation in operations]
        if same_nonce:
            tokens[1] = tokens[0]
        provider = Mock(return_value=service_result("Generated"))
        results = []
        def attempt(index):
            connections.close_all()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                barrier.wait(timeout=5)
                admitted = control.admit(user, operations[index], tokens[index], {"input": "same"}, "same")
                control.call_provider(admitted.request, provider)
                if complete:
                    control.succeed(admitted.request)
                results.append(200)
            except control.ControlError as error:
                results.append(error.status)
            except BaseException as error:
                results.append(error)
            finally:
                connections.close_all()
        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
        with patch("proposal_ai.ai_control.now", return_value=MOMENT):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result in (200, 409, 429, 503) for result in results), results)
        if connection.vendor == "sqlite" and provider.call_count == 0:
            # Shared-cache SQLite can reject both writers/readers on contention.
            # Verify fail-closed safety; it does not promise one winning worker.
            self.assertNotIn(200, results)
            self.assertIn(503, results)
            self.assertFalse(AIRequest.objects.exclude(dispatch_started_at=None).exists())
            self.assertFalse(AIRequest.objects.filter(admitted_at__gte=MOMENT,
                                                      quota_state=Q.CONSUMED).exists())
        else:
            self.assertEqual(provider.call_count, 1)
        self.assertLessEqual(AIRequest.objects.filter(lifecycle__in=control.ACTIVE).count(), 1)
        return results

    def test_simultaneous_same_nonce_dispatches_at_most_once(self):
        self.race([O.PROFILE_SUMMARY, O.PROFILE_SUMMARY], same_nonce=True)
        self.assertLessEqual(AIRequest.objects.count(), 1)

    def test_simultaneous_different_nonces_share_one_active_slot(self):
        self.race([O.PROFILE_SUMMARY, O.PROFILE_SUMMARY])

    def test_simultaneous_different_operations_share_one_active_slot(self):
        self.race([O.PROFILE_SUMMARY, O.JOB_EXTRACTION])

    def test_final_credit_race_cannot_overspend(self):
        for _ in range(23):
            AIRequest.objects.create(user=self.user, nonce=uuid.uuid4(), operation=O.PROFILE_SUMMARY,
                intent=I.GENERATE, submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
                lifecycle=L.SUCCEEDED, quota_state=Q.CONSUMED, quota_units=1,
                admitted_at=MOMENT - timedelta(hours=1), lease_expires_at=MOMENT)
        self.race([O.JOB_EXTRACTION, O.JOB_EXTRACTION])
        self.assertLessEqual(control.allowance(self.user, MOMENT)[1], 25)

    def test_provider_callback_is_outside_database_transaction(self):
        with patch("proposal_ai.ai_control.now", return_value=MOMENT):
            admitted = control.admit(self.user, O.PROFILE_SUMMARY, control.issue_nonce(self.user, O.PROFILE_SUMMARY), {}, "context")
            def provider():
                self.assertFalse(connections["default"].in_atomic_block)
                return service_result("Generated")
            self.assertEqual(control.call_provider(admitted.request, provider).value, "Generated")
            control.succeed(admitted.request)

    def test_two_users_hold_independent_slots_across_connections(self):
        other = get_user_model().objects.create_user(username="race-other")
        with patch("proposal_ai.ai_control.now", return_value=MOMENT):
            first = control.admit(self.user, O.PROFILE_SUMMARY, control.issue_nonce(self.user, O.PROFILE_SUMMARY), {}, "context")
            control.mark_dispatch(first.request)
            results = []
            def admit_other():
                connections.close_all()
                try:
                    user = get_user_model().objects.get(pk=other.pk)
                    results.append(control.admit(user, O.PROFILE_SUMMARY, control.issue_nonce(user, O.PROFILE_SUMMARY), {}, "context"))
                finally:
                    connections.close_all()
            thread = threading.Thread(target=admit_other)
            thread.start()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(results), 1)
            self.assertEqual(AIRequest.objects.filter(lifecycle__in=control.ACTIVE).count(), 2)

    def test_final_credit_race_with_immediate_completion(self):
        for _ in range(23):
            AIRequest.objects.create(user=self.user, nonce=uuid.uuid4(), operation=O.PROFILE_SUMMARY,
                intent=I.GENERATE, submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
                lifecycle=L.SUCCEEDED, quota_state=Q.CONSUMED, quota_units=1,
                admitted_at=MOMENT - timedelta(hours=1), lease_expires_at=MOMENT)
        outcomes = self.race([O.JOB_EXTRACTION, O.JOB_EXTRACTION], complete=True)
        # SQLite permits both contenders to fail closed. Charge only the winner
        # whose provider ran; do not require availability under contention.
        expected = 23 + 2 * outcomes.count(200)
        self.assertEqual(control.allowance(self.user, MOMENT)[1], expected)
        self.assertLessEqual(expected, 25)
