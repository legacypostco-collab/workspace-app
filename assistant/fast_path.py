"""Fast-path intent router — deterministic responses without calling LLM.

Hybrid AI architecture:
- Known scenarios (multi-article paste, "my RFQs", "my orders", "make proposal")
  match a regex/keyword rule and execute the corresponding action directly.
  → ~50ms latency, $0 cost, 100% predictable.
- Everything else falls through to Claude tool-use.
  → smart but slower / paid.

This file owns the fast-path matchers. Each `match_*` returns either
(action_name, params) to execute, or None to defer to LLM.
"""
from __future__ import annotations

import re

# OEM article: 4-19 chars, letters+digits+separators, must contain a digit.
_OEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/.]{3,18}$")
_SPLIT_RE = re.compile(r"[\n,;]+")
_QTY_TRAIL_RE = re.compile(r"^\d+(?:[\.,]\d+)?(?:шт|pcs|x|×)?$", re.IGNORECASE)


def _extract_articles_with_qty(text: str) -> list[tuple[str, int]]:
    """Парсит «OEM qty» по строкам. Поддерживаемые форматы:
        2W1223
        2W1223 1
        2W1223,1
        2W1223 2шт
        2W1223 x3
    Возвращает [(oem, qty), …]. Если qty не распознан — qty=1.
    """
    if not text:
        return []
    out: list[tuple[str, int]] = []
    for chunk in _SPLIT_RE.split(text):
        tokens = [t.strip(".").strip() for t in chunk.split() if t.strip()]
        if not tokens:
            continue
        oem = None
        qty = 1
        for i, tok in enumerate(tokens):
            if tok and _OEM_RE.match(tok) and any(ch.isdigit() for ch in tok):
                # Чистые числа — это qty (1, 12, 100), НО реальные OEM
                # Komatsu/Sandvik бывают чисто-цифровые (3128316557, 707-99-58030).
                # Порог 4 символа: до 3 цифр включительно = qty, 4+ = OEM.
                if tok.isdigit() and len(tok) <= 3:
                    continue
                oem = tok
                if i + 1 < len(tokens):
                    nxt = tokens[i + 1].lstrip("xX×").rstrip("шт.pcs")
                    if _QTY_TRAIL_RE.match(tokens[i + 1]) or nxt.isdigit():
                        try:
                            qty = max(1, int(float(nxt)))
                        except ValueError:
                            pass
                break
        if oem:
            out.append((oem, qty))
    return out


def _extract_articles(text: str) -> list[str]:
    return [oem for oem, _ in _extract_articles_with_qty(text)]


def _has_keyword(msg: str, keywords: tuple[str, ...]) -> bool:
    return any(k in msg for k in keywords)


# ── Intent rules (priority order) ────────────────────────────
RULES: list[tuple[str, callable]] = []


def rule(name: str):
    def deco(fn):
        RULES.append((name, fn))
        return fn
    return deco


@rule("find_order")
def _find_order(msg: str, lower: str) -> tuple[str, dict] | None:
    """Быстрый поиск заказа по ЯВНОМУ маркеру: «#645», «ORD-645», «заказ 645»,
    «заказа 645», «сделка 645», «order 645» → открыть карточку заказа.

    ВЫШЕ multi_article, иначе «ORD-645» уйдёт в OEM-поиск. Матчим только когда
    ВСЯ строка — ссылка на заказ (fullmatch), чтобы «заказать 645 штук» не
    распозналось как заказ. Голый номер (без маркера) ловит find_order_num ниже.
    """
    s = lower.strip().strip(".,!?… ").strip()
    m = re.fullmatch(
        r"(?:(?:покажи|показать|открой|открыть|найди|найти|глянь|show|open|go to)\s+)?"
        r"(?:ord[-\s]?|order\s*#?\s*|заказ[а]?\s*#?\s*|сделк[аи]\s*#?\s*|#)\s*0*(\d{1,7})",
        s)
    if m:
        return ("get_order_detail", {"order_id": int(m.group(1))})
    return None


