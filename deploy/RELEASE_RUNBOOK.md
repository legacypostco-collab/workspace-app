# Release Runbook — Consolidator Parts

Полный сценарий «от закрытого кода до прод-трафика», с откатом и реакцией на инциденты.

---

## 0. Pre-flight (T-1 день)

### Сервер готов?
- [ ] Ubuntu 22.04+ / Debian 12 на `72.56.234.89`
- [ ] `sudo ufw`: открыты 22, 80, 443; всё остальное закрыто
- [ ] DNS: `consolidator.parts` и `www.consolidator.parts` → IP сервера
- [ ] User `deploy` существует, в `sudo` группе, SSH-ключ загружен

### Софт установлен?
```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv postgresql-15 nginx certbot \
                    python3-certbot-nginx libnginx-mod-brotli redis-server git
```

### Postgres настроен?
```bash
sudo -u postgres psql <<SQL
CREATE USER consolidator WITH PASSWORD 'СГЕНЕРИРУЙ_ПАРОЛЬ';
CREATE DATABASE consolidator OWNER consolidator;
GRANT ALL PRIVILEGES ON DATABASE consolidator TO consolidator;
SQL
```

### Код выкатан?
```bash
sudo mkdir -p /var/www/workspace-app
sudo chown deploy:deploy /var/www/workspace-app
git clone https://github.com/YOUR_ORG/workspace-app.git /var/www/workspace-app
cd /var/www/workspace-app
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
```

### .env создан?
```bash
cp deploy/env.example .env
# отредактируй ВСЕ __CHANGE_ME__ значения
chmod 600 .env
```

**Особо критично:**
- [ ] `SECRET_KEY` — сгенерируй заново: `python -c "import secrets;print(secrets.token_urlsafe(60))"`
- [ ] `PAYMENT_CALLBACK_SECRET` — random 64+ chars (P0-2: без него callback вернёт 503)
- [ ] `STRIPE_WEBHOOK_SECRET` — из Stripe Dashboard → Webhooks
- [ ] `DEBUG=False`
- [ ] `ALLOWED_HOSTS=consolidator.parts,www.consolidator.parts`

### HTTPS-сертификат?
```bash
sudo certbot --nginx -d consolidator.parts -d www.consolidator.parts \
             --non-interactive --agree-tos -m admin@consolidator.parts
```

### Бэкап?
- [ ] Cron `0 3 * * * pg_dump consolidator | gzip > /backup/consolidator-$(date +\%F).sql.gz`
- [ ] /backup mounted на отдельный диск или S3-bucket
- [ ] Тест восстановления делал? (попробуй на staging)

---

## 1. Деплой (T-0)

### Команда (с локальной машины)
```bash
ssh deploy@72.56.234.89 'cd /var/www/workspace-app && bash deploy/deploy.sh'
```

Скрипт сделает:
1. Pre-flight (валидация .env)
2. `git pull --ff-only`
3. `pip install -r requirements.txt` (только если изменился)
4. `migrate --plan` → ревью → `migrate --noinput`
5. `collectstatic --clear`
6. `compilemessages`
7. systemd: `daphne.service` + `daphne.socket`
8. nginx config + reload
9. Health-checks: `/`, `/robots.txt`, `/sitemap.xml` + CSP header

### Что должно появиться в выводе
```
✓ env validated
✓ collectstatic ... static files copied
✓ / → 200
✓ /robots.txt → 200
✓ /sitemap.xml → 200
✓ CSP present
✓ Deploy complete — <commit-sha> live
```

### Если что-то упало
```bash
# Сразу откат
ssh deploy@72.56.234.89 'cd /var/www/workspace-app && bash deploy/deploy.sh --rollback'
# Логи
journalctl -u daphne -n 100 --no-pager
tail -100 /var/log/nginx/consolidator-error.log
```

---

## 2. Smoke на проде (T+5 минут)

**6 P0-сценариев — каждый ≤ 2 мин, ручной запуск через браузер на проде.**

