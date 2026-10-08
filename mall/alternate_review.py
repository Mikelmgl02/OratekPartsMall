"""Superuser review of catalog alternos no OEM number carries (alternos, step 2c). mall.prune_catalog_alternos leaves whole every SKU
holding a company alterno a supplier catalog wrote that the SKU cannot reach through its OEM numbers: the copy may come from an item
matched through a number the catalog prints for several parts. Here a person sees the evidence (the SKU's numbers and which catalog
items print each one; the numbers a catalog files the code under) and decides each code:

- keep: the code stays as the SKU's alterno, marked confirmed (its source becomes "CONFIRMADO POR <USUARIO> · <cita>"), so the
  cleanup never treats it as a catalog copy again;
- remove: the alterno is deleted;
- remove_copies: the SKU's catalog alternos it already reaches through its numbers are deleted (customers keep seeing them).
"""
from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from . import prune_catalog_alternos as prune
from .management import IsSuperuser
from .models import Part, PartCode
from .oem_links import part_equivalents
from .oem_reference_models import OEMCrossReference, OEMReferenceSource, compact, manufacturer_name
from .oem_reference_views import OEMReferenceLink, require_tables

ACTIONS = ['keep', 'remove', 'remove_copies']
FILED_LIMIT = 5
TEXT_LIMIT = 200
CHANGED = 'Los alternos de este SKU cambiaron desde que los revisaste. Ya ves su estado actual.'


class AlternateReviewPart(serializers.ModelSerializer):
    class Meta:
        model = Part
        fields = ['id', 'sku', 'name', 'description', 'category', 'subcategory', 'active', 'is_OEM']
        read_only_fields = fields


class AlternateReviewRow(serializers.Serializer):
    part = AlternateReviewPart()
    undecided = serializers.IntegerField(help_text='Alternos de catálogo que ningún número OEM del SKU lleva: esperan una decisión.')
    copies = serializers.IntegerField(help_text='Alternos de catálogo que el SKU ya alcanza por sus números OEM.')


class AlternateReviewListResponse(serializers.Serializer):
    count = serializers.IntegerField()
    results = AlternateReviewRow(many=True)


class AlternateNumberEvidence(serializers.Serializer):
    reference = OEMReferenceLink()
    description = serializers.CharField()
    applications = serializers.CharField()


class AlternatePrintedFor(serializers.Serializer):
    catalog = serializers.CharField()
    brand_codes = serializers.ListField(child=serializers.CharField(), help_text='Artículos del catálogo que imprimen el número.')


class AlternateReviewNumber(AlternateNumberEvidence):
    via = serializers.ListField(child=serializers.CharField())
    printed_for = AlternatePrintedFor(many=True)


class AlternateReviewCode(serializers.Serializer):
    id = serializers.IntegerField()
    brand = serializers.CharField()
    code = serializers.CharField()
    reference_source = serializers.CharField()
    filed_under = AlternateNumberEvidence(many=True, help_text=f'Hasta {FILED_LIMIT} números OEM bajo los que un catálogo archiva el código (que el SKU no alcanza).')
    filed_count = serializers.IntegerField()


class AlternateReviewDetail(serializers.Serializer):
    part = AlternateReviewPart()
    numbers = AlternateReviewNumber(many=True)
    undecided = AlternateReviewCode(many=True)
    copies = AlternateReviewCode(many=True)


class AlternateReviewDecision(serializers.Serializer):
    action = serializers.ChoiceField(choices=ACTIONS)
    alternate = serializers.IntegerField(required=False, help_text='keep y remove: el alterno que decides.')


class AlternateReviewConflict(serializers.Serializer):
    detail = serializers.CharField()
    review = AlternateReviewDetail()


def catalog_alternos(part, equivalents=None):
    """The SKU's company alternos a supplier catalog wrote, split into (undecided, copies) by whether its numbers reach them."""
    citations = prune.catalog_citations()
    codes = [code for code in part.codes.filter(ref_type='company').order_by('brand', 'code', 'pk') if prune.written_by_catalog(code.reference_source, citations)]
    if not codes:
        return [], []
    equivalents = equivalents or part_equivalents(part)
    reached = {(c['brand'], compact(c['code'])) for c in equivalents['cross_references']}
    undecided = [code for code in codes if (manufacturer_name(code.brand), compact(code.code)) not in reached]
    return undecided, [code for code in codes if code not in undecided]


