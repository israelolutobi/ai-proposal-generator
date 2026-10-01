"""Beta AI-use budgets. These do not reduce profile/experience storage limits."""
from django.core.exceptions import ValidationError


JOB_PASTE_CHARACTERS = 20_000
SUMMARY_TITLE_CHARACTERS = 255
SUMMARY_SKILLS_CHARACTERS = 2_000
PROFILE_FIELD_LIMITS = {
    "professional_title": ("Profile title", 255),
    "profile_summary": ("Stored profile summary", 5_000),
    "preferred_tone": ("Preferred tone", 100),
}
PROFILE_CONTEXT_CHARACTERS = 6_000
EXPERIENCE_FIELD_LIMITS = {
    "job_title": ("Experience role title", 255),
    "company_or_project": ("Experience company/project", 255),
    "tasks": ("Experience tasks", 4_000),
    "skills_used": ("Experience skills", 1_000),
    "experience_depth": ("Experience depth", 2_000),
}
EXPERIENCE_CHARACTERS = 8_000
EXPERIENCE_COUNT = 10
EXPERIENCE_CONTEXT_CHARACTERS = 20_000
JOB_DESCRIPTION_CHARACTERS = 20_000
JOB_SKILLS_CHARACTERS = 2_000
JOB_CONTEXT_CHARACTERS = 24_000
REQUEST_CHARACTERS = {"summary": 4_000, "extraction": 24_000, "proposal": 60_000}
REQUEST_UTF8_BYTES = 128_000
COMPLETION_TOKENS = {"summary": 2_048, "extraction": 8_192, "proposal": 6_144}
SUMMARY_OUTPUT_CHARACTERS = 2_000
PROPOSAL_OUTPUT_CHARACTERS = 8_000
# Known extraction fields: 23,310 string characters at their declared maxima
# (20k description + 2k skills + eight 100-char fields + two 255-char fields).
# An escaped non-BMP character uses 12 JSON characters. Thus 279,720 characters
# cover those strings; keys, quotes, budgets and ordinary pretty-printing need
# under 500 more. 281k leaves <1k extra formatting room. Arbitrarily padded JSON
# or ignored extra fields are deliberately bounded, not silently truncated.
EXTRACTION_RESPONSE_CHARACTERS = 281_000


def check_characters(text, maximum, label, *, stored=False, normalize=True):
    """Python code points after normal outer-whitespace normalization."""
    text = text or ""
    if normalize:
        text = text.strip()
    if len(text) > maximum:
        message = f"{label} uses {len(text):,} characters; the AI maximum is {maximum:,}."
        if stored:
            message += " Your stored record can remain; shorten the AI context or select other evidence."
        raise ValidationError(message, code="ai_max_length")
    return text


def check_fields(instance, limits):
    for name, (label, maximum) in limits.items():
        check_characters(getattr(instance, name), maximum, label, stored=True)


def experience_context(experiences):
    """Same prompt labels/order as before; callers supply owned, selected records."""
    experiences = list(experiences)
    if len(experiences) > EXPERIENCE_COUNT:
        raise ValidationError(f"Choose no more than {EXPERIENCE_COUNT} experiences.", code="ai_experience_count")
    blocks = []
    for index, experience in enumerate(experiences, start=1):
        try:
            check_fields(experience, EXPERIENCE_FIELD_LIMITS)
        except ValidationError as error:
            record = getattr(experience, "pk", None)
            prefix = f"Selected experience {index}" + (f" (record {record})" if record is not None else "")
            raise ValidationError([f"{prefix}: {message}" for message in error.messages], code="ai_max_length") from None
        values = {name: (getattr(experience, name) or "").strip() for name in EXPERIENCE_FIELD_LIMITS}
        block = (
            f"Experience {index}:\n"
            f"Role / Project Title: {values['job_title']}\n"
            f"Company / Project: {values['company_or_project']}\n"
            f"Relevant Tasks / Responsibilities: {values['tasks']}\n"
            f"Skills Used: {values['skills_used']}\n"
            f"Experience Depth: {values['experience_depth']}"
        )
        check_characters(block, EXPERIENCE_CHARACTERS, "Formatted experience", stored=True, normalize=False)
        blocks.append(block)
    context = "\n\n".join(blocks) if blocks else "No detailed work experiences were provided."
    check_characters(context, EXPERIENCE_CONTEXT_CHARACTERS, "Selected experience context", stored=True, normalize=False)
    return context


def check_request(messages, operation):
    # Count the exact constructed strings, including separators and instructions.
    texts = [message["content"] for message in messages]
    if not all(isinstance(text, str) for text in texts):
        raise ValidationError("AI input must be text.", code="ai_input_type")
    if sum(map(len, texts)) > REQUEST_CHARACTERS[operation]:
        raise ValidationError("The combined AI request is too large. Shorten the supplied context.", code="ai_request_length")
    try:
        size = sum(len(text.encode("utf-8")) for text in texts)
    except UnicodeEncodeError:
        raise ValidationError("AI input contains invalid Unicode text.", code="ai_input_unicode") from None
    if size > REQUEST_UTF8_BYTES:
        raise ValidationError("The encoded AI request is too large. Shorten the supplied context.", code="ai_request_bytes")
