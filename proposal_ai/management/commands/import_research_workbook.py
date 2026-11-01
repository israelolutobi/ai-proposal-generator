"""Import the ProposalIQ master research workbook without adding an XLSX dependency."""
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, connection
from django.core.exceptions import ValidationError
from django.conf import settings

from proposal_ai.models import ResearchCase, ResearchDataset, ResearchNote, ResearchRequirement, ResearchTerm
from proposal_ai.research_intelligence import case_tokens

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
        entries = archive.infolist()
        if len(entries) > 1000 or sum(e.file_size for e in entries) > 100_000_000:
            raise ValueError("Workbook expansion exceeds import limit.")
        if len({e.filename for e in entries}) != len(entries):
            raise ValueError("Duplicate archive members.")
        def xml(member):
            data = archive.read(member)
            if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                raise ValueError("Unsupported XML declaration.")
            return ET.fromstring(data)
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = xml("xl/sharedStrings.xml")
            for si in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iterfind(".//m:t", NS)))
        workbook = xml("xl/workbook.xml")
        rels = xml("xl/_rels/workbook.xml.rels")
        targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
        result = {}
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            name = sheet.attrib["name"]
            target = targets[sheet.attrib[f"{{{NS['r']}}}id"]]
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            root = xml(target)
            if name not in REQUIRED:
                continue
            rows = []
            for row in root.findall(".//m:sheetData/m:row", NS):
                number = int(row.attrib["r"])
                if number < 1 or number > 100_003 or number <= len(rows):
                    raise ValueError("Invalid row coordinates.")
                while len(rows) < number - 1:
                    rows.append([])
                values = {}
                for cell in row.findall("m:c", NS):
                    idx = _col(cell.attrib["r"])
                    if idx > 100 or idx in values:
                        raise ValueError("Invalid column coordinates.")
                    if cell.find("m:f", NS) is not None:
                        raise ValueError("Formulas are unsupported in imported research tables.")
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
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        return number
    except InvalidOperation:
        raise CommandError("Invalid numeric research value.") from None


def _text(value):
    return str(value or "").strip()


def _create(model, **fields):
    obj = model(**fields)
    try:
        obj.full_clean()
    except ValidationError:
        raise CommandError("Research row fails field validation.") from None
    obj.save()
    return obj


