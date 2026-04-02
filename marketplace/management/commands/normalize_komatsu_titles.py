import re

from django.core.management.base import BaseCommand
from django.db import transaction

from marketplace.models import Part


CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ORIGINAL_CN_RE = re.compile(r"Original CN:\s*(.+?)\.")

REPLACEMENTS = [
    ("软管", "hose"),
    ("螺栓", "bolt"),
    ("螺母", "nut"),
    ("垫圈", "washer"),
    ("接头", "connector"),
    ("密封", "seal"),
    ("密封圈", "seal ring"),
    ("滤芯", "filter element"),
    ("过滤器", "filter"),
    ("轴承", "bearing"),
    ("衬套", "bushing"),
    ("销", "pin"),
    ("管夹", "pipe clamp"),
    ("阀", "valve"),
    ("传感器", "sensor"),
    ("开关", "switch"),
    ("皮带", "belt"),
    ("齿轮", "gear"),
    ("油封", "oil seal"),
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

DIRECT_MAP = {
    "MBOLT": "Komatsu Bolt",
    "M BOLT": "Komatsu Bolt",
    "BOLT": "Komatsu Bolt",
    "HOSE": "Komatsu Hose",
    "NUT": "Komatsu Nut",
    "WASHER": "Komatsu Washer",
    "VALVE": "Komatsu Valve",
    "SENSOR": "Komatsu Sensor",
    "SWITCH": "Komatsu Switch",
    "BEARING": "Komatsu Bearing",
    "SEAL": "Komatsu Seal",
}


def normalize_title(oem: str, title: str) -> str:
    text = (title or "").strip()
    if not text:
        return f"Komatsu Part {oem}"

    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("MBOLT", "BOLT").replace("M BOLT", "BOLT")
    for src, dst in REPLACEMENTS:
        text = text.replace(src, dst)

    # Drop any remaining CJK symbols from title and keep only readable EN/ASCII mix.
    text = CJK_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_/")

    if not text:
        return f"Komatsu Part {oem}"
    upper_text = text.upper()
    if upper_text in DIRECT_MAP:
        return DIRECT_MAP[upper_text]
    if not text.lower().startswith("komatsu"):
        text = f"Komatsu {text}"
    text = " ".join(word.capitalize() if word.lower() != "komatsu" else "Komatsu" for word in text.split())
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
            old_title = p.title or ""
            source_title = old_title
            if not CJK_RE.search(source_title):
                match = ORIGINAL_CN_RE.search(p.description or "")
                if match:
                    source_title = match.group(1).strip()
                else:
                    continue

            new_title = normalize_title(p.oem_number, source_title)
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
