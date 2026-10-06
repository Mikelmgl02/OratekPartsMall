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


@contextmanager
def matching_work():
    token = _inside_worker.set(True)
    try:
        yield
    finally:
        _inside_worker.reset(token)
