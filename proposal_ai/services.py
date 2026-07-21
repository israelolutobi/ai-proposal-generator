from django.conf import settings
from openai import OpenAI


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

    api_key = getattr(settings, "OPENAI_API_KEY", None)

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    client = OpenAI(api_key=api_key)

    response = client.responses.create(
        model="gpt-5-mini",
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
    )

    summary = response.output_text.strip()

    if not summary:
        raise RuntimeError("The AI returned an empty profile summary.")

    return summary