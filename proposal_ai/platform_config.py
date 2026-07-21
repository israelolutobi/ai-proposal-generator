"""
Platform-specific terminology and behaviour for ProposalIQ.

ProposalIQ uses a platform-neutral internal concept:

    Opportunity / Job
        ↓
    Application
        ↓
    Platform-specific written content
        ↓
    Submission
        ↓
    Outcome

Different freelance platforms use different terminology for the written
part of an application. This module centralises those differences so we
do not scatter platform-specific if-statements throughout views,
templates, and AI prompts.

Examples:

    Upwork
        Overall application: Proposal
        Written content: Cover Letter

    Freelancer
        Overall application: Bid
        Written content: Proposal

    Direct client outreach
        Overall application: Pitch
        Written content: Pitch Message

Unknown platforms automatically fall back to generic terminology.
"""


# ---------------------------------------------------------------------
# DEFAULT / GENERIC PLATFORM CONFIGURATION
# ---------------------------------------------------------------------
#
# This configuration is used whenever ProposalIQ does not recognise the
# platform name.
#
# This is deliberately neutral so ProposalIQ can still work with new
# marketplaces without requiring an immediate code change.
# ---------------------------------------------------------------------

DEFAULT_PLATFORM_CONFIG = {
    "key": "other",

    # Name used for the overall application record.
    "application_label": "Application",

    # Name used for the AI-generated written component.
    "content_label": "Application Message",

    # Stored in Proposal.content_type.
    "content_type": "application_message",

    # User-interface wording.
    "generated_heading": "Your Application Message",
    "copy_button_label": "Copy Application Message",
    "submit_button_label": "Mark as Submitted",

    # Used after submission.
    "submitted_label": "Submitted",

    # Instruction supplied to the AI so it understands exactly
    # what kind of text it should generate.
    "ai_content_instruction": (
        "Write the main written application message for this freelance "
        "opportunity. Generate only the written message that the freelancer "
        "would submit to the client. Do not automatically include pricing, "
        "screening-question answers, attachments, or other application "
        "components unless they are explicitly part of the requested text."
    ),
}


# ---------------------------------------------------------------------
# PLATFORM CONFIGURATIONS
# ---------------------------------------------------------------------

PLATFORM_CONFIGS = {

    # -----------------------------------------------------------------
    # UPWORK
    # -----------------------------------------------------------------

    "upwork": {
        "key": "upwork",

        # Broad application object.
        "application_label": "Proposal",

        # Main generated written component.
        "content_label": "Cover Letter",

        # Stored in Proposal.content_type.
        "content_type": "cover_letter",

        # UI wording.
        "generated_heading": "Your Cover Letter",
        "copy_button_label": "Copy Cover Letter",
        "submit_button_label": "Mark Proposal as Submitted",

        "submitted_label": "Submitted",

        # AI-specific instruction.
        "ai_content_instruction": (
            "Write the cover-letter portion of this Upwork proposal. "
            "Generate only the personalised written cover letter that the "
            "freelancer can place into the relevant application text field. "
            "Do not include bid amounts, hourly rates, milestones, screening "
            "question answers, attachments, or other proposal components "
            "unless the job information explicitly requires them inside the "
            "cover letter itself."
        ),
    },


    # -----------------------------------------------------------------
    # FREELANCER / FREELANCER.COM
    # -----------------------------------------------------------------

    "freelancer": {
        "key": "freelancer",

        "application_label": "Bid",

        "content_label": "Proposal",

        "content_type": "proposal_text",

        "generated_heading": "Your Proposal",
        "copy_button_label": "Copy Proposal",
        "submit_button_label": "Mark Bid as Submitted",

        "submitted_label": "Submitted",

        "ai_content_instruction": (
            "Write the main proposal text for this freelance bid. "
            "Generate only the written pitch intended for the client. "
            "Do not automatically include separate pricing fields, "
            "milestones, attachments, or other bid components unless "
            "they genuinely belong inside the written proposal."
        ),
    },


    # -----------------------------------------------------------------
    # PEOPLEPERHOUR
    # -----------------------------------------------------------------

    "peopleperhour": {
        "key": "peopleperhour",

        "application_label": "Proposal",

        "content_label": "Proposal Message",

        "content_type": "proposal_text",

        "generated_heading": "Your Proposal Message",
        "copy_button_label": "Copy Proposal Message",
        "submit_button_label": "Mark Proposal as Submitted",

        "submitted_label": "Submitted",

        "ai_content_instruction": (
            "Write the main personalised proposal message for this "
            "freelance opportunity. Generate only the written message "
            "intended for the client. Do not automatically include separate "
            "pricing fields, delivery fields, attachments, or other "
            "application components unless they belong naturally in the "
            "written proposal."
        ),
    },


    # -----------------------------------------------------------------
    # FIVERR
    # -----------------------------------------------------------------
    #
    # Fiverr workflows can vary depending on how the freelancer and
    # client interact. We therefore use deliberately flexible wording
    # rather than pretending every Fiverr interaction follows the same
    # application structure.
    # -----------------------------------------------------------------

    "fiverr": {
        "key": "fiverr",

        "application_label": "Offer",

        "content_label": "Client Message",

        "content_type": "application_message",

        "generated_heading": "Your Client Message",
        "copy_button_label": "Copy Client Message",
        "submit_button_label": "Mark as Sent",

        "submitted_label": "Sent",

        "ai_content_instruction": (
            "Write the main personalised message or pitch for this Fiverr "
            "client opportunity. Generate only the written client-facing "
            "message. Do not automatically invent pricing, package details, "
            "delivery times, milestones, or other offer terms that were not "
            "provided by the freelancer or job information."
        ),
    },


    # -----------------------------------------------------------------
    # DIRECT CLIENT / OUTREACH
    # -----------------------------------------------------------------

    "direct": {
        "key": "direct",

        "application_label": "Pitch",

        "content_label": "Pitch Message",

        "content_type": "pitch",

        "generated_heading": "Your Pitch",
        "copy_button_label": "Copy Pitch",
        "submit_button_label": "Mark as Sent",

        "submitted_label": "Sent",

        "ai_content_instruction": (
            "Write a personalised freelance pitch for a direct client. "
            "Generate only the main written pitch or introductory message. "
            "Do not invent pricing, contracts, delivery commitments, "
            "portfolio results, or other commercial terms that were not "
            "supplied."
        ),
    },
}


