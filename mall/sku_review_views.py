"""Revisión IA of Productos nuevos (superusers): start a run, review it batch by batch, read its report, and list the products a batch
action can decide. The decisions themselves go through catalog/proposals/bulk/ (approve, reject, link). From inventory refactor S6 a
product is a line in preparación (mall.new_products) and a verdict hangs from its line; the JSON keeps its keys (proposal is the line's
id, current its state as the Productos nuevos grid shows it)."""
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Prefetch, Subquery
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import sku_review
from .ai_budget import feature_budget, month_spend
from .catalog_classification import ClassificationConflict, provider_configuration
from .management import IsSuperuser, UppercaseCharField
from .models import Reference, SupplierItem
from .sku_proposal_models import SkuProposal
from .sku_proposal_views import STATUS_LABELS, codes_of, status_for
from .sku_review_models import SkuReviewRun
from .technical_data import grupo_nombre

PAGE = 50
RECENT_RUNS = 10
VERDICT_FILTERS = ('good', 'bad', 'unsure', 'stale', 'decided')


def line_status(verdict):
    """The status the verdict's line has in Productos nuevos ('' when the verdict has no line: a proposal that never became one)."""
    line = verdict.line
    if line is None:
        return ''
    with_items = getattr(verdict, 'line_has_items', None)
    if with_items is None:
        with_items = SupplierItem.objects.filter(part_id=line.pk).exists()
    return status_for(line.estado, line.origen, with_items)


def state(verdict):
    """fresh (the verdict holds), stale (the line changed since), superseded (a newer run reviewed it again) or decided (approved,
    linked, rejected or resolved since). Only fresh verdicts feed the batch actions."""
    if line_status(verdict) != 'pending':
        return 'decided'
    if verdict.proposal_version != verdict.line.version:
        return 'stale'
    return 'fresh' if getattr(verdict, 'latest_id', verdict.pk) == verdict.pk else 'superseded'


def run_summary(run):
    total = sku_review.total_batches(run)
    return {'id': str(run.pk), 'search': run.search, 'only_unreviewed': run.only_unreviewed, 'created_by': run.created_by.username,
            'created_at': run.created_at, 'finished_at': run.finished_at, 'model': run.model, 'metrics': run.metrics,
            'total_items': len(run.proposal_ids), 'batch_size': run.batch_size, 'completed_batches': run.completed_batches, 'total_batches': total,
            'status': 'running' if sku_review.claim_active(run) else 'completed' if run.completed_batches >= total else 'ready'}


def oem_json(oem):
    return {'code': oem.code, 'brand': oem.brand} if oem is not None else None


def current(verdict):
    """The verdict's line as the Productos nuevos grid shows it now."""
    line, status = verdict.line, line_status(verdict)
    if line is None:
        return {'version': None, 'status': 'resolved', 'status_label': STATUS_LABELS['resolved'], 'sku': verdict.evidence.get('sku', ''),
                'description': verdict.evidence.get('descripcion', ''), 'brand': verdict.evidence.get('marca', ''), 'internal_code': False,
                'oem': None, 'part_type': None, 'field_sources': {}, 'part': None}
    main, oem, _ = codes_of(line)
    part_type = line.part_type
    part = {'id': str(line.pk), 'sku': line.sku} if status == 'approved' else (
        {'id': str(line.merged_into_id), 'sku': line.merged_into.sku} if status == 'linked' and line.merged_into_id else None)
    return {'version': line.version, 'status': status, 'status_label': STATUS_LABELS.get(status, ''), 'sku': line.sku,
            'description': line.description, 'brand': main.brand if main is not None else '',
            'internal_code': main is not None and main.tipo == 'interno', 'oem': oem_json(oem),
            'part_type': {'id': str(part_type.pk), 'category': grupo_nombre(part_type), 'name': part_type.name} if part_type else None,
            'field_sources': line.field_sources or {}, 'part': part}


