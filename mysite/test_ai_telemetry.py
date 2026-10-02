"""Task 3D provider evidence, privacy, fencing, migration and admin regressions."""
from copy import copy
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone as utc_timezone
import importlib
import itertools
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import uuid
from unittest.mock import MagicMock, Mock, patch

import httpx
import openai
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import IntegrityError, OperationalError, connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.db.models.query import QuerySet
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from proposal_ai import ai_control as control, services
from proposal_ai.models import AIRequest, FreelancerProfile, JobPost, Proposal
from .test_runner import ExternalNetworkBlocked


O, L, Q, F = AIRequest.Operation, AIRequest.Lifecycle, AIRequest.Quota, AIRequest.Failure
MOMENT = datetime(2026, 10, 1, 12, tzinfo=utc_timezone.utc)
FAKE_KEY = "test-only-not-a-credential"
PRIVATE = "PRIVATE payload prompt profile experience proposal summary secret"
MISSING = object()
UI_SETTINGS = {
    "OPENAI_API_KEY": FAKE_KEY, "ALLOWED_HOSTS": ["testserver"], "DEBUG": False,
    "STORAGES": {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
}
TOKEN_FIELDS = ("input_tokens", "completion_tokens", "reasoning_tokens", "cached_input_tokens", "total_tokens")
TELEMETRY_FIELDS = tuple(item.name for item in fields(services.AITelemetry))


def completion_response(text=" Generated text 😀 ", finish="stop", usage=MISSING,
                        model="gpt-5-test-snapshot", tier="priority"):
    if usage is MISSING:
        usage = SimpleNamespace(prompt_tokens=13, completion_tokens=21, total_tokens=34,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=8),
            prompt_tokens_details=SimpleNamespace(cached_tokens=3), private=PRIVATE)
    return SimpleNamespace(model=model, usage=usage, service_tier=tier, private=PRIVATE,
        _request_id="request-id-not-stored", choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content=text))])


def measured(operation=O.PROFILE_SUMMARY, **changes):
    values = dict(response_model="gpt-5-test-snapshot",
        input_tokens=13, completion_tokens=21, reasoning_tokens=8, cached_input_tokens=3,
        total_tokens=34, provider_latency_ms=15, service_tier="priority", finish_reason="stop",
        response_text_characters=20)
    values.update(changes)
    return replace(services.request_telemetry(operation), **values)