### S1 — Buyer покупает (escrow hold)
1. Открыть https://consolidator.parts → login `demo_buyer`/`demo12345`
2. Pill «🛒 Найти запчасть» → ввести `2W1223` → click первой карточки
3. Кнопка «Купить» → должна показаться **preview-карточка** (P0-7)
4. Кнопка «✓ Подтвердить заказ» → создан заказ, статус `awaiting_reserve`
5. Pill «📦 Мои заказы» → найти заказ → «💳 Оплатить резерв» → preview → «Списать $X»
6. **Открыть БД:** `SELECT * FROM assistant_wallettx ORDER BY created_at DESC LIMIT 5;` — должна быть **одна** запись `escrow_hold` на эту сумму

### S2 — Double-click защита (P0-5)
1. Тот же экран pay_reserve, **в двух вкладках** одновременно нажать «Списать»
2. Один из запросов должен вернуть «Резерв уже списан»
3. БД: `escrow_hold` всё равно один (не два!)

### S3 — Role-escalation 403 (P0-1)
1. Открыть DevTools → Console:
```js
fetch('/api/assistant/role/', {
  method:'POST',
  headers:{'Content-Type':'application/json','X-CSRFToken': document.cookie.match(/csrftoken=([^;]+)/)[1]},
  body: JSON.stringify({role:'operator'}),
  credentials:'same-origin'
}).then(r=>r.json()).then(console.log)
```
2. Должен вернуться `{"error": "forbidden: ...", "role": "buyer"}` со статусом 403

### S4 — Seller KYB → approve
1. Login `demo_seller`/`demo12345` → должно открыться окно KYB
2. Пройти 4 шага: company / legal_address / bank / director
3. «Submit for review»
4. Open `demo_operator` в incognito → pill «🛡 KYB» → найти заявку → «Approve»
5. Вернуться в seller-сессию → reload → kyb_status должен показать «✓ Верифицирован»

### S5 — Confirm delivery (P0-7)
1. На том же seller-аккаунте взять order в статусе `delivered` (или продвинуть тестовый через operator)
2. На buyer-аккаунте: pill «📦 Мои заказы» → выбрать → кнопка «Подтвердить приёмку»
3. Должен показаться **preview** «Подтвердить приёмку заказа #N?» (P0-7)
4. «✓ Подтверждаю» → эскроу высвободился продавцу

### S6 — Claim refund
1. Buyer на завершённом заказе → «Открыть рекламацию» → заполнить → submit
2. Operator → pill «🧾 Рекламации» → найти → «В работу» → «Approve» → «Settlement $X»
3. Buyer: pill «💰 Депозит» — баланс вырос на `$X`

**Если хоть один из 6 не сработал — НЕ открывать публичный доступ. Откат + разбор.**

---

## 3. Открыть трафик (T+30 минут)

### Если staging-этап был → canary
- nginx upstream: `server 127.0.0.1:8001 weight=1; server old:80 weight=9;`
- 1 час мониторинга → если ok → flip к `weight=10`

### Если сразу прод → постепенный анонс
- Не делать массовую рассылку в первый час
- Соцсети / email-кампания — через 24 часа

---

## 4. Мониторинг (первые 72 часа)

### Что смотреть каждые 30 мин
- `journalctl -u daphne -p err -n 50 --no-pager` — должно быть пусто
- `tail -50 /var/log/nginx/consolidator-error.log` — 5xx ≤ 0
- В чате `demo_operator`: pill «💰 Финансы и эскроу» → «Сверка баланс vs холды» = ✓
- В чате `demo_operator`: pill «⏱ SLA» → SLA здоровье > 90%

### Алёрты (минимум на день первый)
- UptimeRobot на `/` каждые 5 минут → Telegram
- `grep "5[0-9][0-9]" /var/log/nginx/consolidator-access.log | tail -20` — раз в час
- Sentry (если успели подключить) — Performance + Errors

