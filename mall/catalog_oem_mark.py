"""Marcar como OEM and Crear OEM desde SKU: endpoints without a screen since the owner retired OEM linking on 2026-10-14 (the
Inventario grid's right click offered them). From inventory refactor S4 the OEM list is the library's References (tipo oem with a
brand, mall.references.library_q); the endpoints keep their plan shapes.

A SKU is OEM when its code is the OEM number: an exact match to a number of the OEM list (Biblioteca OEM). The two actions are
different things:

- Marcar como OEM ties a SKU to the number already in the list with its code (when the number is filed under several makes, the
  administrator picks one). A number no product carried has a line of its own in referencia: Marcar absorbs that line (rule 3f, a
  merge Separar undoes), so the number becomes one of the SKU's References, and the SKU's main takes tipo oem.
- Crear OEM desde SKU adds the number to the list first (mall.references.add_library_number), from the SKU's code, make, description
  and part type, as a manual entry naming who created it: the SKU carries it, so the number goes on the SKU; then the main takes tipo
  oem under that maker when its key is free.

Quitar marca OEM takes the mark off a SKU whose code is not in the list (and can retire its OEM alterno). A SKU whose code is in the
list is OEM by definition: when that number is wrong, it is put in dispute in Biblioteca OEM.
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
from .lines import catalog_lines
from . import references
from .models import Part, Reference
from .oem_finder import OEMFinder
from .oem_reference import LibraryNumber, STATUS_LABELS
from .oem_reference_models import compact, manufacturer_name

MAX_ROWS = 200
MARKED = 'Marcado como OEM por'  # how a marking's citation starts (Quitar marca OEM offers to retire what it filed)
CREATED = 'Creado desde el SKU por'  # how Crear OEM desde SKU cites the list item it adds


def number_problem(sku):
    number = compact(sku)
    if not 1 <= len(number) <= 60 or not any(char.isdigit() for char in number):
        return f'{sku} no parece un número OEM: necesita dígitos y hasta 60 letras o dígitos.'
    return ''


def library():
    """The OEM list: the library's References (S4)."""
    return Reference.objects.filter(references.library_q())


def same_number(part):
    """Q for the library numbers this SKU's code is: the same compact number (a short one's clave is its printed spelling)."""
    return references.compact_q([compact(part.sku)])


def holder(part, make):
    """Another catalog line that already holds this make's number: the two are the same part (Agrupar SKU). A line in referencia is
    the number's own line, which Marcar absorbs."""
    return (library().filter(same_number(part), brand=make).exclude(part=part).exclude(part__estado='referencia')
            .select_related('part').first())


def listed(part):
    """The numbers of the OEM list with this SKU's code (LibraryNumbers), in dispute ones left out."""
    return [LibraryNumber(ref) for ref in library().filter(same_number(part)).exclude(estado='disputa').select_related('reemplazado_por')
            .order_by('brand')]


def listed_refusal(part):
    """Why a SKU cannot stop being OEM: its code is a number of the OEM list (so it is OEM). '' when it can."""
    items = listed(part)
    return (f'{part.sku} es el número {items[0].manufacturer} {items[0].code} de la lista OEM, así que es OEM: si el número no es correcto, '
            'ponlo en disputa en la Biblioteca OEM.') if items else ''


def unlisted_refusal(part):
    """Why a SKU cannot become OEM by itself: its code is not in the OEM list. '' when it is."""
    return '' if listed(part) else (f'{compact(part.sku)} no está en la lista OEM: agrega el número en la Biblioteca OEM o guárdalo como '
                                    'alterno OEM del SKU, con su marca y fuente.')


def description_makes(part):
    makes = OEMFinder.explicit_makes(f'{part.description} {part.name if part.name != part.sku else ""}')
    return [make for make in makes if make != 'OTHER']


