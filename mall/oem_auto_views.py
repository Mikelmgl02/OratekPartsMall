"""Superuser API of 'Pendientes de aplicación automática' (Inventario → Aplicación OEM): the pending AUTO rows with filters, counts and
the canary/halt state per apply tier; a read-only preview of a canary (50) or batch (100); the apply itself (oem_apply.apply_auto under
the matching advisory lock, idempotent per operation id); and send-to-review, which takes rows out of automatic application. Run history,
spot-check and undo stay the O3 endpoints (/oem-finder/runs/...).
"""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import oem_apply as oa
from . import oem_auto as au
from . import oem_finder as of
from .management import IsSuperuser
from .matching_queue import enqueue_matching
from .oem_finder_views import HaltRow, OEMFinderBusy, RunRow, run_row, runs
from .oem_review import write_lock
from .oem_review_views import CatalogImportBusy

APPLY_TIERS, MODES = list(oa.APPLY_TIERS), list(au.MODES)
FILTER_PARAMETERS = [
    OpenApiParameter('status', OpenApiTypes.STR, enum=list(au.STATUSES), default='pending'),
    OpenApiParameter('tier', OpenApiTypes.STR, enum=list(au.TIERS), description='Pestaña: AUTO_FLAG_CURRENT (el SKU ya es el OEM) o AUTO_RENAME_BASE.'),
    OpenApiParameter('apply_tier', OpenApiTypes.STR, enum=APPLY_TIERS, description='Nivel con el que se aplicaría (canario y lotes por nivel).'),
    OpenApiParameter('chain', OpenApiTypes.STR, description='Cadena de sufijos exacta (G, MOBIS, G-NP).'),
    OpenApiParameter('make', OpenApiTypes.STR, description='Marca del OEM propuesto (TOYOTA, HYUNDAI, KIA...).'),
    OpenApiParameter('system', OpenApiTypes.STR, description='Sistema de formato (TOYOTA, HMG, NISSAN...).'),
    OpenApiParameter('in_stock', OpenApiTypes.STR, enum=['true', 'false']), OpenApiParameter('search', OpenApiTypes.STR, description='SKU, descripción u OEM.'),
    OpenApiParameter('ordering', OpenApiTypes.STR, enum=list(au.ORDERINGS), default='stock', description='stock: con existencias primero (como se aplican).')]


class OEMAutoUnavailable(APIException):
    status_code = 503
    default_detail = 'Los pendientes de aplicación automática aún no están disponibles: faltan migraciones por aplicar.'


class OEMAutoRefused(APIException):
    status_code = 409
    default_detail = 'La aplicación automática no puede empezar ahora.'


class OEMAutoFailed(APIException):
    status_code = 500
    default_detail = 'La ejecución falló y se activó la regla de detención; revisa el historial antes de continuar.'


def require_tables():
    if not au.auto_tables_ready():
        raise OEMAutoUnavailable()


def refused(error):
    return OEMAutoRefused({'detail': error.detail, 'reason': error.reason, **error.extra})


def failed_run(operation_id):
    try:
        return au.operation_run(operation_id)
    except Exception:  # the database itself failed: report the error, not this lookup's
        return None


class PendingRow(serializers.Serializer):
    id = serializers.IntegerField()
    part_id = serializers.UUIDField()
    sku = serializers.CharField(help_text='SKU actual.')
    evaluated_sku = serializers.CharField(help_text='SKU cuando se analizó.')
    description = serializers.CharField(allow_blank=True)
    tier = serializers.ChoiceField(choices=list(au.TIERS))
    apply_tier = serializers.ChoiceField(choices=APPLY_TIERS)
    candidate = serializers.CharField(help_text='OEM propuesto (forma del fabricante).')
    written_form = serializers.CharField(allow_blank=True)
    brand = serializers.CharField(allow_blank=True)
    makes = serializers.ListField(child=serializers.CharField())
    system = serializers.CharField(allow_blank=True)
    grade = serializers.CharField(allow_blank=True)
    chain = serializers.CharField(allow_blank=True)
    chain_tokens = serializers.ListField(child=serializers.DictField(), help_text='sep, tok, token, class, kind, status, auto_eligible.')
    owner_tags = serializers.ListField(child=serializers.CharField(), help_text='Etiquetas confirmadas por el propietario en las que descansa el renombre.')
    method = serializers.ChoiceField(choices=['flag', 'rename'], help_text='flag: el SKU ya es el OEM; rename: el OEM pasa a ser el MAIN.')
    in_stock = serializers.BooleanField()
    available_quantity = serializers.IntegerField()
    evidence = serializers.DictField(help_text='Léxico, unidades, atribuciones, proveedores, hermanos, motivos y advertencias.')
    status = serializers.ChoiceField(choices=list(au.STATUSES))
    reason = serializers.CharField(allow_blank=True)
    reason_label = serializers.CharField(allow_blank=True)
    stale = serializers.CharField(allow_blank=True, help_text='missing, retired o snapshot_changed: el SKU cambió desde el análisis (se omitirá al aplicar).')
    stale_label = serializers.CharField(allow_blank=True)
    note = serializers.CharField(allow_blank=True)
    decided_by = serializers.CharField(allow_null=True)
    decided_at = serializers.DateTimeField(allow_null=True)
    updated_at = serializers.DateTimeField()
    run = serializers.IntegerField(allow_null=True, help_text='Ejecución cuyo análisis cambió la fila por última vez.')


