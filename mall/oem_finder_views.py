"""Superuser API of the OEM finder runs (phase 2B): run history, the spot-check export of an apply_auto run (JSON or CSV), undo of a
run or one of its SKU, and the halt rule. Applying runs from the command line only (python -m mall.oem_finder --apply-auto).
"""
import json

from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .management import IsSuperuser
from .matching_queue import enqueue_matching
from .oem_apply import APPLY_TIERS, METHODS, SKIP_LABELS, active_halt, apply_tables_ready, halt, resume, revert_run, spot_check_csv, spot_check_rows
from .oem_finder import matching_lock
from .oem_finder_models import RUN_MODES, RUN_STATUSES, OEMFinderChange, OEMFinderRun

MODES, STATUSES = [m for m, _ in RUN_MODES], [s for s, _ in RUN_STATUSES]


class OEMFinderUnavailable(APIException):
    status_code = 503
    default_detail = 'La aplicación OEM aún no está disponible: faltan migraciones por aplicar.'


class OEMFinderBusy(APIException):
    status_code = 409
    default_detail = 'Hay un análisis de coincidencias o una búsqueda OEM en curso; vuelve a intentarlo cuando termine.'


def require_tables():
    if not apply_tables_ready():
        raise OEMFinderUnavailable()


class HaltRow(serializers.Serializer):
    reason = serializers.CharField()
    run = serializers.IntegerField(allow_null=True)
    created_by = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()


class RunRow(serializers.Serializer):
    id = serializers.IntegerField()
    mode = serializers.ChoiceField(choices=MODES)
    status = serializers.ChoiceField(choices=STATUSES)
    rules_version = serializers.CharField()
    suffix_table_version = serializers.CharField(allow_blank=True)
    scope = serializers.DictField(help_text='tier, limit, canary, in_stock_first, batch_size, stage (review: action, filters, case, sku).')
    tiers = serializers.DictField(help_text='{nivel: [total, con existencias]} de todo el catálogo al ejecutar.')
    applied = serializers.DictField(help_text='apply_auto: applied, conflicts, skipped, errors, tiers, batches, stopped, excluded, selected, spot_check.')
    errors = serializers.ListField(child=serializers.DictField())
    actor = serializers.CharField(allow_null=True)
    started_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)
    changes = serializers.IntegerField(help_text='Cambios de identidad escritos por la ejecución.')
    reverted = serializers.IntegerField(help_text='De ellos, revertidos.')