def preferred(items, makes):
    """The item whose manufacturer the description names (that make itself before one of its group: TOYOTA before LEXUS), else the
    first one."""
    from .oem_links import same_make
    named = {manufacturer_name(make) for make in makes}
    return (next((item for item in items if item.manufacturer in named), None)
            or next((item for item in items if any(same_make(item.manufacturer, make) for make in makes)), items[0] if items else None))


class ListedItem(serializers.Serializer):
    id = serializers.UUIDField()
    manufacturer = serializers.CharField()
    code = serializers.CharField()
    status = serializers.CharField()
    status_label = serializers.CharField()
    description = serializers.CharField(allow_blank=True)


class MarkOEMRow(serializers.Serializer):
    id = serializers.UUIDField()
    sku = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    is_OEM = serializers.BooleanField()
    makes = serializers.ListField(child=serializers.CharField(), help_text='Las marcas que nombra la descripción.')
    suggested = serializers.CharField(allow_blank=True, help_text='El fabricante propuesto (vacío si no hay ninguno).')
    listed = ListedItem(many=True, help_text='Los ítems de la lista OEM con el código del SKU (Marcar elige uno de estos).')
    description_for_list = serializers.CharField(allow_blank=True, help_text='Crear: la descripción propuesta para el ítem nuevo.')
    part_type_for_list = serializers.CharField(allow_blank=True, help_text='Crear: el tipo de pieza propuesto para el ítem nuevo.')
    warnings = serializers.ListField(child=serializers.CharField())
    blocked = serializers.CharField(allow_blank=True, help_text='Por qué no se puede hacer (vacío si se puede).')
    marked_alterno = serializers.BooleanField(help_text='Tiene el alterno OEM que puso una marca anterior (Quitar marca OEM ofrece retirarlo).')
    oem_brands = serializers.ListField(child=serializers.CharField(), help_text='Los fabricantes de sus alternos OEM con el código del SKU.')


class MarkOEMPlan(serializers.Serializer):
    rows = MarkOEMRow(many=True)
    manufacturers = serializers.ListField(child=serializers.CharField(), help_text='Los fabricantes de la biblioteca OEM, para elegir.')


class MarkOEMInput(serializers.Serializer):
    id = serializers.UUIDField()
    manufacturer = serializers.CharField(max_length=120)
    citation = serializers.CharField(max_length=300, required=False, allow_blank=True, default='',
                                     help_text='La fuente que lo confirma (catálogo, EPC, página); se guarda después de quién lo hizo.')


class CreateOEMInput(MarkOEMInput):
    description = serializers.CharField(max_length=300, required=False, allow_blank=True, default='')
    part_type = serializers.CharField(max_length=60, required=False, allow_blank=True, default='')


class MarkOEMRequest(serializers.Serializer):
    rows = MarkOEMInput(many=True, allow_empty=False, max_length=MAX_ROWS)


class CreateOEMRequest(serializers.Serializer):
    rows = CreateOEMInput(many=True, allow_empty=False, max_length=MAX_ROWS)


class UnmarkOEMInput(serializers.Serializer):
    id = serializers.UUIDField()
    retire_number = serializers.BooleanField(default=False, help_text='Retira también el alterno OEM con el código del SKU.')


class UnmarkOEMRequest(serializers.Serializer):
    rows = UnmarkOEMInput(many=True, allow_empty=False, max_length=MAX_ROWS)


class MarkOEMResult(serializers.Serializer):
    updated = ManagedPartSerializer(many=True)


