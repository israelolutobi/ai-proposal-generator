# ProposalQ research intelligence MVP

ProposalQ now has a database-backed bridge between the master research workbook and proposal generation.

## Flow

1. Research continues in `ProposalIQ_Master_Research_Tracker_updated_paid.xlsx` (or a later workbook with the same required sheet/header contract).
2. An operator imports a snapshot with `python manage.py import_research_workbook /path/to/workbook.xlsx` while pointed at the intended database.
3. The import is atomic. The prior snapshot becomes inactive only when the new snapshot is accepted. Exact duplicate workbook bytes are rejected by SHA-256.
4. Every real ProposalQ proposal-generation call extracts the confirmed job characteristics from the existing application context.
5. The retrieval layer traverses the active research dataset, ranks cases by deterministic lexical overlap, applies small evidence-quality tie breaks, and selects at most eight comparable cases.
6. It aggregates observable fit, gaps, instruction coverage, requirement coverage and confounds into a bounded internal research context.
7. That context is appended to the provider input. The existing writing rules still prohibit invented freelancer evidence. Raw historical proposal text and participant identity are not sent as research context.

## What this MVP does not claim

The current research is observational and small. Outcome is displayed as evidence but never boosts retrieval relevance. The context explicitly tells the model not to infer causation. The engine is intentionally simple enough to replace later with indexed retrieval, precomputed aggregates, embeddings or learned ranking without changing the workbook-to-database boundary.

## Workbook contract

Required sheets are `Cases`, `Job Breakdown`, `Proposal Breakdown`, `Requirement Coverage`, and `Research Notes`. Headers are read from row 3, matching the current master tracker. The importer intentionally ignores raw `Evidence` proposal/job text for generation; it stores the structured analytical fields instead.

## Updating the research

Do not edit Python rules for each research round. Update the master workbook, review it, then import the new snapshot. Subsequent proposal generations automatically use the newest active dataset.

For a production Neon database, run the command from a trusted operator environment with the production database connection configured. Do not commit the private research workbook or database credentials to this public repository.
