"""Пересоздать столбец embedding как vector(1536) — ТОЛЬКО Postgres.

jsonb→vector нельзя кастовать через ALTER COLUMN ... USING, поэтому
DROP COLUMN + ADD COLUMN. На SQLite (локалка/тесты) embedding — JSONField
(см. assistant.models._embedding_field), миграция здесь no-op.

Паттерн вендор-гейта — как в 0002_pgvector_setup. На проде применено
вручную (FAKED), здесь фиксируем канонический вариант для чистых сборок.
"""
from django.db import migrations


def _is_postgres(schema_editor):
    return schema_editor.connection.vendor == "postgresql"


def recreate_vector(apps, schema_editor):
    if _is_postgres(schema_editor):
        schema_editor.execute(
            "ALTER TABLE assistant_knowledgechunk DROP COLUMN IF EXISTS embedding")
        schema_editor.execute(
            "ALTER TABLE assistant_knowledgechunk ADD COLUMN embedding vector(1536)")


def drop_vector(apps, schema_editor):
    if _is_postgres(schema_editor):
        schema_editor.execute(
            "ALTER TABLE assistant_knowledgechunk DROP COLUMN IF EXISTS embedding")


class Migration(migrations.Migration):
    dependencies = [
        ("assistant", "0013_alter_projectdocument_doctype"),
    ]
    operations = [
        migrations.RunPython(recreate_vector, drop_vector),
    ]
