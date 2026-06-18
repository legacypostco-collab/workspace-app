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
from django.utils.translation import gettext as _

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

        # Inbox поддержки — реальные обращения от клиентов (buyer/seller).
        # «Реальное обращение» = последнее USER-сообщение с текстом ≥10 символов
        # за последние 14 дней (не клик «Связаться с оператором», а реальный
        # вопрос/жалоба). Служебные аккаунты (is_staff/is_superuser) скрываем.
        from .models import Conversation
        from django.db.models import Q, Max, OuterRef, Subquery
        cutoff_14 = now - timedelta(days=14)

        # Подзапрос: ID последнего USER-сообщения с осмысленным текстом по каждому conv
        last_user_msg_sub = (Message.objects
            .filter(conversation=OuterRef("pk"), role=Message.Role.USER)
            .exclude(content__isnull=True).exclude(content="")
            .order_by("-created_at"))

        # Все conv в категории support с хотя бы одним user-сообщением длиннее 10 символов
        ticket_convs = list(Conversation.objects
            .filter(category="support", updated_at__gte=cutoff_14)
            .exclude(user__is_staff=True)
            .exclude(user__is_superuser=True)
            .annotate(
                last_user_msg_id=Subquery(last_user_msg_sub.values("id")[:1]),
                last_user_text=Subquery(last_user_msg_sub.values("content")[:1]),
                last_user_at=Subquery(last_user_msg_sub.values("created_at")[:1]),
            )
            .filter(last_user_msg_id__isnull=False)
            .select_related("user")
            .order_by("-last_user_at")[:20])
        # Дополнительно: фильтр на минимальную длину текста (>10 символов = настоящий вопрос)
        ticket_convs = [c for c in ticket_convs if c.last_user_text and len(c.last_user_text.strip()) >= 10]

        complaints = (
            Message.objects.filter(
                role=Message.Role.ACTION,
                actions__icontains="open_complaint",
                created_at__gte=cutoff_14,
            )
            .exclude(conversation__user__is_staff=True)
            .exclude(conversation__user__is_superuser=True)
            .select_related("conversation", "conversation__user")
            .order_by("-created_at")[:20]
        )

        cards = []

        cards.append({"type": "kpi_grid", "data": {
            "title": _("📥 Inbox поддержки"),
            "items": [
                {"label": _("Обращений к оператору"), "value": str(len(ticket_convs)),
                 "tone": "warn" if ticket_convs else None,
                 "sub": _("с реальным вопросом за 14 дней")},
                {"label": _("Жалоб на платформу"),   "value": str(complaints.count()),
                 "tone": "bad" if complaints.count() else None,
                 "sub": _("поданных через форму за 14 дней")},
            ],
        }})

        if ticket_convs:
            rows = []
            for c in ticket_convs[:10]:
                uname = c.user.username if c.user else "—"
                text_snippet = (c.last_user_text or "").strip().replace("\n", " ")[:90]
                when = c.last_user_at.strftime("%d.%m %H:%M") if c.last_user_at else ""
                rows.append({
                    "title":    f"💬 {uname}",
                    "subtitle": f"{when} · «{text_snippet}…»" if len(c.last_user_text or "") > 90
                                  else f"{when} · «{text_snippet}»",
                    "action":   "view_support_ticket",
                    "params":   {"conversation_id": str(c.id)},
                })
            cards.append({"type": "list", "data": {
                "title": _('💬 Обращения к оператору · %(p0)s') % {"p0": f'{len(ticket_convs)}'},
                "items": rows,
            }})

        if complaints:
            rows = []
            seen_convs = set()
            for m in complaints:
                if m.conversation_id in seen_convs: continue
                seen_convs.add(m.conversation_id)
                uname = m.conversation.user.username if m.conversation and m.conversation.user else "—"
                # Берём текст жалобы из params открытия формы
                complaint_text = ""
                try:
                    for a in (m.actions or []):
                        if a.get("action") == "open_complaint":
                            complaint_text = (a.get("params") or {}).get("text") or ""
                            break
                except Exception:
                    pass
                snippet = complaint_text.strip().replace("\n", " ")[:90]
                when = m.created_at.strftime("%d.%m %H:%M")
                rows.append({
                    "title":    f"🚨 {uname}",
                    "subtitle": (f"{when} · «{snippet}…»" if len(complaint_text) > 90
                                   else (f"{when} · «{snippet}»" if snippet
                                            else f"{when} · жалоба подана через форму")),
                    "action":   "view_support_ticket",
                    "params":   {"conversation_id": str(m.conversation_id)},
                })
            cards.append({"type": "list", "data": {
                "title": _('🚨 Жалобы на платформу · %(p0)s') % {"p0": f'{len(rows)}'},
                "items": rows,
            }})

        if not (ticket_convs or complaints):
            cards.append({"type": "list", "data": {
                "title": _("📥 Inbox поддержки"),
                "items": [{"title": _("✅ Очередь поддержки пуста"),
                            "subtitle": _("Нет новых обращений и жалоб")}],
            }})

        return ActionResult(
            text=("📥 Поддержка — входящие обращения и жалобы от пользователей. "
                  "Рекламации по заказам — в отдельной вкладке «Рекламации»."),
            cards=cards,
            actions=[
                {"label": _("📂 Мои диалоги с пользователями"),
                 "action": "op_my_user_chats", "params": {}},
                {"label": _("📋 База знаний (FAQ)"),   "action": "kb_faq",       "params": {}},
                {"label": _("🎨 Цветовая памятка"),    "action": "color_legend", "params": {}},
                {"label": _("🧾 Рекламации"),          "action": "get_claims",   "params": {}},
                {"label": _("🎛 Дашборд"),             "action": "op_dashboard", "params": {}},
            ],
        )

    # ── Buyer/Seller: классический support-home ────────────
    actions = [
        {"action": "kb_faq",           "label": _("❓ Частые вопросы (FAQ)")},
        {"action": "color_legend",     "label": _("🎨 Цветовая памятка")},
        {"action": "my_verifications", "label": _("🛡 Статус моих проверок")},
        {"action": "contact_operator", "label": _("💬 Связаться с оператором")},
        {"action": "open_complaint",   "label": _("🚨 Пожаловаться (на платформу/оператора)")},
    ]
    if is_buyer:
        actions.insert(1, {"action": "my_bonuses",
                            "label": _("🎁 Мои бонусы и скидки")})
        actions.insert(0, {"action": "get_claims",
                            "label": _("🧾 Мои рекламации (по заказам)")})
    elif is_seller:
        actions.insert(0, {"action": "start_onboarding",
                            "label": _("🚀 KYB-верификация")})

    return ActionResult(
        text=(
            "Чем помочь? Выберите раздел ниже — ответим по вашим заказам и "
            "данным. Любой вопрос дойдёт до оператора."
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
                "title": _("Шкала цветов (единая по всей платформе)"),
                "items": [
                    {"title":    _("Критично"),
                     "subtitle": _("Красная линия. Нарушено / просрочено / требует немедленных действий."),
                     "tone":     "bad"},
                    {"title":    _("Под угрозой"),
                     "subtitle": _("Оранжевая линия. Приближается дедлайн или есть риск срыва — требует внимания."),
                     "tone":     "warn"},
                    {"title":    _("В работе"),
                     "subtitle": _("Голубая линия. Нейтральное состояние, идёт по плану."),
                     "tone":     "info"},
                    {"title":    _("В норме"),
                     "subtitle": _("Зелёная линия. Цель достигнута, всё хорошо, контроль не нужен."),
                     "tone":     "ok"},
                ],
            }},
            {"type": "faq", "data": {
                "title": _("Где применяется (раскрыть для деталей)"),
                "items": [
                    {"q": "SLA-нарушения и риски",
                     "rows": [
                        {"title": _("Красная"), "subtitle": _("SLA уже нарушен, дедлайн просрочен")},
                        {"title": _("Оранжевая"), "subtitle": _("SLA под угрозой, осталось <50% времени")},
                        {"title": _("Голубая"), "subtitle": _("SLA соблюдается, времени достаточно")},
                     ]},
                    {"q": "Платежи и эскроу",
                     "rows": [
                        {"title": _("Красная"), "subtitle": _("Возврат, спор, refund_pending")},
                        {"title": _("Оранжевая"), "subtitle": _("Ждёт резерв 10%, ждёт оплату")},
                        {"title": _("Зелёная"), "subtitle": _("Оплачено / переведено продавцу")},
                     ]},
                    {"q": "Рейтинг и тиры (для seller)",
                     "rows": [
                        {"title": _("Красная"), "subtitle": _("Рейтинг 0–59 (Рисковый)")},
                        {"title": _("Оранжевая"), "subtitle": _("Рейтинг 60–79 (Песочница)")},
                        {"title": _("Зелёная"), "subtitle": _("Рейтинг 80–100 (Надёжный)")},
                     ]},
                    {"q": "RFQ-режимы",
                     "rows": [
                        {"title": _("Красная"), "subtitle": _("SLA-сроки оператора нарушены (15 мин / 48 ч)")},
                        {"title": _("Оранжевая"), "subtitle": _("В работе у оператора, идёт обратный отсчёт")},
                        {"title": _("Голубая"), "subtitle": _("AUTO-режим, контроль не нужен")},
                     ]},
                ],
            }},
        ],
        contextual_actions=[
            {"action": "support_home", "label": _("← Поддержка")},
        ],
    )


