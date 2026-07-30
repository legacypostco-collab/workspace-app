"""Создать/обновить аккаунты команды + тестовые аккаунты С ТЕСТОВЫМИ ДАННЫМИ.

Идемпотентно (get_or_create по username) — безопасно запускать повторно.
По умолчанию НИЧЕГО реального не удаляет (только старые _t1/_t2/_t3 прошлой
версии сидера).

Структура (по реальным ролям):
  • KAM (operator_manager):  личный + 3 теста (покупатель/продавец/оператор)
  • Seller:                  личный + 3 теста
  • Разработка (admin):      личный admin + 3 теста
  • 7 безымянных:            client01..client07 (покупатели)

Тестовые данные под сущность:
  • покупатель → RFQ + 2 заказа (активный + доставленный)
  • продавец   → каталог из 4 позиций
  • оператор   → 2 заказа в его очереди (assigned_operator); у KAM — заказ,
                 привязанный к клиенту CRM (customer_ref + assigned_kam)

Запуск:
  python manage.py seed_team_accounts
  python manage.py seed_team_accounts --no-data         # только аккаунты
  TEAM_PASSWORD=… python manage.py seed_team_accounts
"""
from __future__ import annotations

import os
from decimal import Decimal
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth.models import User
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

# (display_name, username_base, role, operator_role, is_admin)
# Логины по имени — узнаваемо и не громоздко.
TEAM = [
    ("Константин К",     "konstantin_k",   "operator", "manager", False),
    ("Аркадий П",        "arkadiy_p",      "operator", "manager", False),
    ("Дмитрий Б",        "dmitriy_b",      "operator", "manager", False),
    ("Денис М",          "denis_m",        "seller",   "",        False),
    ("Владислав Д",      "vladislav_d",    "seller",   "",        False),
    ("Евгений А",        "evgeniy_a",      "seller",   "",        False),
    ("Даниил П",         "daniil_p",       "seller",   "",        False),
    ("Али",              "ali",            "seller",   "",        False),
    ("Альбина",          "albina",         "seller",   "",        False),
    ("Кирилл (разраб.)", "kirill",         "buyer",    "",        True),
    ("Никита М",         "nikita_m",       "buyer",    "",        True),
    ("Александр Зенит",  "aleksandr_zenit","buyer",    "",        False),  # без админа
]
# 3 тест-аккаунта на РАЗНЫЕ сущности: (suffix, role, operator_role, label)
TEST_ENTITIES = [
    ("_buyer",    "buyer",    "", "Покупатель"),
    ("_seller",   "seller",   "", "Продавец"),
    ("_operator", "operator", "", "Оператор"),
]
NUM_ANON = 7


