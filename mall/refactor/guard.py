"""The live-database guard of the stage CLIs (mall.refactor.<stage>) and the catalog CLIs: they write the catalog in bulk, so on the
live database --apply waits for the catalog freeze of a deploy window (mall.maintenance), which holds back the screens and the workers
that write the same rows. A rehearsal or test database is never the live one, so dry runs and rehearsals need nothing.

    django.setup()
    require_freeze(options.apply)     # before the first write; exits with the reason when it refuses"""
from django.conf import settings
from django.db import connections

from ..maintenance import FREEZE_FILE, folder, freeze_level


def live_database(alias='default'):
    """Whether the connection points at the live database (PostgreSQL, named LIVE_DATABASE_NAME); the test runner renames it."""
    connection = connections[alias]
    return connection.vendor == 'postgresql' and connection.settings_dict['NAME'] == settings.LIVE_DATABASE_NAME


def refusal(applying=True, alias='default'):
    """Why --apply may not run now, or None."""
    if not applying or not live_database(alias) or freeze_level():
        return None
    return (f'Base de datos en vivo ({settings.LIVE_DATABASE_NAME}) sin el congelamiento del catálogo: --apply no escribe nada. '
            f'Congela primero (mkdir -p {folder()} && echo catalog > {folder() / FREEZE_FILE}), espera a que MatchingQueue.lease y '
            'ApiagSync.lease estén vacíos y vuelve a ejecutar; quita el archivo al terminar.')


def require_freeze(applying=True, alias='default'):
    """Exits (status 1, the reason on stderr) when `applying` on the live database without the freeze."""
    reason = refusal(applying, alias)
    if reason:
        raise SystemExit(reason)