@rule("multi_article_paste")
def _multi_article(msg: str, lower: str) -> tuple[str, dict] | None:
    """User pasted OEM article numbers → search_parts (БЕЗ LLM, мгновенно).

    БАЗОВАЯ ФИЧА платформы: вставил парт-номер → получил цену из БД быстро.
    Никаких галлюцинаций LLM на коде запчасти — детерминированный код-поиск.

    Поддерживает «OEM qty» формат: «2W1223 1», «1R0750,2», «3EG-66-73330KF x4».
    - 2+ артикулов → всегда fast-path
    - 1 артикул → fast-path только если в сообщении НЕТ обычных слов
      (т.е. это чистая вставка кода, а не вопрос «что такое 1R0750?»).
    """
    pairs = _extract_articles_with_qty(msg)
    if not pairs:
        return None
    if len(pairs) >= 2:
        return ("search_parts", {
            "query": msg,
            "articles": [oem for oem, _ in pairs],
            "quantities": {oem: q for oem, q in pairs},
        })
    # 1 артикул — fast-path только если нет других «человеческих» слов.
    # Иначе вопросы «что такое 1R0750?» / «найди аналог 1R0750» уходят в LLM.
    oem_tokens = {oem for oem, _ in pairs}
    extra_words = []
    for token in re.split(r"[\s,;\n]+", msg):
        token = token.strip(".,;:!?()[]{}")
        if not token:
            continue
        if token in oem_tokens:
            continue
        # Цифры/qty-маркеры — не считаются «человеческими словами».
        if _QTY_TRAIL_RE.match(token) or token.lstrip("xX×").isdigit():
            continue
        # Любое слово 2+ буквенных символов — признак естественного запроса.
        if sum(1 for ch in token if ch.isalpha()) >= 2:
            extra_words.append(token)
    if not extra_words:
        return ("search_parts", {
            "query": msg,
            "articles": [oem for oem, _ in pairs],
            "quantities": {oem: q for oem, q in pairs},
        })
    return None


@rule("show_rfqs")
def _my_rfqs(msg: str, lower: str) -> tuple[str, dict] | None:
    """«мои rfq», «покажи rfq», «активные котировки»"""
    triggers = ("мои rfq", "мои rfq", "мои котировки", "покажи rfq",
                "покажи мои rfq", "активные rfq", "активные котировки",
                "список rfq", "все rfq", "my rfq", "show rfq")
    if _has_keyword(lower, triggers):
        return ("get_rfq_status", {})
    return None


@rule("create_rfq_prompt")
def _create_rfq_prompt(msg: str, lower: str) -> tuple[str, dict] | None:
    """Голая команда «Создать RFQ» (чип-подсказка) без детали → не создаём
    пустой RFQ, а зовём create_rfq с пустыми params: его guard вернёт
    уточняющий вопрос «что запросить?». Матчим ТОЛЬКО точную мета-команду,
    чтобы «создать rfq на фильтр Komatsu» ушло в обычный разбор."""
    s = lower.strip().strip(".!?…").strip()
    meta = {
        "создать rfq", "создать новый rfq", "создание нового rfq", "создание rfq",
        "новый rfq", "rfq", "создать запрос котировок", "новый запрос котировок",
        "оформить rfq", "сделать rfq", "создать запрос", "create rfq", "new rfq",
    }
    if s in meta:
        return ("create_rfq", {})
    return None


@rule("find_order_num")
def _find_order_num(msg: str, lower: str) -> tuple[str, dict] | None:
    """Голый номер заказа: всё сообщение = цифры (≤7) → открыть карточку.
    Стоит ПОСЛЕ multi_article: длинные чисто-цифровые OEM (Komatsu/Sandvik,
    3128316557) уже перехвачены выше; сюда доходят только короткие номера."""
    s = lower.strip().strip(".,!?…# ").strip()
    if re.fullmatch(r"\d{1,7}", s):
        return ("get_order_detail", {"order_id": int(s)})
    return None


@rule("show_orders")
def _my_orders(msg: str, lower: str) -> tuple[str, dict] | None:
    """«мои заказы», «статус заказов», «show orders»"""
    triggers = ("мои заказ", "покажи заказ", "статус заказ", "статус мои заказ",
                "список заказ", "все заказы", "активные заказ",
                "show orders", "my orders", "order status")
    if _has_keyword(lower, triggers):
        return ("get_orders", {})
    return None


@rule("generate_proposal")
def _make_proposal(msg: str, lower: str) -> tuple[str, dict] | None:
    """«сформируй кп», «сделай коммерческое», «выгрузи кп»"""
    triggers = ("сформируй кп", "сформировать кп", "сделай кп", "сделать кп",
                "коммерческое предложение", "выгрузи кп", "генерируй кп",
                "create proposal", "make proposal")
    if _has_keyword(lower, triggers):
        # Try to extract RFQ id from text: "кп для rfq 35", "кп по #35"
        m = re.search(r"(?:rfq\s*#?|#)\s*(\d+)", lower)
        params = {}
        if m:
            params["rfq_id"] = int(m.group(1))
        return ("generate_proposal", params)
    return None


