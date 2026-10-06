"""Conservative catalogue families, reviewed before any records are merged."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q

from .catalog_classification import (
    call_provider, normalized_reference, provider_configuration,
    supplier_suffix_match, validate_suggestions,
)
from .management import IsSuperuser
from .models import Part, PartCode
from .catalog_families import family_candidates, supplier_base


class GroupingPagination(PageNumberPagination):
    page_size = 20


def part_row(part):
    return {'id': str(part.pk), 'sku': part.sku, 'name': part.name,
            'is_OEM': part.is_OEM,
            'description': part.description, 'category': part.category,
            'subcategory': part.subcategory, 'codes': []}


def suggested_families():
    parts = {part.sku: part_row(part) for part in Part.objects.filter(active=True, merged_into__isnull=True).only(
        'id', 'sku', 'is_OEM', 'name', 'description', 'category', 'subcategory').order_by('sku')}
    bases = {base for sku in parts if (base := supplier_base(sku))}
    blocked = set(Part.objects.filter(sku__in=bases).values_list('sku', flat=True)) | set(PartCode.objects.filter(code__in=bases).values_list('code', flat=True))
    return family_candidates(parts, blocked)


class CatalogGroupingList(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(parameters=[OpenApiParameter('search', OpenApiTypes.STR), OpenApiParameter('page', OpenApiTypes.INT)],
                   responses=OpenApiTypes.OBJECT,
                   description='Lista familias de SKU que un superusuario puede revisar antes de unificarlas.')
    def get(self, request):
        search = request.query_params.get('search', '').strip().upper()
        families = list(suggested_families().values())
        if search:
            families = [family for family in families if any(search in f'{part["sku"]} {part["name"]} {part["description"]}'.upper()
                        for part in [family['target'], *family['sources']])]
        paginator = GroupingPagination()
        page = paginator.paginate_queryset(families, request, view=self)
        rows = []
        member_rows = {}
        for family in page:
            target_row, sources = family['target'], family['sources']
            source_rows = sources[:49]
            for row in [target_row, *source_rows]:
                if row['id']:
                    member_rows[row['id']] = row
            proposed = bool(target_row.get('proposed_parent'))
            rows.append({'target_sku': target_row['sku'], 'target': target_row, 'sources': source_rows,
                         'source_skus': [source['sku'] for source in source_rows], 'source_count': len(sources),
                         'reason': ('Crear SKU padre a partir del código base común. ' if proposed else 'SKU base completo y sufijos de proveedor. ') + 'Revisa la equivalencia antes de agrupar.',
                         'warnings': family['warnings'], 'needs_review': True})
        for code in PartCode.objects.filter(part_id__in=member_rows).order_by('code'):
            member_rows[str(code.part_id)]['codes'].append({'brand': code.brand, 'code': code.code, 'ref_type': code.ref_type, 'reference_source': code.reference_source})
        if rows:
            references = Q()
            for row in rows:
                references |= Q(code__startswith=row['target_sku'] + '-')
            for code in PartCode.objects.filter(references).select_related('part'):
                for row in rows:
                    if code.code.startswith(row['target_sku'] + '-') and str(code.part_id) not in {
                        row['target']['id'], *[item['id'] for item in row['sources']]
                    }:
                        row['warnings'].append(f'El código {code.code} está asignado al SKU {code.part.sku}. Revisa esa asignación por separado.')
        result = paginator.get_paginated_response(rows)
        result.data['configured'] = bool(provider_configuration()[0])
        return result


class GroupingClassificationRequest(serializers.Serializer):
    target_sku = serializers.CharField(max_length=200)
    source_skus = serializers.ListField(child=serializers.CharField(max_length=200), min_length=1, max_length=49)


class CatalogGroupingClassify(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(request=GroupingClassificationRequest, responses=OpenApiTypes.OBJECT,
                   description='Propone categorías y equivalencias para una familia seleccionada; no modifica el catálogo.')
    def post(self, request):
        serializer = GroupingClassificationRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        target = serializer.validated_data['target_sku'].strip().upper()
        selected = [value.strip().upper() for value in serializer.validated_data['source_skus']]
        if len(set([target, *selected])) != len(selected) + 1:
            raise ValidationError('Selecciona SKU distintos y un destino diferente de los alternos.')
        parts = {part.sku: part for part in Part.objects.filter(active=True, merged_into__isnull=True, sku__in=[target, *selected]).prefetch_related('codes')}
        family = suggested_families().get(target) if target not in parts else None
        proposed_parent = family['target'] if family and family['target'].get('proposed_parent') else None
        if len(parts) != len(selected) + (0 if proposed_parent else 1) or (proposed_parent and not set(selected).issubset({row['sku'] for row in family['sources']})):
            raise ValidationError('Uno de los SKU ya no está disponible. Actualiza las coincidencias.')
        source = []
        for index, sku in enumerate(selected if proposed_parent else [target, *selected]):
            row = part_row(parts[sku])
            row['row'] = index + 1
            row['codes'] = [{'brand': code.brand, 'code': code.code, 'ref_type': code.ref_type, 'reference_source': code.reference_source} for code in parts[sku].codes.all()][:40]
            source.append(row)
        # A family review has one explicit final SKU. The model can retain any
        # source separately, but cannot invent chains or merge sibling targets.
        candidates = {target: proposed_parent or source[0]}
        allowed = {sku: [] if sku == target else [target] for sku in [target, *selected]}
        proposals = validate_suggestions(call_provider(source, candidates, allowed), source, candidates, allowed)
        canonical = next((item for item in proposals if item['source_sku'] == target), proposals[0])
        category = canonical['category']
        subcategory = canonical['subcategory']
        grouped = [item for item in proposals if item['target_sku'] == target and item['source_sku'] != target]
        return Response({'target_sku': target, 'source_skus': [item['source_sku'] for item in grouped],
                         'category': category, 'subcategory': subcategory,
                         'reason': 'Propuestas de IA; confirma cada equivalencia antes de guardar.',
                         'confidence': min((item['confidence'] for item in grouped), default=0),
                         'suggestions': proposals})
