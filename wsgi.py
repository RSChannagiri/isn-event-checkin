"""WSGI entry point: `flask --app wsgi run` or `gunicorn wsgi:app`."""

from checkin import create_app

app = create_app()
