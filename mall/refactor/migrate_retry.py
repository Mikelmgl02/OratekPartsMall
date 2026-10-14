"""python -m mall.refactor.migrate_retry [app_label [migration_name]] [--attempts 5] [--wait 15]: manage.py migrate, run again when a
migration gives up waiting for a table lock (mall.refactor.migration_ops.LockTimeout: `canceling statement due to lock timeout`,
SQLSTATE 55P03). An atomic migration that timed out rolled back whole, so running it again is safe. Any other error stops at once, and
a plan with a non-atomic migration (it may have run part of its operations) gets a single attempt. From the rebased worktree, in D4
and D7 (and backwards in rollback rung 3):

    docker exec -w /app/.claude/worktrees/<wt> partsmall-app-1 python -m mall.refactor.migrate_retry mall 0071"""
import argparse
import os
import sys
import time

LOCK_NOT_AVAILABLE = '55P03'


def lock_timeout(error):
    """Whether a failure is PostgreSQL's lock timeout, looking through Django's wrapper (psycopg 3 sqlstate, psycopg2 pgcode)."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if LOCK_NOT_AVAILABLE in (getattr(error, 'sqlstate', None), getattr(error, 'pgcode', None)):
            return True
        error = error.__cause__ or error.__context__
    return False


def non_atomic(app_label=None, migration_name=None, alias='default'):
    """The migrations migrate would run for these arguments, forwards or backwards, that are not atomic."""
    from django.db import connections
    from django.db.migrations.executor import MigrationExecutor
    executor = MigrationExecutor(connections[alias])
    if app_label and migration_name:
        targets = ([(app_label, None)] if migration_name == 'zero'
                   else [(app_label, executor.loader.get_migration_by_prefix(app_label, migration_name).name)])
    else:
        targets = [key for key in executor.loader.graph.leaf_nodes() if app_label in (None, key[0])]
    return [f'{migration.app_label}.{migration.name}' for migration, _ in executor.migration_plan(targets) if not migration.atomic]


def migrate(app_label=None, migration_name=None, *, attempts=5, wait=15, sleep=time.sleep, stdout=None, stderr=None):
    """migrate, up to `attempts` times while it fails on a lock timeout. Returns the attempt that worked; the last failure is raised."""
    from django.core.management import call_command
    from django.db import connections
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    partial = non_atomic(app_label, migration_name)
    if partial and attempts > 1:
        print(f'No atómicas: {", ".join(partial)}. Un bloqueo puede cortarlas a medias y reintentarlas fallaría: un solo intento.',
              file=stderr, flush=True)
        attempts = 1
    arguments = [value for value in (app_label, migration_name) if value]
    for attempt in range(1, attempts + 1):
        try:
            call_command('migrate', *arguments, skip_checks=False, stdout=stdout, stderr=stderr)
            return attempt
        except Exception as error:
            if attempt == attempts or not lock_timeout(error):
                raise
            print(f'Intento {attempt} de {attempts}: una tabla siguió bloqueada más que el lock_timeout y la migración se deshizo entera. '
                  f'Nuevo intento en {wait:g} s.', file=stderr, flush=True)
            connections.close_all()
            sleep(wait)


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='migrate, otra vez cuando una migración agota su lock_timeout esperando una tabla.')
    parser.add_argument('app_label', nargs='?')
    parser.add_argument('migration_name', nargs='?', help='Como en migrate: el destino (hacia atrás también) o zero.')
    parser.add_argument('--attempts', type=int, default=5, help='Intentos en total (5).')
    parser.add_argument('--wait', type=float, default=15, help='Segundos entre intentos (15).')
    options = parser.parse_args(argv)
    if options.attempts < 1 or options.wait < 0:
        parser.error('--attempts es al menos 1 y --wait no es negativo.')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    migrate(options.app_label, options.migration_name, attempts=options.attempts, wait=options.wait, stdout=stdout)


if __name__ == '__main__':
    main()
