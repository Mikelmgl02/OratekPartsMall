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
from .models import Part, CatalogImportJob


class MatchingConflict(APIException):
    status_code = 409
    default_detail = 'Los datos cambiaron. Actualiza el análisis antes de continuar.'


def case_result(case):
    return {'id': str(case.pk), 'kind': case.kind, 'sources': case.sources, 'candidates': case.candidates,
            'target_sku': case.target_sku, 'method': case.method, 'reason': case.reason,
            'status': case.status, 'ai': case.ai, 'fingerprint': case.fingerprint,
            'supplier_name': case.item.supplier.name if case.item_id else ''}


CASE_STATUSES = ['review', 'unmatched', 'applied', 'dismissed']


class MatchingCaseResponse(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.CharField(help_text='supplier, catalog, catalog_reference o seed.')
    sources = serializers.ListField(child=serializers.DictField())
    candidates = serializers.ListField(child=serializers.DictField())
    target_sku = serializers.CharField(allow_blank=True)
    method = serializers.CharField(allow_blank=True)
    reason = serializers.CharField(allow_blank=True)
    status = serializers.CharField(help_text='review, unmatched, applied, dismissed o resolved.')
    ai = serializers.DictField(help_text='Propuesta de IA en caché (target_sku, confidence, reason) o {error}; vacío si no se analizó.')
    fingerprint = serializers.CharField(help_text='Envíalo al revisar; si los datos cambiaron, la revisión responde 409.')
    supplier_name = serializers.CharField(allow_blank=True)


class MatchingOverviewResponse(serializers.Serializer):
    running = serializers.BooleanField()
    queued = serializers.BooleanField()
    state = serializers.ChoiceField(choices=['running', 'retrying', 'waiting_import', 'queued', 'completed', 'idle'])
    stage = serializers.CharField(allow_blank=True, help_text='catalog, oem, parents, suppliers, ai, completed o failed.')
    started_at = serializers.DateTimeField(allow_null=True)
    last_run = serializers.DateTimeField(allow_null=True)
    summary = serializers.DictField(help_text='Totales de la última ejecución.')
    error = serializers.CharField(allow_blank=True)
    counts = serializers.DictField(child=serializers.IntegerField(), help_text='Casos por estado.')
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
        cases = MatchingCase.objects.filter(status=status).select_related('item__supplier')
        count = cases.count()
        running = bool(queue and queue.lease and queue.lease_until and queue.lease_until > timezone.now())
        queued = bool(queue and queue.requested > queue.completed)
        waiting_import = queued and not running and CatalogImportJob.objects.filter(status='importing', expires_at__gt=timezone.now()).exists()
        state = ('running' if running else 'retrying' if queued and queue.error else
                 'waiting_import' if waiting_import else 'queued' if queued else 'completed' if queue and queue.finished_at else 'idle')
        return Response({'running': running, 'queued': queued, 'state': state,
            'stage': queue.stage if queue else '', 'started_at': queue.started_at if queue else None,
            'last_run': queue.finished_at if queue else None, 'summary': queue.summary if queue else {},
            'error': queue.error if queue else '', 'counts': counts, 'count': count, 'offset': offset,
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
    action = serializers.ChoiceField(choices=['approve', 'dismiss'])
    fingerprint = serializers.CharField(max_length=64)
    target_sku = serializers.CharField(max_length=200, required=False, allow_blank=True)


class MatchingReview(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_matching_review', request=ReviewRequest, responses={
        200: MatchingCaseResponse, 409: OpenApiResponse(description='El caso cambió, ya tiene otra decisión, el SKU destino no es válido o hay una importación del proveedor en curso.')},
        description='Aprueba o descarta un caso con su fingerprint actual. Repetir la misma decisión devuelve el caso sin cambios.')
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
        if case.status in ['applied', 'dismissed']:
            if (data['action'] == 'approve' and case.status == 'applied' and (not data.get('target_sku') or data['target_sku'] == case.target_sku)) or (data['action'] == 'dismiss' and case.status == 'dismissed'):
                return Response(case_result(case))
            raise MatchingConflict()
        if data['action'] == 'dismiss':
            with transaction.atomic():
                locked = MatchingCase.objects.select_for_update().get(pk=case.pk)
                if locked.fingerprint != data['fingerprint'] or locked.status not in ['review', 'unmatched']:
                    raise MatchingConflict()
                locked.status = 'dismissed'
                locked.save(update_fields=['status', 'updated_at'])
                MatchingDecision.objects.create(case=locked, actor=request.user, action='dismissed',
                    evidence={'sources': case.sources, 'candidates': case.candidates, 'fingerprint': case.fingerprint})
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
                        part = Part.objects.filter(sku=target_sku, active=True, merged_into__isnull=True).first()
                        if not part:
                            raise ValidationError('Selecciona un SKU activo del catálogo interno.')
                        target = next((c for c in case.candidates if c['id'] == str(part.pk)), part_row(part))
                        apply_item(case, target, actor=request.user)
            except ValidationError as error:
                raise MatchingConflict(error.detail)
            enqueue_matching()
        case.refresh_from_db()
        return Response(case_result(case))
