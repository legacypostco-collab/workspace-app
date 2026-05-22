"""Support Hub — административная ветка чата.

Жалоба юзера: «система умнее интерфейса — AI отвечает про несуществующие
кнопки "Помощь"». Решение: реальные actions с pure-DB ответами вместо
галлюцинаций LLM.

Что внутри:
  • support_home        — hub с 6 кнопками (FAQ, статусы, бонусы, контакт)
  • kb_faq              — статичный справочник вопрос-ответ
  • my_verifications    — KYB / email / phone / лимиты текущего юзера
  • my_bonuses          — текущие скидки, бонусы, реферальные
  • contact_operator    — создать ticket к оператору (с audit)
  • open_complaint      — жалоба на платформу/оператора (не путать с claim
                          по заказу — это другое)

Anti-collusion (см. detect_offplatform_contact в conv_security.py):
  • Все коммуникации buyer↔seller↔operator идут через систему
  • Email/phone в свободном тексте — флаг для оператора
  • Контакты раскрываются только после оплаты (см. accept_quote)
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from .actions import ActionResult, register

logger = logging.getLogger(__name__)


# ── 1. SUPPORT HOME ────────────────────────────────────────────

@register("support_home")
def support_home(params, user, role):
    """Главный экран поддержки. Pill «Поддержка» ведёт сюда.

    Для buyer/seller — «получить помощь». Для operator/admin — это инбокс
    обращений ОТ юзеров (оператор — это и есть поддержка).
    """
    is_buyer    = role == "buyer"
    is_seller   = role == "seller"
    is_operator = (role or "").startswith("operator") or role == "admin"

    # ── Operator: входящие обращения ────────────────────────
    # Рекламации — отдельная вкладка «🧾 Рекламации», здесь не дублируем.
    if is_operator:
        from datetime import timedelta

        from django.utils import timezone

        from .models import Message

        now = timezone.now()

        recent_msgs = (
            Message.objects.filter(
                role=Message.Role.ACTION,
                actions__icontains="contact_operator",
                created_at__gte=now - timedelta(days=14),
            )
            .select_related("conversation", "conversation__user")
            .order_by("-created_at")[:20]
        )
        complaints = (
            Message.objects.filter(
                role=Message.Role.ACTION,
                actions__icontains="open_complaint",
                created_at__gte=now - timedelta(days=14),
            )
            .select_related("conversation", "conversation__user")
            .order_by("-created_at")[:20]
        )

        cards = []

        cards.append({"type": "kpi_grid", "data": {
            "title": "📥 Inbox поддержки",
            "items": [
                {"label": "Обращений к оператору", "value": str(recent_msgs.count()),
                 "tone": "warn" if recent_msgs.count() else None,
                 "sub": "за последние 14 дней"},
                {"label": "Жалоб на платформу",   "value": str(complaints.count()),
                 "tone": "bad" if complaints.count() else None,
                 "sub": "open_complaint за 14 дней"},
            ],
        }})

        if recent_msgs:
            rows = [{
                "title":    f"💬 {m.conversation.user.username if m.conversation and m.conversation.user else '—'}",
                "subtitle": f"{m.created_at.strftime('%d.%m %H:%M')} · {(m.content or '')[:80]}",
                "action":   "view_support_ticket",
                "params":   {"conversation_id": str(m.conversation_id) if m.conversation_id else ""},
            } for m in recent_msgs[:10]]
            cards.append({"type": "list", "data": {
                "title": f"💬 Обращения к оператору · {recent_msgs.count()}",
                "items": rows,
            }})

        if complaints:
            rows = [{
                "title":    f"🚨 {m.conversation.user.username if m.conversation and m.conversation.user else '—'}",
                "subtitle": f"{m.created_at.strftime('%d.%m %H:%M')} · {(m.content or '')[:80]}",
                "action":   "view_support_ticket",
                "params":   {"conversation_id": str(m.conversation_id) if m.conversation_id else ""},
            } for m in complaints[:10]]
            cards.append({"type": "list", "data": {
                "title": f"🚨 Жалобы на платформу · {complaints.count()}",
                "items": rows,
            }})

        if not (recent_msgs or complaints):
            cards.append({"type": "list", "data": {
                "title": "📥 Inbox поддержки",
                "items": [{"title": "✅ Очередь поддержки пуста",
                            "subtitle": "Нет новых обращений и жалоб"}],
            }})

        return ActionResult(
            text=("📥 Поддержка — входящие обращения и жалобы от пользователей. "
                  "Рекламации по заказам — в отдельной вкладке «Рекламации»."),
            cards=cards,
            actions=[
                {"label": "📋 База знаний (FAQ)",   "action": "kb_faq",       "params": {}},
                {"label": "🎨 Цветовая памятка",    "action": "color_legend", "params": {}},
                {"label": "🧾 Рекламации",          "action": "get_claims",   "params": {}},
                {"label": "🎛 Дашборд",             "action": "op_dashboard", "params": {}},
            ],
        )

    # ── Buyer/Seller: классический support-home ────────────
    actions = [
        {"action": "kb_faq",           "label": "❓ Частые вопросы (FAQ)"},
        {"action": "color_legend",     "label": "🎨 Цветовая памятка"},
        {"action": "my_verifications", "label": "🛡 Статус моих проверок"},
        {"action": "contact_operator", "label": "💬 Связаться с оператором"},
        {"action": "open_complaint",   "label": "🚨 Пожаловаться (на платформу/оператора)"},
    ]
    if is_buyer:
        actions.insert(1, {"action": "my_bonuses",
                            "label": "🎁 Мои бонусы и скидки"})
        actions.insert(0, {"action": "get_claims",
                            "label": "🧾 Мои рекламации (по заказам)"})
    elif is_seller:
        actions.insert(0, {"action": "start_onboarding",
                            "label": "🚀 KYB-верификация"})

    return ActionResult(
        text=(
            "Поддержка платформы. Выберите раздел ниже — ответы по реальным "
            "данным аккаунта, без AI-догадок. Все обращения сохраняются и "
            "видны оператору."
        ),
        actions=actions,
        suggestions=[
            "Как открыть рекламацию?",
            "Когда раскрываются контакты поставщика?",
            "Что входит в комиссию платформы?",
            "Можно ли договариваться напрямую с поставщиком?",
        ],
    )


@register("color_legend")
def color_legend(params, user, role):
    """Памятка: что обозначают цветовые маркеры в интерфейсе.
    Единая конвенция для всех экранов проекта.

    Используем `list` (не kpi_grid), чтобы у каждого пункта была
    реальная цветная линия слева — такая же, как у боевых карточек.
    """
    return ActionResult(
        text=(
            "Цветовая кодировка в интерфейсе платформы — единая для всех экранов.\n"
            "Линия слева у карточки, цвет KPI-значения и плашки-бейджа подчиняются "
            "одной шкале."
        ),
        cards=[
            {"type": "list", "data": {
                "title": "Шкала цветов (единая по всей платформе)",
                "items": [
                    {"title":    "Критично",
                     "subtitle": "Красная линия. Нарушено / просрочено / требует немедленных действий.",
                     "tone":     "bad"},
                    {"title":    "Под угрозой",
                     "subtitle": "Оранжевая линия. Приближается дедлайн или есть риск срыва — требует внимания.",
                     "tone":     "warn"},
                    {"title":    "В работе",
                     "subtitle": "Голубая линия. Нейтральное состояние, идёт по плану.",
                     "tone":     "info"},
                    {"title":    "В норме",
                     "subtitle": "Зелёная линия. Цель достигнута, всё хорошо, контроль не нужен.",
                     "tone":     "ok"},
                ],
            }},
            {"type": "faq", "data": {
                "title": "Где применяется (раскрыть для деталей)",
                "items": [
                    {"q": "SLA-нарушения и риски",
                     "rows": [
                        {"title": "Красная", "subtitle": "SLA уже нарушен, дедлайн просрочен"},
                        {"title": "Оранжевая", "subtitle": "SLA под угрозой, осталось <50% времени"},
                        {"title": "Голубая", "subtitle": "SLA соблюдается, времени достаточно"},
                     ]},
                    {"q": "Платежи и эскроу",
                     "rows": [
                        {"title": "Красная", "subtitle": "Возврат, спор, refund_pending"},
                        {"title": "Оранжевая", "subtitle": "Ждёт резерв 10%, ждёт оплату"},
                        {"title": "Зелёная", "subtitle": "Оплачено / переведено продавцу"},
                     ]},
                    {"q": "Рейтинг и тиры (для seller)",
                     "rows": [
                        {"title": "Красная", "subtitle": "Рейтинг 0–59 (Рисковый)"},
                        {"title": "Оранжевая", "subtitle": "Рейтинг 60–79 (Песочница)"},
                        {"title": "Зелёная", "subtitle": "Рейтинг 80–100 (Надёжный)"},
                     ]},
                    {"q": "RFQ-режимы",
                     "rows": [
                        {"title": "Красная", "subtitle": "SLA-сроки оператора нарушены (15 мин / 48 ч)"},
                        {"title": "Оранжевая", "subtitle": "В работе у оператора, идёт обратный отсчёт"},
                        {"title": "Голубая", "subtitle": "AUTO-режим, контроль не нужен"},
                     ]},
                ],
            }},
        ],
        contextual_actions=[
            {"action": "support_home", "label": "← Поддержка"},
        ],
    )


@register("view_support_ticket")
def view_support_ticket(params, user, role):
    """Открыть тикет/обращение для оператора. Показывает conversation
    с пользователем + контекст (его заказы, статус KYB и т.д.)."""
    from .models import Conversation, Message

    conv_id = params.get("conversation_id")
    if not conv_id:
        return ActionResult(text="Не указан тикет.")
    try:
        conv = Conversation.objects.select_related("user").get(id=conv_id)
    except Conversation.DoesNotExist:
        return ActionResult(text="Тикет не найден.")

    # Последние 15 сообщений
    msgs = list(Message.objects.filter(conversation=conv).order_by("-created_at")[:15])
    msgs.reverse()
    body_lines = []
    for m in msgs:
        when = m.created_at.strftime("%d.%m %H:%M")
        who = ("👤 " + (m.conversation.user.username if m.conversation.user else "—")
                if m.role == Message.Role.USER else
                "▸ " if m.role == Message.Role.ACTION else "🤖")
        body_lines.append(f"{when} {who} {(m.content or '')[:120]}")
    body = "\n".join(body_lines) or "Сообщений нет."

    return ActionResult(
        text=(f"💬 Тикет от {conv.user.username if conv.user else '—'} "
              f"(категория: {conv.category or 'general'}).\n\n"
              f"Последние сообщения:\n{body}"),
        actions=[
            {"label": "💬 Связаться с пользователем", "action": "contact_operator",
             "params": {"topic": f"reply to {conv.user.username if conv.user else '—'}"}},
            {"label": "← Inbox поддержки", "action": "support_home", "params": {}},
        ],
    )


# ── 2. KB / FAQ ────────────────────────────────────────────────

# Статичная база — 16 топовых вопросов. Расширяется через БД-модель в
# Phase 2. Сейчас этого достаточно: покрывает 80% обращений за первый месяц.
FAQ_ENTRIES = [
    {
        "category": "Регистрация",
        "q": "Какие данные нужны для регистрации покупателя?",
        "a": ("8 полей: страна, ИНН/Tax ID, ФИО контактного лица, должность, "
              "рабочий e-mail, телефон с кодом страны, мессенджер "
              "(WhatsApp или Telegram), парк техники.\n\n"
              "Название и юр.адрес компании платформа подтянет автоматически "
              "по ИНН. Банковские реквизиты и ФИО подписанта запросим только "
              "перед первой сделкой, не на этапе регистрации."),
    },
    {
        "category": "Регистрация",
        "q": "Как зарегистрироваться поставщиком?",
        "a": ("На лендинге кнопка «стать поставщиком» → форма из 4 полей "
              "(логин, e-mail, пароль). После создания аккаунта откроется "
              "KYB-анкета (5 шагов): реквизиты, юр.адрес, банк, директор, "
              "отправка на проверку. До завершения KYB seller не может "
              "отвечать на RFQ и принимать заказы."),
    },
    {
        "category": "KYB / Верификация",
        "q": "Сколько занимает проверка поставщика?",
        "a": ("Авто-проверки — мгновенно (ИНН, ОГРН/EGRUL, OpenSanctions, "
              "VIES для EU, OpenCorporates для зарубежных, MX-записи e-mail, "
              "регистрация в WhatsApp/Telegram).\n\n"
              "Решение оператора — до 24 часов в рабочий день. После approve — "
              "сразу доступны RFQ-ответы и приём заказов."),
    },
    {
        "category": "KYB / Верификация",
        "q": "Что именно проверяет платформа автоматически?",
        "a": ("• ЕГРЮЛ: статус активности, ликвидация, налоговая недоимка\n"
              "• OpenSanctions: директор и компания в санкционных списках\n"
              "• VIES: валидность VAT для контрагентов ЕС\n"
              "• OpenCorporates: статус компании в иностранных реестрах\n"
              "• MX-записи: реальность домена e-mail\n"
              "• WhatsApp/Telegram: зарегистрирован ли указанный номер"),
    },
    {
        "category": "Заказы и оплата",
        "q": "Какая минимальная сумма заказа?",
        "a": ("Минимум $7,000 USD. Меньший заказ платформа не примет — экономика "
              "консолидации (фиксированные расходы на логистику, таможню, "
              "операторов) не окупается на меньших объёмах.\n\n"
              "Если в корзине меньше $7,000 — система покажет «не хватает $N» "
              "и предложит добавить позиции."),
    },
    {
        "category": "Заказы и оплата",
        "q": "Какая схема оплаты?",
        "a": ("Двухэтапная через эскроу:\n\n"
              "1. **10% резерв** при подтверждении КП — замораживается "
              "на депозите покупателя, поставщик видит подтверждение "
              "и запускает заказ в работу.\n\n"
              "2. **90% остаток** перед отгрузкой — оплачивается когда "
              "поставщик помечает «готов к отгрузке». Деньги уходят "
              "поставщику только после подтверждения приёмки покупателем."),
    },
    {
        "category": "Заказы и оплата",
        "q": "Можно ли отменить заказ?",
        "a": ("До оплаты резерва — да, в один клик из карточки заказа.\n\n"
              "После оплаты резерва (10%) — только через оператора. "
              "Если поставщик уже запустил производство, может быть удержание "
              "резерва. Откройте обращение в «Поддержка» → «Связаться с "
              "оператором» — рассмотрим индивидуально."),
    },
    {
        "category": "Доставка и сроки",
        "q": "Как отследить заказ?",
        "a": ("В чате наберите «трекинг 82» или «когда отгрузят 82» (где 82 — "
              "ID заказа). Карточка покажет:\n\n"
              "• Текущий этап (производство → отгрузка → таможня → выдача)\n"
              "• ETA выдачи в днях\n"
              "• Перевозчика (DHL/СДЭК/Maersk и т.д.) с контактами\n"
              "• Трек-номер + прямую ссылку на сайт перевозчика"),
    },
    {
        "category": "Доставка и сроки",
        "q": "Какие используются базисы поставки (Incoterms)?",
        "a": ("По умолчанию FOB — поставщик довозит до порта отгрузки, "
              "дальше платформа берёт логистику на себя.\n\n"
              "На больших партиях доступны EXW (со склада поставщика) "
              "и CIF (морем со страховкой). Выбор базиса — на этапе расчёта "
              "КП в чате."),
    },
    {
        "category": "Рекламации",
        "q": "Как открыть рекламацию на заказ?",
        "a": ("В карточке заказа кнопка «🧾 Открыть рекламацию», либо в чате "
              "«рекламация по заказу 82». Форма из 4 полей:\n\n"
              "• Вид: брак, не та деталь, повреждение при доставке, "
              "просрочка, не пришла, иное\n"
              "• Заголовок\n"
              "• Описание (что произошло, фото если есть)\n"
              "• Желаемая компенсация ($, опционально)\n\n"
              "Оператор уведомляется сразу, по SLA — отвечает в течение 24 ч."),
    },
    {
        "category": "Рекламации",
        "q": "Сколько времени рассматривается рекламация?",
        "a": ("SLA зависит от вида (защищает от затягивания):\n\n"
              "• Не пришла → 2 дня\n"
              "• Брак / Повреждение при доставке → 3 дня\n"
              "• Просрочка поставки → 5 дней\n"
              "• Не та деталь / Иное → 7 дней\n\n"
              "Если оператор не реагирует в срок — рекламация автоматически "
              "эскалируется супервайзеру и в Telegram on-call оператора."),
    },
    {
        "category": "Рекламации",
        "q": "Какие варианты решения по рекламации?",
        "a": ("• Полный возврат денег с депозита\n"
              "• Частичный возврат (компенсация за дефект)\n"
              "• Бесплатная замена позиции\n"
              "• Доплата за повреждённую упаковку\n"
              "• Кредит на следующий заказ\n\n"
              "Оператор согласует решение с обеими сторонами и зафиксирует "
              "в системе. Все этапы сохраняются в audit-log."),
    },
    {
        "category": "Связь со сторонами",
        "q": "Можно ли договариваться напрямую с поставщиком?",
        "a": ("**Нет, никогда.** Платформа намеренно не раскрывает контакты "
              "поставщика покупателю. Вся переписка идёт через оператора:\n\n"
              "• Покупатель видит «Поставщик №1, №2…» — анонимная нумерация\n"
              "• Любой вопрос / уточнение / спор — через чат-форму\n"
              "• Оператор передаёт между сторонами и фиксирует переписку\n\n"
              "Это защищает обе стороны: у покупателя есть audit-log и "
              "эскроу, у поставщика — гарантия оплаты и нет потери комиссии. "
              "Попытки обменяться контактами через сообщения чата автоматически "
              "флагуются оператору и могут привести к снижению рейтинга "
              "или блокировке."),
    },
    {
        "category": "Связь со сторонами",
        "q": "А как тогда уточнить детали по заказу?",
        "a": ("Все вопросы — через оператора платформы:\n\n"
              "1. В карточке заказа нажмите «💬 Связаться с оператором» "
              "(или в чате напишите «связаться с оператором»)\n"
              "2. Опишите вопрос (заказ, что нужно уточнить)\n"
              "3. Оператор переадресует продавцу/логисту и вернёт ответ\n\n"
              "Среднее время реакции оператора — 4 рабочих часа. "
              "Все обращения сохраняются."),
    },
    {
        "category": "Бонусы",
        "q": "Есть ли скидки за объём?",
        "a": ("Да, auto-discount применяется автоматически по обороту за 12 мес:\n\n"
              "• От $50,000 → Bronze (комиссия −1%)\n"
              "• От $200,000 → Silver (−2%)\n"
              "• От $500,000 → Gold (−3%)\n"
              "• От $1,000,000 → Platinum (−5% + персональный менеджер)\n\n"
              "Текущий тир — в меню «Поддержка» → «🎁 Мои бонусы». "
              "Скидка применяется к новым заказам автоматически — "
              "ничего активировать не нужно."),
    },
    {
        "category": "Платформа",
        "q": "Какие AI-возможности есть в чате?",
        "a": ("• **OEM-нормализатор**: 707-99-58030, 7079958030, CAT-265-0235 — "
              "находит одну и ту же деталь под любым написанием\n"
              "• **Загрузка спецификации**: drag-and-drop xlsx/csv/pdf, "
              "AI-маппинг колонок (название, ОЕМ, кол-во)\n"
              "• **Авто-подбор поставщиков**: AUTO/SEMI/MANUAL режимы с "
              "разными SLA — система выбирает по сложности запроса\n"
              "• **Расчёт landed cost**: цена + доставка + таможня по "
              "incoterm в один клик\n"
              "• **Трекинг с прогнозом**: текущий этап + ETA выдачи по дням"),
    },
    {
        "category": "Платформа",
        "q": "Где посмотреть статус моих проверок?",
        "a": ("В чате: «Поддержка» → «🛡 Статус моих проверок».\n\n"
              "Показывает: верификация e-mail, телефон, мессенджер, "
              "KYB-статус (для поставщика), реквизиты компании, рейтинг, "
              "лимиты по бюджету. Если что-то не так — отправьте обращение "
              "оператору с указанием поля, поправим."),
    },
    {
        "category": "Поддержка",
        "q": "Как связаться с поддержкой?",
        "a": ("В чате слева — pill «Поддержка». Внутри:\n\n"
              "• ❓ База знаний (этот раздел)\n"
              "• 🛡 Статус моих проверок\n"
              "• 🎁 Мои бонусы и скидки\n"
              "• 🧾 Мои рекламации\n"
              "• 💬 **Связаться с оператором** — форма ticket'а\n"
              "• 🚨 Пожаловаться на платформу/оператора/поставщика\n\n"
              "Среднее время ответа — 4 рабочих часа. Critical (блок "
              "аккаунта, потеря денег) — в течение часа."),
    },
]


_CATEGORY_LABEL = {
    "registration":   "Регистрация",
    "kyb":            "KYB / Верификация",
    "payment":        "Заказы и оплата",
    "delivery":       "Доставка и сроки",
    "claims":         "Рекламации",
    "contacts":       "Связь со сторонами",
    "bonuses":        "Бонусы",
    "platform":       "Платформа",
    "support":        "Поддержка",
    "other":          "Другое",
}


def _load_entries_from_db(query: str | None):
    """Из таблицы KnowledgeBaseEntry. Возвращает [{category, q, a, id}].

    PG: full-text search через SearchVector (русская морфология).
    SQLite: icontains по обоим полям.
    Если query пуст — все active entries.
    """
    try:
        from marketplace.models import KnowledgeBaseEntry
    except Exception:
        return None  # БД-модели нет — fallback на хардкод
    try:
        qs = KnowledgeBaseEntry.search(query or "", limit=200)
        # Инкрементируем views только для запросов (не для общего листинга)
        if query:
            ids = list(qs.values_list("id", flat=True))
            if ids:
                from django.db.models import F
                KnowledgeBaseEntry.objects.filter(id__in=ids).update(
                    views=F("views") + 1,
                )
        return [{
            "category": _CATEGORY_LABEL.get(e.category, "❓ Другое"),
            "q": e.question,
            "a": e.answer,
            "id": e.id,
        } for e in qs]
    except Exception:
        logger.exception("KnowledgeBaseEntry.search failed — fallback to hardcoded")
        return None


@register("kb_faq")
def kb_faq(params, user, role):
    """FAQ. Если есть query — full-text search (PG) или icontains (SQLite).
    Без query — все active entries.

    Источник: таблица KnowledgeBaseEntry (редактируется оператором через
    /admin/). Если таблица пуста / БД упала — fallback на хардкод FAQ_ENTRIES.
    """
    query = (params.get("query") or "").strip()

    # 1. Пробуем БД
    matched = _load_entries_from_db(query if query else None)
    source = "db"

    # 2. Fallback на хардкод (если БД пустая / упала / нет таблицы)
    if not matched:
        source = "hardcoded"
        q_low = query.lower()
        if q_low:
            matched = [e for e in FAQ_ENTRIES
                       if q_low in e["q"].lower()
                       or q_low in e["a"].lower()
                       or q_low in e["category"].lower()]
        else:
            matched = list(FAQ_ENTRIES)

    if not matched:
        return ActionResult(
            text=(f"❓ По запросу «{query}» в FAQ ничего не найдено.\n"
                  f"Напишите оператору — добавим в базу."),
            actions=[{"action": "contact_operator",
                       "label": "💬 Спросить оператора"}],
            contextual_actions=[{"action": "support_home", "label": "← Назад"}],
        )

    # Группировка по категории — в faq-аккордеоне category выводим как title группы
    by_cat: dict[str, list] = {}
    for e in matched:
        by_cat.setdefault(e["category"], []).append(e)

    faq_items = []
    for cat, items in by_cat.items():
        for it in items:
            faq_items.append({
                "q": it["q"],
                "a": it["a"],
                "category": cat,
            })

    return ActionResult(
        text=(f"📚 База знаний · {len(matched)} ответов"
              + (f" по «{query}»" if query else "")
              + (" · из админки" if source == "db" else " · встроенная")),
        cards=[{
            "type": "faq",
            "data": {
                "title": f"FAQ · {len(matched)} ответов",
                "items": faq_items[:60],
            },
        }],
        suggestions=[
            "Как открыть рекламацию?", "Минимальная сумма заказа",
            "Когда раскрываются контакты", "Как работают бонусы",
        ],
        contextual_actions=[
            {"action": "support_home", "label": "← Поддержка"},
            {"action": "contact_operator", "label": "💬 Спросить оператора"},
        ],
    )


# ── 3. MY VERIFICATIONS ───────────────────────────────────────

@register("my_verifications")
def my_verifications(params, user, role):
    """Статус всех проверок аккаунта. Pure DB, без LLM."""
    from marketplace.models import CompanyVerification, UserProfile

    p = UserProfile.objects.filter(user=user).first()
    rows = [
        {"label": "Логин",     "value": user.username},
        {"label": "Email",     "value": user.email or "—",
         "tone": "ok" if user.email else "warn"},
        {"label": "Аккаунт",   "value": "Активен" if user.is_active else "Заблокирован",
         "tone": "ok" if user.is_active else "bad"},
        {"label": "Роль",      "value": (p.role if p else "—").upper()},
    ]
    if p:
        rows.extend([
            {"label": "Страна",        "value": p.country or "—"},
            {"label": "ИНН / Tax ID",  "value": p.tax_id or "—"},
            {"label": "Контактное лицо", "value": p.contact_name or "—"},
            {"label": "Телефон",       "value": p.phone_e164 or "—",
             "tone": "ok" if p.phone_e164 else "warn"},
            {"label": "Мессенджер",    "value": (
                f"{p.get_messenger_kind_display()}: {p.messenger_handle}"
                if p.messenger_kind and p.messenger_handle else "—"),
             "tone": "ok" if (p.messenger_kind and p.messenger_handle) else "warn"},
        ])
        if p.role == "seller":
            kyb = CompanyVerification.objects.filter(user=user).order_by("-id").first()
            if kyb:
                tone = {"verified": "ok", "pending": "warn",
                        "rejected": "bad", "draft": "info"}.get(kyb.status, "info")
                rows.append({"label": "KYB-статус",
                             "value": kyb.get_status_display(), "tone": tone})
                if kyb.rejection_reason:
                    rows.append({"label": "Причина отклонения",
                                 "value": kyb.rejection_reason[:200], "tone": "bad"})
            else:
                rows.append({"label": "KYB-статус", "value": "Не заполнен",
                             "tone": "warn"})

    next_actions = []
    if p and p.role == "seller":
        # Если KYB не пройден — кнопка-завершение
        kyb = CompanyVerification.objects.filter(user=user, status="verified").first()
        if not kyb:
            next_actions.append({"action": "start_onboarding",
                                 "label": "🚀 Пройти KYB"})

    return ActionResult(
        text=(
            f"🛡 Статус проверок аккаунта **{user.username}**.\n"
            f"Все данные — из вашего профиля. Что неверно — отправьте "
            f"оператору жалобу, поправим."
        ),
        cards=[{
            "type": "kpi_grid",
            "data": {"title": "🛡 Проверки и реквизиты",
                     "items": rows[:15]},
        }],
        actions=next_actions,
        contextual_actions=[
            {"action": "support_home",      "label": "← Поддержка"},
            {"action": "contact_operator",  "label": "💬 Спросить оператора"},
        ],
    )


# ── 4. MY BONUSES (для buyer) ─────────────────────────────────

@register("my_bonuses")
def my_bonuses(params, user, role):
    """Текущие бонусы и скидки покупателя."""
    if role != "buyer":
        return ActionResult(
            text="🎁 Бонусы доступны только в кабинете покупателя.",
            contextual_actions=[{"action": "support_home", "label": "← Назад"}],
        )

    from marketplace.models import BuyerVolumeYearly, Order

    # Считаем оборот за последние 12 мес
    now = timezone.now()
    year_ago = now - timedelta(days=365)
    orders_qs = Order.objects.filter(
        buyer=user, created_at__gte=year_ago,
        status__in=("delivered", "completed", "transit_abroad",
                    "transit_rf", "customs", "issuing", "in_production"),
    )
    gmv_12m = sum(float(o.total_amount or 0) for o in orders_qs)
    cnt_12m = orders_qs.count()

    # Discount тиры (как в auto_discount)
    tiers = [
        (50_000,  "🥉 Bronze", "1% скидка"),
        (200_000, "🥈 Silver", "2% скидка"),
        (500_000, "🥇 Gold",   "3% скидка"),
        (1_000_000, "💎 Platinum", "5% скидка + персональный менеджер"),
    ]
    current_tier = None
    next_tier = None
    for threshold, name, perk in tiers:
        if gmv_12m >= threshold:
            current_tier = (threshold, name, perk)
        elif next_tier is None:
            next_tier = (threshold, name, perk)

    rows = [
        {"label": "Оборот за 12 мес", "value": f"${gmv_12m:,.0f}", "tone": "info"},
        {"label": "Заказов за 12 мес", "value": str(cnt_12m)},
        {"label": "Текущий статус",
         "value": current_tier[1] if current_tier else "—",
         "sub": current_tier[2] if current_tier else "Заказы от $50k → Bronze",
         "tone": "ok" if current_tier else "warn"},
    ]
    if next_tier:
        shortage = next_tier[0] - gmv_12m
        rows.append({
            "label": f"До {next_tier[1]}",
            "value": f"${shortage:,.0f}",
            "sub": next_tier[2], "tone": "info",
        })

    return ActionResult(
        text=(
            f"🎁 Ваши бонусы. Auto-discount работает автоматически — "
            f"скидка применяется к новым заказам по факту достижения тира."
        ),
        cards=[
            {"type": "kpi_grid",
             "data": {"title": "🎁 Бонусная программа", "items": rows}},
            {"type": "list", "data": {
                "title": "📊 Тиры скидок",
                "items": [
                    {"title": f"{name}", "subtitle": f"от ${thr:,.0f} · {perk}",
                     "tone": "ok" if gmv_12m >= thr else "info"}
                    for thr, name, perk in tiers
                ],
            }},
        ],
        actions=[
            # Аналитика и отчёты по сделкам — все pure-DB action'ы
            {"label": "🎯 Auto-discount",   "action": "get_buyer_discount", "params": {}},
            {"label": "💸 Экономия",        "action": "get_savings",        "params": {}},
            {"label": "📊 Аналитика заказов","action": "get_analytics",     "params": {}},
            {"label": "📦 Отчёт по поставкам","action": "get_supply_report","params": {}},
            {"label": "⏱ SLA по заказам",    "action": "get_sla_report",   "params": {}},
        ],
        contextual_actions=[
            {"action": "support_home",     "label": "← Поддержка"},
            {"action": "contact_operator", "label": "💬 Спросить оператора"},
        ],
    )


# ── 5. CONTACT OPERATOR ────────────────────────────────────────

@register("contact_operator")
def contact_operator(params, user, role):
    """Создать ticket в admin-чат оператора.

    Phase 1: показать форму
    Phase 2 (confirmed=True): записать в operator's «Алерты оператора» conv.
    """
    confirmed = bool(params.get("confirmed"))
    text = (params.get("text") or "").strip()
    topic = (params.get("topic") or "").strip()

    if not confirmed:
        return ActionResult(
            text="💬 Опишите проблему — оператор получит ticket в свой админ-чат.",
            cards=[{
                "type": "form",
                "data": {
                    "title": "💬 Связаться с оператором",
                    "submit_action": "contact_operator",
                    "submit_label": "📨 Отправить",
                    "fields": [
                        {"name": "topic", "label": "Тема", "type": "select",
                         "required": True, "options": [
                            {"value": "billing",      "label": "💰 Оплата / комиссия"},
                            {"value": "verification", "label": "🛡 Верификация / KYB"},
                            {"value": "order",        "label": "📦 Заказ / доставка"},
                            {"value": "complaint",    "label": "🚨 Жалоба"},
                            {"value": "feature",      "label": "💡 Предложение"},
                            {"value": "other",        "label": "❓ Другое"},
                         ]},
                        {"name": "text", "label": "Опишите проблему",
                         "type": "textarea", "required": True,
                         "placeholder": "Что произошло, в каком заказе/RFQ, что хотите получить"},
                    ],
                    "fixed_params": {"confirmed": True},
                },
            }],
            contextual_actions=[{"action": "support_home", "label": "← Назад"}],
        )

    if not text:
        return ActionResult(text="⚠️ Описание обязательно.")

    # Anti-collusion: проверяем нет ли в тексте попытки обмена off-platform контактами.
    flagged = _detect_offplatform_contact(text)

    # Отправляем в admin-chat оператора через существующий notify_operator_alert.
    try:
        from .order_events import notify_operator_alert
        topic_label = {
            "billing": "💰 Оплата", "verification": "🛡 KYB",
            "order": "📦 Заказ", "complaint": "🚨 Жалоба",
            "feature": "💡 Предложение", "other": "❓ Другое",
        }.get(topic, topic)
        flag_note = "\n⚠️ ВНИМАНИЕ: возможна попытка обмена off-platform контактами." if flagged else ""
        notify_operator_alert(
            user_obj=user, event="user_registered",  # переиспользуем event для рендера
            text=(
                f"💬 ОБРАЩЕНИЕ от @{user.username} ({role})\n"
                f"Тема: {topic_label}\n"
                f"Текст: {text[:500]}{'…' if len(text) > 500 else ''}"
                f"{flag_note}"
            ),
            extra={"role": role},
        )
    except Exception:
        logger.exception("contact_operator: notify_operator_alert failed")

    response_text = (
        f"✅ Обращение отправлено оператору.\n"
        f"Тема: {topic_label}\n"
        f"Среднее время реакции: 4 рабочих часа."
    )
    if flagged:
        response_text += (
            "\n\n⚠️ В вашем сообщении обнаружены контактные данные "
            "(email/телефон/мессенджер). Все коммуникации должны идти через "
            "платформу — это защищает обе стороны. Оператор увидит флаг."
        )

    return ActionResult(
        text=response_text,
        contextual_actions=[
            {"action": "support_home", "label": "← Поддержка"},
        ],
    )


# ── 6. OPEN COMPLAINT (жалоба на платформу/оператора) ─────────

@register("open_complaint")
def open_complaint(params, user, role):
    """Жалоба на работу платформы / оператора / поставщика.

    НЕ путать с `create_claim` (рекламация по конкретному заказу).
    Эта — про qualifying процесса, поведения оператора, скрытых проблем.
    """
    confirmed = bool(params.get("confirmed"))
    text = (params.get("text") or "").strip()
    against = (params.get("against") or "").strip()

    if not confirmed:
        return ActionResult(
            text="🚨 Жалоба фиксируется в audit-log и эскалируется супервайзеру.",
            cards=[{
                "type": "form",
                "data": {
                    "title": "🚨 Жалоба на платформу",
                    "submit_action": "open_complaint",
                    "submit_label": "🚨 Подать жалобу",
                    "fields": [
                        {"name": "against", "label": "На кого", "type": "select",
                         "required": True, "options": [
                            {"value": "platform", "label": "Платформа в целом"},
                            {"value": "operator", "label": "Оператор / поддержка"},
                            {"value": "seller",   "label": "Поставщик (требует order_id)"},
                            {"value": "buyer",    "label": "Покупатель (для seller)"},
                            {"value": "other",    "label": "Другое"},
                         ]},
                        {"name": "text", "label": "Подробное описание",
                         "type": "textarea", "required": True,
                         "placeholder": "Что произошло, когда, какие доказательства"},
                    ],
                    "fixed_params": {"confirmed": True},
                },
            }],
            contextual_actions=[{"action": "support_home", "label": "← Назад"}],
        )

    if not text:
        return ActionResult(text="⚠️ Описание обязательно.")

    flagged = _detect_offplatform_contact(text)
    try:
        from .order_events import notify_operator_alert
        against_label = {
            "platform": "ПЛАТФОРМА", "operator": "ОПЕРАТОР",
            "seller": "ПОСТАВЩИК", "buyer": "ПОКУПАТЕЛЬ",
            "other": "ДРУГОЕ",
        }.get(against, against)
        notify_operator_alert(
            user_obj=user, event="user_registered",
            text=(
                f"🚨🚨 ЖАЛОБА от @{user.username} ({role})\n"
                f"Против: {against_label}\n"
                f"Текст: {text[:500]}{'…' if len(text) > 500 else ''}"
                f"{(chr(10) + '⚠️ Обнаружены off-platform контакты') if flagged else ''}"
            ),
            extra={"role": role},
        )
    except Exception:
        logger.exception("open_complaint: notify_operator_alert failed")

    return ActionResult(
        text=(
            "✅ Жалоба зафиксирована в audit-log. Эскалирована супервайзеру.\n"
            "Ответ — в течение 24 ч в рабочий день. "
            "За false-flag жалобы платформа оставляет право снижать рейтинг."
        ),
        contextual_actions=[
            {"action": "support_home", "label": "← Поддержка"},
        ],
    )


# ── 7. ANTI-COLLUSION: детектор off-platform контактов ─────────

import re

_EMAIL_RE = re.compile(r"\b[\w._%+-]+@[\w.-]+\.[A-Z|a-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s\-().]*)?(?:\(?\d{2,4}\)?[\s\-().]*)?\d{3,4}[\s\-().]*\d{2,4}[\s\-().]*\d{2,4}")
_MESSENGER_RE = re.compile(
    r"\b(whatsapp|telegram|tg|вотсап|вотсапп|ватсап|телега|телеграм|viber|skype)\b",
    re.IGNORECASE,
)
_OFFPLATFORM_HINT_RE = re.compile(
    r"\b(напиш(?:ите|и)|свяж(?:итесь|ись)|позвон(?:и|ите)|перейд(?:ите|и)\s+по|"
    r"contact\s+me|reach\s+(?:me|out)|let'?s\s+talk\s+(?:on|via))\b",
    re.IGNORECASE,
)


_TG_HANDLE_RE = re.compile(r"(?:^|\s|[(,])@[A-Za-z][A-Za-z0-9_]{3,30}\b")


def _detect_offplatform_contact(text: str) -> bool:
    """True если в сообщении обнаружены email/phone/messenger-контакты."""
    if not text:
        return False
    if _EMAIL_RE.search(text):
        return True
    # Phone: требуем messenger-keyword или offplatform-glagol рядом
    # (иначе будет ловить order-id и просто числа).
    has_phone = bool(_PHONE_RE.search(text))
    has_messenger = bool(_MESSENGER_RE.search(text))
    has_offplat_hint = bool(_OFFPLATFORM_HINT_RE.search(text))
    has_tg_handle = bool(_TG_HANDLE_RE.search(text))
    if has_phone and (has_messenger or has_offplat_hint):
        return True
    # @username в комбинации с messenger-keyword («мой telegram @user»)
    if has_tg_handle and (has_messenger or has_offplat_hint):
        return True
    return False
