"""Reviewed grouping for catalog entries that have already been imported."""
from django.db import IntegrityError, transaction
from django.db.models import Q, Max
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .management import IsSuperuser, ManagedPartSerializer, UppercaseCharField
from .models import Alternate, CatalogMerge, Part, PartCode, PartImage, SupplierItem
from .wishlist import merge_wishlists
from .catalog_families import family_candidates, supplier_base


class CatalogMergeRequest(serializers.Serializer):
    target_sku = UppercaseCharField(max_length=200)
    source_skus = serializers.ListField(child=UppercaseCharField(max_length=200), min_length=1, max_length=50)
    category = UppercaseCharField(max_length=120, required=False, allow_blank=True, default='')
    subcategory = UppercaseCharField(max_length=120, required=False, allow_blank=True, default='')
    create_target = serializers.BooleanField(default=False)

    def validate(self, data):
        sources = data['source_skus']
        if data['target_sku'] in sources:
            raise serializers.ValidationError('El SKU principal no puede incluirse entre los alternos por unificar.')
        if len(sources) != len(set(sources)):
            raise serializers.ValidationError('No repitas los SKU por unificar.')
        if any(len(sku) > 120 for sku in sources):
            raise serializers.ValidationError('Los SKU por unificar deben tener hasta 120 caracteres para conservarlos como códigos alternos.')
        return data


def _merge_alternates(source_ids, target):
    """Keep historical relationships without introducing self-links or duplicates."""
    affected = list(Alternate.objects.select_for_update().filter(
        Q(part_id__in=source_ids) | Q(replacement_id__in=source_ids)).order_by('pk'))
    affected_ids = {row.pk for row in affected}
    resulting = {}
    for row in affected:
        pair = (target.pk if row.part_id in source_ids else row.part_id,
                target.pk if row.replacement_id in source_ids else row.replacement_id)
        if pair[0] != pair[1]:
            resulting.setdefault(pair, []).append(row)
    existing = {}
    for pair in resulting:
        relation = Alternate.objects.select_for_update().filter(part_id=pair[0], replacement_id=pair[1]).exclude(pk__in=affected_ids).first()
        if relation:
            existing[pair] = relation
    # Remove the old links before inserting the deduplicated replacement pairs.
    # This avoids unique-constraint conflicts when two old links converge.
    Alternate.objects.filter(pk__in=affected_ids).delete()
    for pair, rows in resulting.items():
        old = existing.get(pair)
        notes = list(dict.fromkeys(note for note in [old.notes if old else '', *(row.notes for row in rows)] if note))
        if old:
            note = '\n'.join(notes)
            if old.notes != note:
                old.notes = note
                old.save(update_fields=['notes'])
        else:
            Alternate.objects.create(part_id=pair[0], replacement_id=pair[1], notes='\n'.join(notes))
    return len(affected), len(affected) - len(set(resulting) - set(existing))


