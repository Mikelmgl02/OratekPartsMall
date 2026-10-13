"""Productos nuevos (superusers): the proposed new catalog SKUs of mall.sku_proposals, with their supplier items."""
import uuid

from django.db.models import Count, F, IntegerField, Max, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Greatest
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, serializers
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import sku_proposals
from .list_ordering import StableOrdering
from .management import IsSuperuser
from .sku_proposal_models import SkuProposal, SkuProposalItem

PREVIEW_ITEMS = 5
MAX_BULK = 100


def item_json(row):
    item = row.item
    return {'id': str(item.pk), 'supplier': str(item.supplier_id), 'supplier_name': item.supplier.name, 'supplier_invent_id': item.supplier_invent_id,
            'codigo': item.codigo, 'brand': item.brand, 'description': item.description, 'references': item.references,
            'source': item.source, 'available': item.available_quantity, 'matching_status': item.matching_status}


def proposal_json(proposal, *, all_items=False):
    rows = list(proposal.items.all())
    part_type = proposal.part_type
    return {
        'id': str(proposal.pk), 'status': proposal.status, 'status_label': proposal.get_status_display(), 'version': proposal.version,
        'sku': proposal.sku, 'description': proposal.description, 'brand': proposal.brand,
        'oem': {'code': proposal.oem_code, 'brand': proposal.oem_brand} if proposal.oem_code else None,
        'part_type': {'id': str(part_type.pk), 'category': part_type.category, 'name': part_type.name} if part_type else None,
        'references': proposal.references, 'field_sources': proposal.field_sources,
        'item_count': len(rows), 'supplier_count': len({row.item.supplier_id for row in rows}),
        'stock': sum(row.item.available_quantity for row in rows),
        'items': [item_json(row) for row in (rows if all_items else rows[:PREVIEW_ITEMS])],
        # The detail only: the catalog SKUs its code or OEM number already belongs to, to link the items there.
        **({'catalog_match': sku_proposals.catalog_match(proposal) if proposal.status == 'pending' else []} if all_items else {}),
        'part': {'id': str(proposal.part_id), 'sku': proposal.part.sku} if proposal.part_id else None,
        'decided_by': proposal.decided_by.username if proposal.decided_by else None, 'decided_at': proposal.decided_at,
        'reject_reason': proposal.reject_reason, 'reject_reason_label': proposal.get_reject_reason_display() if proposal.reject_reason else '',
        'reject_note': proposal.reject_note, 'created_at': proposal.created_at, 'updated_at': proposal.updated_at,
        # The same product approved or linked before (a supplier that started carrying it later): link the items there.
        'earlier': {'id': str(proposal.earlier_part), 'sku': proposal.earlier_sku}
                   if getattr(proposal, 'earlier_part', None) and proposal.status not in ('approved', 'linked') else None,
    }


def proposals():
    decided = (SkuProposal.objects.filter(key=OuterRef('key'), status__in=['approved', 'linked'], part__active=True, part__merged_into__isnull=True)
               .exclude(pk=OuterRef('pk')).order_by('-decided_at'))
    return (SkuProposal.objects.select_related('part_type', 'part', 'decided_by')
            .prefetch_related('items__item__supplier')
            .annotate(item_total=Count('items', distinct=True),
                      stock_total=Coalesce(Sum(Greatest(F('items__item__reported_quantity') - F('items__item__reserved_quantity'), Value(0))),
                                           Value(0), output_field=IntegerField()),
                      earlier_part=Subquery(decided.values('part_id')[:1]), earlier_sku=Subquery(decided.values('part__sku')[:1])))


class ProposalSerializer(serializers.BaseSerializer):
    def to_representation(self, instance):
        return proposal_json(instance)


