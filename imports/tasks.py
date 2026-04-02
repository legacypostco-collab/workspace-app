from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .models import ImportJob
from .services import ErrorReportBuilder, ImportRowPipeline

logger = logging.getLogger("imports")


@contextmanager
def _import_job_lock(import_job_id: int):
    lock_key = f"import-job-lock:{import_job_id}"
    lock_token = f"{timezone.now().timestamp()}:{import_job_id}"
    client = None
    locked = False
    try:
        try:
            import redis

            client = redis.Redis.from_url(settings.CELERY_BROKER_URL)
            locked = bool(client.set(lock_key, lock_token, nx=True, ex=900))
        except Exception:
            # If Redis is unavailable, continue without hard-failing the task.
            locked = True
        yield locked
    finally:
        if client is not None and locked:
            try:
                current_val = client.get(lock_key)
                if current_val and current_val.decode("utf-8") == lock_token:
                    client.delete(lock_key)
            except Exception:
                pass


@shared_task(bind=True, max_retries=3, default_retry_delay=2)
def process_import_job(self, import_job_id: int):
    started_monotonic = time.monotonic()
    try:
        job = ImportJob.objects.select_related("supplier", "source_file").get(id=import_job_id)
    except ImportJob.DoesNotExist:
        logger.warning("import_job_not_found", extra={"job_id": import_job_id})
        return {"ok": False, "reason": "not_found", "job_id": import_job_id}

    if job.status in {ImportJob.Status.COMPLETED, ImportJob.Status.PARTIAL_SUCCESS, ImportJob.Status.FAILED}:
        return {"ok": True, "reason": "already_finished", "job_id": import_job_id, "status": job.status}

    with _import_job_lock(import_job_id) as locked:
        if not locked:
            logger.info("import_job_locked_skip", extra={"job_id": import_job_id, "supplier_id": job.supplier_id})
            return {"ok": True, "reason": "locked", "job_id": import_job_id}

        try:
            logger.info("import_job_start", extra={"job_id": job.id, "supplier_id": job.supplier_id})
            job.status = ImportJob.Status.PROCESSING
            if not job.started_at:
                job.started_at = timezone.now()
            job.save(update_fields=["status", "started_at", "updated_at"])

            summary = ImportRowPipeline().process_job(job)
            job.refresh_from_db()

            if summary.error_rows > 0:
                ErrorReportBuilder().build_for_job(job)

            if summary.error_rows == 0:
                final_status = ImportJob.Status.COMPLETED
            elif summary.valid_rows > 0:
                final_status = ImportJob.Status.PARTIAL_SUCCESS
            else:
                final_status = ImportJob.Status.FAILED

            job.status = final_status
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at", "updated_at"])
            try:
                from dashboard.services import refresh_dashboard_projection_for_user

                refresh_dashboard_projection_for_user(job.supplier)
            except Exception:
                logger.exception("dashboard_projection_refresh_failed_after_import", extra={"job_id": job.id, "supplier_id": job.supplier_id})

            duration_ms = int((time.monotonic() - started_monotonic) * 1000)
            logger.info(
                "import_job_end",
                extra={
                    "job_id": job.id,
                    "supplier_id": job.supplier_id,
                    "status": job.status,
                    "duration_ms": duration_ms,
                    "total_rows": job.total_rows,
                    "valid_rows": job.valid_rows,
                    "error_rows": job.error_rows,
                },
            )
            return {"ok": True, "job_id": job.id, "status": job.status}
        except Exception as exc:
            logger.exception(
                "import_job_failed_attempt",
                extra={"job_id": job.id, "supplier_id": job.supplier_id, "retry": self.request.retries},
            )
            if self.request.retries >= self.max_retries:
                job.status = ImportJob.Status.FAILED
                job.error_message = str(exc)[:2000]
                job.finished_at = timezone.now()
                job.save(update_fields=["status", "error_message", "finished_at", "updated_at"])
                try:
                    from dashboard.services import refresh_dashboard_projection_for_user

                    refresh_dashboard_projection_for_user(job.supplier)
                except Exception:
                    logger.exception("dashboard_projection_refresh_failed_after_import_error", extra={"job_id": job.id, "supplier_id": job.supplier_id})
                logger.error(
                    "import_job_failed_final",
                    extra={"job_id": job.id, "supplier_id": job.supplier_id, "error": str(exc)},
                )
                return {"ok": False, "job_id": job.id, "status": job.status, "error": str(exc)}

            countdown = 2 ** (self.request.retries + 1)
            raise self.retry(exc=exc, countdown=countdown)