@transaction.atomic
def merge_catalog_parts(*, actor, target_sku, source_skus, category='', subcategory='', create_target=False, assistant_job=None):
    """Preserve the canonical UUID, supplier identities, and every stock entry."""
    requested = {target_sku, *source_skus}
    parts = {part.sku: part for part in Part.objects.select_for_update().filter(sku__in=requested).order_by('pk')}
    missing = sorted(requested - set(parts) - ({target_sku} if create_target else set()))
    if missing:
        raise ValidationError({'detail': f'Estos SKU ya no están en el catálogo: {", ".join(missing)}.'})
    created_target = target_sku not in parts
    if created_target:
        if not create_target or any(supplier_base(sku) != target_sku for sku in source_skus):
            raise ValidationError('El nuevo SKU debe ser el código base completo de los alternos seleccionados.')
        if PartCode.objects.filter(code=target_sku).exists():
            raise ValidationError('El código base ya es un alterno de otro SKU. Revisa su asignación.')
        family_rows = {part.sku: {'sku': part.sku, 'description': part.description, 'is_OEM': part.is_OEM} for part in
                       Part.objects.select_for_update().filter(active=True, merged_into__isnull=True,
                                                               sku__startswith=target_sku + '-').order_by('pk')}
        family = family_candidates(family_rows).get(target_sku)
        if not family or not set(source_skus).issubset({row['sku'] for row in family['sources']}):
            raise ValidationError('El código base requiere al menos dos variantes con descripciones compatibles para crear el SKU padre.')
        target = Part.objects.create(sku=target_sku, name=target_sku, description=family['target']['description'])
        parts[target_sku] = target
    else:
        target = parts[target_sku]
    if not target.active or target.merged_into_id:
        raise ValidationError({'detail': 'Selecciona un SKU principal activo que todavía no esté unificado.'})
    sources, already = [], []
    for sku in source_skus:
        source = parts[sku]
        if source.merged_into_id == target.pk:
            already.append(source)
        elif source.merged_into_id:
            raise ValidationError({'detail': f'El SKU {sku} ya fue unificado con otro SKU. Revisa el catálogo actual.'})
        elif not source.active:
            raise ValidationError({'detail': f'El SKU {sku} está inactivo. Actívalo antes de unificarlo.'})
        else:
            sources.append(source)
    summary = {'target_created': created_target, 'merged_skus': len(sources), 'codes_transferred': 0, 'sku_aliases_created': 0,
               'supplier_items_relinked': 0, 'historical_alternates_updated': 0, 'historical_alternates_removed': 0}
    if assistant_job:
        summary['assistant_job'] = str(assistant_job)
    merge = None
    if sources:
        source_ids = {part.pk for part in sources}
        # Technical sheets and fitment claims require review before retiring an
        # identity. Never silently drop them or broaden compatibility by union.
        from .models import PartSpecification, ItemApplication
        if PartSpecification.objects.filter(part_id__in=source_ids).exists() or ItemApplication.objects.filter(part_id__in=source_ids).exists():
            raise ValidationError('Los SKU de origen tienen fichas técnicas o aplicaciones. Revisa y conserva esos datos en el SKU principal antes de vaciar las fichas de origen y unificarlos.')
        # Flatten older merges if their current canonical SKU is now a source.
        # No merge chain is exposed to matching or catalog management.
        children = list(Part.objects.select_for_update().filter(merged_into_id__in=source_ids).order_by('pk'))
        retired_ids = source_ids | {part.pk for part in children}
        participant_ids = retired_ids | {target.pk}
        source_codes = list(PartCode.objects.select_for_update().filter(part_id__in=retired_ids).order_by('pk'))
        incoming = {(row.brand, row.code) for row in source_codes} | {('', part.sku) for part in sources}
        identifiers = {code for _, code in incoming}
        # A neutral alias reserves the code across brands, matching the same
        # rules used by normal catalog management and supplier matching.
        for row in PartCode.objects.select_for_update().filter(code__in=identifiers).exclude(part_id__in=participant_ids).order_by('pk'):
            if any(row.code == code and (not brand or not row.brand or brand == row.brand) for brand, code in incoming):
                raise ValidationError({'detail': f'El código {row.code} pertenece a otro SKU. Corrige su asignación antes de unificar.'})
        other_skus = Part.objects.select_for_update().filter(sku__in=identifiers).exclude(pk__in=participant_ids).exclude(merged_into=target).order_by('pk')
        collision = other_skus.first()
        if collision:
            raise ValidationError({'detail': f'El código {collision.sku} identifica otro SKU. Corrige su asignación antes de unificar.'})
        summary['codes_transferred'] = PartCode.objects.filter(pk__in=[row.pk for row in source_codes]).update(part=target)
        for source in sources:
            alias, created = PartCode.objects.get_or_create(part=target, brand='', code=source.sku,
                                                        defaults={'ref_type': 'oem' if source.is_OEM else 'unknown'})
            if source.is_OEM and alias.ref_type == 'unknown':
                alias.ref_type = 'oem'
                alias.save(update_fields=['ref_type'])
            summary['sku_aliases_created'] += int(created)
        # Only the catalog link changes. Inventory ID, codigo, brand, stock
        # counters, matching status, timestamps and ledger remain intact.
        list(SupplierItem.objects.select_for_update().filter(part_id__in=retired_ids).order_by('pk').values_list('pk', flat=True))
        summary['supplier_items_relinked'] = SupplierItem.objects.filter(part_id__in=retired_ids).update(part=target)
        updated, removed = _merge_alternates(retired_ids, target)
        summary['historical_alternates_updated'] = updated
        summary['historical_alternates_removed'] = removed
        merge_wishlists(retired_ids, target)
        # Preserve every photo and its immutable storage key when combining SKU.
        maximum = target.images.aggregate(value=Max('position'))['value']
        position = maximum + 1 if maximum is not None else 0
        images = list(PartImage.objects.filter(part_id__in=retired_ids).order_by('part_id', 'position', 'created_at', 'id'))
        for offset, image in enumerate(images):
            image.part = target
            image.position = position + offset
        PartImage.objects.bulk_update(images, ['part', 'position'])
        summary['images_transferred'] = len(images)
        Part.objects.filter(pk__in=retired_ids).update(active=False, merged_into=target)
        merge = CatalogMerge.objects.create(actor=actor, target=target,
                                           sources=[{'id': str(part.pk), 'sku': part.sku} for part in sources], summary=summary)
    metadata_fields = []
    for field, value in [('category', category), ('subcategory', subcategory)]:
        if value and getattr(target, field) != value:
            setattr(target, field, value)
            metadata_fields.append(field)
    if metadata_fields:
        target.save(update_fields=metadata_fields)
    from .catalog_identity import prefer_oem, company_reference, preserve_sku_reference
    for source in sources:
        if not source.is_OEM and company_reference(source.sku):
            preserve_sku_reference(target, source.sku)
    target = prefer_oem(target, actor=actor, confirm_current=True)
    target.stock_record_count = target.supplier_items.count()
    return {'target': ManagedPartSerializer(target).data, 'merged_skus': [part.sku for part in sources],
            'already_merged_skus': [part.sku for part in already], 'summary': summary,
            'merge_id': str(merge.pk) if merge else None, 'retry': not bool(sources)}


class CatalogGroupingMerge(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(request=CatalogMergeRequest, responses=OpenApiTypes.OBJECT,
                   description='Unifica SKU revisados por el superusuario. Conserva sus códigos e identidades de inventario; no modifica existencias ni movimientos.')
    def post(self, request):
        serializer = CatalogMergeRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = merge_catalog_parts(actor=request.user, **serializer.validated_data)
        except IntegrityError:
            raise ValidationError({'detail': 'El catálogo cambió durante la unificación. Revisa los códigos y vuelve a intentarlo.'})
        return Response(result)
