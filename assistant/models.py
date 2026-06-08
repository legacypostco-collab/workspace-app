"""AI Assistant models — Conversation, Message, KnowledgeChunk, Feedback.

Vector storage uses pgvector when DATABASE_URL points to Postgres.
For SQLite (local dev), embeddings are stored as JSON blobs and search
falls back to in-Python cosine similarity (slow but functional).
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# pgvector is only available on Postgres. Fallback to JSONField on SQLite.
try:
    from pgvector.django import VectorField
    _PGVECTOR = True
except Exception:
    _PGVECTOR = False


def _embedding_field():
    """Use pgvector VectorField on Postgres, JSONField elsewhere."""
    if _PGVECTOR and "postgres" in settings.DATABASES["default"]["ENGINE"]:
        return VectorField(dimensions=1536, null=True, blank=True)
    return models.JSONField(null=True, blank=True, help_text="Vector as JSON list (SQLite fallback)")


class Conversation(models.Model):
    ROLE_CHOICES = [
        ("buyer", _("Покупатель")),
        ("seller", _("Поставщик")),
        ("operator_logist", _("Логист")),
        ("operator_customs", _("Таможенный брокер")),
        ("operator_payment", _("Платёжный агент")),
        ("operator_manager", _("Менеджер по продажам")),
        ("admin", _("Администратор")),
    ]
    # Категория группирует похожие действия в один долгий чат, чтобы не плодить
    # отдельный conv на каждый клик пилюли. Заголовок при этом меняется
    # динамически по текущему действию ("Верификация · Шаг 2/5", "Команда…").
    CATEGORY_CHOICES = [
        ("general",  _("Общее")),         # default — search, RFQ, разговор
        ("admin",    _("Управление")),    # KYB, team, integrations, settings
        ("purchase", _("Покупка")),       # quick_order, pay_*, track_order, claims
        ("support",  _("Поддержка")),     # claim disputes, op_resolve
        # ТЗ: chat-type меняется по жизненному циклу сделки.
        ("calc",     _("Расчёт")),        # RFQ → КП → reserve confirm
        ("shipment", _("Сделка")),        # после reserve_paid: трекинг, оплаты, доставка
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="assistant_conversations",
    )
    project = models.ForeignKey(
        "Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations",
    )
    role = models.CharField(max_length=30, choices=ROLE_CHOICES, default="buyer")
    category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, default="general", db_index=True,
        help_text="Группа для reuse чата по типу задачи (admin/purchase/support/general)",
    )
    title = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "-updated_at"]),
            models.Index(fields=["user", "category", "-updated_at"], name="conv_user_cat_idx"),
        ]

    def __str__(self):
        return f"Conv[{self.id}]:{self.user_id}:{self.title or 'untitled'}"


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user", _("Пользователь")
        ASSISTANT = "assistant", _("Ассистент")
        SYSTEM = "system", _("Системное")
        ACTION = "action", _("Действие")  # User clicked an action button

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField(help_text="Markdown text + ::: card blocks")
    # Chat-First TZ: structured cards & action buttons inside messages
    cards = models.JSONField(
        default=list, blank=True,
        help_text='Cards: [{"type":"product","data":{...}}, ...]',
    )
    actions = models.JSONField(
        default=list, blank=True,
        help_text='Buttons: [{"label":"...","action":"...","params":{...}}]',
    )
    context_refs = models.JSONField(default=list, blank=True)
    tokens_used = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["conversation", "created_at"])]


class KnowledgeChunk(models.Model):
    class SourceType(models.TextChoices):
        PRODUCT = "product", _("Товар")
        BRAND = "brand", _("Бренд")
        CATEGORY = "category", _("Категория")
        ORDER = "order", _("Заказ")
        RFQ = "rfq", _("RFQ")
        SHIPMENT = "shipment", _("Отгрузка")
        DOCUMENT = "document", _("Документ")
        REGULATION = "regulation", _("Регламент")
        FAQ = "faq", _("FAQ")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    source_id = models.CharField(max_length=100)
    title = models.CharField(max_length=300)
    content = models.TextField()
    embedding = _embedding_field()
    metadata = models.JSONField(default=dict, blank=True)
    language = models.CharField(
        max_length=5,
        default="ru",
        choices=[("ru", "Русский"), ("en", "English"), ("zh", "中文")],
    )
    access_roles = models.JSONField(
        default=list,
        help_text='Roles allowed to access: ["buyer","seller","operator_logist", ...]',
    )
    is_active = models.BooleanField(default=True, db_index=True)
    indexed_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["source_type", "source_id"]),
            models.Index(fields=["language", "is_active"]),
        ]
        unique_together = [("source_type", "source_id")]

    def __str__(self):
        return f"{self.source_type}:{self.source_id}:{self.title[:60]}"


class Feedback(models.Model):
    message = models.OneToOneField(
        Message, on_delete=models.CASCADE, related_name="feedback"
    )
    rating = models.SmallIntegerField(choices=[(1, "👍"), (-1, "👎")])
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)


# ── Chat-First Projects (workspace folders for grouping chats/RFQs/orders) ──
class Project(models.Model):
    DOT_COLORS = [
        ("green", "Green"),
        ("orange", "Orange"),
        ("blue", "Blue"),
        ("purple", "Purple"),
        ("red", "Red"),
        ("gray", "Gray"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="projects",
    )
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50, blank=True, help_text="Short code, e.g. NORQ2")
    customer = models.CharField(max_length=200, blank=True,
                                  help_text="Customer name (Norilsk Nickel — Kola Division)")
    customer_ref = models.ForeignKey(
        "marketplace.Customer", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="projects",
        help_text="Заказчик из CRM продавца (если проект заведён под конкретного заказчика)")
    tags = models.JSONField(default=list, blank=True,
                              help_text='Free-form tags: ["квартальная закупка","CAT 988H","793F"]')
    deadline = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True)
    dot_color = models.CharField(max_length=10, choices=DOT_COLORS, default="green")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["owner", "is_active", "-updated_at"])]

    def __str__(self):
        return f"{self.name} ({self.owner_id})"


class ProjectDocument(models.Model):
    DOC_TYPES = [
        ("spec", "Спецификация"),
        ("fleet", "Парк техники"),
        ("drawing", "Чертёж"),
        ("regulation", "Регламент ТО"),
        ("conditions", "Условия"),
        ("contract", "Договор"),
        ("invoice", "Счёт"),
        # Seller (товарное направление):
        ("pricelist", "Прайс-лист"),
        ("certificate", "Сертификат"),
        ("photo", "Фото товара"),
        # Operator (сделка/консолидированная поставка):
        ("customs", "Таможенный документ"),
        ("logistics", "Логистика"),
        ("payment", "Платёжный документ"),
        ("other", "Другое"),
    ]
    STATUS = [
        ("uploaded", "Загружен"),
        ("processing", "Обработка"),
        ("processed", "Обработан"),
        ("error", "Ошибка"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="documents")
    name = models.CharField(max_length=300)
    file = models.FileField(upload_to="projects/%Y/%m/", null=True, blank=True)
    doctype = models.CharField(max_length=20, choices=DOC_TYPES, default="other")
    status = models.CharField(max_length=20, choices=STATUS, default="processed")
    size_bytes = models.IntegerField(default=0)
    meta = models.JSONField(default=dict, blank=True,
                              help_text='{rows, pages, units, ...}')
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-uploaded_at"]


class Wallet(models.Model):
    """Депозит покупателя — простая модель: один кошелёк на пользователя.

    На демо-аккаунтах автоматически наполняется при первом обращении.
    Поле `balance` — текущий доступный остаток в USD (для упрощения — одна валюта).
    Транзакции пишутся в WalletTx.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet"
    )
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=10, default="USD")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_user(cls, user, *, demo_seed_amount=50000):
        """Get-or-create. Демо-аккаунтам (demo_*) выдаём стартовый баланс."""
        from decimal import Decimal
        wallet, created = cls.objects.get_or_create(user=user)
        if created and (user.username or "").startswith("demo_"):
            wallet.balance = Decimal(str(demo_seed_amount))
            wallet.save(update_fields=["balance", "updated_at"])
            WalletTx.objects.create(
                wallet=wallet, amount=wallet.balance, kind="topup",
                description="Демо-депозит",
            )
        return wallet


