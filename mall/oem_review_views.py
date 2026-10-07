"""Superuser API of the 'Revisión OEM' queue (phase 2C): list with filters, counts and facets; one decision per case under its
fingerprint; bulk approval of a filter (preview, start, step) as an apply_auto run with scope.stage='review'. Run history, the
spot-check export and the undo of those runs are the O3 endpoints (/oem-finder/runs/?stage=review, .../spot-check/, .../revert/).
"""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import oem_finder as of
from . import oem_review as rv
from .management import IsSuperuser
from .matching_queue import enqueue_matching
from .oem_finder_views import OEMFinderBusy, RunRow, run_row, runs

ACTIONS = ['approve', 'choose_oem', 'send_to_merge', 'dismiss', 'reopen']
FILTER_PARAMETERS = [
    OpenApiParameter('status', OpenApiTypes.STR, enum=list(rv.STATUSES), default='review'), OpenApiParameter('tier', OpenApiTypes.STR, enum=list(of.REVIEW_TIERS)),
    OpenApiParameter('blocker', OpenApiTypes.STR, description='Bloqueo exacto o su prefijo (no_second_unit, system:MAZDA, unknown:U).'),
    OpenApiParameter('chain', OpenApiTypes.STR, description='Cadena de sufijos exacta (MANDO, G-NP).'),
    OpenApiParameter('make', OpenApiTypes.STR, description='Marca del OEM propuesto (TOYOTA, HYUNDAI, KIA...).'),
    OpenApiParameter('system', OpenApiTypes.STR, description='Sistema de formato (TOYOTA, HMG, MAZDA...).'),
    OpenApiParameter('in_stock', OpenApiTypes.STR, enum=['true', 'false']), OpenApiParameter('search', OpenApiTypes.STR, description='SKU, descripción u OEM.')]


class OEMReviewUnavailable(APIException):
    status_code = 503
    default_detail = 'La revisión OEM aún no está disponible: faltan migraciones por aplicar.'


class OEMReviewConflict(APIException):
    status_code = 409
    default_detail = 'El caso cambió o ya tiene otra decisión. Actualiza la lista antes de continuar.'


class CatalogImportBusy(APIException):
    status_code = 409
    default_detail = 'Hay una importación del catálogo en curso; vuelve a intentarlo cuando termine.'


def require_tables():
    if not rv.review_tables_ready():
        raise OEMReviewUnavailable()


def conflict(error):
    return OEMReviewConflict({'detail': error.detail, **{k: v for k, v in error.extra.items() if v is not None}})


class ReviewChoice(serializers.Serializer):
    key = serializers.CharField()
    code = serializers.CharField(help_text='Forma del fabricante (MAIN).')
    written = serializers.CharField(help_text='Como lo escribe el catálogo (queda como alterno).')
    system = serializers.CharField(allow_blank=True)
    grade = serializers.IntegerField()
    brand = serializers.CharField(allow_blank=True)