class Command(BaseCommand):
    help = "Import a ProposalIQ master research XLSX snapshot into the research intelligence tables."

    def add_arguments(self, parser):
        parser.add_argument("workbook")
        parser.add_argument("--validate-only", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["workbook"])
        if not path.is_file():
            raise CommandError("Research workbook does not exist.")
        if path.stat().st_size > 20_000_000:
            raise CommandError("Workbook exceeds the 20 MB import limit.")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            sheets = _xlsx_rows(path)
        except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, IndexError, AttributeError, OSError):
            raise CommandError("The research workbook is not a supported XLSX file.") from None
        missing = REQUIRED - set(sheets)
        if missing:
            raise CommandError("Research workbook is missing required sheets: " + ", ".join(sorted(missing)))

        expected = {
            "Cases": {"Case ID", "Participant", "Round", "Case type", "Outcome", "Application route", "Client relationship", "Domain / niche", "Job title", "Budget type", "Advertised budget low", "Advertised budget high", "Submitted bid/rate", "Final rate", "Currency", "Competition / timing", "Record status"},
            "Job Breakdown": {"Case ID", "Core problem", "Primary stack / domain", "Required experience / skills", "Explicit application instructions", "Scope complexity", "Risk / difficulty signals", "Observable fit signals", "Source fidelity"},
            "Proposal Breakdown": {"Case ID", "Opening approach", "Experience evidence", "Direct job-match evidence", "Solution / approach described", "Specific tools / stack mentioned", "Addresses explicit instructions?", "Strong observable features", "Observable gaps", "Outcome confounds", "Source fidelity"},
            "Requirement Coverage": {"Case ID", "Job requirement / instruction", "Importance", "Proposal evidence", "Coverage", "Interpretation note"},
            "Research Notes": {"Case ID", "Note type", "Observation / hypothesis", "Why it matters", "Confidence", "Do not overclaim"},
        }
        for sheet, headers in expected.items():
            actual = sheets[sheet][2] if len(sheets[sheet]) >= 3 else []
            nonempty = [h for h in actual if h]
            if len(nonempty) != len(set(nonempty)) or not headers.issubset(actual):
                raise CommandError("Invalid required headers in " + sheet + ".")
        cases = _table(sheets["Cases"])
        jobs = {r.get("Case ID"): r for r in _table(sheets["Job Breakdown"])}
        proposals = {r.get("Case ID"): r for r in _table(sheets["Proposal Breakdown"])}
        requirements = _table(sheets["Requirement Coverage"])
        notes = _table(sheets["Research Notes"])
        if not cases:
            raise CommandError("Research workbook contains no cases.")
        keys = [_text(r.get("Case ID")) for r in cases]
        if len(set(keys)) != len(keys) or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) for key in keys):
            raise CommandError("Every case requires a unique valid Case ID.")
        for sheet in ("Job Breakdown", "Proposal Breakdown"):
            linked = [_text(r.get("Case ID")) for r in _table(sheets[sheet])]
            if len(linked) != len(set(linked)) or set(linked) != set(keys):
                raise CommandError("Every case requires exactly one linked row in " + sheet + ".")
        for row in requirements:
            if _text(row.get("Case ID")) not in keys or not _text(row.get("Job requirement / instruction")):
                raise CommandError("Invalid requirement reference or empty requirement.")
        for row in notes:
            if (_text(row.get("Case ID")) not in keys + ["ROUND"] or
                    _text(row.get("Note type")) not in {"Observed fact", "Hypothesis", "Research rule"} or
                    not _text(row.get("Observation / hypothesis"))):
                raise CommandError("Invalid research note reference, type or observation.")
        if settings.APP_ENV == "production" and (connection.vendor != "postgresql" or settings.AI_ENABLED):
            raise CommandError("Production import requires PostgreSQL and AI disabled.")

        with transaction.atomic():
            if connection.vendor == "postgresql":
                # Serializes concurrent imports, including the empty-database case.
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [70422117])
            existing = ResearchDataset.objects.filter(source_sha256=digest).first()
            if existing and not options["validate_only"]:
                self.stdout.write("Snapshot already imported; active dataset unchanged.")
                return
            dataset = ResearchDataset(source_name=path.name, source_sha256=digest,
                                      case_count=len(cases), active=False)
            try:
                dataset.full_clean(validate_unique=False)
            except ValidationError:
                raise CommandError("Invalid dataset metadata.") from None
            if options["validate_only"]:
                dataset.source_sha256 = hashlib.sha256((digest + "validation").encode()).hexdigest()
            dataset.save()
            created = {}
            for row in cases:
                key = _text(row.get("Case ID"))
                if not key or key in created:
                    raise CommandError("Every research case must have a unique Case ID.")
                job, proposal = jobs.get(key, {}), proposals.get(key, {})
                obj = _create(ResearchCase,
                    dataset=dataset, case_key=key, participant_key=hashlib.sha256(_text(row.get("Participant")).encode()).hexdigest(),
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
                ResearchTerm.objects.bulk_create([ResearchTerm(research_case=obj, token=t) for t in sorted(case_tokens(obj))])
                created[key] = obj
            for row in requirements:
                case = created.get(_text(row.get("Case ID")))
                if case and _text(row.get("Job requirement / instruction")):
                    _create(ResearchRequirement, research_case=case, requirement=_text(row.get("Job requirement / instruction")),
                        importance=_text(row.get("Importance")), proposal_evidence=_text(row.get("Proposal evidence")),
                        coverage=_text(row.get("Coverage")), interpretation_note=_text(row.get("Interpretation note")))
            for row in notes:
                observation = _text(row.get("Observation / hypothesis"))
                if observation:
                    _create(ResearchNote, dataset=dataset, case_key=_text(row.get("Case ID")), note_type=_text(row.get("Note type")),
                        observation=observation, why_it_matters=_text(row.get("Why it matters")), confidence=_text(row.get("Confidence")),
                        do_not_overclaim=_text(row.get("Do not overclaim")))

            if options["validate_only"]:
                transaction.set_rollback(True)
                self.stdout.write(f"Validated {len(cases)} cases; database unchanged.")
                return
            ResearchDataset.objects.filter(active=True).update(active=False)
            dataset.active = True
            dataset.save(update_fields=["active"])

        self.stdout.write(self.style.SUCCESS(f"Imported {len(cases)} research cases as dataset {dataset.pk}."))
