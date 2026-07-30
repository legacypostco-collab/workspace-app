"""
Загружает справочник HS-кодов из CSV в модель HSCode.

hs_code на Part заполняется ТОЛЬКО таможенным брокером вручную (hs_verified=True).
Авто-простановка намеренно отключена — точность <100% неприемлема.

Запуск:
    python manage.py load_hs_codes   # загрузить/обновить справочник HSCode
"""
from __future__ import annotations

import csv
import re
import tempfile
from pathlib import Path
from urllib.request import Request

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from marketplace.models import HSCode, Part

CSV_URL = "https://raw.githubusercontent.com/datasets/harmonized-system/master/data/harmonized-system.csv"
BATCH = 5000
MAX_CSV_BYTES = 50 * 1024 * 1024

# ── Правила автоклассификации ─────────────────────────────────────────────────
# Порядок важен: первое сработавшее правило побеждает.
# Каждое правило: (regex по title+brand, hs_code 4-знака)

RULES: list[tuple[re.Pattern, str]] = [r for r in [
    # Подшипники
    (re.compile(r'\bbearing\b', re.I),                          "8482"),
    # Уплотнения, прокладки, сальники
    (re.compile(r'\b(seal|gasket|o.?ring|simmering)\b', re.I), "8484"),
    # Фильтры (масляные, воздушные, топливные, гидравлические)
    (re.compile(r'\bfilter\b', re.I),                           "8421"),
    # Ремни приводные
    (re.compile(r'\b(belt|v.?belt|drive\s+belt)\b', re.I),     "4010"),
    # Гидравлические насосы, цилиндры, клапаны
    (re.compile(r'\b(hydraulic\s+(pump|cylinder|motor|valve)|hydrostatic)\b', re.I), "8413"),
    # Насосы прочие (водяные, масляные)
    (re.compile(r'\b(water\s+pump|oil\s+pump|pump\b)', re.I),  "8413"),
    # Компрессоры и запчасти к ним
    (re.compile(r'\bcompressor\b', re.I),                       "8414"),
    # Детали двигателей внутреннего сгорания
    (re.compile(r'\b(piston|crankshaft|camshaft|cylinder\s+(head|liner|block)|connecting\s+rod|valve\s+(spring|seat|guide)|injector|fuel\s+(pump|injection)|turbo(charger)?|intercooler|rocker|timing)\b', re.I), "8409"),
    # Трансмиссия: КПП, дифференциалы, ведущие мосты
    (re.compile(r'\b(gearbox|transmission|differential|axle\s+shaft|torque\s+converter|clutch\s+(disc|plate|kit|cover))\b', re.I), "8483"),
    # Тормозные системы
    (re.compile(r'\b(brake\s+(disc|drum|pad|lining|caliper|shoe|master\s+cylinder)|abs\s+sensor)\b', re.I), "8708"),
    # Электрооборудование: генераторы, стартеры, свечи, датчики
    (re.compile(r'\b(alternator|starter|spark\s+plug|glow\s+plug|ignition|sensor\s|harness|wiring|ecu|solenoid)\b', re.I), "8511"),
    # Электрические жгуты, провода
    (re.compile(r'\b(wire|cable|harness|loom)\b', re.I),       "8544"),
    # Радиаторы, теплообменники, маслоохладители
    (re.compile(r'\b(radiator|heat\s+exchanger|oil\s+cooler|intercooler|cooler\b)\b', re.I), "8419"),
    # Шины и резиновые гусеницы
    (re.compile(r'\b(tyre|tire|rubber\s+track)\b', re.I),      "4011"),
    # Стёкла кабины
    (re.compile(r'\b(glass|windshield|windscreen)\b', re.I),   "7007"),
    # Ковши, зубья, режущие кромки (землеройная техника)
    (re.compile(r'\b(bucket|tooth|cutting\s+edge|blade\s+tip|ground\s+engaging)\b', re.I), "8431"),
    # Запчасти к строительной и горнодобывающей технике (CAT/Komatsu/Volvo/JD экскаваторы, бульдозеры)
    (re.compile(r'\b(track\s+(chain|shoe|link|roller|idler|sprocket)|undercarriage|dozer|excavator|loader\s+bucket|boom|stick\s+arm)\b', re.I), "8431"),
    # Подъёмные краны, погрузчики — запчасти
    (re.compile(r'\b(forklift|reach\s+truck|mast|tilt\s+cylinder|carriage)\b', re.I), "8431"),
    # С/х техника — запчасти (комбайны, тракторы)
    (re.compile(r'\b(combine|harvester|thresher|header|grain\s+elevator|straw\s+walker|concave)\b', re.I), "8433"),
    # Трансмиссионные валы, шестерни, зубчатые передачи
    (re.compile(r'\b(gear\b|sprocket|chain\s+drive|shaft\b|pinion|rack\b|worm\s+gear)\b', re.I), "8483"),
    # Болты, гайки, крепёж (стальные)
    (re.compile(r'\b(bolt\b|nut\b|screw\b|washer\b|fastener)\b', re.I),           "7318"),
    # Топливные баки, радиаторные баки
    (re.compile(r'\b(fuel\s+tank|oil\s+tank|tank\b)\b', re.I),                    "8708"),
    # Шланги, трубопроводы
    (re.compile(r'\b(hose\b|pipe\b|tube\b|tubing)\b', re.I),                      "8484"),
    # Муфты сцепления (общие)
    (re.compile(r'\bclutch\b', re.I),                                              "8483"),
    # Диски тормозные/сцепления
    (re.compile(r'\b(disc|disk)\b', re.I),                                         "8483"),
    # Переключатели, клапаны управления
    (re.compile(r'\b(switch\b|valve\b|regulator\b|control\s+valve)\b', re.I),     "8543"),
    # Двигатели электрические, моторы
    (re.compile(r'\b(electric\s+motor|motor\s+\w+|fan\s+motor|wiper\s+motor)\b', re.I), "8501"),
    # Вентиляторы, крыльчатки
    (re.compile(r'\b(fan\b|impeller|cooling\s+fan)\b', re.I),                     "8414"),
    # Колёса, ступицы, тормозные барабаны
    (re.compile(r'\b(wheel\b|hub\b|rim\b|brake\s+drum)\b', re.I),                 "8708"),
    # Кабины и части кузова
    (re.compile(r'\b(cab\b|cabin\b|door\b|hood\b|bonnet\b|fender\b|panel\b)\b', re.I), "8708"),
    # Двигатели ДВС в сборе
    (re.compile(r'\b(engine\b|motor\s+engine)\b', re.I),                           "8408"),
    # Общие запчасти к машинам глав 84-85 (fallback для механических деталей)
    (re.compile(r'\b(part|assembly|kit|repair|overhaul|rebuild|plate\b|bracket\b|mount\b|support\b)\b', re.I), "8431"),
]]


