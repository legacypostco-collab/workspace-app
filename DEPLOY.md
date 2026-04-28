# Deploy Guide

Production checklist for Consolidator Parts.

## Architecture

```
[Browser] ─HTTPS─→ [Nginx] ─→ [Daphne (ASGI)]   ← HTTP + WebSocket
                              [Celery worker]    ← background tasks
                              [Celery beat]      ← scheduled tasks (SLA, digests)
                              [Postgres]         ← primary DB
                              [Redis]            ← Celery broker + Channels layer + cache
```

## 1. Required environment variables

```bash
# Core
SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')"
DEBUG_MODE=False
ALLOWED_HOSTS="consolidator.parts,www.consolidator.parts"
CSRF_TRUSTED_ORIGINS="https://consolidator.parts,https://www.consolidator.parts"

# HTTPS (after TLS cert is in place)
USE_HTTPS=True
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_HSTS_SECONDS=31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS=True
SECURE_HSTS_PRELOAD=True

# Database (Postgres recommended for production)
DATABASE_URL="postgres://consolidator:STRONG_PASS@127.0.0.1:5432/consolidator"
DB_SSL_REQUIRE=False  # True if managed Postgres requires SSL
DB_CONN_MAX_AGE=60

# Celery + Channels (Redis)
CELERY_BROKER_URL="redis://127.0.0.1:6379/0"
CELERY_RESULT_BACKEND="redis://127.0.0.1:6379/0"
CHANNELS_REDIS_URL="redis://127.0.0.1:6379/1"

# Email (Mailgun example)
EMAIL_HOST=smtp.mailgun.org
EMAIL_PORT=587
EMAIL_HOST_USER=postmaster@mg.consolidator.parts
EMAIL_HOST_PASSWORD=<mailgun-smtp-password>
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL="Consolidator Parts <noreply@consolidator.parts>"
ADMINS="Admin:admin@consolidator.parts"

# Payments (optional — defaults to stub)
PAYMENT_PROVIDER=yookassa  # or stripe, or stub
YOOKASSA_SHOP_ID=...
YOOKASSA_SECRET_KEY=...

# Sentry (optional)
SENTRY_DSN=https://xxx@o123.ingest.sentry.io/456
SENTRY_ENV=production
SENTRY_TRACES_SAMPLE_RATE=0.1
```

## 2. Server setup (Ubuntu)

**System packages:**
```bash
sudo apt update && sudo apt install -y \
    python3 python3-venv python3-pip nginx \
    postgresql postgresql-contrib redis-server \
    certbot python3-certbot-nginx gettext supervisor
```

**Database init:**
```bash
sudo -u postgres psql <<SQL
CREATE DATABASE consolidator;
CREATE USER consolidator WITH ENCRYPTED PASSWORD 'STRONG_PASS';
GRANT ALL PRIVILEGES ON DATABASE consolidator TO consolidator;
ALTER DATABASE consolidator OWNER TO consolidator;
SQL
```

**App install:**
```bash
sudo mkdir -p /srv/consolidator && sudo chown $USER /srv/consolidator
cd /srv/consolidator
git clone git@github.com:legacypostco-collab/workspace-app.git .
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# create /srv/consolidator/.env with the env vars from section 1
python3 manage.py migrate
python3 manage.py collectstatic --noinput
python3 manage.py compilemessages
python3 manage.py createsuperuser
```

## 3. Systemd units

**`/etc/systemd/system/consolidator.service`** (Daphne — ASGI for HTTP+WebSocket):
```ini
[Unit]
Description=Consolidator Parts (Daphne ASGI)
After=network.target postgresql.service redis-server.service

[Service]
User=www-data
WorkingDirectory=/srv/consolidator
EnvironmentFile=/srv/consolidator/.env
ExecStart=/srv/consolidator/.venv/bin/daphne -b 127.0.0.1 -p 8001 consolidator_site.asgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/consolidator-celery.service`** (worker):
```ini
[Unit]
Description=Consolidator Celery Worker
After=network.target redis-server.service

[Service]
User=www-data
WorkingDirectory=/srv/consolidator
EnvironmentFile=/srv/consolidator/.env
ExecStart=/srv/consolidator/.venv/bin/celery -A consolidator_site worker -l info --concurrency=4
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/consolidator-celery-beat.service`** (scheduler):
```ini
[Unit]
Description=Consolidator Celery Beat Scheduler
After=network.target redis-server.service

[Service]
User=www-data
WorkingDirectory=/srv/consolidator
EnvironmentFile=/srv/consolidator/.env
ExecStart=/srv/consolidator/.venv/bin/celery -A consolidator_site beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Enable + start:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now consolidator consolidator-celery consolidator-celery-beat
sudo systemctl status consolidator
```

## 4. Nginx config (`/etc/nginx/sites-enabled/consolidator`)