class ApplyTierState(serializers.Serializer):
    pending = serializers.IntegerField()
    in_stock = serializers.IntegerField()
    canary_done = serializers.BooleanField(help_text='Un canario de estas reglas habilitó los lotes de este nivel.')


class PendingListResponse(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = PendingRow(many=True)
    tiers = serializers.DictField(child=serializers.IntegerField(), help_text='Filas por pestaña con los demás filtros.')
    apply_tiers = serializers.DictField(child=serializers.IntegerField(), help_text='Filas por nivel de aplicación con los demás filtros.')
    statuses = serializers.DictField(child=serializers.IntegerField())
    facets = serializers.DictField(help_text='chains, makes, systems: [[valor, filas], ...] con los demás filtros.')
    apply = serializers.DictField(child=ApplyTierState(), help_text='Por nivel de aplicación: pendientes y crédito de canario.')
    sizes = serializers.DictField(child=serializers.IntegerField(), help_text='canary y batch: filas por ejecución.')
    rules_version = serializers.CharField(help_text='Versión actual de las reglas del buscador: el crédito de canario es por versión.')
    halt = HaltRow(allow_null=True)
    busy = serializers.DictField(help_text='lock: hay un análisis de coincidencias o una búsqueda OEM en curso; running: ejecución automática sin terminar.')
    last_refresh = serializers.DictField(allow_null=True, help_text='Última ejecución que actualizó los pendientes: id, mode, stage, finished_at, rules_version.')


class PreviewRequest(serializers.Serializer):
    tier = serializers.ChoiceField(choices=APPLY_TIERS)
    mode = serializers.ChoiceField(choices=MODES)


class PreviewRow(serializers.Serializer):
    part_id = serializers.UUIDField()
    sku = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    oem = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    method = serializers.ChoiceField(choices=['flag', 'rename'])
    outcome = serializers.ChoiceField(choices=['applied', 'conflict', 'skipped'])
    detail = serializers.CharField(allow_blank=True)
    available_quantity = serializers.IntegerField()
    in_stock = serializers.BooleanField()


class PreviewResponse(serializers.Serializer):
    tier = serializers.ChoiceField(choices=APPLY_TIERS)
    mode = serializers.ChoiceField(choices=MODES)
    size = serializers.IntegerField()
    eligible = serializers.IntegerField(help_text='Filas del nivel que la aplicación tomaría en total (sin las excluidas).')
    selected = serializers.IntegerField(help_text='Las que tomaría esta ejecución (con existencias primero).')
    would_apply = serializers.IntegerField()
    conflicts = serializers.IntegerField()
    skipped = serializers.DictField(child=serializers.IntegerField())
    excluded = serializers.DictField(child=serializers.IntegerField(), help_text='sent_to_review, open_conflict, previously_reverted.')
    rows = PreviewRow(many=True)
    canary_done = serializers.BooleanField()
    halt = serializers.DictField(allow_null=True)
    suffix_table_version = serializers.CharField()
    seconds = serializers.FloatField()


class ApplyRequest(PreviewRequest):
    operation_id = serializers.UUIDField(help_text='Identificador de la operación (uno por clic): repetirlo devuelve la misma ejecución.')


class ApplyResponse(serializers.Serializer):
    run = RunRow()
    replayed = serializers.BooleanField(help_text='La operación ya se había ejecutado: no se aplicó nada nuevo.')


class SendRequest(serializers.Serializer):
    parts = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=au.SEND_LIMIT)
    note = serializers.CharField(max_length=300, required=False, allow_blank=True)


class SentRow(serializers.Serializer):
    part_id = serializers.UUIDField()
    sku = serializers.CharField()
    case = serializers.IntegerField(required=False, help_text='Caso de la Revisión OEM.')


class SendSkipped(serializers.Serializer):
    part_id = serializers.UUIDField()
    sku = serializers.CharField(allow_blank=True)
    reason = serializers.CharField()
    label = serializers.CharField()


