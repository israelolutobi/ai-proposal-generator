import io
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from types import SimpleNamespace
import traceback
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape
import httpx
import openai

from proposal_ai import services
from proposal_ai.models import FreelancerProfile, JobPost, Proposal
from mysite.test_runner import ExternalNetworkBlocked, NoNetworkDiscoverRunner, block_external_network


FAKE_KEY = "test-only-not-a-credential"
PRIVATE_DETAIL = "PRIVATE_PROVIDER_DETAIL_FIXTURE"


def chat_response(content=" Generated text. "):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@override_settings(OPENAI_API_KEY=FAKE_KEY)
class AIServiceTests(SimpleTestCase):
    def setUp(self):
        self.sdk_patch = patch("proposal_ai.services.OpenAI", autospec=True)
        self.constructor = self.sdk_patch.start()
        self.addCleanup(self.sdk_patch.stop)
        self.client = MagicMock()
        self.constructor.return_value = self.client
        self.client.__enter__.return_value = self.client
        self.client.chat.completions.create.return_value = chat_response()
        self.client.responses.create.return_value = SimpleNamespace(output_text=" Legacy summary. ")

    def test_environment_key_is_used_without_logging_its_value(self):
        with override_settings(OPENAI_API_KEY=None), patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY}):
            with self.assertNoLogs("proposal_ai.services"):
                self.assertEqual(services.generate_profile_summary("Private prompt"), "Generated text.")
        self.assertEqual(self.constructor.call_args.kwargs["api_key"], FAKE_KEY)

    def test_settings_key_takes_precedence_over_environment(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "other-test-placeholder"}):
            services.generate_profile_summary("Prompt")
        self.assertEqual(self.constructor.call_args.kwargs["api_key"], FAKE_KEY)

    def test_missing_blank_whitespace_or_invalid_key_is_controlled_before_client_creation(self):
        for value in (None, "", " \t ", 42):
            with self.subTest(value_type=type(value).__name__):
                with override_settings(OPENAI_API_KEY=value), patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                    with self.assertRaises(services.AIConfigurationError) as caught:
                        services.generate_profile_summary("Prompt")
                    self.assertEqual(str(caught.exception), "AI generation is temporarily unavailable.")
                    self.assertNotIn("OPENAI_API_KEY", str(caught.exception))
        self.constructor.assert_not_called()

    def test_timeout_and_zero_retries_are_explicit_for_every_entry_point(self):
        operations = (
            lambda: services.extract_job_details("Extract"),
            lambda: services.generate_profile_summary("Summary"),
            lambda: services.generate_proposal("Instructions", "Context"),
            lambda: services.generate_freelancer_profile_summary("Developer", "Django"),
        )
        for operation in operations:
            self.constructor.reset_mock()
            operation()
            self.constructor.assert_called_once()
            options = self.constructor.call_args.kwargs
            self.assertEqual(options["max_retries"], 0)
            self.assertEqual(options["timeout"].connect, 5.0)
            for phase in ("read", "write", "pool"):
                self.assertEqual(getattr(options["timeout"], phase), 45.0)

    def test_extraction_keeps_chat_endpoint_model_and_exact_input(self):
        result = services.extract_job_details("Exact extraction prompt")
        self.assertEqual(result, "Generated text.")
        self.client.chat.completions.create.assert_called_once_with(
            model="gpt-5", messages=[{"role": "user", "content": "Exact extraction prompt"}],
        )
        self.client.responses.create.assert_not_called()

    def test_active_summary_keeps_chat_endpoint_model_and_exact_input(self):
        self.assertEqual(services.generate_profile_summary("Exact summary prompt"), "Generated text.")
        self.client.chat.completions.create.assert_called_once_with(
            model="gpt-5", messages=[{"role": "user", "content": "Exact summary prompt"}],
        )

    def test_proposal_keeps_system_and_user_messages_in_order(self):
        self.assertEqual(services.generate_proposal("Exact instructions", "Exact context"), "Generated text.")
        self.client.chat.completions.create.assert_called_once_with(model="gpt-5", messages=[
            {"role": "system", "content": "Exact instructions"},
            {"role": "user", "content": "Exact context"},
        ])

    def test_legacy_helper_keeps_responses_endpoint_model_and_reasoning(self):
        self.assertEqual(services.generate_freelancer_profile_summary(" Developer ", " Django "), "Legacy summary.")
        self.client.responses.create.assert_called_once()
        options = self.client.responses.create.call_args.kwargs
        self.assertEqual(options["model"], "gpt-5-mini")
        self.assertEqual(options["reasoning"], {"effort": "low"})
        self.assertIn("Professional title:\nDeveloper", options["input"])
        self.assertIn("Key skills or services:\nDjango", options["input"])
        self.client.chat.completions.create.assert_not_called()

    def test_legacy_empty_input_still_rejected_without_provider_call(self):
        for title, skills in ((" ", "Django"), ("Developer", " ")):
            with self.assertRaises(ValueError):
                services.generate_freelancer_profile_summary(title, skills)
        self.constructor.assert_not_called()

    def mapped_error(self, error, expected_type, category):
        self.client.chat.completions.create.side_effect = error
        self.client.reset_mock()
        with self.assertRaises(expected_type) as caught:
            services.generate_profile_summary("PRIVATE_PROMPT_FIXTURE")
        failure = caught.exception
        self.assertEqual(failure.category, category)
        self.assertNotIn(PRIVATE_DETAIL, str(failure))
        self.assertNotIn(PRIVATE_DETAIL, "".join(traceback.format_exception(failure)))
        self.assertTrue(failure.__suppress_context__)
        self.assertIsNone(failure.__cause__)
        self.client.chat.completions.create.assert_called_once()
        self.client.__exit__.assert_called_once()

    def status_error(self, cls, status):
        request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
        response = httpx.Response(status, request=request)
        return cls(PRIVATE_DETAIL, response=response, body={"private": PRIVATE_DETAIL})

    def test_timeout_maps_to_timeout_failure(self):
        request = httpx.Request("POST", "https://example.invalid")
        self.mapped_error(openai.APITimeoutError(request), services.AITimeoutError, "timeout")

    def test_connection_maps_to_connection_failure(self):
        request = httpx.Request("POST", "https://example.invalid")
        self.mapped_error(openai.APIConnectionError(message=PRIVATE_DETAIL, request=request), services.AIConnectionError, "connection")

    def test_rate_limit_maps_to_capacity_failure(self):
        self.mapped_error(self.status_error(openai.RateLimitError, 429), services.AICapacityError, "capacity")

    def test_authentication_maps_to_configuration_failure(self):
        self.mapped_error(self.status_error(openai.AuthenticationError, 401), services.AIConfigurationError, "configuration")

    def test_permission_maps_to_configuration_failure(self):
        self.mapped_error(self.status_error(openai.PermissionDeniedError, 403), services.AIConfigurationError, "configuration")

    def test_bad_request_maps_to_request_failure(self):
        self.mapped_error(self.status_error(openai.BadRequestError, 400), services.AIRequestError, "invalid_request")

    def test_invalid_model_or_unprocessable_request_maps_to_request_failure(self):
        for cls, status in ((openai.NotFoundError, 404), (openai.UnprocessableEntityError, 422)):
            self.mapped_error(self.status_error(cls, status), services.AIRequestError, "invalid_request")

    def test_server_failure_maps_to_temporary_failure(self):
        self.mapped_error(self.status_error(openai.InternalServerError, 500), services.AITemporaryError, "temporary")

    def test_generic_api_error_maps_to_temporary_failure(self):
        request = httpx.Request("POST", "https://example.invalid")
        self.mapped_error(openai.APIError(PRIVATE_DETAIL, request, body={"private": PRIVATE_DETAIL}), services.AITemporaryError, "temporary")

    def test_generic_openai_error_maps_to_temporary_failure(self):
        self.mapped_error(openai.OpenAIError(PRIVATE_DETAIL), services.AITemporaryError, "temporary")

    def test_generic_status_errors_distinguish_transient_and_invalid_requests(self):
        for status, cls, category in ((408, services.AITimeoutError, "timeout"), (409, services.AITemporaryError, "temporary"), (503, services.AITemporaryError, "temporary"), (418, services.AIRequestError, "invalid_request")):
            self.mapped_error(self.status_error(openai.APIStatusError, status), cls, category)

    def test_response_validation_error_maps_to_invalid_response(self):
        response = httpx.Response(200, request=httpx.Request("POST", "https://example.invalid"))
        self.mapped_error(openai.APIResponseValidationError(response, {"private": PRIVATE_DETAIL}, message=PRIVATE_DETAIL), services.AIResponseError, "invalid_response")

    def test_response_structure_and_content_are_validated(self):
        bad_responses = (None, SimpleNamespace(), SimpleNamespace(choices=[]), SimpleNamespace(choices=[None]))
        bad_responses += tuple(chat_response(value) for value in (None, "", " \n ", [], {}, 42, True))
        for response in bad_responses:
            with self.subTest(response_type=type(response).__name__):
                self.client.chat.completions.create.return_value = response
                with self.assertRaises(services.AIResponseError):
                    services.extract_job_details("Prompt")

    def test_empty_generation_and_summary_are_rejected(self):
        self.client.chat.completions.create.return_value = chat_response("")
        for call in (lambda: services.generate_proposal("Instructions", "Context"), lambda: services.generate_profile_summary("Prompt")):
            with self.assertRaises(services.AIResponseError):
                call()

    def test_legacy_response_content_is_validated(self):
        for response in (None, SimpleNamespace(), SimpleNamespace(output_text=""), SimpleNamespace(output_text=[])):
            self.client.responses.create.return_value = response
            with self.assertRaises(services.AIResponseError):
                services.generate_freelancer_profile_summary("Developer", "Django")

    def test_legacy_helper_uses_the_same_error_mapping(self):
        self.client.responses.create.side_effect = self.status_error(openai.RateLimitError, 429)
        with self.assertRaises(services.AICapacityError):
            services.generate_freelancer_profile_summary("Developer", "Django")

    def test_client_is_closed_after_success(self):
        services.generate_profile_summary("Prompt")
        self.client.__exit__.assert_called_once_with(None, None, None)

    def test_invalid_client_configuration_is_safe(self):
        self.constructor.side_effect = ValueError(PRIVATE_DETAIL)
        with self.assertRaises(services.AIConfigurationError) as caught:
            services.generate_profile_summary("Prompt")
        self.assertNotIn(PRIVATE_DETAIL, str(caught.exception))

    def test_programming_errors_are_not_silently_classified_as_provider_errors(self):
        self.client.chat.completions.create.side_effect = AssertionError("Mock programming error")
        with self.assertRaises(AssertionError):
            services.generate_profile_summary("Prompt")

    def test_sdk_payload_debug_logging_is_suppressed(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        for name in ("openai._base_client", "openai._response"):
            logger = logging.getLogger(name)
            level = logger.level
            logger.setLevel(logging.DEBUG)
            logger.addHandler(handler)
            try:
                logger.debug("Request options: %s", {"prompt": PRIVATE_DETAIL, "key": FAKE_KEY})
            finally:
                logger.removeHandler(handler)
                logger.setLevel(level)
        self.assertEqual(stream.getvalue(), "")


@override_settings(OPENAI_API_KEY=FAKE_KEY)
class SDKPolicyTests(SimpleTestCase):
    def fake_transport(self, handler):
        real_constructor = openai.OpenAI
        def create(**options):
            return real_constructor(**options, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        return patch("proposal_ai.services.OpenAI", side_effect=create)

    def test_installed_sdk_makes_only_one_attempt_for_retryable_and_permanent_errors(self):
        for status, failure in ((400, services.AIRequestError), (401, services.AIConfigurationError), (429, services.AICapacityError), (500, services.AITemporaryError), (503, services.AITemporaryError)):
            attempts = []
            def handler(request):
                attempts.append(request)
                return httpx.Response(status, json={"error": {"message": PRIVATE_DETAIL}}, headers={"x-should-retry": "true", "retry-after": "60"})
            with self.subTest(status=status), self.fake_transport(handler):
                with self.assertRaises(failure):
                    services.generate_profile_summary("Prompt")
                self.assertEqual(len(attempts), 1)

    def test_installed_sdk_timeout_has_one_attempt_and_explicit_timeout_extensions(self):
        attempts = []
        def handler(request):
            attempts.append(request)
            self.assertEqual(request.extensions["timeout"], {"connect": 5.0, "read": 45.0, "write": 45.0, "pool": 45.0})
            raise httpx.ReadTimeout(PRIVATE_DETAIL, request=request)
        with self.fake_transport(handler):
            with self.assertRaises(services.AITimeoutError):
                services.generate_profile_summary("Prompt")
        self.assertEqual(len(attempts), 1)

    def test_installed_sdk_connection_error_has_one_attempt(self):
        attempts = []
        def handler(request):
            attempts.append(request)
            raise httpx.ConnectError(PRIVATE_DETAIL, request=request)
        with self.fake_transport(handler):
            with self.assertRaises(services.AIConnectionError):
                services.generate_profile_summary("Prompt")
        self.assertEqual(len(attempts), 1)

    def test_installed_sdk_success_returns_text_and_preserves_request_body(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body, {"model": "gpt-5", "messages": [{"role": "user", "content": "Exact prompt"}]})
            return httpx.Response(200, json={"id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "gpt-5", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": " Success "}}]})
        with self.fake_transport(handler):
            self.assertEqual(services.generate_profile_summary("Exact prompt"), "Success")

    def test_installed_sdk_malformed_response_body_is_a_safe_response_failure(self):
        for body in ("garbled json", "[]", '"text"', "null", '{"choices": []}'):
            def handler(request):
                return httpx.Response(200, text=body, headers={"content-type": "application/json"})
            with self.subTest(body=body), self.fake_transport(handler):
                with self.assertRaises(services.AIResponseError):
                    services.generate_profile_summary("Prompt")


