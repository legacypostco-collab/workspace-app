from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class HSCode(models.Model):
    """Справочник кодов HS (Harmonized System) — 2/4/6-значные."""
    LEVEL_CHOICES = [(2, "Глава"), (4, "Группа"), (6, "Субпозиция")]

    section   = models.CharField(max_length=5)
    hscode    = models.CharField(max_length=10, unique=True, db_index=True)
    description = models.CharField(max_length=500)
    parent    = models.ForeignKey("self", null=True, blank=True,
                    on_delete=models.SET_NULL, related_name="children",
                    to_field="hscode")
    level     = models.SmallIntegerField(choices=LEVEL_CHOICES)

    class Meta:
        ordering = ["hscode"]

    def __str__(self) -> str:
        return f"{self.hscode} — {self.description[:60]}"


class Category(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True)

    def __str__(self) -> str:
        return self.name


class Brand(models.Model):
    REGION_CHOICES = [
        ("global", "Global"),
        ("korea", "Korea"),
        ("china", "China"),
        ("europe", "Europe"),
        ("components", "Component Manufacturer"),
    ]

    name = models.CharField(max_length=140, unique=True)
    slug = models.SlugField(max_length=180, unique=True)
    region = models.CharField(max_length=20, choices=REGION_CHOICES, default="global")
    is_component_manufacturer = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class SellerWarehouse(models.Model):
    """Виртуальный склад продавца — папка для группировки позиций.

    Одна загрузка прайса = один склад с фиксированной логистикой
    (страна, порт морской, порт авиа, физический адрес). Позиции из
    разных стран = разные склады = разные загрузки. Это enforce'ит
    правило «одна загрузка = одна страна» на уровне данных.

    Имя можно переименовывать в любой момент (например, «Турция-Анкара»
    вместо автогенерированного «Склад #3»).
    """
    KIND_CHOICES = [
        ("pricelist", "Прайс — большой каталог, склад по логистике"),
        ("kp",        "КП/расценка — папка по заводу-поставщику"),
    ]
    seller = models.ForeignKey(User, on_delete=models.CASCADE,
        related_name="warehouses")
    name = models.CharField(max_length=120,
        help_text="Произвольное имя — продавец может менять")
    kind = models.CharField(max_length=12, choices=KIND_CHOICES,
        default="pricelist", db_index=True,
        help_text="pricelist=прайс (группа по логистике), kp=расценка (группа по заводу)")
    country_code = models.CharField(max_length=2, blank=True, db_index=True,
        help_text="ISO-код страны отгрузки (TR/CN/RU/...)")
    sea_port = models.CharField(max_length=120, blank=True)
    air_port = models.CharField(max_length=120, blank=True)
    address = models.TextField(blank=True,
        help_text="Полный адрес склада (страна, город, улица)")
    currency = models.CharField(max_length=3, default="USD")
    # Поставщик/завод для КП. Ключ группировки расценок — supplier_tax_id
    # (ИНН для РФ, USCC/единый код для Китая, VAT/рег.№ для прочих стран).
    # Название варьируется → ненадёжно; идентификатор стабилен.
    supplier_name = models.CharField(max_length=200, blank=True,
        help_text="Название завода-поставщика (для КП)")
    supplier_tax_id = models.CharField(max_length=40, blank=True, db_index=True,
        help_text="ИНН/USCC/VAT поставщика — ключ группировки КП")
    supplier_country = models.CharField(max_length=2, blank=True,
        help_text="Юрисдикция поставщика (для типа налогового ID)")
    is_full_catalog = models.BooleanField(default=False, db_index=True,
        help_text="КП-папка: полный каталог завода (отдельная на загрузку) "
                  "vs накопительная папка КП (дедуп по заводу)")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["seller", "-created_at"],
                          name="warehouse_seller_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.seller_id})"


class LogisticsTariff(models.Model):
    """Тариф международной доставки от origin до dest_country.

    Используется калькулятором (assistant.logistics.calc_logistics)
    для расчёта стоимости доставки одной позиции по габаритам и весу.

    Формула:
        volumetric_kg = L*W*H / divisor (5000=sea, 6000=air)
        chargeable_kg = max(actual_kg, volumetric_kg)
        cost = max(chargeable_kg * rate_per_kg, min_charge)
    """
    MODE_CHOICES = [
        ("sea", "Морем"),
        ("air", "Авиа"),
    ]
    SOURCE_CHOICES = [
        ("internal", "Внутренний тариф"),
        ("api_dhl",   "DHL API"),
        ("api_fedex", "FedEx API"),
        ("api_other", "Другой API"),
    ]
    origin_port = models.CharField(max_length=120, db_index=True,
        help_text="Порт отправления (CNNGB, TRMER, PKX и т.п.)")
    dest_country = models.CharField(max_length=2, db_index=True,
        help_text="ISO-код страны назначения (RU, KZ, ...)")
    mode = models.CharField(max_length=4, choices=MODE_CHOICES, db_index=True)
    rate_per_kg = models.DecimalField(max_digits=8, decimal_places=2,
        help_text="USD за килограмм billable weight")
    min_charge = models.DecimalField(max_digits=8, decimal_places=2,
        default=Decimal("0"),
        help_text="Минимальная стоимость отправления (USD)")
    transit_days = models.PositiveIntegerField(default=30,
        help_text="Среднее время в пути")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES,
        default="internal")
    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["origin_port", "dest_country", "mode", "source"],
                name="uniq_tariff_route",
            ),
        ]
        indexes = [
            models.Index(fields=["origin_port", "dest_country", "mode"],
                          name="tariff_route_idx"),
        ]
        ordering = ["origin_port", "dest_country", "mode"]

    def __str__(self):
        return f"{self.origin_port}→{self.dest_country} {self.mode} ${self.rate_per_kg}/кг"


class Part(models.Model):
    CONDITION_CHOICES = [
        ("oem", "OEM"),
        ("aftermarket", "Aftermarket"),
        ("reman", "REMAN"),
    ]

    AVAILABILITY_CHOICES = [
        ("in_stock", "IN_STOCK"),
        ("backorder", "BACKORDER"),
    ]
    AVAILABILITY_STATUS_CHOICES = [
        ("active", "active"),
        ("limited", "limited"),
        ("made_to_order", "made_to_order"),
        ("discontinued", "discontinued"),
        ("blocked", "blocked"),
    ]
    CURRENCY_CHOICES = [
        ("USD", "USD"),
        ("EUR", "EUR"),
        ("RUB", "RUB"),
        ("CNY", "CNY"),
    ]
    INCOTERM_CHOICES = [
        ("FOB", "FOB"),
        ("CIF", "CIF"),
        ("DDP", "DDP"),
    ]
    MAPPING_STATUS_CHOICES = [
        ("auto", "auto"),
        ("confirmed", "confirmed"),
        ("needs_review", "needs_review"),
    ]

    title = models.CharField(max_length=255)
    # Русское название из детерминированного словаря (assistant.part_naming).
    # Оригинал (title) НЕ трогаем — он нужен для заказа/таможни/сверки с поставщиком.
    # Пусто = перевода нет (фронт покажет оригинал). Заполняется backfill-командой.
    title_ru = models.CharField(max_length=255, blank=True, default="")
    slug = models.SlugField(max_length=280, unique=True)
    oem_number = models.CharField(max_length=100, db_index=True)
    oem_clean = models.CharField(max_length=100, blank=True, default="", db_index=True,
        help_text="OEM без спецсимволов, uppercase — для кроссреференса")
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    stock_quantity = models.PositiveIntegerField(default=0)
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default="oem")
    image_url = models.URLField(blank=True)
    seller = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="parts")
    brand = models.ForeignKey(Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name="parts")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="parts")
    availability = models.CharField(max_length=20, choices=AVAILABILITY_CHOICES, default="in_stock")
    availability_status = models.CharField(max_length=20, choices=AVAILABILITY_STATUS_CHOICES, default="active")
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="USD")
    incoterm = models.CharField(max_length=3, choices=INCOTERM_CHOICES, default="FOB")
    moq = models.PositiveIntegerField(default=1)
    production_lead_days = models.PositiveIntegerField(default=1)
    prep_to_ship_days = models.PositiveIntegerField(default=1)
    shipping_lead_days = models.PositiveIntegerField(default=1)
    gross_weight_kg = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal("0.100"))
    length_cm = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1.00"))
    width_cm = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1.00"))
    height_cm = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1.00"))
    country_of_origin = models.CharField(max_length=120, default="Unknown")
    manufacturer = models.CharField(max_length=200, blank=True,
        help_text="Завод-производитель (для OEM — OEM-завод, для AFTERMARKET — завод аналога, для REMAN — завод восстановления)")
    manufacturer_visible = models.BooleanField(default=True,
        help_text="Показывать завод клиенту. False — если бренд непубличный, хранится внутри")
    cross_numbers = models.CharField(max_length=500, blank=True)
    # Расширенные поля прайса поставщика (ТЗ шаблон):
    price_fob_sea = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Цена FOB морским путём")
    price_fob_air = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Цена FOB авиа")
    warehouse = models.ForeignKey("SellerWarehouse", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="parts", db_index=True,
        help_text="Виртуальный склад поставщика — папка по логистическому базису")
    warehouse_address = models.CharField(max_length=255, blank=True,
        help_text="Адрес склада отправления (денормализован из warehouse.address)")
    sea_port = models.CharField(max_length=120, blank=True,
        help_text="Морпорт отправления")
    air_port = models.CharField(max_length=120, blank=True,
        help_text="Аэропорт отправления")
    hs_code = models.CharField(max_length=20, blank=True, db_index=True,
        help_text="Код ТН ВЭД / HS Code — заполняется только брокером")
    hs_verified = models.BooleanField(default=False,
        help_text="True = код подтверждён таможенным брокером")
    backorder_allowed = models.BooleanField(default=False)
    mapping_status = models.CharField(max_length=20, choices=MAPPING_STATUS_CHOICES, default="auto")
    supplier_part_uid = models.CharField(max_length=80, blank=True)
    source_import_id = models.IntegerField(null=True, blank=True, db_index=True,
        help_text="Номер загрузки (PricelistImport.id), которая последней "
                  "записала эту позицию — для истории «Загрузка #N от завода X»")
    data_updated_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)
    admin_note = models.TextField(blank=True, help_text="Комментарий администратора (причина блокировки и т.д.)")
    moderated_at = models.DateTimeField(null=True, blank=True)
    moderated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="moderated_parts")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["seller", "-data_updated_at", "-id"], name="part_seller_updated_idx"),
            models.Index(fields=["seller", "availability_status"], name="part_seller_avail_idx"),
            models.Index(fields=["seller", "is_active"], name="part_seller_active_idx"),
            # Композитный для быстрого matching при импорте:
            # WHERE seller_id=? AND oem_number IN (...) — иначе O(N) скан.
            models.Index(fields=["seller", "oem_number"], name="part_seller_oem_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.oem_number})"

    def mandatory_missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.oem_number:
            missing.append("oem_number")
        if not self.title:
            missing.append("title")
        if self.price is None or self.price <= 0:
            missing.append("price")
        if not self.currency:
            missing.append("currency")
        if not self.incoterm:
            missing.append("incoterm")
        if self.moq <= 0:
            missing.append("moq")
        if self.production_lead_days < 0:
            missing.append("production_lead_days")
        if self.prep_to_ship_days < 0:
            missing.append("prep_to_ship_days")
        if self.shipping_lead_days < 0:
            missing.append("shipping_lead_days")
        if self.gross_weight_kg <= 0:
            missing.append("gross_weight_kg")
        if self.length_cm <= 0 or self.width_cm <= 0 or self.height_cm <= 0:
            missing.append("dimensions")
        if not self.country_of_origin:
            missing.append("country_of_origin")
        if self.availability == "in_stock" and self.stock_quantity <= 0:
            missing.append("stock_quantity")
        if self.availability == "backorder" and not self.backorder_allowed:
            missing.append("backorder_allowed")
        if self.availability_status in {"blocked", "discontinued"}:
            missing.append("availability_status")
        if self.mapping_status == "needs_review":
            missing.append("mapping_status")
        return missing

    @property
    def is_mandatory_complete(self) -> bool:
        return len(self.mandatory_missing_fields()) == 0

    @property
    def is_eligible_for_matching(self) -> bool:
        if not self.is_active:
            return False
        if self.availability_status not in {"active", "limited", "made_to_order"}:
            return False
        return self.is_mandatory_complete


class DrawingFolder(models.Model):
    """Папка для группировки чертежей владельца (напр. «Ходовка Komatsu»).

    Приватна, как и сами чертежи: видна только владельцу. Позволяет разложить
    чертежи по узлам/проектам, чтобы быстрее находить нужный."""
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="drawing_folders")
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="uniq_drawing_folder_owner_name"),
        ]

    def __str__(self) -> str:
        return self.name


