"""The test runner of config.settings_test. --noinput drops an existing database with the test database's name, so before it builds one
it refuses a name that could be a real database: one that does not start with test_, or that is the configured database or the live
one (LIVE_DATABASE_NAME, partsmall). With the app and the workers stopped (rollback rung 4) PostgreSQL would let that drop through."""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.test.runner import DiscoverRunner


def unsafe_test_name(database, live=None):
    """Why a DATABASES entry's test database name is unsafe to drop, or None. No name means Django's own test_<NAME>."""
    name = (database.get('TEST') or {}).get('NAME')
    if not name:
        return None
    live = live or getattr(settings, 'LIVE_DATABASE_NAME', 'partsmall')
    if not str(name).startswith('test_') or str(name) in {str(database.get('NAME')), live}:
        return (f'La base de pruebas se llama {name!r}: debe empezar por test_ y no ser la base configurada ({database.get("NAME")}) ni la '
                f'base en vivo ({live}), porque --noinput borra la que exista con ese nombre.')
    return None


class GuardedTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        for alias in connections:
            problem = unsafe_test_name(connections[alias].settings_dict)
            if problem:
                raise ImproperlyConfigured(problem)
        return super().setup_databases(**kwargs)
