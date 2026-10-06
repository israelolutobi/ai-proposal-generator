"""Deterministic retrieval for ProposalQ's research-informed generation."""
from dataclasses import dataclass
import re
from types import SimpleNamespace

from .models import ResearchDataset

MAX_CASES = 8
MAX_CONTEXT_CHARACTERS = 9_000
_TOKEN = re.compile(r"[a-z0-9+#.]{2,}", re.I)
_STOP = {"the", "and", "for", "with", "that", "this", "from", "your", "you", "are", "job", "project", "work", "client", "using", "into", "our"}


def _tokens(value):
    return {x.lower() for x in _TOKEN.findall(value or "") if x.lower() not in _STOP}


def _case_text(case):
    return " ".join((case.domain_niche, case.job_title, case.job_core_problem,
                     case.primary_stack_domain, case.required_skills, case.tools_stack,
                     case.explicit_instructions, case.observable_fit_signals))


def _score(case, query_tokens):
    case_tokens = _tokens(_case_text(case))
    overlap = len(query_tokens & case_tokens)
    fidelity = 1 if "full" in (case.source_fidelity or "").lower() else 0
    complete = 1 if (case.record_status or "").lower() == "complete" else 0
    return overlap * 10 + fidelity + complete


@dataclass(frozen=True, slots=True)
class ResearchContext:
    text: str
    dataset_id: int | None
    total_cases: int
    matched_cases: int


def build_research_context(job_post):
    dataset = ResearchDataset.objects.filter(active=True).order_by("-imported_at").first()
    if dataset is None:
        return ResearchContext(
            "No research dataset is currently available. Base the application only on the supplied freelancer and job evidence.",
            None, 0, 0,
        )
    query = " ".join(filter(None, [job_post.job_title, job_post.job_description,
                                    job_post.skills_required, job_post.budget_type]))
    query_tokens = _tokens(query)
    cases = list(dataset.cases.prefetch_related("requirements").all())
    ranked = sorted(((_score(case, query_tokens), case) for case in cases),
                    key=lambda item: (-item[0], item[1].case_key))
    selected = [(score, case) for score, case in ranked if score > 0][:MAX_CASES]
    lines = [
        "PROPOSALQ RESEARCH CONTEXT",
        f"Research snapshot: {dataset.case_count} application cases; {len(selected)} relevant cases selected.",
        "Research is observational. Treat patterns as decision support, not proof that any proposal feature caused an outcome.",
        "Never copy historical proposal wording, participant identity, or unsupported claims into the user's application.",
    ]
    for score, case in selected:
        lines.extend([
            "", f"Comparable case {case.case_key} (relevance {score}; outcome: {case.outcome or 'unknown'}; route: {case.application_route or 'unknown'}):",
            f"- Domain/job: {case.domain_niche or case.job_title}",
            f"- Observable fit: {case.observable_fit_signals or 'not recorded'}",
            f"- Strong features: {case.strong_features or 'not recorded'}",
            f"- Observable gaps: {case.observable_gaps or 'not recorded'}",
            f"- Instruction coverage: {case.instruction_coverage or 'not recorded'}",
            f"- Confounds: {case.outcome_confounds or 'not recorded'}",
        ])
        requirements = list(case.requirements.all())
        if requirements:
            lines.append("- Requirement evidence: " + "; ".join(
                f"{r.importance or 'Requirement'}: {r.requirement} [{r.coverage or 'unknown'}]" for r in requirements[:8]
            ))
    notes = dataset.notes.filter(note_type__iexact="Research rule")[:5]
    if notes:
        lines.append("\nResearch rules:")
        for note in notes:
            lines.append(f"- {note.observation} {note.do_not_overclaim}".strip())
    text = "\n".join(lines)
    if len(text) > MAX_CONTEXT_CHARACTERS:
        text = text[:MAX_CONTEXT_CHARACTERS].rsplit("\n", 1)[0] + "\n[Research context truncated to the MVP safety budget.]"
    return ResearchContext(text, dataset.pk, dataset.case_count, len(selected))


def build_research_context_from_application(application_context):
    """Bridge the existing generation boundary to the research DB without changing views."""
    def field(label, next_labels):
        marker = label + ":"
        start = application_context.find(marker)
        if start < 0:
            return ""
        start += len(marker)
        end = len(application_context)
        for next_label in next_labels:
            pos = application_context.find("\n" + next_label + ":", start)
            if pos >= 0:
                end = min(end, pos)
        return application_context[start:end].strip()
    labels = ["Job Title", "Job Description", "Budget Type", "Skills Required", "Client Location"]
    values = {}
    for i, label in enumerate(labels):
        values[label] = field(label, labels[i + 1:])
    job = SimpleNamespace(job_title=values["Job Title"], job_description=values["Job Description"],
                          budget_type=values["Budget Type"], skills_required=values["Skills Required"])
    return build_research_context(job)