class ReviewRow(serializers.Serializer):
    id = serializers.IntegerField()
    part_id = serializers.UUIDField()
    sku = serializers.CharField(help_text='SKU actual.')
    description = serializers.CharField(allow_blank=True)
    evaluated_sku = serializers.CharField(help_text='SKU cuando se analizó.')
    evaluated_description = serializers.CharField(allow_blank=True)
    tier = serializers.ChoiceField(choices=list(of.REVIEW_TIERS))
    tier_label = serializers.CharField()
    underlying_tier = serializers.CharField(allow_blank=True)
    status = serializers.ChoiceField(choices=list(rv.STATUSES))
    fingerprint = serializers.CharField(help_text='Envíalo con la decisión; si el caso cambió, la decisión responde 409.')
    candidate = serializers.CharField(allow_blank=True, help_text='OEM MAIN propuesto (forma del fabricante).')
    written_form = serializers.CharField(allow_blank=True)
    brand = serializers.CharField(allow_blank=True)
    system = serializers.CharField(allow_blank=True)
    grade = serializers.CharField(allow_blank=True)
    chain = serializers.CharField(allow_blank=True)
    chain_tokens = serializers.ListField(child=serializers.DictField(), help_text='sep, tok, token, class, kind, status, auto_eligible.')
    blockers = serializers.ListField(child=serializers.CharField())
    in_stock = serializers.BooleanField()
    available_quantity = serializers.IntegerField()
    method = serializers.ChoiceField(choices=['flag', 'rename'], help_text='flag: el SKU ya es el OEM; rename: el OEM pasa a ser el MAIN.')
    evidence = serializers.DictField(help_text='Léxico, unidades, atribuciones, hermanos, conflictos, advertencias, referencias de empresa, motivos.')
    choices = ReviewChoice(many=True, help_text='MULTI_OEM: los números entre los que se elige.')
    codes = serializers.ListField(child=serializers.DictField(), help_text='Alternos actuales del SKU.')
    supplier_codes = serializers.ListField(child=serializers.DictField(), help_text='Códigos de los proveedores vinculados.')
    stale = serializers.CharField(allow_blank=True, help_text='missing, retired o snapshot_changed: el SKU cambió y no se puede aprobar.')
    stale_label = serializers.CharField(allow_blank=True)
    actions = serializers.ListField(child=serializers.ChoiceField(choices=ACTIONS))
    decision = serializers.DictField()
    decided_by = serializers.CharField(allow_null=True)
    decided_at = serializers.DateTimeField(allow_null=True)


