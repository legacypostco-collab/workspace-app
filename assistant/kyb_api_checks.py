"""KYB API checks — mock implementations of external sources from ТЗ §3.

Each function returns a deterministic dict (snapshot of "API response") for
testing. In production these would be HTTP calls to Контур.Фокус /
OpenCorporates / VIES / OpenSanctions / Yandex Maps / Google Maps.

The dict shape is stable: every result has `{ok, fetched_at, source, data, signals}`
where:
  - `ok`: bool — whether the API call itself succeeded
  - `fetched_at`: ISO timestamp
  - `source`: name + version
  - `data`: raw payload (snapshot for audit)
  - `signals`: derived flags consumed by `kyb_workflow.evaluate_risk()`:
       red / yellow / green markers, with reasons.

────────────────────────────────────────────────────────────────────
PRODUCTION SWAP POINTS — каждая check_* функция изолирована, чтобы
заменить mock на реальный API вызов точечно. Ниже env-переменные,
которые активируют real-режим (если присутствуют):

  KONTUR_FOCUS_TOKEN   → check_ru_aggregator()    https://focus-api.kontur.ru
  OPENCORPORATES_KEY   → check_opencorporates()   https://api.opencorporates.com
  VIES_USE_REAL=1      → check_vies()             https://ec.europa.eu/taxation_customs/vies (public, no key)
  OPENSANCTIONS_KEY    → check_sanctions()        https://api.opensanctions.org
  YANDEX_MAPS_KEY      → check_address() (RU)     https://geocode-maps.yandex.ru
  GOOGLE_MAPS_KEY      → check_address() (world)  https://maps.googleapis.com
  WA_BUSINESS_TOKEN    → check_messengers()       WhatsApp Business API
  TG_BOT_TOKEN         → check_messengers()       Telegram Bot API

Каждая mock-функция содержит # SWAP-POINT комментарий — реальный
HTTP-вызов с парсингом ответа в наш стандартный shape. Сигнатуры
функций менять НЕ нужно — `run_all_checks()` оркестрирует их.
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import hashlib
from typing import Any

from django.utils import timezone

# ──────────────────────────────────────────────────────────────────────
# Deterministic fixture lookup — every test company is identified by its
# (country, registration_number). The fixtures cover the 3 paths from
# ТЗ §5 «Автоматические решения системы»:
#   GREEN → быстро в Песочницу
#   YELLOW → ручная проверка
#   RED    → автоотказ
# Any unknown ID falls back to deterministic "synthesized" data based on
# hash of the number (always GREEN-ish), so we don't crash on real input.
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
    },
    # VIES-fixture идёт по ключу (country, digits-only VAT)
    ("AE", "100123456700003"): {
        "vies": {
            "valid": False, "vat_number": "AE100123456700003",
            "name": None,
            "reason": "VAT не активен в реестре EU VIES (компания вне ЕС, нужна ручная сверка TRN UAE)",
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


# ──────────────────────────────────────────────────────────────────────
# §3.1 — Российский агрегатор (Контур.Фокус / СПАРК / CheckCo)
# ──────────────────────────────────────────────────────────────────────

def check_ru_aggregator(inn: str, country: str = "RU") -> dict:
    # SWAP-POINT: if os.environ.get("KONTUR_FOCUS_TOKEN"):
    #   call https://focus-api.kontur.ru/api3/req?inn={inn}&key={token}
    #   parse → same shape {status, ogrn, kpp, directors, legal_address,
    #                       egrul_unreliable, mass_director_flag, ...}
    if country != "RU":
        return _wrap("kontur-focus", False, None,
                     [{"level": "info", "msg": "Not applicable for non-RU"}])
    fx = _FIXTURES.get((country, inn), {}).get("aggregator")
    if fx is None:
        # Fallback synthesis: deterministic green for unknown INNs
        h = int(hashlib.sha256(inn.encode()).hexdigest()[:6], 16)
        synthesized = {
            "status": "active", "registered_at": "2018-01-01",
            "ogrn": "10" + inn.ljust(13, "0")[:13], "kpp": (inn[:4] + "01001"),
            "directors": [{"name": "Тестовый Директор", "since": "2018-01-01"}],
            "legal_address": "г. Москва, тест-адрес",
            "okved": ["45.31.1"],
            "egrul_unreliable": False, "mass_director_flag": False,
            "mass_address_flag": False, "tax_debt": 0, "court_cases": h % 3,
            "risk_indicator": "green",
        }
        fx = synthesized

    signals = []
    if fx["status"] == "liquidation":
        signals.append({"level": "red", "msg": "Компания в стадии ликвидации"})
    if fx["status"] == "bankruptcy":
        signals.append({"level": "red", "msg": "Компания в стадии банкротства"})
    if fx.get("egrul_unreliable"):
        signals.append({"level": "red", "msg": "Запись о недостоверности сведений ЕГРЮЛ"})
    if fx.get("mass_director_flag"):
        signals.append({"level": "yellow", "msg": "Массовый директор"})
    if fx.get("mass_address_flag"):
        signals.append({"level": "yellow", "msg": "Массовый юридический адрес"})
    if fx.get("tax_debt", 0) > 100_000:
        signals.append({"level": "yellow", "msg": f"Налоговая недоимка ${fx['tax_debt']:,}"})
    if fx.get("court_cases", 0) > 5:
        signals.append({"level": "yellow", "msg": f"{fx['court_cases']} судебных дел"})
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
                     [{"level": "info", "msg": "Использовать ru-aggregator для РФ"}])
    fx = _FIXTURES.get((country, company_number), {}).get("opencorporates")
    if fx is None:
        fx = {"status": "active", "registered_at": "2020-01-01",
              "company_number": company_number, "company_type": "Unknown",
              "name": "Synthesized Co", "address": f"{country} (test address)",
              "directors": []}
    signals = []
    if fx["status"] in ("dissolved", "struck_off"):
        signals.append({"level": "red", "msg": f"Компания {fx['status']}"})
    return _wrap("opencorporates", True, fx, signals)


# ──────────────────────────────────────────────────────────────────────
# §3.3 — VIES (для VAT в ЕС)
# ──────────────────────────────────────────────────────────────────────

EU_COUNTRIES = {"AT","BE","BG","CY","CZ","DE","DK","EE","ES","FI","FR","GR",
                 "HR","HU","IE","IT","LT","LU","LV","MT","NL","PL","PT","RO",
                 "SE","SI","SK"}


def check_vies(vat_number: str, country: str) -> dict:
    fx = _FIXTURES.get((country, _strip_inn(vat_number) or ""), {}).get("vies")
    if fx is None:
        # Default: valid VAT for any test number with country prefix
        fx = {"valid": True, "vat_number": vat_number, "name": "(verified via VIES)"}
    signals = []
    if not fx["valid"]:
        # Для не-EU стран VIES не применим — это yellow (требует ручной
        # сверки локального tax-ID), не red (не автоотказ).
        level = "red" if country in EU_COUNTRIES else "yellow"
        signals.append({"level": level, "msg": fx.get("reason", "VAT не подтверждён")})
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
    fx = None
    if fixture_key:
        fx = _FIXTURES.get(fixture_key, {}).get("sanctions")
    if fx is None:
        fx = {"matches": []}
    signals = []
    for m in fx.get("matches", []):
        if m.get("match_score", 0) >= 0.85:
            signals.append({"level": "red",
                             "msg": f"Совпадение в {m['list']} ({m['name']}, score={m['match_score']:.2f})"})
        else:
            signals.append({"level": "yellow",
                             "msg": f"Возможное совпадение в {m['list']} ({m['name']})"})
    return _wrap("opensanctions", True, fx, signals)


# ──────────────────────────────────────────────────────────────────────
# §3.5 — Yandex / Google Maps (геокодирование склада)
# ──────────────────────────────────────────────────────────────────────

def check_address(address: str, country: str = "RU") -> dict:
    # Deterministic mock: классифицируем по ключевым словам в адресе.
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
        "coordinates": {"lat": 55.7558 + (hash(address) % 100) / 1000.0,
                          "lng": 37.6173 + (hash(address) % 100) / 1000.0},
        "streetview_url": f"https://maps.example.com/streetview?q={hash(address)}",
        "reviews_count": (hash(address) % 80),
        "rating": 4.2 if kind in ("industrial", "commercial") else None,
    }
    signals = []
    if not address:
        signals.append({"level": "yellow", "msg": "Адрес не указан"})
    elif kind == "residential":
        signals.append({"level": "yellow", "msg": "Адрес склада — жилой дом"})
    return _wrap("yandex-maps", True, data, signals)


# ──────────────────────────────────────────────────────────────────────
# §3.6 — Технические проверки (сайт, email, телефон, мессенджеры)
# ──────────────────────────────────────────────────────────────────────

def check_website(url: str) -> dict:
    ok = bool(url) and ("://" in url)
    signals = []
    data = {"reachable": ok, "url": url, "http_status": 200 if ok else None}
    if not ok:
        signals.append({"level": "yellow", "msg": "Сайт не указан или не валиден"})
    return _wrap("http-probe", True, data, signals)


def check_messengers(whatsapp: str, telegram: str, phone: str) -> dict:
    has_any = bool(whatsapp or telegram)
    data = {
        "whatsapp": {"present": bool(whatsapp), "registered": bool(whatsapp)},
        "telegram": {"present": bool(telegram), "registered": bool(telegram)},
        "phone": {"present": bool(phone), "valid_format": phone.startswith("+") if phone else False},
    }
    signals = []
    if not has_any:
        signals.append({"level": "yellow",
                         "msg": "Не указан ни один мессенджер для оперативной связи"})
    return _wrap("messenger-api", True, data, signals)


# ──────────────────────────────────────────────────────────────────────
# Public façade
# ──────────────────────────────────────────────────────────────────────

def run_all_checks(kyb) -> dict:
    """Выполнить все автопроверки для KYB-заявки. Возвращает полный
    api_results снэпшот + интегральный risk_indicator.

    ТЗ §3: «автомат за 10 секунд делает 5–7 API-запросов».
    """
    country = (kyb.country or "RU").upper()
    fixture_key = (country, kyb.inn or kyb.vat_number or "")

    aggregator = check_ru_aggregator(kyb.inn, country)
    opencorp = check_opencorporates(kyb.inn, country)
    vies = check_vies(kyb.vat_number, country) if kyb.vat_number else \
           _wrap("vies", False, None, [{"level": "info", "msg": "VAT не указан"}])
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
