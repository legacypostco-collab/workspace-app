from django.utils import translation
from django.utils.translation import gettext_lazy as _
from .models import Order, Part, RFQ, SellerImportRun


def auth_meta(request):
    role = None
    seller_permissions = {}
    seller_department = None
    if request.user.is_authenticated:
        if request.user.is_superuser:
            role = "seller"
            seller_permissions = {
                "can_manage_assortment": True,
                "can_manage_pricing": True,
                "can_manage_orders": True,
                "can_manage_drawings": True,
                "can_view_analytics": True,
                "can_manage_team": True,
            }
            seller_department = "director"
        else:
            profile = getattr(request.user, "profile", None)
            role = profile.role if profile else "buyer"
            if profile and role == "seller":
                seller_permissions = {
                    "can_manage_assortment": bool(profile.can_manage_assortment),
                    "can_manage_pricing": bool(profile.can_manage_pricing),
                    "can_manage_orders": bool(profile.can_manage_orders),
                    "can_manage_drawings": bool(profile.can_manage_drawings),
                    "can_view_analytics": bool(profile.can_view_analytics),
                    "can_manage_team": bool(profile.can_manage_team),
                }
                seller_department = profile.department
    compare_raw = request.session.get("compare_parts", [])
    compare_count = 0
    for x in compare_raw:
        try:
            int(x)
            compare_count += 1
        except Exception:
            continue
    language_code = (translation.get_language() or "ru").lower()
    if language_code.startswith("en"):
        lang_key = "en"
    elif language_code.startswith("zh"):
        lang_key = "zh"
    else:
        lang_key = "ru"

    ui = {
        "ru": {
            "home": _("Главная"),
            "navigation": _("Навигация"),
            "catalog": _("Каталог"),
            "brands": _("Бренды"),
            "categories": _("Категории"),
            "compare": _("Сравнение"),
            "sales": _("Продажи"),
            "new_rfq": _("Новый RFQ"),
            "rfq_quotes": _("RFQ и котировки"),
            "demo_center": "Demo Center",
            "cart": _("Корзина"),
            "supplier": _("Поставщик"),
            "seller_cabinet": _("Кабинет поставщика"),
            "seller_orders": _("Заказы поставщика"),
            "operator_queue": _("Очередь оператора"),
            "operator_webhooks": _("Webhook логи"),
            "engineering": _("Инженерия"),
            "drawings_docs": _("Чертежи и документы"),
            "finance": _("Финансы"),
            "prices_discounts": _("Цены и скидки"),
            "payouts": _("Выплаты и удержания"),
            "analytics": _("Аналитика"),
            "reports_kpi": _("Отчёты и KPI"),
            "my_cabinet": _("Мой кабинет"),
            "buyer_orders": _("Заказы покупателя"),
            "overview": _("Обзор"),
            "callback": _("Обратный звонок"),
            "you_logged_as": _("Вы вошли как"),
            "cabinet": _("Кабинет"),
            "logout": _("Выйти"),
            "register": _("Зарегистрироваться"),
            "login": _("Войти"),
        },
        "en": {
            "home": "Home",
            "navigation": "Navigation",
            "catalog": "Catalog",
            "brands": "Brands",
            "categories": "Categories",
            "compare": "Compare",
            "sales": "Sales",
            "new_rfq": "New RFQ",
            "rfq_quotes": "RFQ & Quotes",
            "demo_center": "Demo Center",
            "cart": "Cart",
            "supplier": "Supplier",
            "seller_cabinet": "Supplier Cabinet",
            "seller_orders": "Supplier Orders",
            "operator_queue": "Operator Queue",
            "operator_webhooks": "Webhook Logs",
            "engineering": "Engineering",
            "drawings_docs": "Drawings & Docs",
            "finance": "Finance",
            "prices_discounts": "Pricing & Discounts",
            "payouts": "Payouts & Retentions",
            "analytics": "Analytics",
            "reports_kpi": "Reports & KPI",
            "my_cabinet": "My Cabinet",
            "buyer_orders": "Buyer Orders",
            "overview": "Overview",
            "callback": "Request Call",
            "you_logged_as": "Signed in as",
            "cabinet": "Cabinet",
            "logout": "Logout",
            "register": "Register",
            "login": "Login",
        },
        "zh": {
            "home": "首页",
            "navigation": "导航",
            "catalog": "目录",
            "brands": "品牌",
            "categories": "分类",
            "compare": "对比",
            "sales": "销售",
            "new_rfq": "新建 RFQ",
            "rfq_quotes": "RFQ 与报价",
            "demo_center": "Demo Center",
            "cart": "购物车",
            "supplier": "供应商",
            "seller_cabinet": "供应商后台",
            "seller_orders": "供应商订单",
            "operator_queue": "运营队列",
            "operator_webhooks": "Webhook 日志",
            "engineering": "工程",
            "drawings_docs": "图纸与文档",
            "finance": "财务",
            "prices_discounts": "价格与折扣",
            "payouts": "结算与预留",
            "analytics": "分析",
            "reports_kpi": "报表与KPI",
            "my_cabinet": "我的后台",
            "buyer_orders": "采购订单",
            "overview": "概览",
            "callback": "回电请求",
            "you_logged_as": "当前用户",
            "cabinet": "后台",
            "logout": "退出",
            "register": "注册",
            "login": "登录",
        },
    }[lang_key]

    is_demo = request.user.is_authenticated and request.user.username.startswith("demo_")

    # Admin moderation count
    admin_moderation_count = 0
    if request.user.is_authenticated and request.user.is_superuser:
        from .models import UserProfile, OrderClaim
        admin_moderation_count = (
            UserProfile.objects.filter(role="seller", supplier_status="sandbox").count()
            + OrderClaim.objects.exclude(status="closed").count()
        )
        # blocked parts — fast exists check only
        if Part.objects.filter(availability_status="blocked").exists():
            admin_moderation_count += 1

    return {
        "current_role": role,
        "compare_count": compare_count,
        "seller_permissions": seller_permissions,
        "seller_department": seller_department,
        "language_code": lang_key,
        "ui": ui,
        "is_demo": is_demo,
        "admin_moderation_count": admin_moderation_count,
    }