def evidence(ref):
    return {'reference': ref, 'description': ref.description[:TEXT_LIMIT], 'applications': ref.applications[:TEXT_LIMIT]}


def detail(part):
    equivalents = part_equivalents(part)
    undecided, copies = catalog_alternos(part, equivalents)
    numbers = []
    for entry in equivalents['numbers']:
        ref = entry['reference']
        printed = [{'catalog': source.citation, 'brand_codes': source.detail.get('brand_codes') or []}
                   for source in OEMReferenceSource.objects.filter(reference=ref, kind='aftermarket_catalog', detail__has_key='catalog').order_by('citation')]
        numbers.append({**evidence(ref), 'via': entry['via'], 'printed_for': printed})
    reached = {entry['reference'].pk for entry in equivalents['numbers']}

    def row(code, *, filed=True):
        elsewhere = []
        if filed:
            elsewhere = [cross.reference for cross in (OEMCrossReference.objects.filter(brand=manufacturer_name(code.brand), number=compact(code.code))
                                                       .exclude(reference_id__in=reached).select_related('reference').order_by('reference__manufacturer', 'reference__code'))]
        return {'id': code.pk, 'brand': code.brand, 'code': code.code, 'reference_source': code.reference_source,
                'filed_under': [evidence(ref) for ref in elsewhere[:FILED_LIMIT]], 'filed_count': len(elsewhere)}
    return AlternateReviewDetail({'part': part, 'numbers': numbers, 'undecided': [row(code) for code in undecided],
                         'copies': [row(code, filed=False) for code in copies]}).data


def canonical(pk):
    part = Part.objects.filter(pk=pk, merged_into__isnull=True).first()
    if part is None:
        raise NotFound('Ese SKU no está en el catálogo o se agrupó en otro.')
    return part


class AlternateReviewList(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_alternate_review_list', responses=AlternateReviewListResponse,
                   description='SKU con alternos de catálogo que ningún número OEM del SKU lleva, con cuántos esperan decisión y cuántas copias tienen.')
    def get(self, request):
        require_tables()
        rows = []
        for codes in prune.plan()['review'].values():
            part = codes[0].part
            undecided, copies = catalog_alternos(part)
            rows.append({'part': part, 'undecided': len(undecided), 'copies': len(copies)})
        return Response(AlternateReviewListResponse({'count': len(rows), 'results': rows}).data)


class AlternateReviewItem(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_alternate_review_detail', responses=AlternateReviewDetail,
                   description='Un SKU con sus números OEM (y los artículos de catálogo que los imprimen), sus alternos de catálogo por decidir '
                               '(con los números bajo los que un catálogo los archiva) y sus copias.')
    def get(self, request, pk):
        require_tables()
        return Response(detail(canonical(pk)))

    @extend_schema(operation_id='v1_management_alternate_review_decide', request=AlternateReviewDecision, responses={
        200: AlternateReviewDetail, 409: OpenApiResponse(AlternateReviewConflict, description='El alterno ya no está por decidir o cambió.')},
        description='keep confirma un alterno por decidir (deja de tratarse como copia de catálogo), remove lo retira, remove_copies retira los '
                    'alternos de catálogo que el SKU ya alcanza por sus números OEM. Responde el estado actual del SKU.')
    def post(self, request, pk):
        require_tables()
        data = AlternateReviewDecision(data=request.data)
        data.is_valid(raise_exception=True)
        action, alternate = data.validated_data['action'], data.validated_data.get('alternate')
        if action != 'remove_copies' and alternate is None:
            raise serializers.ValidationError({'alternate': 'Indica el alterno que decides.'})
        with transaction.atomic():
            part = Part.objects.select_for_update().filter(pk=canonical(pk).pk).first()
            undecided, copies = catalog_alternos(part)
            if action == 'remove_copies':
                PartCode.objects.filter(pk__in=[code.pk for code in copies]).delete()
                return Response(detail(part))
            eligible = {code.pk: code for code in (undecided if action == 'keep' else undecided + copies)}
            code = eligible.get(alternate)
            if code is None:
                return Response({'detail': CHANGED, 'review': detail(part)}, status=409)
            if action == 'remove':
                code.delete()
            else:
                code.reference_source = f'CONFIRMADO POR {request.user.username.upper()} · {code.reference_source}'[:500]
                code.save(update_fields=['reference_source'])
        return Response(detail(part))
