"""Marcar como OEM, from the Inventario grid: the SKU's own code is the OEM part number of a make. Marking files it as the SKU's OEM
alterno (that make, the SKU code, a citation naming who marked it) and makes the SKU MAIN OEM; the OEM library mirrors the alterno
(mall.oem_reference.sync_from_partcode), so the number enters Biblioteca OEM with its source. Unmarking takes the MAIN flag off and,
when asked, retires that alterno, whose library source goes with it.
"""
import uuid

from django.db import IntegrityError, transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .catalog_families import supplier_base
from .management import IsSuperuser, ManagedPartSerializer, catalog_queryset, validate_code_mapping, with_merged_counts
from .models import Part, PartCode
from .oem_finder import OEMFinder
from .oem_reference_models import OEMReference, compact, manufacturer_name

MAX_ROWS = 200
MARKED = 'Marcado como OEM por'  # how a marking's citation starts (Quitar marca OEM offers to retire what it filed)


def number_problem(sku):
    number = compact(sku)
    if not 1 <= len(number) <= 60 or not any(char.isdigit() for char in number):
        return f'{sku} no parece un número OEM: necesita dígitos y hasta 60 letras o dígitos.'
    return ''


def holder(part, make):
    """Another SKU that already files this make's number as its OEM alterno: the two are the same part (Agrupar SKU)."""
    return (PartCode.objects.filter(ref_type='oem', oem_reference__manufacturer=make, oem_reference__code=compact(part.sku))
            .exclude(part=part).select_related('part').first())


class MarkOEMRow(serializers.Serializer):
    id = serializers.UUIDField()
    sku = serializers.CharField()
    description = serializers.CharField()
    is_OEM = serializers.BooleanField()
    makes = serializers.ListField(child=serializers.CharField(), help_text='Las marcas que nombra la descripción.')
    suggested = serializers.CharField(help_text='La marca propuesta (vacía si la descripción no nombra ninguna).')
    warnings = serializers.ListField(child=serializers.CharField())
    blocked = serializers.CharField(help_text='Por qué no se puede marcar (vacío si se puede).')
    marked_alterno = serializers.BooleanField(help_text='Tiene el alterno OEM que puso una marca anterior (Quitar marca OEM ofrece retirarlo).')
    oem_brands = serializers.ListField(child=serializers.CharField(), help_text='Los fabricantes de sus alternos OEM con el código del SKU.')


class MarkOEMPlan(serializers.Serializer):
    rows = MarkOEMRow(many=True)
    manufacturers = serializers.ListField(child=serializers.CharField(), help_text='Los fabricantes de la biblioteca OEM, para elegir.')


class MarkOEMInput(serializers.Serializer):
    id = serializers.UUIDField()
    manufacturer = serializers.CharField(max_length=120)
    citation = serializers.CharField(max_length=300, required=False, allow_blank=True, default='',
                                     help_text='La fuente que lo confirma (catálogo, EPC, página); se guarda después de quién lo marcó.')


class MarkOEMRequest(serializers.Serializer):
    rows = MarkOEMInput(many=True, allow_empty=False, max_length=MAX_ROWS)


class UnmarkOEMInput(serializers.Serializer):
    id = serializers.UUIDField()
    retire_number = serializers.BooleanField(default=False, help_text='Retira también el alterno OEM con el código del SKU (sale de la biblioteca '
                                                                      'si nada más lo respalda).')


class UnmarkOEMRequest(serializers.Serializer):
    rows = UnmarkOEMInput(many=True, allow_empty=False, max_length=MAX_ROWS)


class MarkOEMResult(serializers.Serializer):
    updated = ManagedPartSerializer(many=True)


def plan_rows(ids):
    parts = list(Part.objects.filter(pk__in=ids, merged_into__isnull=True).prefetch_related('codes').order_by('sku'))
    rows = []
    for part in parts:
        makes = OEMFinder.explicit_makes(f'{part.description} {part.name if part.name != part.sku else ""}')
        makes = [make for make in makes if make != 'OTHER']
        warnings, blocked = [], number_problem(part.sku)
        if not part.active:
            blocked = blocked or 'Un SKU inactivo no se marca: actívalo primero.'
        if supplier_base(part.sku):
            warnings.append(f'El código termina en un sufijo de marca o proveedor ({part.sku[len(supplier_base(part.sku)) + 1:]}): '
                            'confirma que es el número OEM tal cual.')
        if len(makes) > 1:
            warnings.append(f'La descripción nombra {", ".join(makes)}: elige el fabricante del número.')
        suggested = makes[0] if makes else ''
        other = holder(part, suggested) if suggested else None
        if other:
            warnings.append(f'{suggested} {compact(part.sku)} ya es el OEM de {other.part.sku}: si son el mismo repuesto, agrúpalos.')
        own = [code for code in part.codes.all() if code.ref_type == 'oem' and code.code == part.sku]
        rows.append({'id': part.pk, 'sku': part.sku, 'description': part.description, 'is_OEM': part.is_OEM, 'makes': makes, 'suggested': suggested,
                     'warnings': warnings, 'blocked': blocked, 'marked_alterno': any(code.reference_source.startswith(MARKED) for code in own),
                     'oem_brands': sorted({code.brand for code in own if code.brand})})
    return rows


