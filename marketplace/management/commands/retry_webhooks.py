import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.management.base import BaseCommand

from marketplace.models import WebhookDeliveryLog


class Command(BaseCommand):
    help = "Retry failed webhook deliveries based on WebhookDeliveryLog."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100, help="Maximum failed logs to process")

    def handle(self, *args, **options):
        limit = max(1, int(options["limit"] or 100))
        max_attempts = max(1, int(getattr(settings, "WEBHOOK_RETRY_MAX_ATTEMPTS", 5) or 5))

        failed_logs = (
            WebhookDeliveryLog.objects.filter(success=False, attempt__lt=max_attempts)
            .order_by("created_at")[:limit]
        )
        total = failed_logs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("No failed webhooks to retry."))
            return

        secret = getattr(settings, "WEBHOOK_SECRET", "") or ""
        timeout = float(getattr(settings, "WEBHOOK_TIMEOUT_SEC", 2))
        ok_count = 0
        fail_count = 0

        for log in failed_logs:
            payload = log.request_payload or {}
            endpoint = log.endpoint
            attempt = int(log.attempt) + 1
            headers = {"Content-Type": "application/json"}
            if secret:
                headers["X-Webhook-Secret"] = secret
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            retry_log = WebhookDeliveryLog.objects.create(
                order_event=log.order_event,
                order=log.order,
                endpoint=endpoint,
                success=False,
                attempt=attempt,
                request_payload=payload,
            )
            try:
                req = Request(endpoint, data=body, headers=headers, method="POST")
                with urlopen(req, timeout=timeout) as resp:
                    status_code = int(getattr(resp, "status", 200))
                    response_body = resp.read().decode("utf-8", errors="ignore")[:4000]
                is_ok = 200 <= status_code < 300
                retry_log.success = is_ok
                retry_log.status_code = status_code
                retry_log.response_body = response_body
                retry_log.save(update_fields=["success", "status_code", "response_body", "updated_at"])
                if is_ok:
                    ok_count += 1
                else:
                    fail_count += 1
            except HTTPError as exc:
                err_body = ""
                try:
                    err_body = exc.read().decode("utf-8", errors="ignore")[:4000]
                except Exception:
                    err_body = ""
                retry_log.error = f"HTTPError: {exc}"
                retry_log.status_code = int(getattr(exc, "code", 0) or 0)
                retry_log.response_body = err_body
                retry_log.save(update_fields=["error", "status_code", "response_body", "updated_at"])
                fail_count += 1
            except URLError as exc:
                retry_log.error = f"URLError: {exc}"
                retry_log.save(update_fields=["error", "updated_at"])
                fail_count += 1
            except Exception as exc:
                retry_log.error = f"Exception: {exc}"
                retry_log.save(update_fields=["error", "updated_at"])
                fail_count += 1

        self.stdout.write(
            self.style.SUCCESS(f"Webhook retries done. Total: {total}, success: {ok_count}, failed: {fail_count}")
        )
