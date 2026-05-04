from django.core.management.base import BaseCommand

from assistant.indexer import index_all_orders, index_all_parts, index_all_rfqs, index_faq


class Command(BaseCommand):
    help = "Full reindex of all sources (parts, orders, RFQs, FAQ)"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None,
                            help="Cap per source (useful in dev)")
        parser.add_argument("--skip-parts", action="store_true")
        parser.add_argument("--skip-orders", action="store_true")
        parser.add_argument("--skip-rfqs", action="store_true")
        parser.add_argument("--skip-faq", action="store_true")

    def handle(self, *args, **opts):
        if not opts["skip_faq"]:
            n = index_faq()
            self.stdout.write(self.style.SUCCESS(f"  ✓ FAQ: {n}"))
        if not opts["skip_parts"]:
            n = index_all_parts(limit=opts["limit"])
            self.stdout.write(self.style.SUCCESS(f"  ✓ Parts: {n}"))
        if not opts["skip_orders"]:
            n = index_all_orders(limit=opts["limit"])
            self.stdout.write(self.style.SUCCESS(f"  ✓ Orders: {n}"))
        if not opts["skip_rfqs"]:
            n = index_all_rfqs(limit=opts["limit"])
            self.stdout.write(self.style.SUCCESS(f"  ✓ RFQs: {n}"))
        self.stdout.write(self.style.SUCCESS("Done."))
