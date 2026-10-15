"""The Matching Cases queue's API (inventory refactor S7, plan §3 S7; superusers only). The cases are the ones Agrupación inteligente
reads and decides at management/matching (mall.matching_views, kept for the S6.3 cards until S8 replaces them); these routes read them by
kind, status, route, batch, line or Reference, decide one with its fingerprint, and decide a lote batch in one request.

    cases/                  GET kind, status (review by default), route, batch, line (a line id: the case's lines, its item's line or its
                            Reference's line), reference (a Reference id); paginated, newest first
    cases/<id>/             GET one case; POST {action, fingerprint, line, target_sku}: as management/matching/<id> (409 when the case
                            changed or the decision cannot run)
    cases/batches/          GET every batch (batch_key) with its counts by status and its cases in review by kind, the most in review
                            first
    cases/batches/<key>/    GET the batch: its counts by status and kind and its cases in review; POST {action, cases: [{id,
                            fingerprint}]}: each case decided on its own (one failing does not stop the others), at most MAX_BATCH
The case JSON is mall.matching_views.case_result's (MatchingCaseResponse)."""
import logging

from django.db.models import Count, Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import reference_matching
from .line_views import ErrorBodies
from .management import IsSuperuser
from .matching_models import MatchingCase
from .matching_views import ACTIONS, CASE_KINDS, CASE_STATUSES, MatchingCaseResponse, MatchingConflict, MatchingReview, ReviewRequest, case_result

logger = logging.getLogger(__name__)

MAX_BATCH = 200
BATCH_KEY = r'[A-Za-z0-9_.:-]{1,120}'


def cases_queryset():
    return MatchingCase.objects.select_related('item__supplier', 'reference', 'decided_by').prefetch_related('lines')


class CasePagination(PageNumberPagination):
    page_size = 30


class CaseListResponse(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    kinds = serializers.DictField(child=serializers.IntegerField(), help_text='Casos del estado pedido por tipo (sin el filtro de tipo).')
    results = MatchingCaseResponse(many=True)


class CaseList(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_cases_list', responses=CaseListResponse, parameters=[
        OpenApiParameter('kind', OpenApiTypes.STR, enum=CASE_KINDS),
        OpenApiParameter('status', OpenApiTypes.STR, enum=CASE_STATUSES, default='review'),
        OpenApiParameter('route', OpenApiTypes.STR, enum=['lote', 'caso']),
        OpenApiParameter('batch', OpenApiTypes.STR, description='El lote (batch_key).'),
        OpenApiParameter('line', OpenApiTypes.UUID, description='Los casos de esta línea: sus líneas, la de su artículo o la de su referencia.'),
        OpenApiParameter('reference', OpenApiTypes.INT, description='Los casos de esta referencia.'),
        OpenApiParameter('page', OpenApiTypes.INT)],
        description='Los casos de coincidencia por tipo, estado, ruta, lote, línea o referencia (S7).')
    def get(self, request):
        params = request.query_params
        status = params.get('status', 'review')
        if status not in CASE_STATUSES:
            raise ValidationError({'status': 'El estado no es válido.'})
        cases = cases_queryset().filter(status=status)
        route = params.get('route')
        if route:
            if route not in dict(MatchingCase.ROUTES):
                raise ValidationError({'route': 'Usa lote o caso.'})
            cases = cases.filter(route=route)
        if params.get('batch'):
            cases = cases.filter(batch_key=params['batch'])
        if params.get('line'):
            import uuid
            try:
                line = uuid.UUID(params['line'])
            except ValueError:
                raise ValidationError({'line': 'Usa el id de una línea.'})
            cases = cases.filter(Q(lines=line) | Q(item__part_id=line) | Q(reference__part_id=line)).distinct()
        if params.get('reference'):
            try:
                cases = cases.filter(reference_id=int(params['reference']))
            except ValueError:
                raise ValidationError({'reference': 'Usa el id de una referencia.'})
        kinds = dict(cases.order_by().values('kind').annotate(n=Count('pk', distinct=True)).values_list('kind', 'n'))
        kind = params.get('kind')
        if kind:
            if kind not in CASE_KINDS:
                raise ValidationError({'kind': 'El tipo de caso no es válido.'})
            cases = cases.filter(kind=kind)
        paginator = CasePagination()
        page = paginator.paginate_queryset(cases.order_by('-updated_at', 'id'), request, view=self)
        response = paginator.get_paginated_response([case_result(case) for case in page])
        response.data['kinds'] = kinds
        return response


