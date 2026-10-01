"""The OpenAI boundary: validated values and scalar telemetry, never ORM writes."""
from dataclasses import dataclass, field, fields, replace
import logging
import os
import re
import time
from json import JSONDecodeError

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from openai import (
    APIConnectionError, APIError, APIResponseValidationError, APIStatusError,
    APITimeoutError, AuthenticationError, BadRequestError, InternalServerError,
    NotFoundError, OpenAI, OpenAIError, PermissionDeniedError, RateLimitError,
    UnprocessableEntityError,
)

from . import ai_limits


CHAT_MODEL = "gpt-5"
LEGACY_PROFILE_MODEL = "gpt-5-mini"
REQUEST_TIMEOUT = httpx.Timeout(45.0, connect=5.0)
# Fail once and let the user retry. Automatic repeats are deferred until the
# separate idempotency/cost work; there is no application retry loop.
MAX_RETRIES = 0


def _count(value):
    # Keep values compatible with nullable signed-64-bit database counters.
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _model_identifier(value):
    if (type(value) is str and 0 < len(value) <= 200
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value)):
        return value
    return None


@dataclass(frozen=True, slots=True)
class AITelemetry:
    """Allowlisted immutable metadata. Missing/malformed optional values stay unknown."""
    provider: str | None = None
    api_style: str | None = None
    requested_model: str | None = None
    response_model: str | None = None
    input_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    total_tokens: int | None = None
    provider_latency_ms: int | None = None
    service_tier: str | None = None
    completion_token_cap: int | None = None
    finish_reason: str | None = None
    response_text_characters: int | None = None

    def __post_init__(self):
        choices = {
            "provider": ("openai",),
            "api_style": ("chat_completions",),
            "service_tier": ("auto", "default", "flex", "scale", "priority"),
            "finish_reason": ("stop", "length", "tool_calls", "content_filter", "function_call"),
        }
        for item in fields(AITelemetry):
            value = getattr(self, item.name)
            if item.name in choices:
                value = value if type(value) is str and value in choices[item.name] else None
            elif item.name in ("requested_model", "response_model"):
                value = _model_identifier(value)
            else:
                value = _count(value)
            object.__setattr__(self, item.name, value)

    def scalar_fields(self):
        # Explicit dataclass fields only: never serialize an SDK object or extras.
        return {item.name: getattr(self, item.name) for item in fields(AITelemetry)}


@dataclass(frozen=True, slots=True)
class AIServiceResult:
    value: str = field(repr=False)
    telemetry: AITelemetry

    def __post_init__(self):
        if type(self.value) is not str or type(self.telemetry) is not AITelemetry:
            raise TypeError("Invalid AI service result.")


def request_telemetry(operation):
    """Public non-secret configuration snapshot, without client construction."""
    budget = {"profile_summary": "summary", "job_extraction": "extraction",
              "proposal_generation": "proposal"}[operation]
    return AITelemetry(provider="openai", api_style="chat_completions",
                       requested_model=CHAT_MODEL,
                       completion_token_cap=ai_limits.COMPLETION_TOKENS[budget])


class AIError(Exception):
    category = "provider"
    user_message = "ProposalQ couldn't complete the AI request right now. Please try again."

    def __init__(self, telemetry=None):
        # Safe exception text contains no provider messages or payloads.
        super().__init__(self.user_message)
        self.telemetry = telemetry if type(telemetry) is AITelemetry else None


class AITemporaryError(AIError):
    category = "temporary"


class AIConnectionError(AITemporaryError):
    category = "connection"


class AITimeoutError(AIConnectionError):
    category = "timeout"


class AICapacityError(AITemporaryError):
    category = "capacity"


class AIConfigurationError(AIError):
    category = "configuration"
    user_message = "AI generation is temporarily unavailable."


class AIAuthenticationError(AIConfigurationError):
    category = "authentication"


class AIRequestError(AIConfigurationError):
    category = "invalid_request"


class AIResponseError(AIError):
    category = "invalid_response"


