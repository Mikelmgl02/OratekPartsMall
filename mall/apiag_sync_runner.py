"""The apiag-sync service: pulls each supplier's apiag-cloud (mall.apiag_sync), one pull per pass, on the matching runner's loop
(heartbeat, backoff, fresh connections after a failure). It idles while the catalog freeze is on, and SIGTERM stops a pull after its
current page.

    python -m mall.apiag_sync_runner                 the service (docker compose: apiag-sync)
    python -m mall.apiag_sync_runner --once          one pass by hand
    python -m mall.apiag_sync_runner --healthcheck   exit 1 when the heartbeat is missing or stale"""
import argparse
import os
import sys

from .matching_runner import Heartbeat, healthcheck, run

HEARTBEAT_FILE = os.getenv('APIAG_SYNC_HEARTBEAT_FILE', '/tmp/apiag-sync.heartbeat')
HEARTBEAT_MAX_AGE = int(os.getenv('APIAG_SYNC_HEARTBEAT_MAX_AGE', '600'))  # a pull queries the DB on every page, so it beats too


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
    from .apiag_sync import process_due
    run(process_due, once=options.once, heartbeat=Heartbeat('' if options.once else HEARTBEAT_FILE), signals=True)


if __name__ == '__main__':
    main()