class CaseDetail(ErrorBodies, MatchingReview):
    @extend_schema(operation_id='v1_management_cases_retrieve', responses=MatchingCaseResponse, description='Un caso de coincidencia.')
    def get(self, request, pk):
        case = cases_queryset().filter(pk=pk).first()
        if case is None:
            raise NotFound()
        return Response(case_result(case))

    @extend_schema(operation_id='v1_management_cases_decide', request=ReviewRequest, responses={
        200: MatchingCaseResponse, 409: OpenApiResponse(description='El caso cambió, ya tiene otra decisión o la decisión no puede aplicarse.')},
        description='Decide el caso con su fingerprint actual, como management/matching/<id>.')
    def post(self, request, pk):
        return super().post(request, pk)


class CaseBatchItem(serializers.Serializer):
    id = serializers.UUIDField()
    fingerprint = serializers.CharField(max_length=64)


class CaseBatchRequest(serializers.Serializer):
    action = serializers.ChoiceField(choices=ACTIONS)
    cases = CaseBatchItem(many=True, help_text=f'Los casos del lote que la persona vio, con su fingerprint (hasta {MAX_BATCH}).')


class CaseBatchFailure(serializers.Serializer):
    id = serializers.UUIDField()
    detail = serializers.CharField()


class CaseBatchResponse(serializers.Serializer):
    batch_key = serializers.CharField()
    decided = serializers.ListField(child=serializers.UUIDField())
    failed = CaseBatchFailure(many=True)
    results = MatchingCaseResponse(many=True, help_text='Los casos decididos, como quedaron.')


class CaseBatchSummary(serializers.Serializer):
    batch_key = serializers.CharField()
    counts = serializers.DictField(child=serializers.IntegerField(), help_text='Casos del lote por estado.')
    kinds = serializers.DictField(child=serializers.IntegerField(), help_text='Casos en revisión del lote por tipo.')
    count = serializers.IntegerField(help_text='Casos en revisión.')
    results = MatchingCaseResponse(many=True, help_text=f'Los primeros {MAX_BATCH} casos en revisión.')


class CaseBatchIndexRow(serializers.Serializer):
    batch_key = serializers.CharField()
    counts = serializers.DictField(child=serializers.IntegerField(), help_text='Casos del lote por estado.')
    kinds = serializers.DictField(child=serializers.IntegerField(), help_text='Casos en revisión del lote por tipo.')
    count = serializers.IntegerField(help_text='Casos en revisión.')
    created_at = serializers.DateTimeField(allow_null=True, help_text='El caso más antiguo del lote.')


class CaseBatchIndex(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_cases_batches_list', responses=CaseBatchIndexRow(many=True),
                   description='Los lotes de casos (batch_key) con sus totales por estado y sus casos en revisión por tipo; los que más '
                               'casos en revisión tienen primero.')
    def get(self, request):
        from django.db.models import Min
        rows = {}
        for key, status, kind, n, first in (MatchingCase.objects.exclude(batch_key='').order_by().values('batch_key', 'status', 'kind')
                                            .annotate(n=Count('pk'), first=Min('created_at'))
                                            .values_list('batch_key', 'status', 'kind', 'n', 'first')):
            row = rows.setdefault(key, {'batch_key': key, 'counts': {}, 'kinds': {}, 'count': 0, 'created_at': first})
            row['counts'][status] = row['counts'].get(status, 0) + n
            if status == 'review':
                row['kinds'][kind] = row['kinds'].get(kind, 0) + n
                row['count'] += n
            if first is not None and (row['created_at'] is None or first < row['created_at']):
                row['created_at'] = first
        ordered = sorted(rows.values(), key=lambda row: (-row['count'], row['batch_key']))
        return Response(CaseBatchIndexRow(ordered, many=True).data)


