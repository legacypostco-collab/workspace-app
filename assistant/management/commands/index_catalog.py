from django.core.management.base import BaseCommand

from assistant.indexer import index_all_parts


class Command(BaseCommand):
    help = "Index all Part records into KnowledgeChunk for AI assistant"

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument("--limit", type=int, default=None)

    def handle(self, *args, **opts):
        self.stdout.write("Indexing catalog...")
        n = index_all_parts(batch_size=opts["batch_size"], limit=opts["limit"])
        self.stdout.write(self.style.SUCCESS(f"Indexed {n} parts"))
