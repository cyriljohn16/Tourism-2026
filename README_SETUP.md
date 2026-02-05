Project setup (quick)

1. Create and activate virtualenv

   - Windows (PowerShell):
     ```powershell
     py -m venv .venv
     .\.venv\Scripts\Activate.ps1
     ```

2. Install dependencies

   ```powershell
   py -m pip install --upgrade pip
   py -m pip install -r requirements.txt
   ```

3. Environment

   - Create a `.env` file in project root with any required secrets (e.g. `SECRET_KEY`, DB settings).
   - The project uses `python-dotenv` to load `.env` in `tourism_project/settings.py`.

4. Database migrations and run

   ```powershell
   py manage.py migrate
   py manage.py runserver
   ```

Notes
- I installed the following packages during debugging: `python-dotenv`, `django-mathfilters`, `django-crispy-forms`, `requests`, `pytz`, `qrcode` and `pillow`.
- See `requirements.txt` for the full pinned list.
