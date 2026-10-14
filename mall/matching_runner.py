"""Run with python -m mall.matching_runner [--once] [--force] [--no-ai]; --healthcheck checks the heartbeat.

The loop here also runs the apiag-sync and quote-holds services. While the catalog freeze is on (mall.maintenance) it idles instead of
starting a pass, and SIGTERM/SIGINT (docker compose stop or restart, Ctrl-C) stop it cleanly: the pass in progress finishes its current
unit (a matching stage, an apiag-cloud page, an order's hold), gives its lease back and the process exits."""
import argparse
import os
import signal
import sys
import time
import traceback
from pathlib import Path

POLL_SECONDS, MAX_BACKOFF_SECONDS, PROBATION_SECONDS = 5, 60, 60
FREEZE_LEVELS = ('catalog', 'all')
# Seconds after a stop signal before a pass that has not reached a checkpoint is cut at its next query outside a transaction (a unit
# already begun still commits or rolls back whole). Below the services' stop_grace_period (60 s), after which Docker kills them.
STOP_DEADLINE_SECONDS = 40
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


class WorkerStopping(BaseException):
    """Raised between two units of a pass when the runner must stop (a signal) or idle (the freeze it honours began). The pass gives its
    lease back and lets it through. Not an Exception, so no `except Exception` inside a pass swallows it."""


class Stop:
    """The runner's stop state. The signal handler only sets a flag; the pass notices it at its next checkpoint() (mall.matching_worker
    between stages, mall.apiag_sync between pages) or, STOP_DEADLINE_SECONDS later, at its next query outside a transaction. Each pass
    raises WorkerStopping at most once, so the code that gives the lease back can still query."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self):
        self.signal, self.requested_at, self.raised, self.idle_under = None, None, False, ()

    @property
    def requested(self):
        return self.requested_at is not None

    def handle(self, signum, frame=None):
        # Only flags: printing from a handler can land in the middle of another write to the same stream.
        if self.requested:
            raise SystemExit(128 + signum)  # a second signal does not wait for the pass
        self.signal, self.requested_at = signum, self.clock()

    def name(self):
        return signal.Signals(self.signal).name if self.signal else 'stop'

    def reason(self):
        """Why the pass in progress must stop now: 'signal', 'freeze' or None."""
        if self.requested:
            return 'signal'
        if self.idle_under:
            from .maintenance import freeze_level
            if freeze_level() in self.idle_under:
                return 'freeze'
        return None

    def checkpoint(self):
        reason = None if self.raised else self.reason()
        if reason:
            self.raised = True
            raise WorkerStopping(reason)

    def on_query(self, execute, sql, params, many, context):
        if (self.requested and not self.raised and self.clock() - self.requested_at >= STOP_DEADLINE_SECONDS
                and not context['connection'].in_atomic_block):
            self.raised = True
            raise WorkerStopping('signal')
        return execute(sql, params, many, context)

    def sleep(self, seconds):
        """time.sleep that a stop signal cuts short (it looks at the flag every second)."""
        end = self.clock() + seconds
        while not self.requested and end - self.clock() > 0:
            time.sleep(min(end - self.clock(), 1))


STOP = Stop()


def checkpoint():
    """Between two units of a pass: raises WorkerStopping when the runner was told to stop or the freeze it honours began. Outside the
    runner (tests, CLIs, a view) it does nothing."""
    STOP.checkpoint()


def healthcheck(path=None, max_age=None):
    path, max_age = path or heartbeat_path(), float(max_age or os.environ.get('MATCHING_HEARTBEAT_MAX_AGE') or HEARTBEAT_MAX_AGE)
    try:
        age = time.time() - os.stat(path).st_mtime
    except OSError:
        print(f'No matching heartbeat at {path}.')
        return 1
    print(f'Matching heartbeat {age:.0f}s old (limit {max_age:.0f}s).')
    return 0 if age <= max_age else 1


def run(process_queue, *, once=False, force=False, use_ai=True, heartbeat=None, sleep=None, idle_under=FREEZE_LEVELS, signals=False):
    """The service loop. idle_under: the freeze levels it waits out (quote-holds only 'all'). signals: install the SIGTERM/SIGINT handlers
    (the services and manual passes do; tests drive STOP directly)."""
    STOP.reset()
    previous = {number: signal.signal(number, STOP.handle) for number in (signal.SIGTERM, signal.SIGINT)} if signals else {}
    STOP.idle_under = tuple(idle_under)
    try:
        loop(process_queue, once=once, force=force, use_ai=use_ai, heartbeat=heartbeat or Heartbeat(''), sleep=sleep or STOP.sleep)
    finally:
        STOP.idle_under = ()
        for number, handler in previous.items():
            signal.signal(number, handler)


def loop(process_queue, *, once, force, use_ai, heartbeat, sleep):
    from django.db import close_old_connections, connection, connections
    from .maintenance import freeze_level
    failures, idle = 0, None
    while not STOP.requested:
        close_old_connections()
        level = freeze_level()
        if level in STOP.idle_under:
            if idle != level:
                print(f'Maintenance freeze ({level}): {"no pass while it is on" if once else "idle until it is lifted"}.', flush=True)
            idle = level
            heartbeat.beat()  # waiting out a freeze is healthy
            if once:
                return
            sleep(POLL_SECONDS)
            continue
        if idle:
            print('Maintenance freeze lifted: resuming.', flush=True)
            idle = None
        # Queries before a repeated failure prove nothing: while passes keep failing, only one that outlives its probation beats.
        heartbeat.hold_until = heartbeat.clock() + PROBATION_SECONDS if failures else None
        STOP.raised = False
        try:
            # Completed queries are the progress checkpoints of a long pass.
            with connection.execute_wrapper(heartbeat.on_query), connection.execute_wrapper(STOP.on_query):
                result = process_queue(force=force, use_ai=use_ai)
        except WorkerStopping as stopped:
            # The pass stopped between two units and gave its lease back; next turn the loop exits (signal) or idles (freeze).
            print(f'Pass stopped between units ({STOP.name() if str(stopped) == "signal" else "maintenance freeze"}).', flush=True)
            try:
                connections.close_all()
            except Exception:
                traceback.print_exc()
            if once:
                return
            continue
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
        if not STOP.requested:
            sleep(delay)
    print(f'{STOP.name()}: exiting.', flush=True)


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
        heartbeat=Heartbeat('' if options.once else heartbeat_path()), signals=True)


if __name__ == '__main__':
    main()
