# Experience Bayawan Tourism System

Deployment-oriented guide for CATIPO/Tourism Office IT turnover.

## 1. Requirements

- Python 3.11+ recommended
- MySQL/MariaDB (or configured Django database)
- `pip` and virtual environment support

Install dependencies:

```bash
pip install -r requirements.txt
```

## 2. Environment Setup

Copy `.env.example` to `.env` and set deployment values.

Minimum important variables:

- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS`
- `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`
- `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`
- `GEMINI_API_KEY` (if Gemini features are enabled)

## 3. Database and Static

Run:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
```

Optional admin account:

```bash
python manage.py createsuperuser
```

## 4. Run (Local Verification)

```bash
python manage.py runserver
```

App URL (local): `http://127.0.0.1:8000/`

## 5. Key Operational Scope

- Accommodation flow is preview + provider handoff only.
- No internal accommodation payment processing.
- Tour booking remains internal with workflow statuses.
- Monthly reports and tourist influx monitoring are available for owner/admin workflows.

## 6. Deployment Notes

- Keep `DEBUG=False` in production.
- Use HTTPS and secure cookie settings in production environment variables.
- Ensure email credentials and API keys are configured in environment variables only.
- Do not commit `.env` or secret keys.

## 7. Turnover References

- `requirements.txt` for package installation
- `.env.example` for environment template
- `build.sh` for deployment build step (if using Render-style flow)
- Existing markdown docs in repository for SOP/process context