class WalletTx(models.Model):
    """Лог движений по кошельку: пополнения, списания, эскроу-операции."""
    KIND_CHOICES = [
        ("topup", "Пополнение"),
        ("debit", "Списание"),
        ("refund", "Возврат"),
        ("escrow_hold", "Эскроу-холд"),
        ("escrow_release", "Эскроу → продавцу"),
        ("escrow_refund", "Эскроу → возврат"),
    ]
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="transactions")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    description = models.CharField(max_length=300, blank=True)
    order_id = models.IntegerField(null=True, blank=True, db_index=True)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]


class WalletTopupRequest(models.Model):
    """Заявка на пополнение депозита (production flow).

    Юзер → создаёт заявку (сумма + способ оплаты) → видит реквизиты →
    оплачивает за пределами платформы (банк wire) или редиректится
    в платёжный шлюз (card / USDT). Оператор финансы видит pending
    заявки в своей очереди и подтверждает поступление средств → wallet
    автоматически кредитуется через `mark_paid()` метод.

    Состояния: pending → awaiting_confirmation → paid (или cancelled/failed).
    """
    METHOD_CHOICES = [
        ("bank_wire", "Банковский перевод"),
        ("card",      "Банковская карта"),
        ("usdt",      "USDT (TRC-20)"),
    ]
    STATUS_CHOICES = [
        ("pending",                "Ожидает оплаты"),
        ("awaiting_confirmation",  "Юзер сообщил об оплате — ждём подтверждения"),
        ("paid",                   "Оплачено — депозит пополнен"),
        ("cancelled",              "Отменено пользователем"),
        ("failed",                 "Платёж не прошёл"),
        ("expired",                "Истёк срок"),
    ]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="topup_requests",
    )
    amount   = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=10, default="USD")
    method   = models.CharField(max_length=20, choices=METHOD_CHOICES)
    status   = models.CharField(max_length=30, choices=STATUS_CHOICES,
                                 default="pending", db_index=True)
    # Реф-код для мэтчинга банковского перевода (юзер указывает в назначении
    # платежа). Уникальный, 8 символов hex.
    reference_code = models.CharField(max_length=16, unique=True, db_index=True)
    # Реквизиты, выданные юзеру при создании (snapshot — на случай если поменяем).
    payment_details = models.JSONField(default=dict, blank=True)
    # Кто из операторов финансов подтвердил.
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    confirmed_at  = models.DateTimeField(null=True, blank=True)
    user_claim_at = models.DateTimeField(null=True, blank=True,
                                          help_text="Когда юзер кликнул «Я оплатил»")
    cancelled_at  = models.DateTimeField(null=True, blank=True)
    note          = models.CharField(max_length=400, blank=True)
    created_at    = models.DateTimeField(default=timezone.now)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self):
        return f"Topup[{self.id}] {self.user_id} {self.amount}{self.currency} {self.status}"

    @classmethod
    def make_ref(cls) -> str:
        import secrets
        for _ in range(10):
            ref = "DEP-" + secrets.token_hex(4).upper()
            if not cls.objects.filter(reference_code=ref).exists():
                return ref
        # на всякий случай — fallback c timestamp, шансов 0
        import time
        return f"DEP-{int(time.time() * 1000):X}"

    def mark_paid(self, *, by_user=None) -> "WalletTx":
        """Атомарно: статус → paid, кошелёк пополнен, WalletTx создан.

        SECURITY P0-5/P0-6: select_for_update на заявке + re-check статуса
        под блокировкой. Два оператора, одновременно жмущие «Подтвердить
        пополнение», не должны зачислить деньги дважды.
        """
        from django.db import transaction
        # Быстрая проверка без блокировки — оптимизация
        if self.status == "paid":
            return WalletTx.objects.filter(
                wallet__user=self.user, kind="topup",
                description__contains=self.reference_code,
            ).first()
        with transaction.atomic():
            # Перепроверяем под row-lock — другой запрос мог уже зачислить
            locked = (type(self).objects.select_for_update()
                      .get(pk=self.pk))
            if locked.status == "paid":
                return WalletTx.objects.filter(
                    wallet__user=locked.user, kind="topup",
                    description__contains=locked.reference_code,
                ).first()
            locked.status = "paid"
            locked.confirmed_by = by_user
            locked.confirmed_at = timezone.now()
            locked.save(update_fields=["status", "confirmed_by",
                                         "confirmed_at", "updated_at"])
            wallet = Wallet.objects.select_for_update().get(
                pk=Wallet.for_user(locked.user).pk,
            )
            wallet.balance = wallet.balance + locked.amount
            wallet.save(update_fields=["balance", "updated_at"])
            tx = WalletTx.objects.create(
                wallet=wallet, kind="topup", amount=locked.amount,
                description=f"Пополнение по заявке {locked.reference_code}",
                balance_after=wallet.balance,
            )
            # Синхронизируем self чтобы caller получил актуальное состояние
            self.refresh_from_db()
        # Реферал: если пополнивший — покупатель-пригласивший, зачислить его
        # buyer_discount −$100 (отдельной транзакцией, после фиксации пополнения).
        try:
            from . import referral as _ref
            _ref.on_deposit_funded(self.user)
        except Exception:
            pass
        return tx
