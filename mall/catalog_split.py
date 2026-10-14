"""Separar SKU: bring back the SKUs a grouping put under one canonical SKU by mistake (a thin wrapper over mall.line_merge.split_line).

A merge (mall.line_merge.merge_lines) makes each source SKU unificada (merged_into the canonical one) and hands the canonical SKU its
References (its former main among them, as one of the canonical SKU's codes), supplier stock and photos. A split brings back the
SKUs a superuser picks and gives each back the alternos, stock and photos assigned to it, all in one transaction. A restored SKU's
former main always comes back as its main; any other alias of its code goes back to it when it had it before the merge, and is
retired otherwise (the SKU code identifies the part again).

The plan suggests an owner for each row: the rows the merge recorded (since Separar SKU; since S2b the References themselves), else
the evidence that survives: the snapshot of every source's alternos the matching worker kept when it merged (MatchingDecision), the SKU
code a supplier stock row carries and the original SKU in each photo's storage path. Supplier catalog links follow the codes that made
them.

The SKUs a split leaves apart are recorded (CatalogSeparation): the matching worker never merges them again on its own, and Agrupar
SKU shows the separation as a warning.
"""
from django.db import IntegrityError
from django.db.models import Count, Exists, F, IntegerField, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import references
from .management import IsSuperuser, ManagedPartSerializer, catalog_queryset, with_merged_counts
from .media_serializers import PartImageSerializer
from .lines import all_lines, catalog_lines
from .line_merge import (METHODS, SERVICE_ACCOUNT, Owners, code_returns, main_row, merge_history, pair, rehome_catalog_links,  # noqa: F401
                         separated_pairs, split_line)
from .models import CatalogIdentityChange, CatalogMerge, ItemApplication, Part, PartSpecification, SupplierItem

MAX_RESTORE = 200


# ---------------------------------------------------------------- SKUs kept apart
def separation_warnings(families):
    """Agrupar SKU still offers a family whose SKUs were kept apart (a split, or a case dismissed), and says so: a person decides (the
    worker never merges them)."""
    ids = {row['id'] for family in families.values() for row in [family['target'], *family['sources']] if row.get('id')}
    apart = separated_pairs(ids) if ids else {}
    for family in families.values() if apart else ():
        rows = [row for row in [family['target'], *family['sources']] if row.get('id')]
        for index, first in enumerate(rows):
            for second in rows[index + 1:]:
                when = apart.get(pair(first['id'], second['id']))
                if when:
                    family['warnings'].append(f"{first['sku']} y {second['sku']} se separaron el {timezone.localtime(when):%d/%m/%Y} "
                                              '(Separar SKU o Mantener separados). Confirma que son el mismo repuesto antes de volver a agruparlos.')
    return families


# ---------------------------------------------------------------- what each row most likely belonged to
def split_plan(target):
    """The canonical SKU, its retired SKUs and every alterno, stock row and photo it holds, each with its suggested owner."""
    children = list(Part.objects.filter(merged_into=target).order_by('sku'))
    history, merges = merge_history(target, children)
    owners = Owners(children, history, target)
    for child in children:
        entry = history.get(str(child.pk), {})
        child.merged_at, child.merged_by = entry.get('merged_at'), entry.get('merged_by', '')
        child.automatic, child.method, child.snapshot = entry.get('automatic', False), entry.get('method', ''), entry.get('codes')
    codes = list(target.codes.filter(is_main=False).order_by('code', 'brand', 'pk'))  # the target's main is its SKU (S2b): it stays
    by_id = {str(child.pk): child for child in children}
    rows = {code.pk: code for code in codes}
    mains = {row.pk: pk for pk, row in ((pk, main_row(child, rows, history)) for pk, child in by_id.items()) if row is not None}
    for code in codes:
        if code.pk in mains:  # a grouped SKU's former main: it goes back with it, as its main
            code.suggested = code.alias_of = mains[code.pk]
            code.alias_returns = True
            continue
        code.suggested, code.alias_of = owners.code(code)
        code.alias_returns = bool(code.alias_of) and code_returns(code, by_id[code.alias_of], history)
    items = list(SupplierItem.objects.filter(part=target).select_related('supplier').order_by('supplier__name', 'codigo', 'pk'))
    going = {code.pk: code.suggested for code in codes}
    for item in items:
        # A variant goes where its Reference goes (S3). One on the target's main, or on a supplier's interno code (left out of the code
        # list), goes where its own code says: a collapsed spelling by the variant's own codigo (B16), an interno code with its items.
        item.suggested = going[item.reference_id] if item.reference_id in going else owners.item(item)
    images = list(target.images.order_by('position', 'created_at', 'pk'))
    for image in images:
        image.suggested = owners.image(image)
    own = [merge for merge in merges if merge.target_id == target.pk]
    target.created_by_merge = any(merge.summary.get('target_created') for merge in own)
    warnings = []
    if target.created_by_merge:
        warnings.append(f'{target.sku} es un SKU padre que se creó al agrupar: si separas todos sus SKU y no le queda nada, quedará inactivo.')
    if own:
        # A real rename only: marks and unmarks (exact_match, disputed, confirm_current) keep the code.
        renamed = (CatalogIdentityChange.objects.filter(part=target, created_at__gte=own[0].created_at).exclude(previous_sku=F('sku'))
                   .order_by('-created_at').first())
        if renamed:
            warnings.append(f'Después de agrupar, el SKU cambió de {renamed.previous_sku} a {renamed.sku}. Si ese código es de un SKU que separas, '
                            'corrígelo en Editar antes de separarlo.')
    if PartSpecification.objects.filter(part=target).exists() or ItemApplication.objects.filter(part=target).exists():
        warnings.append(f'La ficha técnica y las aplicaciones se quedan en {target.sku}.')
    unknown = [child.sku for child in children if child.snapshot is None]
    if unknown:
        warnings.append(f'No hay registro de los alternos que tenían {", ".join(unknown[:4])}{"…" if len(unknown) > 4 else ""} al agruparse: '
                        'asigna sus alternos a mano.')
    return {'target': target, 'children': children, 'codes': codes, 'items': items, 'images': images, 'warnings': warnings}


