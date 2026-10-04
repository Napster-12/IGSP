# IGSP — Icebolethu Group Supplier Portal

Flask app where suppliers register, complete a 4-step application (profile → category → documents → review), and admins review, approve, and report on applications.

## Setup

```bash
python3 -m venv env && source env/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in DB + SMTP settings
flask --app app init-db     # create tables / apply schema upgrades
flask --app app create-admin
flask --app app run --debug
```

Without SMTP settings, verification codes and password-reset links are printed to the server log, which is handy for local development.

## Useful commands

| Command | What it does |
| --- | --- |
| `flask --app app create-admin` | Create an admin (or reset an existing admin's password) |
| `flask --app app seed-demo --count 30` | Insert demo suppliers (dev only; password `Password123!`) |

## Production

```bash
gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 app:app
```

Set `SECRET_KEY`, `SESSION_COOKIE_SECURE=1` (behind HTTPS) and leave `FLASK_DEBUG=0`. Login rate limiting is in-process, so a single worker with threads keeps it accurate.

Uploaded documents are stored in `static/uploads/<user id>/` but are **not** publicly served; `/uploads/...` only returns a file to the supplier who owns it or to an admin. Back this folder up together with the database.