@override_settings(
    ALLOWED_HOSTS=["testserver"], DEBUG=False,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class AICallerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="ai-owner")
        cls.profile = FreelancerProfile.objects.create(user=cls.owner, professional_title="Original developer", profile_summary="Original profile")
        cls.job = JobPost.objects.create(user=cls.owner, job_title="Original job", job_description="Original description", raw_job_text="Original raw input")

    def setUp(self):
        self.client.force_login(self.owner)
        self.calls = {}
        for name in ("extract_job_details", "generate_profile_summary", "generate_proposal"):
            mocked = patch("proposal_ai.views.services." + name)
            self.calls[name] = mocked.start()
            self.addCleanup(mocked.stop)

    def snapshot(self):
        return tuple(list(model.objects.order_by("pk").values()) for model in (FreelancerProfile, JobPost, Proposal))

    def failures(self):
        for cls in (services.AITimeoutError, services.AIConnectionError, services.AICapacityError, services.AIConfigurationError, services.AIRequestError, services.AITemporaryError, services.AIResponseError):
            error = cls()
            # Even if an internal caller supplies unsafe exception text, views
            # use the safe message property, never str(error).
            error.args = (PRIVATE_DETAIL,)
            yield error

    def raw_text(self):
        return "Looking for a Django developer on an hourly project.\n" + "Build tested application views. " * 20

    def confirm_url(self):
        return reverse("confirm_job_features", args=[self.job.pk])

    def job_data(self):
        return {"platform": "Upwork", "job_title": "Updated job", "job_description": "Updated description"}

    def test_extraction_success_through_boundary_saves_owned_unconfirmed_job(self):
        self.calls["extract_job_details"].return_value = json.dumps(self.job_data())
        response = self.client.post(reverse("extract_job_features"), {"raw_job_text": self.raw_text()})
        self.assertEqual(response.status_code, 302)
        job = JobPost.objects.exclude(pk=self.job.pk).get()
        self.assertEqual(job.user_id, self.owner.pk)
        self.assertFalse(job.confirmed_by_user)
        self.assertEqual(job.raw_job_text, self.raw_text().strip())
        self.calls["extract_job_details"].assert_called_once()

    def test_extraction_typed_failures_preserve_paste_and_write_nothing(self):
        for error in self.failures():
            with self.subTest(category=error.category):
                before = self.snapshot()
                self.calls["extract_job_details"].side_effect = error
                response = self.client.post(reverse("extract_job_features"), {"raw_job_text": self.raw_text()})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["form"]["raw_job_text"].value(), self.raw_text())
                self.assertNotContains(response, PRIVATE_DETAIL)
                self.assertIn("__all__", response.context["form"].errors)
                self.assertEqual(self.snapshot(), before)

    def test_malformed_extracted_json_still_creates_no_job(self):
        before = self.snapshot()
        self.calls["extract_job_details"].return_value = "malformed json"
        response = self.client.post(reverse("extract_job_features"), {"raw_job_text": self.raw_text()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["raw_job_text"].value(), self.raw_text())
        self.assertEqual(self.snapshot(), before)

    def test_summary_success_keeps_json_contract_and_never_saves_profile(self):
        before = self.snapshot()
        self.calls["generate_profile_summary"].return_value = "Generated — summary."
        response = self.client.post(reverse("generate_profile_summary"), {"professional_title": "Developer", "key_skills": "Django"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"summary": "Generated - summary."})
        self.assertEqual(self.snapshot(), before)

    def test_summary_provider_failures_keep_json_contract_and_profile_unchanged(self):
        for error in self.failures():
            with self.subTest(category=error.category):
                before = self.snapshot()
                self.calls["generate_profile_summary"].side_effect = error
                response = self.client.post(reverse("generate_profile_summary"), {"professional_title": "Developer", "key_skills": "Django"})
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json(), {"error": error.user_message})
                self.assertNotIn(PRIVATE_DETAIL, response.content.decode())
                self.assertEqual(self.snapshot(), before)

    def test_proposal_success_persists_confirmed_job_and_proposal(self):
        self.calls["generate_proposal"].return_value = "Generated — proposal."
        response = self.client.post(self.confirm_url(), self.job_data())
        self.assertRedirects(response, reverse("dashboard"))
        self.job.refresh_from_db()
        self.assertTrue(self.job.confirmed_by_user)
        self.assertEqual(self.job.job_title, "Updated job")
        self.assertEqual(Proposal.objects.get().final_text, "Generated - proposal.")
        self.assertEqual(Proposal.objects.get().job_post_id, self.job.pk)

    def test_proposal_provider_failures_keep_lifecycle_unchanged_and_bound_values_available(self):
        for error in self.failures():
            with self.subTest(category=error.category):
                before = self.snapshot()
                self.calls["generate_proposal"].side_effect = error
                response = self.client.post(self.confirm_url(), self.job_data())
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, escape(error.user_message))
                self.assertNotContains(response, PRIVATE_DETAIL)
                self.assertEqual(response.context["form"]["job_title"].value(), "Updated job")
                self.assertEqual(response.context["form"]["job_description"].value(), "Updated description")
                self.assertEqual(self.snapshot(), before)


