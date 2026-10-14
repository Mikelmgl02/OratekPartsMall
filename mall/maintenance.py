"""The catalog write freeze of a deploy window (inventory refactor, S0.11). Files in MAINTENANCE_DIR (/app/.maintenance in the
containers, the main checkout's .maintenance/ on the host, git-ignored) turn it on and off, so no restart is needed:

    mkdir -p .maintenance && echo catalog > .maintenance/freeze     catalog and inventory writes wait (level catalog)
    echo all > .maintenance/freeze                                   also orders, deal actions and the cesta (level all, S10 only)
    touch .maintenance/biblioteca                                    only the Biblioteca OEM is read-only (24 h after S4)
    rm .maintenance/freeze                                           back to normal

While it is on, MaintenanceFreezeMiddleware answers the writes of the frozen routes with a 503; reads, sign-in, messages, the inbox,
push, WhatsApp and favourites always go through. The matching and apiag-sync runners idle at either level and quote-holds at level all
(mall.matching_runner), and the stage and catalog CLIs refuse --apply on the live database unless it is on (mall.refactor.guard).
An empty or unknown level counts as catalog."""
import re
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse

FREEZE_FILE, BIBLIOTECA_FILE = 'freeze', 'biblioteca'
LEVELS = ('catalog', 'all')
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS', 'TRACE'})
RETRY_AFTER = '120'

# Level catalog: the management screens that write the catalog, the Biblioteca, matching and the taxonomy (catalog/ covers its proposals,
# import, assistant and suffixes), and the supplier's own stock writes: ingest and the Excel import, its preview too (a preview staged
# now would be checked against a catalog about to change, and its job would hold up the next window's pre-flight).
CATALOG_ROUTES = [re.compile(pattern) for pattern in (
    r'^/api/v1/management/(catalog|inventory|alternates|alternate-review|oem-references|oem-review|oem-finder|matching|part-types|applications)(/|$)',
    r'^/api/v1/accounts/[^/]+/inventory/(ingest|import)(/|$)',
)]
# Level all adds what holds or moves stock outside the catalog: solicitudes and their quotations, deal actions (also MotionPartes'
# cancel) and the cesta. Deal messages, threads and attachments stay open.
ALL_ROUTES = [re.compile(pattern) for pattern in (
    r'^/api/v1/accounts/[^/]+/(requests|sent-requests)(/|$)',
    r'^/api/v1/accounts/[^/]+/deals/[^/]+/actions/?$',
    r'^/api/v1/management/deals/[^/]+/cancel/?$',
    r'^/api/v1/accounts/[^/]+/basket/?$',
)]
BIBLIOTECA_ROUTES = [re.compile(r'^/api/v1/management/oem-references(/|$)')]

CATALOG_DETAIL = 'Mantenimiento del catálogo en curso. Intenta de nuevo en unos minutos.'
ALL_DETAIL = 'Mantenimiento en curso. Intenta de nuevo en unos minutos.'
BIBLIOTECA_DETAIL = 'La Biblioteca OEM está en solo lectura por mantenimiento. Intenta de nuevo más tarde.'


def folder():
    return Path(settings.MAINTENANCE_DIR)


def freeze_level():
    """'catalog' or 'all' while the freeze file exists, else None. A file that exists but cannot be read counts as catalog."""
    try:
        text = (folder() / FREEZE_FILE).read_text(encoding='utf-8', errors='replace')
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return 'catalog'
    return 'all' if text.strip().lower() == 'all' else 'catalog'


def biblioteca_frozen():
    return (folder() / BIBLIOTECA_FILE).exists()


def frozen_detail(method, path):
    """The 503 message for a request the freeze holds back, or None when it goes through."""
    if method.upper() in SAFE_METHODS:
        return None
    if any(route.match(path) for route in CATALOG_ROUTES):
        if freeze_level():
            return CATALOG_DETAIL
    elif any(route.match(path) for route in ALL_ROUTES):
        if freeze_level() == 'all':
            return ALL_DETAIL
    if any(route.match(path) for route in BIBLIOTECA_ROUTES) and biblioteca_frozen():
        return BIBLIOTECA_DETAIL
    return None


class MaintenanceFreezeMiddleware:
    """Before sessions and authentication, so a held-back write costs one stat of the freeze file and touches no table."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        detail = frozen_detail(request.method, request.path_info)
        if detail:
            response = JsonResponse({'detail': detail}, status=503)
            response['Retry-After'] = RETRY_AFTER
            return response
        return self.get_response(request)