@register("view_support_ticket")
def view_support_ticket(params, user, role):
    """Карточка тикета поддержки — ТОЛЬКО для оператора/админа.

    SECURITY: без role-check любой buyer/seller мог открыть чужой conv по ID
    и увидеть KYB-данные / email / phone других пользователей.
    """
    if not ((role or "").startswith("operator") or role == "admin"):
        return ActionResult(text=_("Доступно только оператору поддержки."))
    """Карточка тикета поддержки для оператора.

    Структура (что важно при разборе обращения):
      1. Краткая шапка — кто, когда последняя активность, сколько сообщений
      2. Контакты — email, телефон, мессенджеры из KYB
      3. Контекст — активные заказы, баланс, статус KYB
      4. О чём разговор — последний вопрос пользователя + темы обращений
      5. Действия — связаться (с авто-контекстом), эскалировать
    """
    from .models import Conversation, Message
    from django.utils import timezone

    conv_id = params.get("conversation_id")
    if not conv_id:
        return ActionResult(text=_("Не указан тикет."))
    try:
        conv = Conversation.objects.select_related("user").get(id=conv_id)
    except Conversation.DoesNotExist:
        return ActionResult(text=_("Тикет не найден."))

    target = conv.user
    target_name = target.username if target else "—"

    # ── 1) Сводка тикета ───────────────────────────────────
    total_msgs = Message.objects.filter(conversation=conv).count()
    last_msg = Message.objects.filter(conversation=conv).order_by("-created_at").first()
    last_when = last_msg.created_at if last_msg else conv.updated_at
    minutes_ago = int((timezone.now() - last_when).total_seconds() / 60) if last_when else None
    if minutes_ago is None:
        last_activity = "—"
    elif minutes_ago < 60:
        last_activity = f"{minutes_ago} мин назад"
    elif minutes_ago < 1440:
        last_activity = f"{minutes_ago // 60} ч назад"
    else:
        last_activity = f"{minutes_ago // 1440} дн назад"

    CATEGORY_LBL = {"support": "Поддержка", "purchase": "Покупки",
                      "admin": "Админ", "general": "Общее"}
    cat_label = CATEGORY_LBL.get(conv.category, conv.category or "Общее")

    summary_rows = [
        {"label": _("Пользователь"), "value": target_name, "primary": True},
        {"label": _("Категория"),   "value": cat_label},
        {"label": _("Сообщений"),   "value": str(total_msgs)},
        {"label": _("Последняя активность"), "value": last_activity},
    ]

    # ── 2) Контакты (из KYB если есть) ─────────────────────
    contact_rows = []
    if target:
        contact_rows.append({"label": "Email", "value": target.email or "—"})
        try:
            from marketplace.models import CompanyVerification
            kyb = CompanyVerification.objects.filter(user=target).first()
            if kyb:
                if kyb.legal_name: contact_rows.append({"label": _("Компания"), "value": kyb.legal_name})
                if kyb.country:    contact_rows.append({"label": _("Страна"), "value": kyb.country})
                if kyb.phone:      contact_rows.append({"label": _("Телефон"), "value": kyb.phone})
                if kyb.whatsapp or kyb.telegram:
                    parts = []
                    if kyb.whatsapp: parts.append(f"WA {kyb.whatsapp}")
                    if kyb.telegram: parts.append(f"TG {kyb.telegram}")
                    contact_rows.append({"label": _("Мессенджеры"), "value": " · ".join(parts)})
        except Exception:
            pass

    # ── 3) Контекст: заказы, баланс, KYB-статус ────────────
    context_rows = []
    if target:
        try:
            from marketplace.models import Order
            active_orders = Order.objects.filter(buyer=target).exclude(
                status__in=("delivered", "completed", "cancelled")).count()
            total_orders = Order.objects.filter(buyer=target).count()
            context_rows.append({
                "label": _("Заказов в работе"),
                "value": str(active_orders),
                "sub": _('%(p0)s всего за всё время') % {"p0": f'{total_orders}'},
                "tone": "warn" if active_orders > 0 else "info",
            })
        except Exception:
            pass
        try:
            from marketplace.models import Wallet
            w = Wallet.objects.filter(user=target).first()
            if w:
                context_rows.append({
                    "label": _("Депозит"),
                    "value": f"${float(w.balance or 0):,.0f}",
                    "tone": "info",
                })
        except Exception:
            pass
        try:
            from marketplace.models import CompanyVerification
            kyb = CompanyVerification.objects.filter(user=target).first()
            if kyb:
                STATUS_LBL = {"none": "Не подавалась", "pending": "На проверке",
                                "verified": "Верифицирована", "rejected": "Отклонена"}
                tone_map = {"verified": "ok", "pending": "warn", "rejected": "bad", "none": "info"}
                context_rows.append({
                    "label": _("KYB-статус"),
                    "value": STATUS_LBL.get(kyb.status, kyb.status),
                    "tone": tone_map.get(kyb.status, "info"),
                })
        except Exception:
            pass

    # ── 4) О чём общался — только реальные вопросы и осмысленные темы ─
    # Пользователь = реально что-то написал. Action-клики типа «Назад», «Поддержка»,
    # «← Поддержка» — это просто навигация, не обращение. Их игнорируем.
    last_user_msg = Message.objects.filter(
        conversation=conv, role=Message.Role.USER,
    ).order_by("-created_at").first()
    last_user_text = (last_user_msg.content or "").strip()[:400] if last_user_msg else ""
    user_msg_count = Message.objects.filter(
        conversation=conv, role=Message.Role.USER,
    ).count()

    # Темы — фильтруем навигационный шум.
    NAV_NOISE = {"назад", "поддержка", "← поддержка", "← назад", "главная",
                   "← главная", "🏠 главная", "← inbox поддержки", "inbox поддержки"}
    action_msgs = list(Message.objects.filter(
        conversation=conv, role=Message.Role.ACTION,
    ).order_by("-created_at")[:30])
    topic_set = []
    seen_topics = set()
    for m in action_msgs:
        topic = (m.content or "").strip().lstrip("▸").strip()
        if not topic: continue
        # Чистим эмодзи в начале для дедупа+проверки на nav
        import re as _re
        clean = _re.sub(r"^[\W_]+", "", topic).strip().lower()
        if clean in NAV_NOISE or clean.startswith("←"): continue
        if clean in seen_topics: continue
        seen_topics.add(clean)
        topic_set.append(topic)
        if len(topic_set) >= 5: break

    # Тип обращения определяем ТОЛЬКО по реально написанному пользователем тексту.
    # Клик на «Пожаловаться» — это просто навигация к форме, не сама жалоба.
    # Жалобой считается случай, когда юзер написал что-то с жалобными словами
    # ИЛИ submit'нул форму жалобы (видно по последнему сообщению ассистента
    # «🚨 Жалоба фиксируется в audit-log...» с конкретным content от юзера).
    if user_msg_count == 0:
        ticket_kind = "browsing"
    else:
        complaint_kws = (
            "брак", "сломан", "не работает", "обман", "мошенник",
            "не пришл", "не дошл", "не отгрузил", "тормозит",
            "хамит", "груб", "молчит", "не отвеч",
            "нарушен", "просроч", "опозда",
            "верн", "возврат", "компенсаци",
        )
        # Берём последние 5 user-сообщений
        recent_user_msgs = list(Message.objects.filter(
            conversation=conv, role=Message.Role.USER,
        ).order_by("-created_at")[:5])
        is_complaint_text = any(
            any(kw in (m.content or "").lower() for kw in complaint_kws)
            for m in recent_user_msgs
        )
        ticket_kind = "complaint" if is_complaint_text else "question"

    # ── Заголовок и intro зависят от типа активности ──
    if ticket_kind == "browsing":
        card_title = f"👁 Просмотр поддержки · {target_name}"
        intro = (f"Реального обращения нет. Пользователь {target_name} "
                 f"просто просматривал FAQ/Поддержку, не задавал вопросов "
                 f"и не жаловался. Последний вход: {last_activity}.")
    elif ticket_kind == "complaint":
        card_title = f"🚨 Жалоба · {target_name}"
        intro = (f"Пользователь {target_name} подал жалобу через поддержку. "
                 f"Требует разбора. Последняя активность: {last_activity}.")
    else:  # question
        card_title = f"💬 Вопрос в поддержку · {target_name}"
        intro = (f"Пользователь {target_name} задал {user_msg_count} вопрос(а/ов) "
                 f"в поддержку. Последняя активность: {last_activity}.")

    # ── Собираем карточки ──
    cards = [
        {"type": "draft", "data": {
            "title": card_title,
            "rows": summary_rows, "confirm_label": "—",
        }},
    ]
    # Контакты и контекст — только если это реальное обращение
    if ticket_kind != "browsing":
        if contact_rows:
            cards.append({"type": "draft", "data": {
                "title": _("📇 Контакты"), "rows": contact_rows, "confirm_label": "—",
            }})
        if context_rows:
            cards.append({"type": "kpi_grid", "data": {
                "title": _("📊 Контекст пользователя"), "items": context_rows,
            }})
        if last_user_text:
            cards.append({"type": "draft", "data": {
                "title": _("💬 Последний вопрос пользователя"),
                "rows": [{"label": _("Текст"), "value": last_user_text, "wide": True, "primary": True}],
                "confirm_label": "—",
            }})
        if topic_set:
            cards.append({"type": "list", "data": {
                "title": _("🧭 Темы, которые искал"),
                "items": [{"title": t, "tone": "info"} for t in topic_set],
            }})

    # Кнопка «Связаться» — авто-доставка контекста через ask_operator
    if ticket_kind == "browsing":
        ctx_label = f"Просмотр поддержки · просто проверка наличия проблем"
    elif ticket_kind == "complaint":
        ctx_label = f"Жалоба пользователя в поддержку"
    else:
        ctx_label = f"Вопрос в поддержку · «{last_user_text[:80]}»" if last_user_text else "Вопрос в поддержку"

    actions = []
    if target:
        action_label = (f"💬 Написать {target_name} (профилактика)"
                          if ticket_kind == "browsing"
                          else f"💬 Связаться с {target_name}")
        actions.append({
            "label": action_label,
            "action": "ask_operator",
            "params": {"to_user_id": target.id, "context": ctx_label},
        })
    actions.append({
        "label": _("← Inbox поддержки"),
        "action": "support_home", "params": {},
    })

    return ActionResult(text=intro, cards=cards, actions=actions)


