"""External supplier rating from external services (Контур.Фокус / СПАРК).

ТЗ §1: внешняя оценка поставщика — 60% веса в общем рейтинге. Берём из:
  • юридический статус (active / banktrupt / liquidated)
  • финансовое состояние (выручка, прибыль, активы)
  • судебные дела (количество, размер исков)
  • признаки банкротства / ликвидации

Архитектура:
  fetch_external_rating(inn) → {score 0-100, bankruptcy, liquidation, reason, source}
  refresh_external_rating(seller) → применяет fetch к UserProfile + save (auto-recalc)

Реальный режим (KONTUR_FOCUS_API_KEY в env):
  GET https://api.kontur.ru/focus/api/3/companies?key=<>&inn=<>
  Ответ парсится: финансы, статус юр.лица, судебные дела → score 0-100

Demo режим (без API key):
  Детерминированный stub по hash(INN). Для презентаций:
    INN с '00…' → bankruptcy=True (тест банкротства)
    INN с '99…' → liquidation=True (тест ликвидации)
    Иначе score 40-99 (стабильный по INN, повторяемый)
"""
from __future__ import annotations

import hashlib
import logging
import os
from decimal import Decimal

logger = logging.getLogger(__name__)


# ── Fetch ─────────────────────────────────────────────────────

def fetch_external_rating(inn: str) -> dict:
    """Получить внешнюю оценку. Real Kontur при наличии env, иначе demo-stub.

    Returns:
        {
            "score": float (0-100),
            "bankruptcy": bool,
            "liquidation": bool,
            "reason": str — пояснение что повлияло
            "source": "live" | "demo" | "error"
        }
    """
    inn = (inn or "").strip()
    if not inn:
        return {"score": 50.0, "bankruptcy": False, "liquidation": False,
                "reason": "ИНН не указан", "source": "error"}

    api_key = os.getenv("KONTUR_FOCUS_API_KEY", "").strip()
    if api_key:
        try:
            return _fetch_kontur(inn, api_key)
        except Exception:
            logger.exception("kontur fetch failed for %s — fallback to demo", inn)
    return _demo_external_rating(inn)


def _fetch_kontur(inn: str, api_key: str) -> dict:
    """Live mode: HTTP к api.kontur.ru/focus/api/3.

    Поля ответа Kontur (упрощённо):
      status: 'active' | 'liquidating' | 'liquidated' | 'bankrupt'
      financialAnalytics.activityIndex: 0-100
      arbitration: список судов (count, totalSum)
    """
    import httpx
    r = httpx.get(
        "https://api.kontur.ru/focus/api/3/companies",
        params={"key": api_key, "inn": inn},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        return {"score": 50.0, "bankruptcy": False, "liquidation": False,
                "reason": f"ИНН {inn} не найден", "source": "live"}

    company = data[0]
    status_raw = (company.get("status") or {}).get("statusString", "").lower()
    bankruptcy = "банкрот" in status_raw or "bankrupt" in status_raw
    liquidation = "ликвидир" in status_raw or "liquidat" in status_raw

    # Финансовый индекс (0-100)
    fin = company.get("financialAnalytics", {})
    base_score = float(fin.get("activityIndex", 60))

    # Минус за судебные дела
    arb = company.get("arbitration", {})
    arb_count = int(arb.get("count", 0))
    score = max(0.0, base_score - min(arb_count * 0.5, 30))

    if bankruptcy or liquidation:
        score = 0.0
        reason = "банкротство" if bankruptcy else "ликвидация"
    elif arb_count > 20:
        reason = f"много судебных дел ({arb_count})"
    elif base_score < 50:
        reason = "слабый финансовый индекс"
    else:
        reason = f"активна, индекс {base_score:.0f}, дел {arb_count}"

    return {
        "score": min(100.0, score),
        "bankruptcy": bankruptcy,
        "liquidation": liquidation,
        "reason": reason,
        "source": "live",
    }


def _demo_external_rating(inn: str) -> dict:
    """Детерминированный stub по hash(INN)."""
    if inn.startswith("00"):
        return {"score": 0.0, "bankruptcy": True, "liquidation": False,
                "reason": "demo: ИНН '00…' → банкротство (тестовая ветка)",
                "source": "demo"}
    if inn.startswith("99"):
        return {"score": 0.0, "bankruptcy": False, "liquidation": True,
                "reason": "demo: ИНН '99…' → ликвидация (тестовая ветка)",
                "source": "demo"}
    h = int(hashlib.md5(inn.encode()).hexdigest(), 16)
    score = 40 + (h % 60)  # 40-99 диапазон
    return {
        "score": float(score),
        "bankruptcy": False,
        "liquidation": False,
        "reason": f"demo · hash-based score {score}",
        "source": "demo",
    }


# ── Apply to profile ──────────────────────────────────────────

def refresh_external_rating(seller) -> dict | None:
    """Получить external rating из Kontur и применить к UserProfile.

    1. Берём INN из CompanyVerification (если KYB пройден)
    2. Fetch
    3. Записываем в profile.external_score + bankruptcy/liquidation flags
    4. profile.save() триггерит recalculate_supplier_rating + status

    Returns: dict с результатом fetch, либо None если данные неполные.
    """
    if not seller or not seller.is_authenticated:
        return None
    try:
        from marketplace.models import CompanyVerification, UserProfile
        kyb = CompanyVerification.objects.filter(user=seller).first()
        inn = (kyb.inn if kyb else "") or ""
        if not inn:
            return {"score": None, "reason": "ИНН не указан в KYB",
                    "source": "skip"}
        data = fetch_external_rating(inn)
        profile = UserProfile.objects.filter(user=seller).first()
        if not profile:
            return None
        profile.external_score = Decimal(str(data["score"]))
        profile.bankruptcy_flag = bool(data["bankruptcy"])
        profile.liquidation_flag = bool(data["liquidation"])
        profile.save()  # → auto recalc rating + status
        return data
    except Exception:
        logger.exception("refresh_external_rating failed for %s", seller)
        return None