class ReviewListResponse(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = ReviewRow(many=True)
    tiers = serializers.DictField(child=serializers.IntegerField(), help_text='Casos por nivel con los demás filtros.')
    statuses = serializers.DictField(child=serializers.IntegerField())
    facets = serializers.DictField(help_text='blockers, chains, makes, systems: [[valor, casos], ...] con los demás filtros.')
    last_run = serializers.DictField(allow_null=True, help_text='Último análisis OEM: id, finished_at, tiers, scenario, cases.')


class ReviewSummaryResponse(serializers.Serializer):
    statuses = serializers.DictField(child=serializers.IntegerField())
    review = serializers.IntegerField()


class DecisionRequest(serializers.Serializer):
    action = serializers.ChoiceField(choices=ACTIONS)
    fingerprint = serializers.CharField(max_length=64)
    code = serializers.CharField(max_length=120, required=False, allow_blank=True, help_text='choose_oem: el OEM elegido.')
    reason = serializers.ChoiceField(choices=list(rv.DISMISS_REASONS), required=False, help_text='dismiss: el motivo.')
    note = serializers.CharField(max_length=300, required=False, allow_blank=True)


class DecisionResponse(serializers.Serializer):
    case = ReviewRow()
    run = serializers.IntegerField(allow_null=True, help_text='approve/choose_oem: la ejecución que se puede deshacer.')
    change = serializers.IntegerField(allow_null=True)
    family = serializers.DictField(allow_null=True, help_text='send_to_merge: la familia para Agrupar SKU (target, sources...).')


class SampleRow(serializers.Serializer):
    case = serializers.IntegerField()
    sku = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    tier = serializers.CharField()
    code = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    method = serializers.ChoiceField(choices=['flag', 'rename'])
    outcome = serializers.ChoiceField(choices=['applied', 'conflict', 'skipped'])
    detail = serializers.CharField(allow_blank=True)


class BulkPreviewResponse(serializers.Serializer):
    count = serializers.IntegerField(help_text='Casos por revisar con estos filtros.')
    selected = serializers.IntegerField(help_text='Los que se aprobarían en esta ejecución.')
    limit = serializers.IntegerField()
    batch_size = serializers.IntegerField()
    excluded = serializers.DictField(child=serializers.IntegerField(), help_text='tier, stale, previously_reverted, over_limit.')
    tiers = serializers.DictField(child=serializers.IntegerField())
    methods = serializers.DictField(child=serializers.IntegerField())
    sample = SampleRow(many=True)
    selection = serializers.CharField(help_text='Envíalo para empezar; si la selección cambió, responde 409.')
    halt = serializers.DictField(allow_null=True)
    running = serializers.IntegerField(allow_null=True)


class BulkStartRequest(serializers.Serializer):
    tier = serializers.CharField(required=False, allow_blank=True)
    blocker = serializers.CharField(required=False, allow_blank=True)
    chain = serializers.CharField(required=False, allow_blank=True)
    make = serializers.CharField(required=False, allow_blank=True)
    system = serializers.CharField(required=False, allow_blank=True)
    in_stock = serializers.CharField(required=False, allow_blank=True)
    search = serializers.CharField(required=False, allow_blank=True)
    selection = serializers.CharField(max_length=64)


class BulkStepRequest(serializers.Serializer):
    action = serializers.ChoiceField(choices=['continue', 'stop'])


class BulkProgressResponse(serializers.Serializer):
    run = RunRow()
    finished = serializers.BooleanField()
    waiting = serializers.CharField(allow_blank=True, help_text='catalog_import: espera a que termine la importación y continúa.')


def get_case(pk):
    from .oem_finder_models import OEMReviewCase
    try:
        return OEMReviewCase.objects.select_related('decided_by').get(pk=pk)
    except OEMReviewCase.DoesNotExist:
        raise NotFound('El caso no existe.')


def one_row(pk):
    return rv.page_rows([get_case(pk)])[0]


def progress(run, waiting=''):
    run = runs().get(pk=run.pk)
    return {'run': run_row(run), 'finished': run.status != 'running', 'waiting': waiting}


class OEMReviewList(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_review_list', responses=ReviewListResponse,
                   parameters=FILTER_PARAMETERS + [OpenApiParameter('page', OpenApiTypes.INT)],
                   description='Cola de revisión OEM: casos (con existencias primero) con su evidencia, el SKU actual y si cambió desde el análisis, '
                               'más casos por nivel y facetas (bloqueos, cadenas, marcas, sistemas) con los demás filtros.')
    def get(self, request):
        require_tables()
        f = rv.parse_filters(request.query_params)
        paginator = PageNumberPagination()
        paginator.page_size = rv.PAGE_SIZE
        page = paginator.paginate_queryset(rv.cases(f).select_related('decided_by').order_by('-in_stock', 'tier_rank', 'id'), request, view=self)
        rows = rv.page_rows(page)
        return Response({'count': paginator.page.paginator.count, 'next': paginator.get_next_link(), 'previous': paginator.get_previous_link(),
                         'results': rows, **rv.overview(f)})


class OEMReviewSummary(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_review_summary', responses=ReviewSummaryResponse,
                   description='Casos de la revisión OEM por estado (para el contador de la pestaña).')
    def get(self, request):
        require_tables()
        from django.db.models import Count
        from .oem_finder_models import OEMReviewCase
        statuses = dict(OEMReviewCase.objects.values_list('status').annotate(c=Count('id')))
        return Response({'statuses': statuses, 'review': statuses.get('review', 0)})


class OEMReviewDecide(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_review_decide', request=DecisionRequest, responses={
        200: DecisionResponse, 400: OpenApiResponse(description='La acción no corresponde al nivel del caso, o falta el OEM o el motivo.'),
        409: OpenApiResponse(description='El caso o el SKU cambió, ya tiene otra decisión, otro SKU usa el número, la familia es incompatible, '
                                         'hay un análisis de coincidencias o una importación en curso.')},
        description='Decide un caso con su fingerprint. approve marca el SKU como OEM o lo renombra al OEM (mismo SKU interno; el SKU anterior queda '
                    'como alterno) en su propia ejecución, que se puede deshacer; choose_oem elige uno de varios OEM y conserva los demás como alternos; '
                    'send_to_merge envía un conflicto a Agrupar SKU con la familia; dismiss descarta con un motivo; reopen devuelve el caso a la cola. '
                    'Repetir la misma decisión devuelve el caso sin cambios.')
    def post(self, request, pk):
        require_tables()
        data = DecisionRequest(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        get_case(pk)
        run = change = family = None
        try:
            if d['action'] in ('approve', 'choose_oem'):
                if of.import_running():
                    raise CatalogImportBusy()
                with rv.write_lock() as acquired:
                    if not acquired:
                        raise OEMFinderBusy()
                    run, link = rv.approve(pk, d['fingerprint'], request.user, action=d['action'], code=d.get('code') or None)
                change = link.pk if link else None
                if link:
                    enqueue_matching()  # like any catalog edit: the new MAIN and alternos may match pending supplier codes
            elif d['action'] == 'dismiss':
                rv.dismiss(pk, d['fingerprint'], request.user, reason=d.get('reason', ''), note=d.get('note', ''))
            elif d['action'] == 'reopen':
                rv.reopen(pk, d['fingerprint'], request.user)
            else:
                _, family = rv.send_to_merge(pk, d['fingerprint'], request.user)
        except rv.Refused as refused:
            raise ValidationError({refused.field: refused.detail})
        except rv.CaseConflict as error:
            raise conflict(error)
        return Response({'case': one_row(pk), 'run': run, 'change': change, 'family': family})


class OEMReviewBulk(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_review_bulk_preview', responses=BulkPreviewResponse, parameters=FILTER_PARAMETERS,
                   description='Vista previa (solo lectura) de aprobar en lote el filtro: cuántos casos se aprobarían (con existencias primero, '
                               f'hasta {rv.BULK_LIMIT}), los excluidos y por qué, y una muestra con lo que diría el catálogo hoy.')
    def get(self, request):
        require_tables()
        return Response(rv.preview(rv.parse_filters(request.query_params)))

    @extend_schema(operation_id='v1_management_oem_review_bulk_start', request=BulkStartRequest, responses={
        201: BulkProgressResponse, 409: OpenApiResponse(description='La selección cambió, la aplicación está detenida, hay otra aprobación en lote sin terminar '
                                                         'o un análisis de coincidencias en curso.')},
        description='Empieza la aprobación en lote de la selección vista en la vista previa: una ejecución que se procesa por lotes con '
                    'POST bulk/{id}/ y que se puede deshacer completa.')
    def post(self, request):
        require_tables()
        data = BulkStartRequest(data=request.data)
        data.is_valid(raise_exception=True)
        f = rv.parse_filters({**data.validated_data, 'status': 'review'})
        try:
            with rv.write_lock() as acquired:  # one start at a time: two concurrent starts would both see no unfinished run
                if not acquired:
                    raise OEMFinderBusy()
                run = rv.start_bulk(f, data.validated_data['selection'], request.user)
        except rv.CaseConflict as error:
            raise conflict(error)
        return Response(progress(run), status=201)


class OEMReviewBulkStep(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_review_bulk_step', request=BulkStepRequest, responses={
        200: BulkProgressResponse, 409: OpenApiResponse(description='Hay un análisis de coincidencias u otro lote en curso.')},
        description=f'continue procesa el siguiente lote (hasta {rv.BULK_BATCH} casos) con comparación de fingerprint por caso; stop la termina. '
                    'Se detiene sola si se activa la regla de detención o hay errores inesperados.')
    def post(self, request, pk):
        require_tables()
        data = BulkStepRequest(data=request.data)
        data.is_valid(raise_exception=True)
        from .oem_finder_models import OEMFinderRun
        run = OEMFinderRun.objects.filter(pk=pk, mode='apply_auto', scope__stage='review', scope__action='approve_batch').first()
        if run is None:
            raise NotFound('La aprobación en lote no existe.')
        waiting, was_running = '', run.status == 'running'
        with rv.write_lock() as acquired:
            if not acquired:
                raise OEMFinderBusy()
            if data.validated_data['action'] == 'stop':
                run = rv.stop_bulk(pk)
            else:
                run, waiting = rv.step_bulk(pk, request.user)
        if was_running and run.status != 'running' and (run.applied or {}).get('applied'):
            enqueue_matching()  # once, when the run ends: a matching pass between batches would only hold the lock
        return Response(progress(run, waiting))