```nginx
upstream consolidator_app { server 127.0.0.1:8001; }

server {
  listen 443 ssl http2;
  server_name consolidator.parts www.consolidator.parts;
  ssl_certificate /etc/letsencrypt/live/consolidator.parts/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/consolidator.parts/privkey.pem;
  client_max_body_size 20M;

  location /static/ { alias /srv/consolidator/staticfiles/; expires 30d; access_log off; }
  location /media/  { alias /srv/consolidator/media/; expires 7d; access_log off; }

  # WebSocket upgrade for /ws/...
  location /ws/ {
    proxy_pass http://consolidator_app;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_read_timeout 86400;
  }

  location / {
    proxy_pass http://consolidator_app;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
  }
}

server { listen 80; server_name consolidator.parts; return 301 https://$host$request_uri; }
```

```bash
sudo certbot --nginx -d consolidator.parts -d www.consolidator.parts
sudo nginx -t && sudo systemctl reload nginx
```

## 5. CI/CD via GitHub Actions

`.github/workflows/test.yml` runs on every PR:
- Postgres + Redis services
- `manage.py check`, `migrate`, `compilemessages`, `collectstatic`
- pytest with coverage
- ruff lint
- `manage.py check --deploy` on push to main

`.github/workflows/deploy.yml` runs on push to main (or manual dispatch):
- Rsyncs code to server via SSH
- Runs migrations, collectstatic, compilemessages remotely
- Restarts systemd services
- Smoke-tests `/login/` over HTTPS
- Notifies Sentry of release

**Required GitHub secrets:**
- `DEPLOY_SSH_KEY` — private key for SSH user
- `SENTRY_AUTH_TOKEN` — for release tracking (optional)

**Required GitHub variables:**
- `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH`, `DEPLOY_DOMAIN`, `DEPLOY_URL`
- `SENTRY_ORG`, `SENTRY_PROJECT` (optional)

## 6. Migrating from SQLite → Postgres

If you're already running on SQLite:
```bash
# 1. Dump SQLite data
python3 manage.py dumpdata --natural-foreign --natural-primary \
    -e contenttypes -e auth.Permission -e admin.LogEntry \
    --indent 2 > dump.json

# 2. Set DATABASE_URL=postgres://... in .env
# 3. Run migrations on empty Postgres
python3 manage.py migrate

# 4. Load data
python3 manage.py loaddata dump.json
```

## 7. Post-deploy verification

- [ ] `curl -I https://consolidator.parts/` returns 200
- [ ] Registration → email verification → login works (real SMTP)
- [ ] Password reset email arrives
- [ ] Switch language EN → ZH → RU on cabinet pages
- [ ] Demo accounts work (`demo_seller`, `demo_buyer`, `demo_operator`)
- [ ] WebSocket connects: open notification dropdown, check browser DevTools Network → WS
- [ ] Trigger a notification (e.g., create new RFQ as buyer) → seller sees badge update **without refreshing**
- [ ] Celery beat schedule runs: `celery -A consolidator_site inspect scheduled`
- [ ] `/api/docs/` Swagger UI loads
- [ ] 404/500 pages render branded
- [ ] Mobile: open on phone, hamburger menu opens sidebar

## 8. Monitoring

- **Sentry** — set `SENTRY_DSN` env, errors auto-sent
- **UptimeRobot** or **Better Uptime** — ping `/login/` every 5 min
- **Postgres backup** — `pg_dump consolidator | gzip > /backup/$(date +%F).sql.gz` daily via cron, retain 30 days
- **Logs** — `journalctl -u consolidator -f` (Daphne) and `journalctl -u consolidator-celery -f` (worker)
- **Flower** (optional) — Celery monitoring UI: `celery -A consolidator_site flower --port=5555`

## 9. AI Assistant (Phase 2)

The `assistant` Django app provides RAG-based chat for buyers/sellers/operators.

### Required env vars
```bash
ANTHROPIC_API_KEY=sk-ant-...               # required for LLM responses
ANTHROPIC_MODEL=claude-sonnet-4-20250514   # optional override
OPENAI_API_KEY=sk-...                      # for embeddings (recommended)
# OR
VOYAGE_API_KEY=...                         # alternative embedding provider
EMBEDDING_PROVIDER=auto                    # openai | voyage | stub | auto
```

Without `ANTHROPIC_API_KEY` the assistant runs in stub mode (returns matched chunks
without LLM synthesis). Without `OPENAI_API_KEY`/`VOYAGE_API_KEY`, embeddings use
deterministic hash-based fallback (works for keyword-overlap matching only).

### Postgres + pgvector setup
The assistant uses pgvector for fast cosine similarity search:
```sql
CREATE EXTENSION IF NOT EXISTS vector;
```
The migration `assistant/0002_pgvector_setup.py` runs this automatically on Postgres.
On SQLite it's a no-op and search falls back to in-Python cosine.

### Initial indexing
After deploy, index existing data:
```bash
python3 manage.py reindex_all
# Or per-source:
python3 manage.py index_catalog --batch-size 200
```

Auto-indexing on Part/Order/RFQ save is wired via Django signals → Celery tasks.

### WebSocket endpoint
`ws://host/ws/assistant/[<conversation_id>/]` — session-auth required, streams
RAG responses token-by-token. The chat widget (`_assistant_widget.html`) is
included in all 4 cabinet bases and connects automatically.
