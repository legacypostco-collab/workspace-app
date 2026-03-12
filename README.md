# hybrid-marketplace

Hybrid platform based on two codebases:
- fast MVP workflow from `django-marketplace`
- operational hardening patterns inspired by `PnPartsPublic`

## Stack
- Python 3.13
- Django 5.1
- Django REST Framework
- SQLite (local dev)

## Project layout
- `consolidator_site/` Django settings and root URLs
- `marketplace/` domain app (models, views, forms, API, commands)
- `templates/marketplace/` HTML templates
- `static/marketplace/` static assets (styles, logos)

## Hybrid additions
- `GET /api/v1/health/` simple liveness probe
- `GET /api/v1/readiness/` DB readiness probe
- `GET /api/v1/analytics/hybrid/?days=30` role-aware operational analytics
- `GET /api/v1/analytics/funnel/?days=30` conversion funnel (RFQ → Order → Delivery/Claims)
- `python manage.py check_deploy_readiness` production readiness checks
- safer CSV import matching by OEM key in `marketplace/services/imports.py`
- production env template in `.env.production.example`

## Quick start
```bash
cd "/Users/konastantinverveyn/Documents/Проект/hybrid_marketplace"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Open: `http://127.0.0.1:8000`

## Useful commands
```bash
make check
make test
make run
make migrate
python manage.py check_deploy_readiness --allow-no-tls
```

## Code style
- Config in `pyproject.toml`
- Tools: `ruff`, `black` (from `requirements-dev.txt`)

Install and run:
```bash
make install-dev
make format
make check-style
```

## Git hygiene
Ignored by default:
- local DB files
- virtual environments
- media files
- IDE/temp files

See `.gitignore`.
