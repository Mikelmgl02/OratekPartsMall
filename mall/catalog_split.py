"""Separar SKU: bring back the SKUs a grouping put under one canonical SKU by mistake.

A merge (mall.catalog_grouping.merge_catalog_parts) retires each source SKU (inactive, merged_into the canonical one) and hands the
canonical SKU its alternos, supplier stock and photos, filing the source's own code as an alterno. A split reactivates the retired
SKUs a superuser picks and gives each back the alternos, stock and photos assigned to it, all in one transaction. The alterno that
is a restored SKU's own code always leaves the canonical SKU: it goes back to its SKU when that SKU had it before the merge, and is
removed otherwise (the SKU code itself identifies the part again).

No row records where it came from, so the plan suggests an owner for each from the evidence that survives: the snapshot of every
source's alternos the matching worker kept when it merged (MatchingDecision), the SKU code a supplier stock row carries and the
original SKU in each photo's storage path. Supplier catalog links follow the codes that made them.

The SKUs a split leaves apart are recorded (CatalogSeparation): the matching worker never merges them again on its own, and Agrupar
SKU shows the separation as a warning.
"""
from django.db import IntegrityError, transaction
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

from .catalog_families import normalized_reference
from .catalog_identity import company_reference
from .catalog_split_models import CatalogSeparation, CatalogSplit
from .management import IsSuperuser, ManagedPartSerializer, catalog_queryset, with_merged_counts
from .media_serializers import PartImageSerializer
from .lines import catalog_lines
from .models import (Account, CatalogIdentityChange, CatalogMerge, ItemApplication, MatchingDecision, Part, PartCode, PartImage,
                     PartSpecification, SupplierInventoryImportJob, SupplierItem)
from .oem_reference_models import PartOEMLink

SERVICE_ACCOUNT = 'motionpartes-matching-service'  # the matching worker's actor (mall.matching_worker)
METHODS = {'base_and_spec': 'Mismo código base y la misma descripción', 'catalog_reference': 'Su código era alterno de este SKU',
           'family_review': 'Familia revisada en Agrupación inteligente'}
MAX_RESTORE = 200


# ---------------------------------------------------------------- SKUs kept apart
def pair(a, b):
    """Two SKU ids in CatalogSeparation order."""
    return tuple(sorted((str(a), str(b))))


def separated_pairs(part_ids=None):
    """{(id, id): when} for the SKUs a split keeps apart: those touching part_ids, or all of them."""
    rows = CatalogSeparation.objects.all()
    if part_ids is not None:
        ids = [str(pk) for pk in part_ids]
        rows = rows.filter(Q(first_id__in=ids) | Q(second_id__in=ids))
    return {pair(a, b): when for a, b, when in rows.values_list('first_id', 'second_id', 'created_at')}


def separation_warnings(families):
    """Agrupar SKU still offers a family whose SKUs a split separated, and says so: a person decides (the worker never merges them)."""
    ids = {row['id'] for family in families.values() for row in [family['target'], *family['sources']] if row.get('id')}
    apart = separated_pairs(ids) if ids else {}
    for family in families.values() if apart else ():
        rows = [row for row in [family['target'], *family['sources']] if row.get('id')]
        for index, first in enumerate(rows):
            for second in rows[index + 1:]:
                when = apart.get(pair(first['id'], second['id']))
                if when:
                    family['warnings'].append(f"{first['sku']} y {second['sku']} se separaron el {timezone.localtime(when):%d/%m/%Y} con Separar SKU. "
                                              'Confirma que son el mismo repuesto antes de volver a agruparlos.')
    return families


# ---------------------------------------------------------------- what each row most likely belonged to
def merge_history(target, children):
    """({retired SKU id: the merge that retired it}, merges): when, by whom, automatic or reviewed, why, and what the SKU brought: the
    alternos, stock rows and photos the merge recorded (merges since Separar SKU), or the alternos in the matching worker's snapshot
    (codes None when nothing recorded them)."""
    ids = {str(child.pk) for child in children}
    merges = list(CatalogMerge.objects.filter(target_id__in=[target.pk, *ids]).select_related('actor').order_by('created_at'))
    decisions = {}
    if merges:
        for decision in MatchingDecision.objects.filter(evidence__merge_id__in=[str(merge.pk) for merge in merges]):
            decisions[str(decision.evidence.get('merge_id'))] = decision
    history = {}
    for merge in merges:
        decision = decisions.get(str(merge.pk))
        snapshots = {str(row.get('id')): row for row in decision.evidence.get('sources') or []} if decision else {}
        method = (decision.evidence.get('method') or '') if decision else ''
        for source in merge.sources:
            pk = str(source.get('id'))
            if pk not in ids or pk in history:
                continue
            recorded = source if 'codes' in source else snapshots.get(pk)
            history[pk] = {'merged_at': merge.created_at, 'merged_by': merge.actor.username, 'automatic': merge.actor.username == SERVICE_ACCOUNT,
                           'method': METHODS.get(method, method) if decision else 'Agrupar SKU',
                           'codes': [{'brand': code.get('brand', ''), 'code': code.get('code', '')} for code in recorded.get('codes', [])] if recorded else None,
                           'items': [str(item) for item in source.get('items', [])], 'images': [str(image) for image in source.get('images', [])]}
    return history, merges


