"""Agrupación inteligente's API (management/matching and management/matching/<uuid>; the proxy routes are unchanged since S6).

GET lists the matching pass's state and the cases of one status (and, from S6, one kind). POST matching/<uuid> decides a case with its
current fingerprint (409 when it changed): approve and dismiss for every kind, and from S6 separar_referencia (line and variant cases),
merge and keep_apart (merge cases); `line` names a line by id (approve a variant onto it, choose the line of a declared code, the line a
merge keeps), and target_sku is still accepted where a SKU names it. The legacy kinds (supplier, catalog, catalog_reference, seed)
keep their decisions until S10."""
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError, APIException
from rest_framework.response import Response
from rest_framework.views import APIView
from .management import IsSuperuser
from .matching_models import MatchingQueue, MatchingCase, MatchingDecision
from .matching_queue import enqueue_matching, matching_work
from .matching_engine import apply_item, apply_family, part_row
from .line_merge import KEEP_APART_METHODS, keep_apart
from .lines import lines_by_codigo, on_storefront
from .models import CatalogImportJob
from . import reference_matching


class MatchingConflict(APIException):
    status_code = 409
    default_detail = 'Los datos cambiaron. Actualiza el análisis antes de continuar.'


def case_result(case):
    ref = case.reference if case.reference_id else None
    lines = sorted(case.lines.all(), key=lambda line: (line.sku, str(line.pk))) if case.pk else []  # prefetched by the list
    return {'id': str(case.pk), 'kind': case.kind, 'sources': case.sources, 'candidates': case.candidates,
            'target_sku': case.target_sku, 'method': case.method, 'reason': case.reason,
            'status': case.status, 'ai': case.ai, 'fingerprint': case.fingerprint,
            'supplier_name': case.item.supplier.name if case.item_id else '',
            # S6 (additive): the case's Reference, the code and brand it proposes, its lines, evidence, guard and route.
            'reference': {'id': ref.pk, 'code': ref.code, 'brand': ref.brand, 'tipo': ref.tipo, 'estado': ref.estado,
                          'line': str(ref.part_id)} if ref is not None else None,
            'new_code': case.new_code, 'new_brand': case.new_brand,
            'lines': [{'id': str(line.pk), 'sku': line.sku, 'description': line.description, 'estado': line.estado} for line in lines],
            'evidence': case.evidence, 'guard': case.guard, 'route': case.route, 'batch_key': case.batch_key,
            'decided_by': case.decided_by.username if case.decided_by_id else '', 'decided_at': case.decided_at,
            'created_at': case.created_at, 'item_id': str(case.item_id) if case.item_id else None}


CASE_STATUSES = ['review', 'unmatched', 'applied', 'dismissed']
CASE_KINDS = [*reference_matching.KINDS, 'supplier', 'catalog', 'catalog_reference', 'seed']
ACTIONS = ['approve', 'dismiss', 'separar_referencia', 'merge', 'keep_apart']


class MatchingLineResponse(serializers.Serializer):
    id = serializers.UUIDField()
    sku = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    estado = serializers.CharField()