class RunListResponse(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = RunRow(many=True)
    halt = HaltRow(allow_null=True, help_text='Regla de detención activa; mientras exista no empieza ni continúa ninguna aplicación.')


class ChangeRow(serializers.Serializer):
    id = serializers.IntegerField()
    change = serializers.IntegerField(help_text='CatalogIdentityChange escrito.')
    part_id = serializers.UUIDField()
    previous_sku = serializers.CharField()
    sku = serializers.CharField()
    current_sku = serializers.CharField()
    code = serializers.CharField()
    brand = serializers.CharField()
    tier = serializers.ChoiceField(choices=list(APPLY_TIERS))
    method = serializers.ChoiceField(choices=list(METHODS.values()))
    batch = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    reverted_at = serializers.DateTimeField(allow_null=True)
    reverted_by = serializers.CharField(allow_null=True)


class RunDetailResponse(RunRow):
    count = serializers.IntegerField(help_text='Cambios de la ejecución (paginados de 50 en 50).')
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = ChangeRow(many=True)


class SpotCheckResponse(serializers.Serializer):
    run = serializers.IntegerField()
    columns = serializers.ListField(child=serializers.CharField(), help_text='Encabezados del CSV.')
    rows = serializers.ListField(child=serializers.DictField(), help_text='Filas con la evidencia completa del buscador (evidence).')
    csv = serializers.CharField(help_text='La misma muestra en CSV, con una columna en blanco para anotar si es correcta.')


class RevertRequest(serializers.Serializer):
    part = serializers.UUIDField(required=False, help_text='Solo este SKU; sin él, toda la ejecución.')


class RevertSkipped(serializers.Serializer):
    change = serializers.IntegerField()
    sku = serializers.CharField()
    reason = serializers.CharField()
    label = serializers.CharField()
    detail = serializers.CharField(allow_blank=True)


class RevertResponse(serializers.Serializer):
    run = serializers.IntegerField()
    reverted = serializers.IntegerField()
    already_reverted = serializers.IntegerField()
    skipped = RevertSkipped(many=True)


class HaltRequest(serializers.Serializer):
    action = serializers.ChoiceField(choices=['halt', 'resume'])
    reason = serializers.CharField(max_length=300, required=False, allow_blank=True)


class HaltResponse(serializers.Serializer):
    halt = HaltRow(allow_null=True)


def halt_row(row):
    return row and {'reason': row.reason, 'run': row.run_id, 'created_by': row.created_by.username if row.created_by_id else None,
                    'created_at': row.created_at}


def runs():
    return OEMFinderRun.objects.select_related('actor').annotate(
        n_changes=Count('changes'), n_reverted=Count('changes', filter=Q(changes__reverted_at__isnull=False))).order_by('-started_at', '-id')


def run_row(run):
    return {'id': run.pk, 'mode': run.mode, 'status': run.status, 'rules_version': run.rules_version, 'suffix_table_version': run.suffix_table_version,
            'scope': {k: v for k, v in (run.scope or {}).items() if k != 'cases'}, 'tiers': (run.counts or {}).get('tiers', {}), 'applied': run.applied, 'errors': run.errors,
            'actor': run.actor.username if run.actor_id else None, 'started_at': run.started_at, 'finished_at': run.finished_at,
            'changes': run.n_changes, 'reverted': run.n_reverted}


def change_row(link):
    return {'id': link.pk, 'change': link.change_id, 'part_id': link.part_id, 'previous_sku': link.change.previous_sku, 'sku': link.change.sku,
            'current_sku': link.part.sku, 'code': link.code, 'brand': link.brand, 'tier': link.tier, 'method': link.method, 'batch': link.batch,
            'created_at': link.created_at, 'reverted_at': link.reverted_at, 'reverted_by': link.reverted_by.username if link.reverted_by_id else None}


class OEMRunList(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_finder_runs_list', responses=RunListResponse, parameters=[
        OpenApiParameter('mode', OpenApiTypes.STR, enum=MODES), OpenApiParameter('page', OpenApiTypes.INT),
        OpenApiParameter('stage', OpenApiTypes.STR, enum=['auto', 'review'], description='review: aprobaciones de la Revisión OEM; auto: las demás.')],
        description='Historial de ejecuciones del buscador OEM (las más recientes primero) y la regla de detención activa.')
    def get(self, request):
        require_tables()
        rows = runs()
        mode, stage = request.query_params.get('mode'), request.query_params.get('stage')
        if mode:
            if mode not in MODES:
                raise ValidationError({'mode': 'El filtro no es válido.'})
            rows = rows.filter(mode=mode)
        if stage:
            if stage not in ('auto', 'review'):
                raise ValidationError({'stage': 'El filtro no es válido.'})
            review = Q(scope__stage='review')
            rows = rows.filter(review) if stage == 'review' else rows.filter(~review | Q(scope__stage__isnull=True))
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return Response({'count': paginator.page.paginator.count, 'next': paginator.get_next_link(), 'previous': paginator.get_previous_link(),
                         'results': [run_row(r) for r in page], 'halt': halt_row(active_halt())})


