"""The supplier panel's view of its apiag-cloud pull: GET the state, POST «Sincronizar ahora» (any member, like an Excel upload).
The pull itself runs in the apiag-sync service (mall.apiag_sync); the token never leaves the server."""
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .apiag_sync import ensure_rows, request_run
from .apiag_sync_models import ApiagSucursal, ApiagSync
from .apiag_tokens import readable
from .views import account_for


def state(sync, now):
    if sync.lease and sync.lease_until and sync.lease_until > now:
        return 'running'
    if sync.requested > sync.completed:
        return 'queued'
    return sync.stage  # idle, completed, failed or paused


# The run's message speaks of the server; these speak to the supplier, who cannot change the connection (MotionPartes does).
SUPPLIER_ERRORS = {
    'redirect': 'La dirección guardada de tu apiag-cloud redirige a otra: pide a MotionPartes que la revise.',
    'not_found': 'Tu apiag-cloud no tiene la ruta de MotionPartes: actualízalo, o pide a MotionPartes que revise la dirección.',
    'internal': 'La dirección guardada de tu apiag-cloud no es pública: pide a MotionPartes que la revise.',
    'tls': 'El certificado HTTPS de tu apiag-cloud no es válido o no responde con HTTPS: revísalo o pide a MotionPartes que revise la dirección.',
}


def supplier_error(sync):
    if not sync.error or sync.error_code == 'token_unreadable':  # the card already says the token must be entered again
        return None
    return {'code': sync.error_code, 'message': SUPPLIER_ERRORS.get(sync.error_code, sync.error)}


def status_data(account):
    connection = getattr(account, 'apiag_connection', None)
    if connection is None:
        return {'connected': False}
    ensure_rows()
    sync = ApiagSync.objects.get(connection=connection)
    now = timezone.now()
    current = state(sync, now)
    return {
        'connected': True,
        'server': urlsplit(connection.server_url).netloc,
        'token_readable': readable(connection),
        'state': current,
        'can_sync': current not in ('running', 'queued'),
        'interval_minutes': settings.APIAG_SYNC_MINUTES,
        'last_started_at': sync.started_at,
        'last_finished_at': sync.finished_at,
        'last_success_at': sync.last_success_at,
        'next_run_at': sync.next_run_at,
        'error': supplier_error(sync),
        'last_mode': sync.summary.get('mode') or None,
        'counts': sync.summary.get('counts', {}),
        'samples': sync.summary.get('samples', [])[:10],
        'progress': sync.progress if current == 'running' else {},
        'sucursales': [{'id': sucursal.apiag_id, 'nombre': sucursal.nombre}
                       for sucursal in ApiagSucursal.objects.filter(account=account, listed=True).order_by('nombre', 'pk')],
    }


class ApiagStatusView(APIView):
    @extend_schema(operation_id='v1_accounts_apiag_status', responses=OpenApiTypes.OBJECT)
    def get(self, request, account_id):
        return Response(status_data(account_for(request.user, account_id, 'supplier')))


class ApiagSyncRequestView(APIView):
    @extend_schema(operation_id='v1_accounts_apiag_sync', request=None, responses=OpenApiTypes.OBJECT)
    def post(self, request, account_id):
        account = account_for(request.user, account_id, 'supplier')
        connection = getattr(account, 'apiag_connection', None)
        if connection is None:
            return Response({'detail': 'Esta cuenta no está conectada con apiag-cloud.'}, status=status.HTTP_404_NOT_FOUND)
        request_run(connection, request.user)
        return Response(status_data(account), status=status.HTTP_202_ACCEPTED)
