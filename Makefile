.PHONY: libs install install-dev format lint check-style check test run migrate makemigrations

libs:
	. .venv/bin/activate && pip list

install:
	. .venv/bin/activate && pip install -r requirements.txt

install-dev:
	. .venv/bin/activate && pip install -r requirements-dev.txt

format:
	. .venv/bin/activate && ruff check . --fix
	. .venv/bin/activate && black .

lint:
	. .venv/bin/activate && ruff check .

check-style:
	. .venv/bin/activate && ruff check .
	. .venv/bin/activate && black --check .

check:
	. .venv/bin/activate && python manage.py check

test:
	. .venv/bin/activate && python manage.py test marketplace.tests -v 1

run:
	. .venv/bin/activate && python manage.py runserver 127.0.0.1:8000

migrate:
	. .venv/bin/activate && python manage.py migrate

makemigrations:
	. .venv/bin/activate && python manage.py makemigrations