@override_settings(OPENAI_API_KEY=FAKE_KEY)
class SDKTelemetryTests(SimpleTestCase):
    def setUp(self):
        sdk = patch("proposal_ai.services.OpenAI", autospec=True)
        self.constructor = sdk.start()
        self.addCleanup(sdk.stop)
        self.sdk = MagicMock()
        self.constructor.return_value.__enter__.return_value = self.sdk
        self.sdk.chat.completions.create.return_value = completion_response()
        timer = patch("proposal_ai.services.time.perf_counter_ns", side_effect=itertools.count(0, 15_000_000))
        self.timer = timer.start()
        self.addCleanup(timer.stop)

    def invoke(self, operation=O.PROFILE_SUMMARY):
        if operation == O.PROFILE_SUMMARY:
            return services.generate_profile_summary("Private input")
        if operation == O.JOB_EXTRACTION:
            return services.extract_job_details("Private extraction input")
        return services.generate_proposal("Private instructions", "Private context")

    def assert_usage(self, operation, cap):
        result = self.invoke(operation)
        self.assertEqual(result.value, "Generated text 😀")
        self.assertEqual(tuple(getattr(result.telemetry, name) for name in TOKEN_FIELDS), (13, 21, 8, 3, 34))
        self.assertEqual(result.telemetry.completion_token_cap, cap)
        self.assertEqual(self.sdk.chat.completions.create.call_args.kwargs["max_completion_tokens"], cap)

    def test_summary_usage_and_cap(self):
        self.assert_usage(O.PROFILE_SUMMARY, 2048)

    def test_extraction_usage_and_cap(self):
        self.assert_usage(O.JOB_EXTRACTION, 8192)

    def test_proposal_usage_and_cap(self):
        self.assert_usage(O.PROPOSAL_GENERATION, 6144)

    def test_requested_and_reported_identity(self):
        data = self.invoke().telemetry
        self.assertEqual((data.provider, data.api_style, data.requested_model, data.response_model),
                         ("openai", "chat_completions", "gpt-5", "gpt-5-test-snapshot"))
        self.assertEqual((data.service_tier, data.finish_reason), ("priority", "stop"))

    def test_absent_usage_stays_unknown(self):
        self.sdk.chat.completions.create.return_value = completion_response(usage=None)
        data = self.invoke().telemetry
        self.assertTrue(all(getattr(data, name) is None for name in TOKEN_FIELDS))

    def test_missing_optional_objects_and_attributes(self):
        response = completion_response(usage=SimpleNamespace(prompt_tokens=7))
        del response.model, response.service_tier
        self.sdk.chat.completions.create.return_value = response
        data = self.invoke().telemetry
        self.assertEqual(data.input_tokens, 7)
        for name in ("response_model", "service_tier", "completion_tokens", "total_tokens", "reasoning_tokens", "cached_input_tokens"):
            self.assertIsNone(getattr(data, name))

    def test_optional_detail_objects_are_absent(self):
        self.sdk.chat.completions.create.return_value = completion_response(usage=SimpleNamespace(
            prompt_tokens=5, completion_tokens=7, total_tokens=12))
        data = self.invoke().telemetry
        self.assertIsNone(data.reasoning_tokens)
        self.assertIsNone(data.cached_input_tokens)

    def test_optional_nested_fields_are_absent(self):
        self.sdk.chat.completions.create.return_value = completion_response(usage=SimpleNamespace(
            prompt_tokens=5, completion_tokens=7, total_tokens=12,
            completion_tokens_details=SimpleNamespace(), prompt_tokens_details=SimpleNamespace()))
        data = self.invoke().telemetry
        self.assertIsNone(data.reasoning_tokens)
        self.assertIsNone(data.cached_input_tokens)

    def test_reported_zero_is_preserved(self):
        usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            prompt_tokens_details=SimpleNamespace(cached_tokens=0))
        self.sdk.chat.completions.create.return_value = completion_response(usage=usage)
        self.assertEqual(tuple(getattr(self.invoke().telemetry, name) for name in TOKEN_FIELDS), (0, 0, 0, 0, 0))

    def test_invalid_optional_counts_are_unknown_without_rejecting_text(self):
        for bad in (True, False, "12", -1, 1.5, {}, [], 2**63):
            with self.subTest(kind=type(bad).__name__):
                usage = SimpleNamespace(prompt_tokens=bad, completion_tokens=bad, total_tokens=bad,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=bad),
                    prompt_tokens_details=SimpleNamespace(cached_tokens=bad))
                self.sdk.chat.completions.create.return_value = completion_response(usage=usage)
                result = self.invoke()
                self.assertEqual(result.value, "Generated text 😀")
                self.assertTrue(all(getattr(result.telemetry, name) is None for name in TOKEN_FIELDS))

    def test_missing_total_is_not_reconstructed(self):
        self.sdk.chat.completions.create.return_value = completion_response(usage=SimpleNamespace(
            prompt_tokens=5, completion_tokens=7))
        self.assertIsNone(self.invoke().telemetry.total_tokens)

    def test_total_is_copied_and_reasoning_not_added(self):
        usage = SimpleNamespace(prompt_tokens=13, completion_tokens=21, total_tokens=34,
                                completion_tokens_details=SimpleNamespace(reasoning_tokens=8))
        self.sdk.chat.completions.create.return_value = completion_response(usage=usage)
        data = self.invoke().telemetry
        self.assertEqual((data.completion_tokens, data.reasoning_tokens, data.total_tokens), (21, 8, 34))

    def test_invalid_model_and_tier_are_unknown_without_rejecting_text(self):
        for model, tier in ((PRIVATE, PRIVATE), ("x" * 201, "default\n"), (True, {}), (None, None)):
            self.sdk.chat.completions.create.return_value = completion_response(model=model, tier=tier)
            data = self.invoke().telemetry
            self.assertIsNone(data.response_model)
            self.assertIsNone(data.service_tier)

    def test_summary_character_count_before_trimming(self):
        text = "  Summary 😀\n"
        self.sdk.chat.completions.create.return_value = completion_response(text)
        result = self.invoke()
        self.assertEqual(result.value, text.strip())
        self.assertEqual(result.telemetry.response_text_characters, len(text))

    def test_proposal_character_count_is_python_characters(self):
        text = " Proposal 😀😀 "
        self.sdk.chat.completions.create.return_value = completion_response(text)
        self.assertEqual(self.invoke(O.PROPOSAL_GENERATION).telemetry.response_text_characters, len(text))

    def test_extraction_character_count_measures_json_completion(self):
        text = ' {"job_title": "😀", "job_description": "Details"} '
        self.sdk.chat.completions.create.return_value = completion_response(text)
        self.assertEqual(self.invoke(O.JOB_EXTRACTION).telemetry.response_text_characters, len(text))

    def test_success_latency_is_integer_milliseconds(self):
        value = self.invoke().telemetry.provider_latency_ms
        self.assertEqual(value, 15)
        self.assertIs(type(value), int)
        self.assertEqual(self.timer.call_count, 2)

    def test_sub_millisecond_call_can_be_zero(self):
        with patch("proposal_ai.services.time.perf_counter_ns", side_effect=[1_000_000, 1_999_999]):
            self.assertEqual(self.invoke().telemetry.provider_latency_ms, 0)

    def test_timing_excludes_client_construction_enter_and_close(self):
        events = []
        def tick():
            events.append("tick")
            return len(events) * 1_000_000
        def construct(**kwargs):
            events.append("construct")
            return self.sdk
        self.constructor.side_effect = construct
        self.sdk.__enter__.side_effect = lambda: events.append("enter") or self.sdk
        self.sdk.__exit__.side_effect = lambda *args: events.append("close") or False
        self.sdk.chat.completions.create.side_effect = lambda **kwargs: events.append("sdk") or completion_response()
        with patch("proposal_ai.services.time.perf_counter_ns", side_effect=tick):
            self.invoke()
        self.assertEqual(events, ["construct", "enter", "tick", "sdk", "tick", "close"])

    def test_wall_clock_is_not_used_for_provider_elapsed_time(self):
        with patch("proposal_ai.services.time.time", side_effect=AssertionError("Wall clock must not measure latency")):
            self.assertEqual(self.invoke().telemetry.provider_latency_ms, 15)

    def provider_error(self, error, expected):
        self.sdk.chat.completions.create.side_effect = error
        with self.assertRaises(expected) as caught:
            self.invoke()
        data = caught.exception.telemetry
        self.assertEqual(data.provider_latency_ms, 15)
        self.assertEqual(data.requested_model, "gpt-5")
        self.assertTrue(all(getattr(data, name) is None for name in TOKEN_FIELDS))
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_timeout_latency_and_unknown_usage(self):
        self.provider_error(openai.APITimeoutError(httpx.Request("POST", "https://example.invalid")), services.AITimeoutError)

    def test_connection_latency_and_unknown_usage(self):
        self.provider_error(openai.APIConnectionError(message=PRIVATE,
            request=httpx.Request("POST", "https://example.invalid")), services.AIConnectionError)

    def test_provider_rejections_have_latency_without_reading_error_body(self):
        for sdk_type, status, failure in ((openai.AuthenticationError, 401, services.AIAuthenticationError),
                (openai.RateLimitError, 429, services.AICapacityError), (openai.BadRequestError, 400, services.AIRequestError),
                (openai.InternalServerError, 500, services.AITemporaryError)):
            with self.subTest(status=status):
                response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid"))
                self.provider_error(sdk_type(PRIVATE, response=response, body={"usage": {"total_tokens": 999}, "secret": PRIVATE}), failure)

    def test_local_client_configuration_failure_has_no_latency(self):
        self.constructor.side_effect = ValueError(PRIVATE)
        with self.assertRaises(services.AIConfigurationError) as caught:
            self.invoke()
        self.assertEqual(caught.exception.telemetry.requested_model, "gpt-5")
        self.assertIsNone(caught.exception.telemetry.provider_latency_ms)
        self.timer.assert_not_called()

    def test_missing_key_has_no_latency_or_sdk_invocation(self):
        with override_settings(OPENAI_API_KEY=""), patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            with self.assertRaises(services.AIConfigurationError) as caught:
                self.invoke()
        self.assertIsNone(caught.exception.telemetry.provider_latency_ms)
        self.constructor.assert_not_called()
        self.timer.assert_not_called()

    def test_input_rejection_has_no_latency_or_sdk_invocation(self):
        with self.assertRaises(services.AIInputError) as caught:
            services.generate_profile_summary("x" * 4001)
        self.assertIsNone(caught.exception.telemetry.provider_latency_ms)
        self.constructor.assert_not_called()

    def test_incomplete_output_retains_usage_and_length(self):
        self.sdk.chat.completions.create.return_value = completion_response("Partial", "length")
        with self.assertRaises(services.AIIncompleteResponseError) as caught:
            self.invoke()
        self.assertEqual(caught.exception.telemetry.total_tokens, 34)
        self.assertEqual(caught.exception.telemetry.finish_reason, "length")
        self.assertEqual(caught.exception.telemetry.response_text_characters, 7)

    def test_all_non_stop_finishes_still_reject_output(self):
        for finish in ("length", "content_filter", "tool_calls", "function_call", "arbitrary", None):
            self.sdk.chat.completions.create.return_value = completion_response("Partial", finish)
            with self.assertRaises(services.AIResponseError) as caught:
                self.invoke()
            self.assertEqual(caught.exception.telemetry.total_tokens, 34)

    def test_oversized_outputs_retain_usage_before_rejection(self):
        for operation, size in ((O.PROFILE_SUMMARY, 2001), (O.JOB_EXTRACTION, 281001), (O.PROPOSAL_GENERATION, 8001)):
            self.sdk.chat.completions.create.return_value = completion_response("x" * size)
            with self.assertRaises(services.AIOversizedResponseError) as caught:
                self.invoke(operation)
            self.assertEqual(caught.exception.telemetry.total_tokens, 34)
            self.assertEqual(caught.exception.telemetry.response_text_characters, size)

    def test_malformed_choices_retain_available_typed_usage(self):
        response = completion_response()
        response.choices = []
        self.sdk.chat.completions.create.return_value = response
        with self.assertRaises(services.AIResponseError) as caught:
            self.invoke()
        self.assertEqual(caught.exception.telemetry.total_tokens, 34)
        self.assertIsNone(caught.exception.telemetry.finish_reason)
        self.assertIsNone(caught.exception.telemetry.response_text_characters)

    def test_non_text_content_retains_usage_but_not_character_count(self):
        self.sdk.chat.completions.create.return_value = completion_response({"private": PRIVATE})
        with self.assertRaises(services.AIResponseError) as caught:
            self.invoke()
        self.assertEqual(caught.exception.telemetry.total_tokens, 34)
        self.assertIsNone(caught.exception.telemetry.response_text_characters)

    def test_result_and_telemetry_are_immutable_and_repr_excludes_text(self):
        result = services.AIServiceResult(PRIVATE, measured())
        with self.assertRaises(FrozenInstanceError):
            result.value = "changed"
        with self.assertRaises(FrozenInstanceError):
            result.telemetry.total_tokens = 999
        self.assertNotIn(PRIVATE, repr(result))

    def test_only_approved_scalars_cross_service_boundary(self):
        data = self.invoke().telemetry.scalar_fields()
        self.assertEqual(set(data), set(TELEMETRY_FIELDS))
        self.assertTrue(all(value is None or type(value) in (str, int) for value in data.values()))
        self.assertNotIn(PRIVATE, json.dumps(data))
        self.assertNotIn("request-id-not-stored", json.dumps(data))

    def test_sdk_debug_payload_suppression_remains_active(self):
        for name in ("openai._base_client", "openai._response"):
            with self.assertNoLogs(name, level="DEBUG"):
                logging.getLogger(name).debug("Response/usage/error: %s", {"secret": PRIVATE, "key": FAKE_KEY})

    def test_installed_sdk_typed_usage_is_captured_without_network(self):
        constructor = openai.OpenAI
        def handler(request):
            return httpx.Response(200, json={"id": "test-completion", "object": "chat.completion", "created": 0,
                "model": "gpt-5-test-snapshot", "service_tier": "default",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Success"}}],
                "usage": {"prompt_tokens": 13, "completion_tokens": 21, "total_tokens": 34,
                    "completion_tokens_details": {"reasoning_tokens": 8}, "prompt_tokens_details": {"cached_tokens": 3}}})
        def create(**kwargs):
            return constructor(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.constructor.side_effect = create
        data = self.invoke().telemetry
        self.assertEqual(tuple(getattr(data, name) for name in TOKEN_FIELDS), (13, 21, 8, 3, 34))
        self.assertEqual((data.response_model, data.service_tier), ("gpt-5-test-snapshot", "default"))


@override_settings(**UI_SETTINGS)
class LedgerCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="telemetry-owner")
        self.other = get_user_model().objects.create_user(username="telemetry-other")
        clock = patch("proposal_ai.ai_control.now", return_value=MOMENT)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def admit(self, operation=O.PROFILE_SUMMARY, token=None, resource=None):
        token = token or control.issue_nonce(self.user, operation, resource)
        return control.admit(self.user, operation, token, {"input": "same"}, "same", resource)

    def dispatched(self):
        row = self.admit().request
        control.mark_dispatch(row)
        return row

    def assert_unknown_tokens(self, row):
        self.assertTrue(all(getattr(row, name) is None for name in TOKEN_FIELDS))


