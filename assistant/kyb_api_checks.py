"""KYB checks with explicit, test-only external-source fixtures.

The project does not currently implement live calls to Контур.Фокус,
OpenCorporates, VIES, OpenSanctions or map providers. Production therefore
fails closed and routes every application to manual review. Deterministic
fixtures are available only when ``KYB_ALLOW_TEST_FIXTURES`` is explicitly
enabled by a test or a development seed command.

The dict shape is stable: every result has `{ok, fetched_at, source, data, signals}`
where:
  - `ok`: bool — whether the API call itself succeeded
  - `fetched_at`: ISO timestamp
  - `source`: name + version
  - `data`: raw payload (snapshot for audit)
  - `signals`: derived flags consumed by `kyb_workflow.evaluate_risk()`:
       red / yellow / green markers, with reasons.

────────────────────────────────────────────────────────────────────
PRODUCTION SWAP POINTS — each ``check_*`` function is isolated so a live
provider can replace the fixture without changing ``run_all_checks()``.
Until that implementation exists, no API key is advertised or interpreted
as enabling a provider.
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

# ──────────────────────────────────────────────────────────────────────
# Deterministic fixture lookup — every test company is identified by its
# (country, registration_number). The fixtures cover the 3 paths from
# ТЗ §5 «Автоматические решения системы»:
#   GREEN → быстро в Песочницу
#   YELLOW → ручная проверка
#   RED    → автоотказ
# Unknown identifiers never receive synthesized company data or a green
# result: they are marked unavailable and require manual review.
# ──────────────────────────────────────────────────────────────────────

_FIXTURES: dict[tuple[str, str], dict[str, Any]] = {
    # ── ПУТЬ A: Чистая российская компания, зелёный риск ──
    ("RU", "7708123456"): {
        "aggregator": {
            "status": "active", "registered_at": "2014-03-12",
            "ogrn": "1027700001234", "kpp": "770801001",
            "directors": [{"name": "Иванов Иван Иванович", "since": "2014-03-12"}],
            "founders": [{"name": "Иванов Иван Иванович", "share": 100}],
            "legal_address": "г. Москва, ул. Промышленная, д. 17, оф. 401",
            "okved": ["45.31.1 Торговля автозапчастями"],
            "egrul_unreliable": False,
            "mass_director_flag": False,
            "mass_address_flag": False,
            "tax_debt": 0,
            "court_cases": 0,
            "risk_indicator": "green",
        },
        "sanctions": {"matches": []},
    },
    # ── ПУТЬ B: Зарубежная компания (UAE) с не подтверждённым VAT —
    # требует уточнения у оператора
    ("AE", "2233445"): {
        "aggregator": None,  # РФ-aggregator не применим для AE
        "opencorporates": {
            "status": "active", "registered_at": "2019-11-04",
            "company_number": "2233445", "company_type": "LLC",
            "name": "Hydrolux Trading FZE",
            "address": "RAKEZ Business Zone, Ras Al Khaimah, UAE",
            "directors": [{"name": "Ahmed Al-Mansoori", "since": "2019-11-04"}],
        },
        "sanctions": {"matches": []},
    },
    # VIES-fixture идёт по ключу (country, digits-only VAT)
    ("AE", "100123456700003"): {
        "vies": {
            "valid": False, "vat_number": "AE100123456700003",
            "name": None,
            "reason": _("VAT не активен в реестре EU VIES (компания вне ЕС, нужна ручная сверка TRN UAE)"),
        },
    },
    # ── ПУТЬ C: Российская компания на грани ликвидации + санкции —
    # автоотказ
    ("RU", "5031000099"): {
        "aggregator": {
            "status": "liquidation", "registered_at": "2007-06-18",
            "ogrn": "1025003755555", "kpp": "503101001",
            "directors": [{"name": "Смирнов Виктор Геннадьевич", "since": "2007-06-18"}],
            "legal_address": "г. Электросталь, ул. Заводская, 3А",
            "okved": ["46.69 Торговля оборудованием"],
            "egrul_unreliable": True,
            "mass_director_flag": True,
            "mass_address_flag": False,
            "tax_debt": 1_240_000,
            "court_cases": 14,
            "risk_indicator": "red",
        },
        "sanctions": {
            "matches": [
                {"list": "OFAC SDN", "match_score": 0.93,
                 "name": "Smirnov, Viktor G.", "type": "individual"},
            ],
        },
    },
}


def _now_iso() -> str:
    return timezone.now().isoformat()


def _wrap(source: str, ok: bool, data: Any, signals: list[dict]) -> dict:
    return {"ok": ok, "fetched_at": _now_iso(), "source": source,
            "data": data, "signals": signals}


def _fixtures_allowed() -> bool:
    return bool(getattr(settings, "KYB_ALLOW_TEST_FIXTURES", False))


def _unavailable(source: str, message: str | None = None) -> dict:
    return _wrap(
        source,
        False,
        None,
        [{
            "level": "yellow",
            "msg": message or _("Внешний источник не настроен; требуется ручная проверка"),
        }],
    )


# ──────────────────────────────────────────────────────────────────────
# §3.1 — Российский агрегатор (Контур.Фокус / СПАРК / CheckCo)
# ──────────────────────────────────────────────────────────────────────

def check_ru_aggregator(inn: str, country: str = "RU") -> dict:
    if country != "RU":
        return _wrap("kontur-focus", False, None,
                     [{"level": "info", "msg": "Not applicable for non-RU"}])
    if not _fixtures_allowed():
        return _unavailable("kontur-focus")
    fx = _FIXTURES.get((country, inn), {}).get("aggregator")
    if fx is None:
        return _unavailable("kontur-focus", _("Компания отсутствует в тестовом наборе; требуется ручная проверка"))

    signals = []
    if fx["status"] == "liquidation":
        signals.append({"level": "red", "msg": _("Компания в стадии ликвидации")})
    if fx["status"] == "bankruptcy":
        signals.append({"level": "red", "msg": _("Компания в стадии банкротства")})
    if fx.get("egrul_unreliable"):
        signals.append({"level": "red", "msg": _("Запись о недостоверности сведений ЕГРЮЛ")})
    if fx.get("mass_director_flag"):
        signals.append({"level": "yellow", "msg": _("Массовый директор")})
    if fx.get("mass_address_flag"):
        signals.append({"level": "yellow", "msg": _("Массовый юридический адрес")})
    if fx.get("tax_debt", 0) > 100_000:
        _tax_debt = f"{fx['tax_debt']:,}"
        signals.append({"level": "yellow", "msg": _("Налоговая недоимка $%(debt)s") % {"debt": _tax_debt}})
    if fx.get("court_cases", 0) > 5:
        signals.append({"level": "yellow", "msg": _("%(count)s судебных дел") % {"count": fx['court_cases']}})
    # Финальный risk-индикатор — берём из API; если есть red signals → red
    has_red = any(s["level"] == "red" for s in signals)
    if has_red:
        fx["risk_indicator"] = "red"
    return _wrap("kontur-focus", True, fx, signals)


# ──────────────────────────────────────────────────────────────────────
# §3.2 — OpenCorporates (для зарубежных)
# ──────────────────────────────────────────────────────────────────────

def check_opencorporates(company_number: str, country: str) -> dict:
    if country == "RU":
        return _wrap("opencorporates", False, None,
                     [{"level": "info", "msg": _("Использовать ru-aggregator для РФ")}])
    if not _fixtures_allowed():
        return _unavailable("opencorporates")
    fx = _FIXTURES.get((country, company_number), {}).get("opencorporates")
    if fx is None:
        return _unavailable("opencorporates", _("Компания отсутствует в тестовом наборе; требуется ручная проверка"))
    signals = []
    if fx["status"] in ("dissolved", "struck_off"):
        signals.append({"level": "red", "msg": _("Компания %(status)s") % {"status": fx['status']}})
    return _wrap("opencorporates", True, fx, signals)


# ──────────────────────────────────────────────────────────────────────
# §3.3 — VIES (для VAT в ЕС)
# ──────────────────────────────────────────────────────────────────────

EU_COUNTRIES = {"AT","BE","BG","CY","CZ","DE","DK","EE","ES","FI","FR","GR",
                 "HR","HU","IE","IT","LT","LU","LV","MT","NL","PL","PT","RO",
                 "SE","SI","SK"}


def check_vies(vat_number: str, country: str) -> dict:
    if not _fixtures_allowed():
        return _unavailable("vies")
    fx = _FIXTURES.get((country, _strip_inn(vat_number) or ""), {}).get("vies")
    if fx is None:
        return _unavailable("vies", _("VAT отсутствует в тестовом наборе; требуется ручная проверка"))
    signals = []
    if not fx["valid"]:
        # Для не-EU стран VIES не применим — это yellow (требует ручной
        # сверки локального tax-ID), не red (не автоотказ).
        level = "red" if country in EU_COUNTRIES else "yellow"
        signals.append({"level": level, "msg": _(fx["reason"]) if fx.get("reason") else _("VAT не подтверждён")})
    return _wrap("vies", True, fx, signals)


def _strip_inn(vat: str) -> str:
    """Strip country prefix and non-digits from VAT to match fixture keys."""
    s = "".join(c for c in (vat or "") if c.isdigit())
    return s


# ──────────────────────────────────────────────────────────────────────
# §3.4 — OpenSanctions (для всех)
# ──────────────────────────────────────────────────────────────────────

def check_sanctions(company_name: str, directors: list[str], country: str = "",
                     fixture_key: tuple[str, str] | None = None) -> dict:
    if not _fixtures_allowed():
        return _unavailable("opensanctions")
    fx = None
    if fixture_key:
        fx = _FIXTURES.get(fixture_key, {}).get("sanctions")
    if fx is None:
        return _unavailable("opensanctions", _("Компания отсутствует в тестовом наборе; требуется ручная проверка"))
    signals = []
    for m in fx.get("matches", []):
        if m.get("match_score", 0) >= 0.85:
            signals.append({"level": "red",
                             "msg": _("Совпадение в %(list)s (%(name)s, score=%(score).2f)") % {
                                 "list": m['list'], "name": m['name'], "score": m['match_score']}})
        else:
            signals.append({"level": "yellow",
                             "msg": _("Возможное совпадение в %(list)s (%(name)s)") % {
                                 "list": m['list'], "name": m['name']}})
    return _wrap("opensanctions", True, fx, signals)


# ──────────────────────────────────────────────────────────────────────
# §3.5 — Yandex / Google Maps (геокодирование склада)
# ──────────────────────────────────────────────────────────────────────

def check_address(address: str, country: str = "RU") -> dict:
    if not _fixtures_allowed():
        return _unavailable("address-check")

    # Test-only local classifier. It does not claim geocoding, reviews,
    # coordinates, Street View availability or provider verification.
    addr_l = (address or "").lower()
    if any(k in addr_l for k in ("ул. промышленная", "industrial", "промзона", "logistics park")):
        kind = "industrial"
    elif any(k in addr_l for k in ("офис", "office", "бизнес-центр", "rakez")):
        kind = "commercial"
    elif any(k in addr_l for k in ("кв.", "apt", "квартира", "жилой")):
        kind = "residential"
    else:
        kind = "commercial"

    data = {
        "found": bool(address),
        "kind": kind,
        "coordinates": None,
        "streetview_url": "",
        "reviews_count": None,
        "rating": None,
        "verification": "test_fixture",
    }
    signals = []
    if not address:
        signals.append({"level": "yellow", "msg": _("Адрес не указан")})
    elif kind == "residential":
        signals.append({"level": "yellow", "msg": _("Адрес склада — жилой дом")})
    return _wrap("fixture-address-classifier", True, data, signals)


# ──────────────────────────────────────────────────────────────────────
# §3.6 — Технические проверки (сайт, email, телефон, мессенджеры)
# ──────────────────────────────────────────────────────────────────────

def check_website(url: str) -> dict:
    """Validate URL shape locally without claiming network reachability."""
    ok = bool(url) and ("://" in url)
    signals = []
    data = {"valid_format": ok, "reachable": None, "url": url, "http_status": None}
    if not ok:
        signals.append({"level": "yellow", "msg": _("Сайт не указан или не валиден")})
    return _wrap("local-url-validation", True, data, signals)


def check_messengers(whatsapp: str, telegram: str, phone: str) -> dict:
    has_any = bool(whatsapp or telegram)
    data = {
        "whatsapp": {"present": bool(whatsapp), "registered": None},
        "telegram": {"present": bool(telegram), "registered": None},
        "phone": {"present": bool(phone), "valid_format": phone.startswith("+") if phone else False},
    }
    signals = []
    if not has_any:
        signals.append({"level": "yellow",
                         "msg": _("Не указан ни один мессенджер для оперативной связи")})
    return _wrap("local-contact-validation", True, data, signals)


# ──────────────────────────────────────────────────────────────────────
# Public façade
# ──────────────────────────────────────────────────────────────────────

def run_all_checks(kyb) -> dict:
    """Выполнить все автопроверки для KYB-заявки. Возвращает полный
    api_results снэпшот + интегральный risk_indicator.

    ТЗ §3: «автомат за 10 секунд делает 5–7 API-запросов».
    """
    if not _fixtures_allowed():
        message = _("Автоматический внешний источник не настроен; требуется ручная проверка")
        sources = {
            "aggregator": "kontur-focus",
            "opencorporates": "opencorporates",
            "vies": "vies",
            "sanctions": "opensanctions",
            "maps": "address-check",
        }
        unavailable = {
            key: _wrap(
                source,
                False,
                None,
                [{"level": "yellow", "msg": message}],
            )
            for key, source in sources.items()
        }
        unavailable["site"] = check_website(kyb.website)
        unavailable["messenger"] = check_messengers(
            kyb.whatsapp,
            kyb.telegram,
            kyb.phone,
        )
        return unavailable

    country = (kyb.country or "RU").upper()
    fixture_key = (country, kyb.inn or kyb.vat_number or "")

    aggregator = check_ru_aggregator(kyb.inn, country)
    opencorp = check_opencorporates(kyb.inn, country)
    vies = check_vies(kyb.vat_number, country) if kyb.vat_number else \
           _wrap("vies", False, None, [{"level": "info", "msg": _("VAT не указан")}])
    director_names = []
    for snap in (aggregator, opencorp):
        if snap.get("ok") and snap.get("data") and isinstance(snap["data"].get("directors"), list):
            director_names.extend(d.get("name", "") for d in snap["data"]["directors"])
    sanctions = check_sanctions(kyb.legal_name, director_names,
                                  country=country, fixture_key=fixture_key)
    maps_check = check_address(kyb.warehouse_address, country)
    site = check_website(kyb.website)
    msg = check_messengers(kyb.whatsapp, kyb.telegram, kyb.phone)

    return {
        "aggregator":    aggregator,
        "opencorporates": opencorp,
        "vies":          vies,
        "sanctions":     sanctions,
        "maps":          maps_check,
        "site":          site,
        "messenger":     msg,
    }


def evaluate_risk(api_results: dict) -> tuple[str, str, list[str]]:
    """Интегральная оценка риска по всем API-результатам.

    Returns (auto_decision, risk_indicator, reasons).

    Логика по ТЗ §5:
      red signal anywhere → auto_decision='auto_reject', risk='red'
      yellow signals only → auto_decision='manual_review', risk='yellow'
      no signals          → auto_decision='sandbox_candidate', risk='green'
    """
    reds = []
    yellows = []
    for source_name, snap in (api_results or {}).items():
        if not isinstance(snap, dict):
            continue
        for sig in (snap.get("signals") or []):
            line = f"[{source_name}] {sig.get('msg', '')}"
            if sig.get("level") == "red":
                reds.append(line)
            elif sig.get("level") == "yellow":
                yellows.append(line)
    if reds:
        return ("auto_reject", "red", reds)
    if yellows:
        return ("manual_review", "yellow", yellows)
    return ("sandbox_candidate", "green", [])