class Drawing(models.Model):
    """Чертёж / CAD-файл, привязанный к детали поставщика."""

    FORMAT_CHOICES = [
        ("pdf", "PDF"),
        ("dwg", "DWG"),
        ("dxf", "DXF"),
        ("step", "STEP"),
        ("iges", "IGES"),
        ("stl", "STL"),
        ("png", "PNG"),
        ("jpg", "JPG"),
    ]
    STATUS_CHOICES = [
        ("draft", _("Черновик")),
        ("on_review", _("На проверке")),
        ("approved", _("Утверждён")),
        ("rejected", _("Отклонён")),
        ("archived", _("Архив")),
    ]

    # ТЗ §3: уровни доступа к чертежу
    ACCESS_CHOICES = [
        ("private",    _("Закрытый — только владелец")),
        ("for_sale",   _("Доступен для продажи (после предоплаты)")),
        ("rewardable", _("Доступен с вознаграждением автору")),
    ]

    title = models.CharField(max_length=255)
    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="drawings", null=True, blank=True)
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name="drawings")
    file_format = models.CharField(max_length=10, choices=FORMAT_CHOICES, default="pdf")
    file_url = models.URLField(blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    file_size_kb = models.PositiveIntegerField(default=0)
    revision = models.CharField(max_length=20, default="A")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    access_level = models.CharField(max_length=20, choices=ACCESS_CHOICES, default="private",
        help_text="ТЗ §3: закрытый/продажа/вознаграждение")
    reward_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0,
        help_text="Сумма вознаграждения автору при использовании в сделке (USD)")
    description = models.TextField(blank=True)
    oem_number = models.CharField(max_length=100, blank=True, db_index=True)
    folder = models.ForeignKey("DrawingFolder", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="drawings")
    # Чертёж может принадлежать проекту покупателя (грузится на странице проекта,
    # слот «Чертежи и спецификации») — тогда в «Мои чертежи» он показывается
    # отдельной виртуальной папкой с названием проекта.
    project = models.ForeignKey("assistant.Project", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="drawings")
    # Связь с документом проекта (мост): чертёж, загруженный на странице проекта,
    # ссылается на свой ProjectDocument — чтобы привязывать артикул из проекта.
    project_doc = models.ForeignKey("assistant.ProjectDocument", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="drawings")
    # Сторона: «need» — покупатель (что нужно), «offer» — продавец (что предлагают).
    # Оператор по артикулу сверяет need vs offer → точность поставки.
    SIDE_CHOICES = [("need", _("Нужно (покупатель)")), ("offer", _("Предлагают (продавец)"))]
    side = models.CharField(max_length=10, choices=SIDE_CHOICES, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"{self.title} (rev {self.revision})"


class DrawingAccessLog(models.Model):
    """ТЗ §12.1: журнал доступа к чертежам — для аудита и расчёта rewards."""
    ACTION_CHOICES = [
        ("view",      _("Просмотр")),
        ("download",  _("Скачивание")),
        ("denied",    _("Отказано (нет прав)")),
        ("watermark_added", _("Применён watermark")),
    ]

    drawing = models.ForeignKey(Drawing, on_delete=models.CASCADE, related_name="access_log")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="drawing_accesses")
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    order = models.ForeignKey("Order", on_delete=models.SET_NULL, null=True, blank=True,
                               help_text="Заказ-контекст (для access_level=for_sale)")
    ip = models.CharField(max_length=64, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["drawing", "-created_at"], name="dal_drw_created_idx"),
            models.Index(fields=["user", "-created_at"], name="dal_user_created_idx"),
        ]

    def __str__(self):
        return f"DAL[{self.drawing_id}/{self.action}/{self.user_id}]"


class RFQ(models.Model):
    MODE_CHOICES = [
        ("auto",       "AUTO"),
        ("semi",       "SEMI"),
        ("manual",     "MANUAL"),
        # Legacy alias — оставлен для совместимости со старыми RFQ. Новый код
        # должен использовать "manual".
        ("manual_oem", "MANUAL (legacy)"),
    ]
    URGENCY_CHOICES = [
        ("standard", "Standard"),
        ("urgent", "Urgent"),
        ("critical", "Critical"),
    ]
    STATUS_CHOICES = [
        ("new", "New"),
        ("quoted", "Quoted"),
        ("needs_review", "Needs Review"),
        ("cancelled", "Cancelled"),
    ]

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="rfqs")
    customer_name = models.CharField(max_length=180)
    customer_email = models.EmailField()
    company_name = models.CharField(max_length=255, blank=True)
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default="semi")
    urgency = models.CharField(max_length=20, choices=URGENCY_CHOICES, default="standard")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new")
    notes = models.TextField(blank=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="Общая скидка на весь RFQ (%)")
    discount_note = models.CharField(max_length=255, blank=True, help_text="Комментарий к скидке")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"RFQ #{self.id} - {self.customer_name}"

    @property
    def estimated_total(self):
        return sum((item.estimated_line_total for item in self.items.all()), 0)


class RFQItem(models.Model):
    STATE_CHOICES = [
        ("new", "NEW"),
        ("auto_matched", "AUTO MATCHED"),
        ("needs_review", "NEEDS REVIEW"),
        ("oem_manual", "OEM MANUAL"),
    ]

    rfq = models.ForeignKey(RFQ, on_delete=models.CASCADE, related_name="items")
    query = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(default=1)
    matched_part = models.ForeignKey(Part, on_delete=models.SET_NULL, null=True, blank=True, related_name="rfq_items")
    state = models.CharField(max_length=20, choices=STATE_CHOICES, default="new")
    confidence = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    decision_reason = models.TextField(blank=True)
    recommended_supplier_status = models.CharField(max_length=20, blank=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="Скидка на позицию (%)")
    discount_fixed = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Фиксированная скидка ($)")

    def __str__(self) -> str:
        return f"{self.query} x{self.quantity}"

    @property
    def estimated_line_total(self):
        if not self.matched_part:
            return 0
        return self.matched_part.price * self.quantity


class Quote(models.Model):
    """Котировка от продавца на RFQ. Поддерживает multi-round negotiation:
    каждая итерация (initial offer / buyer counter / seller respond) — это
    отдельный Quote, связанный через parent_quote с предыдущим раундом.
    """
    STATUS_CHOICES = [
        ("draft", _("Черновик")),
        ("submitted", _("Отправлена")),
        ("countered", _("Контр-оффер от покупателя")),
        ("finalized", _("Финальная (без переторжки)")),
        ("accepted", _("Принята")),
        ("declined", _("Отклонена")),
        ("expired", _("Истекла")),
    ]
    DIRECTION_CHOICES = [
        ("seller_to_buyer", _("От продавца")),
        ("buyer_to_seller", _("От покупателя (counter)")),
    ]

    rfq = models.ForeignKey(RFQ, on_delete=models.CASCADE, related_name="quotes")
    seller = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="quotes_offered")
    direction = models.CharField(max_length=20, choices=DIRECTION_CHOICES, default="seller_to_buyer")
    parent_quote = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="children")
    round_number = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="submitted", db_index=True)
    is_final = models.BooleanField(default=False, help_text="Продавец зафиксировал — переторжка невозможна")
    delivery_days = models.PositiveIntegerField(default=14)
    valid_until = models.DateTimeField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=10, default="USD")
    message = models.TextField(blank=True, help_text="Комментарий к раунду")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["rfq", "-round_number", "-created_at"]
        indexes = [
            models.Index(fields=["rfq", "seller", "-round_number"], name="quote_rfq_seller_idx"),
            models.Index(fields=["status", "-created_at"], name="quote_status_idx"),
        ]

    def __str__(self) -> str:
        return f"Quote #{self.id} (RFQ {self.rfq_id} round {self.round_number})"


