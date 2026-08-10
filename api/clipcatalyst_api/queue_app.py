"""Celery application. Redis broker/backend, or fully-eager for tests/dev."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from .settings import get_settings

# Task modules the worker must import so their @task decorators register.
# Without this the CLI (`celery -A clipcatalyst_api.queue_app worker`) starts a
# worker with ZERO tasks — process_job/reap_expired live in worker.py, which
# this module never imports directly.
TASK_MODULES = ["clipcatalyst_api.worker"]


def create_celery_app() -> Celery:
    settings = get_settings()
    app = Celery(
        "clipcatalyst",
        broker=settings.redis_url,
        backend=settings.redis_url,
        include=TASK_MODULES,
    )

    # A whole job = up to `count` (max 3) sequential ffmpeg renders plus probe
    # and transcription. Budget the render ceiling per clip and add generous
    # transcription headroom so a legitimately long job is never killed.
    hard_time_limit = settings.render_timeout_s * 3 + 1800
    soft_time_limit = hard_time_limit - 60

    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
        broker_connection_retry_on_startup=True,
        # One long-running render per worker slot; don't prefetch a backlog.
        worker_prefetch_multiplier=1,
        task_acks_late=True,
        # Hard kill / soft (catchable) ceilings for one job. Soft fires first so
        # process_job can mark the job failed before the hard limit kills it.
        task_time_limit=hard_time_limit,
        task_soft_time_limit=soft_time_limit,
        # Don't requeue a job whose worker was lost mid-run — a duplicate render
        # is worse than a stranded row, which reconcile_stalled cleans up.
        task_reject_on_worker_lost=False,
        # Keep the broker from redelivering a long job to another worker while
        # the first is still legitimately working on it (acks_late + long jobs).
        broker_transport_options={"visibility_timeout": hard_time_limit + 600},
        # Hourly cleanup of expired jobs. Requires a running `celery beat`
        # process (`celery -A clipcatalyst_api.queue_app beat`); harmless config
        # when no beat is deployed.
        beat_schedule={
            "reap-expired-jobs": {
                "task": "clipcatalyst.reap_expired",
                "schedule": crontab(minute=0),
            },
        },
    )
    if settings.queue == "eager":
        # CC_QUEUE=eager: run tasks inline in the calling process — tests and
        # single-box dev without Redis.
        app.conf.task_always_eager = True
        app.conf.task_eager_propagates = True
    return app


celery_app = create_celery_app()