def code_returns(code, child, history):
    """Whether a grouped SKU's own code, filed on the canonical SKU, goes back to that SKU when it is restored: when the SKU had it before
    the merge (a brand-suffixed SKU files its code as a brand alterno). Otherwise it is removed: the SKU code identifies the part again."""
    snapshot = history.get(str(child.pk), {}).get('codes')
    return {'brand': code.brand, 'code': code.code} in snapshot if snapshot is not None else bool(company_reference(child.sku))


class Owners:
    """The restored SKU a row most likely came from ('' for the canonical SKU). A row the merge recorded goes to the SKU that brought it,
    and a code a snapshot lists to the SKU merged first (its first owner); otherwise a stock row goes to the SKU its supplier code names
    and a photo to the SKU in its storage path."""

    def __init__(self, children, history):
        late = timezone.now()
        ordered = sorted(children, key=lambda child: (history.get(str(child.pk), {}).get('merged_at') or late, child.sku))
        self.ids = [str(child.pk) for child in ordered]
        self.skus = {normalized_reference(child.sku): str(child.pk) for child in ordered}
        self.snapshot, self.numbers, self.company, self.company_numbers, self.items, self.images = {}, {}, {}, {}, {}, {}
        for child in ordered:
            pk = str(child.pk)
            for item in history.get(pk, {}).get('items') or []:
                self.items.setdefault(item, pk)
            for image in history.get(pk, {}).get('images') or []:
                self.images.setdefault(image, pk)
            for code in history.get(pk, {}).get('codes') or []:
                self.snapshot.setdefault((code['brand'], code['code']), pk)
                self.numbers.setdefault(normalized_reference(code['code']), pk)
            # A supplier-suffixed SKU code (0110-ACA30-SPK) names its brand's own code: the merge filed it as a brand alterno.
            info = company_reference(child.sku)
            if info:
                number = normalized_reference(info['code'])
                self.company.setdefault((info['brand'], number), pk)
                self.company_numbers[number] = pk if number not in self.company_numbers else ''

    def code(self, code):
        """(owner, alias_of): alias_of is the restored SKU whose own code this alterno is."""
        number = normalized_reference(code.code)
        alias = self.skus.get(number, '')
        if alias:
            return alias, alias
        return self.snapshot.get((code.brand, code.code)) or self.company.get((code.brand, number), ''), ''

    def item(self, item):
        if str(item.pk) in self.items:
            return self.items[str(item.pk)]
        number = normalized_reference(item.codigo)
        brand = (item.brand or '').strip().upper()
        return (self.skus.get(number) or self.numbers.get(number) or self.company.get((brand, number))
                or ('' if brand else self.company_numbers.get(number, '')))

    def image(self, image):
        name = image.image.name or ''
        return self.images.get(str(image.pk)) or next((pk for pk in self.ids if f'/{pk}/' in name), '')