class QuoteItem(models.Model):
    """Позиция котировки — цена за единицу + количество, привязанные к RFQItem."""
    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="items")
    rfq_item = models.ForeignKey(RFQItem, on_delete=models.SET_NULL, null=True, blank=True)
    part = models.ForeignKey(Part, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="quote_items")
    title_snapshot = models.CharField(max_length=300, blank=True,
                                       help_text="Снимок названия на момент котировки")
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.CharField(max_length=400, blank=True)
    # Per-позиционные характеристики (продавец задаёт по каждой позиции).
    delivery_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Срок поставки этой позиции (дни). Пусто → берём общий Quote.delivery_days")
    CONDITION_CHOICES = [("oem", "OEM"), ("analog", "Аналог")]
    condition = models.CharField(
        max_length=20, choices=CONDITION_CHOICES, default="oem",
        help_text="Тип позиции: оригинал (OEM) или аналог")

    @property
    def line_total(self):
        return self.unit_price * self.quantity

    @property
    def eff_delivery_days(self):
        """Эффективный срок позиции: свой, иначе общий по котировке."""
        return self.delivery_days if self.delivery_days is not None else self.quote.delivery_days


class Order(models.Model):
    STATUS_CHOICES = [
        ("pending", _("Ожидание оплаты")),
        ("reserve_paid", _("Резерв оплачен")),
        ("confirmed", _("Формирование заказа")),
        ("in_production", _("В производстве")),
        ("ready_to_ship", _("Готов к отгрузке")),
        ("transit_abroad", _("Транзит (Зарубеж)")),
        ("customs", _("Таможня")),
        ("transit_rf", _("Транзит (РФ)")),
        ("issuing", _("Выдача")),
        ("shipped", _("Отгружен")),
        ("delivered", _("Доставлен")),
        ("completed", _("Завершён")),
        ("cancelled", _("Отменён")),
    ]
    PAYMENT_STATUS_CHOICES = [
        ("awaiting_reserve", _("Ожидает резерва")),
        ("reserve_paid", _("Резерв оплачен")),
        ("mid_paid", _("Подтверждение оплачено")),
        ("customs_paid", _("Таможня оплачена")),
        ("paid", _("Оплачен")),
        ("refund_pending", _("Возврат в обработке")),
        ("refunded", _("Возвращён")),
    ]
    PAYMENT_SCHEME_CHOICES = [
        ("simple", "10% + 90%"),
        ("staged", "10% + 50% + 40%"),
    ]
    SLA_STATUS_CHOICES = [
        ("on_track", _("В норме")),
        ("at_risk", _("Под угрозой")),
        ("breached", _("Нарушен")),
    ]

    customer_name = models.CharField(max_length=180)
    customer_email = models.EmailField()
    customer_phone = models.CharField(max_length=50)
    delivery_address = models.TextField()
    buyer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    # CRM продавца: точная привязка заказа к заказчику (по ИНН покупателя или
    # ручным подтверждением). Позволяет продавцу контролировать отгрузки по
    # конкретному контрагенту, а не по совпадению названия.
    customer_ref = models.ForeignKey(
        "marketplace.Customer", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="orders", db_index=True,
        help_text="Заказчик из CRM продавца, к которому относится этот заказ",
    )
    # KAM (Key Account Manager) — владелец АККАУНТА по сделке (коммерция).
    # Отделён от assigned_operator (исполнение): KAM видит «свои» сделки,
    # оператор — «свои». Авто-проставляется из customer_ref.owner при привязке.
    assigned_kam = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="kam_orders", db_index=True,
        help_text="KAM — владелец аккаунта по этой сделке",
    )
    # Хэндофф KAM ↔ Оператор (единственная точка касания, без конфликта):
    #   kam        — у KAM (коммерция, до передачи)
    #   operator   — передано оператору на исполнение (KAM read-only)
    #   escalation — оператор вернул KAM (исключение: SLA/брак/перерасход)
    KAM_HANDOFF_CHOICES = [
        ("kam", "У KAM"),
        ("operator", "У оператора (исполнение)"),
        ("escalation", "Эскалация к KAM"),
    ]
    kam_handoff = models.CharField(max_length=12, choices=KAM_HANDOFF_CHOICES,
                                    default="kam", db_index=True)
    kam_handoff_note = models.CharField(max_length=300, blank=True)
    kam_handoff_at = models.DateTimeField(null=True, blank=True)
    # Оператор, ведущий сделку — получает бонус 0.4-0.7% после release.
    # Назначается при первом операторском действии (confirm/dispatch/quote-approve).
    assigned_operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_orders",
        help_text="Оператор, ведущий сделку (получает бонус по закрытию)",
    )
    # PIVOT 2026-05-27: sub-order split.
    # Один RFQ может разбиться на N sub-orders по числу операторов, чьи
    # поставщики попали в заказ. parent_order — оригинальный «общий» заказ
    # видимый покупателю; sub-orders видны только своим операторам.
    parent_order = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sub_orders",
        help_text="Оригинальный заказ если это sub-order (видим покупателю)",
    )
    is_sub_order = models.BooleanField(
        default=False,
        help_text="True если этот Order — часть разбитого по операторам заказа",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    supplier_confirm_deadline = models.DateTimeField(null=True, blank=True)
    ship_deadline = models.DateTimeField(null=True, blank=True)
    sla_status = models.CharField(max_length=20, choices=SLA_STATUS_CHOICES, default="on_track")
    sla_breaches_count = models.PositiveIntegerField(default=0)
    logistics_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    logistics_currency = models.CharField(max_length=10, default="USD")
    logistics_provider = models.CharField(max_length=60, default="internal_fallback")
    logistics_meta = models.JSONField(default=dict, blank=True)
    # ── Реальный перевозчик/логист (виден в track_order) ──────
    # Заполняется оператором или приходит из API перевозчика.
    # Чтобы юзер не писал в LLM «как отследить» — показываем прямо в карточке.
    carrier_name = models.CharField(max_length=120, blank=True,
        help_text="DHL / UPS / СДЭК / Деловые Линии / внутренний курьер")
    carrier_phone = models.CharField(max_length=40, blank=True,
        help_text="Прямой телефон перевозчика (для buyer)")
    carrier_email = models.EmailField(blank=True,
        help_text="Email перевозчика (для уточнений)")
    tracking_number = models.CharField(max_length=80, blank=True,
        help_text="Номер для трекинга на сайте перевозчика")
    tracking_url = models.URLField(blank=True,
        help_text="Прямая ссылка на статус (deep-link). Например dhl.com/track/...")
    shipping_mode = models.CharField(max_length=4, choices=[
        ("sea",  "Морем"),
        ("air",  "Авиа"),
        ("auto", "Авто"),
    ], default="sea", help_text="Способ доставки (выбирает покупатель)")
    incoterm = models.CharField(max_length=3, choices=[
        ("FOB", "FOB — продавец до порта отгрузки"),
        ("CIP", "CIP — фрахт+страховка до места назначения"),
        ("DDP", "DDP — до двери получателя, all-in"),
    ], default="FOB", help_text="Базис поставки Incoterms 2020")
    invoice_number = models.CharField(max_length=80, blank=True)
    payment_status = models.CharField(max_length=30, choices=PAYMENT_STATUS_CHOICES, default="awaiting_reserve")
    payment_scheme = models.CharField(max_length=20, choices=PAYMENT_SCHEME_CHOICES, default="simple")
    reserve_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("10.00"))
    reserve_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reserve_paid_at = models.DateTimeField(null=True, blank=True)
    mid_payment_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    mid_paid_at = models.DateTimeField(null=True, blank=True)
    customs_payment_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    customs_paid_at = models.DateTimeField(null=True, blank=True)
    final_paid_at = models.DateTimeField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["buyer", "status"], name="order_buyer_status_idx"),
            models.Index(fields=["status", "sla_status"], name="order_status_sla_idx"),
            models.Index(fields=["buyer", "-created_at"], name="order_buyer_created_idx"),
        ]

    def __str__(self) -> str:
        return f"Order #{self.id} - {self.customer_name}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    part = models.ForeignKey(Part, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    # Каждый поставщик двигает свои позиции независимо от других.
    # Берём те же choices что и у Order.status — это не sub-status, а
    # реальный статус именно ЭТОЙ позиции у её поставщика.
    status = models.CharField(max_length=30, blank=True, default="",
        help_text="Per-item статус. Пусто = наследует Order.status.")
    status_changed_at = models.DateTimeField(null=True, blank=True)

    @property
    def total_price(self):
        return self.quantity * self.unit_price

    @property
    def effective_status(self):
        """Статус позиции: per-item если задан, иначе общий статус заказа."""
        return self.status or self.order.status


class Shipment(models.Model):
    """Физическая партия отгрузки. Один Order может породить 1 (консолидация)
    или N (split) партий. Каждая партия = свой коносамент, своя таможня,
    свой ETA, свой статус.
    """
    KIND_CHOICES = [
        ("consolidated", "Консолидированная"),
        ("split", "Split (раздельная)"),
    ]
    STATUS_CHOICES = [
        ("formed",          "Сформирована"),
        ("ready_to_ship",   "Готова к отгрузке"),
        ("transit_abroad",  "Транзит (зарубеж)"),
        ("customs",         "Таможня"),
        ("transit_rf",      "Транзит (РФ)"),
        ("issuing",         "Выдача"),
        ("delivered",       "Доставлена"),
    ]
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="shipments")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="consolidated")
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="formed")
    tracking_number = models.CharField(max_length=120, blank=True)
    carrier = models.CharField(max_length=120, blank=True)
    shipping_mode = models.CharField(max_length=20, blank=True)  # sea/air/auto
    cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    eta_delivery = models.DateField(null=True, blank=True)
    items = models.ManyToManyField(OrderItem, related_name="shipments", blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Shipment #{self.id} ORD-{self.order_id} · {self.get_status_display()}"

    @property
    def total_amount(self):
        return sum((it.unit_price * it.quantity for it in self.items.all()), 0)


class OrderEvent(models.Model):
    EVENT_CHOICES = [
        ("order_created", "Order Created"),
        ("status_changed", "Status Changed"),
        ("sla_status_changed", "SLA Status Changed"),
        ("invoice_opened", "Invoice Opened"),
        ("reserve_paid", "Reserve Paid"),
        ("mid_payment_paid", "Mid Payment Paid"),
        ("customs_payment_paid", "Customs Payment Paid"),
        ("final_payment_paid", "Final Payment Paid"),
        ("quality_confirmed", "Quality Confirmed"),
        ("document_uploaded", "Document Uploaded"),
        ("claim_opened", "Claim Opened"),
        ("claim_status_changed", "Claim Status Changed"),
    ]
    SOURCE_CHOICES = [
        ("system", "System"),
        ("buyer", "Buyer"),
        ("seller", "Seller"),
        ("operator", "Operator"),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=40, choices=EVENT_CHOICES)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="system")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="order_events")
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "event_type"], name="event_order_type_idx"),
        ]

    def __str__(self) -> str:
        return f"Order #{self.order_id} {self.event_type}"