class SendResponse(serializers.Serializer):
    sent = SentRow(many=True)
    already = SentRow(many=True)
    skipped = SendSkipped(many=True)


class OEMPendingList(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_finder_pending_list', responses=PendingListResponse,
                   parameters=FILTER_PARAMETERS + [OpenApiParameter('page', OpenApiTypes.INT)],
                   description='Pendientes de aplicación automática (con existencias primero) con su evidencia, filas por pestaña y facetas, y por '
                               'nivel de aplicación los pendientes y si ya tiene canario; también la regla de detención y si hay una ejecución en curso.')
    def get(self, request):
        require_tables()
        f = au.parse_filters(request.query_params)
        paginator = PageNumberPagination()
        paginator.page_size = au.PAGE_SIZE
        page = paginator.paginate_queryset(au.ordered(au.rows(f).select_related('decided_by'), f['ordering']), request, view=self)
        return Response({'count': paginator.page.paginator.count, 'next': paginator.get_next_link(), 'previous': paginator.get_previous_link(),
                         'results': au.page_rows(page), **au.overview(f)})


class OEMPendingPreview(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_finder_pending_preview', request=PreviewRequest, responses=PreviewResponse,
                   description='Vista previa de solo lectura de un canario (50) o un lote (100) de un nivel: evalúa el catálogo como la aplicación y '
                               'devuelve las filas exactas que tomaría, cuántas aplicaría, cuántas pasarían a conflicto y cuántas omitiría. No escribe nada.')
    def post(self, request):
        require_tables()
        data = PreviewRequest(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(au.preview(data.validated_data['tier'], data.validated_data['mode']))


class OEMPendingApply(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_finder_pending_apply', request=ApplyRequest, responses={
        201: ApplyResponse, 200: OpenApiResponse(ApplyResponse, description='La operación ya existía: su ejecución, sin aplicar nada nuevo.'),
        409: OpenApiResponse(description='La aplicación está detenida, el nivel no tiene canario (lote), el identificador ya se usó para otra '
                                         'aplicación, o hay un análisis de coincidencias, otra ejecución o una importación en curso.'),
        500: OpenApiResponse(description='La ejecución falló (quedó registrada como fallida y se activó la regla de detención) o no pudo iniciarse '
                                         '(no se aplicó nada).')},
        description='Aplica un canario (50) o un lote (100) de un nivel con la aplicación automática del buscador OEM: con existencias primero, '
                    'una transacción por SKU, muestra de control de 20 filas y una ejecución que se puede deshacer. Los lotes exigen un canario '
                    'previo del nivel.')
    def post(self, request):
        require_tables()
        data = ApplyRequest(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        if of.import_running() and au.operation_run(d['operation_id']) is None:
            raise CatalogImportBusy()
        try:
            with write_lock() as acquired:
                if not acquired:
                    raise OEMFinderBusy()
                run, replayed = au.apply(d['tier'], d['mode'], d['operation_id'], request.user)
        except au.Refused as error:
            raise refused(error)
        except (APIException, ValidationError):
            raise
        except Exception as error:
            failed = failed_run(d['operation_id'])  # apply_auto records a failed run and raises the halt; before its run nothing was written
            raise OEMAutoFailed(f'La ejecución #{failed.pk} falló ({type(error).__name__}) y se activó la regla de detención; revisa el historial antes de continuar.'
                                if failed else f'No se pudo iniciar la aplicación ({type(error).__name__}); no se aplicó nada. Inténtalo de nuevo.')
        if not replayed and (run.applied or {}).get('applied'):
            enqueue_matching()  # like any catalog edit: new OEM MAINs and alternos may match pending supplier codes
        return Response({'run': run_row(runs().get(pk=run.pk)), 'replayed': replayed}, status=200 if replayed else 201)


class OEMPendingSendToReview(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_finder_pending_send_to_review', request=SendRequest, responses={
        200: SendResponse, 409: OpenApiResponse(description='Hay un análisis de coincidencias o una búsqueda OEM en curso.')},
        description='Saca filas pendientes de la aplicación automática (ninguna ejecución automática las tomará) y crea o reabre su caso en la '
                    'Revisión OEM para que una persona decida. Repetirlo no cambia nada.')
    def post(self, request):
        require_tables()
        data = SendRequest(data=request.data)
        data.is_valid(raise_exception=True)
        with write_lock() as acquired:
            if not acquired:
                raise OEMFinderBusy()
            result = au.send_to_review(data.validated_data['parts'], request.user, data.validated_data.get('note', ''))
        return Response(result)
