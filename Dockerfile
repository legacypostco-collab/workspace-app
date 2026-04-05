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

EXPOSE 8000

CMD ["bash","-lc","python manage.py migrate && python manage.py collectstatic --noinput && python manage.py create_admin && gunicorn consolidator_site.wsgi:application --bind 0.0.0.0:8000 --workers ${WEB_CONCURRENCY:-3} --timeout ${GUNICORN_TIMEOUT:-60}"]