class ActivityEvent(models.Model):
    """Лента важных событий платформы для админа (контроль/безопасность).

    Не дублирует разделы кабинета — это сквозной аудит-поток: кто (actor +
    кабинет/роль), откуда (ip), что (kind + meta: позиции/сумма/id), когда.
    Пишется в момент создания сделки/RFQ/загрузки прайса (IP берётся из запроса
    через ActionView → params['_client_ip'], либо напрямую во вьюхе загрузки).
    """
    KIND_CHOICES = [
        ("order", "Заказ"),
        ("rfq", "RFQ"),
        ("pricelist", "Загрузка прайса"),
    ]
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, db_index=True)
    actor = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="activity_events")
    actor_role = models.CharField(max_length=20, blank=True, default="")
    ip = models.CharField(max_length=64, blank=True, default="")
    title = models.CharField(max_length=255, blank=True, default="")
    # meta: {n_items, amount, currency, order_id, rfq_id, import_id, items:[...]}
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["kind", "-created_at"], name="actev_kind_created_idx"),
            models.Index(fields=["actor", "-created_at"], name="actev_actor_created_idx"),
        ]

    def __str__(self) -> str:
        return f"ActivityEvent {self.kind} by {self.actor_id} @ {self.created_at:%Y-%m-%d %H:%M}"


class OrderDocument(models.Model):
    DOC_TYPE_CHOICES = [
        ("invoice", "Invoice"),
        ("packing_list", "Packing List"),
        ("certificate", "Certificate"),
        ("quality_report", "Quality Report"),
        ("customs", "Customs"),
        ("other", "Other"),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=30, choices=DOC_TYPE_CHOICES, default="other")
    title = models.CharField(max_length=255)
    file_url = models.URLField(blank=True)
    file_obj = models.FileField(upload_to="order_documents/%Y/%m/%d", blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="uploaded_order_documents")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Order #{self.order_id} {self.title}"


class DocumentSignature(models.Model):
    """Подпись участника сделки на документе заказа (Этап 1: ПЭП + загрузка).

    ПЭП (ст. 6 ФЗ-63) — простая электронная подпись: фиксируем кто/когда/IP +
    SHA-256 документа на момент подписи (tamper-evident). Либо участник
    загружает подписанный/с печатью скан (method=upload). Юр. значимость для
    B2B обеспечивается офертой платформы (стороны принимают ПЭП).
    """
    METHOD_CHOICES = [
        ("ep", "ПЭП (в платформе)"),
        ("upload", "Загружен подписанный"),
    ]
    document = models.ForeignKey(OrderDocument, on_delete=models.CASCADE,
                                 related_name="signatures")
    signer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="document_signatures")
    signer_role = models.CharField(max_length=20, blank=True)   # buyer/seller/operator
    signer_name = models.CharField(max_length=200, blank=True)  # снимок имени
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default="ep")
    doc_sha256 = models.CharField(max_length=64, blank=True,
                                  help_text="SHA-256 документа на момент подписи")
    ip = models.CharField(max_length=64, blank=True)
    signed_file = models.FileField(upload_to="signed_documents/%Y/%m/%d", blank=True,
                                   help_text="Загруженный подписанный скан (method=upload)")
    note = models.CharField(max_length=300, blank=True)
    signed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["signed_at"]

    def __str__(self) -> str:
        return f"sig doc#{self.document_id} by {self.signer_id} ({self.method})"


class OrderClaim(models.Model):
    """ТЗ §5.4: рекламация по заказу с полным flow 6 статусов.

    open → in_review → approved/rejected
                          ↓
              corrective_actions OR financial_settlement → closed
                          rejected → closed
    """
    STATUS_CHOICES = [
        ("open",                  _("Открыта")),
        ("in_review",             _("На рассмотрении")),
        ("approved",              _("Подтверждена")),
        ("rejected",              _("Отклонена")),
        ("corrective_actions",    _("Корректирующие действия")),
        ("financial_settlement",  _("Финансовое урегулирование")),
        ("closed",                _("Закрыта")),
    ]
    KIND_CHOICES = [
        ("defect",       _("Брак")),
        ("wrong_part",   _("Не та деталь")),
        ("missing",      _("Не пришла")),
        ("damage",       _("Повреждение при доставке")),
        ("late",         _("Просрочка поставки")),
        ("other",        _("Другое")),
    ]
    RESOLUTION_CHOICES = [
        ("none",          _("Нет")),
        ("repair",        _("Замена/ремонт")),
        ("reproduce",     _("Повторно произвести")),
        ("partial_refund", _("Частичный возврат")),
        ("full_refund",   _("Полный возврат")),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="claims")
    kind = models.CharField(max_length=30, choices=KIND_CHOICES, default="other")
    title = models.CharField(max_length=255)
    description = models.TextField()
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="open", db_index=True)
    resolution_kind = models.CharField(max_length=30, choices=RESOLUTION_CHOICES, default="none")
    refund_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0,
        help_text="Сумма возврата если resolution_kind=*_refund")
    rejection_reason = models.TextField(blank=True)
    opened_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="opened_claims")
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_claims")
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="resolved_claims")
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Эскалация супервайзеру: когда claim открыт > N дней без resolution
    # (см. management command `escalate_stale_claims`).
    escalated_at = models.DateTimeField(null=True, blank=True, db_index=True,
        help_text="Когда был отправлен алерт супервайзеру (защита от повторов)")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="claim_status_idx"),
        ]

    def __str__(self) -> str:
        return f"Claim #{self.id} for Order #{self.order_id} [{self.status}]"


class WebhookDeliveryLog(models.Model):
    order_event = models.ForeignKey(OrderEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="webhook_logs")
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="webhook_logs")
    endpoint = models.URLField()
    success = models.BooleanField(default=False)
    attempt = models.PositiveIntegerField(default=1)
    status_code = models.IntegerField(null=True, blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_body = models.TextField(blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Webhook {self.endpoint} order#{self.order_id} success={self.success} attempt={self.attempt}"


class UserProfile(models.Model):
    ROLE_CHOICES = [
        ("buyer", "Buyer"),
        ("seller", "Seller"),
        ("operator", "Operator"),
    ]
    OPERATOR_ROLE_CHOICES = [
        ("", "—"),
        ("manager", "KAM (менеджер по работе с клиентами)"),
        ("logist", "Логист"),
        ("customs", "Таможенный"),
        ("payment", "Финансовый"),
    ]
    SUPPLIER_STATUS_CHOICES = [
        ("trusted", _("Надёжный")),
        ("sandbox", _("Песочница")),
        ("risky", _("Рисковый")),
        ("rejected", _("Исключён")),
    ]

    DEPARTMENT_CHOICES = [
        ("director", "Director"),
        ("sales", "Sales"),
        ("logistics", "Logistics"),
        ("finance", "Finance"),
        ("engineering", "Engineering"),
        ("viewer", "Viewer"),
    ]

    LANGUAGE_CHOICES = [
        ("ru", "Русский"),
        ("en", "English"),
        ("zh-hans", "中文"),
        ("es", "Español"),
        ("ar", "العربية"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    # Оператор ВЭД, ведущий этого поставщика (PIVOT 2026-05-27).
    # 1 поставщик = 1 оператор. Закрепляется при KYB-approve = тот кто одобрил.
    # Только для role=seller; для buyer не используется.
    assigned_operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="managed_suppliers",
        help_text="Оператор ВЭД, ведущий этого поставщика (1:1)",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="buyer")
    # Суброль оператора (для role=operator). Реальный механизм вместо demo-
    # эвристики по username: detect_user_role → operator_<operator_role>.
    # KAM = operator_role="manager".
    operator_role = models.CharField(max_length=20, choices=OPERATOR_ROLE_CHOICES,
                                     blank=True, default="")
    # AI-кредиты: лимит бесплатных AI-запросов. Гейтим только ПОКУПАТЕЛЕЙ
    # (операторы/продавцы — без лимита). Пополняется при оплате заказа +
    # мягкий ежемесячный долив. Логика — в assistant/ai_credits.py.
    ai_credits = models.IntegerField(
        default=25,
        help_text="Остаток бесплатных AI-запросов (для покупателей). "
                  "Пополняется при оплате заказа.")
    ai_credits_refilled_at = models.DateTimeField(null=True, blank=True)
    company_name = models.CharField(max_length=255, blank=True)
    language = models.CharField(
        max_length=10,
        choices=LANGUAGE_CHOICES,
        default="ru",
        help_text="Язык интерфейса. Меняется при регистрации или в настройках ЛК.",
    )
    department = models.CharField(max_length=20, choices=DEPARTMENT_CHOICES, default="director")
    allowed_regions = models.CharField(max_length=255, blank=True, help_text="CSV list: europe,china,components,...")
    allowed_brands = models.ManyToManyField(Brand, blank=True, related_name="allowed_profiles")
    can_manage_assortment = models.BooleanField(default=True)
    can_manage_pricing = models.BooleanField(default=True)
    can_manage_orders = models.BooleanField(default=True)
    can_manage_drawings = models.BooleanField(default=False)
    can_view_analytics = models.BooleanField(default=True)
    can_manage_team = models.BooleanField(default=False)
    external_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("60.00"))
    behavioral_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("60.00"))
    rating_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("60.00"), editable=False)
    supplier_status = models.CharField(
        max_length=20,
        choices=SUPPLIER_STATUS_CHOICES,
        default="sandbox",
        editable=False,
    )
    bankruptcy_flag = models.BooleanField(default=False)
    liquidation_flag = models.BooleanField(default=False)
    last_rating_recalculated_at = models.DateTimeField(null=True, blank=True, editable=False)
    admin_note = models.TextField(blank=True, help_text="Заметка администратора о поставщике")

    # ── Buyer registration fields (ТЗ §1: 8 полей) ────────────
    # Подтягиваются при self-регистрации через chat-форму.
    # ИНН/Tax ID → автоматически резолвится в название компании через
    # check_ru_aggregator() / check_opencorporates() и пишется в company_name.
    country = models.CharField(max_length=2, blank=True,
        help_text="ISO-3166-1 alpha-2: RU, KZ, BY, AE, ...")
    tax_id = models.CharField(max_length=32, blank=True, db_index=True,
        help_text="ИНН / Tax ID / Company Reg. No.")
    contact_name = models.CharField(max_length=200, blank=True,
        help_text="ФИО контактного лица (кто работает с платформой)")
    position = models.CharField(max_length=80, blank=True,
        help_text="Должность: закупщик / директор / владелец / иное")
    phone_e164 = models.CharField(max_length=20, blank=True,
        help_text="Телефон в международном формате (+7 ...)")
    messenger_kind = models.CharField(max_length=16, blank=True,
        choices=[("whatsapp", "WhatsApp"), ("telegram", "Telegram")],
        help_text="Какой мессенджер: WhatsApp или Telegram")
    messenger_handle = models.CharField(max_length=80, blank=True,
        help_text="Username Telegram (@user) или номер WhatsApp (+7...)")
    equipment_fleet = models.TextField(blank=True,
        help_text="Парк техники: бренды и модели (любым текстом)")

    # ── Durable notification channels ─────────────────────────
    # WS push в чат-сессии работает только когда вкладка открыта. Эти каналы
    # доставляют важные события когда пользователь оффлайн.
    notif_email_enabled = models.BooleanField(default=True)
    notif_telegram_chat_id = models.CharField(max_length=64, blank=True,
        help_text="Telegram chat_id (получают через бот командой /start)")
    notif_telegram_enabled = models.BooleanField(default=False)
    # Через запятую: order,payment,rfq,sla,claim,system,info
    notif_kinds = models.CharField(max_length=200,
        default="order,payment,rfq,sla,claim,system",
        help_text="Какие kinds доставлять через email/telegram (CSV)")

    @staticmethod
    def _clamp_score(value: Decimal) -> Decimal:
        if value < 0:
            return Decimal("0.00")
        if value > 100:
            return Decimal("100.00")
        return value.quantize(Decimal("0.01"))

    def recalculate_supplier_rating(self):
        if self.role != "seller":
            self.rating_score = Decimal("0.00")
            self.supplier_status = "sandbox"
            self.last_rating_recalculated_at = timezone.now()
            return

        if self.bankruptcy_flag or self.liquidation_flag:
            self.rating_score = Decimal("0.00")
            self.supplier_status = "rejected"
            self.last_rating_recalculated_at = timezone.now()
            return

        external = self._clamp_score(Decimal(self.external_score))
        behavioral = self._clamp_score(Decimal(self.behavioral_score))
        score = (external * Decimal("0.6")) + (behavioral * Decimal("0.4"))
        score = self._clamp_score(score)
        self.rating_score = score

        if score >= 80:
            self.supplier_status = "trusted"
        elif score >= 60:
            self.supplier_status = "sandbox"
        elif score >= 0:
            self.supplier_status = "risky"
        self.last_rating_recalculated_at = timezone.now()

    def save(self, *args, **kwargs):
        self.recalculate_supplier_rating()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.user.username} ({self.role})"