def verdict_json(verdict):
    return {
        'id': str(verdict.pk), 'proposal': str(verdict.line_id or verdict.proposal_id), 'state': state(verdict), 'method': verdict.method,
        'verdict': verdict.verdict, 'reason': verdict.reason, 'reason_label': dict(SkuProposal.REJECT_REASONS).get(verdict.reason, ''),
        'confidence': verdict.confidence, 'reasons': verdict.reasons, 'suggestions': verdict.suggestions, 'applied': verdict.applied,
        'reviewed_version': verdict.proposal_version,
        'link_part': {'id': str(verdict.link_part_id), 'sku': verdict.link_part.sku} if verdict.link_part_id else None,
        'current': current(verdict),
        'supplier_description': next(iter(verdict.evidence.get('descripciones_de_proveedores') or []), ''),
        'stock': verdict.evidence.get('existencias', 0), 'signals': verdict.evidence.get('senales', []),
    }


def verdicts(run):
    latest = sku_review.given().filter(line=OuterRef('line')).order_by('-created_at', '-pk').values('pk')[:1]
    codes = Prefetch('line__codes', to_attr='live_codes', queryset=Reference.objects.order_by('pk'))
    return (run.verdicts.select_related('line__part_type__grupo', 'line__merged_into', 'link_part').prefetch_related(codes)
            .annotate(latest_id=Subquery(latest), line_has_items=Exists(SupplierItem.objects.filter(part_id=OuterRef('line_id')))))


def counts(rows):
    """Per report tab, plus what the batch actions would take: bad ones to reject (not the ones the catalog already has) and to link."""
    result = {'good': 0, 'bad': 0, 'unsure': 0, 'stale': 0, 'decided': 0, 'link': 0, 'reject': 0}
    for verdict in rows:
        where = state(verdict)
        if where != 'fresh':
            result['decided' if where == 'decided' else 'stale'] += 1
            continue
        result[verdict.verdict] += 1
        if verdict.verdict == 'bad' and verdict.link_part_id:
            result['link'] += 1
        elif verdict.verdict == 'bad' and verdict.reason != 'duplicate':
            result['reject'] += 1
    return result


def selected(rows, wanted):
    """The verdicts of one report tab: fresh ones by verdict, or the stale or decided ones."""
    if wanted == 'stale':
        return [verdict for verdict in rows if state(verdict) in ('stale', 'superseded')]
    if wanted == 'decided':
        return [verdict for verdict in rows if state(verdict) == 'decided']
    return [verdict for verdict in rows if state(verdict) == 'fresh' and verdict.verdict == wanted]


def report(run, wanted='good', offset=0):
    every = list(verdicts(run))
    rows = selected(every, wanted)
    return {**run_summary(run), 'counts': counts(every), 'verdict': wanted, 'offset': offset, 'count': len(rows),
            'next_offset': offset + PAGE if len(rows) > offset + PAGE else None,
            'results': [verdict_json(verdict) for verdict in rows[offset:offset + PAGE]]}


def actionable(run, wanted):
    """Every fresh line waiting for a decision of a verdict, with the version the batch action must send: approve the good ones,
    reject the bad ones with their reason, link the bad ones the catalog already has."""
    rows = selected(list(verdicts(run)), 'good' if wanted == 'good' else 'bad')
    if wanted == 'link':
        rows = [verdict for verdict in rows if verdict.link_part_id]
    elif wanted == 'bad':  # the ones the catalog already has are linked or decided one by one, never rejected in batch
        rows = [verdict for verdict in rows if verdict.reason != 'duplicate']
    results = []
    for verdict in rows:
        main, oem, _ = codes_of(verdict.line)
        results.append({'id': str(verdict.line_id), 'version': verdict.proposal_version, 'sku': verdict.line.sku,
                        'description': verdict.line.description, 'reason': verdict.reason,
                        'note': ('Revisión IA: ' + ' '.join(verdict.reasons))[:2000],
                        'internal_code': main is not None and main.tipo == 'interno', 'oem': oem_json(oem),
                        'part': str(verdict.link_part_id) if verdict.link_part_id else None})
    return {'verdict': wanted, 'count': len(results), 'results': results}


class ReviewStart(serializers.Serializer):
    id = serializers.UUIDField()
    search = UppercaseCharField(max_length=200, allow_blank=True, default='')
    only_unreviewed = serializers.BooleanField(default=True)


class ReviewAction(serializers.Serializer):
    mode = serializers.ChoiceField(choices=['review'])
    batch_index = serializers.IntegerField(min_value=0)