class ControllerTelemetryTests(LedgerCase):
    def test_dispatch_records_identity_and_exact_cap(self):
        row = self.dispatched()
        row.refresh_from_db()
        self.assertEqual((row.provider, row.api_style, row.requested_model, row.completion_token_cap),
                         ("openai", "chat_completions", "gpt-5", 2048))
        self.assertIsNone(row.provider_latency_ms)
        self.assert_unknown_tokens(row)

    def test_recording_does_not_finalize_or_change_quota(self):
        row = self.dispatched()
        control.record_telemetry(row, measured())
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state, row.quota_units), (L.IN_FLIGHT, Q.RESERVED, 1))
        self.assertEqual(row.total_tokens, 34)
        self.assertEqual(control.allowance(self.user)[0]["daily_remaining"], 24)

    def test_invalid_measurements_are_null_in_database(self):
        row = self.dispatched()
        data = services.AITelemetry(input_tokens=True, completion_tokens="21", reasoning_tokens=-1,
            cached_input_tokens=[], total_tokens=0, provider_latency_ms=1.5, response_model=PRIVATE)
        control.record_telemetry(row, data)
        row.refresh_from_db()
        self.assertEqual(row.total_tokens, 0)
        for name in ("input_tokens", "completion_tokens", "reasoning_tokens", "cached_input_tokens", "provider_latency_ms", "response_model"):
            self.assertIsNone(getattr(row, name))
        self.assertEqual(row.requested_model, "gpt-5")

    def test_reserved_request_cannot_record_provider_outcome(self):
        row = self.admit().request
        with self.assertRaises(control.ControlError):
            control.record_telemetry(row, measured())
        row.refresh_from_db()
        self.assertIsNone(row.total_tokens)

    def test_wrong_account_cannot_record(self):
        row = self.dispatched()
        forged = copy(row)
        forged.user_id = self.other.pk
        with self.assertRaises(control.ControlError):
            control.record_telemetry(forged, measured())
        row.refresh_from_db()
        self.assertIsNone(row.total_tokens)

    def test_wrong_nonce_cannot_record(self):
        row = self.dispatched()
        forged = copy(row)
        forged.nonce = uuid.uuid4()
        with self.assertRaises(control.ControlError):
            control.record_telemetry(forged, measured())
        row.refresh_from_db()
        self.assertIsNone(row.total_tokens)

    def test_expired_worker_cannot_record_and_is_uncertain(self):
        row = self.dispatched()
        self.clock.return_value = MOMENT + control.LEASE
        with self.assertRaises(control.ControlError):
            control.record_telemetry(row, measured())
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.UNCERTAIN, Q.CONSUMED))
        self.assertIsNone(row.total_tokens)

    def test_succeeded_request_telemetry_cannot_be_overwritten(self):
        row = self.dispatched()
        control.record_telemetry(row, measured())
        control.succeed(row)
        with self.assertRaises(control.ControlError):
            control.record_telemetry(row, replace(measured(), total_tokens=999))
        row.refresh_from_db()
        self.assertEqual(row.total_tokens, 34)

    def test_failed_request_telemetry_cannot_be_overwritten(self):
        row = self.dispatched()
        control.fail(row, F.TIMEOUT, telemetry=measured())
        with self.assertRaises(control.ControlError):
            control.record_telemetry(row, replace(measured(), total_tokens=999))
        row.refresh_from_db()
        self.assertEqual(row.total_tokens, 34)

    def test_stale_recovery_preserves_recorded_evidence(self):
        row = self.dispatched()
        control.record_telemetry(row, measured())
        self.clock.return_value = MOMENT + control.LEASE
        control.recover_stale(self.user)
        with self.assertRaises(control.ControlError):
            control.record_telemetry(row, replace(measured(), total_tokens=999))
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state, row.total_tokens), (L.UNCERTAIN, Q.CONSUMED, 34))

    def test_late_failure_cannot_overwrite_terminal_evidence(self):
        row = self.dispatched()
        control.record_telemetry(row, measured())
        self.clock.return_value = MOMENT + control.LEASE
        control.fail(row, F.AUTHENTICATION, release=True, telemetry=replace(measured(), total_tokens=999))
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state, row.total_tokens), (L.UNCERTAIN, Q.CONSUMED, 34))

    def test_replay_reuses_request_and_never_updates_telemetry(self):
        token = control.issue_nonce(self.user, O.PROFILE_SUMMARY)
        row = self.admit(token=token).request
        provider = Mock(return_value=services.AIServiceResult("Summary", measured()))
        control.call_provider(row, provider)
        control.succeed(row)
        replay = self.admit(token=token)
        self.assertTrue(replay.replay)
        self.assertEqual(replay.request.pk, row.pk)
        self.assertEqual(AIRequest.objects.count(), 1)
        self.assertEqual(replay.request.total_tokens, 34)
        provider.assert_called_once()

    def test_fresh_regeneration_has_distinct_evidence(self):
        ids = []
        for total in (34, 40):
            token = control.issue_nonce(self.user, O.PROFILE_SUMMARY, intent=AIRequest.Intent.REGENERATE)
            row = self.admit(token=token).request
            control.call_provider(row, Mock(return_value=services.AIServiceResult("Same summary", replace(measured(), total_tokens=total))))
            control.succeed(row)
            ids.append(row.pk)
        self.assertNotEqual(*ids)
        self.assertEqual(list(AIRequest.objects.order_by("pk").values_list("total_tokens", flat=True)), [34, 40])
        self.assertEqual(control.allowance(self.user)[0]["daily_remaining"], 23)

    def test_missing_preflight_configuration_releases_with_unknown_usage(self):
        row = self.admit().request
        provider = Mock()
        with override_settings(OPENAI_API_KEY=""), patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            with self.assertRaises(services.AIConfigurationError):
                control.call_provider(row, provider)
        provider.assert_not_called()
        row.refresh_from_db()
        self.assertEqual((row.failure_category, row.quota_state), (F.LOCAL_CONFIGURATION, Q.RELEASED))
        self.assertEqual(row.requested_model, "gpt-5")
        self.assertIsNone(row.provider_latency_ms)
        self.assert_unknown_tokens(row)

    def assert_failure(self, failure, category, quota):
        row = self.admit().request
        evidence = replace(services.request_telemetry(O.PROFILE_SUMMARY), provider_latency_ms=15)
        with self.assertRaises(failure):
            control.call_provider(row, Mock(side_effect=failure(evidence)))
        row.refresh_from_db()
        self.assertEqual((row.failure_category, row.quota_state), (category, quota))
        self.assertEqual(row.provider_latency_ms, 15)
        self.assertIsNotNone(row.dispatch_started_at)
        self.assert_unknown_tokens(row)

    def test_authentication_category_retains_released_accounting(self):
        self.assert_failure(services.AIAuthenticationError, F.AUTHENTICATION, Q.RELEASED)

    def test_capacity_category_retains_released_accounting(self):
        self.assert_failure(services.AICapacityError, F.CAPACITY, Q.RELEASED)

    def test_invalid_request_category_retains_released_accounting(self):
        self.assert_failure(services.AIRequestError, F.INVALID_REQUEST, Q.RELEASED)

    def test_timeout_retains_consumed_accounting(self):
        self.assert_failure(services.AITimeoutError, F.TIMEOUT, Q.CONSUMED)

    def test_connection_retains_consumed_accounting(self):
        self.assert_failure(services.AIConnectionError, F.CONNECTION, Q.CONSUMED)

    def test_server_failure_retains_consumed_accounting(self):
        self.assert_failure(services.AITemporaryError, F.TEMPORARY, Q.CONSUMED)

    def test_dispatched_released_failures_still_exhaust_burst(self):
        for _ in range(3):
            row = self.admit().request
            with self.assertRaises(services.AIAuthenticationError):
                control.call_provider(row, Mock(side_effect=services.AIAuthenticationError()))
        with self.assertRaises(control.ControlError) as caught:
            self.admit()
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 600)
        self.assertEqual(control.allowance(self.user)[0]["daily_remaining"], 25)

    def test_telemetry_write_failure_consumes_without_retry(self):
        row = self.admit().request
        provider = Mock(return_value=services.AIServiceResult("Summary", measured()))
        original = QuerySet.update
        def reject_metadata(query, **values):
            if "provider_latency_ms" in values and "lifecycle" not in values:
                raise OperationalError(PRIVATE)
            return original(query, **values)
        with patch.object(QuerySet, "update", new=reject_metadata):
            with self.assertRaises(control.ControlError) as caught:
                control.call_provider(row, provider)
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn(PRIVATE, str(caught.exception))
        provider.assert_called_once()
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state, row.failure_category), (L.FAILED, Q.CONSUMED, F.COORDINATION))
        self.assertIsNone(row.total_tokens)

    def test_full_database_outage_retains_reservation_for_stale_recovery(self):
        row = self.admit().request
        provider = Mock(return_value=services.AIServiceResult("Summary", measured()))
        original = QuerySet.update
        def unavailable_after_dispatch(query, **values):
            if values.get("lifecycle") == L.IN_FLIGHT:
                return original(query, **values)
            raise OperationalError(PRIVATE)
        with patch.object(QuerySet, "update", new=unavailable_after_dispatch):
            with self.assertRaises(control.ControlError) as caught:
                control.call_provider(row, provider)
        self.assertEqual(caught.exception.status, 503)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.IN_FLIGHT, Q.RESERVED))
        self.clock.return_value = MOMENT + control.LEASE
        control.recover_stale(self.user)
        row.refresh_from_db()
        self.assertEqual((row.lifecycle, row.quota_state), (L.UNCERTAIN, Q.CONSUMED))
        provider.assert_called_once()

    def test_payload_and_raw_error_never_enter_ledger(self):
        row = self.admit().request
        failure = services.AIAuthenticationError(services.request_telemetry(O.PROFILE_SUMMARY))
        failure.args = (PRIVATE,)
        with self.assertRaises(services.AIAuthenticationError):
            control.call_provider(row, Mock(side_effect=failure))
        self.assertNotIn(PRIVATE, str(list(AIRequest.objects.values())))


class WorkflowTelemetryTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.profile = FreelancerProfile.objects.create(user=self.user, professional_title="Developer", profile_summary=PRIVATE)
        self.job = JobPost.objects.create(user=self.user, job_title="Original", job_description=PRIVATE)
        self.client.force_login(self.user)
        sdk = patch("proposal_ai.services.OpenAI", autospec=True)
        self.constructor = sdk.start()
        self.addCleanup(sdk.stop)
        self.sdk = self.constructor.return_value.__enter__.return_value
        timer = patch("proposal_ai.services.time.perf_counter_ns", side_effect=itertools.count(0, 15_000_000))
        self.timer = timer.start()
        self.addCleanup(timer.stop)

    def post(self, operation, text="Summary", finish="stop", token=None):
        resource = self.job.pk if operation == O.PROPOSAL_GENERATION else None
        data = {"ai_nonce": token or control.issue_nonce(self.user, operation, resource)}
        if operation == O.PROFILE_SUMMARY:
            url = reverse("generate_profile_summary")
            data.update(professional_title="Developer", key_skills="Python")
        elif operation == O.JOB_EXTRACTION:
            url = reverse("extract_job_features")
            data.update(raw_job_text="Looking for a developer for an hourly project", continue_anyway="true")
        else:
            url = reverse("confirm_job_features", args=[self.job.pk])
            data.update(job_title="Updated", job_description="Updated description", selected_experiences=[])
        self.sdk.chat.completions.create.return_value = completion_response(text, finish)
        return self.client.post(url, data)

    def assert_evidence(self, quota=Q.CONSUMED):
        row = AIRequest.objects.get()
        self.assertEqual((row.input_tokens, row.completion_tokens, row.reasoning_tokens, row.cached_input_tokens, row.total_tokens), (13, 21, 8, 3, 34))
        self.assertEqual(row.provider_latency_ms, 15)
        self.assertEqual(row.quota_state, quota)
        self.assertEqual(row.response_model, "gpt-5-test-snapshot")
        return row

    def test_summary_success_records_usage_without_saving_profile(self):
        response = self.post(O.PROFILE_SUMMARY, PRIVATE)
        self.assertEqual(response.status_code, 200)
        row = self.assert_evidence()
        self.assertEqual(row.lifecycle, L.SUCCEEDED)
        self.assertEqual(row.response_text_characters, len(PRIVATE))
        self.assertNotIn(PRIVATE, str(list(AIRequest.objects.values())))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.profile_summary, PRIVATE)

    def test_extraction_success_records_json_length_and_result_reference(self):
        text = json.dumps({"job_title": "Extracted", "job_description": PRIVATE})
        self.assertEqual(self.post(O.JOB_EXTRACTION, text).status_code, 302)
        row = self.assert_evidence()
        self.assertEqual(row.response_text_characters, len(text))
        self.assertEqual(row.job_post.job_title, "Extracted")
        self.assertEqual(row.lifecycle, L.SUCCEEDED)

    def test_proposal_success_records_usage_and_result_reference(self):
        self.assertEqual(self.post(O.PROPOSAL_GENERATION, PRIVATE).status_code, 302)
        row = self.assert_evidence()
        self.assertEqual(row.proposal.final_text, PRIVATE)
        self.assertEqual(row.response_text_characters, len(PRIVATE))
        self.assertEqual(row.completion_token_cap, 6144)

    def test_extraction_json_rejection_preserves_provider_evidence(self):
        self.assertEqual(self.post(O.JOB_EXTRACTION, "not JSON").status_code, 200)
        row = self.assert_evidence()
        self.assertEqual((row.lifecycle, row.failure_category), (L.FAILED, F.INVALID_RESPONSE))
        self.assertEqual(JobPost.objects.count(), 1)

    def test_extraction_schema_rejection_preserves_provider_evidence(self):
        self.assertEqual(self.post(O.JOB_EXTRACTION, '{"job_title": "Missing description"}').status_code, 200)
        row = self.assert_evidence()
        self.assertEqual(row.failure_category, F.INVALID_RESPONSE)
        self.assertEqual(JobPost.objects.count(), 1)

    def test_incomplete_response_consumes_and_retains_usage(self):
        self.assertEqual(self.post(O.PROPOSAL_GENERATION, "Partial", "length").status_code, 200)
        row = self.assert_evidence()
        self.assertEqual((row.failure_category, row.finish_reason), (F.INCOMPLETE_RESPONSE, "length"))
        self.assertEqual(Proposal.objects.count(), 0)

    def test_oversized_response_consumes_and_retains_usage(self):
        self.assertEqual(self.post(O.PROFILE_SUMMARY, "x" * 2001).status_code, 500)
        row = self.assert_evidence()
        self.assertEqual((row.failure_category, row.response_text_characters), (F.OVERSIZED_RESPONSE, 2001))

    def test_job_persistence_rollback_does_not_erase_provider_evidence(self):
        with patch("proposal_ai.views.JobPost.save", side_effect=IntegrityError(PRIVATE)):
            response = self.post(O.JOB_EXTRACTION, '{"job_title": "Extracted", "job_description": "Description"}')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(JobPost.objects.count(), 1)
        self.assertEqual(self.assert_evidence().failure_category, F.PERSISTENCE)

    def test_proposal_persistence_rollback_preserves_telemetry_and_job(self):
        with patch("proposal_ai.views.Proposal.objects.create", side_effect=IntegrityError(PRIVATE)):
            response = self.post(O.PROPOSAL_GENERATION, "Generated")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.assert_evidence().failure_category, F.PERSISTENCE)
        self.assertEqual(Proposal.objects.count(), 0)
        self.job.refresh_from_db()
        self.assertEqual(self.job.job_title, "Original")
        self.assertFalse(self.job.confirmed_by_user)

    def test_persistence_delay_is_excluded_from_provider_latency(self):
        original = JobPost.save
        def delayed_save(job, *args, **kwargs):
            self.assertEqual(self.timer.call_count, 2)
            self.clock.return_value = MOMENT + control.LEASE / 2
            return original(job, *args, **kwargs)
        with patch.object(JobPost, "save", new=delayed_save):
            self.assertEqual(self.post(O.JOB_EXTRACTION, '{"job_title": "Extracted", "job_description": "Description"}').status_code, 302)
        self.assertEqual(self.assert_evidence().provider_latency_ms, 15)
        self.assertEqual(self.timer.call_count, 2)

    def test_successful_extraction_replay_reuses_evidence_and_job(self):
        token = control.issue_nonce(self.user, O.JOB_EXTRACTION)
        text = '{"job_title": "Extracted", "job_description": "Description"}'
        first = self.post(O.JOB_EXTRACTION, text, token=token)
        before = list(AIRequest.objects.values())
        again = self.post(O.JOB_EXTRACTION, text, token=token)
        self.assertEqual(first.url, again.url)
        self.assertEqual(list(AIRequest.objects.values()), before)
        self.assertEqual(JobPost.objects.count(), 2)
        self.sdk.chat.completions.create.assert_called_once()

    def test_late_extraction_cannot_write_telemetry_or_job(self):
        def late(**kwargs):
            self.clock.return_value = MOMENT + control.LEASE
            return completion_response('{"job_title": "Extracted", "job_description": "Description"}')
        self.sdk.chat.completions.create.side_effect = late
        self.assertEqual(self.post(O.JOB_EXTRACTION).status_code, 409)
        row = AIRequest.objects.get()
        self.assertEqual((row.lifecycle, row.quota_state), (L.UNCERTAIN, Q.CONSUMED))
        self.assertIsNone(row.total_tokens)
        self.assertEqual(JobPost.objects.count(), 1)

    def test_late_proposal_cannot_write_telemetry_or_application(self):
        def late(**kwargs):
            self.clock.return_value = MOMENT + control.LEASE
            return completion_response("Generated")
        self.sdk.chat.completions.create.side_effect = late
        self.assertEqual(self.post(O.PROPOSAL_GENERATION).status_code, 409)
        self.assertIsNone(AIRequest.objects.get().total_tokens)
        self.assertEqual(Proposal.objects.count(), 0)
        self.job.refresh_from_db()
        self.assertFalse(self.job.confirmed_by_user)

    def test_metadata_write_failure_prevents_application_persistence(self):
        original = QuerySet.update
        def reject_metadata(query, **values):
            if "provider_latency_ms" in values and "lifecycle" not in values:
                raise OperationalError(PRIVATE)
            return original(query, **values)
        with patch.object(QuerySet, "update", new=reject_metadata):
            response = self.post(O.PROPOSAL_GENERATION, "Generated")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(Proposal.objects.count(), 0)
        self.assertEqual(AIRequest.objects.get().quota_state, Q.CONSUMED)
        self.sdk.chat.completions.create.assert_called_once()

    def test_network_guard_remains_active_behind_telemetry(self):
        with patch("proposal_ai.services.OpenAI", new=openai.OpenAI):
            with self.assertRaises(ExternalNetworkBlocked):
                self.post(O.PROFILE_SUMMARY)


class AdminTelemetryTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.row = self.dispatched()
        control.record_telemetry(self.row, measured())
        control.succeed(self.row)
        permission = Permission.objects.get(content_type__app_label="proposal_ai", codename="view_airequest")
        self.staff = get_user_model().objects.create_user(username="telemetry-staff", is_staff=True)
        self.staff.user_permissions.add(permission)
        self.user.user_permissions.add(permission)
        self.client.force_login(self.staff)
        self.list_url = reverse("admin:proposal_ai_airequest_changelist")
        self.detail_url = reverse("admin:proposal_ai_airequest_change", args=[self.row.pk])

    def test_ledger_registered_in_installed_app_admin(self):
        self.assertIn(AIRequest, admin.site._registry)

    def test_staff_with_view_permission_can_inspect(self):
        self.assertEqual(self.client.get(self.list_url).status_code, 200)
        self.assertContains(self.client.get(self.detail_url), "gpt-5-test-snapshot")

    def test_staff_without_model_permission_cannot_inspect(self):
        self.staff.user_permissions.clear()
        self.assertEqual(self.client.get(self.list_url).status_code, 403)

    def test_nonstaff_even_with_view_permission_cannot_access_admin(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.list_url).status_code, 302)

    def test_add_is_disabled_even_for_superuser(self):
        self.staff.is_superuser = True
        self.staff.save()
        self.assertEqual(self.client.get(reverse("admin:proposal_ai_airequest_add")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:proposal_ai_airequest_add"), {}).status_code, 403)

    def test_change_is_disabled_even_for_superuser(self):
        self.staff.is_superuser = True
        self.staff.save()
        self.assertEqual(self.client.post(self.detail_url, {"quota_units": 999}).status_code, 403)
        self.row.refresh_from_db()
        self.assertEqual(self.row.quota_units, 1)

    def test_delete_is_disabled_even_for_superuser(self):
        self.staff.is_superuser = True
        self.staff.save()
        url = reverse("admin:proposal_ai_airequest_delete", args=[self.row.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
        self.assertTrue(AIRequest.objects.filter(pk=self.row.pk).exists())

    def test_bulk_mutation_action_is_unavailable(self):
        response = self.client.get(self.list_url)
        self.assertFalse(response.context["cl"].model_admin.get_actions(response.wsgi_request))
        self.client.post(self.list_url, {"action": "delete_selected", "_selected_action": [self.row.pk]})
        self.assertTrue(AIRequest.objects.filter(pk=self.row.pk).exists())

    def test_private_fingerprints_nonces_and_related_text_are_not_displayed(self):
        job = JobPost.objects.create(user=self.user, job_title=PRIVATE, job_description=PRIVATE)
        AIRequest.objects.filter(pk=self.row.pk).update(job_post=job)
        self.row.refresh_from_db()
        for url in (self.list_url, self.detail_url):
            response = self.client.get(url)
            for value in (PRIVATE, str(self.row.nonce), self.row.submitted_fingerprint, self.row.effective_fingerprint):
                self.assertNotContains(response, value)

    def test_operation_and_failure_filters_work(self):
        response = self.client.get(self.list_url, {"operation__exact": O.JOB_EXTRACTION})
        self.assertEqual(response.context["cl"].result_count, 0)
        response = self.client.get(self.list_url, {"operation__exact": O.PROFILE_SUMMARY})
        self.assertEqual(response.context["cl"].result_count, 1)


class MigrationTelemetryTests(SimpleTestCase):
    def test_exactly_one_additive_telemetry_migration_without_backfill(self):
        files = list(Path("proposal_ai/migrations").glob("0015*.py"))
        self.assertEqual([p.name for p in files], ["0015_ai_request_telemetry.py"])
        migration = importlib.import_module("proposal_ai.migrations.0015_ai_request_telemetry").Migration
        self.assertEqual(migration.dependencies, [("proposal_ai", "0014_ai_request")])
        added = [operation for operation in migration.operations if isinstance(operation, migrations.AddField)]
        self.assertEqual({op.name for op in added}, set(TELEMETRY_FIELDS))
        self.assertTrue(all(op.model_name == "airequest" and op.field.null for op in added))
        changed = [operation for operation in migration.operations if isinstance(operation, migrations.AlterField)]
        self.assertEqual([(op.model_name, op.name) for op in changed], [("airequest", "failure_category")])
        self.assertEqual(len(migration.operations), len(added) + len(changed))

    def test_schema_has_no_payload_cost_or_provider_request_identifier(self):
        names = {field.name for field in AIRequest._meta.fields}
        prohibited = {"provider_request_id", "estimated_cost", "pricing_version", "currency", "prompt",
                      "response_text", "summary_text", "proposal_text", "metadata", "error_body"}
        self.assertFalse(names & prohibited)


@override_settings(OPENAI_API_KEY=FAKE_KEY)
class TransactionTelemetryTests(TransactionTestCase):
    def test_sdk_invocation_and_metadata_recording_are_outside_admission_transaction(self):
        user = get_user_model().objects.create_user(username="telemetry-transaction")
        with patch("proposal_ai.ai_control.now", return_value=MOMENT):
            row = control.admit(user, O.PROFILE_SUMMARY, control.issue_nonce(user, O.PROFILE_SUMMARY), {}, "context").request
            def provider():
                self.assertFalse(connection.in_atomic_block)
                return services.AIServiceResult("Summary", measured())
            control.call_provider(row, provider)
            self.assertFalse(connection.in_atomic_block)
            row.refresh_from_db()
            self.assertEqual((row.lifecycle, row.total_tokens), (L.IN_FLIGHT, 34))
            control.succeed(row)

    def test_preexisting_rows_remain_valid_with_null_telemetry(self):
        old = [("proposal_ai", "0014_ai_request")]
        new = [("proposal_ai", "0015_ai_request_telemetry")]
        executor = MigrationExecutor(connection)
        final_targets = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(old)
            apps = executor.loader.project_state(old).apps
            user = apps.get_model("auth", "User").objects.create(username="historical-telemetry")
            row = apps.get_model("proposal_ai", "AIRequest").objects.create(user_id=user.pk,
                operation=O.PROFILE_SUMMARY, nonce=uuid.uuid4(), intent="generate",
                submitted_fingerprint="a" * 64, effective_fingerprint="b" * 64,
                lifecycle=L.SUCCEEDED, quota_state=Q.CONSUMED, quota_units=1,
                admitted_at=MOMENT, lease_expires_at=MOMENT)
            executor = MigrationExecutor(connection)
            executor.migrate(new)
            current = executor.loader.project_state(new).apps.get_model("proposal_ai", "AIRequest").objects.get(pk=row.pk)
            self.assertTrue(all(getattr(current, name) is None for name in TELEMETRY_FIELDS))
            self.assertEqual((current.lifecycle, current.quota_state, current.quota_units), (L.SUCCEEDED, Q.CONSUMED, 1))
        finally:
            MigrationExecutor(connection).migrate(final_targets)
