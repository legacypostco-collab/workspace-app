from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from catalog.models import Product, ProductCrossReference
from files.models import StoredFile
from files.storage import read_stored_file_bytes, store_generated_file_bytes
from offers.models import SupplierOffer, SupplierOfferPrice

from .models import ImportErrorReport, ImportJob, ImportRow


def _normalize_header(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("_", " ").split())


def _decode_csv_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


@dataclass
class PreviewResult:
    fieldnames: list[str]
    detected_columns: dict[str, str]
    sample_rows: list[dict[str, str]]


class ImportParser:
    OEM_ALIASES = {"oem", "part number", "partnumber", "sku", "article"}
    PRICE_ALIASES = {"price", "unitprice", "unit price", "price exw", "price fob sea", "price fob air"}
    PRICE_EXW_ALIASES = {"price exw", "exw", "price"}
    PRICE_FOB_SEA_ALIASES = {"price fob sea", "fob sea", "fob_sea"}
    PRICE_FOB_AIR_ALIASES = {"price fob air", "fob air", "fob_air"}
    BRAND_ALIASES = {"brand", "make", "manufacturer"}
    NAME_ALIASES = {"name", "description", "title"}
    QUANTITY_ALIASES = {"quantity", "qty", "stock"}
    CROSS_NUMBER_ALIASES = {"crossnumber", "cross number", "cross_number", "analog", "cross"}
    CONDITION_ALIASES = {"condition", "item condition", "state"}
    WAREHOUSE_ALIASES = {"warehouseaddress", "warehouse address", "warehouse"}
    SEA_PORT_ALIASES = {"seaport", "sea port"}
    AIR_PORT_ALIASES = {"airport", "air port"}
    WEIGHT_ALIASES = {"weight"}
    LENGTH_ALIASES = {"length"}
    WIDTH_ALIASES = {"width"}
    HEIGHT_ALIASES = {"height"}

    FIELD_ALIASES: dict[str, set[str]] = {
        "oem": OEM_ALIASES,
        "cross_number": CROSS_NUMBER_ALIASES,
        "brand": BRAND_ALIASES,
        "name": NAME_ALIASES,
        "quantity": QUANTITY_ALIASES,
        "condition": CONDITION_ALIASES,
        "price_exw": PRICE_EXW_ALIASES,
        "price_fob_sea": PRICE_FOB_SEA_ALIASES,
        "price_fob_air": PRICE_FOB_AIR_ALIASES,
        "warehouse_address": WAREHOUSE_ALIASES,
        "sea_port": SEA_PORT_ALIASES,
        "air_port": AIR_PORT_ALIASES,
        "weight": WEIGHT_ALIASES,
        "length": LENGTH_ALIASES,
        "width": WIDTH_ALIASES,
        "height": HEIGHT_ALIASES,
    }

    def parse_csv_rows(self, storage_key: str) -> list[tuple[int, dict[str, str]]]:
        raw = read_stored_file_bytes(storage_key)
        text = _decode_csv_bytes(raw)
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return []
        rows: list[tuple[int, dict[str, str]]] = []
        for row_no, row in enumerate(reader, start=2):
            raw_row = {str(k or "").strip(): str(v or "").strip() for k, v in row.items()}
            rows.append((row_no, raw_row))
        return rows

    def infer_column_mapping(self, fieldnames: list[str]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for header in fieldnames:
            normalized_header = _normalize_header(header)
            for target, aliases in self.FIELD_ALIASES.items():
                if target in mapping:
                    continue
                if normalized_header in aliases:
                    mapping[target] = header
        return mapping

    def build_preview(self, storage_key: str, rows_limit: int = 10) -> PreviewResult:
        raw = read_stored_file_bytes(storage_key)
        text = _decode_csv_bytes(raw)
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = [str(h or "").strip() for h in (reader.fieldnames or []) if str(h or "").strip()]
        mapping = self.infer_column_mapping(fieldnames)
        sample_rows: list[dict[str, str]] = []
        for row in reader:
            sample_rows.append({str(k or "").strip(): str(v or "").strip() for k, v in row.items()})
            if len(sample_rows) >= rows_limit:
                break
        return PreviewResult(fieldnames=fieldnames, detected_columns=mapping, sample_rows=sample_rows)

    def extract_fields(self, raw_row: dict[str, str], column_mapping: dict[str, str] | None = None) -> dict[str, str]:
        extracted = {
            "oem": "",
            "price": "",
            "price_exw": "",
            "price_fob_sea": "",
            "price_fob_air": "",
            "brand": "",
            "name": "",
            "quantity": "",
            "cross_number": "",
            "condition": "",
            "warehouse_address": "",
            "sea_port": "",
            "air_port": "",
            "weight": "",
            "length": "",
            "width": "",
            "height": "",
        }
        if column_mapping:
            for target, source_column in column_mapping.items():
                if target in extracted and source_column in raw_row:
                    extracted[target] = (raw_row.get(source_column) or "").strip()
            if not extracted["price_exw"]:
                extracted["price_exw"] = extracted["price"]
            if not extracted["price"]:
                extracted["price"] = extracted["price_exw"] or extracted["price_fob_sea"] or extracted["price_fob_air"]
            return extracted

        for key, value in raw_row.items():
            normalized_key = _normalize_header(key)
            if normalized_key in self.OEM_ALIASES and not extracted["oem"]:
                extracted["oem"] = value.strip()
            elif normalized_key in self.PRICE_ALIASES and not extracted["price"]:
                extracted["price"] = value.strip()
            elif normalized_key in self.PRICE_EXW_ALIASES and not extracted["price_exw"]:
                extracted["price_exw"] = value.strip()
            elif normalized_key in self.PRICE_FOB_SEA_ALIASES and not extracted["price_fob_sea"]:
                extracted["price_fob_sea"] = value.strip()
            elif normalized_key in self.PRICE_FOB_AIR_ALIASES and not extracted["price_fob_air"]:
                extracted["price_fob_air"] = value.strip()
            elif normalized_key in self.BRAND_ALIASES and not extracted["brand"]:
                extracted["brand"] = value.strip()
            elif normalized_key in self.NAME_ALIASES and not extracted["name"]:
                extracted["name"] = value.strip()
            elif normalized_key in self.QUANTITY_ALIASES and not extracted["quantity"]:
                extracted["quantity"] = value.strip()
            elif normalized_key in self.CROSS_NUMBER_ALIASES and not extracted["cross_number"]:
                extracted["cross_number"] = value.strip()
            elif normalized_key in self.CONDITION_ALIASES and not extracted["condition"]:
                extracted["condition"] = value.strip()
            elif normalized_key in self.WAREHOUSE_ALIASES and not extracted["warehouse_address"]:
                extracted["warehouse_address"] = value.strip()
            elif normalized_key in self.SEA_PORT_ALIASES and not extracted["sea_port"]:
                extracted["sea_port"] = value.strip()
            elif normalized_key in self.AIR_PORT_ALIASES and not extracted["air_port"]:
                extracted["air_port"] = value.strip()
            elif normalized_key in self.WEIGHT_ALIASES and not extracted["weight"]:
                extracted["weight"] = value.strip()
            elif normalized_key in self.LENGTH_ALIASES and not extracted["length"]:
                extracted["length"] = value.strip()
            elif normalized_key in self.WIDTH_ALIASES and not extracted["width"]:
                extracted["width"] = value.strip()
            elif normalized_key in self.HEIGHT_ALIASES and not extracted["height"]:
                extracted["height"] = value.strip()
        if not extracted["price_exw"]:
            extracted["price_exw"] = extracted["price"]
        if not extracted["price"]:
            extracted["price"] = extracted["price_exw"] or extracted["price_fob_sea"] or extracted["price_fob_air"]
        return extracted


class ColumnMappingResolver:
    REQUIRED_KEYS = {"oem", "warehouse_address"}
    PRICE_KEYS = {"price_exw", "price_fob_sea", "price_fob_air"}

    def validate_mapping(self, mapping: dict[str, str], fieldnames: list[str]) -> tuple[bool, str]:
        normalized_fieldnames = {str(f or "").strip() for f in fieldnames}
        for key in self.REQUIRED_KEYS:
            if not (mapping.get(key) or "").strip():
                return False, f"Не задана обязательная колонка для поля: {key}."
        has_price = any((mapping.get(k) or "").strip() for k in self.PRICE_KEYS)
        if not has_price:
            return False, "Нужна хотя бы одна колонка цены: price_exw/price_fob_sea/price_fob_air."
        for target, source in mapping.items():
            if source and source not in normalized_fieldnames:
                return False, f"Колонка '{source}' не найдена в файле для поля {target}."
        return True, ""

class OEMNormalizer:
    @staticmethod
    def normalize_oem(value: str | None) -> str:
        cleaned = (value or "").strip()
        return cleaned.upper() if cleaned else ""

    @staticmethod
    def normalize_brand(value: str | None) -> str:
        cleaned = (value or "").strip()
        return cleaned.upper() if cleaned else ""

    @staticmethod
    def normalize_condition(value: str | None) -> str:
        raw = (value or "").strip().upper()
        if raw in {"", "NEW"}:
            return SupplierOffer.Condition.OEM
        alias_map = {
            "ORIGINAL": SupplierOffer.Condition.ORIGINAL,
            "OEM": SupplierOffer.Condition.OEM,
            "AFTERMARKET": SupplierOffer.Condition.AFTERMARKET,
            "REMAN": SupplierOffer.Condition.REMAN,
        }
        return alias_map.get(raw, raw)


@dataclass
class ValidationResult:
    is_valid: bool
    error_code: str = ""
    error_message: str = ""
    error_hint: str = ""
    parsed_price: Decimal | None = None
    parsed_quantity: int | None = None
    parsed_condition: str = SupplierOffer.Condition.OEM
    parsed_price_exw: Decimal | None = None
    parsed_price_fob_sea: Decimal | None = None
    parsed_price_fob_air: Decimal | None = None
    parsed_weight: Decimal | None = None
    parsed_length: Decimal | None = None
    parsed_width: Decimal | None = None
    parsed_height: Decimal | None = None


class ImportValidator:
    @staticmethod
    def _parse_decimal(raw: str) -> Decimal | None:
        cleaned = (raw or "").strip().replace(" ", "").replace(",", ".")
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None

    @staticmethod
    def _parse_non_negative_decimal(raw: str, field_name: str) -> tuple[Decimal | None, ValidationResult | None]:
        if not (raw or "").strip():
            return None, None
        parsed = ImportValidator._parse_decimal(raw)
        if parsed is None:
            return None, ValidationResult(
                is_valid=False,
                error_code="invalid_format",
                error_message=f"Некорректный формат поля {field_name}: {raw}.",
                error_hint=f"Укажите {field_name} числом больше или равно 0.",
            )
        if parsed < 0:
            return None, ValidationResult(
                is_valid=False,
                error_code="invalid_format",
                error_message=f"Поле {field_name} не может быть отрицательным: {raw}.",
                error_hint=f"Укажите {field_name} числом больше или равно 0.",
            )
        return parsed, None

    def validate(self, extracted: dict[str, str]) -> ValidationResult:
        oem = (extracted.get("oem") or "").strip()
        warehouse_address = (extracted.get("warehouse_address") or "").strip()
        price_raw = (extracted.get("price") or "").strip()
        price_exw_raw = (extracted.get("price_exw") or "").strip()
        price_fob_sea_raw = (extracted.get("price_fob_sea") or "").strip()
        price_fob_air_raw = (extracted.get("price_fob_air") or "").strip()
        quantity_raw = (extracted.get("quantity") or "").strip()
        condition_raw = (extracted.get("condition") or "").strip()

        if not oem:
            return ValidationResult(
                is_valid=False,
                error_code="missing_oem",
                error_message="Не заполнено обязательное поле OEM.",
                error_hint="Добавьте значение в колонку OEM/Part Number.",
            )
        if not warehouse_address:
            return ValidationResult(
                is_valid=False,
                error_code="missing_warehouse_address",
                error_message="Не заполнено обязательное поле WarehouseAddress.",
                error_hint="Добавьте адрес склада в колонку WarehouseAddress.",
            )
        if not (price_raw or price_exw_raw or price_fob_sea_raw or price_fob_air_raw):
            return ValidationResult(
                is_valid=False,
                error_code="missing_price",
                error_message="Не заполнена ни одна цена (EXW/FOB SEA/FOB AIR).",
                error_hint="Добавьте цену больше 0 минимум в одну колонку цены.",
            )

        condition = OEMNormalizer.normalize_condition(condition_raw)
        if condition not in {
            SupplierOffer.Condition.ORIGINAL,
            SupplierOffer.Condition.OEM,
            SupplierOffer.Condition.AFTERMARKET,
            SupplierOffer.Condition.REMAN,
        }:
            return ValidationResult(
                is_valid=False,
                error_code="invalid_condition",
                error_message="Не удалось распознать condition.",
                error_hint="Допустимые значения: ORIGINAL, OEM, AFTERMARKET, REMAN.",
            )

        parsed_price_exw = self._parse_decimal(price_exw_raw or price_raw)
        parsed_price_fob_sea = self._parse_decimal(price_fob_sea_raw)
        parsed_price_fob_air = self._parse_decimal(price_fob_air_raw)
        parsed_price = parsed_price_exw or parsed_price_fob_sea or parsed_price_fob_air
        if parsed_price is None:
            return ValidationResult(
                is_valid=False,
                error_code="invalid_price_format",
                error_message="Некорректный формат цены.",
                error_hint="Используйте число, например 1250.50.",
            )
        for label, parsed in (
            ("EXW", parsed_price_exw),
            ("FOB_SEA", parsed_price_fob_sea),
            ("FOB_AIR", parsed_price_fob_air),
        ):
            if parsed is not None and parsed <= 0:
                return ValidationResult(
                    is_valid=False,
                    error_code="invalid_price_value",
                    error_message=f"Цена {label} должна быть больше 0.",
                    error_hint="Укажите цену > 0.",
                )

        parsed_quantity: int | None = None
        if quantity_raw:
            try:
                parsed_quantity = int(float(quantity_raw.replace(",", ".")))
            except ValueError:
                return ValidationResult(
                    is_valid=False,
                    error_code="invalid_quantity_format",
                    error_message=f"Некорректное количество: {quantity_raw}.",
                    error_hint="Укажите целое число, например 10.",
                )
            if parsed_quantity < 0:
                return ValidationResult(
                    is_valid=False,
                    error_code="invalid_quantity_value",
                    error_message=f"Количество не может быть отрицательным: {quantity_raw}.",
                    error_hint="Укажите количество 0 или больше.",
                )

        parsed_weight, weight_error = self._parse_non_negative_decimal((extracted.get("weight") or "").strip(), "Weight")
        if weight_error:
            return weight_error
        parsed_length, length_error = self._parse_non_negative_decimal((extracted.get("length") or "").strip(), "Length")
        if length_error:
            return length_error
        parsed_width, width_error = self._parse_non_negative_decimal((extracted.get("width") or "").strip(), "Width")
        if width_error:
            return width_error
        parsed_height, height_error = self._parse_non_negative_decimal((extracted.get("height") or "").strip(), "Height")
        if height_error:
            return height_error

        return ValidationResult(
            is_valid=True,
            parsed_price=parsed_price.quantize(Decimal("0.01")),
            parsed_quantity=parsed_quantity,
            parsed_condition=condition,
            parsed_price_exw=parsed_price_exw.quantize(Decimal("0.01")) if parsed_price_exw is not None else None,
            parsed_price_fob_sea=parsed_price_fob_sea.quantize(Decimal("0.01")) if parsed_price_fob_sea is not None else None,
            parsed_price_fob_air=parsed_price_fob_air.quantize(Decimal("0.01")) if parsed_price_fob_air is not None else None,
            parsed_weight=parsed_weight,
            parsed_length=parsed_length,
            parsed_width=parsed_width,
            parsed_height=parsed_height,
        )


@dataclass
class ImportProcessingSummary:
    total_rows: int
    valid_rows: int
    error_rows: int
    created_products: int
    updated_offers: int


@dataclass
class MatchResult:
    status: str
    product: Product | None = None
    matched_by: str = "part_number"


class ProductMatcher:
    STATUS_MATCHED = "matched"
    STATUS_NO_MATCH = "no_match"
    STATUS_AMBIGUOUS = "ambiguous_match"

    def match(self, normalized_part_number: str, normalized_brand: str, normalized_cross_number: str = "") -> MatchResult:
        if not normalized_part_number:
            return MatchResult(status=self.STATUS_NO_MATCH, product=None)

        product_qs = Product.objects.all()
        if normalized_brand:
            product_qs = product_qs.filter(brand_normalized=normalized_brand)

        product_candidates = list(product_qs.filter(oem_normalized=normalized_part_number).order_by("id")[:2])
        if not normalized_brand and len(product_candidates) > 1:
            return MatchResult(status=self.STATUS_AMBIGUOUS, product=None, matched_by="part_number")
        product = product_candidates[0] if product_candidates else None
        if not product and normalized_brand:
            product = product_qs.filter(normalized_part_number=normalized_part_number).first()
        if product:
            return MatchResult(status=self.STATUS_MATCHED, product=product, matched_by="part_number")

        if normalized_cross_number:
            cross_qs = ProductCrossReference.objects.filter(normalized_cross_number=normalized_cross_number).select_related("product")
            if normalized_brand:
                cross_qs = cross_qs.filter(product__brand_normalized=normalized_brand)
            candidates = list(cross_qs[:2])
            if len(candidates) == 1:
                return MatchResult(status=self.STATUS_MATCHED, product=candidates[0].product, matched_by="cross_number")
            if len(candidates) > 1:
                return MatchResult(status=self.STATUS_AMBIGUOUS, product=None, matched_by="cross_number")

        return MatchResult(status=self.STATUS_NO_MATCH, product=None, matched_by="part_number")


class CatalogUpsertService:
    def upsert_offer(
        self,
        *,
        supplier,
        product: Product,
        condition: str,
        price: Decimal,
        price_exw: Decimal | None,
        price_fob_sea: Decimal | None,
        price_fob_air: Decimal | None,
        quantity: int | None,
        warehouse_address: str,
        sea_port: str,
        air_port: str,
        weight: Decimal | None,
        length: Decimal | None,
        width: Decimal | None,
        height: Decimal | None,
        import_job: ImportJob,
    ) -> tuple[SupplierOffer, bool, int]:
        offer, created = SupplierOffer.objects.get_or_create(
            supplier=supplier,
            product=product,
            condition=condition,
            defaults={
                "price": price,
                "quantity": quantity,
                "warehouse_address": warehouse_address,
                "sea_port": sea_port,
                "air_port": air_port,
                "weight": weight,
                "length": length,
                "width": width,
                "height": height,
                "last_import_job": import_job,
                "last_synced_at": timezone.now(),
                "status": SupplierOffer.Status.ACTIVE,
            },
        )
        if not created:
            offer.price = price
            offer.quantity = quantity
            offer.warehouse_address = warehouse_address
            offer.sea_port = sea_port
            offer.air_port = air_port
            offer.weight = weight
            offer.length = length
            offer.width = width
            offer.height = height
            offer.last_import_job = import_job
            offer.last_synced_at = timezone.now()
            offer.status = SupplierOffer.Status.ACTIVE
            offer.save(
                update_fields=[
                    "price",
                    "quantity",
                    "warehouse_address",
                    "sea_port",
                    "air_port",
                    "weight",
                    "length",
                    "width",
                    "height",
                    "last_import_job",
                    "last_synced_at",
                    "status",
                    "updated_at",
                ]
            )

        updated_prices = 0
        for incoterm, mode, value in (
            (SupplierOfferPrice.IncotermBasis.EXW, SupplierOfferPrice.TransportMode.NONE, price_exw),
            (SupplierOfferPrice.IncotermBasis.FOB, SupplierOfferPrice.TransportMode.SEA, price_fob_sea),
            (SupplierOfferPrice.IncotermBasis.FOB, SupplierOfferPrice.TransportMode.AIR, price_fob_air),
        ):
            if value is None:
                continue
            _, price_created = SupplierOfferPrice.objects.update_or_create(
                supplier_offer=offer,
                incoterm_basis=incoterm,
                transport_mode=mode,
                defaults={
                    "price": value,
                    "currency": offer.currency,
                    "source_import_run": import_job,
                },
            )
            if not price_created:
                updated_prices += 1

        return offer, created, updated_prices


class ErrorReportBuilder:
    OEM_ALIASES = {"oem", "part number", "partnumber", "sku", "article"}
    PRICE_ALIASES = {"price", "unitprice", "unit price", "price exw", "price fob sea", "price fob air"}
    BRAND_ALIASES = {"brand", "make", "manufacturer"}
    QUANTITY_ALIASES = {"quantity", "qty", "stock"}
    CROSS_NUMBER_ALIASES = {"crossnumber", "cross number", "cross_number", "analog", "cross"}

    @staticmethod
    def _extract_input_value(raw_payload: dict[str, str], aliases: set[str]) -> str:
        for key, value in raw_payload.items():
            normalized_key = _normalize_header(key)
            if normalized_key in aliases:
                return str(value or "").strip()
        return ""

    def build_for_job(self, job: ImportJob) -> ImportErrorReport | None:
        invalid_rows = list(ImportRow.objects.filter(job=job, status=ImportRow.Status.INVALID).order_by("row_no"))
        if not invalid_rows:
            return None

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "row_no",
                "error_code",
                "error_message",
                "error_hint",
                "input_oem",
                "input_brand",
                "input_cross_number",
                "input_price",
                "input_quantity",
            ]
        )
        for row in invalid_rows:
            raw = row.raw_payload or {}
            writer.writerow(
                [
                    row.row_no,
                    row.error_code,
                    row.error_message,
                    row.error_hint,
                    self._extract_input_value(raw, self.OEM_ALIASES),
                    self._extract_input_value(raw, self.BRAND_ALIASES),
                    self._extract_input_value(raw, self.CROSS_NUMBER_ALIASES),
                    self._extract_input_value(raw, self.PRICE_ALIASES),
                    self._extract_input_value(raw, self.QUANTITY_ALIASES),
                ]
            )

        content = buffer.getvalue().encode("utf-8")
        generated = store_generated_file_bytes(
            content=content,
            original_name=f"import_errors_{job.id}.csv",
            content_type="text/csv",
            prefix="imports/error-reports",
        )
        stored_file = StoredFile.objects.create(
            supplier=job.supplier,
            source_type=StoredFile.SourceType.IMPORT_ERROR_REPORT,
            storage_key=generated.storage_key,
            original_name=generated.original_name,
            content_type=generated.content_type,
            size_bytes=generated.size_bytes,
            checksum_sha256=generated.checksum_sha256,
        )
        report, _ = ImportErrorReport.objects.update_or_create(
            job=job,
            defaults={
                "file": stored_file,
                "report_format": ImportErrorReport.ReportFormat.CSV,
                "error_count": len(invalid_rows),
            },
        )
        return report