# ---------------------------------------------------------------- the split
def split_catalog_part(*, actor, target_id, restore, reason=''):
    """Separar SKU through mall.line_merge.split_line; the rows come back as the Inventario grid shows them."""
    result = split_line(target_id, restore, actor=actor, reason=reason)
    ids = [str(result['target'].pk), *(str(part.pk) for part in result['restored'])]
    rows = {str(part.pk): part for part in with_merged_counts(catalog_queryset(all_lines().filter(pk__in=ids)))}
    return {'split_id': result['split'].pk, 'target': rows[ids[0]], 'restored': [rows[pk] for pk in ids[1:]], 'summary': result['summary']}


# ---------------------------------------------------------------- API
class SplitPartSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    sku = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField()
    category = serializers.CharField()
    subcategory = serializers.CharField()
    is_OEM = serializers.BooleanField()
    active = serializers.BooleanField()


class SplitTargetSerializer(SplitPartSerializer):
    created_by_merge = serializers.BooleanField(help_text='Un SKU padre creado al agrupar: queda inactivo si la separación lo deja vacío.')


class SplitSnapshotCodeSerializer(serializers.Serializer):
    brand = serializers.CharField()
    code = serializers.CharField()


class SplitChildSerializer(SplitPartSerializer):
    merged_at = serializers.DateTimeField(allow_null=True)
    merged_by = serializers.CharField()
    automatic = serializers.BooleanField(help_text='Agrupado por el servicio de coincidencias, sin revisión.')
    method = serializers.CharField()
    codes = SplitSnapshotCodeSerializer(source='snapshot', many=True, allow_null=True, help_text='Los alternos que tenía al agruparse (si quedaron registrados).')


class SplitCodeSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    brand = serializers.CharField()
    code = serializers.CharField()
    ref_type = serializers.CharField()
    reference_source = serializers.CharField()
    suggested = serializers.CharField(help_text='El SKU agrupado del que probablemente vino; vacío si se queda.')
    alias_of = serializers.CharField(help_text='El SKU agrupado cuyo código es este alterno: nunca pasa a otro SKU.')
    alias_returns = serializers.BooleanField(help_text='Si ese SKU se separa, el código vuelve con él (verdadero) o se retira (falso).')


class SplitItemSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    supplier_name = serializers.CharField(source='supplier.name')
    supplier_invent_id = serializers.CharField()
    codigo = serializers.CharField()
    brand = serializers.CharField()
    description = serializers.CharField()
    available_quantity = serializers.IntegerField()
    suggested = serializers.CharField()


class SplitImageSerializer(PartImageSerializer):
    suggested = serializers.CharField(read_only=True)

    class Meta(PartImageSerializer.Meta):
        fields = PartImageSerializer.Meta.fields + ['suggested']
        read_only_fields = fields


class SplitPlanSerializer(serializers.Serializer):
    target = SplitTargetSerializer()
    children = SplitChildSerializer(many=True)
    codes = SplitCodeSerializer(many=True)
    items = SplitItemSerializer(many=True)
    images = SplitImageSerializer(many=True)
    warnings = serializers.ListField(child=serializers.CharField())


class SplitRestoreSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text='Un SKU agrupado en este SKU, que vuelve a estar activo.')
    codes = serializers.ListField(child=serializers.IntegerField(), required=False, default=list, max_length=5000)
    items = serializers.ListField(child=serializers.UUIDField(), required=False, default=list, max_length=5000)
    images = serializers.ListField(child=serializers.UUIDField(), required=False, default=list, max_length=500)


class SplitRequestSerializer(serializers.Serializer):
    restore = SplitRestoreSerializer(many=True, allow_empty=False, max_length=MAX_RESTORE)
    reason = serializers.CharField(required=False, allow_blank=True, default='', max_length=500)


