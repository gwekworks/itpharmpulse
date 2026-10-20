# Benmore-to-Django Conversion Guide

A practical reference for migrating Benmore platform apps to standalone Django 4.x.
Drawn from the PharmacyPulse (BEN-156) conversion.

---

## Overview

Benmore apps are built on a proprietary DSL layered over Go. The templates use
custom tags (`<query>`, `<include>`, `<page>`, `{{t "key"}}`), variable syntax
(`{{param_X}}`, `{{user_id}}`), and a YAML config layer (`app.yaml`, `flows.yaml`).
The migration strategy is **preserve the HTML/CSS faithfully** and swap only the
plumbing — queries, routing, auth, and i18n.

---

## 1. Project Scaffold

```
config/
  settings.py      # Django settings
  urls.py          # Root URLconf
  wsgi.py
pharmacypulse/
  _prod_queries.py # Auto-extracted SQL catalog (see §4)
  page_data.py     # Data providers for every page
  views.py         # Thin view wrappers
  urls.py          # App URL patterns
  flows.py         # All POST/mutation endpoints
  models.py        # Django ORM models
  auth_views.py    # Login / signup / OAuth
  templates/
    pharmacypulse/
      layouts/     # base.html, blank.html, app.html
      pages/       # One file per route
      partials/    # topbar, sidebar, footer…
  static/
    pharmacypulse/
      theme.css    # App-wide styles
      app.js
```

---

## 2. Template Syntax Conversion

### 2.1 Variables

| Benmore | Django |
|---|---|
| `{{variable}}` | `{{ variable }}` |
| `{{param_q}}` | `{{ param_q }}` (comes from `request.GET`) |
| `{{user_id}}` | `{{ request.user.id }}` |
| `{{csrf_token}}` | `{% csrf_token %}` (standalone tag, not inside `<input>`) |

> **Watch out**: Benmore `<input type="hidden" name="csrf_token" value="{{csrf_token}}">` must
> become a standalone `{% csrf_token %}` — leaving the `<input>` wrapper causes a duplicate CSRF field.

### 2.2 Query tags

Benmore embeds SQL directly in templates:

```html
<query sql="SELECT * FROM pharmacies WHERE id = '{{param_id}}'" as="p">
```

These must be **removed from templates entirely**. The SQL is extracted to
`_prod_queries.py` and run in `page_data.py` before the template renders.
See §4 for the extraction pipeline.

### 2.3 Includes and layouts

| Benmore | Django |
|---|---|
| `<include file="topbar.html">` | `{% include "pharmacypulse/partials/topbar.html" %}` |
| `<page extends="app.html">` | `{% extends "pharmacypulse/layouts/app.html" %}` |
| `{{#section name}}…{{/section}}` | `{% block name %}…{% endblock %}` |

### 2.4 Conditionals / loops

| Benmore | Django |
|---|---|
| `{{#if condition}}` | `{% if condition %}` |
| `{{/if}}` | `{% endif %}` |
| `{{#each items}}` | `{% for item in items %}` |
| `{{/each}}` | `{% endfor %}` |
| `{{@index}}` | `{{ forloop.counter0 }}` |

### 2.5 Filters

| Benmore | Django |
|---|---|
| `{{name \| upper}}` | `{{ name\|upper }}` |
| `{{date \| format:"MMM D, YYYY"}}` | `{{ date\|date:"M j, Y" }}` |
| `{{ts \| timesince}}` | `{{ ts\|timesince }}` — requires `datetime` object, not string |
| `{{initials name}}` | Custom `{% load pharmacypulse %}` → `{{ name\|initials }}` |

### 2.6 i18n

Replace `{{t "key"}}` / `{% t "key" %}` with Django gettext:

```html
{% load i18n %}
{% trans "Search pharmacies" %}
{% blocktrans %}Found {{ count }} results{% endblocktrans %}
```

Settings required:

```python
MIDDLEWARE = [..., "django.middleware.locale.LocaleMiddleware", ...]
LANGUAGE_COOKIE_NAME = "lang"   # Benmore uses "lang" not "django_language"
LOCALE_PATHS = [BASE_DIR / "locale"]
LANGUAGES = [("en", "English"), ("es", "Español")]
```

---

## 3. URL Routing

`app.yaml` paths map 1:1 to Django `urlpatterns`. Example:

```yaml
# app.yaml
routes:
  - path: /pharmacy
    page: pharmacy.html
  - path: /api/flow/submit-review
    flow: submit_review
```

