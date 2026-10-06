"""Run with python -m mall.matching_runner [--once] [--force] [--no-ai]; --healthcheck checks the heartbeat."""
import argparse
import os
import sys
import time
import traceback
from pathlib import Path

POLL_SECONDS, MAX_BACKOFF_SECONDS, PROBATION_SECONDS = 5, 60, 60
# Beats come from every loop pass and every completed query inside a pass, so the longest healthy
# silence is one AI request (90 s socket timeout) or a row-lock wait behind a web request (120 s
# gunicorn timeout), not the pass itself (30-minute lease). Keep this well above both.
HEARTBEAT_MAX_AGE = 600


def heartbeat_path():
    return os.environ.get('MATCHING_HEARTBEAT_FILE') or '/tmp/matching-worker.heartbeat'


class Heartbeat:
    """Touched only by the work loop itself: a hung pass or a loop that keeps failing goes stale."""

    def __init__(self, path, *, interval=5, clock=time.monotonic):
        self.path, self.interval, self.clock, self.last, self.failed, self.hold_until = path, interval, clock, None, False, None

    def beat(self):
        now = self.clock()
        if not self.path or (self.last is not None and now - self.last < self.interval):
            return
        self.last = now
        try:
            Path(self.path).touch()
            self.failed = False
        except Exception as error:
            # Liveness reporting never stops matching; the stale file surfaces in the healthcheck.
            if not self.failed:
                print(f'Matching heartbeat not written to {self.path}: {error}', file=sys.stderr, flush=True)
            self.failed = True

    def on_query(self, execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if self.hold_until is None or self.clock() >= self.hold_until:
            self.beat()
        return result


def healthcheck(path=None, max_age=None):
    path, max_age = path or heartbeat_path(), float(max_age or os.environ.get('MATCHING_HEARTBEAT_MAX_AGE') or HEARTBEAT_MAX_AGE)
    try:
        age = time.time() - os.stat(path).st_mtime
    except OSError:
        print(f'No matching heartbeat at {path}.')
        return 1
    print(f'Matching heartbeat {age:.0f}s old (limit {max_age:.0f}s).')
    return 0 if age <= max_age else 1


def run(process_queue, *, once=False, force=False, use_ai=True, heartbeat=None, sleep=time.sleep):
    from django.db import close_old_connections, connection, connections
    heartbeat, failures = heartbeat or Heartbeat(''), 0
    while True:
        close_old_connections()
        # Queries before a repeated failure prove nothing: while passes keep failing, only one that outlives its probation beats.
        heartbeat.hold_until = heartbeat.clock() + PROBATION_SECONDS if failures else None
        try:
            # Completed queries are the progress checkpoints of a long pass.
            with connection.execute_wrapper(heartbeat.on_query):
                result = process_queue(force=force, use_ai=use_ai)
        except Exception:
            if once:
                raise
            failures += 1
            delay = min(POLL_SECONDS * 2 ** min(failures - 1, 6), MAX_BACKOFF_SECONDS)
            print(f'Matching pass failed ({failures} in a row); retrying in {delay}s.', file=sys.stderr, flush=True)
            traceback.print_exc()
            sys.stderr.flush()
            # Django cannot drop a connection inside a leaked atomic block; exit so Docker restarts us.
            if connection.in_atomic_block:
                raise
            try:
                # A fresh session discards broken connections and any session advisory lock left behind.
                connections.close_all()
            except Exception:
                traceback.print_exc()
        else:
            failures, delay = 0, POLL_SECONDS
            heartbeat.beat()
            if result:
                print(result, flush=True)
        if once:
            return
        force = False
        sleep(delay)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--no-ai', action='store_true')
    parser.add_argument('--healthcheck', action='store_true', help='Exit 1 when the worker heartbeat is missing or stale.')
    options = parser.parse_args(argv)
    if options.healthcheck:
        sys.exit(healthcheck())
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .matching_worker import process_queue
    # Manual --once passes must not refresh the long-running worker's liveness file.
    run(process_queue, once=options.once, force=options.force, use_ai=not options.no_ai,
        heartbeat=Heartbeat('' if options.once else heartbeat_path()))


if __name__ == '__main__':
    main()
