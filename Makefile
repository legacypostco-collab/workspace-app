.PHONY: libs install install-dev format lint check-style

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
