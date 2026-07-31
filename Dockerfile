FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential curl \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV DJANGO_SETTINGS_MODULE=consolidator_site.settings

EXPOSE 8001

# Быстрый старт (docker compose):
#   cp .env.example .env          # заполнить SECRET_KEY + ANTHROPIC_API_KEY
#   docker compose up --build
#   # Первый запуск — миграции + суперпользователь:
#   docker compose exec web python manage.py migrate
#   docker compose exec web python manage.py createsuperuser
#   # Сайт: http://localhost:8001

# Daphne — ASGI-сервер с поддержкой WebSocket (Django Channels).
CMD ["bash","-lc","python manage.py migrate --noinput && python manage.py create_admin --if-configured && python manage.py collectstatic --noinput --clear && daphne -b 0.0.0.0 -p 8001 consolidator_site.asgi:application"]