class ReviewList(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_proposals_review_list', responses=OpenApiTypes.OBJECT,
                   parameters=[OpenApiParameter('search', OpenApiTypes.STR)])
    def get(self, request):
        search = (request.query_params.get('search') or '').strip().upper()
        if len(search) > 200:
            raise ValidationError({'search': 'La búsqueda es demasiado larga.'})
        rows = sku_review.eligible(search)
        return Response({'configured': bool(provider_configuration()[0]), 'cap_micro_usd': feature_budget(sku_review.FEATURE),
                         'month_spend_micro_usd': month_spend(sku_review.FEATURE), 'eligible_count': rows.count(),
                         'unreviewed_count': rows.exclude(pk__in=sku_review.fresh_ids()).count(),
                         'runs': [run_summary(run) for run in SkuReviewRun.objects.select_related('created_by')[:RECENT_RUNS]]})

    @extend_schema(operation_id='v1_management_catalog_proposals_review_create', request=ReviewStart, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        serializer = ReviewStart(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        existing = SkuReviewRun.objects.select_related('created_by').filter(pk=data['id']).first()
        if existing:
            return self.same(existing, data)
        rows = sku_review.eligible(data['search'])
        if data['only_unreviewed']:
            rows = rows.exclude(pk__in=sku_review.fresh_ids())
        ids = [str(pk) for pk in rows.values_list('pk', flat=True)]
        if not ids:
            raise ValidationError({'detail': 'No hay productos nuevos por revisar con ese filtro.'})
        try:
            with transaction.atomic():
                run = SkuReviewRun.objects.create(id=data['id'], created_by=request.user, search=data['search'], only_unreviewed=data['only_unreviewed'],
                                                  proposal_ids=ids, batch_size=sku_review.BATCH_SIZE, model=provider_configuration()[1])
        except IntegrityError:
            return self.same(SkuReviewRun.objects.select_related('created_by').get(pk=data['id']), data)
        return Response(report(run), status=201)

    def same(self, run, data):
        if run.search != data['search'] or run.only_unreviewed != data['only_unreviewed']:
            raise ClassificationConflict('El identificador ya pertenece a otra revisión.')
        return Response(report(run))


class ReviewDetail(APIView):
    permission_classes = [IsSuperuser]

    def run(self, pk):
        run = SkuReviewRun.objects.select_related('created_by').filter(pk=pk).first()
        if run is None:
            raise NotFound('No existe esa revisión.')
        return run

    def page(self, request):
        wanted = request.query_params.get('verdict') or 'good'
        if wanted not in VERDICT_FILTERS:
            raise ValidationError({'verdict': 'Usa good, bad, unsure, stale o decided.'})
        try:
            offset = int(request.query_params.get('offset') or 0)
        except ValueError:
            offset = -1
        if offset < 0 or offset % PAGE:
            raise ValidationError({'offset': 'La página solicitada no es válida.'})
        return wanted, offset

    @extend_schema(operation_id='v1_management_catalog_proposals_review_retrieve', responses=OpenApiTypes.OBJECT, parameters=[
        OpenApiParameter('verdict', OpenApiTypes.STR, description='good, bad, unsure, stale o decided; con mode=actionable: good, bad o link.'),
        OpenApiParameter('offset', OpenApiTypes.INT), OpenApiParameter('mode', OpenApiTypes.STR, description='actionable: todas las de un veredicto, sin paginar.')])
    def get(self, request, pk):
        run = self.run(pk)
        if request.query_params.get('mode') == 'actionable':
            wanted = request.query_params.get('verdict')
            if wanted not in ('good', 'bad', 'link'):
                raise ValidationError({'verdict': 'Usa good, bad o link.'})
            return Response(actionable(run, wanted))
        return Response(report(run, *self.page(request)))

    @extend_schema(operation_id='v1_management_catalog_proposals_review_batch', request=ReviewAction, responses=OpenApiTypes.OBJECT)
    def post(self, request, pk):
        serializer = ReviewAction(data=request.data)
        serializer.is_valid(raise_exception=True)
        index = serializer.validated_data['batch_index']
        sku_review.review_batch(request.user, pk, index)
        run = self.run(pk)
        return Response(report(run, *self.page(request)))