def plan_rows(ids, mode='mark'):
    from .oem_reference import part_defaults
    parts = list(catalog_lines().filter(pk__in=ids).prefetch_related('codes').order_by('sku'))
    labels = STATUS_LABELS
    rows = []
    for part in parts:
        makes, items = description_makes(part), listed(part)
        warnings, blocked = [], number_problem(part.sku)
        if not part.active:
            blocked = blocked or 'Un SKU inactivo no se marca: actívalo primero.'
        if supplier_base(part.sku):
            warnings.append(f'El código termina en un sufijo de marca o proveedor ({part.sku[len(supplier_base(part.sku)) + 1:]}): '
                            'confirma que es el número OEM tal cual.')
        best = preferred(items, makes)
        disputed = [] if items else list(library().filter(same_number(part), estado='disputa').values_list('brand', flat=True))
        if disputed:
            warnings.append(f'{" / ".join(disputed)} {compact(part.sku)} está en disputa en la Biblioteca OEM: resuelve la disputa antes de marcarlo.')
        if mode == 'mark':
            if not items:
                blocked = blocked or (f'{compact(part.sku)} está en disputa en la lista OEM.' if disputed else
                                      f'{compact(part.sku)} no está en la lista OEM: usa «Crear OEM desde SKU».')
            elif len(items) > 1:
                warnings.append(f'El número está en la lista con {len(items)} fabricantes ({", ".join(item.manufacturer for item in items)}): '
                                'elige el del SKU.')
            suggested = best.manufacturer if best else ''
        else:
            taken = set(library().filter(same_number(part)).values_list('brand', flat=True))
            free = [make for make in makes if manufacturer_name(make) not in taken]
            suggested = free[0] if free else ''  # a make the list already has is refused by create_oem: Marcar ties to it
            if len(makes) > 1:
                warnings.append(f'La descripción nombra {", ".join(makes)}: elige el fabricante del número.')
            if items:
                warnings.append(f'Ya está en la lista como {", ".join(f"{item.manufacturer} {item.code}" for item in items)}: con ese fabricante '
                                'usa «Marcar como OEM»; con otro se agrega un ítem nuevo.')
        other = holder(part, suggested) if suggested else None
        if other:
            warnings.append(f'{suggested} {compact(part.sku)} ya es el OEM de {other.part.sku}: si son el mismo repuesto, agrúpalos.')
        own = [code for code in part.codes.all() if code.ref_type == 'oem' and code.code == part.sku]
        defaults = part_defaults(part.description)
        rows.append({'id': part.pk, 'sku': part.sku, 'description': part.description, 'is_OEM': part.is_OEM, 'makes': makes, 'suggested': suggested,
                     'listed': [{'id': item.pk, 'manufacturer': item.manufacturer, 'code': item.code, 'status': item.status,
                                 'status_label': labels.get(item.status, item.status), 'description': item.description} for item in items],
                     'description_for_list': defaults['description'], 'part_type_for_list': (part.subcategory or defaults['part_type'])[:60],
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


def tie(part, make, cite, actor=None):
    """File the SKU code as the make's OEM number on the SKU (a line in referencia holding it is absorbed first, rule 3f), and give the
    SKU's main tipo oem. Returns a problem or ''."""
    other = holder(part, make)
    if other:
        return f'{make} {compact(part.sku)} ya es el OEM de {other.part.sku}. Si son el mismo repuesto, agrúpalos con Agrupar SKU.'
    try:
        with transaction.atomic():
            references.absorb_library_lines(part, part.sku, make, actor=actor)
            validate_code_mapping(part=part, brand=make, code=part.sku)
    except ValidationError as error:
        return ' '.join(str(item) for item in (error.detail if isinstance(error.detail, list) else [error.detail]))
    codes = list(Reference.objects.select_for_update().filter(part=part, clave=references.clave(part.sku), supplier__isnull=True).order_by('pk'))
    # The make's code already on the SKU, or its neutral alias, becomes the OEM number; otherwise a new one is filed.
    code = (next((item for item in codes if item.brand == make), None)
            or next((item for item in codes if not item.brand and item.ref_type == 'unknown' and not item.is_main), None))
    try:
        with transaction.atomic():
            if code is None:
                code, added = references.add(part, part.sku, make, tipo='oem', source=cite, actor=actor)
                if not added and code.tipo != 'oem':  # the make's spelling of the same key was there under another type
                    references.update(code, actor=actor, tipo='oem', reference_source=code.reference_source.strip() or cite)
            else:
                references.update(code, actor=actor, brand=make, tipo='oem', reference_source=code.reference_source.strip() or cite)
    except (IntegrityError, references.ReferenceConflict):
        return f'{make} {part.sku} ya es alterno de otro SKU. Revisa esa asignación o agrupa los SKU.'
    references.retype_main(part, 'oem', actor=actor, reason=MARKED)
    part.refresh_from_db(fields=['tipo', 'is_OEM'])
    return ''


def citation(prefix, actor, extra):
    extra = ' '.join((extra or '').split())
    return f'{prefix} {actor.username}' + (f' · {extra}' if extra else '')


def locked_parts(rows):
    return {part.pk: part for part in catalog_lines(queryset=Part.objects.select_for_update()).filter(pk__in=[row['id'] for row in rows])}


def checked(part, make):
    if part is None:
        return 'Uno de los SKU ya no está en el catálogo o se agrupó en otro.'
    return number_problem(part.sku) or ('' if part.active else f'{part.sku} está inactivo.') or ('' if make else f'{part.sku}: indica el fabricante.')


@transaction.atomic
def mark_oem(*, actor, rows):
    """Tie each SKU to the item of the OEM list with its code under the chosen make. All or none."""
    parts, errors = locked_parts(rows), []
    for row in rows:
        part, make = parts.get(row['id']), manufacturer_name(row['manufacturer'])
        problem = checked(part, make)
        if not problem and not library().filter(same_number(part), brand=make).exclude(estado='disputa').exists():
            problem = f'{make} {compact(part.sku)} no está en la lista OEM: usa «Crear OEM desde SKU».'
        problem = problem or tie(part, make, citation(MARKED, actor, row.get('citation')), actor)
        if problem:
            errors.append(problem)
    if errors:
        transaction.set_rollback(True)
        raise ValidationError({'detail': 'No se marcó ningún SKU: ' + ' '.join(errors)})
    return updated_rows(parts)


@transaction.atomic
def create_oem(*, actor, rows):
    """Add each SKU's code to the OEM list under the chosen make (a manual entry naming who created it, with the description and part
    type given: mall.references.add_library_number puts it on the SKU, which carries it), then tie the SKU to it and give its main the
    maker when that key is free. A number the list already has under that make is refused: Marcar como OEM ties to it. All or none."""
    parts, errors = locked_parts(rows), []
    for row in rows:
        part, make = parts.get(row['id']), manufacturer_name(row['manufacturer'])
        problem = checked(part, make)
        existing = None if problem else library().filter(same_number(part), brand=make).first()
        if existing:
            problem = (f'{make} {compact(part.sku)} está en disputa en la Biblioteca OEM: resuelve la disputa primero.' if existing.estado == 'disputa'
                       else f'{make} {compact(part.sku)} ya está en la lista OEM: usa «Marcar como OEM».')
        if problem:
            errors.append(problem)
            continue
        try:
            references.add_library_number(make, part.sku, printed=[part.sku], source_kind='manual', citation=citation(CREATED, actor, row.get('citation'))[:500],
                                          source_detail={'verified': False, 'part': str(part.pk), 'sku': part.sku}, actor=actor,
                                          description=' '.join((row.get('description') or part.description or '').upper().split())[:300],
                                          part_type=' '.join((row.get('part_type') or '').upper().split())[:60])
        except references.ReferenceConflict as error:
            errors.append(error.message)
            continue
        problem = tie(part, make, citation(MARKED, actor, row.get('citation')), actor)
        if problem:
            errors.append(problem)
            continue
        references.retype_main(part, 'oem', brand=make, actor=actor, reason=CREATED)
    if errors:
        transaction.set_rollback(True)
        raise ValidationError({'detail': 'No se creó ningún OEM: ' + ' '.join(errors)})
    return updated_rows(parts)


@transaction.atomic
def unmark_oem(*, rows, actor=None):
    parts = locked_parts(rows)
    if len(parts) != len({row['id'] for row in rows}):
        raise ValidationError({'detail': 'Uno de los SKU ya no está en el catálogo o se agrupó en otro.'})
    errors = [problem for problem in (listed_refusal(parts[row['id']]) for row in rows) if problem]
    if errors:
        raise ValidationError({'detail': 'No se quitó ninguna marca: ' + ' '.join(errors)})
    for row in rows:
        part = parts[row['id']]
        if row['retire_number']:  # the OEM alterno its marking filed is retired (mall.references), and its number stays in the list
            references.retire(Reference.objects.filter(part=part, ref_type='oem', code=part.sku, is_main=False), actor=actor,
                              reason='Quitar marca OEM')
        # The mark is the main's tipo (S2b): it goes back to otro, and with retire_number the make leaves the main too.
        main = references.main_of(part)
        if part.is_OEM or (main is not None and main.tipo == 'oem'):
            references.retype_main(part, 'otro', brand='' if row['retire_number'] else None, actor=actor, reason='Quitar marca OEM')
    return updated_rows(parts)


def manufacturers():
    return sorted(set(library().values_list('brand', flat=True).distinct()))


class CatalogMarkOEM(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_mark_oem_plan', responses=MarkOEMPlan,
                   parameters=[OpenApiParameter('ids', OpenApiTypes.STR, description=f'Hasta {MAX_ROWS} SKU separados por comas.')],
                   description='Para marcar SKU como OEM: los ítems de la lista OEM con su código, el fabricante propuesto y avisos.')
    def get(self, request):
        return Response(MarkOEMPlan({'rows': plan_rows(parse_ids(request.query_params.get('ids')), 'mark'), 'manufacturers': manufacturers()}).data)

    @extend_schema(operation_id='v1_management_catalog_mark_oem', request=MarkOEMRequest, responses=MarkOEMResult,
                   description='Marca los SKU como OEM: cada uno se vincula al ítem de la lista OEM con su código y el fabricante elegido (su '
                               'código queda como alterno OEM). Todos o ninguno.')
    def post(self, request):
        data = MarkOEMRequest(data=request.data)
        data.is_valid(raise_exception=True)
        rows = mark_oem(actor=request.user, rows=data.validated_data['rows'])
        return Response({'updated': ManagedPartSerializer(rows, many=True, context={'request': request}).data})


class CatalogCreateOEM(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_create_oem_plan', responses=MarkOEMPlan,
                   parameters=[OpenApiParameter('ids', OpenApiTypes.STR, description=f'Hasta {MAX_ROWS} SKU separados por comas.')],
                   description='Para crear el OEM desde SKU: el fabricante que nombra la descripción, la descripción y el tipo de pieza propuestos.')
    def get(self, request):
        return Response(MarkOEMPlan({'rows': plan_rows(parse_ids(request.query_params.get('ids')), 'create'), 'manufacturers': manufacturers()}).data)

    @extend_schema(operation_id='v1_management_catalog_create_oem', request=CreateOEMRequest, responses=MarkOEMResult,
                   description='Agrega el código de cada SKU a la lista OEM con el fabricante elegido (registro manual) y marca el SKU como OEM. '
                               'Todos o ninguno.')
    def post(self, request):
        data = CreateOEMRequest(data=request.data)
        data.is_valid(raise_exception=True)
        rows = create_oem(actor=request.user, rows=data.validated_data['rows'])
        return Response({'updated': ManagedPartSerializer(rows, many=True, context={'request': request}).data})


class CatalogUnmarkOEM(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_unmark_oem', request=UnmarkOEMRequest, responses=MarkOEMResult,
                   description='Quita la marca OEM de SKU cuyo código no está en la lista OEM; con retire_number retira también su alterno OEM.')
    def post(self, request):
        data = UnmarkOEMRequest(data=request.data)
        data.is_valid(raise_exception=True)
        rows = unmark_oem(rows=data.validated_data['rows'], actor=request.user)
        return Response({'updated': ManagedPartSerializer(rows, many=True, context={'request': request}).data})