class ProposalList(generics.ListAPIView):
    permission_classes = [IsSuperuser]
    serializer_class = ProposalSerializer
    filter_backends = [StableOrdering]
    ordering_fields = ['sku', 'description', 'brand', 'status', 'created_at', 'updated_at', 'item_total', 'stock_total']
    ordering = ['-stock_total', 'sku']

    def get_queryset(self):
        params = self.request.query_params
        rows = proposals()
        state = params.get('status') or 'pending'
        if state != 'all':
            if state not in dict(SkuProposal.STATUSES):
                raise ValidationError({'status': 'Usa pending, approved, linked, rejected, resolved o all.'})
            rows = rows.filter(status=state)
        search = (params.get('search') or '').strip()
        # Item filters go through subqueries: another join on the items would multiply stock_total.
        if search:
            carried = SkuProposalItem.objects.filter(Q(item__codigo__icontains=search) | Q(item__supplier__name__icontains=search)).values('proposal_id')
            rows = rows.filter(Q(sku__icontains=search) | Q(description__icontains=search) | Q(oem_code__icontains=search) | Q(pk__in=carried))
        if params.get('brand'):
            rows = rows.filter(brand__iexact=params['brand'].strip())
        if params.get('has_oem') in ('true', 'false'):
            rows = rows.exclude(oem_code='') if params['has_oem'] == 'true' else rows.filter(oem_code='')
        if params.get('supplier'):
            try:
                uuid.UUID(params['supplier'])
            except ValueError:
                raise ValidationError({'supplier': 'Elige un proveedor.'})
            rows = rows.filter(pk__in=SkuProposalItem.objects.filter(item__supplier_id=params['supplier']).values('proposal_id'))
        return rows

    @extend_schema(operation_id='v1_management_catalog_proposals_list', responses=OpenApiTypes.OBJECT, parameters=[
        OpenApiParameter('status', OpenApiTypes.STR, description='pending (por omisión), approved, linked, rejected, resolved o all.'),
        OpenApiParameter('search', OpenApiTypes.STR), OpenApiParameter('brand', OpenApiTypes.STR), OpenApiParameter('has_oem', OpenApiTypes.STR),
        OpenApiParameter('supplier', OpenApiTypes.UUID), OpenApiParameter('ordering', OpenApiTypes.STR), OpenApiParameter('page', OpenApiTypes.INT)])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ProposalSummary(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_proposals_summary', responses=OpenApiTypes.OBJECT)
    def get(self, request):
        counts = dict(SkuProposal.objects.values_list('status').annotate(total=Count('pk')))
        summary = {state: counts.get(state, 0) for state, _ in SkuProposal.STATUSES}
        # When a pending proposal was last created, reopened or changed: the storefront reminder, once closed, shows again only after
        # this moves (frontend/components/new-products-snackbar.tsx).
        summary['pending_changed_at'] = SkuProposal.objects.filter(status='pending').aggregate(at=Max('updated_at'))['at']
        return Response(summary)


class ActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=['approve', 'reject', 'link'])
    expected_version = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    part = serializers.UUIDField(required=False)


class ProposalDetail(APIView):
    permission_classes = [IsSuperuser]

    def current(self, pk):
        return proposal_json(get_object_or_404(proposals(), pk=pk), all_items=True)

    @extend_schema(operation_id='v1_management_catalog_proposals_retrieve', responses=OpenApiTypes.OBJECT)
    def get(self, request, pk):
        return Response(self.current(pk))

    @extend_schema(operation_id='v1_management_catalog_proposals_edit', request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def patch(self, request, pk):
        data = dict(request.data)
        version = data.pop('expected_version', None)
        if not isinstance(version, int):
            raise ValidationError({'expected_version': 'Envía la versión que estás editando.'})
        return self.answer(pk, lambda: sku_proposals.edit(pk, request.user, version, data))

    @extend_schema(operation_id='v1_management_catalog_proposals_decide', request=ActionSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request, pk):
        serializer = ActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data['action'] == 'approve':
            run = lambda: sku_proposals.approve(pk, request.user, data['expected_version'])
        elif data['action'] == 'reject':
            run = lambda: sku_proposals.reject(pk, request.user, data['expected_version'], data.get('reason', ''), data.get('note', ''))
        else:
            if not data.get('part'):
                raise ValidationError({'part': 'Elige el SKU del catálogo.'})
            run = lambda: sku_proposals.link_existing(pk, request.user, data['expected_version'], data['part'])
        return self.answer(pk, run)

    def answer(self, pk, run):
        try:
            run()
        except sku_proposals.ProposalConflict as conflict:
            return Response({'detail': conflict.detail, 'proposal': self.current(pk)}, status=conflict.status_code)
        except sku_proposals.DuplicateProposal as refused:
            return Response({'detail': refused.detail, 'owners': refused.owners}, status=400)
        return Response(self.current(pk))


class ProposalBulk(APIView):
    """Aprobar seleccionadas: each proposal on its own (one refused does not stop the others), at the version the grid showed."""
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_proposals_bulk', request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        rows = request.data.get('proposals') if isinstance(request.data, dict) else None
        if request.data.get('action') != 'approve' or not isinstance(rows, list) or not rows or len(rows) > MAX_BULK:
            raise ValidationError({'detail': f'Envía action approve y de 1 a {MAX_BULK} propuestas con su versión.'})
        results = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('version'), int) or not row.get('id'):
                results.append({'id': str(row.get('id') if isinstance(row, dict) else ''), 'ok': False, 'message': 'Falta el id o la versión.'})
                continue
            try:
                proposal = sku_proposals.approve(row['id'], request.user, row['version'])
            except sku_proposals.DuplicateProposal as refused:
                results.append({'id': str(row['id']), 'ok': False, 'message': refused.detail})
            except (APIException, ValidationError) as error:
                detail = error.detail
                message = detail if isinstance(detail, str) else ' '.join(str(value) for values in (detail.values() if isinstance(detail, dict) else [detail])
                                                                       for value in (values if isinstance(values, list) else [values]))
                results.append({'id': str(row['id']), 'ok': False, 'message': str(message)})
            else:
                results.append({'id': str(proposal.pk), 'ok': True, 'sku': proposal.part.sku})
        return Response({'approved': sum(result['ok'] for result in results), 'results': results})
