# PharmacyPulse (BEN-156) — Django port

Server-side-rendered Django port of the Benmore PharmacyPulse prototype.
Community-powered pharmacy review platform for ByondRx.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo --reset
python manage.py runserver 8765
open http://127.0.0.1:8765
```

## Environment variables

Flow endpoints read real keys when present and return a friendly error
when they aren't. Copy the table into a `.env` (or export in your shell)
as needed:

| Key                                                      | Used by                                                         | Required? |
| -------------------------------------------------------- | --------------------------------------------------------------- | --------- |
| `DJANGO_SECRET_KEY`                                      | Django                                                          | prod      |
| `DJANGO_DEBUG`                                           | Django                                                          | dev=1     |
| `DJANGO_ALLOWED_HOSTS`                                   | Django                                                          | prod      |
| `DATABASE_URL`                                           | Postgres                                                        | prod      |
| `GOOGLE_PLACES_KEY`                                      | `ingest_pharmacies_by_zip`, `fetch_pharmacy_details`, map tiles | optional  |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`  | social login                                                    | optional  |
| `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`                   | `create_checkout`                                               | optional  |
| `SITE_URL`                                               | Stripe redirect                                                 | optional  |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` | claim OTP                                                       | optional  |

## Data model

17 tables ported from `BEN-156-pharmacypulse/schema.sql`. Custom `User`
replaces `_benmore_users` so Benmore's auth schema is preserved 1:1.

## Flows

25 flow endpoints mounted under `/api/flow/…` matching Benmore's paths
and semantics (FDA shortage sync, Google Places ingest, NPI auto-verify,
Stripe checkout, CCPA export/delete, team invite/accept, newsletter,
compare, etc.).

## Hooks

6 Benmore hooks ported as Django signals:

- `reviews` insert → rate-limit + PHI/profanity moderation + recompute
  time-decay stock_confidence; activity_log entry
- `reviews` update → recompute stats + response_counts tick
- `reviews` delete → activity_log entry
- `pharmacy_claims` insert/update → activity_log
- `_benmore_users` insert → TCPA/ToS/DNS consents

## Deployment

See Procfile + Dockerfile. Works on Render/Railway/Fly with
`DATABASE_URL` pointing at Postgres.

## Local Deployment On Windows Using Django dev server

cd "/mnt/d/Program Files/xamp/htdocs/Niishcloud PharmacyPulse"

# 1. Create & activate a virtualenv

python -m venv .venv ; .\.venv\Scripts\Activate.ps1


python3 -m venv .venv
source .venv/bin/activate # Windows (Git Bash): source .venv/Scripts/activate # Windows (cmd): .venv\Scripts\activate

# 2. Install deps

pip install -r requirements.txt

# 3. Database (SQLite is default, no Postgres needed)

python manage.py migrate

python manage.py sync_publish_pharmacies

# 4. Seed demo data (optional but recommended)

python manage.py seed_demo --reset

# 5. Run it

python manage.py runserver 8765
Open http://127.0.0.1:8765

```

```

# To access the admin dashboard:

1. URL: http://localhost:8765/admin-dashboard (left-click URL in browser)
2. Log in (if not already) — credentials I just set on the existing demo admin:

- email: admin@demo.pharmacypulse.com
- password: AdminPass123!

## To import postgres database:

- Open the terminal in the project directory
- Run the following command: & "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" -U postgres -d postgres --clean --if-exists --no-owner --no-privileges latest.dump

-


### Add Dummy Users
1. Run it with:

python manage.py seed_dummy_users

2. Role - Email - Password
   admin		admin@test.com		AdminPass123!
   pharmacist	pharmacist@test.com	PharmPass123!
   user		user@test.com		UserPass123!