class UserRole(models.Model):
    ROLE_CHOICES = UserProfile.ROLE_CHOICES
    OPERATOR_ROLE_CHOICES = UserProfile.OPERATOR_ROLE_CHOICES

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="roles")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    operator_role = models.CharField(
        max_length=20,
        choices=OPERATOR_ROLE_CHOICES,
        blank=True,
        default="",
    )
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role", "operator_role"],
                name="uniq_user_role_operator_role",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "role", "is_enabled"]),
        ]

    def __str__(self) -> str:
        suffix = f"_{self.operator_role}" if self.role == "operator" and self.operator_role else ""
        return f"{self.user.username}: {self.role}{suffix}"


class SupplierRatingEvent(models.Model):
    EVENT_CHOICES = [
        ("rfq_response", "RFQ Response"),
        ("rfq_response_late", "RFQ Response Late"),
        ("terms_worsened", "Terms Worsened"),
        ("data_mismatch", "Data Mismatch"),
        ("delivery_delay", "Delivery Delay"),
        ("delivery_on_time", "Delivery On Time"),
        ("order_cancellation", "Order Cancellation"),
        ("return", "Return"),
        ("sandbox_selected", "Sandbox Selected"),
        ("risky_selected", "Risky Selected"),
        ("manual_oem_escalation", "Manual OEM Escalation"),
        ("claim_confirmed", "Claim Confirmed"),
        ("buyer_review", "Buyer Review"),
    ]

    supplier = models.ForeignKey(User, on_delete=models.CASCADE, related_name="supplier_rating_events")
    event_type = models.CharField(max_length=40, choices=EVENT_CHOICES)
    impact_score = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.supplier_id}:{self.event_type}:{self.impact_score}"


class SellerImportRun(models.Model):
    MODE_CHOICES = [
        ("preview", "Preview"),
        ("apply", "Apply"),
    ]
    STATUS_CHOICES = [
        ("success", "Success"),
        ("failed", "Failed"),
    ]

    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name="import_runs")
    filename = models.CharField(max_length=255, blank=True, default="")
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default="apply")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="success")
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_no_price_count = models.PositiveIntegerField(default=0)
    skipped_invalid_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.seller_id}:{self.filename}:{self.status}:{self.created_at.isoformat()}"


# ── Notifications & Team & KYB ──────────────────────────────
class Notification(models.Model):
    KIND_CHOICES = [
        ("info", _("Информация")),
        ("order", _("Заказ")),
        ("rfq", _("RFQ")),
        ("payment", _("Оплата")),
        ("sla", _("SLA")),
        ("claim", _("Рекламация")),
        ("system", _("Система")),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="info")
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    url = models.CharField(max_length=400, blank=True, help_text="Optional click target")
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "is_read", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.title}"


class TeamMember(models.Model):
    """Sub-users belonging to a company (the owner is the User with role buyer/seller)."""
    ROLE_CHOICES = [
        ("admin", _("Администратор")),
        ("manager", _("Менеджер")),
        ("ved", _("Менеджер ВЭД")),
        ("logist", _("Логист")),
        ("finance", _("Финансист")),
        ("viewer", _("Только просмотр")),
    ]
    STATUS_CHOICES = [
        ("active", _("Активен")),
        ("invited", _("Приглашён")),
        ("disabled", _("Отключён")),
    ]
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="team_members",
                               help_text="Company account owner")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="team_membership",
                             null=True, blank=True, help_text="Set when invitation accepted")
    invited_email = models.EmailField()
    full_name = models.CharField(max_length=180, blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="viewer")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="invited")
    invite_token = models.CharField(max_length=100, blank=True)
    invited_at = models.DateTimeField(default=timezone.now)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("owner", "invited_email")]
        ordering = ["-invited_at"]

    def __str__(self) -> str:
        return f"{self.owner_id} → {self.invited_email} ({self.role})"


class Customer(models.Model):
    """Заказчик продавца — контрагент, которого продавец заводит по ИНН в своём
    кабинете. По заказчику создаются проекты и контролируются отгрузки.
    1 заказчик уникален в рамках одного продавца (owner+inn)."""
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="customers",
        help_text="Продавец, который ведёт этого заказчика")
    inn = models.CharField(max_length=20, db_index=True, verbose_name="ИНН")
    kpp = models.CharField(max_length=20, blank=True, verbose_name="КПП")
    name = models.CharField(max_length=255)
    country = models.CharField(max_length=2, default="RU")
    legal_address = models.CharField(max_length=500, blank=True)
    contact_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    note = models.TextField(blank=True)
    # Инвайт заказчика на платформу (продавец/менеджер генерит ссылку).
    invite_token = models.CharField(max_length=100, blank=True, db_index=True)
    invited_at = models.DateTimeField(null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="customer_records",
                             help_text="Аккаунт заказчика после принятия инвайта")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "inn"], name="uniq_customer_owner_inn"),
        ]
        indexes = [models.Index(fields=["owner", "is_active", "name"])]

    def __str__(self) -> str:
        return f"{self.name} (ИНН {self.inn})"


class CompanyVerification(models.Model):
    """KYB (Know Your Business) verification: collected docs and status."""
    STATUS_CHOICES = [
        ("none", _("Не подавалась")),
        ("pending", _("На проверке")),
        ("verified", _("Верифицирована")),
        ("rejected", _("Отклонена")),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="kyb")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="none", db_index=True)
    legal_name = models.CharField(max_length=300, blank=True)
    inn = models.CharField(max_length=20, blank=True, verbose_name="ИНН")
    kpp = models.CharField(max_length=20, blank=True, verbose_name="КПП")
    ogrn = models.CharField(max_length=20, blank=True, verbose_name="ОГРН")
    legal_address = models.TextField(blank=True)
    bank_name = models.CharField(max_length=200, blank=True)
    bank_account = models.CharField(max_length=30, blank=True)
    bik = models.CharField(max_length=20, blank=True, verbose_name="БИК")
    director_name = models.CharField(max_length=200, blank=True)
    doc_charter = models.FileField(upload_to="kyb/charter/", null=True, blank=True,
                                    help_text="Устав")
    doc_egrul = models.FileField(upload_to="kyb/egrul/", null=True, blank=True,
                                  help_text="Выписка ЕГРЮЛ/ЕГРИП")
    doc_passport = models.FileField(upload_to="kyb/passport/", null=True, blank=True,
                                     help_text="Паспорт директора (1 разворот)")
    # ── ТЗ «Онбординг и проверка поставщика» §2 ───────────────────────
    # Поля, которые поставщик заполняет в форме онбординга. Дополняют
    # существующие реквизиты (legal_name/inn/kpp/ogrn) — для зарубежных
    # компаний legal_name + inn используются как Company Number + Tax ID.
    country = models.CharField(max_length=4, blank=True,
        help_text="ISO-2 страны регистрации: RU/CN/AE/DE/...")
    vat_number = models.CharField(max_length=40, blank=True,
        help_text="VAT / Tax ID (если применимо)")
    warehouse_address = models.TextField(blank=True,
        help_text="Адрес склада, откуда реально отгружает")
    website = models.URLField(max_length=300, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    whatsapp = models.CharField(max_length=80, blank=True)
    telegram = models.CharField(max_length=80, blank=True)
    contact_email = models.EmailField(blank=True)
    categories = models.CharField(max_length=400, blank=True,
        help_text="Бренды и типы запчастей (CSV или текст)")
    doc_dealership = models.FileField(upload_to="kyb/dealership/", null=True, blank=True,
        help_text="Сертификаты дилерства (если заявляет «официальный дилер»)")
    doc_bank = models.FileField(upload_to="kyb/bank/", null=True, blank=True,
        help_text="Реквизиты банковского счёта (PDF/PNG)")
    # ── Авто-API результаты (§3 ТЗ) ─────────────────────────────────
    # JSON со снэпшотами всех API ответов: aggregator/opencorporates/vies/
    # opensanctions/maps/site/messenger. Каждое решение оператора может
    # сослаться на конкретный источник + дату получения данных (§11 аудит).
    api_results = models.JSONField(default=dict, blank=True,
        help_text="Снэпшоты API: {aggregator, opencorporates, vies, sanctions, maps, site, messenger}")
    # Risk-индикатор по итогам автопроверок: red/yellow/green/unknown.
    # red → автоотказ (§5 ТЗ); yellow/green → попадает на ручную проверку.
    risk_indicator = models.CharField(max_length=10, default="unknown",
        choices=[("green", "Зелёный"), ("yellow", "Жёлтый"), ("red", "Красный"), ("unknown", "Не определён")])
    auto_decision = models.CharField(max_length=20, default="", blank=True,
        help_text="auto_reject / sandbox_candidate / manual_review")
    auto_checked_at = models.DateTimeField(null=True, blank=True)
    # Чеклист оператора (§4 ТЗ): {streetview_ok, reviews_ok, site_ok, bank_ok, certs_ok, messenger_test_ok}
    operator_checklist = models.JSONField(default=dict, blank=True)
    operator_note = models.TextField(blank=True,
        help_text="Свободный комментарий оператора при решении")
    # ── ТЗ §8 Постоянный мониторинг ─────────────────────────────────
    last_monitored_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="reviewed_kyb")

    def __str__(self) -> str:
        return f"KYB[{self.user_id}]={self.status}"


