from django.utils import translation


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
            "home": "Главная",
            "navigation": "Навигация",
            "catalog": "Каталог",
            "brands": "Бренды",
            "categories": "Категории",
            "compare": "Сравнение",
            "sales": "Продажи",
            "new_rfq": "Новый RFQ",
            "rfq_quotes": "RFQ и котировки",
            "demo_center": "Demo Center",
            "cart": "Корзина",
            "supplier": "Поставщик",
            "seller_cabinet": "Кабинет поставщика",
            "seller_orders": "Заказы поставщика",
            "operator_queue": "Очередь оператора",
            "operator_webhooks": "Webhook логи",
            "engineering": "Инженерия",
            "drawings_docs": "Чертежи и документы",
            "finance": "Финансы",
            "prices_discounts": "Цены и скидки",
            "payouts": "Выплаты и удержания",
            "analytics": "Аналитика",
            "reports_kpi": "Отчёты и KPI",
            "my_cabinet": "Мой кабинет",
            "buyer_orders": "Заказы покупателя",
            "overview": "Обзор",
            "callback": "Обратный звонок",
            "you_logged_as": "Вы вошли как",
            "cabinet": "Кабинет",
            "logout": "Выйти",
            "register": "Зарегистрироваться",
            "login": "Войти",
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

    return {
        "current_role": role,
        "compare_count": compare_count,
        "seller_permissions": seller_permissions,
        "seller_department": seller_department,
        "language_code": lang_key,
        "ui": ui,
    }
