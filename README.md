# AI Proposal Generator

A Django-based AI tool that helps generate tailored freelance proposal drafts.

## Overview

AI Proposal Generator is a web application designed to help freelancers create more relevant and structured proposal drafts for freelance job opportunities.

The project focuses on combining user profile data, job post information, and AI-assisted text generation to produce proposal drafts that can be reviewed and edited before submission.

This project was built to strengthen skills in Django, backend development, database modelling, template rendering, and AI workflow design.

---

## Features

- Freelancer profile creation
- Job post input workflow
- Proposal draft generation concept
- Django models for storing profile and proposal-related data
- Template-based web pages
- Database migrations
- Separation of project and app logic
- Environment variable support for sensitive configuration

---

## Tech Stack

- Python
- Django
- SQLite
- HTML templates
- OpenAI API concept
- Git & GitHub

---

## Installation

Clone the repository:

```bash
git clone https://github.com/israelolutobi/ai-proposal-generator.git
```

Navigate into the project folder:

```bash
cd ai-proposal-generator
```

Install dependencies:

```bash
pip install -r requirements.txt
```

### Local configuration

For first-time setup, copy `.env.example` to the ignored root `.env` file.
Preserve any existing `.env` and add missing settings rather than overwriting it:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Set your own private `SECRET_KEY` in the environment or `.env`.
The application will not start if it is missing or blank.

`DEBUG` defaults to `False`. For development over local HTTP, explicitly set
the following environment variable or add it to `.env`:

```dotenv
DEBUG=True
```

With debug enabled, session and CSRF cookies can be used over local HTTP.
Leave debug disabled in production, where these cookies require HTTPS.
Only `true` enables debug (case-insensitive, with surrounding whitespace ignored).
`False`, missing values, and invalid values leave debug disabled.
Process environment variables take precedence over values in `.env`.
Never commit `.env` or secret values. `OPENAI_API_KEY` is needed only for AI
features; it can remain blank for non-AI development and tests.

Run migrations:

```bash
.\.venv\Scripts\python.exe manage.py migrate
```

Start the development server:

```bash
.\.venv\Scripts\python.exe manage.py runserver
```

AI calls go through `proposal_ai/services.py`. Existing prompts, models and
API styles are preserved. Requests use a 5-second connection timeout and
45-second read/write/pool timeouts (network inactivity limits, not an overall
deadline). SDK retries are disabled; users can retry a failed request.

Run tests with `.\.venv\Scripts\python.exe manage.py test`. The configured
test runner clears AI credentials and blocks networking, so tests need no
OpenAI key. Mock the application service functions in view tests; service tests
use mocked SDK clients or in-memory HTTP transports. Tests run serially;
parallel execution is rejected until worker network guards are implemented.

Beta AI limits are defined in `proposal_ai/ai_limits.py`, separately from storage:
pasted/confirmed descriptions 20,000 Unicode characters, job skills 2,000;
summary title/skills 255/2,000; profile summary sent to AI 5,000. Selected experience
tasks/skills/depth allow 4,000/1,000/2,000 characters; role/company allow 255 each.
Choose up to 10 owned experiences (8,000 characters per formatted record and
20,000 total). All are initially selected only when they fit; otherwise explicitly
choose a subset. Stored records are never deleted or truncated by selection.
Formatted profile/job context ceilings are 6,000/24,000 characters. Final summary,
extraction and proposal requests allow 4,000/24,000/60,000 characters, with a shared
128,000 UTF-8 byte backstop. Fields are outer-trimmed; constructed context and
requests count their exact formatting. Browser counters count Unicode code points;
server validation is authoritative.

Chat completion caps are 2,048/8,192/6,144 tokens for summary/extraction/proposal,
including reasoning. Visible summaries/proposals allow 2,000/8,000 characters.
Only complete (`finish_reason="stop"`) responses are accepted. Extraction raw JSON
is limited to 281,000 characters before parsing: the known schema's 23,310 bounded
string characters may each take 12 characters as escaped non-BMP JSON, plus keys,
budgets and normal formatting. Arbitrarily padded/extra JSON is bounded too.
Over-limit input or incomplete/oversized output fails without partial persistence,
silent truncation or automatic retries. These provisional caps are not exact input
token budgets or measured latency guarantees. Per-user quotas and evidence ranking
remain separate work. The unused legacy Responses helper is not an active workflow.

---

## Project Status

Active development.

This project is currently being developed as a portfolio project focused on applied AI, freelance workflow automation, and backend web development.

---

## Future Improvements

- Improve proposal generation logic
- Add user authentication
- Add proposal editing and saving
- Improve UI styling
- Add OpenAI API integration safeguards
- Add testing
- Deploy a live demo
- Add screenshots and usage examples

---

## Notes

This project is intended as a portfolio and learning project. Sensitive configuration such as API keys and environment variables are excluded from version control.