class CompetitorOffer(models.Model):
    """ТЗ §5.2: загрузка конкурентного предложения от buyer'а для триггера
    переторжки. Seller или operator может посмотреть и применить ручную
    скидку с комментарием.
    """
    STATUS_CHOICES = [
        ("uploaded",   _("Загружено")),
        ("under_review", _("Рассматривается")),
        ("matched",    _("Скидка применена")),
        ("declined",   _("Отклонено (наша цена ниже)")),
    ]
    rfq = models.ForeignKey(RFQ, on_delete=models.CASCADE, related_name="competitor_offers",
                              null=True, blank=True)
    quote = models.ForeignKey("Quote", on_delete=models.CASCADE,
                                related_name="competitor_offers", null=True, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="competitor_offers_uploaded")
    competitor_name = models.CharField(max_length=200, blank=True)
    quoted_price = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=10, default="USD")
    delivery_days = models.PositiveIntegerField(default=14)
    file_url = models.URLField(blank=True, help_text="Скан/PDF предложения")
    note = models.TextField(blank=True, help_text="Комментарий buyer'а")
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="uploaded")
    seller_response_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0,
        help_text="Скидка от seller'а в ответ (% от quote.total_amount)")
    seller_comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["rfq", "-created_at"], name="comp_rfq_created_idx"),
            models.Index(fields=["quote", "-created_at"], name="comp_quote_created_idx"),
        ]

    def __str__(self):
        return f"CompOffer[{self.id}] {self.competitor_name}: ${self.quoted_price}"


class PlatformRevenueLine(models.Model):
    """ТЗ §15: декомпозиция дохода группы по компонентам.

    На каждый paid+delivered заказ генерируются строки:
      • basis_fee     IT-Платформа за SLA, проверку, выдачу
                      6% FOB / 8% CIF / 12% DDP (ТЗ §15.1)
      • logistics_margin  3-7% по правилам портов (ТЗ §16)
      • success_fee   5% от завода — удерживается из эскроу
      • rf_agent      2% (если оплата RUB)
      • customs_fee   $300 (если we оформляем таможню)
    """
    KIND_CHOICES = [
        ("basis_fee",        "IT-Платформа FOB/CIF/DDP"),
        ("logistics_margin", "Логистическая маржа"),
        ("success_fee",      "Success fee 5%"),
        ("rf_agent",         "РФ-агент 2%"),
        ("customs_fee",      "Customs $300"),
        ("volume_discount",  "Скидка по обороту (минус)"),
    ]
    BASIS_CHOICES = [("FOB", "FOB"), ("CIF", "CIF"), ("DDP", "DDP"),
                      ("EXW", "EXW"), ("CIP", "CIP")]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="revenue_lines")
    kind = models.CharField(max_length=30, choices=KIND_CHOICES)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=10, default="USD")
    pct = models.DecimalField(max_digits=5, decimal_places=2, default=0,
        help_text="% использованный для расчёта (для basis_fee и logistics_margin)")
    basis = models.CharField(max_length=10, choices=BASIS_CHOICES, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "kind"], name="rev_order_kind_idx"),
            models.Index(fields=["kind", "-created_at"], name="rev_kind_created_idx"),
        ]

    def __str__(self):
        return f"Rev[{self.order_id}/{self.kind}]: ${self.amount}"


class OperatorBonusLine(models.Model):
    """Вознаграждение оператора за закрытую сделку.

    Единая комиссия по Incoterm:
      FOB 0.4% · CIP 0.5% · DDP 0.7% от стоимости товара
      min $50 / max $5,000 на сделку
      −50% при подтверждённой вине оператора

    Жизненный цикл строки:
      • pending  — создана при release (status=delivered + payment=paid),
                   холд 14 дней на случай рекламации
      • released — спустя 14 дней без проблем → зачисление в Wallet оператора
      • withheld — рекламация подтверждена по вине → коммисия удержана
      • reduced  — частично выплачена (−50%) при вине оператора
    """
    STATUS_CHOICES = [
        ("pending",  "Холд (14 дней)"),
        ("released", "Зачислено"),
        ("withheld", "Удержано (вина оператора)"),
        ("reduced",  "−50% (вина оператора)"),
    ]
    BASIS_CHOICES = [("FOB", "FOB"), ("CIP", "CIP"), ("DDP", "DDP")]
    RATE_BY_BASIS = {"FOB": 0.40, "CIP": 0.50, "DDP": 0.70}  # в процентах
    MIN_BONUS_USD = 50
    MAX_BONUS_USD = 5000

    operator = models.ForeignKey(User, on_delete=models.CASCADE,
                                  related_name="bonus_lines")
    order = models.OneToOneField(Order, on_delete=models.CASCADE,
                                  related_name="operator_bonus")
    basis = models.CharField(max_length=10, choices=BASIS_CHOICES)
    base_amount = models.DecimalField(max_digits=14, decimal_places=2,
                                       help_text="Стоимость товара (база начисления)")
    rate_pct = models.DecimalField(max_digits=5, decimal_places=2,
                                    help_text="% применённый к base_amount")
    amount = models.DecimalField(max_digits=14, decimal_places=2,
                                  help_text="Итоговая сумма бонуса (USD), с учётом min/max")
    currency = models.CharField(max_length=10, default="USD")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    release_at = models.DateTimeField(null=True, blank=True,
                                       help_text="Когда выйти из холда (created_at + 14 дней)")
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["operator", "-created_at"],
                         name="opbon_op_created_idx"),
            models.Index(fields=["status", "release_at"],
                         name="opbon_status_release_idx"),
        ]

    def __str__(self):
        return f"Bonus[{self.operator_id}/{self.order_id}]: ${self.amount} ({self.status})"


class ReferralReward(models.Model):
    """Реферальное вознаграждение пригласившего — для всех ролей, КРОМЕ KAM.

    Мотивация по ролям (согласована с владельцем продукта):
      • Покупатель приглашает → −$100 на свой первый заказ (зачёт при
        пополнении депозита) — kind=buyer_discount, начисляется один раз
        на пригласившего при ближайшем пополнении кошелька.
      • Продавец / оператор / прочие → $100 с первой покупки приглашённого
        — kind=flat_first_order, начисляется когда приглашённый оплатил
        резерв своего первого заказа.
      • KAM — НЕ через эту таблицу: его «награда» = CRM-привязка клиента и
        резидуальные начисления (OperatorBonusLine / customer_bonuses),
        0.02% со сделок + бонус с дожатых отказных.

    Жизненный цикл строки: pending → credited (зачислено в Wallet) | cancelled.
    Идемпотентность — через uniq-констрейнты ниже + проверку статуса под
    транзакцией в assistant/referral.py.
    """
    KIND_CHOICES = [
        ("flat_first_order", "$100 за первый заказ приглашённого"),
        ("buyer_discount",   "−$100 на первый заказ (зачёт при пополнении)"),
    ]
    STATUS_CHOICES = [
        ("pending",   "Ожидает условия"),
        ("credited",  "Зачислено"),
        ("cancelled", "Отменено"),
    ]
    FLAT_AMOUNT_USD = 100

    referrer = models.ForeignKey(User, on_delete=models.CASCADE,
                                  related_name="referral_rewards_given")
    referred = models.ForeignKey(User, on_delete=models.SET_NULL,
                                  null=True, blank=True,
                                  related_name="referral_rewards_received")
    referrer_role = models.CharField(max_length=32, blank=True,
                                      help_text="Роль пригласившего на момент приглашения")
    kind = models.CharField(max_length=24, choices=KIND_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=FLAT_AMOUNT_USD)
    currency = models.CharField(max_length=10, default="USD")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                               default="pending", db_index=True)
    trigger_order = models.ForeignKey(Order, on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name="+",
                                       help_text="Заказ, активировавший начисление")
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    credited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # flat_first_order — один раз на пару (пригласивший, приглашённый)
            models.UniqueConstraint(
                fields=["referrer", "referred", "kind"],
                condition=models.Q(kind="flat_first_order"),
                name="refrwd_uniq_flat_pair",
            ),
            # buyer_discount — один раз на пригласившего (его собственная скидка)
            models.UniqueConstraint(
                fields=["referrer", "kind"],
                condition=models.Q(kind="buyer_discount"),
                name="refrwd_uniq_buyer_discount",
            ),
        ]
        indexes = [
            models.Index(fields=["referred", "status"], name="refrwd_referred_status_idx"),
            models.Index(fields=["referrer", "-created_at"], name="refrwd_referrer_idx"),
        ]

    def __str__(self):
        return f"Ref[{self.referrer_id}→{self.referred_id}] {self.kind} ${self.amount} ({self.status})"