class AIIncompleteResponseError(AIResponseError):
    category = "incomplete_response"


class AIOversizedResponseError(AIResponseError):
    category = "oversized_response"


class AIInputError(AIError):
    category = "input_limit"
    user_message = "This AI request is too large or contains invalid text. Shorten the supplied context or select fewer experiences."


class _DropProviderLogs(logging.Filter):
    def filter(self, record):
        return False


# Installed SDK debug output includes request options and response parse details.
# Suppress those payload-bearing loggers even if OPENAI_LOG=debug is enabled.
# Application-level categories above are available for later safe observability.
for _name in ("openai._base_client", "openai._response"):
    logging.getLogger(_name).addFilter(_DropProviderLogs())


def _api_key():
    key = getattr(settings, "OPENAI_API_KEY", None) or os.getenv("OPENAI_API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise AIConfigurationError()
    return key


def check_configuration():
    """Local preflight only; never constructs a client or performs network I/O."""
    _api_key()


def _create_client():
    key = _api_key()
    try:
        return OpenAI(api_key=key, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES)
    except (TypeError, ValueError):
        raise AIConfigurationError() from None


def _request(operation, telemetry=None):
    try:
        with _create_client() as client:
            if telemetry is None:  # Preserve the inactive legacy Responses helper.
                return operation(client)
            started = time.perf_counter_ns()
            try:
                response = operation(client)
            finally:
                elapsed = max(0, (time.perf_counter_ns() - started) // 1_000_000)
                telemetry = replace(telemetry, provider_latency_ms=elapsed)
            return response, telemetry
    except AIError as error:
        # Local configuration/client construction failed before SDK invocation.
        if error.telemetry is None:
            error.telemetry = telemetry
        raise
    except (APITimeoutError, TimeoutError):
        raise AITimeoutError(telemetry) from None
    except APIConnectionError:
        raise AIConnectionError(telemetry) from None
    except RateLimitError:
        raise AICapacityError(telemetry) from None
    except AuthenticationError:
        raise AIAuthenticationError(telemetry) from None
    except PermissionDeniedError:
        raise AIConfigurationError(telemetry) from None
    except (BadRequestError, NotFoundError, UnprocessableEntityError):
        raise AIRequestError(telemetry) from None
    except (APIResponseValidationError, JSONDecodeError, UnicodeDecodeError):
        raise AIResponseError(telemetry) from None
    except InternalServerError:
        raise AITemporaryError(telemetry) from None
    except APIStatusError as error:
        if error.status_code == 408:
            raise AITimeoutError(telemetry) from None
        if error.status_code == 409 or error.status_code >= 500:
            raise AITemporaryError(telemetry) from None
        raise AIRequestError(telemetry) from None
    except (APIError, OpenAIError):
        raise AITemporaryError(telemetry) from None


def _validated_text(content, maximum=None, telemetry=None):
    if not isinstance(content, str) or not content.strip():
        raise AIResponseError(telemetry)
    if maximum is not None and len(content.strip()) > maximum:
        raise AIOversizedResponseError(telemetry)
    return content.strip()


def _optional_attribute(value, name):
    try:
        return getattr(value, name, None)
    except (AttributeError, TypeError, ValueError):
        return None


def _first_choice(response):
    choices = _optional_attribute(response, "choices")
    return choices[0] if isinstance(choices, (list, tuple)) and choices else None


def _response_telemetry(response, telemetry):
    usage = _optional_attribute(response, "usage")
    completion = _optional_attribute(usage, "completion_tokens_details")
    prompt = _optional_attribute(usage, "prompt_tokens_details")
    choice = _first_choice(response)
    content = _optional_attribute(_optional_attribute(choice, "message"), "content")
    return replace(
        telemetry, response_model=_optional_attribute(response, "model"),
        input_tokens=_optional_attribute(usage, "prompt_tokens"),
        completion_tokens=_optional_attribute(usage, "completion_tokens"),
        total_tokens=_optional_attribute(usage, "total_tokens"),
        reasoning_tokens=_optional_attribute(completion, "reasoning_tokens"),
        cached_input_tokens=_optional_attribute(prompt, "cached_tokens"),
        service_tier=_optional_attribute(response, "service_tier"),
        finish_reason=_optional_attribute(choice, "finish_reason"),
        response_text_characters=len(content) if type(content) is str else None,
    )


def _chat_text(messages, operation, output_maximum):
    telemetry = AITelemetry(provider="openai", api_style="chat_completions",
                            requested_model=CHAT_MODEL,
                            completion_token_cap=ai_limits.COMPLETION_TOKENS[operation])
    try:
        ai_limits.check_request(messages, operation)
    except ValidationError:
        raise AIInputError(telemetry) from None
    response, telemetry = _request(lambda client: client.chat.completions.create(
        model=CHAT_MODEL, messages=messages,
        max_completion_tokens=ai_limits.COMPLETION_TOKENS[operation],
    ), telemetry)
    # Capture typed scalar evidence before rejecting incomplete/invalid content.
    telemetry = _response_telemetry(response, telemetry)
    try:
        choice = _first_choice(response)
        if choice.finish_reason != "stop":
            if telemetry.finish_reason is not None:
                raise AIIncompleteResponseError(telemetry)
            raise AIResponseError(telemetry)
        content = choice.message.content
    except (AttributeError, IndexError, TypeError):
        raise AIResponseError(telemetry) from None
    # Bound raw extraction JSON before trimming/parsing, including huge padding.
    if operation == "extraction" and isinstance(content, str) and len(content) > output_maximum:
        raise AIOversizedResponseError(telemetry)
    return AIServiceResult(_validated_text(content, output_maximum, telemetry), telemetry)


def extract_job_details(prompt):
    # Task 2C's JobExtractionForm remains the schema validation layer.
    return _chat_text([{"role": "user", "content": prompt}], "extraction", ai_limits.EXTRACTION_RESPONSE_CHARACTERS)


def generate_profile_summary(prompt):
    return _chat_text([{"role": "user", "content": prompt}], "summary", ai_limits.SUMMARY_OUTPUT_CHARACTERS)


def generate_proposal(writing_instructions, application_context):
    return _chat_text([
        {"role": "system", "content": writing_instructions},
        {"role": "user", "content": application_context},
    ], "proposal", ai_limits.PROPOSAL_OUTPUT_CHARACTERS)


def generate_freelancer_profile_summary(
    professional_title: str,
    key_skills: str,
) -> str:
    """
    Generate a short freelancer profile summary from simple factual inputs.

    The function must not invent experience, qualifications, years of
    experience, client results, or technologies that the user did not provide.
    """

    professional_title = professional_title.strip()
    key_skills = key_skills.strip()

    if not professional_title:
        raise ValueError("Professional title is required.")

    if not key_skills:
        raise ValueError("Please enter a few key skills or services.")

    response = _request(lambda client: client.responses.create(
        model=LEGACY_PROFILE_MODEL,
        reasoning={"effort": "low"},
        instructions="""
You write concise professional freelancer profile summaries.

Create a natural-sounding profile summary using ONLY the facts supplied
by the user.

Rules:
- Write approximately 50 to 80 words.
- Use clear, natural professional English.
- Do not invent years of experience.
- Do not invent employers, clients, qualifications, results, statistics,
  certifications, technologies, or achievements.
- Do not claim the freelancer is an expert unless explicitly stated.
- Do not use em dashes.
- Avoid exaggerated marketing language.
- Avoid generic phrases such as "passionate professional" unless genuinely
  useful.
- Focus on what the freelancer does, their core skills, and the type of
  value they can provide.
- Return only the profile summary. Do not add headings or commentary.
""",
        input=f"""
Professional title:
{professional_title}

Key skills or services:
{key_skills}
""",
    ))

    try:
        content = response.output_text
    except AttributeError:
        raise AIResponseError() from None
    return _validated_text(content)
