"""Hosted Linux startup. Administrative build/release work is separate."""
import os

from dotenv import load_dotenv
from mysite.runtime import gunicorn_configuration, validate_gunicorn_startup


load_dotenv()
globals().update(gunicorn_configuration(os.environ))
on_starting = validate_gunicorn_startup