### Финансовые инварианты (cron каждый час)
```bash
# /etc/cron.hourly/wallet-recon.sh
psql -U consolidator -d consolidator -c "
SELECT
  (SELECT SUM(balance) FROM assistant_wallet WHERE user_id IS NULL) AS platform_balance,
  (SELECT
     COALESCE(SUM(CASE WHEN kind='escrow_hold' AND wallet_id=(SELECT id FROM assistant_wallet WHERE user_id IS NULL) THEN amount
                       WHEN kind IN ('escrow_release','escrow_refund') AND wallet_id=(SELECT id FROM assistant_wallet WHERE user_id IS NULL) THEN -amount
                       ELSE 0 END), 0)
   FROM assistant_wallettx) AS computed_balance;
"
# Если platform_balance != computed_balance → ALERT
```

---

## 5. Incident response

### Симптом: 5xx на лендинге
```bash
journalctl -u daphne -n 100 --no-pager
# если import error / migration — откат:
bash deploy/deploy.sh --rollback
```

### Симптом: «двойное списание» жалоба
1. Остановить trafic: `systemctl stop nginx`
2. Проверить: `SELECT order_id, kind, COUNT(*) FROM assistant_wallettx WHERE created_at > NOW() - INTERVAL '1 hour' GROUP BY order_id, kind HAVING COUNT(*) > 1;`
3. Если есть дубли — это P0-5/P0-6 регрессия. **Откат немедленно.**
4. Ручной refund через operator-инструмент после анализа

### Симптом: AI отвечает чушь / медленно
- `ANTHROPIC_API_KEY` валиден? `curl -H "x-api-key: $ANTHROPIC_API_KEY" https://api.anthropic.com/v1/messages -X POST ...`
- Лимит превышен? Anthropic Dashboard
- Fallback: убрать API-key из .env — `_stub_with_action` возьмёт на себя read-only ответы (см. `rag.py:553`)

### Симптом: WS не подключается
- `nginx -t` — конфиг ok?
- `journalctl -u daphne | grep -i websocket`
- Проверь nginx.conf: `proxy_set_header Upgrade $http_upgrade` в `/ws/`

### Симптом: payment_callback не работает
- Проверь `.env`: `PAYMENT_CALLBACK_SECRET` есть и совпадает с тем, что в Stripe Dashboard
- Лог: `grep payment_callback /var/log/nginx/consolidator-access.log | tail -10`

---

## 6. Что отложено на «спринт после релиза»

Из pre-prod аудита, не блокеры:

| Тема | Файл | Приоритет |
|---|---|---|
| `add_product`/`edit_product` без confirmed-gate | `assistant/seller_actions.py:2344,1464` | P1 |
| `create_rfq` auto-dispatch без подтверждения | `assistant/actions.py:1878` | P1 |
| RAG-инъекция через имена поставщиков (нет XML-разделителей) | `assistant/prompts.py:163` | P1 |
| Утечка % наценки в seller-ответе AI | `assistant/seller_actions.py:1066` | P2 |
| Nonce-based CSP (убрать `unsafe-inline`) | `consolidator_site/middleware.py` | P2 |
| Wallet reconciliation cron (Issue 8 от data-integrity) | новый management command | P1 |
| `_log_event` в `op_resolve_dispute` | `operator_actions.py:1501` | P2 |
| 6 P0 e2e payment-тестов | новые в `e2e/tests/` | P1 |
| База64 → WebP, gzip/brotli уже есть в nginx.conf | `templates/landing.html` | P2 |
| Sentry / Plausible | новый | P1 |

---

## 7. Контакты / эскалация

- **Dev on-call:** ___
- **Ops on-call:** ___
- **Anthropic support:** support@anthropic.com (если AI лежит)
- **Stripe support:** Dashboard → Support
- **Hosting (Timeweb):** support@timeweb.com / +7 ___

---

**Last update:** 2026-05-21 (после audit-цикла 6 агентов)