class OEMRunDetail(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_finder_run_detail', responses=RunDetailResponse,
                   parameters=[OpenApiParameter('page', OpenApiTypes.INT)],
                   description='Una ejecución con sus cambios de identidad (SKU anterior y nuevo, OEM, nivel, lote y si se revirtió).')
    def get(self, request, pk):
        require_tables()
        run = get_object_or_404(runs(), pk=pk)
        paginator = PageNumberPagination()
        paginator.page_size = 50
        links = OEMFinderChange.objects.filter(run=run).select_related('change', 'part', 'reverted_by').order_by('batch', 'pk')
        page = paginator.paginate_queryset(links, request, view=self)
        return Response({**run_row(run), 'count': paginator.page.paginator.count, 'next': paginator.get_next_link(),
                         'previous': paginator.get_previous_link(), 'results': [change_row(link) for link in page]})


class OEMRunSpotCheck(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_finder_run_spot_check', responses={200: SpotCheckResponse}, parameters=[
        OpenApiParameter('download', OpenApiTypes.STR, enum=['csv', 'json'], description='Descarga la muestra como archivo adjunto.')],
        description='Muestra de control de una ejecución apply_auto: 20 filas aplicadas al azar con su evidencia. Si más de una es '
                    'incorrecta, activa la regla de detención y deshaz la ejecución.')
    def get(self, request, pk):
        require_tables()
        run = get_object_or_404(OEMFinderRun, pk=pk, mode='apply_auto')
        rows = spot_check_rows(run)
        download = request.query_params.get('download', '')
        if download not in ('', 'csv', 'json'):
            raise ValidationError({'download': 'Usa csv o json.'})
        if download:
            # The BOM lets Excel read the accents of the descriptions (UTF-8).
            body = '\ufeff' + spot_check_csv(rows) if download == 'csv' else json.dumps({'run': run.pk, 'rows': rows}, ensure_ascii=False, default=str)
            response = HttpResponse(body, content_type='text/csv; charset=utf-8' if download == 'csv' else 'application/json; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="oem-run-{run.pk}-spot-check.{download}"'
            return response
        from .oem_apply import SPOT_COLUMNS
        return Response({'run': run.pk, 'columns': [header for _, header in SPOT_COLUMNS], 'rows': rows, 'csv': spot_check_csv(rows)})


class OEMRunRevert(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_finder_run_revert', request=RevertRequest, responses={
        200: RevertResponse, 409: OpenApiResponse(description='Hay un análisis de coincidencias o una búsqueda OEM en curso.')},
        description='Deshace la ejecución (o un SKU de ella): devuelve el SKU anterior si está libre, borra el OEM y los alternos que '
                    'creó, quita la marca OEM y registra el cambio inverso. Repetirlo no cambia nada.')
    def post(self, request, pk):
        require_tables()
        run = get_object_or_404(OEMFinderRun, pk=pk, mode='apply_auto')
        data = RevertRequest(data=request.data)
        data.is_valid(raise_exception=True)
        part = data.validated_data.get('part')
        if part and not run.changes.filter(part_id=part).exists():
            raise ValidationError({'part': 'Ese SKU no se aplicó en esta ejecución.'})
        with matching_lock() as acquired:
            if not acquired:
                raise OEMFinderBusy()
            result = revert_run(run.pk, actor=request.user, part=part)
        if result['reverted']:
            enqueue_matching()
        result['skipped'] = [{**row, 'label': SKIP_LABELS.get(row['reason'], row['reason'])} for row in result['skipped']]
        return Response(result)


class OEMApplyHaltView(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_finder_halt', request=HaltRequest, responses=HaltResponse,
                   description='halt activa la regla de detención (con un motivo): ninguna aplicación automática empieza y la que corre se '
                               'detiene antes de su siguiente lote. resume la levanta.')
    def post(self, request):
        require_tables()
        data = HaltRequest(data=request.data)
        data.is_valid(raise_exception=True)
        if data.validated_data['action'] == 'halt':
            reason = data.validated_data.get('reason', '').strip()
            if not reason:
                raise ValidationError({'reason': 'Indica el motivo de la detención.'})
            halt(reason, actor=request.user)
        else:
            resume(actor=request.user)
        return Response({'halt': halt_row(active_halt())})
