"""Import the ProposalIQ master research workbook without adding an XLSX dependency."""
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from proposal_ai.models import ResearchCase, ResearchDataset, ResearchNote, ResearchRequirement

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "p": "http://schemas.openxmlformats.org/package/2006/relationships"}
REQUIRED = {"Cases", "Job Breakdown", "Proposal Breakdown", "Requirement Coverage", "Research Notes"}


def _col(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for char in letters:
        n = n * 26 + ord(char) - 64
    return n - 1


def _xlsx_rows(path):
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iterfind(".//m:t", NS)))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
        result = {}
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            name = sheet.attrib["name"]
            target = targets[sheet.attrib[f"{{{NS['r']}}}id"]]
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            root = ET.fromstring(archive.read(target))
            rows = []
            for row in root.findall(".//m:sheetData/m:row", NS):
                values = {}
                for cell in row.findall("m:c", NS):
                    idx = _col(cell.attrib["r"])
                    kind = cell.attrib.get("t")
                    value = cell.find("m:v", NS)
                    if kind == "inlineStr":
                        text = "".join(t.text or "" for t in cell.iterfind(".//m:t", NS))
                    elif value is None:
                        text = ""
                    elif kind == "s":
                        text = shared[int(value.text)]
                    else:
                        text = value.text or ""
                    values[idx] = text
                width = max(values, default=-1) + 1
                rows.append([values.get(i, "") for i in range(width)])
            result[name] = rows
        return result


def _table(rows):
    if len(rows) < 3:
        return []
    headers = rows[2]
    return [{headers[i]: row[i] if i < len(row) else "" for i in range(len(headers)) if headers[i]}
            for row in rows[3:] if any(str(v).strip() for v in row)]


def _decimal(value):
    if value in (None, ""): return None
    try: return Decimal(str(value))
    except InvalidOperation: return None


def _text(value):
    return str(value or "").strip()


class Command(BaseCommand):
    help = "Import a ProposalIQ master research XLSX snapshot into the research intelligence tables."

    def add_arguments(self, parser):
        parser.add_argument("workbook")

    def handle(self, *args, **options):
        path = Path(options["workbook"])
        if not path.is_file():
            raise CommandError("Research workbook does not exist.")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if ResearchDataset.objects.filter(source_sha256=digest).exists():
            raise CommandError("This exact research workbook snapshot has already been imported.")
        try:
            sheets = _xlsx_rows(path)
        except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as error:
            raise CommandError("The research workbook is not a readable XLSX file.") from error
        missing = REQUIRED - set(sheets)
        if missing:
            raise CommandError("Research workbook is missing required sheets: " + ", ".join(sorted(missing)))

        cases = _table(sheets["Cases"])
        jobs = {r.get("Case ID"): r for r in _table(sheets["Job Breakdown"])}
        proposals = {r.get("Case ID"): r for r in _table(sheets["Proposal Breakdown"])}
        requirements = _table(sheets["Requirement Coverage"])
        notes = _table(sheets["Research Notes"])
        if not cases:
            raise CommandError("Research workbook contains no cases.")

        with transaction.atomic():
            ResearchDataset.objects.update(active=False)
            dataset = ResearchDataset.objects.create(source_name=path.name, source_sha256=digest,
                                                     case_count=len(cases), active=True)
            created = {}
            for row in cases:
                key = _text(row.get("Case ID"))
                if not key or key in created:
                    raise CommandError("Every research case must have a unique Case ID.")
                job, proposal = jobs.get(key, {}), proposals.get(key, {})
                obj = ResearchCase.objects.create(
                    dataset=dataset, case_key=key, participant_key=_text(row.get("Participant")),
                    round_label=_text(row.get("Round")), case_type=_text(row.get("Case type")),
                    outcome=_text(row.get("Outcome")), application_route=_text(row.get("Application route")),
                    client_relationship=_text(row.get("Client relationship")), domain_niche=_text(row.get("Domain / niche")),
                    job_title=_text(row.get("Job title")), budget_type=_text(row.get("Budget type")),
                    budget_low=_decimal(row.get("Advertised budget low")), budget_high=_decimal(row.get("Advertised budget high")),
                    submitted_rate=_decimal(row.get("Submitted bid/rate")), final_rate=_decimal(row.get("Final rate")),
                    currency=_text(row.get("Currency")), competition_timing=_text(row.get("Competition / timing")),
                    record_status=_text(row.get("Record status")), job_core_problem=_text(job.get("Core problem")),
                    primary_stack_domain=_text(job.get("Primary stack / domain")), required_skills=_text(job.get("Required experience / skills")),
                    explicit_instructions=_text(job.get("Explicit application instructions")), scope_complexity=_text(job.get("Scope complexity")),
                    risk_signals=_text(job.get("Risk / difficulty signals")), observable_fit_signals=_text(job.get("Observable fit signals")),
                    opening_approach=_text(proposal.get("Opening approach")), experience_evidence=_text(proposal.get("Experience evidence")),
                    direct_job_match_evidence=_text(proposal.get("Direct job-match evidence")), solution_approach=_text(proposal.get("Solution / approach described")),
                    tools_stack=_text(proposal.get("Specific tools / stack mentioned")), instruction_coverage=_text(proposal.get("Addresses explicit instructions?")),
                    strong_features=_text(proposal.get("Strong observable features")), observable_gaps=_text(proposal.get("Observable gaps")),
                    outcome_confounds=_text(proposal.get("Outcome confounds")), source_fidelity=_text(proposal.get("Source fidelity") or job.get("Source fidelity")),
                )
                created[key] = obj
            for row in requirements:
                case = created.get(_text(row.get("Case ID")))
                if case and _text(row.get("Job requirement / instruction")):
                    ResearchRequirement.objects.create(research_case=case, requirement=_text(row.get("Job requirement / instruction")),
                        importance=_text(row.get("Importance")), proposal_evidence=_text(row.get("Proposal evidence")),
                        coverage=_text(row.get("Coverage")), interpretation_note=_text(row.get("Interpretation note")))
            for row in notes:
                observation = _text(row.get("Observation / hypothesis"))
                if observation:
                    ResearchNote.objects.create(dataset=dataset, case_key=_text(row.get("Case ID")), note_type=_text(row.get("Note type")),
                        observation=observation, why_it_matters=_text(row.get("Why it matters")), confidence=_text(row.get("Confidence")),
                        do_not_overclaim=_text(row.get("Do not overclaim")))

        self.stdout.write(self.style.SUCCESS(f"Imported {len(cases)} research cases as dataset {dataset.pk}."))