def classify(title: str, brand_name: str | None) -> str | None:
    """Вернуть 4-значный HS-код или None."""
    text = f"{title} {brand_name or ''}".strip()
    for pattern, code in RULES:
        if pattern.search(text):
            return code
    return None


class Command(BaseCommand):
    help = "Загрузить справочник HSCode (hs_code на Part — только брокер вручную)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            help="Локальный CSV вместо загрузки официального справочника.",
        )

    def handle(self, *args, **options):
        local_path = options.get("csv")
        if local_path:
            csv_path = Path(local_path).expanduser().resolve()
            if not csv_path.is_file():
                raise CommandError(f"CSV-файл не найден: {csv_path}")
            if csv_path.stat().st_size > MAX_CSV_BYTES:
                raise CommandError("CSV-файл превышает допустимый размер 50 МБ.")
            self._load_csv(csv_path)
            return

        self.stdout.write("Скачиваем HS CSV...")
        from assistant.security import urlopen_no_redirect

        request = Request(CSV_URL, headers={"User-Agent": "ConsolidatorParts/1.0"})
        with tempfile.NamedTemporaryFile("w+b", suffix=".csv") as temp_file:
            with urlopen_no_redirect(request, timeout=30) as response:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    if temp_file.tell() + len(chunk) > MAX_CSV_BYTES:
                        raise CommandError("Удаленный CSV превышает допустимый размер 50 МБ.")
                    temp_file.write(chunk)
            temp_file.flush()
            self._load_csv(Path(temp_file.name))

    # ── Загрузка CSV ──────────────────────────────────────────────────────────

    def _load_csv(self, csv_path: Path):
        existing = set(HSCode.objects.values_list("hscode", flat=True))
        to_create: list[HSCode] = []

        # Сначала — только уровень 2 и 4 (без parent-зависимости)
        rows_6: list[dict] = []
        with csv_path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                level = int(row["level"])
                hscode = row["hscode"].strip()
                if hscode in existing:
                    continue
                if level in (2, 4):
                    to_create.append(HSCode(
                        section=row["section"],
                        hscode=hscode,
                        description=row["description"][:500],
                        level=level,
                    ))
                elif level == 6:
                    rows_6.append(row)

        with transaction.atomic():
            HSCode.objects.bulk_create(to_create, ignore_conflicts=True)

        # Теперь уровень 6 — parent уже есть
        to_create_6: list[HSCode] = []
        parent_map = {h.hscode: h for h in HSCode.objects.filter(level=4)}
        for row in rows_6:
            hscode = row["hscode"].strip()
            if hscode in existing:
                continue
            parent = parent_map.get(row["parent"].strip())
            to_create_6.append(HSCode(
                section=row["section"],
                hscode=hscode,
                description=row["description"][:500],
                parent=parent,
                level=6,
            ))

        with transaction.atomic():
            HSCode.objects.bulk_create(to_create_6, ignore_conflicts=True)

        total = HSCode.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f"✓ HS-коды загружены: {total} записей "
            f"(2-знак: {HSCode.objects.filter(level=2).count()}, "
            f"4-знак: {HSCode.objects.filter(level=4).count()}, "
            f"6-знак: {HSCode.objects.filter(level=6).count()})"
        ))

    # ── Автоклассификация ─────────────────────────────────────────────────────

    def _assign_all(self, only_empty: bool):
        qs = Part.objects.select_related("brand").only(
            "id", "title", "hs_code", "brand__name"
        )
        if only_empty:
            qs = qs.filter(hs_code="")

        total = qs.count()
        self.stdout.write(f"Классифицируем {total:,} позиций...")

        batch, assigned, skipped = [], 0, 0
        for p in qs.iterator(chunk_size=BATCH):
            code = classify(p.title, getattr(p.brand, "name", None))
            if code:
                p.hs_code = code
                batch.append(p)
                assigned += 1
            else:
                skipped += 1

            if len(batch) >= BATCH:
                Part.objects.bulk_update(batch, ["hs_code"])
                batch = []
                self.stdout.write(f"  ... {assigned:,} присвоено")

        if batch:
            Part.objects.bulk_update(batch, ["hs_code"])

        pct = 100 * assigned // total if total else 0
        self.stdout.write(self.style.SUCCESS(
            f"✓ Присвоено: {assigned:,} ({pct}%), без кода: {skipped:,}"
        ))

        # Статистика по кодам
        self.stdout.write("\nТоп HS-кодов:")
        from django.db.models import Count
        for row in (Part.objects.exclude(hs_code="")
                    .values("hs_code").annotate(n=Count("id"))
                    .order_by("-n")[:15]):
            hs = HSCode.objects.filter(hscode=row["hs_code"]).first()
            desc = hs.description[:50] if hs else "?"
            self.stdout.write(f"  {row['hs_code']}  {row['n']:>8,}  {desc}")
