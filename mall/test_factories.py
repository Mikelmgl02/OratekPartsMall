"""Shared test helpers for the inventory refactor (plan §1.4).

historical_apps() is the app registry as of one migration, for tests that run a data migration's functions: the live registry carries
today's models, which the refactor keeps changing, while a migration only ever sees the models of its own time.
"""
import importlib
from functools import lru_cache
from types import SimpleNamespace

from django.db import connection


@lru_cache(maxsize=None)
def historical_apps(migration, app='mall'):
    """The models as they were right after `migration` (its full name: '0039_oem_reference'), reading the current test database."""
    from django.db.migrations.loader import MigrationLoader
    return MigrationLoader(connection, ignore_no_migrations=True).project_state((app, migration)).apps


def migration_module(migration, app='mall'):
    return importlib.import_module(f'{app}.migrations.{migration}')


def run_migration_function(migration, name, app='mall'):
    """Call a migration's RunPython function (forwards or backwards) the way migrate does: historical models, default database."""
    return getattr(migration_module(migration, app), name)(historical_apps(migration, app), SimpleNamespace(connection=connection))