# ── 2. KB / FAQ ────────────────────────────────────────────────

# Статичная база — 16 топовых вопросов. Расширяется через БД-модель в
# Phase 2. Сейчас этого достаточно: покрывает 80% обращений за первый месяц.
FAQ_ENTRIES = [
    {
        "category": _("Регистрация"),
        "q": _("Какие данные нужны для регистрации покупателя?"),
        "a": (_("8 полей: страна, ИНН/Tax ID, ФИО контактного лица, должность, "
              "рабочий e-mail, телефон с кодом страны, мессенджер "
              "(WhatsApp или Telegram), парк техники.\n\n"
              "Название и юр.адрес компании платформа подтянет автоматически "
              "по ИНН. Банковские реквизиты и ФИО подписанта запросим только "
              "перед первой сделкой, не на этапе регистрации.")),
    },
    {
        "category": _("Регистрация"),
        "q": _("Как зарегистрироваться поставщиком?"),
        "a": (_("На лендинге кнопка «стать поставщиком» → форма из 4 полей "
              "(логин, e-mail, пароль). После создания аккаунта откроется "
              "KYB-анкета (5 шагов): реквизиты, юр.адрес, банк, директор, "
              "отправка на проверку. До завершения KYB seller не может "
              "отвечать на RFQ и принимать заказы.")),
    },
    {
        "category": _("KYB / Верификация"),
        "q": _("Сколько занимает проверка поставщика?"),
        "a": (_("Авто-проверки — мгновенно (ИНН, ОГРН/EGRUL, OpenSanctions, "
              "VIES для EU, OpenCorporates для зарубежных, MX-записи e-mail, "
              "регистрация в WhatsApp/Telegram).\n\n"
              "Решение оператора — до 24 часов в рабочий день. После approve — "
              "сразу доступны RFQ-ответы и приём заказов.")),
    },
    {
        "category": _("KYB / Верификация"),
        "q": _("Что именно проверяет платформа автоматически?"),
        "a": (_("• ЕГРЮЛ: статус активности, ликвидация, налоговая недоимка\n"
              "• OpenSanctions: директор и компания в санкционных списках\n"
              "• VIES: валидность VAT для контрагентов ЕС\n"
              "• OpenCorporates: статус компании в иностранных реестрах\n"
              "• MX-записи: реальность домена e-mail\n"
              "• WhatsApp/Telegram: зарегистрирован ли указанный номер")),
    },
    {
        "category": _("Заказы и оплата"),
        "q": _("Какая минимальная сумма заказа?"),
        "a": (_("Минимум $7,000 USD. Меньший заказ платформа не примет — экономика "
              "консолидации (фиксированные расходы на логистику, таможню, "
              "операторов) не окупается на меньших объёмах.\n\n"
              "Если в корзине меньше $7,000 — система покажет «не хватает $N» "
              "и предложит добавить позиции.")),
    },
    {
        "category": _("Заказы и оплата"),
        "q": _("Какая схема оплаты?"),
        "a": (_("Двухэтапная через эскроу:\n\n"
              "1. 10% резерв при подтверждении КП — замораживается "
              "на депозите покупателя, поставщик видит подтверждение "
              "и запускает заказ в работу.\n\n"
              "2. 90% остаток перед отгрузкой — оплачивается когда "
              "поставщик помечает «готов к отгрузке». Деньги уходят "
              "поставщику только после подтверждения приёмки покупателем.")),
    },
    {
        "category": _("Заказы и оплата"),
        "q": _("Можно ли отменить заказ?"),
        "a": (_("До оплаты резерва — да, в один клик из карточки заказа.\n\n"
              "После оплаты резерва (10%) — только через оператора. "
              "Если поставщик уже запустил производство, может быть удержание "
              "резерва. Откройте обращение в «Поддержка» → «Связаться с "
              "оператором» — рассмотрим индивидуально.")),
    },
    {
        "category": _("Доставка и сроки"),
        "q": _("Как отследить заказ?"),
        "a": (_("В чате наберите «трекинг 82» или «когда отгрузят 82» (где 82 — "
              "ID заказа). Карточка покажет:\n\n"
              "• Текущий этап (производство → отгрузка → таможня → выдача)\n"
              "• ETA выдачи в днях\n"
              "• Перевозчика (DHL/СДЭК/Maersk и т.д.) с контактами\n"
              "• Трек-номер + прямую ссылку на сайт перевозчика")),
    },
    {
        "category": _("Доставка и сроки"),
        "q": _("Какие используются базисы поставки (Incoterms)?"),
        "a": (_("По умолчанию FOB — поставщик довозит до порта отгрузки, "
              "дальше платформа берёт логистику на себя.\n\n"
              "На больших партиях доступны EXW (со склада поставщика) "
              "и CIF (морем со страховкой). Выбор базиса — на этапе расчёта "
              "КП в чате.")),
    },
    {
        "category": _("Рекламации"),
        "q": _("Как открыть рекламацию на заказ?"),
        "a": (_("В карточке заказа кнопка «🧾 Открыть рекламацию», либо в чате "
              "«рекламация по заказу 82». Форма из 4 полей:\n\n"
              "• Вид: брак, не та деталь, повреждение при доставке, "
              "просрочка, не пришла, иное\n"
              "• Заголовок\n"
              "• Описание (что произошло, фото если есть)\n"
              "• Желаемая компенсация ($, опционально)\n\n"
              "Оператор уведомляется сразу, по SLA — отвечает в течение 24 ч.")),
    },
    {
        "category": _("Рекламации"),
        "q": _("Сколько времени рассматривается рекламация?"),
        "a": (_("SLA зависит от вида (защищает от затягивания):\n\n"
              "• Не пришла → 2 дня\n"
              "• Брак / Повреждение при доставке → 3 дня\n"
              "• Просрочка поставки → 5 дней\n"
              "• Не та деталь / Иное → 7 дней\n\n"
              "Если оператор не реагирует в срок — рекламация автоматически "
              "эскалируется супервайзеру и в Telegram on-call оператора.")),
    },
    {
        "category": _("Рекламации"),
        "q": _("Какие варианты решения по рекламации?"),
        "a": (_("• Полный возврат денег с депозита\n"
              "• Частичный возврат (компенсация за дефект)\n"
              "• Бесплатная замена позиции\n"
              "• Доплата за повреждённую упаковку\n"
              "• Кредит на следующий заказ\n\n"
              "Оператор согласует решение с обеими сторонами и зафиксирует "
              "в системе. Все этапы сохраняются в audit-log.")),
    },
    {
        "category": _("Связь со сторонами"),
        "q": _("Можно ли договариваться напрямую с поставщиком?"),
        "a": (_("Нет, никогда. Платформа намеренно не раскрывает контакты "
              "поставщика покупателю. Вся переписка идёт через оператора:\n\n"
              "• Покупатель видит «Поставщик №1, №2…» — анонимная нумерация\n"
              "• Любой вопрос / уточнение / спор — через чат-форму\n"
              "• Оператор передаёт между сторонами и фиксирует переписку\n\n"
              "Это защищает обе стороны: у покупателя есть audit-log и "
              "эскроу, у поставщика — гарантия оплаты и нет потери комиссии. "
              "Попытки обменяться контактами через сообщения чата автоматически "
              "флагуются оператору и могут привести к снижению рейтинга "
              "или блокировке.")),
    },
    {
        "category": _("Связь со сторонами"),
        "q": _("А как тогда уточнить детали по заказу?"),
        "a": (_("Все вопросы — через оператора платформы:\n\n"
              "1. В карточке заказа нажмите «💬 Связаться с оператором» "
              "(или в чате напишите «связаться с оператором»)\n"
              "2. Опишите вопрос (заказ, что нужно уточнить)\n"
              "3. Оператор переадресует продавцу/логисту и вернёт ответ\n\n"
              "Среднее время реакции оператора — 4 рабочих часа. "
              "Все обращения сохраняются.")),
    },
    {
        "category": _("Бонусы"),
        "q": _("Есть ли скидки за объём?"),
        "a": (_("Да, auto-discount применяется автоматически по обороту за 12 мес:\n\n"
              "• От $50,000 → Bronze (комиссия −1%)\n"
              "• От $200,000 → Silver (−2%)\n"
              "• От $500,000 → Gold (−3%)\n"
              "• От $1,000,000 → Platinum (−5% + персональный менеджер)\n\n"
              "Текущий тир — в меню «Поддержка» → «🎁 Мои бонусы». "
              "Скидка применяется к новым заказам автоматически — "
              "ничего активировать не нужно.")),
    },
    {
        "category": _("Платформа"),
        "q": _("Какие AI-возможности есть в чате?"),
        "a": (_("• OEM-нормализатор: 707-99-58030, 7079958030, CAT-265-0235 — "
              "находит одну и ту же деталь под любым написанием\n"
              "• Загрузка спецификации: drag-and-drop xlsx/csv/pdf, "
              "AI-маппинг колонок (название, ОЕМ, кол-во)\n"
              "• Авто-подбор поставщиков: AUTO/SEMI/MANUAL режимы с "
              "разными SLA — система выбирает по сложности запроса\n"
              "• Расчёт landed cost: цена + доставка + таможня по "
              "incoterm в один клик\n"
              "• Трекинг с прогнозом: текущий этап + ETA выдачи по дням")),
    },
    {
        "category": _("Платформа"),
        "q": _("Где посмотреть статус моих проверок?"),
        "a": (_("В чате: «Поддержка» → «🛡 Статус моих проверок».\n\n"
              "Показывает: верификация e-mail, телефон, мессенджер, "
              "KYB-статус (для поставщика), реквизиты компании, рейтинг, "
              "лимиты по бюджету. Если что-то не так — отправьте обращение "
              "оператору с указанием поля, поправим.")),
    },
    {
        "category": _("Поддержка"),
        "q": _("Как связаться с поддержкой?"),
        "a": (_("В чате слева — pill «Поддержка». Внутри:\n\n"
              "• ❓ База знаний (этот раздел)\n"
              "• 🛡 Статус моих проверок\n"
              "• 🎁 Мои бонусы и скидки\n"
              "• 🧾 Мои рекламации\n"
              "• 💬 Связаться с оператором — форма ticket'а\n"
              "• 🚨 Пожаловаться на платформу/оператора/поставщика\n\n"
              "Среднее время ответа — 4 рабочих часа. Critical (блок "
              "аккаунта, потеря денег) — в течение часа.")),
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
            text=(_('❓ По запросу «%(p0)s» в FAQ ничего не найдено.\nНапишите оператору — добавим в базу.') % {"p0": f'{query}'}),
            actions=[{"action": "contact_operator",
                       "label": _("💬 Спросить оператора")}],
            contextual_actions=[{"action": "support_home", "label": _("← Назад")}],
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
                "title": _('FAQ · %(p0)s ответов') % {"p0": f'{len(matched)}'},
                "items": faq_items[:60],
            },
        }],
        suggestions=[
            "Как открыть рекламацию?", "Минимальная сумма заказа",
            "Когда раскрываются контакты", "Как работают бонусы",
        ],
        contextual_actions=[
            {"action": "support_home", "label": _("← Поддержка")},
            {"action": "contact_operator", "label": _("💬 Спросить оператора")},
        ],
    )