def split_plan(target):
    """The canonical SKU, its retired SKUs and every alterno, stock row and photo it holds, each with its suggested owner."""
    children = list(Part.objects.filter(merged_into=target).order_by('sku'))
    history, merges = merge_history(target, children)
    owners = Owners(children, history)
    for child in children:
        entry = history.get(str(child.pk), {})
        child.merged_at, child.merged_by = entry.get('merged_at'), entry.get('merged_by', '')
        child.automatic, child.method, child.snapshot = entry.get('automatic', False), entry.get('method', ''), entry.get('codes')
    codes = list(target.codes.order_by('code', 'brand', 'pk'))
    by_id = {str(child.pk): child for child in children}
    for code in codes:
        code.suggested, code.alias_of = owners.code(code)
        code.alias_returns = bool(code.alias_of) and code_returns(code, by_id[code.alias_of], history)
    items = list(SupplierItem.objects.filter(part=target).select_related('supplier').order_by('supplier__name', 'codigo', 'pk'))
    for item in items:
        item.suggested = owners.item(item)
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
def rehome_catalog_links(target, restored):
    """A supplier catalog tied the canonical SKU to an OEM number because its code or an alterno matched the catalog item (the item's
    code closes each citation; the number is the link's). Each link goes to the restored SKUs that hold those codes now; it stays on the
    canonical SKU when that one holds them too (shared) or nobody does."""
    links = list(PartOEMLink.objects.select_for_update().filter(part=target, source='catalog').select_related('reference').order_by('pk'))
    if not links:
        return {'moved': 0, 'shared': 0}
    keys = {part.pk: {normalized_reference(part.sku), *map(normalized_reference, PartCode.objects.filter(part=part).values_list('code', flat=True))}
            for part in [target, *restored]}
    held = set(PartOEMLink.objects.filter(source='catalog', part__in=restored).values_list('part_id', 'reference_id'))
    moved = shared = 0
    for link in links:
        wanted = {normalized_reference(link.reference.code)} | {normalized_reference(cite.rsplit(' · ', 1)[-1]) for cite in link.citations if ' · ' in cite}
        owners = [part for part in restored if keys[part.pk] & wanted]
        if not owners:
            continue
        PartOEMLink.objects.bulk_create([PartOEMLink(part=part, reference_id=link.reference_id, source='catalog', citations=link.citations)
                                         for part in owners if (part.pk, link.reference_id) not in held])
        if keys[target.pk] & wanted:
            shared += 1
        else:
            link.delete()
            moved += 1
    return {'moved': moved, 'shared': shared}