class ReferralCode(models.Model):
    """Короткий человекочитаемый реф-код пользователя — для аккуратных ссылок
    вида consolidatorparts.com/i/AB23CD вместо длинного подписанного токена.

    Алфавит без двусмысленных символов (0/O, 1/I/L). Код случайный → не
    перебирается и не подделывается (в отличие от обратимого кодека id).
    """
    ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # без 0 O 1 I L

    user = models.OneToOneField(User, on_delete=models.CASCADE,
                                 related_name="referral_code")
    code = models.CharField(max_length=16, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} → user {self.user_id}"

    @classmethod
    def _gen(cls, n=8):
        # 8 символов из 31-буквенного алфавита ≈ 8.5e11 вариантов — запас против
        # перебора при сохранении читабельности (без 0/O/1/I/L).
        import secrets
        return "".join(secrets.choice(cls.ALPHABET) for _ in range(n))

    @classmethod
    def for_user(cls, user):
        """Get-or-create уникальный короткий код для пользователя.

        Race-safe: каждый INSERT в своём savepoint (transaction.atomic). При
        коллизии кода — повтор; при гонке по user (OneToOne уже создан другим
        запросом) — возвращаем уже существующую строку.
        """
        from django.db import IntegrityError, transaction
        obj = cls.objects.filter(user=user).first()
        if obj:
            return obj
        for n in range(12):
            code = cls._gen(8 if n < 8 else 12)
            try:
                with transaction.atomic():
                    return cls.objects.create(user=user, code=code)
            except IntegrityError:
                existing = cls.objects.filter(user=user).first()
                if existing:           # гонка по user — вернём чужой инсерт
                    return existing
                continue               # коллизия кода — пробуем другой
        # Крайне маловероятно: длинный код как последний шанс
        return cls.objects.create(user=user, code=cls._gen(14))

    @classmethod
    def resolve(cls, code):
        """Код → пользователь (None если нет). Без учёта регистра."""
        obj = (cls.objects.filter(code=(code or "").strip().upper())
               .select_related("user").first())
        return obj.user if obj else None


class MissingDemand(models.Model):
    """Аналитика спроса без предложения (PIVOT 2026-05-26).

    Каждый раз когда покупатель запрашивает OEM-номер, которого НЕТ в каталоге
    ни у одного поставщика, мы фиксируем это здесь. На основе агрегатов отдел
    развития каталога ищет и заводит новых поставщиков с этими позициями.

    Идемпотентность: одна строка на (oem, day) — counter++ при повторных запросах.
    """
    oem = models.CharField(max_length=128, db_index=True)
    day = models.DateField(db_index=True)
    buyer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="missing_demand_records")
    count = models.PositiveIntegerField(default=1)
    rfq_id = models.IntegerField(null=True, blank=True,
                                  help_text="ID первого RFQ где это спросили")
    last_rfq_id = models.IntegerField(null=True, blank=True,
                                       help_text="ID последнего RFQ где это спросили")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("oem", "day")]
        ordering = ["-day", "-count"]
        indexes = [
            models.Index(fields=["-day", "-count"], name="missdem_day_count_idx"),
            models.Index(fields=["oem", "-day"], name="missdem_oem_day_idx"),
        ]

    def __str__(self):
        return f"{self.oem} · {self.day} × {self.count}"


class BuyerVolumeYearly(models.Model):
    """ТЗ §4.1: годовой объём закупок клиента → уровень auto-discount.

    Уровень рассчитывается из суммы paid+completed orders за календарный год:
      ≥ 1 000 000 000 ₽ (~$11M) → level 3 → discount 3%
      ≥   500 000 000 ₽ (~$5.5M) → level 2 → discount 1.5%
      ≥   100 000 000 ₽ (~$1.1M) → level 1 → discount 1%
      < 100M                              → level 0 → 0%

    Пересчитывается:
      • по событию: order.payment_status='paid' → recalc для buyer
      • по cron: ежедневный пересчёт уровней (см. mgmt command)
    """
    DISCOUNT_LEVELS = [
        (0, "Без скидки"),
        (1, "Уровень 1 (≥100M, 1%)"),
        (2, "Уровень 2 (≥500M, 1.5%)"),
        (3, "Уровень 3 (≥1B, 3%)"),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="volume_yearly")
    year = models.PositiveIntegerField(db_index=True)
    volume_usd = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    level = models.PositiveSmallIntegerField(choices=DISCOUNT_LEVELS, default=0)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    last_recalculated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("user", "year")]
        ordering = ["-year"]
        indexes = [
            models.Index(fields=["user", "-year"], name="bvy_user_year_idx"),
            models.Index(fields=["level"], name="bvy_level_idx"),
        ]

    def __str__(self):
        return f"{self.user.username}/{self.year}: ${self.volume_usd} L{self.level}"


class TwoFactorAuth(models.Model):
    """TOTP-based 2FA. Stored separately for security."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="twofa")
    secret = models.CharField(max_length=64, blank=True, help_text="Base32-encoded TOTP secret")
    enabled = models.BooleanField(default=False)
    backup_codes = models.TextField(blank=True, help_text="One-time backup codes, comma-separated")
    enabled_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"2FA[{self.user_id}]={'on' if self.enabled else 'off'}"


class MagicLinkToken(models.Model):
    """Passwordless login: одноразовый токен в email. TTL 15 минут.

    Flow:
      1. User вводит email → POST /accounts/magic-link/
      2. Создаётся MagicLinkToken, ссылка шлётся в email
      3. User кликает → GET /accounts/magic-link/<token>/
      4. Если active (не used, не expired) → login + redirect
    """
    token = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="magic_links")
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    ip_requested = models.CharField(max_length=64, blank=True)
    ip_used = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="magic_user_created_idx"),
        ]

    def __str__(self):
        return f"MagicLink[{self.user_id}]={'used' if self.used_at else 'active'}"

    @property
    def is_active(self):
        return self.used_at is None and self.expires_at > timezone.now()


class ApiToken(models.Model):
    """API tokens для программного доступа партнёров.

    Хранится только prefix + hashed_token (как у Stripe sk_live_xxx).
    Реальный token виден один раз при создании.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    label = models.CharField(max_length=80, help_text="Человеко-читаемое имя ('CI deploy', 'Telegram bot')")
    prefix = models.CharField(max_length=12, db_index=True,
        help_text="Первые символы токена для UI ('ck_live_abcd…')")
    hashed_token = models.CharField(max_length=128, unique=True, db_index=True,
        help_text="SHA-256 hex от полного токена")
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    permissions = models.CharField(max_length=200, default="read",
        help_text="CSV: read,write,admin")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="apitoken_user_created_idx"),
        ]

    def __str__(self):
        return f"ApiToken[{self.user_id}] {self.prefix}…"

    @property
    def is_active(self):
        return self.revoked_at is None


class LearnedColumnSynonym(models.Model):
    """ТЗ: «AI возвращает маппинг — добавляй новые варианты в словарь».

    Накопленные синонимы, найденные через AI или ручное переопределение
    seller'ом. При следующих загрузках применяется поверх статического
    COLUMN_MAP (assistant/price_mappings.py).
    """
    SOURCE_CHOICES = [
        ("ai",     "AI"),
        ("manual", "Ручной"),
        ("seed",   "Seed"),
    ]
    canonical = models.CharField(max_length=40, db_index=True,
        help_text="Канонический ключ (part_number, description, …)")
    raw_header = models.CharField(max_length=200,
        help_text="Оригинальный заголовок как из файла (для аудита)")
    header_normalized = models.CharField(max_length=200, unique=True,
        db_index=True,
        help_text="Нормализованный заголовок (lower + trim + без разделителей)")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="ai")
    learned_at = models.DateTimeField(default=timezone.now)
    use_count = models.PositiveIntegerField(default=0,
        help_text="Сколько раз применялся (для аналитики)")

    class Meta:
        ordering = ["-learned_at"]
        indexes = [
            models.Index(fields=["canonical", "-learned_at"], name="lcs_canonical_idx"),
        ]

    def __str__(self):
        return f"LCS[{self.id}] '{self.raw_header}' → {self.canonical}"


class PricelistMapping(models.Model):
    """ТЗ: запоминаем последний маппинг колонок прайса для seller'а.

    На каждом следующем upload AI предлагает этот маппинг как дефолт,
    seller может изменить — тогда сохраняется новый.
    """
    seller = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="pricelist_mapping",
    )
    # mapping: {standard_field: source_column_header_or_index_str}
    # Пример: {"oem_number": "Артикул", "title": "Наименование",
    #          "price": "Цена", "currency": "Валюта"}
    mapping = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"PricelistMapping[{self.seller_id}] {len(self.mapping)} cols"


class SupplierImportProfile(models.Model):
    """Профиль импорта поставщика — кеш маппингов + правил трансформации.

    При повторной загрузке от того же поставщика (определяется по
    fingerprint заголовков) AI не вызывается, маппинг берётся из профиля.
    """
    seller = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="import_profiles",
    )
    name = models.CharField(max_length=200, blank=True,
        help_text="Имя профиля (авто или ручное)")
    headers_fingerprint = models.CharField(max_length=64, db_index=True,
        help_text="SHA256 от отсортированных нормализованных заголовков")
    source_headers = models.JSONField(default=list,
        help_text="Оригинальные заголовки из файла")
    column_mapping = models.JSONField(default=dict,
        help_text="{std_field: source_header_or_fix}")
    transform_rules = models.JSONField(default=dict, blank=True,
        help_text='{std_field: {"formula": "price * 1.15", "type": "formula"}}')
    constants = models.JSONField(default=dict, blank=True,
        help_text='{std_field: "fixed_value"} — поля с постоянными значениями')
    header_row = models.PositiveIntegerField(default=1,
        help_text="Номер строки с заголовками (1-based)")
    data_start_row = models.PositiveIntegerField(default=2,
        help_text="Номер строки, с которой начинаются данные (1-based)")
    use_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["seller", "headers_fingerprint"]),
            models.Index(fields=["seller", "-updated_at"]),
        ]
        unique_together = [("seller", "headers_fingerprint")]

    def __str__(self):
        return f"ImportProfile[{self.id}] {self.seller_id} ({self.name or 'auto'})"

    @staticmethod
    def compute_fingerprint(headers: list[str]) -> str:
        import hashlib

        from assistant.price_mappings import normalize
        normalized = sorted(normalize(h) for h in headers if str(h).strip())
        return hashlib.sha256("|".join(normalized).encode()).hexdigest()


