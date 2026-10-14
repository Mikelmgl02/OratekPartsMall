"""The quote-holds service: frees the stock of sent quotations whose hold lapsed while the client decided
(mall.stock_reservations.expire_due), on the matching runner's loop (heartbeat, backoff, fresh connections after a failure).

    python -m mall.quote_holds_runner                 the service (docker compose: quote-holds)
    python -m mall.quote_holds_runner --once          one pass by hand
    python -m mall.quote_holds_runner --healthcheck   exit 1 when the heartbeat is missing or stale"""
import argparse
import os
import sys

from .matching_runner import Heartbeat, healthcheck, run

HEARTBEAT_FILE = os.getenv('QUOTE_HOLDS_HEARTBEAT_FILE', '/tmp/quote-holds.heartbeat')
HEARTBEAT_MAX_AGE = int(os.getenv('QUOTE_HOLDS_HEARTBEAT_MAX_AGE', '600'))


def expire_pass(*, force=False, use_ai=True):
    """One pass: the lapsed holds released now, as a line for the log, or None when none was due."""
    from .stock_reservations import expire_due
    expired = expire_due()
    return f'Reservas de cotización vencidas: {expired} {"orden" if expired == 1 else "órdenes"}.' if expired else None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--healthcheck', action='store_true', help='Exit 1 when the worker heartbeat is missing or stale.')
    options = parser.parse_args(argv)
    if options.healthcheck:
        sys.exit(healthcheck(path=HEARTBEAT_FILE, max_age=HEARTBEAT_MAX_AGE))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    # Holds only touch reserved units and the deal's events, so they keep running under the catalog freeze; level all stops them.
    run(expire_pass, once=options.once, heartbeat=Heartbeat('' if options.once else HEARTBEAT_FILE), idle_under=('all',), signals=True)


if __name__ == '__main__':
    main()