class MatchingCaseResponse(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.CharField(help_text='reference, code_change, variant, line o merge (S6); supplier, catalog, catalog_reference o seed (antes de S6).')
    sources = serializers.ListField(child=serializers.DictField())
    candidates = serializers.ListField(child=serializers.DictField())
    target_sku = serializers.CharField(allow_blank=True)
    method = serializers.CharField(allow_blank=True)
    reason = serializers.CharField(allow_blank=True)
    status = serializers.CharField(help_text='review, applied o dismissed (unmatched y resolved antes de S6).')
    ai = serializers.DictField(help_text='Propuesta de IA en caché (target_sku, confidence, reason) o {error}; vacío si no se analizó.')
    fingerprint = serializers.CharField(help_text='Envíalo al revisar; si los datos cambiaron, la revisión responde 409.')
    supplier_name = serializers.CharField(allow_blank=True)
    reference = serializers.DictField(allow_null=True, help_text='La referencia del caso: id, code, brand, tipo, estado, line.')
    new_code = serializers.CharField(allow_blank=True, help_text='El código que propone un cambio de código o una referencia declarada.')
    new_brand = serializers.CharField(allow_blank=True)
    lines = MatchingLineResponse(many=True, help_text='Las líneas entre las que el caso elige o que unificaría.')
    evidence = serializers.DictField()
    guard = serializers.CharField(allow_blank=True, help_text='El control que detuvo la decisión automática (subgrupo, spec_conflict, disputa...).')
    route = serializers.CharField(allow_blank=True, help_text='lote o caso.')
    batch_key = serializers.CharField(allow_blank=True)
    decided_by = serializers.CharField(allow_blank=True)
    decided_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField(allow_null=True)
    item_id = serializers.UUIDField(allow_null=True)


class MatchingOverviewResponse(serializers.Serializer):
    running = serializers.BooleanField()
    queued = serializers.BooleanField()
    state = serializers.ChoiceField(choices=['running', 'retrying', 'waiting_import', 'queued', 'completed', 'idle'])
    stage = serializers.CharField(allow_blank=True, help_text='catalog, oem, parents, suppliers, proposals, ai, completed, failed o stopped (detenido por una señal o el congelamiento).')
    started_at = serializers.DateTimeField(allow_null=True)
    last_run = serializers.DateTimeField(allow_null=True)
    summary = serializers.DictField(help_text='Totales de la última ejecución.')
    error = serializers.CharField(allow_blank=True)
    counts = serializers.DictField(child=serializers.IntegerField(), help_text='Casos por estado.')
    kinds = serializers.DictField(child=serializers.IntegerField(), help_text='Casos en revisión por tipo (S6).')
    count = serializers.IntegerField()
    offset = serializers.IntegerField()
    next_offset = serializers.IntegerField(allow_null=True)
    results = MatchingCaseResponse(many=True)


class MatchingRun(serializers.Serializer):
    retry_ai = serializers.BooleanField(required=False, help_text='true reintenta solo las consultas de IA fallidas.')


class MatchingRunResponse(serializers.Serializer):
    queued = serializers.BooleanField()
    detail = serializers.CharField()


class MatchingOverview(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_matching_list', responses=MatchingOverviewResponse, parameters=[
        OpenApiParameter('status', OpenApiTypes.STR, enum=CASE_STATUSES, default='review'),
        OpenApiParameter('kind', OpenApiTypes.STR, enum=CASE_KINDS, required=False, description='Solo los casos de este tipo.'),
        OpenApiParameter('offset', OpenApiTypes.INT, default=0, description='Casos a omitir; cada página trae 30 (usa next_offset).')],
        description='Estado del análisis de coincidencias y casos del estado indicado.')
    def get(self, request):
        queue = MatchingQueue.objects.filter(pk=1).first()
        counts = dict(MatchingCase.objects.values('status').annotate(count=Count('pk')).values_list('status', 'count'))
        status = request.query_params.get('status', 'review')
        if status not in CASE_STATUSES:
            raise ValidationError('El estado no es válido.')
        try:
            offset = max(0, int(request.query_params.get('offset', 0)))
        except ValueError:
            raise ValidationError('La página no es válida.')
        cases = MatchingCase.objects.filter(status=status).select_related('item__supplier', 'reference', 'decided_by').prefetch_related('lines')
        kind = request.query_params.get('kind')
        if kind:
            if kind not in CASE_KINDS:
                raise ValidationError('El tipo de caso no es válido.')
            cases = cases.filter(kind=kind)
        count = cases.count()
        kinds = dict(MatchingCase.objects.filter(status='review').values('kind').annotate(count=Count('pk')).values_list('kind', 'count'))
        running = bool(queue and queue.lease and queue.lease_until and queue.lease_until > timezone.now())
        queued = bool(queue and queue.requested > queue.completed)
        waiting_import = queued and not running and CatalogImportJob.objects.filter(status='importing', expires_at__gt=timezone.now()).exists()
        state = ('running' if running else 'retrying' if queued and queue.error else
                 'waiting_import' if waiting_import else 'queued' if queued else 'completed' if queue and queue.finished_at else 'idle')
        return Response({'running': running, 'queued': queued, 'state': state,
            'stage': queue.stage if queue else '', 'started_at': queue.started_at if queue else None,
            'last_run': queue.finished_at if queue else None, 'summary': queue.summary if queue else {},
            'error': queue.error if queue else '', 'counts': counts, 'kinds': kinds, 'count': count, 'offset': offset,
            'next_offset': offset + 30 if offset + 30 < count else None,
            'results': [case_result(case) for case in cases[offset:offset + 30]]})

    @extend_schema(operation_id='v1_management_matching_run', request=MatchingRun, responses={202: MatchingRunResponse},
                   description='Programa el análisis de coincidencias en segundo plano.')
    def post(self, request):
        if request.data.get('retry_ai') is True:
            # Keep valid cached responses; explicitly retry failed provider calls.
            for case in MatchingCase.objects.filter(status='review').exclude(ai={}):
                if case.ai.get('error'):
                    MatchingCase.objects.filter(pk=case.pk, fingerprint=case.fingerprint).update(ai={})
        enqueue_matching()
        return Response({'queued': True, 'detail': 'Análisis programado. Puedes cerrar esta ventana; continuará en segundo plano.'}, status=202)


class ReviewRequest(serializers.Serializer):
    action = serializers.ChoiceField(choices=ACTIONS)
    fingerprint = serializers.CharField(max_length=64)
    target_sku = serializers.CharField(max_length=200, required=False, allow_blank=True)
    line = serializers.UUIDField(required=False, allow_null=True, help_text='La línea elegida, por su id (S6).')