def seller_context(request):
    if not request.user.is_authenticated:
        return {}

    profile = getattr(request.user, "profile", None)
    if not profile or profile.role != "seller":
        return {}

    seller = request.user
    seller_products_active = Part.objects.filter(seller=seller, is_active=True).count()
    seller_requests_new = RFQ.objects.filter(items__matched_part__seller=seller).distinct().count()
    seller_orders_action = Order.objects.filter(items__part__seller=seller).distinct().filter(
        status__in=["confirmed", "in_production", "ready_to_ship"]
    ).count()
    seller_sla_alert = Order.objects.filter(items__part__seller=seller).distinct().filter(
        sla_status__in=["at_risk", "breached"]
    ).count()
    seller_imports_total = SellerImportRun.objects.filter(seller=seller).count()

    seller_nav_items = [
        {"key": "dashboard", "label": _("Дашборд"), "url_name": "seller_dashboard", "badge": None, "enabled": True},
        {"key": "products", "label": _("Товары и прайсы"), "url_name": "seller_product_list", "badge": seller_products_active, "enabled": True},
        {"key": "requests", "label": _("Запросы клиентов"), "url_name": "seller_request_list", "badge": seller_requests_new, "enabled": True},
        {"key": "orders", "label": _("Заказы"), "url_name": "seller_orders", "badge": seller_orders_action, "enabled": True},
        {"key": "discounts", "label": _("Согласование"), "url_name": "seller_negotiations", "badge": None, "enabled": True},
        {"key": "sla", "label": _("Контроль SLA"), "url_name": "seller_sla", "badge": seller_sla_alert, "enabled": True},
        {"key": "logistics", "label": _("Логистика"), "url_name": "seller_logistics", "badge": None, "enabled": True},
        {"key": "drawings", "label": _("Чертежи"), "url_name": "seller_drawings", "badge": None, "enabled": True},
        {"key": "qr", "label": _("QR-контроль"), "url_name": "seller_qr_control", "badge": None, "enabled": True},
        {"key": "_group", "label": _("АНАЛИТИКА"), "collapsible": True},
        {"key": "analytics", "label": _("Аналитика"), "url_name": "seller_analytics", "badge": None, "enabled": True},
        {"key": "finance", "label": _("Финансы"), "url_name": "seller_finance", "badge": None, "enabled": True},
        {"key": "rating", "label": _("Рейтинг"), "url_name": "seller_rating", "badge": None, "enabled": True},
        {"key": "_group", "label": _("НАСТРОЙКИ"), "collapsible": True},
        {"key": "team", "label": _("Команда"), "url_name": "seller_team", "badge": None, "enabled": True},
        {"key": "integrations", "label": _("Интеграции"), "url_name": "seller_integrations", "badge": None, "enabled": True},
    ]

    return {
        "seller_supplier": seller,
        "seller_nav_items": seller_nav_items,
        "seller_badge_requests": seller_requests_new,
        "seller_badge_orders_action": seller_orders_action,
        "seller_badge_products_active": seller_products_active,
        "seller_badge_imports_total": seller_imports_total,
        "seller_rating_score": profile.rating_score,
        "seller_status_label": profile.get_supplier_status_display(),
        "seller_company_name": profile.company_name,
        "seller_team_department": profile.get_department_display(),
    }


