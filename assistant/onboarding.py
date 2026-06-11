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
    """Показать текущий шаг onboarding'а или приветственный экран.
    Доступно только продавцам — KYB-верификация это юр.процедура для
    тех кто хочет ПРОДАВАТЬ на платформе. Покупателю / оператору
    смотреть тут нечего (оператор для проверки KYB заходит через
    op_kyb_queue, не через эту анкету)."""
    # Role gate: показываем только под seller. Operator / buyer / admin
    # перенаправляются к своему интерфейсу.
    if role not in ("seller",):
        if role and role.startswith("operator") or role == "admin":
            return ActionResult(
                text=("🛡 Верификация (KYB-анкета) предназначена для продавцов. "
                      "Очередь анкет на проверку у оператора — это другой экран."),
                actions=[{"label": "🛡 Очередь KYB на проверке",
                          "action": "op_kyb_queue", "params": {}}],
            )
        return ActionResult(
            text=("🛡 Верификация компании нужна продавцам — чтобы выставлять "
                  "товары на платформе. Если вы покупатель, никаких анкет "
                  "заполнять не нужно: сразу создавайте RFQ или ищите запчасти."),
            actions=[{"label": "🔍 Найти запчасть", "action": "search_parts", "params": {}}],
        )
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
        # 1) Wizard-карточка с все шагами «done» (единый UX с onboarding-флоу)
        # 2) Рейтинг + tier + breakdown (откуда баллы)
        # 3) Конкретный план роста — что сделать чтобы поднять рейтинг
        STEPS = [
            ("company_info",     "Реквизиты компании"),
            ("legal_address",    "Юридический адрес"),
            ("bank",             "Банковские реквизиты"),
            ("director",         "Директор"),
            ("ready_for_review", "Отправить на проверку"),
        ]
        steps_data = [
            {"n": i, "label": label, "state": "done"}
            for i, (_k, label) in enumerate(STEPS, start=1)
        ]

        # ── Рейтинг (ТЗ §3): Внешняя × 0.6 + Поведенческая × 0.4 ──
        from marketplace.models import Order, OrderClaim, Part
        from .seller_actions import _effective_seller
        effective = _effective_seller(user)
        my_orders = Order.objects.filter(items__part__seller=effective).distinct()
        n_total      = my_orders.count()
        n_delivered  = my_orders.filter(status__in=("delivered", "completed")).count()
        n_in_flight  = my_orders.exclude(status__in=("delivered", "completed", "cancelled")).count()
        n_breached   = my_orders.filter(sla_status="breached").count()
        n_claims_act = OrderClaim.objects.filter(
            order__items__part__seller=effective, status__in=("open", "in_review")
        ).distinct().count()
        sla_pct   = ((n_total - n_breached) / n_total * 100) if n_total else 0
        catalog_n = Part.objects.filter(seller=effective, is_active=True).count()

        _EXT_BY_RISK = {"green": 90, "yellow": 70, "red": 35, "unknown": 60}
        external_score = _EXT_BY_RISK.get(kyb.risk_indicator or "unknown", 60)
        beh_sla_pts    = sla_pct * 0.5 if n_total else 50
        beh_volume_pts = min(n_delivered, 100) / 100 * 30
        beh_claims_pen = min(n_claims_act * 5, 20)
        behavioral_score = max(0, min(100, beh_sla_pts + beh_volume_pts + 20 - beh_claims_pen))
        score = round(external_score * 0.6 + behavioral_score * 0.4)

        if score >= 80:
            tier, tier_tone, next_tier = "Надёжный", "ok", None
        elif score >= 60:
            tier, tier_tone, next_tier = "Песочница", "warn", (80, "Надёжный", 80 - score)
        else:
            tier, tier_tone, next_tier = "Рисковый", "bad", (60, "Песочница", 60 - score)

        # Каждая ячейка кликабельна → ведёт на релевантный детальный экран.
        kpi_items = [
            {"label": "Рейтинг", "value": f"{score}/100", "tone": tier_tone,
             "action": "seller_analytics_hub", "params": {},
             "sub": tier + (f" · до «{next_tier[1]}»: +{next_tier[2]}" if next_tier else " · максимум")},
            {"label": "Внешняя оценка (60%)", "value": f"{external_score}/100",
             "tone": "ok" if external_score >= 80 else "warn" if external_score >= 60 else "bad",
             "action": "start_onboarding", "params": {},
             "sub": "Контур/СПАРК · юр.статус, финансы"},
            {"label": "Поведенческая (40%)", "value": f"{round(behavioral_score)}/100",
             "tone": "ok" if behavioral_score >= 80 else "warn" if behavioral_score >= 60 else "bad",
             "action": "get_orders", "params": {},
             "sub": "SLA · сделки · рекламации"},
            {"label": "SLA", "value": f"{sla_pct:.0f}%" if n_total else "—",
             "tone": "ok" if sla_pct >= 95 else "warn" if sla_pct >= 80 else "bad" if n_total else "info",
             "action": "get_orders", "params": {},
             "sub": f"{n_breached} наруш. из {n_total}" if n_total else "Нет завершённых сделок"},
            {"label": "Заказов выполнено", "value": str(n_delivered),
             "action": "get_orders", "params": {},
             "sub": f"в работе: {n_in_flight}" if n_in_flight else None},
            {"label": "Каталог", "value": f"{catalog_n:,} поз.",
             "action": "seller_warehouses", "params": {},
             "tone": "ok" if catalog_n >= 100 else "warn" if catalog_n > 0 else "info"},
        ]

        # ── План роста: только конкретные пункты, привязанные к реальным
        # данным/действиям. Показываем ТОЛЬКО то что:
        #  (а) Есть в БД-модели (можно проверить — заполнено или нет)
        #  (б) Привязано к существующему action'у (есть куда кликнуть)
        #  (в) Даёт измеримый эффект на рейтинг (+N баллов конкретно куда)

        # Реальные метрики из БД
        try:
            from marketplace.models import Quote
            quotes_count = Quote.objects.filter(seller=effective).count()
            quotes_accepted = Quote.objects.filter(seller=effective, status="accepted").count()
        except Exception:
            quotes_count = quotes_accepted = 0
        accept_rate = round(quotes_accepted / quotes_count * 100) if quotes_count else None

        # Свежесть прайс-листа (когда был последний апдейт)
        try:
            from django.utils import timezone as _tz
            latest_part = Part.objects.filter(seller=effective).order_by("-updated_at").first()
            days_since_pricelist = (_tz.now() - latest_part.updated_at).days if latest_part else None
        except Exception:
            days_since_pricelist = None

        boosters = []

        # ── 1) КОНТАКТЫ (что заметно покупателю и оператору) ──
        if not kyb.website:
            boosters.append({"title": "🌐 Сайт компании · +5 к внешней оценке",
                              "action": "update_kyb_contacts", "params": {"focus": "website"}})
        if not (kyb.whatsapp or kyb.telegram):
            boosters.append({"title": "💬 WhatsApp или Telegram · +3 к скорости отклика",
                              "action": "update_kyb_contacts", "params": {"focus": "messenger"}})
        if not kyb.phone:
            boosters.append({"title": "📞 Телефон срочной связи · обязательно для SEMI-режима",
                              "action": "update_kyb_contacts", "params": {"focus": "phone"}})
        if not kyb.warehouse_address:
            boosters.append({"title": "📍 Адрес склада · покупатель видит ETA на этапе подбора",
                              "action": "update_kyb_contacts", "params": {"focus": "warehouse"}})

        # ── 2) ДОКУМЕНТЫ (юридическое доверие) ──
        if not kyb.doc_dealership:
            boosters.append({"title": "📜 Сертификат дилерства · бейдж «Официальный дилер» (+10 внешней)",
                              "action": "upload_kyb_doc", "params": {"kind": "dealership"}})
        if not getattr(kyb, "vat_number", ""):
            boosters.append({"title": "🏢 VAT/Tax ID · обязателен для трансграничных сделок (RU↔CN, EU)",
                              "action": "submit_company_info", "params": {}})

        # ── 3) КАТАЛОГ (объём + актуальность) ──
        if catalog_n == 0:
            boosters.append({"title": "📦 Загрузить прайс · без каталога вы не появляетесь в поиске",
                              "action": "upload_pricelist", "params": {}})
        elif catalog_n < 100:
            boosters.append({"title": f"📦 В каталоге {catalog_n} поз. · 100+ позиций даёт выдачу в 3× чаще",
                              "action": "upload_pricelist", "params": {}})
        if days_since_pricelist is not None and days_since_pricelist > 30:
            boosters.append({"title": f"🔄 Прайс не обновлялся {days_since_pricelist} дн · устаревшие цены → рекламации",
                              "action": "upload_pricelist", "params": {}})

        # ── 4) ИСПОЛНЕНИЕ (SLA + рекламации — реальные штрафы) ──
        if n_breached > 0:
            boosters.append({"title": f"⏱ {n_breached} SLA-нарушение(ий) · каждое −{round(beh_sla_pts * (1/n_total) if n_total else 0, 1)}б Поведенческой",
                              "action": "seller_inbox", "params": {}})
        if n_claims_act > 0:
            boosters.append({"title": f"⚠️ {n_claims_act} активная(ых) рекламация(й) · −5б за каждую, закрытие снимает штраф",
                              "action": "get_claims", "params": {}})

        # ── 5) КОНВЕРСИЯ КП (если данные есть) ──
        if accept_rate is not None and quotes_count >= 5 and accept_rate < 30:
            boosters.append({"title": f"💼 Принято {accept_rate}% КП (среднее 35–50%) · проверьте цены и сроки",
                              "action": "seller_inbox", "params": {}})

        # ── 6) ВЫРАЩИВАНИЕ ОБЪЁМА ──
        if next_tier and n_delivered < 100:
            boosters.append({"title": f"📈 До +30б за объём · сейчас +{round(beh_volume_pts)} (из 30), берите больше RFQ",
                              "action": "seller_inbox", "params": {}})

        if not boosters:
            boosters.append({"title": "🎉 Все базовые рычаги отработаны · держите SLA и закрывайте сделки — рейтинг будет расти.",
                              "action": "seller_inbox", "params": {}})

        # ── Поведенческие правила платформы (мини-обучение) ─────
        # Привязано к РЕАЛЬНОЙ механике Consolidator:
        #  • State machine из 13 статусов (от «Запрос создан» до «Заказ закрыт»)
        #  • Режимы подбора AUTO / SEMI / MANUAL
        #  • Финмодель: 6% FOB / 8% CIF / 12% DDP (платит seller-экспортёр)
        #  • Резерв 10% → производство → 90% после готовности
        #  • Рейтинг = 60% внешняя + 40% поведение, ручной правки нет
        tips = [
            {
                "title": "🤖 AUTO-режим — принимайте только то, что реально на складе",
                "subtitle": "Платформа автоматически подбирает вас по каталогу. Срыв AUTO-заказа = критично: вы сами «согласились» наличием в прайсе. 2–3 несовпадения → понижение из Надёжного в Песочницу.",
            },
            {
                "title": "🎯 SEMI-режим — подтверждайте подбор оператора в первые 15 минут",
                "subtitle": "Оператор подобрал ваш аналог из каталога и ждёт «да/нет». Долгое молчание = передача другому seller'у. 15 мин — ваш золотой интервал.",
            },
            {
                "title": "📋 Прайс-лист — 16 фиксированных колонок, не отклоняйтесь",
                "subtitle": "OEM · бренд · наличие · lead_time · цена FOB · страна происхождения — без этого позиция не попадает в AUTO. Загрузка через произвольный шаблон = позиции в «отбраковке».",
            },
            {
                "title": "⏱ Резерв 10% оплачен → у вас 24 часа на подтверждение",
                "subtitle": "Это «Деньги поступили» → «Заказ оформлен». Просрочка = автовозврат buyer'у и −SLA балл. Подтверждайте сразу, даже если уточняете детали позже.",
            },
            {
                "title": "🏭 «Производство начато» — не виси в этом статусе >7 дней без апдейта",
                "subtitle": "Покупатель смотрит таймлайн. Молчание 7+ дней → автоматический alert оператору → запрос статуса → потеря репутации. Раз в 2–3 дня — короткое обновление.",
            },
            {
                "title": "✅ «Готовность к проверке» — этап до отгрузки, не пропускайте",
                "subtitle": "Покупатель может: (а) видеоинспекция через оператора, (б) приехать лично, (в) подписать без проверки. Сами выберут — вы готовите партию. Прыжок мимо этого статуса = срыв сценария.",
            },
            {
                "title": "🚚 «Готовность к отгрузке» — маркируйте только когда груз реально в порту/на складе",
                "subtitle": "После этого статуса включается ETA-таймер. Маркировка «преждевременно» → провал по ETA → SLA breach. Маркируйте после физической готовности к погрузке.",
            },
            {
                "title": "💰 Incoterm меняет комиссию платформы: FOB 6% / CIF 8% / DDP 12%",
                "subtitle": "Это платите ВЫ (экспортёр). FOB — если у покупателя свой логист (дешевле для вас). CIF — стандарт. DDP — только за премию к цене, иначе потеря маржи.",
            },
            {
                "title": "🇷🇺 RU-сделка: РФ-агент 2% от RUB + 300 USD таможни (опционально)",
                "subtitle": "Если покупатель в РФ — закладывайте в цену. Не пытайтесь обходить через прямой контакт: блокировка аккаунта + чёрный список операторов.",
            },
            {
                "title": "📊 Рейтинг 60% внешняя + 40% поведение — ручной правки нет",
                "subtitle": "Внешняя (Контур/СПАРК) — это юр-факты, влияет долгосрочно. Поведенческая — SLA + объём − рекламации, меняется быстро. Хотите быстрый рост — закрывайте сделки чисто и в срок.",
            },
            {
                "title": "🚫 Прямой контакт с buyer'ом в обход чата = блок",
                "subtitle": "Платформа отслеживает попытки увести сделку. Один зафиксированный случай = warning, повтор = бан. Все вопросы по сделке — через чат заказа, не в WhatsApp напрямую.",
            },
            {
                "title": "🔄 Эскалация спора — через оператора, не через рекламацию",
                "subtitle": "Если buyer недоволен — сначала «связаться с оператором», потом если не решилось → claim. Рекламация без попытки оператора = −5б рейтинга даже при вашей правоте.",
            },
        ]

        # Текст сообщения — кратко
        next_line = (f"До «{next_tier[1]}»: +{next_tier[2]} баллов."
                     if next_tier else "Максимальный тир — поддерживайте качество.")
        text = (
            f"✓ Компания «{kyb.legal_name or '—'}» верифицирована · {tier} · {score}/100.\n"
            f"{next_line}"
        )

        return ActionResult(
            text=text,
            cards=[
                {
                    "type": "onboarding_progress",
                    "data": {
                        "title": "Верификация компании",
                        "current": 5,
                        "total": 5,
                        "current_label": "Проверено",
                        "status": "✓ Верифицирована",
                        "steps": steps_data,
                    },
                },
                {
                    "type": "kpi_grid",
                    "data": {
                        "title": f"📊 Рейтинг {score}/100 · {tier}",
                        "items": kpi_items,
                    },
                },
                {
                    "type": "list",
                    "data": {
                        "title": "📈 План роста — что повысит рейтинг",
                        "items": boosters,
                    },
                },
                {
                    "type": "list",
                    "data": {
                        "title": "🎓 Поведенческие правила — мини-обучение",
                        "items": tips,
                        # Свёрнуто по умолчанию: 12 пунктов занимают пол-экрана,
                        # не отвлекают от рейтинга и плана роста. Клик по заголовку → разворот.
                        "collapsible": True,
                        "collapsed": True,
                    },
                },
            ],
            actions=[
                {"label": "📝 Обновить реквизиты", "action": "submit_company_info", "params": {}},
                {"label": "📦 Мой каталог",        "action": "seller_warehouses",   "params": {}},
                {"label": "💬 Связаться с менеджером", "action": "contact_operator",
                 "params": {"topic": "kyb"}},
            ],
            contextual_actions=[{"action": "go_home", "label": "🏠 Главная"}],
        )
    if step == "pending":
        return ActionResult(
            text=(
                f"⏳ Анкета отправлена на проверку оператору ({kyb.submitted_at.strftime('%d.%m.%Y %H:%M') if kyb.submitted_at else 'недавно'}).\n"
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

    # Полный список шагов с маркером текущего/пройденных
    STEPS = [
        ("company_info",     "Реквизиты компании"),
        ("legal_address",    "Юридический адрес"),
        ("bank",             "Банковские реквизиты"),
        ("director",         "Директор"),
        ("ready_for_review", "Отправить на проверку"),
    ]
    steps_data = []
    for i, (key, label) in enumerate(STEPS, start=1):
        if i < cur:
            state = "done"
        elif i == cur:
            state = "current"
        else:
            state = "pending"
        steps_data.append({"n": i, "label": label, "state": state})

    return ActionResult(
        text=(
            f"👋 Добро пожаловать! Чтобы заключать сделки на платформе, "
            f"пройдите верификацию компании (KYB)."
        ),
        cards=[{
            "type": "onboarding_progress",
            "data": {
                "title": "Верификация компании",
                "current": cur,
                "total": total,
                "current_label": next_action[1],
                "status": "Черновик",
                "steps": steps_data,
            },
        }],
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


# Карта focus → набор полей формы. None означает «все поля».
_FOCUS_FIELDS = {
    "website":   ["website"],
    "email":     ["contact_email"],
    "phone":     ["phone"],
    "messenger": ["whatsapp", "telegram"],
    "warehouse": ["warehouse_address"],
    "categories": ["categories"],
    "":          None,  # все поля
}

# Метаданные полей (label/type/placeholder/hint) — единый источник правды
_FIELD_META = {
    "website": {
        "label": "Сайт компании", "type": "url",
        "placeholder": "https://example.com",
        "hint": "Покупатели проверят сайт перед сделкой — повышает доверие (+5 баллов внешней оценки)",
    },
    "contact_email": {
        "label": "Контактный email", "type": "email",
        "placeholder": "sales@example.com",
        "hint": "Куда писать по новым запросам",
    },
    "phone": {
        "label": "Телефон компании",
        "placeholder": "+971 50 123 4567",
        "hint": "Оперативная связь с оператором платформы",
    },
    "whatsapp": {
        "label": "WhatsApp",
        "placeholder": "+971 50 123 4567",
        "hint": "Если есть — оператор подтвердит, что номер активен",
    },
    "telegram": {
        "label": "Telegram",
        "placeholder": "@example_company или +971…",
        "hint": "Альтернатива WhatsApp — выберите удобный канал",
    },
    "warehouse_address": {
        "label": "Адрес склада (откуда отгружаете)", "type": "textarea",
        "placeholder": "Город, улица, дом, индекс",
        "hint": "Покупатель видит «откуда едет груз» — важно для оценки логистики",
    },
    "categories": {
        "label": "Бренды и категории запчастей", "type": "textarea",
        "placeholder": "Например: CAT, Komatsu, Volvo CE — ходовая, гидравлика, двигатели",
        "hint": "По чему вас будут искать в каталоге платформы",
    },
}


@register("update_kyb_contacts")
def update_kyb_contacts(params, user, role):
    """Форма «контакты + дополнительные данные компании» — для повышения
    рейтинга после KYB. Заполняется отдельно от обязательных Шагов 1-5.

    params: {focus: 'website'|'messenger'|'email'|'phone'|'warehouse'|'categories'|''}
            confirmed: bool — submit

    focus='docs' → отдельный action `upload_kyb_doc` (файловая загрузка).
    """
    kyb = _kyb(user)
    confirmed = bool(params.get("confirmed"))
    focus = (params.get("focus") or "").strip()

    # docs — особый случай: file upload, не form
    if focus == "docs":
        return upload_kyb_doc({"kind": "dealership"}, user, role)

    if not confirmed:
        focus_titles = {
            "website":   "🌐 Сайт компании",
            "messenger": "💬 Подключить мессенджер",
            "email":     "📧 Контактный email",
            "phone":     "📞 Телефон компании",
            "warehouse": "📍 Адрес склада",
            "categories": "🏷 Бренды и категории",
        }
        title = focus_titles.get(focus, "📝 Дополнительные данные компании")
        intent = (
            "Поле повышает рейтинг и доверие покупателей. После сохранения "
            "вернётесь к статусу — можете заполнить остальные позже."
            if focus else
            "Заполните доступные поля — каждое повышает доверие покупателей "
            "и рейтинг. Можно заполнить только то, что нужно сейчас."
        )
        # Выбираем поля по focus (или все, если focus пустой)
        field_keys = _FOCUS_FIELDS.get(focus)
        if field_keys is None:
            field_keys = list(_FIELD_META.keys())
        fields = []
        for fk in field_keys:
            meta = _FIELD_META.get(fk, {})
            fields.append({
                "name": fk,
                "label": meta.get("label", fk),
                "type": meta.get("type", "text"),
                "placeholder": meta.get("placeholder", ""),
                "value": getattr(kyb, fk, "") or "",
                "hint": meta.get("hint", ""),
            })
        return ActionResult(
            text=title,
            cards=[{"type": "form", "data": {
                "title":         title,
                "intent":        intent,
                "submit_action": "update_kyb_contacts",
                "submit_label":  "Сохранить",
                "fields":        fields,
                "fixed_params":  {"confirmed": True, "focus": focus},
            }}],
            actions=[
                {"label": "← Назад в статус", "action": "kyb_status", "params": {}},
            ],
        )

    # Сохранение — обновляем только поля из focus (или все, если focus пустой)
    field_keys = _FOCUS_FIELDS.get(focus)
    if field_keys is None:
        field_keys = list(_FIELD_META.keys())
    changed = []
    for field in field_keys:
        new_val = (params.get(field) or "").strip()
        if hasattr(kyb, field) and new_val != getattr(kyb, field):
            setattr(kyb, field, new_val[:400 if field == "categories" else 200])
            changed.append(field)
    if changed:
        kyb.save(update_fields=changed + (["updated_at"] if hasattr(kyb, "updated_at") else []))

    return ActionResult(
        text=(f"✓ Сохранено {len(changed)} {'поле' if len(changed)==1 else 'поля'}."
              if changed else "Изменений нет."),
        actions=[
            {"label": "🛡 Вернуться к статусу", "action": "kyb_status", "params": {}},
        ],
    )


@register("upload_kyb_doc")
def upload_kyb_doc(params, user, role):
    """Загрузка KYB-документа (сертификат дилерства, банк, etc).
    params: {kind: 'dealership'|'bank', uploaded: bool}

    Phase 1 (uploaded=False) → карточка со статусом и кнопкой «Загрузить файл»
    Phase 2 (uploaded=True)  → подтверждение после успешной загрузки

    Сам файл грузится через REST endpoint POST /api/assistant/kyb/doc/<kind>/
    """
    kyb = _kyb(user)
    kind = (params.get("kind") or "dealership").strip()

    KIND_META = {
        "dealership": {
            "title": "📜 Сертификаты дилерства",
            "field": "doc_dealership",
            "hint": (
                "Документы, подтверждающие что вы официальный дилер брендов "
                "(CAT, Komatsu, Volvo CE и т.д.). После загрузки оператор "
                "проверит и выдаст бейдж «Официальный дилер» — это сильно "
                "повышает доверие покупателей и приоритет в подборе."
            ),
            "accept": ".pdf,.png,.jpg,.jpeg,.heic",
            "badge_on_approve": "Официальный дилер",
        },
        "bank": {
            "title": "🏦 Банковские реквизиты",
            "field": "doc_bank",
            "hint": "PDF или фото официальной выписки из банка с реквизитами счёта.",
            "accept": ".pdf,.png,.jpg,.jpeg",
            "badge_on_approve": None,
        },
    }
    meta = KIND_META.get(kind, KIND_META["dealership"])
    field = meta["field"]
    current_file = getattr(kyb, field, None)

    # Текущий статус документа
    if current_file and getattr(current_file, "name", ""):
        try:
            fname = current_file.name.split("/")[-1]
            size_kb = round((current_file.size or 0) / 1024, 1)
            status_text = f"✓ Загружен · {fname} · {size_kb} КБ"
            status_tone = "ok"
        except Exception:
            status_text = "✓ Загружен"
            status_tone = "ok"
    else:
        status_text = "Не загружен"
        status_tone = "info"

    rows = [
        {"label": "Документ",   "value": meta["title"].split(" ", 1)[-1]},
        {"label": "Статус",     "value": status_text, "tone": status_tone},
    ]
    if meta.get("badge_on_approve"):
        rows.append({"label": "После одобрения", "value": f"Бейдж «{meta['badge_on_approve']}»", "tone": "info"})

    return ActionResult(
        text=meta["title"],
        cards=[{"type": "draft", "data": {
            "title": meta["title"],
            "intent": meta["hint"],
            "rows": rows,
            # Особый confirm_action — JS подхватит и откроет file-picker,
            # затем сделает multipart POST на endpoint
            "confirm_action": "_upload_kyb_file",
            "confirm_label": ("📤 Заменить файл" if current_file else "📤 Загрузить файл"),
            "confirm_params": {"kind": kind, "accept": meta["accept"]},
            "cancel_label": "Назад",
        }}],
        actions=[
            {"label": "← Назад в статус", "action": "kyb_status", "params": {}},
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
    if not _is_operator(role) and role != "admin":
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    pending = list(CompanyVerification.objects.filter(status="pending").select_related("user").order_by("submitted_at")[:20])
    # Группировка по risk_indicator для приоритизации
    RISK_BADGE = {
        "green":  ("Низкий риск",   "ok"),
        "yellow": ("Средний риск",  "warn"),
        "red":    ("Высокий риск",  "bad"),
    }
    rows = []
    for kyb in pending:
        risk = kyb.risk_indicator or "unknown"
        badge_label, tone = RISK_BADGE.get(risk, ("Не проверено", "info"))
        # Кратко: сколько сигналов в каждой категории
        api = kyb.api_results or {}
        n_red = sum(1 for s in (api.get("aggregator", {}).get("signals") or [])
                      + (api.get("sanctions", {}).get("signals") or [])
                      + (api.get("vies", {}).get("signals") or [])
                      if s.get("level") == "red")
        n_yellow = sum(1 for snap in api.values() if isinstance(snap, dict)
                        for s in (snap.get("signals") or []) if s.get("level") == "yellow")
        flags = []
        if n_red:    flags.append(f"{n_red} красных")
        if n_yellow: flags.append(f"{n_yellow} жёлтых")
        flags_str = " · ".join(flags) if flags else "чисто"
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
        text=f"Очередь KYB · {len(pending)} анкет ждут проверки. Клик по строке → детальная карточка.",
        cards=[{"type": "list", "data": {
            "title": "KYB на модерации",
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
    if not _is_operator(role) and role != "admin":
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    try:
        kyb = CompanyVerification.objects.get(user_id=int(params.get("user_id") or 0))
    except (CompanyVerification.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Анкета не найдена.")

    # ── 1) Данные формы (§2 ТЗ) — сгруппированы по смыслу ──────────
    RISK_RU = {"green": "Низкий", "yellow": "Средний", "red": "Высокий"}
    company_rows = [
        {"label": "Компания", "value": kyb.legal_name or "—", "primary": True, "wide": True},
        {"label": "Страна",   "value": kyb.country or "RU"},
        {"label": "ИНН/Tax ID", "value": kyb.inn or "—"},
        {"label": "VAT", "value": kyb.vat_number or "—"},
        {"label": "ОГРН", "value": kyb.ogrn or "—"},
        {"label": "Категории", "value": (kyb.categories or "—")[:120], "wide": True},
    ]
    address_rows = [
        {"label": "Юр. адрес", "value": (kyb.legal_address or "—")[:120], "wide": True},
        {"label": "Склад", "value": (kyb.warehouse_address or "—")[:120], "wide": True},
    ]
    contact_rows = [
        {"label": "Сайт", "value": kyb.website or "—"},
        {"label": "Email", "value": kyb.contact_email or "—"},
        {"label": "Телефон", "value": kyb.phone or "—"},
        {"label": "WhatsApp", "value": kyb.whatsapp or "—"},
        {"label": "Telegram", "value": kyb.telegram or "—"},
        {"label": "Директор", "value": kyb.director_name or "—"},
    ]
    bank_rows = [
        {"label": "Банк", "value": kyb.bank_name or "—"},
        {"label": "БИК", "value": kyb.bik or "—"},
        {"label": "Счёт", "value": kyb.bank_account or "—", "wide": True},
    ]
    status_rows = [
        {"label": "Статус", "value": kyb.get_status_display()},
        {"label": "Risk", "value": RISK_RU.get(kyb.risk_indicator, "Не определён")},
    ]
    if kyb.submitted_at:
        status_rows.append({"label": "Подана", "value": kyb.submitted_at.strftime("%d.%m.%Y %H:%M")})
    if kyb.auto_checked_at:
        status_rows.append({"label": "Авто-проверка", "value": kyb.auto_checked_at.strftime("%d.%m.%Y %H:%M")})

    form_groups = [
        {"title": "Компания",     "rows": company_rows},
        {"title": "Адреса",       "rows": address_rows},
        {"title": "Контакты",     "rows": contact_rows},
        {"title": "Банк",         "rows": bank_rows},
        {"title": "Статус",       "rows": status_rows},
    ]
    # Совместимость: оставляю старый плоский form_rows для других мест где используется
    form_rows = company_rows + address_rows + contact_rows + bank_rows + status_rows

    # ── 2) Авто-API панель (§3 ТЗ) ──────────────────────────────
    SOURCE_LABELS = {
        "aggregator":     "Контур.Фокус (РФ-агрегатор)",
        "opencorporates": "OpenCorporates (зарубежные)",
        "vies":           "VIES (VAT в ЕС)",
        "sanctions":      "OpenSanctions",
        "maps":           "Яндекс/Google Maps",
        "site":           "Сайт",
        "messenger":      "Мессенджеры",
    }
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
                             "subtitle": "Без замечаний",
                             "tone": "ok"})
            continue
        # 1 строка на сигнал, сгруппировано — цвет передаётся через tone (CSS-акцент слева)
        for sig in signals[:3]:
            lvl = sig.get("level", "info")
            api_rows.append({
                "title": label,
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
    # Чеклист: используем спец-флаг `checkbox: True` чтобы фронт нарисовал
    # настоящий чекбокс вместо символов.
    checklist_rows = [
        {"title": lbl,
         "subtitle": ("Проверено" if checklist_state.get(k)
                       else "Кликнуть → отметить как проверено"),
         "checkbox": True,
         "checked": bool(checklist_state.get(k)),
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
                "title": f"Анкета · {kyb.user.username}",
                "rows": form_rows,
                "confirm_label": "—",
            }},
            {"type": "list", "data": {
                "title": "Авто-проверки (5–7 API за 10 сек)",
                "items": api_rows or [{"title": "Авто-проверки не запускались",
                                         "subtitle": "Использовать submit_for_review для запуска"}],
            }},
            {"type": "list", "data": {
                "title": "Что проверить глазами (2–3 минуты)",
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
    if not _is_operator(role) and role != "admin":
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
    if not _is_operator(role) and role != "admin":
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
    if not _is_operator(role) and role != "admin":
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    confirmed = bool(params.get("confirmed"))
    # FIX (HIGH): защита от TOCTOU race — двое операторов одновременно approve.
    # Используем UPDATE WHERE status='pending' для атомарного захвата:
    # если RowsAffected=0, кто-то уже approve'нул раньше.
    from django.db import transaction
    user_id_int = int(params.get("user_id") or 0)
    try:
        kyb = CompanyVerification.objects.get(user_id=user_id_int)
    except (CompanyVerification.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Анкета не найдена.")

    if kyb.status != "pending":
        return ActionResult(text=f"Анкета не в статусе pending (сейчас: {kyb.get_status_display()}).")

    # Проверка чеклиста — все пункты должны быть отмечены до одобрения
    REQUIRED_CHECKS = [
        ("streetview_ok",     "Склад в Street View"),
        ("reviews_ok",        "Отзывы на картах"),
        ("site_ok",           "Сайт — рабочий магазин"),
        ("bank_ok",           "Реквизиты счёта"),
        ("certs_ok",          "Сертификаты дилерства"),
        ("messenger_test_ok", "Мессенджер отвечает"),
    ]
    state = kyb.operator_checklist or {}
    missing = [lbl for (k, lbl) in REQUIRED_CHECKS if not state.get(k)]

    # Если чеклист НЕ полный — требуем явное подтверждение с предупреждением.
    # Если все 6 пунктов отмечены — одобряем сразу (без промежуточного шага).
    if not confirmed and missing:
        rows = [
            {"label": "Компания", "value": kyb.legal_name, "primary": True},
            {"label": "ИНН", "value": kyb.inn},
            {"label": "Пользователь", "value": kyb.user.username},
            {"label": "Чеклист", "value": f"{len(REQUIRED_CHECKS)-len(missing)} / {len(REQUIRED_CHECKS)} отмечено"},
        ]
        warnings = [
            f"Не отмечено в чеклисте ({len(missing)}): " +
            ", ".join(missing[:3]) + ("…" if len(missing) > 3 else ""),
            "Рекомендуется завершить все пункты перед одобрением.",
        ]
        return ActionResult(
            text=f"Чеклист не завершён — одобрить всё равно?",
            cards=[{"type": "draft", "data": {
                "title": f"Одобрение KYB · {kyb.legal_name}",
                "rows": rows,
                "warnings": warnings,
                "confirm_action": "op_kyb_approve",
                "confirm_label": "Всё равно одобрить",
                "confirm_params": {"user_id": kyb.user_id, "confirmed": True},
                "cancel_label": "Вернуться",
            }}],
        )

    # PIVOT 2026-05-27: capacity guard — оператор не может вести больше 25
    # активных поставщиков (защита от перегрузки).
    from marketplace.models import UserProfile as _UP
    MAX_SUPPLIERS_PER_OP = 25
    if not user.is_staff:  # супер-админу не лимитируем
        current_count = _UP.objects.filter(assigned_operator=user).count()
        if current_count >= MAX_SUPPLIERS_PER_OP:
            return ActionResult(
                text=(
                    f"⚠️ Превышен лимит подопечных поставщиков ({current_count}/{MAX_SUPPLIERS_PER_OP}).\n"
                    "Освободите слот (передайте поставщика коллеге через лида) или попросите "
                    "лида одобрить эту анкету вместо вас."
                ),
                contextual_actions=[
                    {"action": "op_kyb_queue", "label": "← Очередь"},
                ],
            )

    # FIX (HIGH): атомарный захват через UPDATE WHERE status='pending'.
    # Если двое одобряют одновременно — только один получит rows_affected=1.
    rows_affected = CompanyVerification.objects.filter(
        user_id=user_id_int, status="pending",
    ).update(status="verified", reviewed_at=timezone.now(),
              reviewed_by=user, rejection_reason="")
    if rows_affected == 0:
        return ActionResult(text="Анкета уже была обработана другим оператором.")
    kyb.refresh_from_db()

    # Закрепляем поставщика за этим оператором (PIVOT 2026-05-27).
    try:
        _profile, _ = _UP.objects.get_or_create(user=kyb.user, defaults={"role": "seller"})
        _profile.assigned_operator = user
        _profile.save(update_fields=["assigned_operator"])
    except Exception:
        logger.exception("assign_operator on KYB approve failed for user %s", kyb.user_id)

    # ТЗ §1: после verify сразу обновляем external_score из Kontur/СПАРК
    # → bankruptcy_flag/liquidation_flag → status может сразу стать rejected
    rating_info = None
    try:
        from .external_rating import refresh_external_rating
        rating_info = refresh_external_rating(kyb.user)
    except Exception:
        logger.exception("auto-refresh external rating after KYB approve failed")

    # Получаем итоговый рейтинг и статус продавца
    final_rating = None
    final_status = None
    try:
        from marketplace.models import UserProfile
        profile = UserProfile.objects.filter(user=kyb.user).first()
        if profile:
            final_rating = float(profile.rating) if hasattr(profile, "rating") and profile.rating else None
            final_status = (profile.supplier_status if hasattr(profile, "supplier_status") else None)
    except Exception:
        logger.exception("could not fetch final rating after approve")

    _notify(
        kyb.user, kind="system",
        title=f"KYB одобрен · {kyb.legal_name}",
        body="Все возможности платформы теперь доступны: можно отвечать на RFQ, оформлять заказы, управлять каталогом.",
        url="/chat/",
    )

    # Итоговая карточка одобрения с рейтингом
    SUPPLIER_STATUS_RU = {
        "trusted":  "Надёжный",
        "sandbox":  "Песочница (новичок)",
        "risky":    "Рисковый",
        "rejected": "Исключён",
    }
    result_rows = [
        {"label": "Компания", "value": kyb.legal_name, "primary": True},
        {"label": "ИНН", "value": kyb.inn},
        {"label": "Пользователь", "value": kyb.user.username},
    ]
    if final_rating is not None:
        result_rows.append({"label": "Итоговый рейтинг", "value": f"{final_rating:.1f} / 100"})
    if rating_info and rating_info.get("score") is not None:
        result_rows.append({"label": "External score (Kontur)", "value": f"{float(rating_info['score']):.1f} / 100"})
    if final_status:
        result_rows.append({"label": "Статус продавца", "value": SUPPLIER_STATUS_RU.get(final_status, final_status)})
    if missing:
        result_rows.append({"label": "Не отмечено в чеклисте", "value": f"{len(missing)} пунктов"})

    return ActionResult(
        text=f"KYB одобрен · «{kyb.legal_name}» (ИНН {kyb.inn}). Уведомление отправлено.",
        cards=[{"type": "draft", "data": {
            "title": f"Одобрено · {kyb.legal_name}",
            "rows": result_rows,
            "confirm_label": "—",
        }}],
        contextual_actions=[
            {"action": "op_kyb_queue", "label": "← Очередь"},
        ],
    )


@register("op_kyb_reject")
def op_kyb_reject(params, user, role):
    if not _is_operator(role) and role != "admin":
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
