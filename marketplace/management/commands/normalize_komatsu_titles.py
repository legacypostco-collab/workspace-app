import re

from django.core.management.base import BaseCommand
from django.db import transaction

from marketplace.models import Part


CJK_RE = re.compile(r"[\u4e00-\u9fff]")

REPLACEMENTS = [
    ("铲斗唇板护块", "bucket lip shroud"),
    ("横斗齿销", "tooth pin"),
    ("横销斗齿", "tooth pin"),
    ("支重轮", "track roller"),
    ("托链轮", "carrier roller"),
    ("岩石铲斗", "rock bucket"),
    ("岩石斗", "rock bucket"),
    ("普通斗", "general bucket"),
    ("带吊钩", "with hook"),
    ("右边齿", "right tooth"),
    ("左边齿", "left tooth"),
    ("中间齿", "center tooth"),
    ("销子", "pin"),
    ("卡特", "CAT"),
    ("日立", "Hitachi"),
    ("立方", "m3"),
]


def normalize_title(oem: str, title: str) -> str:
    text = (title or "").strip()
    if not text:
        return f"Komatsu Part {oem}"

    text = text.replace("（", "(").replace("）", ")")
    for src, dst in REPLACEMENTS:
        text = text.replace(src, dst)

    # Drop any remaining CJK symbols from title and keep only readable EN/ASCII mix.
    text = CJK_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_/")

    if not text:
        return f"Komatsu Part {oem}"
    if not text.lower().startswith("komatsu"):
        text = f"Komatsu {text}"
    return text[:255]


class Command(BaseCommand):
    help = "Normalize Komatsu titles from CN-heavy text to readable EN titles; keep original CN in description."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=1000)

    def handle(self, *args, **options):
        batch_size = int(options["batch_size"])
        qs = Part.objects.filter(brand__name="Komatsu").only("id", "oem_number", "title", "description")

        to_update = []
        changed = 0
        scanned = 0

        def flush():
            nonlocal changed
            if not to_update:
                return
            with transaction.atomic():
                Part.objects.bulk_update(to_update, ["title", "description"], batch_size=batch_size)
            changed += len(to_update)
            to_update.clear()

        for p in qs.iterator(chunk_size=5000):
            scanned += 1
            if not CJK_RE.search(p.title or ""):
                continue

            old_title = p.title or ""
            new_title = normalize_title(p.oem_number, old_title)
            if new_title == old_title:
                continue

            old_desc = (p.description or "").strip()
            marker = f"Original CN: {old_title}."
            if marker not in old_desc:
                p.description = f"{marker} {old_desc}".strip()
            p.title = new_title
            to_update.append(p)

            if len(to_update) >= batch_size:
                flush()

        flush()
        self.stdout.write(self.style.SUCCESS(f"Komatsu normalize done. Scanned: {scanned}, Updated: {changed}"))
