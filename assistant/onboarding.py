"""Onboarding + KYB wizard actions.

Цепочка для нового продавца:
  start_onboarding → submit_company_info → submit_legal_address →
  submit_bank → submit_director → submit_for_review →
  [operator: op_kyb_review → op_kyb_approve|op_kyb_reject] →
  ✓ trader status

Используем существующую модель `marketplace.CompanyVerification` — без
новых миграций. Все шаги — DraftCard preview → confirm.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from .actions import ActionResult, _notify, register

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────

INN_RE = re.compile(r"^\d{10}(\d{2})?$")    # 10 (юр. лицо) или 12 (ИП)
KPP_RE = re.compile(r"^\d{9}$")
OGRN_RE = re.compile(r"^\d{13}(\d{2})?$")    # 13 (юр) или 15 (ИП)
BIK_RE = re.compile(r"^\d{9}$")
ACCOUNT_RE = re.compile(r"^\d{20}$")


def _kyb(user):
    """Get-or-create CompanyVerification для пользователя."""
    from marketplace.models import CompanyVerification
    kyb, _created = CompanyVerification.objects.get_or_create(user=user)
    return kyb


def _kyb_step(kyb) -> str:
    """Какой шаг текущий — на основании заполненности полей."""
    if kyb.status == "verified":
        return "verified"
    if kyb.status == "pending":
        return "pending"
    if kyb.status == "rejected":
        return "rejected"
    if not (kyb.legal_name and kyb.inn):
        return "company_info"
    if not kyb.legal_address:
        return "legal_address"
    if not (kyb.bank_name and kyb.bik and kyb.bank_account):
        return "bank"
    if not kyb.director_name:
        return "director"
    return "ready_for_review"


def _step_progress(step: str) -> tuple[int, int]:
    order = ["company_info", "legal_address", "bank", "director", "ready_for_review", "pending", "verified"]
    if step in order:
        return order.index(step) + 1, 5  # 5 заполняемых шагов до verify
    return 0, 5


# ══════════════════════════════════════════════════════════
# 0. Точка входа — start_onboarding
# ══════════════════════════════════════════════════════════

@register("start_onboarding")
def start_onboarding(params, user, role):
    """Показать текущий шаг onboarding'а или приветственный экран."""
    # Feature flag: в production KYB может быть выключен (mock-API дают
    # неверные данные). При выключенном — показываем contact-сообщение.
    from django.conf import settings
    if not getattr(settings, "KYB_ENABLED", True):
        return ActionResult(
            text=(
                "🛡 Самостоятельная верификация временно недоступна.\n"
                "Свяжитесь с менеджером: support@consolidator.parts — "
                "пройдём проверку вместе за 1-2 рабочих дня."
            ),
            contextual_actions=[{"action": "go_home", "label": "🏠 Главная"}],
        )
    kyb = _kyb(user)
    step = _kyb_step(kyb)

    if step == "verified":
        from marketplace.models import Order, OrderClaim, Part

        # ── Метрики работы поставщика на платформе ──────────────
        # Тест-юзеры маппятся на demo_seller — иначе свежий аккаунт
        # без своих заказов получит «нет событий», что не информативно.
        from .seller_actions import _effective_seller
        effective = _effective_seller(user)
        my_orders = Order.objects.filter(items__part__seller=effective).distinct()
        n_total      = my_orders.count()
        n_delivered  = my_orders.filter(status__in=("delivered", "completed")).count()
        n_in_flight  = my_orders.exclude(status__in=("delivered", "completed", "cancelled")).count()
        n_breached   = my_orders.filter(sla_status="breached").count()
        n_claims_act = OrderClaim.objects.filter(
            order__items__part__seller=user, status__in=("open", "in_review")
        ).distinct().count()
        sla_pct   = ((n_total - n_breached) / n_total * 100) if n_total else 0
        catalog_n = Part.objects.filter(seller=user, is_active=True).count()

        # ── ФОРМУЛА РЕЙТИНГА (ТЗ §3) ────────────────────────────
        # Итоговый рейтинг = Внешняя × 0.6 + Поведенческая × 0.4
        # Обе компоненты приводятся к шкале 0–100.

        # 1) Внешняя оценка (60% веса) — из Контур/СПАРК.
        # Здесь — proxy через risk_indicator KYB (для прода подключить
        # реальный коннектор). Banker/liquidation → блокировка ("Исключён").
        _EXT_BY_RISK = {"green": 90, "yellow": 70, "red": 35, "unknown": 60}
        external_score = _EXT_BY_RISK.get(kyb.risk_indicator or "unknown", 60)

        # 2) Поведенческая оценка (40% веса) — из реальных событий:
        #    скорость ответов + точность данных + срывы + возвраты.
        # Упрощённая формула:
        #    SLA% (×0.5) + (выполненные сделки до 100 × 0.3) − рекламации×5
        # — приводится к 0..100.
        beh_sla_pts    = sla_pct * 0.5 if n_total else 50  # без сделок = средне (50)
        beh_volume_pts = min(n_delivered, 100) / 100 * 30
        beh_claims_pen = min(n_claims_act * 5, 20)
        behavioral_score = max(0, min(100, beh_sla_pts + beh_volume_pts + 20 - beh_claims_pen))
        # +20 базовый — стартовый «кредит доверия» если нет негатива

        # Итог
        score = round(external_score * 0.6 + behavioral_score * 0.4)

        # Excluded override: банкротство/ликвидация → Исключён
        is_excluded = (kyb.risk_indicator == "red" and getattr(kyb, "auto_decision", "") == "auto_reject")
        if is_excluded:
            score = 0

        # ── СТАТУСЫ (ТЗ §1, §4) ─────────────────────────────────
        # 80–100 → Надёжный    — может быть исполнителем в AUTO
        # 60–79  → Песочница   — в AUTO только если нет Надёжных, в SEMI/MANUAL по решению оператора
        # 0–59   → Рисковый    — только справочно, исполнителем — особое разрешение оператора
        # bankruptcy → Исключён — полная блокировка
        TIERS = [
            (80, "Надёжный",  "ok",
             "AUTO-режим: автоматически выбирается исполнителем. SEMI: в приоритете у оператора. MANUAL: получает запросы первой очередью."),
            (60, "Песочница", "warn",
             "AUTO: участвует в расчёте, но не выбирается единственным исполнителем если есть «Надёжные». SEMI: только по решению оператора. MANUAL: вторая очередь рассылки."),
            (0,  "Рисковый",  "bad",
             "Только справочная информация о ценах. Исполнителем — только с явным подтверждением оператора и записью в аудит."),
        ]
        if is_excluded:
            tier, tier_tone, tier_perks = "Исключён", "bad", \
                "Поставщик заблокирован (банкротство/ликвидация). Не участвует ни в одном режиме подбора."
            next_tier = None
        else:
            tier, tier_tone, tier_perks = "Рисковый", "bad", TIERS[-1][3]
            next_tier = None
            for threshold, name, tone, perks in TIERS:
                if score >= threshold:
                    tier, tier_tone, tier_perks = name, tone, perks
                    break
            for threshold, name, tone, perks in TIERS:
                if threshold > score:
                    next_tier = (threshold, name, threshold - score)

        # ── Breakdown — из чего складывается рейтинг (ТЗ §3) ─────
        breakdown_items = [
            {"label": "Внешняя оценка (60% веса)",
             "value": f"{external_score}/100",
             "sub":   "Контур/СПАРК · юр.статус, финансы, суды, банкротство",
             "tone":  "ok" if external_score >= 80 else "warn" if external_score >= 60 else "bad"},
            {"label": "Поведенческая (40% веса)",
             "value": f"{round(behavioral_score)}/100",
             "sub":   "Скорость ответов · точность цен/сроков · возвраты · срывы",
             "tone":  "ok" if behavioral_score >= 80 else "warn" if behavioral_score >= 60 else "bad"},
            {"label": "Итог",
             "value": f"{score}/100",
             "sub":   f"= {external_score} × 0.6 + {round(behavioral_score)} × 0.4",
             "tone":  tier_tone},
        ]

        # Подсветка компонентов поведения (для блока «что повлияет»)
        # — отдельно от итоговой формулы, для понимания юзеру.
        sla_points    = round(beh_sla_pts) if n_total else 0
        volume_points = round(beh_volume_pts)
        claims_penalty = round(beh_claims_pen)

        # ── Чек-лист роста (кликабельный — каждый ведёт к нужному action)
        boosters: list[dict] = []
        if not kyb.website:
            boosters.append({"title": "🌐 Добавьте сайт компании — повышает доверие",
                              "action": "update_kyb_contacts", "params": {"focus": "website"}})
        if not (kyb.whatsapp or kyb.telegram):
            boosters.append({"title": "💬 Подключите мессенджер (WhatsApp/Telegram) — для оперативной связи",
                              "action": "update_kyb_contacts", "params": {"focus": "messenger"}})
        if not kyb.warehouse_address:
            boosters.append({"title": "📍 Заполните адрес склада — покупатель видит «откуда» едет груз",
                              "action": "update_kyb_contacts", "params": {"focus": "warehouse"}})
        if not kyb.doc_dealership:
            boosters.append({"title": "📜 Загрузите сертификаты дилерства — даёт бейдж «Официальный дилер»",
                              "action": "update_kyb_contacts", "params": {"focus": "docs"}})
        if not kyb.contact_email:
            boosters.append({"title": "📧 Укажите контактный email",
                              "action": "update_kyb_contacts", "params": {"focus": "email"}})
        if not kyb.phone:
            boosters.append({"title": "📞 Добавьте телефон для оператора",
                              "action": "update_kyb_contacts", "params": {"focus": "phone"}})
        if catalog_n == 0:
            boosters.append({"title": "📦 Загрузите прайс — без каталога вас не видят в поиске",
                              "action": "upload_pricelist", "params": {}})
        elif catalog_n < 100:
            boosters.append({"title": f"📦 В каталоге {catalog_n} позиций — расширьте до 100+ для роста выдачи",
                              "action": "upload_pricelist", "params": {}})
        if n_breached > 0:
            boosters.append({"title": f"⏱ Закройте {n_breached} SLA-нарушений — каждое снижает SLA-компонент",
                              "action": "seller_inbox", "params": {}})
        if n_claims_act > 0:
            boosters.append({"title": f"⚠️ Активных рекламаций: {n_claims_act} — каждая −5 баллов, быстрое решение снимает штраф",
                              "action": "get_claims", "params": {}})
        if next_tier and n_delivered < 100:
            boosters.append({"title": f"📈 Выполните больше сделок — Volume-компонент даст до +30 баллов (сейчас +{volume_points})",
                              "action": "get_demand_report", "params": {}})
        if not boosters:
            boosters.append({"title": "🎉 Базовые поля заполнены. Поддерживайте SLA и выполняйте сделки — рейтинг будет расти автоматически.",
                              "action": "seller_inbox", "params": {}})

        # ── Audit ───────────────────────────────────────────────
        risk_label = {"green":"🟢 Низкий риск", "yellow":"🟡 Средний риск",
                      "red":"🔴 Высокий риск", "unknown":"⚪ Риск не определён"}\
                     .get(kyb.risk_indicator or "", "")
        risk_tone = {"green":"ok", "yellow":"warn", "red":"bad"}.get(kyb.risk_indicator or "", "info")

        # ── Тиры — лестница для seller_status (отдельная list-карточка)
        ladder_items = []
        for threshold, name, tone, perks in TIERS:
            if score >= threshold:
                state = "current" if (next_tier and next_tier[0] != threshold and score >= threshold and not any(
                    s >= threshold and s != score for s in [score])) else "current"
                # упрощение: помечаем current тот тир, к которому относится текущий score
                state = "current" if (
                    (next_tier and threshold < next_tier[0] and threshold <= score) or
                    (not next_tier and threshold <= score)
                ) and not any(t[0] <= score and t[0] > threshold for t in TIERS) else "done" if threshold <= score else "future"
            else:
                state = "future"
            icon = "●" if state == "current" else ("✓" if state == "done" else "○")
            tone_class = "ok" if state in ("done", "current") else "info"
            ladder_items.append({
                "title":    f"{icon}  {name}  ·  от {threshold} баллов",
                "subtitle": perks,
                "tone":     tone_class,
            })

        # ── Hero metrics для нового seller_status-card ─────────
        hero = [
            {"label": "SLA",  "value": f"{sla_pct:.0f}%" if n_total else "—",
             "tone": "ok" if sla_pct >= 95 else "warn" if sla_pct >= 80 else "bad" if n_total else "info",
             "sub":  f"{n_breached} наруш. из {n_total}" if n_total else "Нет сделок"},
            {"label": "Выполнено", "value": str(n_delivered),
             "tone": "ok" if n_delivered >= 20 else "info",
             "sub":  f"в работе: {n_in_flight}" if n_in_flight else "—"},
            {"label": "Рекламации", "value": str(n_claims_act),
             "tone": "bad" if n_claims_act > 0 else "ok",
             "sub":  "снижают рейтинг" if n_claims_act > 0 else "всё чисто"},
        ]
        secondary = [
            {"label": "Каталог активный",
             "value": f"{catalog_n:,} поз.",
             "sub":   "≥100 позиций — лучшая выдача в поиске" if catalog_n < 100 else None},
        ]
        if next_tier:
            secondary.append({
                "label": f"До тира «{next_tier[1]}»",
                "value": f"+{next_tier[2]} баллов",
                "sub":   "Что даст следующий тир: " + next((t[3] for t in TIERS if t[1] == next_tier[1]), "—"),
            })
        else:
            secondary.append({
                "label": "Максимальный тир",
                "value": "Trusted Plus",
                "sub":   "Все условия максимальные. Удерживайте качество.",
            })

        # Text для message bubble
        next_line = (f"До «{next_tier[1]}» осталось +{next_tier[2]} баллов."
                      if next_tier else "Вы на максимальном тире — Trusted Plus.")
        text = (
            f"✓ Компания «{kyb.legal_name}» верифицирована.\n"
            f"Tier: {tier} · рейтинг {score}/100.\n"
            f"{next_line}"
        )

        # ── KPI-метрики (тот же первый вариант — 6 плашек + tier с подсказкой)
        kpi_items = [
            {"label": "Статус", "value": "✓ Верифицирована", "tone": "ok"},
            {"label": "Tier",   "value": tier, "tone": tier_tone,
             "sub": f"Рейтинг {score}/100"},
            {"label": "SLA",    "value": f"{sla_pct:.0f}%" if n_total else "—",
             "tone": "ok" if sla_pct >= 95 else "warn" if sla_pct >= 80 else "bad" if n_total else "info",
             "sub": f"{n_breached} нарушений из {n_total}" if n_total else "Нет завершённых сделок"},
            {"label": "Каталог", "value": f"{catalog_n:,} поз.",
             "tone": "ok" if catalog_n >= 100 else "warn" if catalog_n > 0 else "info"},
            {"label": "Заказов выполнено", "value": str(n_delivered),
             "sub": f"в работе: {n_in_flight}" if n_in_flight else None},
            {"label": "Активные рекламации", "value": str(n_claims_act),
             "tone": "bad" if n_claims_act > 0 else "ok"},
        ]

        # ── Текст-сообщение
        if is_excluded:
            next_line = "Восстановление — только после устранения причины блокировки и запроса оператору."
        elif next_tier:
            next_line = f"До статуса «{next_tier[1]}»: +{next_tier[2]} баллов."
        else:
            next_line = "Максимальный статус — поддерживайте качество."
        text = (
            f"✓ Компания «{kyb.legal_name}» верифицирована.\n"
            f"Статус: {tier} · рейтинг {score}/100. {next_line}\n\n"
            + (f"{risk_label} · юрисдикция {kyb.country}\n\n" if risk_label and kyb.country else "")
            + f"📊 Формула: Внешняя {external_score}/100 × 0.6 + Поведенческая {round(behavioral_score)}/100 × 0.4 = {score}/100"
        )

        # ── Тиры как list-карточка с подсветкой текущего (по ТЗ §1)
        tier_items = []
        for threshold, name, tone, perks in TIERS:
            is_current = (name == tier)
            tier_items.append({
                "title":    f"{'● ' if is_current else '○ '}{name} · {threshold}+ баллов{'  ← вы здесь' if is_current else ''}",
                "subtitle": perks,
                "tone":     "ok" if is_current else "info",
            })

        # ── События, повлиявшие на рейтинг (последние 20) ────────
        # Извлекаем из OrderEvent + OrderClaim для реальных трекаемых событий.
        from marketplace.models import OrderClaim, OrderEvent

        my_order_ids = list(my_orders.values_list("id", flat=True)[:500])
        rating_events: list[dict] = []
        # 1) SLA-нарушения — OrderEvent sla_status_changed → breached
        sla_evts = (
            OrderEvent.objects
            .filter(order_id__in=my_order_ids, event_type="sla_status_changed")
            .order_by("-created_at")[:30]
        )
        for ev in sla_evts:
            meta = ev.meta or {}
            new_status = meta.get("to") or meta.get("new_status")
            if new_status == "breached":
                rating_events.append({
                    "ts":     ev.created_at,
                    "title":  f"⬇ −3 балла · SLA нарушен по заказу #{ev.order_id}",
                    "subtitle": f"{ev.created_at.strftime('%d.%m.%Y %H:%M')} · поведенческая оценка снижена",
                    "badge":  {"label": "−3", "tone": "bad"},
                })
            elif new_status == "on_track":
                rating_events.append({
                    "ts":     ev.created_at,
                    "title":  f"⬆ +1 балл · SLA восстановлен по заказу #{ev.order_id}",
                    "subtitle": f"{ev.created_at.strftime('%d.%m.%Y %H:%M')}",
                    "badge":  {"label": "+1", "tone": "ok"},
                })
        # 2) Рекламации — OrderClaim события
        claims = OrderClaim.objects.filter(order_id__in=my_order_ids).order_by("-created_at")[:20]
        for c in claims:
            if c.status in ("open", "in_review"):
                rating_events.append({
                    "ts":     c.created_at,
                    "title":  f"⬇ −5 баллов · Открыта рекламация по заказу #{c.order_id}",
                    "subtitle": f"{c.created_at.strftime('%d.%m.%Y')} · вид: {c.get_kind_display() if hasattr(c, 'get_kind_display') else c.kind}",
                    "badge":  {"label": "−5", "tone": "bad"},
                })
            elif c.status in ("resolved", "closed"):
                rating_events.append({
                    "ts":     getattr(c, "closed_at", None) or c.created_at,
                    "title":  f"⬆ +5 баллов · Рекламация по #{c.order_id} закрыта",
                    "subtitle": f"{(c.closed_at or c.created_at).strftime('%d.%m.%Y')} · штраф снят",
                    "badge":  {"label": "+5", "tone": "ok"},
                })
        # 3) Выполненные сделки (delivered) — даёт + к volume-компоненту
        delivered_evts = (
            OrderEvent.objects
            .filter(order_id__in=my_order_ids, event_type="status_changed")
            .order_by("-created_at")[:50]
        )
        for ev in delivered_evts:
            meta = ev.meta or {}
            if meta.get("to") in ("delivered", "completed"):
                rating_events.append({
                    "ts":     ev.created_at,
                    "title":  f"⬆ +0.3 балла · Сделка закрыта (заказ #{ev.order_id})",
                    "subtitle": f"{ev.created_at.strftime('%d.%m.%Y')} · вклад в volume-компонент",
                    "badge":  {"label": "+0.3", "tone": "ok"},
                })

        # Сортируем по времени, обрезаем
        rating_events.sort(key=lambda e: e["ts"], reverse=True)
        rating_events = rating_events[:20]

        events_card_items = [{
            "title":    e["title"],
            "subtitle": e["subtitle"],
            "badge":    e["badge"],
        } for e in rating_events] or [{
            "title": "Пока нет событий, повлиявших на рейтинг",
            "subtitle": "Выполняйте заказы вовремя — каждое успешное закрытие даёт +0.3 балла",
        }]
        # Если Excluded — добавляем строку
        if is_excluded:
            tier_items.append({
                "title":    "✖ Исключён · полная блокировка",
                "subtitle": tier_perks,
                "tone":     "bad",
            })

        return ActionResult(
            text=text,
            cards=[
                {"type": "kpi_grid", "data": {
                    "title": "🛡 Статус и метрики поставщика",
                    "items": kpi_items,
                }},
                {"type": "list", "data": {
                    "title": "🏆 Тиры платформы и их преимущества",
                    "items": tier_items,
                }},
                {"type": "list", "data": {
                    "title": "📜 События, повлиявшие на рейтинг (последние 20)",
                    "items": events_card_items,
                }},
                {"type": "list", "data": {
                    "title": "📈 План роста: что повысит рейтинг",
                    "items": boosters,
                }},
            ],
            actions=[
                {"label": "📝 Обновить реквизиты", "action": "submit_company_info", "params": {}},
                {"label": "📦 Мой каталог",        "action": "seller_warehouses",   "params": {}},
                {"label": "🔥 Срочные задачи",     "action": "seller_inbox",        "params": {}},
                {"label": "📊 Спрос на рынке",     "action": "get_demand_report",   "params": {}},
                {"label": "💬 Связаться с менеджером", "action": "contact_operator",
                 "params": {"topic": "kyb"}},
            ],
            contextual_actions=[{"action": "go_home", "label": "🏠 Главная"}],
        )
    if step == "pending":
        return ActionResult(
            text=(
                f"⏳ Анкета отправлена на проверку оператору ({kyb.submitted_at:%d.%m.%Y %H:%M}).\n"
                f"Обычно проверка занимает до 24 часов. Дождитесь решения — мы пришлём "
                f"нотификацию."
            ),
            cards=[{"type": "kpi_grid", "data": {"title": "🛡 Статус KYB", "items": [
                {"label": "Статус", "value": "На проверке", "tone": "info"},
                {"label": "Компания", "value": kyb.legal_name or "—"},
                {"label": "ИНН", "value": kyb.inn or "—"},
            ]}}],
        )
    if step == "rejected":
        return ActionResult(
            text=(
                f"❌ Анкета отклонена оператором.\nПричина: {kyb.rejection_reason or '—'}\n\n"
                f"Исправьте данные и отправьте повторно."
            ),
            contextual_actions=[
                {"action": "submit_company_info", "label": "🔄 Начать заново"},
            ],
        )

    cur, total = _step_progress(step)
    next_action = {
        "company_info":     ("submit_company_info",  "Реквизиты компании"),
        "legal_address":    ("submit_legal_address", "Юридический адрес"),
        "bank":             ("submit_bank",          "Банковские реквизиты"),
        "director":         ("submit_director",      "Директор"),
        "ready_for_review": ("submit_for_review",    "Отправить на проверку"),
    }[step]

    return ActionResult(
        text=(
            f"👋 Добро пожаловать! Чтобы заключать сделки на платформе, "
            f"пройдите верификацию компании (KYB).\n"
            f"Шаг {cur}/{total} · {next_action[1]}"
        ),
        cards=[{"type": "kpi_grid", "data": {"title": "🚀 Onboarding", "items": [
            {"label": "Шаг", "value": f"{cur}/{total}", "tone": "info"},
            {"label": "Текущий", "value": next_action[1]},
            {"label": "Статус", "value": "Черновик"},
        ]}}],
        actions=[
            {"action": next_action[0], "label": f"➡ {next_action[1]}"},
        ],
        suggestions=[
            "Сколько времени занимает верификация?",
            "Какие документы нужны?",
        ],
    )