def message(error):
    detail = getattr(error, 'detail', None) or str(error)
    if isinstance(detail, dict):
        detail = ' '.join(str(value) for value in detail.values())
    elif isinstance(detail, list):
        detail = ' '.join(str(value) for value in detail)
    return str(detail)


class CaseBatch(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_cases_batch_retrieve', responses=CaseBatchSummary,
                   description='Un lote de casos (route lote): sus totales y sus casos en revisión.')
    def get(self, request, key):
        cases = MatchingCase.objects.filter(batch_key=key)
        counts = dict(cases.order_by().values('status').annotate(n=Count('pk')).values_list('status', 'n'))
        if not counts:
            raise NotFound('No existe ese lote.')
        review = cases_queryset().filter(batch_key=key, status='review')
        kinds = dict(review.order_by().values('kind').annotate(n=Count('pk')).values_list('kind', 'n'))
        rows = list(review.order_by('created_at', 'id')[:MAX_BATCH])
        return Response({'batch_key': key, 'counts': counts, 'kinds': kinds, 'count': sum(kinds.values()),
                         'results': [case_result(case) for case in rows]})

    @extend_schema(operation_id='v1_management_cases_batch_decide', request=CaseBatchRequest, responses=CaseBatchResponse,
                   description='Decide los casos del lote que se enviaron, cada uno con su fingerprint: uno que cambió o no puede '
                               'aplicarse queda en failed con el motivo, y los demás siguen.')
    def post(self, request, key):
        from .matching_queue import enqueue_matching, matching_work
        data = CaseBatchRequest(data=request.data)
        data.is_valid(raise_exception=True)
        data = data.validated_data
        if not data['cases']:
            raise ValidationError({'cases': 'Envía los casos del lote que quieres decidir.'})
        if len(data['cases']) > MAX_BATCH:
            raise ValidationError({'cases': f'Decide hasta {MAX_BATCH} casos por envío.'})
        wanted = {str(row['id']): row['fingerprint'] for row in data['cases']}
        found = {str(case.pk): case for case in MatchingCase.objects.filter(pk__in=list(wanted), batch_key=key)}
        decided, failed = [], []
        for pk, fingerprint in wanted.items():
            case = found.get(pk)
            if case is None:
                failed.append({'id': pk, 'detail': 'El caso no es de este lote.'})
                continue
            if case.fingerprint != fingerprint:
                failed.append({'id': pk, 'detail': MatchingConflict.default_detail})
                continue
            if case.kind not in reference_matching.KINDS:
                failed.append({'id': pk, 'detail': 'Este caso es anterior a S6: decídelo por separado en Casos de coincidencia.'})
                continue
            if case.status != 'review':
                failed.append({'id': pk, 'detail': 'El caso ya tiene una decisión.'})
                continue
            try:
                with matching_work():
                    reference_matching.decide(case, data['action'], actor=request.user)
            except ValidationError as error:
                failed.append({'id': pk, 'detail': message(error)})
                continue
            except Exception as error:  # each case is its own transaction: the ones decided stand and the rest go on
                logger.exception('Batch %s: case %s could not be decided.', key, pk)
                failed.append({'id': pk, 'detail': f'No se pudo decidir el caso ({type(error).__name__}): vuelve a cargarlo.'})
                continue
            decided.append(pk)
        if decided:
            enqueue_matching()
        results = [case_result(case) for case in cases_queryset().filter(pk__in=decided).order_by('created_at', 'id')]
        return Response({'batch_key': key, 'decided': decided, 'failed': failed, 'results': results})
