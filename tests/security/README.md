# Security Testing

## OWASP ZAP

### Baseline scan (passive, safe for prod)
```bash
bash tests/security/zap_baseline.sh https://beta.your-domain.tld
# → HTML+JSON report в tests/security/zap-reports/zap-baseline-<ts>.{html,json}
```

### Full active scan (DESTRUCTIVE — только на staging!)
```bash
docker run --rm \
  -v $(pwd)/tests/security/zap-reports:/zap/wrk:rw \
  ghcr.io/zaproxy/zaproxy:stable \
  zap-full-scan.py -t https://staging.your-domain -r full.html
```

Active scan стреляет XSS / SQLi / path traversal payload'ами по всем найденным
формам и URL-параметрам. Может создать тестовые данные в БД — НЕ запускать
на prod-БД с реальными пользователями.

## Python-уровневые тесты (pytest)

```bash
pytest tests/test_security_owasp.py -v   # IDOR, XSS, SQLi, CSRF
pytest tests/test_auth_security.py -v    # rate-limits, GDPR, legacy redirects
```

## Что проверяется автоматически

| Категория | Файл | Покрытие |
|---|---|---|
| Broken Access Control (A01) | test_security_owasp.py | IDOR на create_claim, op_order_detail, admin_panel |
| Injection (A03) | test_security_owasp.py | SQLi в search_parts, XSS в claim title и KYB legal_name |
| Security Misconfiguration (A05) | test_security_owasp.py | CSRF на mutations, X-Frame-Options, MIME sniffing |
| Auth flows (A07) | test_auth_security.py | Rate-limits на login/register/password_reset, demo backdoor closed, legacy redirect |
| Data exposure | test_auth_security.py | GDPR export login_required, soft-delete anonymizes PII |
