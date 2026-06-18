"""PERF: GIN-trigram индексы для быстрого icontains-поиска по каталогу (916K).

Без них `search_parts` делает seq-scan всей таблицы Part на каждый свободный
текстовый запрос (~1.3с). pg_trgm + GIN на полях поиска → icontains идёт по
индексу (BitmapOr нескольких GIN-сканов), миллисекунды вместо секунд.

БЕЗОПАСНОСТЬ ВЫКАТА:
- vendor-guard: на SQLite (локалка) — no-op (там объём мал, индекс не нужен);
- CREATE INDEX CONCURRENTLY: НЕ лочит таблицу → нет простоя на живом проде;
- atomic=False: обязательно для CONCURRENTLY (вне транзакции);
- IF NOT EXISTS: идемпотентно, безопасно к повтору.
"""
from django.db import migrations

TRGM_FIELDS = ["title", "title_ru", "oem_number", "cross_numbers"]


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    table = apps.get_model("marketplace", "Part")._meta.db_table
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for f in TRGM_FIELDS:
        schema_editor.execute(
            f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{table}_{f}_trgm" '
            f'ON "{table}" USING gin ("{f}" gin_trgm_ops)'
        )


def drop_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    table = apps.get_model("marketplace", "Part")._meta.db_table
    for f in TRGM_FIELDS:
        schema_editor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{table}_{f}_trgm"')


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("marketplace", "0090_deactivate_negative_price_parts")]
    operations = [migrations.RunPython(create_indexes, drop_indexes)]