```python
# urls.py
urlpatterns = [
    path("pharmacy", views.pharmacy_detail, name="pharmacy"),
    path("api/flow/submit-review", flows.submit_review, name="flow_submit_review"),
]
```

Flow endpoints (POST mutations) go in `flows.py`; page views go in `views.py`.

---

## 4. SQL / Query Migration

### 4.1 Extract queries from prod source

Run `scripts/convert_templates.py` — it:
1. Strips `<query>` tags from every template (using an attribute-aware regex — see §4.3).
2. Extracts `(alias, sql)` pairs and writes `pharmacypulse/_prod_queries.py`.

```python
PAGE_QUERIES = {
  "pharmacy.html": [
    ["p", "SELECT p.*, ... FROM pharmacies p WHERE p.id = '{{param_id}}'"],
    ["reviews", "SELECT r.* FROM reviews r WHERE r.pharmacy_id = '{{param_id}}' ..."],
  ],
  ...
}
```

### 4.2 Runtime execution — `_run_prod_queries`

```python
# page_data.py
_PARAM_RE = re.compile(r"'?\{\{param_(\w+)\}\}'?")
_USER_RE  = re.compile(r"\{\{user_id\}\}")

def _run_prod_queries(filename: str, request) -> dict:
    ctx = {}
    for alias, sql in PAGE_QUERIES.get(filename, []):
        # Substitute {{param_X}} — quoted form gets SQL-escaped string,
        # unquoted form gets raw value (for numeric columns).
        def _replace(m):
            val = request.GET.get(m.group(1), "")
            if m.group(0).startswith("'"):
                return f"'{str(val).replace(chr(39), chr(39)*2)}'"
            return str(val) if val else "NULL"
        sql = _PARAM_RE.sub(_replace, sql)
        sql = _USER_RE.sub(str(request.user.id or "NULL"), sql)

        with connection.cursor() as cur:
            cur.execute(sql)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Coerce ISO datetime strings so |timesince works
        rows = [_coerce(r) for r in rows]
        ctx[alias] = rows[0] if _is_single(alias) else rows
    return ctx
```

> **Key insight**: Use literal string substitution (not `%s` parameterized queries) because
> Benmore SQL often has `LIKE '{{param_q}}%'` where the `%` is a SQL wildcard, not a Python
> format placeholder — mixing the two breaks `cursor.execute`.

### 4.3 The `<query>` strip regex

Naive `<query[^>]*>` breaks on multi-line SQL containing `>` operators
(e.g. `stock_confidence > 0`). Use an attribute-aware pattern instead:

```python
QUERY_OPEN  = re.compile(r'<query\b(?:\s+\w+="[^"]*")*\s*>', re.DOTALL)
QUERY_CLOSE = re.compile(r'</query>')
# Also clean up orphan closing fragments like:  0" as="alias">
ORPHAN_LINE = re.compile(r'^\s*[^<{\s][^<{]*\s+as="[^"]*">\s*$')
```

### 4.4 `@with_prod_queries` decorator

```python
def with_prod_queries(filename: str):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(request, *args, **kwargs):
            ctx = _run_prod_queries(filename, request)
            # Inject all GET params as param_X
            for k, v in request.GET.items():
                ctx[f"param_{k}"] = v
            extra = fn(request, *args, **kwargs)
            if extra:
                ctx.update(extra)
            return ctx
        return wrapper
    return decorator

# Usage
@with_prod_queries("pharmacy.html")
def page_pharmacy_detail(request):
    return {"brand": settings.BRAND}
```

---

## 5. Authentication

Benmore's auth maps to Django's built-in `AbstractBaseUser`:

| Benmore | Django |
|---|---|
| `{{user.id}}` | `request.user.id` |
| `{{user.role}}` | `request.user.role` (custom field on User model) |
| `@authenticated` flow guard | `@login_required` decorator |
| `@role: admin` page guard | `@_admin_required` custom decorator |
| `session_duration: 30d` | `SESSION_COOKIE_AGE = 2592000` |

Google OAuth flow: `auth/google/start` → redirect to Google → `auth/google/callback`
→ `get_or_create` user → `auth.login()`.

---

## 6. Data / Models

Benmore uses its own managed Postgres. For local dev with SQLite:

```bash
# Pull prod schema + data via rsync
rsync -avz root@<ip>:/opt/benmore/apps/<app>/data/ ./data/

# Import with management command or direct sqlite3
python manage.py import_prod_data
```

