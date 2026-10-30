# IGSP — Icebolethu Group Supplier Portal

Flask app where suppliers register, complete a 4-step application (profile → category → documents → review), and admins review, approve, and report on applications.

## Setup

```bash
python3 -m venv env && source env/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in DB + email settings
flask --app app init-db     # create tables / apply schema upgrades
flask --app app create-admin
flask --app app run --debug
```

Without email settings, verification codes and password-reset links are printed to the server log, which is handy for local development. On AWS, set `MAIL_PROVIDER=ses` to send through the Amazon SES API with the server's IAM role (the server needs `ses:SendEmail` for the sender address); otherwise the `SMTP_*` settings are used.

## Useful commands

| Command | What it does |
| --- | --- |
| `flask --app app create-admin` | Create an admin (or reset an existing admin's password) |
| `flask --app app seed-demo --count 30` | Insert demo suppliers (dev only; password `Password123!`) |
| `flask --app app seed-audit` | Insert dummy user-audit entries for the existing admins (dev only; IPs in 198.51.100.x; `--remove` deletes them) |

## Test admin accounts

The local development database has five test admins for each role. They all use the password `Password123!`. These accounts are for local development only; never create them on staging or production with this password.

| Role | Can open | Logins |
| --- | --- | --- |
| Procurement Team Lead (`super`) | Everything | `teamlead1@icebolethu.co.za` … `teamlead5@icebolethu.co.za` |
| Procurement Officer (`applications`) | Applications and suppliers | `officer1@icebolethu.co.za` … `officer5@icebolethu.co.za` |
| Procurement Manager (`reports`) | Reports only | `manager1@icebolethu.co.za` … `manager5@icebolethu.co.za` |
| ICT Admin (`ict`) | Everything except the dashboard, plus user audit, email templates and deleting applications and directors | `ict1@icebolethu.co.za` … `ict5@icebolethu.co.za` |

To create them in another local database, run `flask --app app create-admin --email <email> --name "<name>" --role <super|applications|reports|ict> --password 'Password123!'` for each account. The role key in brackets is what `--role` takes.

## Admin sign-in through Cognito (staging and production)

On AWS, Icebolethu staff sign in through the company's Cognito user pool, the same one the other Icebolethu systems use. **Continue with Microsoft** goes through Cognito to Microsoft; "Sign in with your Icebolethu login" opens Cognito's own sign-in page. It's switched on by the `COGNITO_*` settings (see `.env.example`).

Access is managed in Cognito, not in the portal. Add a person to **one** of these Cognito groups:

| Cognito group | Portal role |
| --- | --- |
| `IGSP_PROCUREMENT_TEAMLEAD` | Procurement Team Lead (`super`) |
| `IGSP_PROCUREMENT_ICT` | ICT Admin (`ict`) |
| `IGSP_PROCUREMENT_OFFICER` | Procurement Officer (`applications`) |
| `IGSP_PROCUREMENT_MANAGER` | Procurement Manager (`reports`) |

- Their admin account is created the first time they sign in, with the role from their group. If they're in more than one group, the highest role in the list above wins.
- Their role follows their group: change the group and the new role applies at their next sign-in.
- Anyone signing in who isn't in one of these groups is refused, and the refusal shows in the user audit.
- Someone signing in with Microsoft for the first time only appears in Cognito after that first attempt (as `AzureAD_...`). Add them to their group then, and they sign in again.

The email and password login still works for admins created with `create-admin`.

## Production

```bash
gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 app:app
```

Set `SECRET_KEY`, `SESSION_COOKIE_SECURE=1` (behind HTTPS) and leave `FLASK_DEBUG=0`. Login rate limiting is in-process, so a single worker with threads keeps it accurate.

Uploaded documents are stored in `static/uploads/<user id>/` but are **not** publicly served; `/uploads/...` only returns a file to the supplier who owns it or to an admin. Back this folder up together with the database.

## Staging and deploys

Staging runs at https://igsp.staging.icebolethu.co.za (AWS af-south-1). Every push to `main` deploys there automatically through GitHub Actions ([`.github/workflows/deploy-staging.yml`](.github/workflows/deploy-staging.yml)):

1. **Build check:** install the requirements, compile, and import the app. This also runs on every pull request.
2. **Deploy:** only if the check passes, and only from `main`. The workflow signs in to AWS through GitHub OIDC (no stored keys) and runs a fixed deploy script on the server. The script pulls `main`, installs dependencies, runs `init-db`, restarts, and checks `/health`. **If anything fails, it rolls back to the previous version automatically.**

To deploy, merge to `main` and watch the run in the **Actions** tab. To redeploy without a code change, use **Run workflow** on the "Deploy to staging" workflow.
