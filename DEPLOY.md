# Deploy Guide

Production checklist for Consolidator Parts.

## 1. Required environment variables

```bash
# Core
SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')"
DEBUG_MODE=False
ALLOWED_HOSTS="consolidator.parts,www.consolidator.parts"
CSRF_TRUSTED_ORIGINS="https://consolidator.parts,https://www.consolidator.parts"

# HTTPS (set after TLS cert is in place)
USE_HTTPS=True
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_HSTS_SECONDS=31536000              # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS=True
SECURE_HSTS_PRELOAD=True

# Email (Mailgun example — also works with SendGrid, SES, Yandex)
EMAIL_HOST=smtp.mailgun.org
EMAIL_PORT=587
EMAIL_HOST_USER=postmaster@mg.consolidator.parts
EMAIL_HOST_PASSWORD=<mailgun-smtp-password>
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL="Consolidator Parts <noreply@consolidator.parts>"
ADMINS="Admin:admin@consolidator.parts"

# Database (when migrating off SQLite)
# DATABASE_URL=postgres://user:pass@db:5432/consolidator
```

## 2. Pre-deploy checks

```bash
# Verify settings & security
python3 manage.py check --deploy

# Generate static files
python3 manage.py collectstatic --noinput

# Apply migrations
python3 manage.py migrate

# Compile translations (en + zh-hans)
python3 manage.py compilemessages

# Smoke test
curl -I https://consolidator.parts/
```

## 3. Server setup (Timeweb VPS / generic)

**Install:**
```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip nginx postgresql certbot python3-certbot-nginx gettext
```

**Run with systemd + gunicorn:**
```ini
# /etc/systemd/system/consolidator.service
[Unit]
Description=Consolidator Parts
After=network.target

[Service]
User=www-data
WorkingDirectory=/srv/consolidator
EnvironmentFile=/srv/consolidator/.env
ExecStart=/srv/consolidator/.venv/bin/gunicorn consolidator_site.wsgi:application \
    --bind 127.0.0.1:8001 --workers 4 --timeout 60
Restart=always

[Install]
WantedBy=multi-user.target
```

**Nginx (TLS via Certbot):**
```nginx
server {
  listen 443 ssl http2;
  server_name consolidator.parts;
  ssl_certificate /etc/letsencrypt/live/consolidator.parts/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/consolidator.parts/privkey.pem;
  client_max_body_size 20M;

  location /static/ { alias /srv/consolidator/staticfiles/; expires 30d; }
  location /media/  { alias /srv/consolidator/media/; expires 7d; }
  location / { proxy_pass http://127.0.0.1:8001; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto https; }
}
server { listen 80; server_name consolidator.parts; return 301 https://$host$request_uri; }
```

## 4. Post-deploy

- [ ] Verify `/` loads on HTTPS
- [ ] Test registration → email verification → login
- [ ] Test password reset
- [ ] Switch language EN → ZH on cabinet pages
- [ ] Test demo accounts work (`demo_seller`, `demo_buyer`, `demo_operator`)
- [ ] Hit `/jsi18n/` returns valid JS catalog
- [ ] 404/500 pages render branded
- [ ] Mobile: open on phone, hamburger menu opens sidebar

## 5. Monitoring (recommended)

- **Sentry** for error tracking — `pip install sentry-sdk`, set `SENTRY_DSN` env
- **UptimeRobot** or **Better Uptime** — ping `/` every 5 min
- **Cron backup** — `pg_dump` daily, retain 30 days
