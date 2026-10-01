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
