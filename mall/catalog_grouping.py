"""Reviewed grouping for catalog entries that have already been imported: Agrupar SKU, a thin wrapper over mall.line_merge."""
from django.db import IntegrityError, transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .management import IsSuperuser, ManagedPartSerializer, UppercaseCharField
from .lines import LineStateError, all_lines, create_line, on_storefront, set_subgrupo, storefront_lines
from .technical_data import classification, text_subgrupo
from . import line_merge
from .models import Part, Reference
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


@transaction.atomic
def merge_catalog_parts(*, actor, target_sku, source_skus, category='', subcategory='', create_target=False, assistant_job=None,
                        automatic=False):
    """Agrupar SKU (and the matching worker's families, the catalog assistant): the SKUs a superuser reviewed, by codigo, merged through
    mall.line_merge.merge_lines, which keeps the canonical UUID, supplier identities and every stock entry. Here: the request's rules
    (a target and sources on sale, a parent created from its family when asked), then the reviewed taxonomy, the company codes of the
    sources and prefer_oem on the target."""
    requested = {target_sku, *source_skus}
    # Every estado: the exact rows the SKUs name, so the checks below refuse a merged or retired one by name. Supplier accounts are
    # locked before the lines (mall.line_merge.lock_lines).
    # A line in referencia (a library number no product carries, S4) is none of them: a parent with its codigo promotes it.
    found = list(all_lines().filter(sku__in=requested).exclude(estado='referencia').values_list('pk', flat=True))
    locked = line_merge.lock_lines(found)
    parts = {part.sku: part for part in locked.values() if part.sku in requested}
    missing = sorted(requested - set(parts) - ({target_sku} if create_target else set()))
    if missing:
        raise ValidationError({'detail': f'Estos SKU ya no están en el catálogo: {", ".join(missing)}.'})
    created_target = target_sku not in parts
    if created_target:
        if not create_target or any(supplier_base(sku) != target_sku for sku in source_skus):
            raise ValidationError('El nuevo SKU debe ser el código base completo de los alternos seleccionados.')
        if Reference.objects.filter(code=target_sku).exclude(part__estado='referencia').exists():
            raise ValidationError('El código base ya es un alterno de otro SKU. Revisa su asignación.')
        family_rows = {part.sku: {'sku': part.sku, 'description': part.description, 'is_OEM': part.is_OEM} for part in
                       storefront_lines(Part.objects.select_for_update()).filter(sku__startswith=target_sku + '-').order_by('pk')}
        family = family_candidates(family_rows).get(target_sku)
        if not family or not set(source_skus).issubset({row['sku'] for row in family['sources']}):
            raise ValidationError('El código base requiere al menos dos variantes con descripciones compatibles para crear el SKU padre.')
        try:  # its main (S2b); the line in referencia with this codigo, if any, is promoted (S4)
            target = create_line(target_sku, description=family['target']['description'], actor=actor, strict=True)
        except LineStateError as error:  # two makes' library numbers share the codigo
            raise ValidationError({'detail': str(error)})
        parts[target_sku] = target
    else:
        target = parts[target_sku]
    if not on_storefront(target):
        raise ValidationError({'detail': 'Selecciona un SKU principal activo que todavía no esté unificado.'})
    sources, already = [], []
    for sku in source_skus:
        source = parts[sku]
        if source.merged_into_id == target.pk:
            already.append(source)
        elif source.merged_into_id:
            raise ValidationError({'detail': f'El SKU {sku} ya fue unificado con otro SKU. Revisa el catálogo actual.'})
        elif not on_storefront(source):
            raise ValidationError({'detail': f'El SKU {sku} está inactivo. Actívalo antes de unificarlo.'})
        else:
            sources.append(source)
    summary = {'target_created': created_target, 'merged_skus': 0, 'codes_transferred': 0, 'sku_aliases_created': 0,
               'supplier_items_relinked': 0, 'historical_alternates_updated': 0, 'historical_alternates_removed': 0}
    if assistant_job:
        summary['assistant_job'] = str(assistant_job)
    merge = None
    if sources:
        merge = line_merge.merge_lines(target, sources, actor=actor, automatic=automatic, assistant_job=assistant_job,
                                       target_created=created_target, reason='family' if automatic else 'agrupar_sku')
        summary = merge.summary
        target.refresh_from_db()
    if category or subcategory:  # the reviewed grupo / subgrupo names the target's subgrupo; a half left blank stays the target's own
        current = classification(target)
        pair = (category or current[0], subcategory or current[1])
        if pair != current:
            target.save(update_fields=set_subgrupo(target, text_subgrupo(*pair)))
    from .catalog_identity import company_base_code, prefer_oem
    for source in sources:  # a company-suffix source's company code stays with the target (its codigo came along as a Reference)
        if not source.is_OEM:
            company_base_code(target, source.sku, actor)
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