# ══════════════════════════════════════════════════════════
# 1. Реквизиты компании (legal_name + ИНН + КПП + ОГРН)
# ══════════════════════════════════════════════════════════

_COUNTRY_OPTIONS = [
    {"value": "RU", "label": "🇷🇺 Россия"},
    {"value": "CN", "label": "🇨🇳 Китай"},
    {"value": "AE", "label": "🇦🇪 ОАЭ"},
    {"value": "TR", "label": "🇹🇷 Турция"},
    {"value": "DE", "label": "🇩🇪 Германия"},
    {"value": "IT", "label": "🇮🇹 Италия"},
    {"value": "JP", "label": "🇯🇵 Япония"},
    {"value": "KR", "label": "🇰🇷 Южная Корея"},
    {"value": "US", "label": "🇺🇸 США"},
    {"value": "GB", "label": "🇬🇧 Великобритания"},
    {"value": "IN", "label": "🇮🇳 Индия"},
    {"value": "BR", "label": "🇧🇷 Бразилия"},
    {"value": "KZ", "label": "🇰🇿 Казахстан"},
    {"value": "BY", "label": "🇧🇾 Беларусь"},
    {"value": "OTHER", "label": "🌍 Другая страна"},
]

# Country-specific схемы: (label1, hint1, regex_check), (label2, …) и т.д.
# Хранятся в одних и тех же полях БД (inn/kpp/ogrn) — это просто разные UI-обёртки.
_COUNTRY_FIELDS = {
    "RU": [
        {"name": "inn",  "label": "ИНН", "hint": "10 цифр (юр.лицо) или 12 (ИП)",
         "required": True, "pattern": r"^\d{10}(\d{2})?$"},
        {"name": "kpp",  "label": "КПП", "hint": "9 цифр (только для юр.лица)",
         "pattern": r"^\d{9}$"},
        {"name": "ogrn", "label": "ОГРН", "hint": "13 цифр (юр) или 15 (ИП)",
         "pattern": r"^\d{13}(\d{2})?$"},
    ],
    "AE": [
        {"name": "inn",  "label": "Trade License No.", "hint": "Например 5022051 (RAKEZ / DED / IFZA)",
         "required": True},
        {"name": "ogrn", "label": "Tax Registration No. (TRN)", "hint": "15 цифр (если зарегистрирован для VAT)",
         "pattern": r"^\d{15}$"},
        {"name": "kpp",  "label": "Free Zone Authority", "hint": "RAKEZ, JAFZA, DMCC, IFZA, и т.д."},
    ],
    "CN": [
        {"name": "inn",  "label": "Unified Social Credit Code (统一社会信用代码)",
         "hint": "18 символов: цифры + буквы",
         "required": True, "pattern": r"^[0-9A-Z]{18}$"},
    ],
    "DE": [
        {"name": "inn",  "label": "Handelsregisternummer (HRB)",
         "hint": "Например HRB 12345 + город регистрации", "required": True},
        {"name": "kpp",  "label": "Steuernummer", "hint": "Налоговый номер компании"},
        {"name": "ogrn", "label": "USt-IdNr (VAT ID)", "hint": "Формат DE + 9 цифр"},
    ],
    "TR": [
        {"name": "inn",  "label": "Vergi Numarası (Tax No.)", "hint": "10 цифр",
         "required": True, "pattern": r"^\d{10}$"},
        {"name": "ogrn", "label": "Ticaret Sicil Numarası", "hint": "Trade Registry Number"},
    ],
    "US": [
        {"name": "inn",  "label": "EIN (Federal Tax ID)", "hint": "Формат XX-XXXXXXX",
         "required": True, "pattern": r"^\d{2}-?\d{7}$"},
        {"name": "kpp",  "label": "State of Incorporation", "hint": "DE / CA / NY / FL / …"},
    ],
    "GB": [
        {"name": "inn",  "label": "Company Number (Companies House)",
         "hint": "8 символов: цифры или 2 буквы + 6 цифр", "required": True},
        {"name": "ogrn", "label": "VAT Registration Number", "hint": "GB + 9 цифр"},
    ],
    "KZ": [
        {"name": "inn",  "label": "БИН / ИИН", "hint": "12 цифр",
         "required": True, "pattern": r"^\d{12}$"},
    ],
    "BY": [
        {"name": "inn",  "label": "УНП", "hint": "9 цифр",
         "required": True, "pattern": r"^\d{9}$"},
    ],
}