# ── 3. MY VERIFICATIONS ───────────────────────────────────────

@register("my_verifications")
def my_verifications(params, user, role):
    """Статус всех проверок аккаунта. Pure DB, без LLM."""
    from marketplace.models import CompanyVerification, UserProfile

    p = UserProfile.objects.filter(user=user).first()
    rows = [
        {"label": _("Логин"),     "value": user.username},
        {"label": "Email",     "value": user.email or "—",
         "tone": "ok" if user.email else "warn"},
        {"label": _("Аккаунт"),   "value": "Активен" if user.is_active else "Заблокирован",
         "tone": "ok" if user.is_active else "bad"},
        {"label": _("Роль"),      "value": (p.role if p else "—").upper()},
    ]
    if p:
        rows.extend([
            {"label": _("Страна"),        "value": p.country or "—"},
            {"label": _("ИНН / Tax ID"),  "value": p.tax_id or "—"},
            {"label": _("Контактное лицо"), "value": p.contact_name or "—"},
            {"label": _("Телефон"),       "value": p.phone_e164 or "—",
             "tone": "ok" if p.phone_e164 else "warn"},
            {"label": _("Мессенджер"),    "value": (
                f"{p.get_messenger_kind_display()}: {p.messenger_handle}"
                if p.messenger_kind and p.messenger_handle else "—"),
             "tone": "ok" if (p.messenger_kind and p.messenger_handle) else "warn"},
        ])
        if p.role == "seller":
            kyb = CompanyVerification.objects.filter(user=user).order_by("-id").first()
            if kyb:
                tone = {"verified": "ok", "pending": "warn",
                        "rejected": "bad", "draft": "info"}.get(kyb.status, "info")
                rows.append({"label": _("KYB-статус"),
                             "value": kyb.get_status_display(), "tone": tone})
                if kyb.rejection_reason:
                    rows.append({"label": _("Причина отклонения"),
                                 "value": kyb.rejection_reason[:200], "tone": "bad"})
            else:
                rows.append({"label": _("KYB-статус"), "value": "Не заполнен",
                             "tone": "warn"})

    next_actions = []
    if p and p.role == "seller":
        # Если KYB не пройден — кнопка-завершение
        kyb = CompanyVerification.objects.filter(user=user, status="verified").first()
        if not kyb:
            next_actions.append({"action": "start_onboarding",
                                 "label": _("🚀 Пройти KYB")})

    return ActionResult(
        text=(
            _('🛡 Статус проверок аккаунта %(p0)s.\nВсе данные — из вашего профиля. Что неверно — отправьте оператору жалобу, поправим.') % {"p0": f'{user.username}'}
        ),
        cards=[{
            "type": "kpi_grid",
            "data": {"title": _("🛡 Проверки и реквизиты"),
                     "items": rows[:15]},
        }],
        actions=next_actions,
        contextual_actions=[
            {"action": "support_home",      "label": _("← Поддержка")},
            {"action": "contact_operator",  "label": _("💬 Спросить оператора")},
        ],
    )


