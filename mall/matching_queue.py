from contextlib import contextmanager
from contextvars import ContextVar
from django.db import transaction
from django.db.models import F
from .matching_models import MatchingQueue

_inside_worker = ContextVar('matching_worker', default=False)


def _enqueue():
    MatchingQueue.objects.get_or_create(pk=1)
    from django.utils import timezone
    MatchingQueue.objects.filter(pk=1).update(requested=F('requested') + 1, requested_at=timezone.now())


def enqueue_matching(*args, **kwargs):
    if _inside_worker.get():
        return
    connection = transaction.get_connection()
    # One notification per transaction, even for a 500-row Excel batch.
    if not any(entry[1] is _enqueue for entry in connection.run_on_commit):
        transaction.on_commit(_enqueue)


MATCHING_LOCK = 724631109  # pg advisory lock key of a matching pass (quote assistant 724631110, suffix table 724631111)


@contextmanager
def matching_lock():
    """Session-level pg_try_advisory_lock: it survives per-item commits, so a long matching pass never overlaps another worker
    or an OEM finder run. Yields False while someone else holds it; always True off PostgreSQL."""
    from django.db import connection
    if connection.vendor != 'postgresql':
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [MATCHING_LOCK])
        locked = cursor.fetchone()[0]
    try:
        yield locked
    finally:
        if locked:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [MATCHING_LOCK])


@contextmanager
def matching_work():
    token = _inside_worker.set(True)
    try:
        yield
    finally:
        _inside_worker.reset(token)