def parse_ids(value):
    try:
        return [uuid.UUID(item.strip()) for item in (value or '').split(',') if item.strip()][:MAX_ROWS]
    except ValueError:
        raise ValidationError({'ids': 'Usa identificadores de SKU separados por comas.'})


def updated_rows(ids):
    return with_merged_counts(catalog_queryset().filter(pk__in=list(ids)))


@transaction.atomic
def mark_oem(*, actor, rows):
    parts = {part.pk: part for part in Part.objects.select_for_update().filter(pk__in=[row['id'] for row in rows], merged_into__isnull=True)}
    errors = []
    for row in rows:
        part = parts.get(row['id'])
        if part is None:
            errors.append('Uno de los SKU ya no está en el catálogo o se agrupó en otro.')
            continue
        make = manufacturer_name(row['manufacturer'])
        problem = number_problem(part.sku) or ('' if part.active else f'{part.sku} está inactivo.') or ('' if make else f'{part.sku}: indica el fabricante.')
        other = holder(part, make) if not problem else None
        if other:
            problem = f'{make} {compact(part.sku)} ya es el OEM de {other.part.sku}. Si son el mismo repuesto, agrúpalos con Agrupar SKU.'
        if not problem:
            try:
                validate_code_mapping(part=part, brand=make, code=part.sku)
            except ValidationError as error:
                problem = ' '.join(str(item) for item in (error.detail if isinstance(error.detail, list) else [error.detail]))
        if problem:
            errors.append(problem)
            continue
        extra = ' '.join(row.get('citation', '').split())
        cite = f'{MARKED} {actor.username}' + (f' · {extra}' if extra else '')
        codes = list(PartCode.objects.select_for_update().filter(part=part, code=part.sku).order_by('pk'))
        # The make's code already on the SKU, or its neutral alias, becomes the OEM alterno; otherwise a new alterno is filed.
        code = next((item for item in codes if item.brand == make), None) or next((item for item in codes if not item.brand and item.ref_type == 'unknown'), None)
        try:
            with transaction.atomic():
                if code is None:
                    PartCode.objects.create(part=part, brand=make, code=part.sku, ref_type='oem', reference_source=cite)
                else:
                    code.brand, code.ref_type = make, 'oem'
                    code.reference_source = code.reference_source.strip() or cite
                    code.save()
        except IntegrityError:
            errors.append(f'{make} {part.sku} ya es alterno de otro SKU. Revisa esa asignación o agrupa los SKU.')
            continue
        if not part.is_OEM:
            part.is_OEM = True
            part.save(update_fields=['is_OEM'])
    if errors:
        transaction.set_rollback(True)
        raise ValidationError({'detail': 'No se marcó ningún SKU: ' + ' '.join(errors)})
    return updated_rows(parts)


@transaction.atomic
def unmark_oem(*, rows):
    parts = {part.pk: part for part in Part.objects.select_for_update().filter(pk__in=[row['id'] for row in rows], merged_into__isnull=True)}
    if len(parts) != len({row['id'] for row in rows}):
        raise ValidationError({'detail': 'Uno de los SKU ya no está en el catálogo o se agrupó en otro.'})
    for row in rows:
        part = parts[row['id']]
        if row['retire_number']:
            for code in PartCode.objects.filter(part=part, ref_type='oem', code=part.sku):
                code.delete()  # its library source goes with it (mall.oem_reference.partcode_deleted)
        if part.is_OEM:
            part.is_OEM = False
            part.save(update_fields=['is_OEM'])
    return updated_rows(parts)


class CatalogMarkOEM(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_mark_oem_plan', responses=MarkOEMPlan,
                   parameters=[OpenApiParameter('ids', OpenApiTypes.STR, description=f'Hasta {MAX_ROWS} SKU separados por comas.')],
                   description='Para marcar SKU como OEM: la marca que nombra cada descripción, avisos y los fabricantes de la biblioteca OEM.')
    def get(self, request):
        manufacturers = sorted(set(OEMReference.objects.values_list('manufacturer', flat=True).distinct()))
        return Response(MarkOEMPlan({'rows': plan_rows(parse_ids(request.query_params.get('ids'))), 'manufacturers': manufacturers}).data)

    @extend_schema(operation_id='v1_management_catalog_mark_oem', request=MarkOEMRequest, responses=MarkOEMResult,
                   description='Marca los SKU como MAIN OEM: su código queda como alterno OEM del fabricante indicado y entra a la biblioteca OEM. '
                               'Todos o ninguno.')
    def post(self, request):
        data = MarkOEMRequest(data=request.data)
        data.is_valid(raise_exception=True)
        rows = mark_oem(actor=request.user, rows=data.validated_data['rows'])
        return Response({'updated': ManagedPartSerializer(rows, many=True, context={'request': request}).data})


class CatalogUnmarkOEM(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_unmark_oem', request=UnmarkOEMRequest, responses=MarkOEMResult,
                   description='Quita la marca MAIN OEM; con retire_number retira también el alterno OEM con el código del SKU.')
    def post(self, request):
        data = UnmarkOEMRequest(data=request.data)
        data.is_valid(raise_exception=True)
        rows = unmark_oem(rows=data.validated_data['rows'])
        return Response({'updated': ManagedPartSerializer(rows, many=True, context={'request': request}).data})