# ── 4. MY BONUSES (для buyer) ─────────────────────────────────

@register("my_bonuses")
def my_bonuses(params, user, role):
    """Текущие бонусы и скидки покупателя."""
    if role != "buyer":
        return ActionResult(
            text=_("🎁 Бонусы доступны только в кабинете покупателя."),
            contextual_actions=[{"action": "support_home", "label": _("← Назад")}],
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
        {"label": _("Оборот за 12 мес"), "value": f"${gmv_12m:,.0f}", "tone": "info"},
        {"label": _("Заказов за 12 мес"), "value": str(cnt_12m)},
        {"label": _("Текущий статус"),
         "value": current_tier[1] if current_tier else "—",
         "sub": current_tier[2] if current_tier else "Заказы от $50k → Bronze",
         "tone": "ok" if current_tier else "warn"},
    ]
    if next_tier:
        shortage = next_tier[0] - gmv_12m
        rows.append({
            "label": _('До %(p0)s') % {"p0": f'{next_tier[1]}'},
            "value": f"${shortage:,.0f}",
            "sub": next_tier[2], "tone": "info",
        })

    return ActionResult(
        text=(
            _('🎁 Ваши бонусы. Auto-discount работает автоматически — скидка применяется к новым заказам по факту достижения тира.')
        ),
        cards=[
            {"type": "kpi_grid",
             "data": {"title": _("🎁 Бонусная программа"), "items": rows}},
            {"type": "list", "data": {
                "title": _("📊 Тиры скидок"),
                "items": [
                    {"title": f"{name}", "subtitle": _('от $%(p0)s · %(p1)s') % {"p0": f'{thr:,.0f}', "p1": f'{perk}'},
                     "tone": "ok" if gmv_12m >= thr else "info"}
                    for thr, name, perk in tiers
                ],
            }},
        ],
        actions=[
            # Аналитика и отчёты по сделкам — все pure-DB action'ы
            {"label": "🎯 Auto-discount",   "action": "get_buyer_discount", "params": {}},
            {"label": _("💸 Экономия"),        "action": "get_savings",        "params": {}},
            {"label": _("📊 Аналитика заказов"),"action": "get_analytics",     "params": {}},
            {"label": _("📦 Отчёт по поставкам"),"action": "get_supply_report","params": {}},
        ],
        contextual_actions=[
            {"action": "support_home",     "label": _("← Поддержка")},
            {"action": "contact_operator", "label": _("💬 Спросить оператора")},
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
            text=_("💬 Опишите проблему — оператор получит ticket в свой админ-чат."),
            cards=[{
                "type": "form",
                "data": {
                    "title": _("💬 Связаться с оператором"),
                    "submit_action": "contact_operator",
                    "submit_label": "📨 Отправить",
                    "fields": [
                        {"name": "topic", "label": _("Тема"), "type": "select",
                         "required": True, "options": [
                            {"value": "billing",      "label": _("💰 Оплата / комиссия")},
                            {"value": "verification", "label": _("🛡 Верификация / KYB")},
                            {"value": "order",        "label": _("📦 Заказ / доставка")},
                            {"value": "complaint",    "label": _("🚨 Жалоба")},
                            {"value": "feature",      "label": _("💡 Предложение")},
                            {"value": "other",        "label": _("❓ Другое")},
                         ]},
                        {"name": "text", "label": _("Опишите проблему"),
                         "type": "textarea", "required": True,
                         "placeholder": _("Что произошло, в каком заказе/RFQ, что хотите получить")},
                    ],
                    "fixed_params": {"confirmed": True},
                },
            }],
            contextual_actions=[{"action": "support_home", "label": _("← Назад")}],
        )

    if not text:
        return ActionResult(text=_("⚠️ Описание обязательно."))

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
                _('💬 ОБРАЩЕНИЕ от @%(p0)s (%(p1)s)\nТема: %(p2)s\nТекст: %(p3)s%(p4)s%(p5)s') % {"p0": f'{user.username}', "p1": f'{role}', "p2": f'{topic_label}', "p3": f'{text[:500]}', "p4": f"{('…' if len(text) > 500 else '')}", "p5": f'{flag_note}'}
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
            {"action": "support_home", "label": _("← Поддержка")},
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
            text=_("🚨 Жалоба фиксируется в audit-log и эскалируется супервайзеру."),
            cards=[{
                "type": "form",
                "data": {
                    "title": _("🚨 Жалоба на платформу"),
                    "submit_action": "open_complaint",
                    "submit_label": "🚨 Подать жалобу",
                    "fields": [
                        {"name": "against", "label": _("На кого"), "type": "select",
                         "required": True, "options": [
                            {"value": "platform", "label": _("Платформа в целом")},
                            {"value": "operator", "label": _("Оператор / поддержка")},
                            {"value": "seller",   "label": _("Поставщик (требует order_id)")},
                            {"value": "buyer",    "label": _("Покупатель (для seller)")},
                            {"value": "other",    "label": _("Другое")},
                         ]},
                        {"name": "text", "label": _("Подробное описание"),
                         "type": "textarea", "required": True,
                         "placeholder": _("Что произошло, когда, какие доказательства")},
                    ],
                    "fixed_params": {"confirmed": True},
                },
            }],
            contextual_actions=[{"action": "support_home", "label": _("← Назад")}],
        )

    if not text:
        return ActionResult(text=_("⚠️ Описание обязательно."))

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
                _('🚨🚨 ЖАЛОБА от @%(p0)s (%(p1)s)\nПротив: %(p2)s\nТекст: %(p3)s%(p4)s%(p5)s') % {"p0": f'{user.username}', "p1": f'{role}', "p2": f'{against_label}', "p3": f'{text[:500]}', "p4": f"{('…' if len(text) > 500 else '')}", "p5": f"{(chr(10) + '⚠️ Обнаружены off-platform контакты' if flagged else '')}"}
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
            {"action": "support_home", "label": _("← Поддержка")},
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
