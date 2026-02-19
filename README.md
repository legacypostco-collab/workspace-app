# django-marketplace

Django MVP for Consolidator Parts marketplace.

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

## Quick start
```bash
cd "/Users/konastantinverveyn/Documents/Проект/django_marketplace"
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
