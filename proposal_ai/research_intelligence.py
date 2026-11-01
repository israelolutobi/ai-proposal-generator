"""Indexed deterministic retrieval. Private research prose never enters prompts."""
from collections import Counter
from dataclasses import dataclass
import json
import re

from django.db.models import Count
from .models import ResearchCase, ResearchDataset, ResearchNote, ResearchRequirement

MAX_CASES = 8
MAX_CONTEXT_CHARACTERS = 4000
MAX_QUERY_TERMS = 64
ALGORITHM = "lexical-overlap-v1"
_TOKEN = re.compile(r"[a-z][a-z0-9+#]{1,63}", re.I)
_STOP = frozenset("the and for with that this from your you are job project work client using into our developer development required experience skills senior existing take over full system only strong core description title budget hourly fixed not new application professional need looking build complete design working to of in on as at be is a an it we us have has will can all or by should must about please support service services high low medium general years review implement understand make good great want software platform technology tools like similar within through more other some any provided confirmed freelancer details use no true false".split())


def tokens(value):
    return {word.lower() for word in _TOKEN.findall(value or "") if word.lower() not in _STOP}


def case_tokens(case):
    # Outcomes and participant/proposal quality never influence relevance.
    return tokens(" ".join((case.domain_niche, case.job_title, case.job_core_problem,
                            case.primary_stack_domain, case.required_skills)))


@dataclass(frozen=True, slots=True)
class ResearchContext:
    text: str
    dataset_id: int
    total_cases: int
    matched_cases: int
    case_keys: tuple[str, ...]


class ResearchUnavailable(Exception):
    user_message = "Research intelligence is temporarily unavailable. Please try again later."


def build_research_context(job_post):
    dataset = ResearchDataset.objects.filter(active=True).first()
    if dataset is None:
        raise ResearchUnavailable()
    primary = sorted(tokens(" ".join((job_post.job_title or "", job_post.skills_required or ""))))
    secondary = sorted(tokens(job_post.job_description or "") - set(primary))
    query = (primary + secondary)[:MAX_QUERY_TERMS]
    selected = list(ResearchCase.objects.filter(dataset=dataset, terms__token__in=query)
                    .annotate(overlap=Count("terms", distinct=True))
                    .order_by("-overlap", "case_key")[:MAX_CASES])
    ids = [case.pk for case in selected]
    # Closed categories and counts: never original text, names, instructions,
    # historical freelancer claims, URLs, or participant identifiers.
    routes = Counter(case.application_route if case.application_route in
                     {"Cold", "Invitation"} else "Unknown" for case in selected)
    outcomes = Counter("Hired" if case.outcome == "Hired" else
                       "No response" if case.outcome == "No response" else
                       "Did not progress" if case.outcome == "Did not progress / not hired" else
                       "Unknown/other" for case in selected)
    fidelity = Counter("Full text" if case.source_fidelity.lower().startswith("full") else
                       "Partial/summary" for case in selected)
    coverage = list(ResearchRequirement.objects.filter(research_case_id__in=ids)
                    .values("importance", "coverage").annotate(count=Count("id")))
    allowed_importance = {"Core", "Mandatory", "Bonus", "Application instruction"}
    allowed_coverage = {"Explicit", "Partial", "Not met", "Unknown"}
    coverage_counts = Counter()
    for row in coverage:
        importance = row["importance"] if row["importance"] in allowed_importance else "Other"
        covered = row["coverage"] if row["coverage"] in allowed_coverage else "Unknown"
        coverage_counts[f"{importance}: {covered}"] += row["count"]
    notes = ResearchNote.objects.filter(dataset=dataset, case_key__in=[case.case_key for case in selected])
    summary = {
        "algorithm": ALGORITHM, "snapshot": dataset.pk, "dataset_cases": dataset.case_count,
        "selected_cases": len(selected), "maximum_selected": MAX_CASES,
        "application_routes": dict(sorted(routes.items())), "outcomes": dict(sorted(outcomes.items())),
        "evidence_quality": dict(sorted(fidelity.items())),
        "requirement_coverage": dict(sorted(coverage_counts.items())),
        "observations_recorded": notes.filter(note_type="Observed fact").count(),
        "hypotheses_recorded": notes.filter(note_type="Hypothesis").count(),
        "cases_with_recorded_confounds": sum(bool(case.outcome_confounds.strip()) for case in selected),
    }
    text = """INTERNAL RESEARCH DECISION SUPPORT
Research is observational, not proof of causation. A hire or no response cannot establish why it happened.
These counts describe a bounded comparable sample, not probabilities, validated rules or guaranteed results.
Keep invitation, cold and unknown routes distinct. Consider profile, bid, timing, competition, client preference and job scope as confounds.
Hypotheses remain untested. Full text fidelity does not establish causal validity.
Use coverage gaps to check the CURRENT job's explicit instructions and requested questions. Explain relevant experience only from the CURRENT freelancer's supplied facts. Never invent missing skills, costs, timelines or bids.
Do not mention this research, its statistics, participants or historical wording in customer output. Return only the requested application.
If no cases match, use the supplied freelancer/job evidence without inventing research conclusions.
""" + json.dumps(summary, sort_keys=True, separators=(",", ":"))
    if len(text) > MAX_CONTEXT_CHARACTERS:
        raise ResearchUnavailable()
    return ResearchContext(text, dataset.pk, dataset.case_count, len(selected),
                           tuple(case.case_key for case in selected))
