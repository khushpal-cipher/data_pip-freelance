from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery("refund_reconciler", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.update(
    task_always_eager=settings.celery_always_eager,
    # False (the default) lets self.retry() chain synchronously in eager mode
    # (tests/demo.py) instead of raising Retry out to the caller; genuine
    # failures never reach Celery unhandled since reconcile_refund_task
    # catches everything itself and classifies it via the ledger.
    task_eager_propagates=False,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="refunds",
)

# Import (not autodiscover_tasks: its default related_name="tasks" looks for
# app.tasks.tasks, which doesn't exist here) so the task is registered
# whenever this module loads, in both the web process and `celery worker -A
# app.celery_app`.
from app.tasks import reconcile as _reconcile  # noqa: E402,F401