# Дефолт для CN/IN/BR/IT/JP/KR/OTHER — универсальные поля
_UNIVERSAL_FIELDS = [
    {"name": "inn",  "label": "Company Registration Number",
     "hint": "Регистрационный номер по реестру юр. лиц вашей страны",
     "required": True},
    {"name": "ogrn", "label": "Tax ID / VAT Number",
     "hint": "Налоговый идентификатор (если есть)"},
]


def _fields_for_country(code: str):
    return _COUNTRY_FIELDS.get(code) or _UNIVERSAL_FIELDS


@register("submit_company_info")
def submit_company_info(params, user, role):
    kyb = _kyb(user)
    country = (params.get("country") or kyb.country or "").strip().upper()
    legal_name = (params.get("legal_name") or "").strip()
    inn = (params.get("inn") or "").strip()
    kpp = (params.get("kpp") or "").strip()
    ogrn = (params.get("ogrn") or "").strip()
    confirmed = bool(params.get("confirmed"))

    # Phase 1: страна не выбрана — отдельный мини-шаг с одним select'ом
    if not country:
        return ActionResult(
            text="📋 Шаг 1/5 · Страна регистрации компании",
            cards=[{"type": "form", "data": {
                "title": "📋 Шаг 1/5 · Где зарегистрирована компания?",
                "intent": "В зависимости от страны мы попросим разные реквизиты: ИНН для РФ, Trade License для ОАЭ, USCC для Китая, и т.д.",
                "submit_action": "submit_company_info",
                "submit_label": "Далее →",
                "fields": [
                    {"name": "country", "label": "Страна", "type": "select",
                     "required": True, "options": _COUNTRY_OPTIONS,
                     "value": kyb.country or "RU"},
                ],
            }}],
        )

    fields_spec = _fields_for_country(country)

    # Phase 2: страна выбрана, показываем форму с country-specific полями
    if not confirmed or not (legal_name and inn):
        country_label = next((c["label"] for c in _COUNTRY_OPTIONS if c["value"] == country), country)
        form_fields = [
            {"name": "legal_name",
             "label": "Полное наименование компании" if country != "RU"
                       else "Полное наименование (как в ЕГРЮЛ)",
             "required": True, "value": kyb.legal_name},
        ]
        for f in fields_spec:
            form_fields.append({
                "name":     f["name"],
                "label":    f["label"],
                "hint":     f.get("hint", ""),
                "required": f.get("required", False),
                "value":    getattr(kyb, f["name"]) or "",
            })
        return ActionResult(
            text=f"📋 Шаг 1/5 · Реквизиты компании · {country_label}",
            cards=[{"type": "form", "data": {
                "title":         f"📋 Шаг 1/5 · Реквизиты компании · {country_label}",
                "intent":        "Поля помечены звёздочкой — обязательны. Остальное можно заполнить позже.",
                "submit_action": "submit_company_info",
                "fields":        form_fields,
                "fixed_params":  {"confirmed": True, "country": country},
            }}],
            actions=[
                {"label": "← Сменить страну", "action": "submit_company_info",
                 "params": {"country": ""}},
            ],
        )

    # Phase 3: валидация по правилам страны
    import re as _re
    errors = []
    for f in fields_spec:
        val = (params.get(f["name"]) or "").strip()
        if f.get("required") and not val:
            errors.append(f"{f['label']} — обязательное поле")
            continue
        pattern = f.get("pattern")
        if val and pattern and not _re.match(pattern, val):
            errors.append(f"{f['label']}: формат не подходит ({f.get('hint','')})")
    if errors:
        return ActionResult(
            text="⚠️ Проверьте данные:\n• " + "\n• ".join(errors),
            actions=[{"label": "← Назад к форме", "action": "submit_company_info",
                       "params": {"country": country}}],
        )

    kyb.country    = country
    kyb.legal_name = legal_name
    kyb.inn        = inn
    kyb.kpp        = kpp
    kyb.ogrn       = ogrn
    kyb.save(update_fields=["country", "legal_name", "inn", "kpp", "ogrn"])

    country_label = next((c["label"] for c in _COUNTRY_OPTIONS if c["value"] == country), country)
    return ActionResult(
        text=f"✓ Шаг 1/5 готов · {legal_name} ({country_label}).",
        actions=[
            {"action": "submit_legal_address", "label": "➡ Шаг 2/5 · Юридический адрес"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 2. Юридический адрес
# ══════════════════════════════════════════════════════════

@register("submit_legal_address")
def submit_legal_address(params, user, role):
    kyb = _kyb(user)
    address = (params.get("legal_address") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not address:
        return ActionResult(
            text="📍 Шаг 2/5 · Юридический адрес",
            cards=[{"type": "form", "data": {
                "title": "📍 Шаг 2/5 · Юридический адрес",
                "submit_action": "submit_legal_address",
                "fields": [
                    {"name": "legal_address", "label": "Адрес как в ЕГРЮЛ",
                     "type": "textarea", "required": True, "value": kyb.legal_address},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    kyb.legal_address = address
    kyb.save(update_fields=["legal_address"])
    return ActionResult(
        text="✓ Шаг 2/5 готов.",
        actions=[
            {"action": "submit_bank", "label": "➡ Шаг 3/5 · Банковские реквизиты"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 3. Банковские реквизиты
# ══════════════════════════════════════════════════════════

# Bank-форма зависит от страны компании (из шага 1).
# RU → БИК (9 цифр) + расчётный счёт (20 цифр)
# Все прочие → SWIFT/BIC + IBAN (или Account No.). БИК (Russian) ≠ SWIFT/BIC.
_BANK_FIELDS_BY_COUNTRY = {
    "RU": [
        {"name": "bank_name",    "label": "Наименование банка", "required": True,
         "hint": "Например: ПАО Сбербанк, Тинькофф Банк, ВТБ"},
        {"name": "bik",          "label": "БИК банка", "required": True,
         "hint": "9 цифр. Это российский Bank Identifier Code, не SWIFT.",
         "pattern": r"^\d{9}$"},
        {"name": "bank_account", "label": "Расчётный счёт", "required": True,
         "hint": "20 цифр (начинается с 4070… для коммерческих организаций)",
         "pattern": r"^\d{20}$"},
    ],
    "AE": [
        {"name": "bank_name",    "label": "Bank Name", "required": True,
         "hint": "Например: Emirates NBD (Gold & Diamond Park Branch), ADCB, …"},
        {"name": "bik",          "label": "SWIFT / BIC Code", "required": True,
         "hint": "8 или 11 символов, например UNILAEAD",
         "pattern": r"^[A-Z0-9]{8}([A-Z0-9]{3})?$"},
        {"name": "bank_account", "label": "IBAN", "required": True,
         "hint": "Например: AE34 0470 0000 0020 0830 094",
         "pattern": r"^AE\d{2}\s?(\d{4}\s?){4}\d{3}$"},
    ],
}
_BANK_UNIVERSAL = [
    {"name": "bank_name",    "label": "Bank Name", "required": True,
     "hint": "Полное наименование банка-получателя"},
    {"name": "bik",          "label": "SWIFT / BIC", "required": True,
     "hint": "8 или 11 символов (международный Bank Identifier Code)",
     "pattern": r"^[A-Z0-9]{8}([A-Z0-9]{3})?$"},
    {"name": "bank_account", "label": "IBAN / Account No.", "required": True,
     "hint": "IBAN если страна его поддерживает, иначе локальный номер счёта"},
]


def _bank_fields_for(country: str):
    return _BANK_FIELDS_BY_COUNTRY.get((country or "").upper()) or _BANK_UNIVERSAL


@register("submit_bank")
def submit_bank(params, user, role):
    import re as _re

    kyb = _kyb(user)
    country = (kyb.country or "RU").upper()
    fields_spec = _bank_fields_for(country)

    bank_name = (params.get("bank_name") or "").strip()
    bik       = (params.get("bik") or "").strip()
    account   = (params.get("bank_account") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not (bank_name and bik and account):
        country_label = next((c["label"] for c in _COUNTRY_OPTIONS if c["value"] == country), country)
        # Подсказка про SWIFT vs БИК для не-RU
        intent = (
            "Для российских банков указываем БИК (9 цифр) и 20-значный расчётный счёт."
            if country == "RU" else
            "Для международного перевода нужны SWIFT/BIC код банка и IBAN (или локальный номер счёта)."
        )
        form_fields = []
        for f in fields_spec:
            form_fields.append({
                "name":     f["name"],
                "label":    f["label"],
                "hint":     f.get("hint", ""),
                "required": f.get("required", False),
                "value":    getattr(kyb, f["name"]) or "",
            })
        return ActionResult(
            text=f"🏦 Шаг 3/5 · Банковские реквизиты · {country_label}",
            cards=[{"type": "form", "data": {
                "title":         f"🏦 Шаг 3/5 · Банковские реквизиты · {country_label}",
                "intent":        intent,
                "submit_action": "submit_bank",
                "fields":        form_fields,
                "fixed_params":  {"confirmed": True},
            }}],
        )

    # Валидация по country-specific patterns
    errors = []
    # Нормализуем IBAN/SWIFT — убираем пробелы и регистр для regex
    normalized = {
        "bank_name":    bank_name,
        "bik":          bik.replace(" ", "").upper(),
        "bank_account": account.replace(" ", "").upper(),
    }
    for f in fields_spec:
        val = normalized.get(f["name"], "")
        pattern = f.get("pattern")
        if val and pattern and not _re.match(pattern, val):
            errors.append(f"{f['label']}: формат не подходит ({f.get('hint','')})")
    if errors:
        return ActionResult(
            text="⚠️ Проверьте данные:\n• " + "\n• ".join(errors),
            actions=[{"label": "← Назад", "action": "submit_bank", "params": {}}],
        )

    kyb.bank_name    = bank_name
    kyb.bik          = normalized["bik"]
    kyb.bank_account = normalized["bank_account"]
    kyb.save(update_fields=["bank_name", "bik", "bank_account"])

    return ActionResult(
        text="✓ Шаг 3/5 готов.",
        actions=[
            {"action": "submit_director", "label": "➡ Шаг 4/5 · Директор"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 4. Директор
# ══════════════════════════════════════════════════════════

@register("submit_director")
def submit_director(params, user, role):
    kyb = _kyb(user)
    name = (params.get("director_name") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not name:
        return ActionResult(
            text="👤 Шаг 4/5 · Директор / уполномоченное лицо",
            cards=[{"type": "form", "data": {
                "title": "👤 Шаг 4/5 · Директор",
                "submit_action": "submit_director",
                "fields": [
                    {"name": "director_name", "label": "ФИО директора (как в паспорте)",
                     "required": True, "value": kyb.director_name},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    kyb.director_name = name
    kyb.save(update_fields=["director_name"])
    return ActionResult(
        text="✓ Шаг 4/5 готов.",
        actions=[
            {"action": "submit_for_review", "label": "➡ Шаг 5/5 · Отправить на проверку"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 5. Финал — submit_for_review
# ══════════════════════════════════════════════════════════

@register("submit_for_review")
def submit_for_review(params, user, role):
    kyb = _kyb(user)
    step = _kyb_step(kyb)
    if step != "ready_for_review":
        return ActionResult(
            text=f"Анкета не готова к отправке (текущий шаг: {step}). Заполните все поля.",
            actions=[{"action": "start_onboarding", "label": "Продолжить заполнение"}],
        )

    confirmed = bool(params.get("confirmed"))
    if not confirmed:
        return ActionResult(
            text="📨 Шаг 5/5 · Отправка на проверку",
            cards=[{"type": "draft", "data": {
                "title": "Подтвердите отправку анкеты",
                "rows": [
                    {"label": "Компания", "value": kyb.legal_name, "primary": True},
                    {"label": "ИНН / ОГРН", "value": f"{kyb.inn} / {kyb.ogrn or '—'}"},
                    {"label": "Адрес", "value": kyb.legal_address[:80]},
                    {"label": "Банк", "value": f"{kyb.bank_name} (БИК {kyb.bik})"},
                    {"label": "Счёт", "value": kyb.bank_account},
                    {"label": "Директор", "value": kyb.director_name},
                ],
                "warnings": [
                    "После отправки данные нельзя редактировать до решения оператора.",
                    "Проверка обычно занимает до 24 часов.",
                ],
                "confirm_action": "submit_for_review",
                "confirm_label": "📨 Отправить на проверку",
                "confirm_params": {"confirmed": True},
                "cancel_label": "Отмена",
            }}],
        )

    # ── ТЗ §3 — автоматические проверки за 10 секунд ──────────────
    # Прогоняем все 5–7 API-источников, сохраняем снэпшоты в api_results,
    # вычисляем risk_indicator и auto_decision. По итогам:
    #   red    → автоотказ (статус rejected сразу)
    #   yellow → попадает в очередь оператора (status=pending) на ручную проверку
    #   green  → попадает в очередь оператора, помечен как кандидат в «Песочницу»
    try:
        from .kyb_api_checks import evaluate_risk, run_all_checks
        kyb.api_results = run_all_checks(kyb)
        decision, risk, reasons = evaluate_risk(kyb.api_results)
        kyb.risk_indicator = risk
        kyb.auto_decision = decision
        kyb.auto_checked_at = timezone.now()
        if decision == "auto_reject":
            kyb.status = "rejected"
            kyb.rejection_reason = "АВТООТКАЗ по результатам авто-проверок:\n• " + "\n• ".join(reasons[:5])
            kyb.reviewed_at = timezone.now()
        else:
            kyb.status = "pending"
            kyb.rejection_reason = ""
        kyb.submitted_at = timezone.now()
        kyb.save()
    except Exception:
        # Если что-то с авто-проверками — всё равно ставим в очередь оператора
        logger.exception("auto-checks failed; falling back to manual review")
        kyb.status = "pending"
        kyb.submitted_at = timezone.now()
        kyb.rejection_reason = ""
        kyb.save(update_fields=["status", "submitted_at", "rejection_reason"])

    # Уведомляем всех операторов (через bell-notif + через admin-chat alerts)
    try:
        from django.contrib.auth import get_user_model
        for op in get_user_model().objects.filter(username__icontains="operator")[:5]:
            _notify(
                op, kind="system",
                title=f"Новая KYB-анкета: {kyb.legal_name}",
                body=f"ИНН {kyb.inn} · от {user.username}. Проверьте и одобрите/отклоните.",
                url="/chat/",
            )
    except Exception:
        logger.exception("notify operators on KYB submit failed")

    # Системное сообщение в admin-chat «Алерты оператора» — единая лента
    try:
        from .order_events import notify_operator_alert
        notify_operator_alert(user_obj=user, event="kyb_submitted",
                              extra={"legal_name": kyb.legal_name or "—"})
    except Exception:
        logger.exception("notify_operator_alert kyb_submitted failed")

    return ActionResult(
        text=(
            f"✓ Анкета «{kyb.legal_name}» отправлена на проверку.\n"
            f"Мы пришлём нотификацию когда оператор примет решение (обычно в течение 24 часов)."
        ),
        contextual_actions=[
            {"action": "start_onboarding", "label": "Статус анкеты"},
        ],
    )


@register("update_kyb_contacts")
def update_kyb_contacts(params, user, role):
    """Форма «контакты + дополнительные данные компании» — для повышения
    рейтинга после KYB. Заполняется отдельно от обязательных Шагов 1-5.

    params: {focus: 'website'|'messenger'|'email'|'phone'|'warehouse'|'docs'|''}
            confirmed: bool — submit
    """
    kyb = _kyb(user)
    confirmed = bool(params.get("confirmed"))
    focus = (params.get("focus") or "").strip()

    if not confirmed:
        focus_titles = {
            "website":   "🌐 Добавить сайт компании",
            "messenger": "💬 Подключить мессенджер",
            "email":     "📧 Контактный email",
            "phone":     "📞 Телефон компании",
            "warehouse": "📍 Адрес склада",
            "docs":      "📜 Сертификаты дилерства",
        }
        title = focus_titles.get(focus, "📝 Дополнительные данные компании")
        intent = (
            "Заполните доступные поля — каждое повышает доверие покупателей "
            "и рейтинг. Можно заполнить только то, что нужно сейчас, и вернуться позже."
        )
        return ActionResult(
            text=f"{title}",
            cards=[{"type": "form", "data": {
                "title":         title,
                "intent":        intent,
                "submit_action": "update_kyb_contacts",
                "submit_label":  "Сохранить",
                "fields": [
                    {"name": "website",
                     "label": "Сайт компании",
                     "type": "url",
                     "placeholder": "https://example.com",
                     "value": kyb.website or "",
                     "hint": "Покупатели проверят сайт перед сделкой — повышает доверие"},
                    {"name": "contact_email",
                     "label": "Контактный email",
                     "type": "email",
                     "placeholder": "sales@example.com",
                     "value": kyb.contact_email or "",
                     "hint": "Куда писать по новым запросам"},
                    {"name": "phone",
                     "label": "Телефон компании",
                     "placeholder": "+971 50 123 4567",
                     "value": kyb.phone or "",
                     "hint": "Оперативная связь с оператором платформы"},
                    {"name": "whatsapp",
                     "label": "WhatsApp",
                     "placeholder": "+971 50 123 4567",
                     "value": kyb.whatsapp or "",
                     "hint": "Если есть — оператор подтвердит, что номер активен"},
                    {"name": "telegram",
                     "label": "Telegram",
                     "placeholder": "@example_company или +971…",
                     "value": kyb.telegram or ""},
                    {"name": "warehouse_address",
                     "label": "Адрес склада (откуда отгружаете)",
                     "type": "textarea",
                     "placeholder": "Город, улица, дом, индекс",
                     "value": kyb.warehouse_address or "",
                     "hint": "Покупатель видит «откуда едет груз» — важно для оценки логистики"},
                    {"name": "categories",
                     "label": "Бренды и категории запчастей",
                     "type": "textarea",
                     "placeholder": "Например: CAT, Komatsu, Volvo CE — ходовая, гидравлика, двигатели",
                     "value": kyb.categories or "",
                     "hint": "По чему вас будут искать в каталоге платформы"},
                ],
                "fixed_params": {"confirmed": True},
            }}],
            actions=[
                {"label": "← Назад в статус", "action": "kyb_status", "params": {}},
            ],
        )

    # Сохранение
    changed = []
    for field in ("website", "contact_email", "phone", "whatsapp", "telegram",
                  "warehouse_address", "categories"):
        new_val = (params.get(field) or "").strip()
        if hasattr(kyb, field) and new_val != getattr(kyb, field):
            setattr(kyb, field, new_val[:400 if field == "categories" else 200])
            changed.append(field)
    if changed:
        kyb.save(update_fields=changed + ["updated_at"] if hasattr(kyb, "updated_at") else changed)

    return ActionResult(
        text=(f"✓ Сохранено {len(changed)} {'поле' if len(changed)==1 else 'поля'}."
              if changed else "Изменений нет."),
        actions=[
            {"label": "🛡 Вернуться к статусу", "action": "kyb_status", "params": {}},
        ],
    )


@register("kyb_status")
def kyb_status(params, user, role):
    """Read-only: статус KYB пользователя."""
    return start_onboarding(params, user, role)


# ══════════════════════════════════════════════════════════
# Operator — модерация KYB
# ══════════════════════════════════════════════════════════

def _is_operator(role: str) -> bool:
    return bool(role) and (role == "operator" or role.startswith("operator_"))


@register("op_kyb_queue")
def op_kyb_queue(params, user, role):
    if not _is_operator(role):
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    pending = list(CompanyVerification.objects.filter(status="pending").select_related("user").order_by("submitted_at")[:20])
    # Группировка по risk_indicator для приоритизации: green быстрее, yellow требует внимания
    RISK_BADGE = {
        "green":  ("🟢 Зелёный риск",  "ok"),
        "yellow": ("🟡 Жёлтый риск",  "warn"),
        "red":    ("🔴 Красный",       "bad"),
    }
    rows = []
    for kyb in pending:
        risk = kyb.risk_indicator or "unknown"
        badge_label, tone = RISK_BADGE.get(risk, ("⚪ Не проверено", "info"))
        # Кратко: сколько сигналов в каждой категории
        api = kyb.api_results or {}
        n_red = sum(1 for s in (api.get("aggregator", {}).get("signals") or [])
                      + (api.get("sanctions", {}).get("signals") or [])
                      + (api.get("vies", {}).get("signals") or [])
                      if s.get("level") == "red")
        n_yellow = sum(1 for snap in api.values() if isinstance(snap, dict)
                        for s in (snap.get("signals") or []) if s.get("level") == "yellow")
        flags = []
        if n_red:    flags.append(f"🔴 {n_red}")
        if n_yellow: flags.append(f"🟡 {n_yellow}")
        flags_str = " · ".join(flags) if flags else "✓ чисто"
        submitted_str = (kyb.submitted_at.strftime('%d.%m %H:%M')
                          if kyb.submitted_at else '—')
        rows.append({
            "title": f"{kyb.legal_name or '—'} · {kyb.country or 'RU'}",
            "subtitle": f"ИНН/№ {kyb.inn or '—'} · от {kyb.user.username} · "
                        f"{submitted_str} · {flags_str}",
            "tone": tone,
            "badge": {"label": badge_label, "tone": tone},
            "action": "op_kyb_review",
            "params": {"user_id": kyb.user_id},
        })
    return ActionResult(
        text=f"📋 Очередь KYB · {len(pending)} анкет ждут проверки. Клик по строке → детальная карточка.",
        cards=[{"type": "list", "data": {
            "title": "🛡 KYB на модерации",
            "items": rows or [{"title": "Очередь пуста", "subtitle": "Все анкеты обработаны"}],
        }}],
        contextual_actions=[
            {"action": "op_dashboard", "label": "← Сводка"},
        ],
    )


@register("op_kyb_review")
def op_kyb_review(params, user, role):
    """ТЗ §4 + §6: операторская карточка KYB.

    Показывает: данные формы поставщика + ВСЕ авто-проверки (5 API) с
    цветовыми сигналами + чеклист оператора (что нужно проверить глазами) +
    кнопки решений (одобрить / запросить уточнения / отклонить).
    """
    if not _is_operator(role):
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    try:
        kyb = CompanyVerification.objects.get(user_id=int(params.get("user_id") or 0))
    except (CompanyVerification.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Анкета не найдена.")

    # ── 1) Данные формы (§2 ТЗ) ──────────────────────────────────
    form_rows = [
        {"label": "Компания", "value": kyb.legal_name or "—", "primary": True},
        {"label": "Страна",   "value": kyb.country or "RU"},
        {"label": "Рег. номер (ИНН/Tax ID)", "value": kyb.inn or "—"},
        {"label": "VAT", "value": kyb.vat_number or "—"},
        {"label": "ОГРН / Company No.", "value": kyb.ogrn or "—"},
        {"label": "Юр. адрес", "value": (kyb.legal_address or "—")[:120]},
        {"label": "Адрес склада", "value": (kyb.warehouse_address or "—")[:120]},
        {"label": "Сайт", "value": kyb.website or "—"},
        {"label": "Телефон", "value": kyb.phone or "—"},
        {"label": "WhatsApp / Telegram", "value": f"{kyb.whatsapp or '—'} / {kyb.telegram or '—'}"},
        {"label": "Email", "value": kyb.contact_email or "—"},
        {"label": "Категории", "value": (kyb.categories or "—")[:120]},
        {"label": "Банк / БИК", "value": f"{kyb.bank_name or '—'} ({kyb.bik or '—'})"},
        {"label": "Счёт", "value": kyb.bank_account or "—"},
        {"label": "Директор", "value": kyb.director_name or "—"},
        {"label": "Статус", "value": kyb.get_status_display()},
        {"label": "Risk-индикатор", "value":
            {"green": "🟢 Зелёный", "yellow": "🟡 Жёлтый", "red": "🔴 Красный"}
            .get(kyb.risk_indicator, "⚪ Не определён")},
    ]
    if kyb.submitted_at:
        form_rows.append({"label": "Подана", "value": kyb.submitted_at.strftime("%d.%m.%Y %H:%M")})
    if kyb.auto_checked_at:
        form_rows.append({"label": "Авто-проверки", "value": kyb.auto_checked_at.strftime("%d.%m.%Y %H:%M")})

    # ── 2) Авто-API панель (§3 ТЗ) ──────────────────────────────
    SOURCE_LABELS = {
        "aggregator":     "📊 Контур.Фокус (РФ-агрегатор)",
        "opencorporates": "🌍 OpenCorporates (зарубежные)",
        "vies":           "🇪🇺 VIES (VAT в ЕС)",
        "sanctions":      "🛑 OpenSanctions",
        "maps":           "🗺 Яндекс/Google Maps",
        "site":           "🌐 Сайт",
        "messenger":      "💬 Мессенджеры",
    }
    LVL_DOT = {"red": "🔴", "yellow": "🟡", "green": "🟢", "info": "ℹ️"}
    api_rows = []
    for src_key, label in SOURCE_LABELS.items():
        snap = (kyb.api_results or {}).get(src_key) or {}
        signals = snap.get("signals") or []
        if not snap.get("ok"):
            api_rows.append({"title": label,
                             "subtitle": (signals[0]["msg"] if signals else "не применимо"),
                             "tone": "info"})
            continue
        if not signals:
            api_rows.append({"title": label,
                             "subtitle": "✓ Без замечаний",
                             "tone": "ok"})
            continue
        # 1 строка на сигнал, сгруппировано
        for sig in signals[:3]:
            lvl = sig.get("level", "info")
            api_rows.append({
                "title": f"{LVL_DOT.get(lvl, '·')} {label}",
                "subtitle": sig.get("msg", "—"),
                "tone": {"red": "bad", "yellow": "warn", "green": "ok"}.get(lvl, "info"),
            })

    # ── 3) Чеклист оператора (§4 ТЗ — глазами 2-3 минуты) ──────
    checklist_items = [
        ("streetview_ok",     "Склад в Street View — реальное здание, не жилой дом"),
        ("reviews_ok",        "Отзывы на картах и в сети — нет массовых жалоб"),
        ("site_ok",           "Сайт — рабочий магазин запчастей, не пустой лендинг"),
        ("bank_ok",           "Реквизиты счёта — страна совпадает, без посредников"),
        ("certs_ok",          "Сертификаты дилерства выглядят настоящими (если заявлены)"),
        ("messenger_test_ok", "Мессенджер отвечает (тестовое сообщение)"),
    ]
    checklist_state = kyb.operator_checklist or {}
    checklist_rows = [
        {"title": ("✅ " if checklist_state.get(k) else "⬜ ") + lbl,
         "subtitle": "Кликнуть → отметить как проверено",
         "action": "op_kyb_check",
         "params": {"user_id": kyb.user_id, "item": k}}
        for (k, lbl) in checklist_items
    ]

    # ── 4) Решения (§6 ТЗ) — кнопки ─────────────────────────────
    actions = []
    if kyb.status == "pending":
        actions = [
            {"action": "op_kyb_approve",
             "label": "✓ Одобрить → Песочница",
             "params": {"user_id": kyb.user_id}},
            {"action": "op_kyb_clarify",
             "label": "❓ Запросить уточнения",
             "params": {"user_id": kyb.user_id}},
            {"action": "op_kyb_reject",
             "label": "✗ Отклонить",
             "params": {"user_id": kyb.user_id}},
        ]
    elif kyb.status == "rejected":
        actions = [
            {"action": "op_kyb_approve",
             "label": "🔄 Пересмотреть → одобрить",
             "params": {"user_id": kyb.user_id}},
        ]

    return ActionResult(
        text=(
            f"🛡 KYB · {kyb.legal_name or '—'}\n"
            f"Авто-проверка: {kyb.risk_indicator or 'unknown'} · "
            f"решение системы: {kyb.auto_decision or 'не определено'}"
        ),
        cards=[
            {"type": "draft", "data": {
                "title": f"📋 Анкета · {kyb.user.username}",
                "rows": form_rows,
                "confirm_label": "—",
            }},
            {"type": "list", "data": {
                "title": "🔬 Авто-проверки (5–7 API за 10 сек)",
                "items": api_rows or [{"title": "Авто-проверки не запускались",
                                         "subtitle": "Использовать submit_for_review для запуска"}],
            }},
            {"type": "list", "data": {
                "title": "👁 Что проверить глазами (2–3 минуты)",
                "items": checklist_rows,
            }},
        ],
        actions=actions,
        contextual_actions=[
            {"action": "op_kyb_queue", "label": "← Очередь KYB"},
        ],
    )


@register("op_kyb_check")
def op_kyb_check(params, user, role):
    """Toggle для отметки оператором пункта чеклиста (§4 ТЗ)."""
    if not _is_operator(role):
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    try:
        kyb = CompanyVerification.objects.get(user_id=int(params.get("user_id") or 0))
    except (CompanyVerification.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Анкета не найдена.")
    item = (params.get("item") or "").strip()
    if not item:
        return ActionResult(text="Не указан пункт чеклиста.")
    state = kyb.operator_checklist or {}
    state[item] = not bool(state.get(item))
    kyb.operator_checklist = state
    kyb.save(update_fields=["operator_checklist"])
    # Перерисовываем review-карточку (обновлённый чеклист)
    return op_kyb_review({"user_id": kyb.user_id}, user, role)


@register("op_kyb_clarify")
def op_kyb_clarify(params, user, role):
    """ТЗ §6: «Запросить уточнения» — отправить запрос поставщику на доп. документы."""
    if not _is_operator(role):
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    try:
        kyb = CompanyVerification.objects.get(user_id=int(params.get("user_id") or 0))
    except (CompanyVerification.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Анкета не найдена.")
    confirmed = bool(params.get("confirmed"))
    note = (params.get("note") or "").strip()
    if not confirmed:
        return ActionResult(
            text=f"❓ Запросить уточнения у {kyb.legal_name}?",
            cards=[{"type": "form", "data": {
                "title": "Запрос на уточнения",
                "submit_action": "op_kyb_clarify",
                "fields": [
                    {"name": "note", "label": "Что запросить",
                     "type": "textarea", "required": True,
                     "value": "Просим прислать фото склада с вывеской и копию сертификата дилерства."},
                ],
                "fixed_params": {"user_id": kyb.user_id, "confirmed": True},
            }}],
        )
    kyb.operator_note = note
    kyb.save(update_fields=["operator_note"])
    try:
        _notify(kyb.user, kind="system",
                title="Уточнения по KYB",
                body=note, url="/chat/")
    except Exception:
        pass
    return ActionResult(
        text=f"✓ Запрос отправлен поставщику {kyb.user.username}.",
        contextual_actions=[{"action": "op_kyb_queue", "label": "← Очередь KYB"}],
    )


@register("op_kyb_approve")
def op_kyb_approve(params, user, role):
    if not _is_operator(role):
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    confirmed = bool(params.get("confirmed"))
    try:
        kyb = CompanyVerification.objects.get(user_id=int(params.get("user_id") or 0))
    except (CompanyVerification.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Анкета не найдена.")

    if kyb.status != "pending":
        return ActionResult(text=f"Анкета не в статусе pending (сейчас: {kyb.get_status_display()}).")

    if not confirmed:
        return ActionResult(
            text=f"Одобрить анкету {kyb.legal_name}?",
            cards=[{"type": "draft", "data": {
                "title": f"✓ Одобрение KYB · {kyb.legal_name}",
                "rows": [
                    {"label": "Компания", "value": kyb.legal_name, "primary": True},
                    {"label": "ИНН", "value": kyb.inn},
                    {"label": "Пользователь", "value": kyb.user.username},
                ],
                "confirm_action": "op_kyb_approve",
                "confirm_label": "✓ Одобрить",
                "confirm_params": {"user_id": kyb.user_id, "confirmed": True},
                "cancel_label": "Отмена",
            }}],
        )

    kyb.status = "verified"
    kyb.reviewed_at = timezone.now()
    kyb.reviewed_by = user
    kyb.rejection_reason = ""
    kyb.save(update_fields=["status", "reviewed_at", "reviewed_by", "rejection_reason"])

    # ТЗ §1: после verify сразу обновляем external_score из Kontur/СПАРК
    # → bankruptcy_flag/liquidation_flag → status может сразу стать rejected
    try:
        from .external_rating import refresh_external_rating
        refresh_external_rating(kyb.user)
    except Exception:
        logger.exception("auto-refresh external rating after KYB approve failed")

    _notify(
        kyb.user, kind="system",
        title=f"✓ KYB одобрен · {kyb.legal_name}",
        body="Все возможности платформы теперь доступны: можно отвечать на RFQ, оформлять заказы, управлять каталогом.",
        url="/chat/",
    )

    return ActionResult(
        text=f"✓ KYB одобрен · «{kyb.legal_name}» (ИНН {kyb.inn}). Уведомление отправлено.",
        contextual_actions=[
            {"action": "op_kyb_queue", "label": "← Очередь"},
        ],
    )


@register("op_kyb_reject")
def op_kyb_reject(params, user, role):
    if not _is_operator(role):
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    try:
        kyb = CompanyVerification.objects.get(user_id=int(params.get("user_id") or 0))
    except (CompanyVerification.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Анкета не найдена.")
    if kyb.status != "pending":
        return ActionResult(text=f"Анкета не в статусе pending (сейчас: {kyb.get_status_display()}).")

    reason = (params.get("reason") or "").strip()
    confirmed = bool(params.get("confirmed"))
    if not confirmed or not reason:
        return ActionResult(
            text="Укажите причину отклонения",
            cards=[{"type": "form", "data": {
                "title": f"✗ Отклонить KYB · {kyb.legal_name}",
                "submit_action": "op_kyb_reject",
                "fields": [
                    {"name": "reason", "label": "Причина (видна заявителю)",
                     "type": "textarea", "required": True},
                ],
                "fixed_params": {"user_id": kyb.user_id, "confirmed": True},
            }}],
        )

    kyb.status = "rejected"
    kyb.rejection_reason = reason
    kyb.reviewed_at = timezone.now()
    kyb.reviewed_by = user
    kyb.save(update_fields=["status", "rejection_reason", "reviewed_at", "reviewed_by"])

    _notify(
        kyb.user, kind="system",
        title=f"✗ KYB отклонён · {kyb.legal_name}",
        body=f"Причина: {reason[:160]}. Исправьте данные и отправьте повторно.",
        url="/chat/",
    )

    return ActionResult(
        text=f"✗ KYB отклонён · «{kyb.legal_name}». Причина передана заявителю.",
        contextual_actions=[
            {"action": "op_kyb_queue", "label": "← Очередь"},
        ],
    )


# ══════════════════════════════════════════════════════════
# Gating helper — exposed для actions.py / can_execute()
# ══════════════════════════════════════════════════════════

def kyb_required_for_seller(user) -> bool:
    """True если у пользователя KYB не verified (нужно блокировать seller-actions).

    Demo-аккаунты (demo_*) пропускаются — у них статусы могут быть пустыми, но они
    должны работать «из коробки» для презентаций.
    """
    if not user or not user.is_authenticated:
        return False
    if (user.username or "").startswith("demo_"):
        return False
    try:
        from marketplace.models import CompanyVerification
        kyb = CompanyVerification.objects.filter(user=user).first()
        if not kyb:
            return True
        return kyb.status != "verified"
    except Exception:
        return False