def buyer_context(request):
    if not request.user.is_authenticated:
        return {}

    profile = getattr(request.user, "profile", None)
    if not profile or profile.role != "buyer":
        return {}

    buyer = request.user
    buyer_orders_count = Order.objects.filter(buyer=buyer).count()
    buyer_active_orders = Order.objects.filter(buyer=buyer).exclude(
        status__in=["delivered", "completed", "cancelled"]
    ).count()
    buyer_rfq_count = RFQ.objects.filter(created_by=buyer).count()
    buyer_active_rfq = RFQ.objects.filter(created_by=buyer).exclude(
        status__in=["cancelled"]
    ).count()

    is_demo = buyer.username.startswith("demo_")

    if is_demo:
        buyer_nav_items = [
            {"key": "dashboard", "label": _("Дашборд"), "url_name": "buyer_dashboard", "badge": None, "enabled": True},
            {"key": "rfq", "label": _("Запросы RFQ"), "url_name": "buyer_rfq_list", "badge": buyer_active_rfq or None, "enabled": True},
            {"key": "orders", "label": _("Заказы"), "url_name": "buyer_orders", "badge": buyer_active_orders or None, "enabled": True},
            {"key": "shipments", "label": _("Отгрузки"), "url_name": "buyer_shipments", "badge": None, "enabled": True},
            {"key": "claims", "label": _("Рекламации"), "url_name": "buyer_claims", "badge": None, "enabled": True},
            {"key": "suppliers", "label": _("Поставщики"), "url_name": "buyer_suppliers", "badge": None, "enabled": True},
            {"key": "negotiations", "label": _("Переторжка"), "url_name": "buyer_negotiations", "badge": None, "enabled": True},
            {"key": "finance", "label": _("Финансы"), "url_name": "buyer_finance", "badge": None, "enabled": True},
            {"key": "catalog", "label": _("Избранное"), "url_name": "buyer_catalog", "badge": None, "enabled": True},
            {"key": "analytics", "label": _("Аналитика"), "url_name": "buyer_analytics", "badge": None, "enabled": True},
        ]
    else:
        buyer_nav_items = [
            {"key": "dashboard", "label": _("Дашборд"), "url_name": "buyer_dashboard", "badge": None, "enabled": True},
            {"key": "catalog", "label": _("Каталог"), "url_name": "buyer_catalog", "badge": None, "enabled": True},
            {"key": "rfq", "label": _("Запросы"), "url_name": "buyer_rfq_list", "badge": buyer_active_rfq or None, "enabled": True},
            {"key": "orders", "label": _("Заказы"), "url_name": "buyer_orders", "badge": buyer_active_orders or None, "enabled": True},
            {"key": "shipments", "label": _("Поставки"), "url_name": "buyer_shipments", "badge": None, "enabled": True},
        ]

    return {
        "buyer_nav_items": buyer_nav_items,
        "buyer_orders_count": buyer_orders_count,
        "buyer_active_orders": buyer_active_orders,
        "buyer_rfq_count": buyer_rfq_count,
        "buyer_active_rfq": buyer_active_rfq,
        "buyer_company_name": profile.company_name,
    }