class SplitSummarySerializer(serializers.Serializer):
    restored_skus = serializers.IntegerField()
    codes_moved = serializers.IntegerField()
    aliases_removed = serializers.IntegerField()
    items_moved = serializers.IntegerField()
    images_moved = serializers.IntegerField()
    links_moved = serializers.IntegerField()
    links_shared = serializers.IntegerField()
    target_retired = serializers.BooleanField()
    mains_returned = serializers.IntegerField(help_text='SKU restaurados con su código principal de antes (S2b).')
    references_revived = serializers.IntegerField(help_text='Referencias que la unificación absorbió y vuelven de retiradas.')
    favorites_returned = serializers.IntegerField(help_text='Favoritos que vuelven a los SKU restaurados.')


class SplitResultSerializer(serializers.Serializer):
    split_id = serializers.UUIDField()
    target = ManagedPartSerializer()
    restored = ManagedPartSerializer(many=True)
    summary = SplitSummarySerializer()


class CatalogSplitView(APIView):
    """GET the plan of a canonical SKU's split; POST it."""
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_split_plan', responses=SplitPlanSerializer,
                   description='Los SKU agrupados en este SKU y, para cada alterno, existencia e imagen, el SKU del que probablemente vino.')
    def get(self, request, pk):
        target = Part.objects.filter(pk=pk).first()
        if target is None:
            raise NotFound('Este SKU ya no está en el catálogo.')
        if target.merged_into_id:
            raise NotFound(f'Este SKU está agrupado en {target.merged_into.sku}: sepáralo desde ese SKU.')
        return Response(SplitPlanSerializer(split_plan(target), context={'request': request}).data)

    @extend_schema(operation_id='v1_management_catalog_split', request=SplitRequestSerializer, responses=SplitResultSerializer,
                   description='Reactiva los SKU agrupados elegidos y les devuelve los alternos, existencias e imágenes asignados, todo o nada. '
                               'El servicio de coincidencias no vuelve a agrupar los SKU separados.')
    def post(self, request, pk):
        data = SplitRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            result = split_catalog_part(actor=request.user, target_id=pk, **data.validated_data)
        except IntegrityError:
            raise ValidationError({'detail': 'El catálogo cambió durante la separación. Vuelve a cargarla e inténtalo de nuevo.'})
        return Response(SplitResultSerializer(result, context={'request': request}).data)


class MergedPreviewSerializer(serializers.Serializer):
    sku = serializers.CharField()
    description = serializers.CharField()


class MergedSkuSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    sku = serializers.CharField()
    description = serializers.CharField()
    active = serializers.BooleanField()
    merged_count = serializers.IntegerField()
    last_merged_at = serializers.DateTimeField(allow_null=True)
    automatic = serializers.BooleanField(help_text='Al menos una agrupación la hizo el servicio de coincidencias, sin revisión.')
    merged = MergedPreviewSerializer(source='merged_preview', many=True, help_text='Los primeros SKU agrupados, por código.')


class MergedPagination(PageNumberPagination):
    page_size = 25


class CatalogMergedList(generics.ListAPIView):
    """Canonical SKUs that hold grouped SKUs, latest grouping first: where Separar SKU starts."""
    permission_classes = [IsSuperuser]
    serializer_class = MergedSkuSerializer
    pagination_class = MergedPagination

    @extend_schema(operation_id='v1_management_catalog_merged', parameters=[
        OpenApiParameter('search', OpenApiTypes.STR, description='SKU o descripción del SKU principal, o código de un SKU agrupado.'),
        OpenApiParameter('origin', OpenApiTypes.STR, enum=['automatic', 'manual'], description='Agrupados por el servicio de coincidencias o por una persona.')])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        grouped = catalog_lines(('unificada',))
        children = grouped.filter(merged_into=OuterRef('pk')).order_by().values('merged_into').annotate(total=Count('pk')).values('total')[:1]
        merges = CatalogMerge.objects.filter(target=OuterRef('pk'))
        queryset = catalog_lines().filter(pk__in=grouped.values('merged_into')).annotate(
            merged_count=Coalesce(Subquery(children, output_field=IntegerField()), 0),
            last_merged_at=Subquery(merges.order_by('-created_at').values('created_at')[:1]),
            automatic=Exists(merges.filter(actor__username=SERVICE_ACCOUNT)))
        search = self.request.query_params.get('search', '').strip()
        if search:
            queryset = queryset.filter(Q(sku__icontains=search) | Q(description__icontains=search) |
                                       Q(pk__in=grouped.filter(sku__icontains=search).values('merged_into')))
        origin = self.request.query_params.get('origin', '')
        if origin in ('automatic', 'manual'):
            queryset = queryset.filter(automatic=origin == 'automatic')
        preview = Prefetch('merged_parts', queryset=Part.objects.order_by('sku')[:8], to_attr='merged_preview')
        return queryset.prefetch_related(preview).order_by(F('last_merged_at').desc(nulls_last=True), 'sku', 'pk')