class PricelistImport(models.Model):
    """Журнал каждой загрузки прайса."""
    STATUS_CHOICES = [
        ("preview",   "Превью (ждём подтверждения)"),
        ("imported",  "Импортирован"),
        ("failed",    "Ошибка"),
        ("cancelled", "Отменён"),
    ]
    seller = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="pricelist_imports",
    )
    file_obj = models.FileField(upload_to="pricelists/%Y/%m/", blank=True)
    filename = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="preview")
    headers = models.JSONField(default=list, blank=True,
        help_text="Заголовки колонок, прочитанные из файла")
    sample_rows = models.JSONField(default=list, blank=True,
        help_text="Первые 3 строки для AI и UI")
    suggested_mapping = models.JSONField(default=dict, blank=True,
        help_text="Маппинг, предложенный AI (или предыдущий)")
    final_mapping = models.JSONField(default=dict, blank=True,
        help_text="Маппинг, подтверждённый seller'ом")
    total_rows = models.PositiveIntegerField(default=0)
    imported_rows = models.PositiveIntegerField(default=0,
        help_text="created + updated")
    created_rows = models.PositiveIntegerField(default=0)
    updated_rows = models.PositiveIntegerField(default=0)
    failed_rows = models.PositiveIntegerField(default=0)
    error_details = models.JSONField(default=list, blank=True,
        help_text="[{row, oem, reason}] первых ~50 ошибок")
    ai_called = models.BooleanField(default=False, db_index=True,
        help_text="Был ли вызов AI для маппинга колонок (для лимита 3/день)")
    ai_estimates = models.JSONField(default=dict, blank=True,
        help_text="AI-оценки per-part вес/габариты по описанию: {oem: {weight_kg, length_cm, ...}}")
    output_file = models.FileField(upload_to="pricelist_outputs/%Y/%m/", blank=True, null=True,
        help_text="Сгенерированный XLSX в формате маркетплейса (как у claude.ai)")
    output_preview_html = models.TextField(blank=True,
        help_text="Кэш HTML-превью первых 100 строк (для мгновенного preview)")
    output_total_rows = models.PositiveIntegerField(default=0,
        help_text="Сколько строк в output_file")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["seller", "-created_at"], name="pricelist_seller_created_idx"),
        ]

    def __str__(self):
        return f"PricelistImport[{self.id}] {self.seller_id} {self.status}"


class PartReference(models.Model):
    """Эталонная база запчастей — точные данные из таможни, дилеров, OEM-каталогов.

    Используется как enrichment layer при импорте прайс-листов:
    если в файле seller'а нет веса/габаритов — берём отсюда (если знаем).
    Это намного точнее чем AI guess.

    Источники (в поле `source`):
      - customs:  таможенные декларации (HS code + реальный вес)
      - dealer:   официальный каталог дилера (Caterpillar, Komatsu, ...)
      - oem:      OEM-каталог производителя
      - manual:   ручной ввод оператора
      - ai:       AI-оценка (для items без других источников)

    Lookup priority при импорте: customs > dealer > oem > manual > ai.
    """
    SOURCE_CHOICES = [
        ("customs",  "Таможенная декларация"),
        ("dealer",   "Дилер"),
        ("oem",      "OEM-каталог"),
        ("manual",   "Ручной ввод"),
        ("ai",       "AI-оценка"),
    ]
    oem_number = models.CharField(max_length=100, db_index=True,
        help_text="OEM-номер запчасти — основной ключ для lookup")
    brand = models.CharField(max_length=100, blank=True, db_index=True,
        help_text="Бренд (Caterpillar, Komatsu, ...) — для уточнения lookup")
    title = models.CharField(max_length=255, blank=True,
        help_text="Название/описание из источника")
    weight_kg = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    length_cm = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    width_cm  = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    height_cm = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    hs_code   = models.CharField(max_length=20, blank=True, db_index=True,
        help_text="Код ТН ВЭД из таможни")
    cross_numbers = models.CharField(max_length=500, blank=True,
        help_text="Перекрёстные номера (cross/reference numbers) из каталогов")
    country_of_origin = models.CharField(max_length=80, blank=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual",
        db_index=True)
    source_ref = models.CharField(max_length=255, blank=True,
        help_text="Ссылка/ID в источнике (номер декларации, URL дилера, ...)")
    confidence = models.FloatField(default=1.0,
        help_text="0.0-1.0. Для customs/dealer = 1.0, для AI = 0.7-0.9")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["oem_number", "brand"], name="part_ref_oem_brand_idx"),
            models.Index(fields=["source", "-created_at"], name="part_ref_source_idx"),
        ]
        # Один (oem, brand, source) — одна запись (последняя по updated_at)
        constraints = [
            models.UniqueConstraint(
                fields=["oem_number", "brand", "source"],
                name="uniq_part_ref_oem_brand_source",
            ),
        ]

    def __str__(self):
        return f"PartRef[{self.oem_number}|{self.brand}|{self.source}]"


class ErpSyncLog(models.Model):
    """ТЗ §17.2: журнал двустороннего обмена с 1С/ERP.

    Каждая операция (push/pull, parts/orders/ack) пишется отдельной
    строкой. Используется для аудита, диагностики, идемпотентности
    (проверка по external_ref).
    """
    DIRECTION_CHOICES = [
        ("push", "Push (платформа → ERP)"),
        ("pull", "Pull (ERP → платформа)"),
    ]
    KIND_CHOICES = [
        ("parts",      "Каталог: цены/остатки"),
        ("orders",     "Заказы: новые на отгрузку"),
        ("order_ack",  "Подтверждение заказа от 1С"),
        ("status",     "Обновление статуса заказа"),
    ]
    STATUS_CHOICES = [
        ("ok",      "Успешно"),
        ("partial", "Частично"),
        ("failed",  "Ошибка"),
    ]

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="erp_sync_logs",
                              help_text="Чей ERP — обычно seller или operator")
    direction = models.CharField(max_length=8, choices=DIRECTION_CHOICES)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default="ok")
    items_count = models.PositiveIntegerField(default=0)
    items_failed = models.PositiveIntegerField(default=0)
    external_ref = models.CharField(max_length=120, blank=True, db_index=True,
        help_text="Внешний идентификатор (для идемпотентности)")
    payload = models.JSONField(default=dict, blank=True,
        help_text="Краткая выжимка содержимого для отладки")
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="erpsync_user_created_idx"),
            models.Index(fields=["kind", "-created_at"], name="erpsync_kind_created_idx"),
        ]

    def __str__(self):
        return f"ErpSync[{self.id}] {self.direction}/{self.kind} {self.status}"


# ── Knowledge Base ─────────────────────────────────────────────
# Редактируется оператором через /admin/. Раньше FAQ был хардкодом
# в assistant/support_hub.py — для каждого изменения нужен был релиз.
# Теперь оператор сам добавляет вопросы/ответы.

class KnowledgeBaseEntry(models.Model):
    """FAQ-entry для Support Hub в чате (kb_faq action).

    Видим всем юзерам через `🛟 Поддержка → ❓ Частые вопросы`.
    Полнотекстовый поиск через PostgreSQL tsvector — см. search() ниже.
    """
    CATEGORY_CHOICES = [
        ("registration",   "📝 Регистрация"),
        ("kyb",            "🛡 KYB / Верификация"),
        ("payment",        "💰 Заказ и оплата"),
        ("delivery",       "🚚 Доставка и сроки"),
        ("claims",         "🧾 Рекламации"),
        ("contacts",       "👥 Контакты сторон"),
        ("bonuses",        "🎁 Бонусы"),
        ("platform",       "⚙️ Платформа"),
        ("support",        "🛟 Поддержка"),
        ("other",          "❓ Другое"),
    ]

    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES,
                                default="other", db_index=True)
    question = models.CharField(max_length=300,
        help_text="Короткий вопрос — отображается как заголовок")
    answer = models.TextField(
        help_text="Полный ответ. Markdown НЕ поддерживается, plain-text.")
    is_active = models.BooleanField(default=True, db_index=True,
        help_text="Снимите чтобы скрыть entry без удаления.")
    sort_order = models.PositiveIntegerField(default=100, db_index=True,
        help_text="Меньше число — выше в списке (для важных вопросов).")
    views = models.PositiveIntegerField(default=0,
        help_text="Сколько раз показано (для аналитики популярности).")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="kb_entries_created")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "sort_order", "id"]
        verbose_name = "FAQ entry"
        verbose_name_plural = "FAQ entries"
        indexes = [
            models.Index(fields=["category", "is_active", "sort_order"],
                          name="kb_cat_sort_idx"),
        ]

    def __str__(self):
        return f"[{self.category}] {self.question[:60]}"

    @classmethod
    def search(cls, query: str, *, limit: int = 50):
        """Полнотекстовый поиск + фильтр по категории.

        PostgreSQL: SearchVector(question + answer) + russian config.
        SQLite (dev): fallback на icontains по обоим полям.
        """
        from django.db import connection
        qs = cls.objects.filter(is_active=True)
        if not query:
            return qs[:limit]
        if connection.vendor == "postgresql":
            from django.contrib.postgres.search import (
                SearchQuery, SearchRank, SearchVector,
            )
            vec = SearchVector("question", weight="A", config="russian") \
                + SearchVector("answer", weight="B", config="russian")
            q = SearchQuery(query, config="russian")
            return (qs.annotate(rank=SearchRank(vec, q))
                      .filter(rank__gt=0)
                      .order_by("-rank")[:limit])
        # SQLite-fallback: substring по lower
        from django.db.models import Q
        q_low = query.lower()
        return qs.filter(Q(question__icontains=q_low)
                          | Q(answer__icontains=q_low))[:limit]


class CustomsRecord(models.Model):
    """Таможенная аналитика — высокоценный ручной засев администратора.
    Реальные данные ввоза (ФТС-выписки и т.п.): HS-код, страна, объём, цена.
    Привязка по oem_number обогащает граф рынка реальными ценами импорта."""
    DIRECTION = [("import", "Импорт"), ("export", "Экспорт")]
    hs_code = models.CharField(max_length=14, db_index=True, verbose_name="ТН ВЭД / HS")
    commodity = models.CharField(max_length=300, blank=True, verbose_name="Товар")
    oem_number = models.CharField(max_length=100, blank=True, db_index=True, verbose_name="Парт-номер")
    direction = models.CharField(max_length=8, choices=DIRECTION, default="import")
    origin_country = models.CharField(max_length=2, blank=True, verbose_name="Страна происх.")
    dest_country = models.CharField(max_length=2, default="RU", verbose_name="Страна назн.")
    importer = models.CharField(max_length=255, blank=True, verbose_name="Импортёр")
    importer_inn = models.CharField(max_length=20, blank=True, db_index=True)
    supplier = models.CharField(max_length=255, blank=True, verbose_name="Поставщик/отправитель")
    qty = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    unit = models.CharField(max_length=20, blank=True, default="шт")
    net_weight_kg = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    customs_value_usd = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    period = models.DateField(null=True, blank=True, verbose_name="Период (месяц)")
    source = models.CharField(max_length=120, blank=True, default="manual")
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="customs_records")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period", "-created_at"]
        indexes = [
            models.Index(fields=["hs_code", "origin_country"]),
            models.Index(fields=["oem_number"]),
        ]

    def __str__(self) -> str:
        return f"{self.hs_code} {self.origin_country}→{self.dest_country} ${self.customs_value_usd}"
