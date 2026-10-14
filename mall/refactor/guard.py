"""The live-database guard of the stage CLIs (mall.refactor.<stage>) and the catalog CLIs: they write the catalog in bulk, so on the
live database --apply waits for the catalog freeze of a deploy window (mall.maintenance), which holds back the screens and the workers
that write the same rows, and then for the writers already running when it began: a matching pass or an apiag-cloud pull that still
holds its lease (they stop at their next stage or page), and an Excel or catalog import left half applied. A rehearsal or test database
is never the live one, so dry runs and rehearsals need nothing.

    django.setup()
    require_freeze(options.apply)     # before the first write; exits with the reason when it refuses"""
from django.conf import settings
from django.db import connections
from django.utils import timezone

from ..maintenance import FREEZE_FILE, folder, freeze_level


def live_database(alias='default'):
    """Whether the connection points at the live database (PostgreSQL, named LIVE_DATABASE_NAME); the test runner renames it."""
    connection = connections[alias]
    return connection.vendor == 'postgresql' and connection.settings_dict['NAME'] == settings.LIVE_DATABASE_NAME


def still_writing(alias='default'):
    """What still writes the catalog under the freeze, one line each (read-only queries): the workers' leases and the imports that are
    half applied (status importing; a ready preview cannot be applied while frozen)."""
    from ..apiag_sync_models import ApiagSync
    from ..matching_models import MatchingQueue
    from ..models import CatalogImportJob
    from ..supplier_import_models import SupplierInventoryImportJob
    now, busy = timezone.now(), []
    if MatchingQueue.objects.using(alias).filter(lease__isnull=False, lease_until__gt=now).exists():
        busy.append('un análisis de coincidencias (MatchingQueue.lease)')
    pulls = ApiagSync.objects.using(alias).filter(lease__isnull=False, lease_until__gt=now).count()
    if pulls:
        busy.append(f'{pulls} sincronización{"" if pulls == 1 else "es"} de apiag-cloud (ApiagSync.lease)')
    for model, name in ((CatalogImportJob, 'importación del catálogo'), (SupplierInventoryImportJob, 'importación de inventario')):
        jobs = list(model.objects.using(alias).filter(status='importing', expires_at__gt=now).values_list('pk', flat=True)[:5])
        if jobs:
            busy.append(f'{name} a medias ({model.__name__}: {", ".join(str(pk) for pk in jobs)})')
    return busy


def refusal(applying=True, alias='default'):
    """Why --apply may not run now, or None."""
    if not applying or not live_database(alias):
        return None
    if not freeze_level():
        return (f'Base de datos en vivo ({settings.LIVE_DATABASE_NAME}) sin el congelamiento del catálogo: --apply no escribe nada. '
                f'Congela primero (mkdir -p {folder()} && echo catalog > {folder() / FREEZE_FILE}), espera a que MatchingQueue.lease y '
                'ApiagSync.lease estén vacíos y vuelve a ejecutar; quita el archivo al terminar.')
    busy = still_writing(alias)
    if busy:
        return ('El catálogo está congelado, pero todavía escribe: ' + '; '.join(busy) + '. --apply no escribe nada. Los trabajadores se '
                'detienen en su siguiente etapa o página: espera y vuelve a ejecutar. Una importación a medias termina sin el '
                'congelamiento o vence (expires_at).')
    return None


def require_freeze(applying=True, alias='default'):
    """Exits (status 1, the reason on stderr) when `applying` on the live database without the freeze, or while a writer is still busy."""
    reason = refusal(applying, alias)
    if reason:
        raise SystemExit(reason)