class NetworkSafetyTests(SimpleTestCase):
    def test_default_runner_removes_credentials(self):
        self.assertEqual(settings.TEST_RUNNER, "mysite.test_runner.NoNetworkDiscoverRunner")
        self.assertFalse(os.environ.get("OPENAI_API_KEY"))
        self.assertFalse(settings.OPENAI_API_KEY)

    def test_dns_socket_and_datagram_attempts_are_blocked(self):
        with block_external_network():
            with self.assertRaises(ExternalNetworkBlocked):
                socket.getaddrinfo("example.invalid", 443)
            with self.assertRaises(ExternalNetworkBlocked):
                socket.create_connection(("example.invalid", 443))
            with socket.socket() as connection:
                with self.assertRaises(ExternalNetworkBlocked):
                    connection.connect(("127.0.0.1", 443))
                with self.assertRaises(ExternalNetworkBlocked):
                    connection.connect_ex(("127.0.0.1", 443))
            with socket.socket(type=socket.SOCK_DGRAM) as connection:
                with self.assertRaises(ExternalNetworkBlocked):
                    connection.sendto(b"test", ("127.0.0.1", 443))

    def test_unmocked_real_sdk_attempt_is_blocked_not_converted_to_safe_provider_failure(self):
        with override_settings(OPENAI_API_KEY=FAKE_KEY), block_external_network():
            with self.assertRaises(ExternalNetworkBlocked):
                services.generate_profile_summary("Test prompt")

    def test_missing_key_is_safe_without_constructing_or_contacting_provider(self):
        with override_settings(OPENAI_API_KEY=""), patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with patch("proposal_ai.services.OpenAI") as constructor:
                with self.assertRaises(services.AIConfigurationError):
                    services.generate_profile_summary("Prompt")
                constructor.assert_not_called()

    def test_parallel_mode_is_refused_before_unprotected_workers_start(self):
        runner = NoNetworkDiscoverRunner(parallel=2, verbosity=0)
        with patch.object(runner, "setup_databases") as databases:
            with self.assertRaisesRegex(RuntimeError, "requires serial execution"):
                runner.run_tests([])
            databases.assert_not_called()

    def test_ordinary_manage_test_returns_failure_for_accidental_unmocked_network(self):
        script = '''
import os, runpy, sys, types
from unittest.mock import patch
from django.test import SimpleTestCase
module = types.ModuleType("network_probe")
class Probe(SimpleTestCase):
    def test_accidental_network(self):
        import socket
        socket.create_connection(("example.invalid", 443))
module.Probe = Probe
sys.modules["network_probe"] = module
sys.argv = ["manage.py", "test", "network_probe.Probe", "--verbosity", "0"]
with patch("dotenv.load_dotenv", return_value=False):
    runpy.run_path("manage.py", run_name="__main__")
'''
        environment = os.environ.copy()
        environment.update(SECRET_KEY=secrets.token_urlsafe(48), OPENAI_API_KEY="", GEMINI_API_KEY="", DEBUG="False", DATABASE_URL="", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=Path(__file__).resolve().parent.parent, env=environment, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ExternalNetworkBlocked", result.stderr)
        self.assertIn("FAILED (errors=1)", result.stderr)
