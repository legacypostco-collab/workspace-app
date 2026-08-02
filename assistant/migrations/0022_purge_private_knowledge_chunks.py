from django.db import migrations


PRIVATE_SOURCE_TYPES = ("order", "rfq", "shipment", "document")


def purge_private_knowledge_chunks(apps, schema_editor):
    knowledge_chunk = apps.get_model("assistant", "KnowledgeChunk")
    knowledge_chunk.objects.filter(source_type__in=PRIVATE_SOURCE_TYPES).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("assistant", "0021_clean_internal_action_params"),
    ]

    operations = [
        migrations.RunPython(
            purge_private_knowledge_chunks,
            migrations.RunPython.noop,
        ),
    ]