@transaction.atomic
def split_catalog_part(*, actor, target_id, restore, reason=''):
    # Supplier accounts first, then the catalog rows: the lock order of stock imports and merges.
    suppliers = sorted(set(SupplierItem.objects.filter(part_id=target_id).values_list('supplier_id', flat=True)))
    list(Account.objects.select_for_update().filter(pk__in=suppliers).order_by('pk'))
    if SupplierInventoryImportJob.objects.filter(supplier_id__in=suppliers, status='importing', expires_at__gt=timezone.now()).exists():
        raise ValidationError({'detail': 'Un proveedor de este SKU está importando su inventario. Espera a que termine y vuelve a intentarlo.'})
    target = Part.objects.select_for_update().filter(pk=target_id).first()
    if target is None or target.merged_into_id:
        raise ValidationError({'detail': 'Este SKU ya no está en el catálogo o se agrupó en otro. Vuelve a cargar el inventario.'})
    children = {str(part.pk): part for part in Part.objects.select_for_update().filter(merged_into=target).order_by('pk')}
    wanted = {}
    for entry in restore:
        pk = str(entry['id'])
        if pk not in children:
            raise ValidationError({'detail': f'Uno de los SKU ya no está agrupado en {target.sku}. Vuelve a cargar la separación.'})
        if pk in wanted:
            raise ValidationError({'detail': 'No repitas un SKU por separar.'})
        wanted[pk] = entry
    codes = {code.pk: code for code in PartCode.objects.select_for_update().filter(part=target).order_by('pk')}
    items = {str(item.pk): item for item in SupplierItem.objects.select_for_update().filter(part=target).order_by('pk')}
    images = {str(image.pk): image for image in PartImage.objects.select_for_update().filter(part=target).order_by('pk')}
    plan = {pk: {'codes': [], 'items': [], 'images': []} for pk in wanted}
    taken = {}
    for pk, entry in wanted.items():
        for kind, rows, keys in (('codes', codes, entry.get('codes', [])), ('items', items, map(str, entry.get('items', []))),
                                 ('images', images, map(str, entry.get('images', [])))):
            for key in keys:
                if key not in rows:
                    raise ValidationError({'detail': f'{target.sku} cambió desde que abriste la separación. Vuelve a cargarla.'})
                if taken.setdefault((kind, key), pk) != pk:
                    raise ValidationError({'detail': 'Cada alterno, existencia o imagen va a un solo SKU.'})
                if key not in plan[pk][kind]:
                    plan[pk][kind].append(key)
    history, merges = merge_history(target, list(children.values()))
    child_codes = {normalized_reference(part.sku): pk for pk, part in children.items()}
    target_number, removed = normalized_reference(target.sku), []
    for code in codes.values():
        number, claimed = normalized_reference(code.code), taken.get(('codes', code.pk))
        alias = child_codes.get(number)
        if alias:
            # A grouped SKU's own code is that SKU's and never passes to another. It stays on the canonical SKU while its SKU stays
            # grouped; a restored SKU takes it back or it is removed (code_returns), so the worker never sees it as an alterno of the
            # canonical SKU.
            if claimed and claimed != alias:
                raise ValidationError({'detail': f'{code.code} es el código de {children[alias].sku}: no puede pasar a otro SKU.'})
            if alias not in wanted:
                continue
            had = code_returns(code, children[alias], history)
            if had and not claimed:
                plan[alias]['codes'].append(code.pk)
            elif not had:
                if claimed:
                    plan[alias]['codes'].remove(code.pk)
                removed.append(code)
        elif claimed and number == target_number:
            raise ValidationError({'detail': f'{code.code} es el código de {target.sku}: cámbialo en Editar antes de pasarlo a otro SKU.'})
    for pk in wanted:
        clash = PartCode.objects.filter(code=children[pk].sku).exclude(part_id__in=[target.pk, *wanted]).select_related('part').first()
        if clash:
            raise ValidationError({'detail': f'{children[pk].sku} es alterno de {clash.part.sku}. Corrige esa asignación antes de separarlo.'})

    restored = [children[pk] for pk in wanted]
    for part in restored:
        moves = plan[str(part.pk)]
        PartCode.objects.filter(pk__in=moves['codes']).update(part=part)
        SupplierItem.objects.filter(pk__in=moves['items']).update(part=part)
        PartImage.objects.filter(pk__in=moves['images']).update(part=part)
    if removed:
        PartCode.objects.filter(pk__in=[code.pk for code in removed]).delete()
    for part in [target, *restored]:
        gallery = list(PartImage.objects.filter(part=part).order_by('position', 'created_at', 'pk'))
        for position, image in enumerate(gallery):
            image.position = position
        PartImage.objects.bulk_update(gallery, ['position'])
    for part in restored:
        part.active, part.merged_into, part.estado = True, None, 'publicada'
        part.save(update_fields=['active', 'merged_into', 'estado'])  # its name links come back (mall.oem_links.part_saved)
    links = rehome_catalog_links(target, restored)

    # A parent SKU the grouping created and the split leaves empty has nothing to identify: it retires, which also keeps the worker
    # from proposing the same parent again (mall.matching_engine.catalog_families blocks inactive bases).
    created = any(merge.target_id == target.pk and merge.summary.get('target_created') for merge in merges)
    retired = bool(created and target.active and not Part.objects.filter(merged_into=target).exists() and not PartCode.objects.filter(part=target).exists()
                   and not SupplierItem.objects.filter(part=target).exists() and not PartImage.objects.filter(part=target).exists())
    if retired:
        target.active, target.estado = False, 'retirada'
        target.save(update_fields=['active', 'estado'])
    summary = {'restored_skus': len(restored), 'codes_moved': sum(len(plan[pk]['codes']) for pk in plan), 'aliases_removed': len(removed),
               'items_moved': sum(len(plan[pk]['items']) for pk in plan), 'images_moved': sum(len(plan[pk]['images']) for pk in plan),
               'links_moved': links['moved'], 'links_shared': links['shared'], 'target_retired': retired}
    split = CatalogSplit.objects.create(target=target, actor=actor, reason=reason.strip(), summary=summary,
                                        restored=[{'id': str(part.pk), 'sku': part.sku} for part in restored],
                                        moved={'parts': {pk: {'codes': plan[pk]['codes'], 'items': plan[pk]['items'], 'images': plan[pk]['images']} for pk in plan},
                                               'aliases_removed': [{'brand': code.brand, 'code': code.code} for code in removed]})
    ids = [str(target.pk), *wanted]
    known = set(separated_pairs(ids))
    CatalogSeparation.objects.bulk_create([CatalogSeparation(first_id=first, second_id=second, split=split) for first, second in
                                           sorted({pair(a, b) for index, a in enumerate(ids) for b in ids[index + 1:]} - known)])
    rows = {str(part.pk): part for part in with_merged_counts(catalog_queryset().filter(pk__in=ids))}
    return {'split_id': split.pk, 'target': rows[str(target.pk)], 'restored': [rows[pk] for pk in wanted], 'summary': summary}


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