# ---------------------------------------------------------------------
# PLATFORM ALIASES
# ---------------------------------------------------------------------
#
# The AI extraction process or a user may enter the same platform in
# slightly different ways.
#
# Examples:
#
#     "Upwork"
#     "upwork.com"
#     "UPWORK"
#
# All should resolve to:
#
#     "upwork"
#
# This prevents platform recognition from depending on exact spelling.
# ---------------------------------------------------------------------

PLATFORM_ALIASES = {
    # Upwork
    "upwork": "upwork",
    "upwork.com": "upwork",

    # Freelancer
    "freelancer": "freelancer",
    "freelancer.com": "freelancer",

    # PeoplePerHour
    "peopleperhour": "peopleperhour",
    "people per hour": "peopleperhour",
    "peopleperhour.com": "peopleperhour",

    # Fiverr
    "fiverr": "fiverr",
    "fiverr.com": "fiverr",

    # Direct / off-platform work
    "direct": "direct",
    "direct client": "direct",
    "direct client outreach": "direct",
    "direct outreach": "direct",
    "email": "direct",
    "email outreach": "direct",
    "linkedin": "direct",
    "linkedin outreach": "direct",
}


# ---------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------

def normalize_platform_name(platform):
    """
    Convert a platform name into a consistent internal key.

    Examples:

        "Upwork"            -> "upwork"
        "UPWORK.COM"        -> "upwork"
        "People Per Hour"   -> "peopleperhour"
        "Direct Client"     -> "direct"
        "UnknownSite"       -> "unknownsite"

    Returning a normalised value separately from configuration lookup
    keeps this function useful for future platform detection logic.
    """

    if not platform:
        return "other"

    normalized = str(platform).strip().lower()

    # Remove a trailing slash if an extracted value looks URL-like.
    normalized = normalized.rstrip("/")

    # Remove common URL prefixes.
    for prefix in (
        "https://www.",
        "http://www.",
        "https://",
        "http://",
        "www.",
    ):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]

    # Remove any path after the domain/platform name.
    if "/" in normalized:
        normalized = normalized.split("/", 1)[0]

    return PLATFORM_ALIASES.get(
        normalized,
        normalized,
    )


def get_platform_config(platform):
    """
    Return ProposalIQ's configuration for a platform.

    Unknown platforms automatically receive a copy of the generic
    configuration.

    Example:

        config = get_platform_config("Upwork")

        config["application_label"]
        -> "Proposal"

        config["content_label"]
        -> "Cover Letter"

        config["content_type"]
        -> "cover_letter"

    Returning a copy prevents callers from accidentally modifying the
    global configuration dictionary.
    """

    platform_key = normalize_platform_name(platform)

    config = PLATFORM_CONFIGS.get(platform_key)

    if config:
        return config.copy()

    fallback = DEFAULT_PLATFORM_CONFIG.copy()

    # Preserve the unknown platform key for future debugging, analytics,
    # or UI decisions while still using safe generic terminology.
    fallback["key"] = platform_key or "other"

    return fallback


def get_content_type_for_platform(platform):
    """
    Convenience helper for determining what should be saved in
    Proposal.content_type.

    Examples:

        Upwork
            -> cover_letter

        Freelancer
            -> proposal_text

        Direct client
            -> pitch

        Unknown platform
            -> application_message
    """

    config = get_platform_config(platform)

    return config["content_type"]


def get_content_label_for_platform(platform):
    """
    Return the user-facing name for the generated written content.

    Examples:

        Upwork
            -> Cover Letter

        Freelancer
            -> Proposal

        Direct
            -> Pitch Message
    """

    config = get_platform_config(platform)

    return config["content_label"]


def get_application_label_for_platform(platform):
    """
    Return the user-facing name for the broader application.

    Examples:

        Upwork
            -> Proposal

        Freelancer
            -> Bid

        Direct
            -> Pitch
    """

    config = get_platform_config(platform)

    return config["application_label"]