class ImportRowPipeline:
    def __init__(
        self,
        parser: ImportParser | None = None,
        validator: ImportValidator | None = None,
        normalizer: OEMNormalizer | None = None,
        matcher: ProductMatcher | None = None,
        upsert_service: CatalogUpsertService | None = None,
    ):
        self.parser = parser or ImportParser()
        self.validator = validator or ImportValidator()
        self.normalizer = normalizer or OEMNormalizer()
        self.matcher = matcher or ProductMatcher()
        self.upsert_service = upsert_service or CatalogUpsertService()

    @staticmethod
    def _mark_duplicate_rows(job: ImportJob) -> None:
        grouped: dict[str, list[ImportRow]] = {}
        for row in (
            ImportRow.objects.filter(job=job, status=ImportRow.Status.VALID)
            .exclude(part_number_normalized="")
            .order_by("row_no")
        ):
            key = f"{row.part_number_normalized}::{row.normalized_brand}::{(row.normalized_payload or {}).get('condition', '')}"
            grouped.setdefault(key, []).append(row)
        for rows in grouped.values():
            if len(rows) <= 1:
                continue
            last_row_no = rows[-1].row_no
            for duplicate in rows[:-1]:
                duplicate.status = ImportRow.Status.INVALID
                duplicate.validation_status = ImportRow.ValidationStatus.INVALID
                duplicate.match_status = ImportRow.MatchStatus.FAILED
                duplicate.error_code = "duplicate_in_file"
                duplicate.error_message = "Дублирующий OEM в текущем файле."
                duplicate.error_hint = f"Использована последняя строка с этим OEM: {last_row_no}."
                duplicate.save(
                    update_fields=[
                        "status",
                        "validation_status",
                        "match_status",
                        "error_code",
                        "error_message",
                        "error_hint",
                        "updated_at",
                    ]
                )

    @transaction.atomic
    def process_job(self, job: ImportJob) -> ImportProcessingSummary:
        if job.source_type != ImportJob.SourceType.CSV:
            raise ValueError("Сейчас поддерживается только CSV источник.")
        if not job.source_file_id:
            raise ValueError("Для CSV импорта отсутствует source_file.")

        rows = self.parser.parse_csv_rows(job.source_file.storage_key)
        total_rows = 0

        job.status = ImportJob.Status.PROCESSING
        job.started_at = timezone.now()
        job.error_message = ""
        job.finished_at = None
        job.save(update_fields=["status", "started_at", "finished_at", "error_message", "updated_at"])

        for row_no, raw_row in rows:
            total_rows += 1
            import_row = ImportRow.objects.create(
                job=job,
                row_no=row_no,
                raw_payload=raw_row,
                status=ImportRow.Status.PENDING,
            )
            extracted = self.parser.extract_fields(raw_row, column_mapping=job.column_mapping_json or None)
            validation = self.validator.validate(extracted)

            import_row.normalized_oem = self.normalizer.normalize_oem(extracted.get("oem"))
            import_row.normalized_brand = self.normalizer.normalize_brand(extracted.get("brand"))
            import_row.part_number_raw = (extracted.get("oem") or "").strip()
            import_row.part_number_normalized = import_row.normalized_oem
            import_row.cross_number_raw = (extracted.get("cross_number") or "").strip()
            import_row.cross_number_normalized = self.normalizer.normalize_oem(extracted.get("cross_number"))
            import_row.row_number = row_no
            import_row.parsed_name = (extracted.get("name") or "").strip()
            import_row.normalized_payload = {
                "condition": validation.parsed_condition if validation.is_valid else self.normalizer.normalize_condition(extracted.get("condition")),
                "warehouse_address": (extracted.get("warehouse_address") or "").strip(),
                "sea_port": (extracted.get("sea_port") or "").strip(),
                "air_port": (extracted.get("air_port") or "").strip(),
            }

            if validation.is_valid:
                import_row.status = ImportRow.Status.VALID
                import_row.validation_status = ImportRow.ValidationStatus.VALID
                import_row.match_status = ImportRow.MatchStatus.NOT_PROCESSED
                import_row.parsed_price = validation.parsed_price
                import_row.parsed_quantity = validation.parsed_quantity
                import_row.error_code = ""
                import_row.error_message = ""
                import_row.error_hint = ""
            else:
                import_row.status = ImportRow.Status.INVALID
                import_row.validation_status = ImportRow.ValidationStatus.INVALID
                import_row.match_status = ImportRow.MatchStatus.FAILED
                import_row.error_code = validation.error_code
                import_row.error_message = validation.error_message
                import_row.error_hint = validation.error_hint

            import_row.save(
                update_fields=[
                    "normalized_oem",
                    "normalized_brand",
                    "part_number_raw",
                    "part_number_normalized",
                    "cross_number_raw",
                    "cross_number_normalized",
                    "row_number",
                    "parsed_name",
                    "normalized_payload",
                    "parsed_price",
                    "parsed_quantity",
                    "status",
                    "validation_status",
                    "match_status",
                    "error_code",
                    "error_message",
                    "error_hint",
                    "updated_at",
                ]
            )

        self._mark_duplicate_rows(job)

        created_products = 0
        updated_offers = 0
        updated_prices = 0
        created_offers = 0
        matched_by_cross = 0
        upserted_rows = 0

        for row in ImportRow.objects.filter(job=job, status=ImportRow.Status.VALID).order_by("row_no"):
            match = self.matcher.match(row.part_number_normalized, row.normalized_brand, row.cross_number_normalized)
            if match.status == ProductMatcher.STATUS_AMBIGUOUS:
                row.status = ImportRow.Status.INVALID
                row.validation_status = ImportRow.ValidationStatus.INVALID
                row.match_status = ImportRow.MatchStatus.AMBIGUOUS
                row.error_code = "ambiguous_match"
                row.error_message = "Найдено несколько товаров по OEM без бренда."
                row.error_hint = "Укажите бренд в файле для однозначного сопоставления."
                row.save(
                    update_fields=[
                        "status",
                        "validation_status",
                        "match_status",
                        "error_code",
                        "error_message",
                        "error_hint",
                        "updated_at",
                    ]
                )
                continue

            product = match.product
            if product is None:
                product = Product.objects.create(
                    oem_raw=row.part_number_raw,
                    oem_normalized=row.part_number_normalized,
                    part_number=row.part_number_raw,
                    normalized_part_number=row.part_number_normalized,
                    brand_raw=row.normalized_brand,
                    brand_normalized=row.normalized_brand,
                    name=row.parsed_name or f"Part {row.part_number_normalized}",
                    created_by_supplier=job.supplier,
                )
                created_products += 1
                row.match_status = ImportRow.MatchStatus.CREATED_NEW_PRODUCT
            else:
                row.match_status = (
                    ImportRow.MatchStatus.MATCHED_BY_CROSS if match.matched_by == "cross_number" else ImportRow.MatchStatus.MATCHED
                )
                if match.matched_by == "cross_number":
                    matched_by_cross += 1

            if row.cross_number_normalized and row.cross_number_normalized != row.part_number_normalized:
                ProductCrossReference.objects.get_or_create(
                    product=product,
                    normalized_cross_number=row.cross_number_normalized,
                    defaults={
                        "cross_number": row.cross_number_raw or row.cross_number_normalized,
                        "cross_type": ProductCrossReference.CrossType.ANALOG,
                        "source": ProductCrossReference.Source.IMPORT,
                    },
                )

            condition = (row.normalized_payload or {}).get("condition") or SupplierOffer.Condition.OEM
            price_exw = self.validator._parse_decimal(self.parser.extract_fields(row.raw_payload).get("price_exw", ""))
            price_fob_sea = self.validator._parse_decimal(self.parser.extract_fields(row.raw_payload).get("price_fob_sea", ""))
            price_fob_air = self.validator._parse_decimal(self.parser.extract_fields(row.raw_payload).get("price_fob_air", ""))
            if price_exw is not None:
                price_exw = price_exw.quantize(Decimal("0.01"))
            if price_fob_sea is not None:
                price_fob_sea = price_fob_sea.quantize(Decimal("0.01"))
            if price_fob_air is not None:
                price_fob_air = price_fob_air.quantize(Decimal("0.01"))

            offer, created_offer, row_updated_prices = self.upsert_service.upsert_offer(
                supplier=job.supplier,
                product=product,
                condition=condition,
                price=row.parsed_price or Decimal("0.00"),
                price_exw=price_exw,
                price_fob_sea=price_fob_sea,
                price_fob_air=price_fob_air,
                quantity=row.parsed_quantity,
                warehouse_address=(row.raw_payload.get("WarehouseAddress") or row.raw_payload.get("warehouse_address") or "").strip(),
                sea_port=(row.raw_payload.get("SeaPort") or row.raw_payload.get("sea_port") or "").strip(),
                air_port=(row.raw_payload.get("AirPort") or row.raw_payload.get("air_port") or "").strip(),
                weight=self.validator._parse_decimal((row.raw_payload.get("Weight") or row.raw_payload.get("weight") or "").strip()),
                length=self.validator._parse_decimal((row.raw_payload.get("Length") or row.raw_payload.get("length") or "").strip()),
                width=self.validator._parse_decimal((row.raw_payload.get("Width") or row.raw_payload.get("width") or "").strip()),
                height=self.validator._parse_decimal((row.raw_payload.get("Height") or row.raw_payload.get("height") or "").strip()),
                import_job=job,
            )
            if not created_offer:
                updated_offers += 1
            else:
                created_offers += 1
            updated_prices += row_updated_prices

            row.product = product
            row.supplier_offer = offer
            row.matched_product = product
            row.matched_supplier_offer = offer
            row.status = ImportRow.Status.UPSERTED
            row.validation_status = ImportRow.ValidationStatus.VALID
            row.matched_by = match.matched_by
            row.error_code = ""
            row.error_message = ""
            row.error_hint = ""
            row.save(
                update_fields=[
                    "product",
                    "supplier_offer",
                    "matched_product",
                    "matched_supplier_offer",
                    "status",
                    "validation_status",
                    "match_status",
                    "matched_by",
                    "error_code",
                    "error_message",
                    "error_hint",
                    "updated_at",
                ]
            )
            upserted_rows += 1

        error_rows = ImportRow.objects.filter(job=job, status=ImportRow.Status.INVALID).count()
        valid_rows = upserted_rows

        job.total_rows = total_rows
        job.rows_total = total_rows
        job.processed_rows = upserted_rows + error_rows
        job.valid_rows = valid_rows
        job.error_rows = error_rows
        job.created_products = created_products
        job.updated_offers = updated_offers
        job.rows_created_products = created_products
        job.rows_created_offers = created_offers
        job.rows_updated_offers = updated_offers
        job.rows_updated_prices = updated_prices
        job.rows_failed = error_rows
        job.rows_matched_by_cross_number = matched_by_cross
        job.created_count = created_products
        job.updated_count = updated_offers
        job.error_count = error_rows
        job.status = ImportJob.Status.COMPLETED if error_rows == 0 else ImportJob.Status.PARTIAL_SUCCESS
        job.finished_at = timezone.now()
        job.summary_json = {
            "rows_total": total_rows,
            "rows_created_products": created_products,
            "rows_created_offers": created_offers,
            "rows_updated_offers": updated_offers,
            "rows_updated_prices": updated_prices,
            "rows_failed": error_rows,
            "rows_matched_by_cross_number": matched_by_cross,
        }
        job.save(
            update_fields=[
                "total_rows",
                "rows_total",
                "processed_rows",
                "valid_rows",
                "error_rows",
                "created_products",
                "updated_offers",
                "rows_created_products",
                "rows_created_offers",
                "rows_updated_offers",
                "rows_updated_prices",
                "rows_failed",
                "rows_matched_by_cross_number",
                "created_count",
                "updated_count",
                "error_count",
                "status",
                "finished_at",
                "summary_json",
                "updated_at",
            ]
        )

        return ImportProcessingSummary(
            total_rows=total_rows,
            valid_rows=valid_rows,
            error_rows=error_rows,
            created_products=created_products,
            updated_offers=updated_offers,
        )