class MatchingReview(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_matching_review', request=ReviewRequest, responses={
        200: MatchingCaseResponse, 409: OpenApiResponse(description='El caso cambió, ya tiene otra decisión, el SKU destino no es válido o hay una importación del proveedor en curso.')},
        description='Decide un caso con su fingerprint actual: approve, dismiss, separar_referencia (casos line y variant), merge y '
                    'keep_apart (casos merge). Repetir la misma decisión devuelve el caso sin cambios.')
    def post(self, request, pk):
        data = ReviewRequest(data=request.data)
        data.is_valid(raise_exception=True)
        data = data.validated_data
        try:
            case = MatchingCase.objects.select_related('item__supplier').get(pk=pk)
        except MatchingCase.DoesNotExist:
            raise NotFound()
        if case.fingerprint != data['fingerprint']:
            raise MatchingConflict()
        if case.kind in reference_matching.KINDS:
            return self.decide(request, case, data)
        if data['action'] not in ('approve', 'dismiss') and not (case.kind == 'catalog' and data['action'] in ('merge', 'keep_apart')):
            raise ValidationError('Esta acción no aplica a este caso.')
        action = {'merge': 'approve', 'keep_apart': 'dismiss'}.get(data['action'], data['action'])
        if case.status in ['applied', 'dismissed']:
            if (action == 'approve' and case.status == 'applied' and (not data.get('target_sku') or data['target_sku'] == case.target_sku)) or (action == 'dismiss' and case.status == 'dismissed'):
                return Response(case_result(case))
            raise MatchingConflict()
        if action == 'dismiss':
            with transaction.atomic():
                locked = MatchingCase.objects.select_for_update().get(pk=case.pk)
                if locked.fingerprint != data['fingerprint'] or locked.status not in ['review', 'unmatched']:
                    raise MatchingConflict()
                locked.status = 'dismissed'
                locked.decided_by, locked.decided_at = request.user, timezone.now()
                locked.save(update_fields=['status', 'decided_by', 'decided_at', 'updated_at'])
                evidence = {'sources': case.sources, 'candidates': case.candidates, 'fingerprint': case.fingerprint}
                if locked.kind == 'catalog' and locked.method in KEEP_APART_METHODS:
                    # Mantener separados (S2b): the lines of a compact_duplicate or shared_oem_number case never merge on their own.
                    lines = [row.get('id') for row in [*case.sources, *case.candidates] if row.get('id')]
                    evidence['separated'] = keep_apart(lines, actor=request.user, case=locked, reason=locked.method)
                MatchingDecision.objects.create(case=locked, actor=request.user, action='dismissed', evidence=evidence)
        else:
            target_sku = data.get('target_sku') or case.target_sku
            try:
                with matching_work():
                    if case.kind == 'catalog_reference':
                        target = next((c for c in case.candidates if c['sku'] == target_sku), None)
                        if not target:
                            raise ValidationError('Selecciona uno de los SKU propuestos por sus referencias.')
                        apply_family(case, actor=request.user, selected_target=target)
                    elif case.kind == 'catalog':
                        if target_sku != case.target_sku:
                            raise ValidationError('Revisa el SKU padre propuesto para esta familia.')
                        apply_family(case, actor=request.user)
                    else:
                        part = chosen_line(data) or next(iter(lines_by_codigo(target_sku)), None)
                        if not on_storefront(part):
                            raise ValidationError('Selecciona un SKU activo del catálogo interno.')
                        target = next((c for c in case.candidates if c['id'] == str(part.pk)), part_row(part))
                        apply_item(case, target, actor=request.user)
            except ValidationError as error:
                raise MatchingConflict(error.detail)
            enqueue_matching()
        case.refresh_from_db()
        return Response(case_result(case))

    def decide(self, request, case, data):
        """An S6 case (mall.reference_matching.decide). The same decision repeated returns the case unchanged."""
        action = data['action']
        if case.status in ['applied', 'dismissed']:
            same = (case.status == 'applied' and action in ('approve', 'merge', 'separar_referencia')) or \
                   (case.status == 'dismissed' and action in ('dismiss', 'keep_apart'))
            if same:
                return Response(case_result(case))
            raise MatchingConflict()
        line = data.get('line')
        if line is None and data.get('target_sku') and case.kind != 'merge':
            found = chosen_line({'target_sku': data['target_sku']})
            if found is None:
                raise MatchingConflict('Selecciona una línea que exista.')
            line = found.pk
        try:
            with matching_work():
                reference_matching.decide(case, action, actor=request.user, line=line)
        except ValidationError as error:
            raise MatchingConflict(error.detail)
        enqueue_matching()
        case.refresh_from_db()
        return Response(case_result(case))


def chosen_line(data):
    """The line a decision names: by its id (line), or by a SKU (target_sku: the catalog line with that codigo first)."""
    from .lines import detail_line
    if data.get('line'):
        try:
            return detail_line(data['line'])
        except Exception:
            return None
    if data.get('target_sku'):
        return next(iter(lines_by_codigo(data['target_sku'])), None)
    return None
