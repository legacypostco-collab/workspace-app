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
    from pgvector.django import VectorField, HnswIndex
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
    title = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["user", "-updated_at"])]

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
