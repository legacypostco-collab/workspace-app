"""Create pgvector extension and ivfflat index — Postgres only.

On SQLite this migration is a no-op (RunSQL with elidable=True).
"""
from django.db import migrations


def _is_postgres(schema_editor):
    return schema_editor.connection.vendor == "postgresql"


def create_extension(apps, schema_editor):
    if _is_postgres(schema_editor):
        schema_editor.execute("CREATE EXTENSION IF NOT EXISTS vector;")


def drop_extension(apps, schema_editor):
    if _is_postgres(schema_editor):
        schema_editor.execute("DROP EXTENSION IF EXISTS vector;")


def create_index(apps, schema_editor):
    if _is_postgres(schema_editor):
        # Note: ivfflat requires data; skip if table empty
        schema_editor.execute("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM assistant_knowledgechunk LIMIT 1) THEN
                    CREATE INDEX IF NOT EXISTS assistant_knowledgechunk_embedding_idx
                    ON assistant_knowledgechunk
                    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
                END IF;
            END $$;
        """)


def drop_index(apps, schema_editor):
    if _is_postgres(schema_editor):
        schema_editor.execute("DROP INDEX IF EXISTS assistant_knowledgechunk_embedding_idx;")


class Migration(migrations.Migration):
    dependencies = [("assistant", "0001_initial")]
    operations = [
        migrations.RunPython(create_extension, drop_extension),
        migrations.RunPython(create_index, drop_index),
    ]