@rule("budget")
def _budget(msg: str, lower: str) -> tuple[str, dict] | None:
    """«бюджет», «расходы», «сколько потратили»"""
    triggers = ("бюджет", "расходы за", "сколько потратили", "сколько потратил",
                "общая сумма заказ", "budget", "spending")
    if _has_keyword(lower, triggers):
        period = "month"
        if "год" in lower or "year" in lower:
            period = "year"
        elif "квартал" in lower or "quarter" in lower:
            period = "quarter"
        elif "недел" in lower or "week" in lower:
            period = "week"
        return ("get_budget", {"period": period})
    return None


@rule("analytics")
def _analytics(msg: str, lower: str) -> tuple[str, dict] | None:
    # «дашборд» / «dashboard» отдают приоритет seller_dashboard rule (ниже).
    triggers = ("аналитик", "kpi", "метрик", "analytics")
    if _has_keyword(lower, triggers):
        return ("get_analytics", {})
    return None


@rule("sla_report")
def _sla(msg: str, lower: str) -> tuple[str, dict] | None:
    if "sla" in lower or "просрочк" in lower or "нарушен" in lower:
        return ("get_sla_report", {})
    return None


@rule("claims")
def _claims(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("рекламац", "претенз", "брак", "claim", "complaint")
    if _has_keyword(lower, triggers):
        return ("get_claims", {})
    return None


@rule("ship_order_fp")
def _ship_order_fp(msg: str, lower: str) -> tuple[str, dict] | None:
    """«отгрузил 138 RA12345» / «отгрузить заказ #138 трекинг RA123 DHL»."""
    triggers = ("отгруз", "отправ", "ship ", "shipped", "shipping ")
    if not _has_keyword(lower, triggers):
        return None
    m_id = re.search(r"(?:заказ|order)?\s*#?\s*(\d{1,7})\b", msg)
    if not m_id:
        return None
    order_id = int(m_id.group(1))
    # Tracking номер: всё что после order id, выглядит как код (буквы+цифры >= 6 символов)
    rest = msg[m_id.end():]
    m_track = re.search(r"\b([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9\-]{5,})\b", rest)
    params = {"order_id": order_id}
    if m_track:
        params["tracking_number"] = m_track.group(1)
        # Опциональный перевозчик: предыдущее или последнее слово
        m_car = re.search(r"\b(DHL|FedEx|UPS|EMS|TNT|Boxberry|СДЭК|CDEK|China\s*Post|Self)\b", msg, re.I)
        if m_car:
            params["carrier"] = m_car.group(1)
    return ("ship_order", params)


@rule("seller_qr")
def _seller_qr(msg: str, lower: str) -> tuple[str, dict] | None:
    if _has_keyword(lower, ("qr", "кьюар", "сканирован")):
        return ("seller_qr", {})
    return None


@rule("seller_logistics")
def _seller_logistics(msg: str, lower: str) -> tuple[str, dict] | None:
    if _has_keyword(lower, ("логистик", "в пути", "транзит", "tracking",
                            "logistics", "in transit")):
        return ("seller_logistics", {})
    return None


@rule("seller_negotiations")
def _seller_negotiations(msg: str, lower: str) -> tuple[str, dict] | None:
    if _has_keyword(lower, ("переговор", "negotiation", "торг")):
        return ("seller_negotiations", {})
    return None


@rule("notifications")
def _notifications(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("уведомлен", "что новенького", "что нового", "колоколь",
                "notifications", "inbox bell")
    if _has_keyword(lower, triggers):
        return ("notifications", {})
    return None


@rule("audit_log")
def _audit_log(msg: str, lower: str) -> tuple[str, dict] | None:
    """«аудит заказа N», «лог по #N», «события заказа N»."""
    triggers = ("аудит", "лог по", "события заказ", "events", "audit log")
    if not _has_keyword(lower, triggers):
        return None
    m = re.search(r"(?:заказ|order)?\s*#?\s*(\d+)", lower)
    if m:
        return ("audit_log", {"order_id": int(m.group(1))})
    return None


@rule("price_quote")
def _price_quote(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = (
        "конфигуратор цен", "посчитай цен", "цена для клиента",
        "fob ", "cif ", "ddp ", "сколько будет с маржей",
        "price quote", "calculate price",
    )
    if _has_keyword(lower, triggers):
        return ("price_quote", {})
    return None


@rule("kb_search")
def _kb_search(msg: str, lower: str) -> tuple[str, dict] | None:
    """«база знаний», «кросс-номер», «как упаковать», «таможенный код», «маршрут»."""
    triggers = (
        "база знаний", "что в базе", "регламент", "кросс-номер", "кросс номер",
        "как упаков", "как принять", "таможенный код", "тн вэд",
        "маршрут", "лидтайм", "lead time", "knowledge base", "kb ",
    )
    if not _has_keyword(lower, triggers):
        return None
    return ("kb_search", {"query": msg.strip()})


@rule("seller_inbox")
def _seller_inbox(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("сегодня", "что делать", "что срочно", "горящи", "inbox",
                "to-do", "чеклист", "что важно", "что в первую")
    if _has_keyword(lower, triggers):
        return ("seller_inbox", {})
    return None


@rule("seller_catalog")
def _seller_catalog(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("каталог", "мои товар", "мой каталог", "список товаров",
                "products list", "my catalog")
    if _has_keyword(lower, triggers):
        return ("seller_catalog", {})
    return None


@rule("seller_drawings")
def _seller_drawings(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("чертеж", "drawing", "схем")
    if _has_keyword(lower, triggers):
        return ("seller_drawings", {})
    return None


@rule("seller_team")
def _seller_team(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("команд", "сотрудник", "пригласи", "team",
                "my staff", "members")
    if _has_keyword(lower, triggers):
        return ("seller_team", {})
    return None


@rule("seller_integrations")
def _seller_integrations(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("интеграц", "1с", "битрикс", "telegram", "api",
                "integration", "webhook")
    if _has_keyword(lower, triggers):
        return ("seller_integrations", {})
    return None


@rule("seller_reports")
def _seller_reports(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("отчет", "отчёт", "выгрузк", "экспорт",
                "report", "export", "download")
    if _has_keyword(lower, triggers):
        return ("seller_reports", {})
    return None


@rule("add_product")
def _add_product(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("добавь товар", "добавить товар", "новый товар",
                "add product", "new product")
    if _has_keyword(lower, triggers):
        return ("add_product", {})
    return None


@rule("seller_dashboard")
def _seller_dashboard(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("дашборд", "сводка", "сводку", "kpi", "главная", "обзор",
                "dashboard", "overview")
    if _has_keyword(lower, triggers):
        return ("seller_dashboard", {})
    return None


@rule("seller_finance")
def _seller_finance(msg: str, lower: str) -> tuple[str, dict] | None:
    # «выручка», «финансы», «продажи за месяц», «когда выплата»
    triggers = ("выручк", "финанс", "продаж", "выплат", "доход",
                "revenue", "income", "payouts", "earnings")
    if _has_keyword(lower, triggers):
        return ("seller_finance", {})
    return None


@rule("seller_rating")
def _seller_rating(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("рейтинг", "отзыв", "репутац", "балл",
                "rating", "reviews", "score")
    if _has_keyword(lower, triggers):
        return ("seller_rating", {})
    return None


@rule("seller_pipeline")
def _seller_pipeline(msg: str, lower: str) -> tuple[str, dict] | None:
    """«к отгрузке», «очередь», «что отгружать», «мои отгрузки»."""
    triggers = (
        "к отгрузке", "очередь", "очередь отгруз", "что отгружать",
        "мои отгрузки", "что в очереди", "очередь продавц",
        "shipment queue", "to ship", "seller queue", "shipping queue",
    )
    if _has_keyword(lower, triggers):
        return ("seller_pipeline", {})
    return None


@rule("track_order")
def _track_order(msg: str, lower: str) -> tuple[str, dict] | None:
    """«трекинг 124», «где заказ 124», «отследи заказ #124», «статус заказа 124»,
    «когда отгрузят 82», «как скоро отгрузят заказ #82», «когда придёт 82»,
    «когда доедет 82», «ETA по 82», «срок поставки 82».

    Расширено: покрывает популярные вопросы про сроки поставки —
    раньше падали в LLM (≈1500 input + 300 output токенов на запрос).
    """
    triggers = (
        # tracking / location
        "трекинг", "отслеж", "отследи", "где заказ", "где мой заказ",
        "где посылк", "статус заказ", "статус #",
        # ETA / shipping date
        "когда отгруз", "как скоро отгруз", "когда придёт", "когда придет",
        "когда доедет", "когда прибуд", "когда поедет", "когда привез",
        "срок поставки", "срок доставки", "eta", "ета",
        # English
        "track", "where is order", "tracking", "when will", "when ship",
        "delivery date", "eta order",
    )
    if not _has_keyword(lower, triggers):
        return None
    # Захватываем число в окружении (русские окончания «заказу/заказа», «order #»,
    # или цифры без префикса в формате «отгрузят 82»).
    m = (
        re.search(r"(?:заказ\w*|order)\s*#?\s*(\d+)", lower)
        or re.search(r"#\s*(\d+)", lower)
        or re.search(r"\b(\d{2,7})\b", lower)
    )
    if m:
        return ("track_order", {"order_id": int(m.group(1))})
    return None


@rule("track_shipment")
def _track(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("шипмент", "shipment", "где посылк")
    if _has_keyword(lower, triggers):
        m = re.search(r"(?:заказ|order)\s*#?\s*(\w+)", lower)
        return ("track_shipment", {"order_id": m.group(1)} if m else {})
    return None


@rule("demand_report")
def _demand(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("спрос", "что ищут", "что чаще ищут", "топ запрос",
                "demand report", "what is in demand")
    if _has_keyword(lower, triggers):
        return ("get_demand_report", {})
    return None


@rule("get_balance")
def _balance(msg: str, lower: str) -> tuple[str, dict] | None:
    """«баланс», «депозит», «сколько на счёте», «кошелёк», «история операций»."""
    triggers = (
        "баланс", "депозит", "кошел", "сколько на счет", "сколько на счёт",
        "сколько денег", "остаток на счет", "остаток на счёт",
        "история операц", "история списан", "транзакц",
        "balance", "wallet", "deposit", "transactions",
    )
    if _has_keyword(lower, triggers):
        return ("get_balance", {})
    return None


@rule("topup_wallet")
def _topup(msg: str, lower: str) -> tuple[str, dict] | None:
    """«пополни на 5000», «пополнить депозит на $1000», «закинь 10к»."""
    triggers = ("пополни", "пополн", "закинь", "topup", "top up", "top-up", "add funds")
    if not _has_keyword(lower, triggers):
        return None
    m = re.search(r"\$?\s*([\d][\d\s]*(?:[.,]\d+)?)\s*(k|к|тыс|thousand)?", lower)
    amount = 10000.0
    if m:
        try:
            num = float(m.group(1).replace(" ", "").replace(",", "."))
            mult = 1000 if m.group(2) else 1
            amount = num * mult
        except ValueError:
            pass
    return ("topup_wallet", {"amount": amount})


@rule("pay_reserve_fp")
def _pay_reserve_fp(msg: str, lower: str) -> tuple[str, dict] | None:
    """«оплати резерв заказа 126», «спиши 10% по #126»."""
    triggers_pay = ("оплат", "списать", "спиши", "pay")
    triggers_reserve = ("резерв", "10%", "reserve")
    if not _has_keyword(lower, triggers_pay):
        return None
    if not _has_keyword(lower, triggers_reserve):
        return None
    m = re.search(r"(?:заказ|order)?\s*#?\s*(\d+)", lower)
    if not m:
        return None
    return ("pay_reserve", {"order_id": int(m.group(1))})


@rule("pay_final_fp")
def _pay_final_fp(msg: str, lower: str) -> tuple[str, dict] | None:
    """«оплати остаток заказа 126», «доплати 90% по #126»."""
    triggers_pay = ("оплат", "доплат", "списать", "спиши", "pay")
    triggers_final = ("остаток", "90%", "финал", "final", "balance", "rest")
    if not _has_keyword(lower, triggers_pay):
        return None
    if not _has_keyword(lower, triggers_final):
        return None
    m = re.search(r"(?:заказ|order)?\s*#?\s*(\d+)", lower)
    if not m:
        return None
    return ("pay_final", {"order_id": int(m.group(1))})


@rule("top_suppliers")
def _suppliers(msg: str, lower: str) -> tuple[str, dict] | None:
    triggers = ("топ поставщик", "лучшие поставщик", "сравни поставщик",
                "сравнить поставщик", "best suppliers", "compare suppliers",
                "top suppliers")
    if _has_keyword(lower, triggers):
        m = re.search(r"топ[\-\s]*(\d+)", lower)
        limit = int(m.group(1)) if m else 3
        return ("top_suppliers", {"limit": limit})
    return None


# ── Public API ───────────────────────────────────────────────
def match(message: str, role: str) -> tuple[str, dict, str] | None:
    """Try every rule in order. Return (action, params, rule_name) or None.

    Не матчим действия, на которые у роли нет прав, чтобы не показывать
    «нет прав» вместо нормального ответа от LLM.
    """
    if not message or not message.strip():
        return None
    lower = message.lower().strip()
    try:
        from . import actions as _actions
        allowed = _actions.can_execute
    except Exception:
        allowed = lambda a, r: True
    for name, fn in RULES:
        result = fn(message, lower)
        if result is None:
            continue
        action, params = result
        if not allowed(action, role):
            continue  # роль не может — даём фразе уйти в LLM
        return (action, params, name)
    return None
