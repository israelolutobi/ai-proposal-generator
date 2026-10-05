FROM python:3.13.5-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHON_DOTENV_DISABLED=1

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# An allowlisted build context excludes credentials, SQLite and local artifacts.
COPY . ./
RUN python scripts/build_static.py

RUN groupadd --system proposalq && useradd --system --gid proposalq proposalq
USER proposalq
EXPOSE 8000
STOPSIGNAL SIGTERM
CMD ["gunicorn", "--config", "gunicorn.conf.py", "mysite.wsgi:application"]
