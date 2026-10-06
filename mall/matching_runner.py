"""Run with python -m mall.matching_runner [--once] [--force] [--no-ai]."""
import argparse
import os
import time


def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from django.db import close_old_connections
    from .matching_worker import process_queue
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--no-ai', action='store_true')
    options = parser.parse_args()
    while True:
        close_old_connections()
        try:
            result = process_queue(force=options.force, use_ai=not options.no_ai)
            if result:
                print(result, flush=True)
        except Exception:
            if options.once:
                raise
        if options.once:
            return
        options.force = False
        time.sleep(5)


if __name__ == '__main__':
    main()