Benmore stores users in `_benmore_users` (not Django's `auth_user`). Options:
- Map `_benmore_users` to a custom `AUTH_USER_MODEL`
- Import rows into the custom user table with a management command

Column order matters on `INSERT INTO X SELECT * FROM prod.X` — always use explicit column lists.

---

## 7. Static Files

Benmore serves static assets from a CDN automatically. In Django:

1. Add `whitenoise` to `MIDDLEWARE` (right after `SecurityMiddleware`)
2. Set `STATIC_ROOT = BASE_DIR / "staticfiles"` and run `collectstatic`
3. On Heroku: `whitenoise` serves from `staticfiles/` with no extra config

```python
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
```

---

## 8. Gotchas & Lessons Learned

| Issue | Root Cause | Fix |
|---|---|---|
| `position: sticky` broken | `overflow-x: hidden` on `<body>` creates a scroll container | Use `overflow-x: clip` instead |
| `\|timesince` crashes | Raw cursor returns datetime as ISO string, not `datetime` object | Coerce with `datetime.fromisoformat()` in `_run_prod_queries` |
| `{% extends %}` must be first | Django rejects any content (even `{# comments #}`) before `{% extends %}` | Keep layout files to one line |
| Widget blocked in iframes | Default `X-Frame-Options: DENY` | Add `@xframe_options_exempt` to the widget view |
| CSRF double-field | Keeping `<input name="csrf_token">` wrapper around `{{csrf_token}}` | Replace entire `<input>` with standalone `{% csrf_token %}` |
| `LIKE '{{param_q}}%'` breaks with `%s` | `%` is a Python format placeholder | Stick to literal substitution for Benmore-sourced SQL |
| Orphan SQL fragments in templates | `<query>` strip regex stops at first `>` inside multi-line SQL | Use attribute-aware regex + orphan-line cleanup pass |
| Loop-var over-prefixing | `{% if bareword %}` → `{% if item.bareword %}` sweep also hits top-level context vars | Revert prod-query alias names to top-level; only prefix actual loop item fields |

---

## 9. Heroku Deployment

### Prerequisites

```bash
heroku login
heroku create <app-name>
heroku addons:create heroku-postgresql:essential-0
```

### Config vars

```bash
heroku config:set \
  DJANGO_SECRET_KEY="..." \
  DJANGO_DEBUG="0" \
  DJANGO_ALLOWED_HOSTS="<app-name>.herokuapp.com" \
  DJANGO_CSRF_TRUSTED_ORIGINS="https://<app-name>.herokuapp.com" \
  GOOGLE_PLACES_KEY="..." \
  GOOGLE_OAUTH_CLIENT_ID="..." \
  GOOGLE_OAUTH_CLIENT_SECRET="..." \
  STRIPE_SECRET_KEY="..." \
  STRIPE_PRICE_ID="..." \
  STRIPE_WEBHOOK_SECRET="..." \
  TWILIO_ACCOUNT_SID="..." \
  TWILIO_AUTH_TOKEN="..." \
  TWILIO_FROM="..." \
  SITE_URL="https://<app-name>.herokuapp.com" \
  DEFAULT_FROM_EMAIL="no-reply@byondrx.com"
```

### Deploy

```bash
# Procfile (already present)
# web: gunicorn config.wsgi --log-file -
# release: python manage.py migrate --noinput

git push heroku v6-actual-prod:main
heroku run python manage.py seed_demo   # optional demo data
heroku open
```

### Custom domain

```bash
heroku domains:add pharmacypulse.byondrx.com
# Add CNAME in DNS: pharmacypulse.byondrx.com → <app-name>.herokuapp.com
heroku config:set \
  DJANGO_ALLOWED_HOSTS="pharmacypulse.byondrx.com,<app-name>.herokuapp.com" \
  DJANGO_CSRF_TRUSTED_ORIGINS="https://pharmacypulse.byondrx.com"
```

---

## 10. File Checklist

| File | Purpose |
|---|---|
| `Procfile` | `web: gunicorn config.wsgi --log-file -` + `release: migrate` |
| `runtime.txt` | `python-3.11.9` |
| `requirements.txt` | Django, gunicorn, whitenoise, dj-database-url, psycopg[binary] |
| `config/settings.py` | `DATABASE_URL` env var → `dj_database_url.parse()`; `STATIC_ROOT`; `ALLOWED_HOSTS` from env |
| `.env` | Local-only secrets (gitignored); Heroku uses `heroku config:set` |
| `pharmacypulse/_prod_queries.py` | Auto-generated SQL catalog — commit this |
| `scripts/convert_templates.py` | Re-run when pulling updated prod source |