class Command(BaseCommand):
    help = "Аккаунты команды + тестовые аккаунты с тестовыми данными (идемпотентно)."

    def add_arguments(self, parser):
        parser.add_argument("--wallet", type=int, default=50000)
        parser.add_argument("--password", type=str, default="")
        parser.add_argument(
            "--allow-production",
            action="store_true",
            help="Явно разрешить создание и сброс паролей в боевой среде.",
        )
        parser.add_argument("--no-data", action="store_true",
                            help="Не сеять тестовые данные (только аккаунты).")

    def handle(self, *args, **opts):
        from marketplace.models import UserProfile, Customer
        from assistant.models import Wallet, WalletTx

        if not settings.DEBUG and not opts.get("allow_production"):
            raise CommandError(
                "Команда меняет пароли и запрещена в боевой среде без "
                "--allow-production."
            )
        password = (opts.get("password") or os.getenv("TEAM_PASSWORD") or "").strip()
        if not password:
            raise CommandError(
                "Передайте уникальный пароль через --password или TEAM_PASSWORD."
            )
        if len(password) < 10:
            raise CommandError("Пароль должен содержать не менее 10 символов.")
        wallet_amount = Decimal(str(opts.get("wallet") or 0))
        created_rows, updated_rows = [], []
        sellers, buyers, operators, kam_links = [], [], [], []
        kams = []  # KAM-менеджеры (operator_role="manager") для привязки клиентов

        def ensure_user(username, *, display, role, operator_role="",
                        is_admin=False, company=""):
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@consolidatorparts.com"})
            user.set_password(password)
            user.first_name = display[:30]
            user.is_staff = bool(is_admin)
            user.is_superuser = bool(is_admin)
            if not user.email:
                user.email = f"{username}@consolidatorparts.com"
            user.save()
            prof, _ = UserProfile.objects.get_or_create(user=user)
            prof.role = role
            prof.operator_role = operator_role or ""
            if company:
                prof.company_name = company
            prof.save()
            if wallet_amount > 0:
                w = Wallet.for_user(user)
                if (w.balance or Decimal("0")) < wallet_amount:
                    w.balance = wallet_amount
                    w.save(update_fields=["balance", "updated_at"])
                    WalletTx.objects.get_or_create(
                        wallet=w, kind="topup", amount=wallet_amount,
                        defaults={"description": "Тестовое пополнение (seed)",
                                  "balance_after": wallet_amount})
            (created_rows if created else updated_rows).append(
                (username, role + (f"/{operator_role}" if operator_role else "")
                 + ("+admin" if is_admin else "")))
            return user

        with transaction.atomic():
            old = [f"{base}_t{i}" for _, base, *_ in TEAM for i in (1, 2, 3)]
            User.objects.filter(username__in=old).delete()

            for display, base, role, op_role, is_admin in TEAM:
                personal = ensure_user(base, display=display, role=role,
                                       operator_role=op_role, is_admin=is_admin,
                                       company=display)
                if role == "seller":
                    sellers.append(personal)
                elif role == "operator":
                    operators.append(personal)
                accts = {}
                for suffix, trole, top_role, label in TEST_ENTITIES:
                    a = ensure_user(f"{base}{suffix}",
                                    display=f"{display} · {label}",
                                    role=trole, operator_role=top_role,
                                    company=f"{display} — {label} (тест)")
                    accts[trole] = a
                buyers.append(accts["buyer"])
                sellers.append(accts["seller"])
                operators.append(accts["operator"])
                if role == "operator" and op_role == "manager":
                    kams.append(personal)
                    cust, _ = Customer.objects.get_or_create(
                        owner=personal, inn=f"TEST{personal.id:04d}1",
                        defaults={"name": f"{display} — клиент (тест)",
                                  "user": accts["buyer"],
                                  "note": "seed: тестовый клиент"})
                    kam_links.append((personal, cust, accts["buyer"]))

            # Безымянные — тоже по 3 аккаунта на разные сущности, БЕЗ личного.
            for n in range(1, NUM_ANON + 1):
                cbase = f"client{n:02d}"
                for suffix, trole, top_role, label in TEST_ENTITIES:
                    a = ensure_user(f"{cbase}{suffix}",
                                    display=f"Клиент {n:02d} · {label}",
                                    role=trole, operator_role=top_role,
                                    company=f"Клиент {n:02d} — {label} (тест)")
                    if trole == "buyer":
                        buyers.append(a)
                    elif trole == "seller":
                        sellers.append(a)
                    else:
                        operators.append(a)

            # KAM закрепляется за КАЖДЫМ тест-покупателем (round-robin),
            # чтобы блок «мой персональный менеджер» был заполнен у всех.
            if kams:
                for idx, b in enumerate({bu.id: bu for bu in buyers}.values()):
                    if Customer.objects.filter(user=b, owner__in=kams,
                                               is_active=True).exists():
                        continue
                    kam = kams[idx % len(kams)]
                    Customer.objects.get_or_create(
                        owner=kam, inn=f"77{b.id:08d}",
                        defaults={"name": (b.first_name or b.username),
                                  "country": "RU",
                                  "legal_address": "г. Москва, Тестовая ул., 1",
                                  "contact_name": (b.first_name or b.username),
                                  "phone": "+7 (495) 000-00-00",
                                  "user": b, "is_active": True,
                                  "note": "seed: KAM ведёт закупки клиента"})

        data_msg = enrich_msg = "пропущены (--no-data)"
        if not opts.get("no_data"):
            data_msg = self._seed_data(sellers, buyers, operators, kam_links)
            enrich_msg = self._enrich(sellers, buyers, operators)

        self.stdout.write(self.style.SUCCESS(f"\nГотово. Пароль: {password}"))
        self.stdout.write(f"Аккаунтов создано: {len(created_rows)} · обновлено: {len(updated_rows)}")
        self.stdout.write(f"Базовые данные: {data_msg}")
        self.stdout.write(f"Обогащение: {enrich_msg}")

    # ── Сидинг тестовых данных под сущность ──────────────────────────
    def _seed_data(self, sellers, buyers, operators, kam_links):
        from marketplace.models import (Part, Category, Brand, RFQ, RFQItem,
                                         Order, OrderItem, OrderEvent)
        cat = Category.objects.first()
        brand = Brand.objects.first()
        pool = list(Part.objects.filter(is_active=True, price__gt=0).order_by("id")[:6])
        if not pool or not cat:
            return "пропущены (нет каталога/категорий в БД)"
        now = timezone.now()
        stats = {"parts": 0, "rfqs": 0, "orders": 0}

        def mk_order(buyer, parts, status, pstatus, *, operator=None,
                     customer=None, kam=None):
            sub = sum((p.price * (i + 1) for i, p in enumerate(parts)), Decimal("0"))
            logi = Decimal("190.00")
            total = (sub + logi).quantize(Decimal("0.01"))
            o = Order.objects.create(
                customer_name=buyer.first_name or buyer.username,
                customer_email=buyer.email or f"{buyer.username}@chat.local",
                customer_phone="+7 999 000 00 00",
                delivery_address="Тестовый адрес, г. Москва",
                buyer=buyer, status=status, payment_status=pstatus,
                reserve_percent=Decimal("10.00"),
                reserve_amount=(total * Decimal("0.10")).quantize(Decimal("0.01")),
                reserve_paid_at=now, total_amount=total,
                logistics_cost=logi, logistics_currency="USD",
                ship_deadline=now + timedelta(days=6), sla_status="on_track")
            for i, p in enumerate(parts):
                OrderItem.objects.create(order=o, part=p, quantity=i + 1,
                                         unit_price=p.price)
            fields = []
            if operator is not None:
                o.assigned_operator = operator; fields.append("assigned_operator")
            if customer is not None:
                o.customer_ref = customer; fields.append("customer_ref")
            if kam is not None:
                o.assigned_kam = kam; fields.append("assigned_kam")
            if fields:
                o.save(update_fields=fields)
            OrderEvent.objects.create(order=o, event_type="order_created",
                                      source="buyer", actor=buyer,
                                      meta={"seed": True})
            stats["orders"] += 1
            return o

        # уникализируем для идемпотентности
        seen_sellers = {s.id for s in sellers}
        seen_buyers = {b.id for b in buyers}
        seen_ops = {o.id for o in operators}

        # Продавцы → каталог из 4 позиций
        for s in {s.id: s for s in sellers}.values():
            if Part.objects.filter(seller=s).exists():
                continue
            rows = [("Гидронасос", "DEMO-PUMP", Decimal("1480")),
                    ("Уплотнение", "DEMO-SEAL", Decimal("180")),
                    ("Клапан", "DEMO-VALVE", Decimal("860")),
                    ("Датчик давления", "DEMO-SENS", Decimal("210"))]
            for title, oem, price in rows:
                try:
                    Part.objects.create(
                        seller=s, oem_number=f"{oem}-{s.id}",
                        title=title, slug=f"{oem.lower()}-{uuid4().hex[:6]}",
                        description=f"{title} (тестовая позиция)", price=price,
                        stock_quantity=20, condition="oem", category=cat,
                        brand=brand, is_active=True, availability="in_stock",
                        availability_status="active", currency="USD",
                        incoterm="FOB", moq=1, country_of_origin="China")
                    stats["parts"] += 1
                except Exception:
                    pass

        # Покупатели → RFQ + 2 заказа
        for b in {b.id: b for b in buyers}.values():
            if Order.objects.filter(buyer=b).exists():
                continue
            try:
                rfq = RFQ.objects.create(
                    created_by=b, customer_name=b.first_name or b.username,
                    customer_email=b.email or f"{b.username}@chat.local",
                    mode="auto", urgency="standard", status="quoted",
                    notes="seed: тестовый запрос")
                for i, p in enumerate(pool[:3]):
                    RFQItem.objects.create(rfq=rfq, query=p.oem_number,
                        quantity=i + 1, matched_part=p, state="auto_matched",
                        confidence=Decimal("92.00"), decision_reason="seed",
                        recommended_supplier_status="trusted")
                stats["rfqs"] += 1
            except Exception:
                pass
            mk_order(b, pool[:3], "reserve_paid", "reserve_paid")
            mk_order(b, pool[:2], "delivered", "paid")

        # Операторы → 2 заказа в очередь (assigned_operator)
        client_buyers = [b for b in buyers
                         if b.username.startswith("client")] or buyers
        for idx, op in enumerate({o.id: o for o in operators}.values()):
            if Order.objects.filter(assigned_operator=op).exists():
                continue
            byr = client_buyers[idx % len(client_buyers)]
            mk_order(byr, pool[:3], "reserve_paid", "reserve_paid", operator=op)
            mk_order(byr, pool[:2], "shipped", "mid_paid", operator=op)

        # KAM → заказ, привязанный к клиенту CRM
        for kam, cust, buyer in kam_links:
            if Order.objects.filter(assigned_kam=kam).exists():
                continue
            mk_order(buyer, pool[:3], "reserve_paid", "reserve_paid",
                     operator=kam, customer=cust, kam=kam)

        return (f"товаров {stats['parts']}, RFQ {stats['rfqs']}, "
                f"заказов {stats['orders']}")

    # ── Обогащение: рекламации, уведомления, KYB, КП, чертежи, пайплайн ──
    def _enrich(self, sellers, buyers, operators):
        from marketplace.models import (OrderClaim, Notification,
                                         CompanyVerification, Quote, Drawing,
                                         Order, OrderItem, RFQ, Part)
        st = {"claims": 0, "notif": 0, "kyb": 0, "quotes": 0, "drawings": 0,
              "pipe": 0}

        def uniq(lst):
            return list({u.id: u for u in lst}.values())

        all_users = uniq(sellers + buyers + operators)

        # 1) Уведомления — всем (разные типы)
        NOTI = [("order", "Заказ обновлён", "Статус вашего заказа изменился."),
                ("payment", "Платёж принят", "Резерв по заказу зачислен."),
                ("rfq", "Новый ответ на запрос", "Поставщик прислал КП."),
                ("system", "Добро пожаловать", "Тестовый аккаунт готов к работе.")]
        for u in all_users:
            if Notification.objects.filter(user=u).exists():
                continue
            for kind, title, body in NOTI:
                try:
                    Notification.objects.create(user=u, kind=kind, title=title,
                                                body=body, is_read=False)
                    st["notif"] += 1
                except Exception:
                    pass

        # 2) KYB — продавцам (часть pending → попадут в очередь оператора),
        #    покупателям — verified. pending = «на проверке» у оператора.
        # Тест-продавцы verified (чтобы можно было тестировать продавцовые
        # действия без блокировок). Пару client-продавцов держим pending —
        # чтобы у оператора не пустовала очередь KYB на проверку.
        PENDING_KYB_DEMO = {"client01_seller", "client02_seller"}
        for u in uniq(sellers):
            if CompanyVerification.objects.filter(user=u).exists():
                continue
            status = "pending" if u.username in PENDING_KYB_DEMO else "verified"
            kw = dict(user=u, status=status, submitted_at=timezone.now(),
                      legal_name=f'ООО «{(u.first_name or u.username)[:40]}»',
                      inn=f"77{u.id:08d}"[:12])
            if status == "verified":
                kw["reviewed_at"] = timezone.now()
            try:
                CompanyVerification.objects.create(**kw)
                st["kyb"] += 1
            except Exception:
                pass
        for u in uniq(buyers):
            if CompanyVerification.objects.filter(user=u).exists():
                continue
            try:
                CompanyVerification.objects.create(
                    user=u, status="verified",
                    submitted_at=timezone.now(),
                    legal_name=f'ООО «{(u.first_name or u.username)[:40]}»',
                    inn=f"50{u.id:08d}"[:12])
                st["kyb"] += 1
            except Exception:
                pass

        # 3) Рекламации — покупателям (на доставленный заказ); операторы видят
        CLAIMS = [("defect", "Брак детали", "Получена деталь с дефектом корпуса."),
                  ("missing", "Недокомплект", "В поставке не хватает позиции.")]
        for j, b in enumerate(uniq(buyers)):
            if OrderClaim.objects.filter(opened_by=b).exists():
                continue
            # Рекламацию можно открыть ТОЛЬКО на доставленный/завершённый заказ
            # (open_claim гейтит delivered/completed): до отгрузки жаловаться не
            # на что. Берём такой; если у покупателя их нет — пропускаем.
            o = (Order.objects.filter(buyer=b, status__in=("delivered", "completed"))
                 .order_by("-id").first())
            if not o:
                continue
            kind, title, desc = CLAIMS[j % len(CLAIMS)]
            try:
                OrderClaim.objects.create(order=o, opened_by=b, kind=kind,
                                          status="open", title=title,
                                          description=desc)
                st["claims"] += 1
            except Exception:
                pass

        # 4) КП (Quote) — каждый продавец отвечает на RFQ, СОСТАВЛЕННЫЙ ИЗ ЕГО
        #    позиций. Раньше брали топ-2 свежих RFQ подряд (а это pool из Komatsu
        #    KM-*, которых у продавца нет — у него каталог DEMO-*-<id>) → «КП
        #    отправлен» по запросу с 0 позиций в каталоге (репорт пользователя:
        #    «как могла прийти заявка, где ни одной позиции нет на складе?»).
        from django.contrib.auth import get_user_model as _gum
        from marketplace.models import RFQItem as _RFQItem
        demo_buyer = (uniq(buyers) or [None])[0]
        # Кто ДОЛЖЕН иметь демо-КП: управляемые этой командой продавцы ∪ те, у
        # кого уже были seed-КП (интенция демо в т.ч. из других сидов — demo_seller
        # и пр.). Так чиним и аккаунты вне списка команды.
        intended_ids = {s.id for s in uniq(sellers)}
        intended_ids |= set(
            Quote.objects.filter(rfq__notes__icontains="seed:")
                 .values_list("seller_id", flat=True))
        # (a) ГЛОБАЛЬНАЯ самокоррекция: сносим ВСЕ нерелевантные seed-КП — на
        # запросах, где НИ ОДНА позиция не из каталога продавца. Сам seed-RFQ не
        # трогаем (легитимный запрос покупателя), убираем лишь бессмысленный КП.
        for q in list(Quote.objects.filter(
                rfq__notes__icontains="seed:").select_related("rfq")):
            if not _RFQItem.objects.filter(
                    rfq=q.rfq, matched_part__seller=q.seller).exists():
                q.delete()
        # (a2) Бэкфилл строк: у существующих seed-КП без QuoteItem создаём их из
        # позиций RFQ, принадлежащих продавцу — чтобы КП открывался составом.
        from django.db.models import Count as _Count
        from marketplace.models import QuoteItem as _QI0
        for q in (Quote.objects.filter(rfq__notes__icontains="seed:")
                  .annotate(_n=_Count("items")).filter(_n=0)):
            for ri in _RFQItem.objects.filter(
                    rfq=q.rfq, matched_part__seller=q.seller).select_related("matched_part"):
                p = ri.matched_part
                _QI0.objects.create(
                    quote=q, rfq_item=ri, part=p,
                    title_snapshot=(p.title or "")[:300], quantity=ri.quantity or 1,
                    unit_price=(p.price or Decimal("0")), condition="oem")

        # (b) Каждому intended-продавцу — когерентный seed-КП (RFQ из ЕГО позиций).
        for s in _gum().objects.filter(id__in=intended_ids):
            if Quote.objects.filter(seller=s, rfq__notes__icontains="seed:",
                                    rfq__items__matched_part__seller=s).exists():
                continue
            s_parts = list(Part.objects.filter(seller=s, is_active=True)[:3])
            if not demo_buyer or not s_parts:
                continue
            try:
                from marketplace.models import QuoteItem as _QI
                rfq = RFQ.objects.create(
                    created_by=demo_buyer,
                    customer_name=demo_buyer.first_name or demo_buyer.username,
                    customer_email=demo_buyer.email or f"{demo_buyer.username}@chat.local",
                    mode="auto", urgency="standard", status="quoted",
                    notes="seed: тестовый запрос")
                total = Decimal("0")
                made = []
                for i, p in enumerate(s_parts):
                    ri = _RFQItem.objects.create(
                        rfq=rfq, query=p.oem_number, quantity=i + 1,
                        matched_part=p, state="auto_matched",
                        confidence=Decimal("92.00"), decision_reason="seed",
                        recommended_supplier_status="trusted")
                    made.append((ri, p, i + 1))
                    total += (p.price or Decimal("0")) * (i + 1)
                quote = Quote.objects.create(
                    rfq=rfq, seller=s,
                    total_amount=(total or Decimal("1850.00")).quantize(Decimal("0.01")),
                    delivery_days=12, status="submitted")
                # QuoteItem — поценовые строки, чтобы КП открывался составом
                # (spec_results), а не пустым «Позиции: —».
                for ri, p, qty in made:
                    _QI.objects.create(
                        quote=quote, rfq_item=ri, part=p,
                        title_snapshot=(p.title or "")[:300], quantity=qty,
                        unit_price=(p.price or Decimal("0")), condition="oem")
                st["quotes"] += 1
            except Exception:
                pass

        # 5) Чертежи — покупателям и продавцам
        for u in uniq(buyers + sellers):
            if Drawing.objects.filter(seller=u).exists():
                continue
            for title, fmt, statd in [("Сборочный чертёж узла", "pdf", "approved"),
                                      ("Деталировка корпуса", "dwg", "on_review")]:
                try:
                    Drawing.objects.create(seller=u, title=title, file_format=fmt,
                                           status=statd, oem_number="DWG-0001")
                    st["drawings"] += 1
                except Exception:
                    pass

        # 6) Пайплайн продавца — заказ на ЕГО товары (видно в «Мои продажи»)
        now = timezone.now()
        a_buyer = (uniq(buyers) or [None])[0]
        for s in uniq(sellers):
            sparts = list(Part.objects.filter(seller=s)[:2])
            if not sparts or not a_buyer:
                continue
            if Order.objects.filter(items__part__seller=s).exists():
                continue
            try:
                sub = sum((p.price for p in sparts), Decimal("0"))
                total = (sub + Decimal("190")).quantize(Decimal("0.01"))
                o = Order.objects.create(
                    customer_name=a_buyer.username, customer_email=a_buyer.email,
                    delivery_address="Тестовый адрес", buyer=a_buyer,
                    status="in_production", payment_status="reserve_paid",
                    reserve_percent=Decimal("10"),
                    reserve_amount=(total * Decimal("0.1")).quantize(Decimal("0.01")),
                    reserve_paid_at=now, total_amount=total,
                    logistics_cost=Decimal("190"))
                for p in sparts:
                    OrderItem.objects.create(order=o, part=p, quantity=1,
                                             unit_price=p.price)
                st["pipe"] += 1
            except Exception:
                pass

        # 5) Глубина срезов: именованным покупателям ≥4 RFQ (блок котировок),
        #    именованным продавцам — открытая рекламация (блок претензий).
        from marketplace.models import RFQItem
        QDEPTH = [("Гидронасос Caterpillar 2W1223", 2), ("Уплотнение 1R0750", 5),
                  ("Клапан гидрораспределителя", 3), ("Датчик давления 320D", 4),
                  ("Подшипник ступицы", 6), ("Фильтр гидравлический HF6553", 8)]
        st["rfqs_depth"] = 0
        for u in uniq(buyers):
            if u.username.startswith("client"):
                continue
            have = RFQ.objects.filter(created_by=u).count()
            prof = getattr(u, "userprofile", None) or getattr(u, "profile", None)
            company = ((getattr(prof, "company_name", "") if prof else "")
                       or (u.get_full_name() or u.username))
            for k in range(have, 4):
                try:
                    rfq = RFQ.objects.create(
                        created_by=u, customer_name=(u.get_full_name() or u.username),
                        customer_email=(u.email or f"{u.username}@chat.local"),
                        company_name=company, mode=["semi", "auto", "manual"][k % 3],
                        urgency="standard", notes="Тестовый RFQ — проверка блока котировок")
                    off = (k * 2) % len(QDEPTH)
                    for q, qty in QDEPTH[off:off + 2]:
                        RFQItem.objects.create(rfq=rfq, query=q, quantity=qty)
                    st["rfqs_depth"] += 1
                except Exception:
                    pass
        cidx = 0
        for u in uniq(sellers):
            if u.username.startswith("client"):
                continue
            if OrderClaim.objects.filter(order__items__part__seller=u).exists():
                continue
            # Только доставленный/завершённый заказ — рекламация «на базисе
            # отгрузки», а не на этапе формирования/производства.
            o = (Order.objects.filter(items__part__seller=u,
                                      status__in=("delivered", "completed"))
                 .distinct().first())
            if not o:
                continue
            try:
                OrderClaim.objects.create(
                    order=o, kind=["defect", "missing", "late"][cidx % 3], status="open",
                    description="Тестовая рекламация: позиция не соответствует спецификации.")
                st["claims"] += 1
                cidx += 1
            except Exception:
                pass

        # 6) Глубина аналитики продавца: заказы по статусам (упор на
        #    delivered/completed → оборот), разнесены по месяцам, SLA-микс.
        #    Без них отчёты продавца пустые ($0 оборот, 0 закрытых).
        st["seller_orders"] = 0
        _now2 = timezone.now()
        _PLAN = [("delivered", "mid_paid"), ("completed", "full_paid"),
                 ("delivered", "mid_paid"), ("shipped", "mid_paid"),
                 ("in_production", "reserve_paid"), ("delivered", "mid_paid"),
                 ("ready_to_ship", "mid_paid"), ("completed", "full_paid"),
                 ("delivered", "mid_paid"), ("reserve_paid", "reserve_paid")]
        from django.db.models import Count as _Count
        _bpool = [b for b in uniq(buyers) if not b.username.startswith("client")] or uniq(buyers)
        # ВСЕ продавцы по каталогу (вкл. demo_seller и client*) — аналитика для всех.
        _seller_users = list(User.objects.annotate(_np=_Count("parts")).filter(_np__gt=0))
        for s in _seller_users:
            if not _bpool:
                break
            if Order.objects.filter(items__part__seller=s,
                                    status__in=("delivered", "completed")).distinct().count() >= 6:
                continue
            sparts = list(Part.objects.filter(seller=s, is_active=True)[:4])
            if not sparts:
                continue
            for i, (st_status, pst) in enumerate(_PLAN):
                ps = sparts[(i % len(sparts)):(i % len(sparts)) + 2] or sparts[:1]
                b = _bpool[i % len(_bpool)]
                sub = sum((p.price * Decimal((i % 3) + 1) for p in ps), Decimal("0"))
                total = (sub + Decimal("190")).quantize(Decimal("0.01"))
                try:
                    o = Order.objects.create(
                        customer_name=(b.first_name or b.username),
                        customer_email=(b.email or f"{b.username}@chat.local"),
                        customer_phone="+7 999 000 00 00",
                        delivery_address="Тестовый адрес, Москва", buyer=b,
                        status=st_status, payment_status=pst, reserve_percent=Decimal("10"),
                        reserve_amount=(total * Decimal("0.1")).quantize(Decimal("0.01")),
                        reserve_paid_at=_now2, total_amount=total,
                        logistics_cost=Decimal("190"), logistics_currency="USD",
                        ship_deadline=_now2 + timedelta(days=6),
                        sla_status=("breached" if i in (4, 7) else "on_track"))
                    for p in ps:
                        OrderItem.objects.create(order=o, part=p, quantity=(i % 3) + 1,
                                                 unit_price=p.price)
                    Order.objects.filter(id=o.id).update(
                        created_at=_now2 - timedelta(days=14 * i + 1))
                    st["seller_orders"] += 1
                except Exception:
                    pass

        return (f"уведомлений {st['notif']}, KYB {st['kyb']}, рекламаций "
                f"{st['claims']}, КП {st['quotes']}, чертежей {st['drawings']}, "
                f"заказов-в-пайплайне {st['pipe']}, RFQ-глубина {st['rfqs_depth']}, "
                f"заказов-продавцам {st['seller_orders']}")
