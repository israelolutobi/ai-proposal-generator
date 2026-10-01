"""The single OpenAI boundary. Callers receive text or a safe typed failure."""
import logging
import os
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


class AIError(Exception):
    category = "provider"
    user_message = "ProposalQ couldn't complete the AI request right now. Please try again."

    def __init__(self):
        # Safe exception text contains no provider messages or payloads.
        super().__init__(self.user_message)


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


class AIRequestError(AIConfigurationError):
    category = "invalid_request"


class AIResponseError(AIError):
    category = "invalid_response"


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


def _request(operation):
    try:
        with _create_client() as client:
            return operation(client)
    except (APITimeoutError, TimeoutError):
        raise AITimeoutError() from None
    except APIConnectionError:
        raise AIConnectionError() from None
    except RateLimitError:
        raise AICapacityError() from None
    except (AuthenticationError, PermissionDeniedError):
        raise AIConfigurationError() from None
    except (BadRequestError, NotFoundError, UnprocessableEntityError):
        raise AIRequestError() from None
    except (APIResponseValidationError, JSONDecodeError, UnicodeDecodeError):
        raise AIResponseError() from None
    except InternalServerError:
        raise AITemporaryError() from None
    except APIStatusError as error:
        if error.status_code == 408:
            raise AITimeoutError() from None
        if error.status_code == 409 or error.status_code >= 500:
            raise AITemporaryError() from None
        raise AIRequestError() from None
    except (APIError, OpenAIError):
        raise AITemporaryError() from None


def _validated_text(content, maximum=None):
    if not isinstance(content, str) or not content.strip():
        raise AIResponseError()
    if maximum is not None and len(content.strip()) > maximum:
        raise AIResponseError()
    return content.strip()


def _chat_text(messages, operation, output_maximum):
    try:
        ai_limits.check_request(messages, operation)
    except ValidationError:
        raise AIInputError() from None
    response = _request(lambda client: client.chat.completions.create(
        model=CHAT_MODEL, messages=messages,
        max_completion_tokens=ai_limits.COMPLETION_TOKENS[operation],
    ))
    try:
        choice = response.choices[0]
        if choice.finish_reason != "stop":
            raise AIResponseError()
        content = choice.message.content
    except (AttributeError, IndexError, TypeError):
        raise AIResponseError() from None
    # Bound raw extraction JSON before trimming/parsing, including huge padding.
    if operation == "extraction" and isinstance(content, str) and len(content) > output_maximum:
        raise AIResponseError()
    return _validated_text(content, output_maximum)


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
