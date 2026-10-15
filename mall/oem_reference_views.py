"""Superuser API of the OEM reference library (Biblioteca OEM). From inventory refactor S4 its rows are the library's References (tipo
oem with a brand, mall.references.library_q): list with search, filters and counts; create or merge a number written in any form with
its first source (mall.references.add_library_number); one number with its sources, the lines that carry it, its aftermarket codes and
its supersession; edits and actions under the version the reviewer saw (409 with the current number otherwise). Repeating an action
that is already in place returns the number unchanged.

Ids, URLs and keys stay as they were: a number's id is its Reference's uid (the OEMReference id for the numbers the library had before
S4), its code the compact number (its code without separators), and until S8 status and source kinds keep their legacy values in the JSON, the filters
accepting the new ones too (mall.oem_reference.LibraryNumber). Edits write the number's detail and touch the fields of its line only
when that line is in referencia (the number's own line), never a published one. A dispute puts the Reference in disputa (it stops
holding its key), and no line is unmarked any more. After S4's window the Biblioteca stays read-only for 24 hours (mall.maintenance,
.maintenance/biblioteca, OD22).
"""
import uuid
from collections import Counter

from django.db import connection, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.db.models.lookups import StartsWith
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import oem_reference as orf
from . import references
from .catalog_suffix_models import values
from .management import IsSuperuser, UppercaseCharField
from .models import Part, PartType, Reference, ReferenceSource
from .oem_links import MIN_SEARCH, part_equivalents
from .oem_links import tables_ready as oem_links_ready
from .oem_reference_models import (LINK_SOURCES, MANUAL_KINDS, REFERENCE_STATUSES, SOURCE_KINDS, OEMCrossReference, OEMCrossReferenceRetired,
                                   OEMReference, OEMReferenceSource, citation_list, compact, manufacturer_name)

REFERENCE_LOCK = 724631112  # pg advisory lock key of supersession edits (matching 724631109, quote assistant 724631110, suffixes 724631111)
ACTIONS = ['edit', 'add_source', 'remove_source', 'dispute', 'clear_dispute', 'set_superseded_by', 'clear_superseded_by']
EDITABLE = ('description', 'part_type', 'applications', 'notes')
MANUAL_CHOICES = [(kind, label) for kind, label in SOURCE_KINDS if kind in MANUAL_KINDS]
VIA = ['linea', 'sku', 'oem', 'company', 'unknown', *values(LINK_SOURCES)]
CROSS_PREVIEW = 3
CONFLICT = 'La referencia cambió desde que la revisaste. Revisa sus valores actuales antes de continuar.'
MISSING = 'Ese número no está en la biblioteca OEM.'
MAX_CHAIN = 50
MAX_ROW_IDS = 100
MAX_BULK_ROWS = 500


class OEMReferenceUnavailable(APIException):
    status_code = 503
    default_detail = 'La biblioteca OEM aún no está disponible: faltan migraciones por aplicar.'


def require_tables():
    if not orf.tables_ready() or not oem_links_ready():
        raise OEMReferenceUnavailable()


class OEMReferenceLink(serializers.ModelSerializer):
    class Meta:
        model = OEMReference
        fields = ['id', 'manufacturer', 'code', 'status']
        read_only_fields = fields


class OEMLinkedSKU(serializers.Serializer):
    id = serializers.UUIDField()
    sku = serializers.CharField()
    is_OEM = serializers.BooleanField()
    active = serializers.BooleanField()
    via = serializers.ChoiceField(choices=VIA, help_text='linea: el número está en esa línea (su SKU o un código suyo); sku_name: el código del SKU '
                                                       'nombra el número; catalog: un catálogo de marca de repuesto lo vinculó. sku, oem, company y '
                                                       'unknown quedan de antes de S4.')
    code = serializers.CharField(help_text='Como lo escribe el catálogo.')


class OEMCrossReferenceRow(serializers.ModelSerializer):
    citations = serializers.ListField(child=serializers.CharField(), read_only=True, help_text='Fuentes que imprimen la equivalencia (catálogo y página).')

    class Meta:
        model = OEMCrossReference
        fields = ['id', 'brand', 'code', 'citations']
        read_only_fields = fields


class OEMReferenceSourceRow(serializers.ModelSerializer):
    kind_label = serializers.CharField(source='get_kind_display', read_only=True)
    created_by = serializers.SlugRelatedField(slug_field='username', read_only=True, allow_null=True)
    detail = serializers.DictField(read_only=True, help_text='run, rule y rules_version (buscador OEM); part_codes: los alternos del catálogo que la '
                                                             'respaldan; verified (registro manual); approved (búsqueda con IA aprobada).')
    removable = serializers.BooleanField(read_only=True, help_text='Solo se quitan las fuentes registradas a mano; las demás siguen a los alternos del catálogo.')

    class Meta:
        model = OEMReferenceSource
        fields = ['id', 'kind', 'kind_label', 'citation', 'detail', 'created_by', 'created_at', 'removable']
        read_only_fields = fields


class OEMReferenceRow(serializers.ModelSerializer):
    printed_forms = serializers.ListField(child=serializers.CharField(), read_only=True, help_text='Formas impresas en las fuentes (17801-30070): solo evidencia.')
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    superseded_by = OEMReferenceLink(read_only=True, allow_null=True)
    created_by = serializers.SlugRelatedField(slug_field='username', read_only=True, allow_null=True)
    updated_by = serializers.SlugRelatedField(slug_field='username', read_only=True, allow_null=True)
    source_count = serializers.IntegerField(read_only=True)
    source_kinds = serializers.DictField(child=serializers.IntegerField(), read_only=True, help_text='Fuentes por tipo.')
    linked_skus = OEMLinkedSKU(many=True, read_only=True, help_text=f'Hasta {orf.LINKED_LIMIT} SKU del catálogo que llevan el número.')
    linked_count = serializers.IntegerField(read_only=True)
    cross_reference_count = serializers.IntegerField(read_only=True, help_text='Códigos de marcas de repuesto equivalentes al número.')
    cross_reference_preview = OEMCrossReferenceRow(many=True, read_only=True, help_text=f'Los primeros {CROSS_PREVIEW}, por marca y código.')

    class Meta:
        model = OEMReference
        fields = ['id', 'manufacturer', 'code', 'printed_forms', 'system', 'family', 'part_type', 'description', 'applications', 'status',
                  'status_label', 'dispute_note', 'superseded_by', 'notes', 'source_count', 'source_kinds', 'linked_skus', 'linked_count',
                  'cross_reference_count', 'cross_reference_preview', 'created_by', 'updated_by', 'created_at', 'updated_at', 'version']
        read_only_fields = fields


class OEMReferenceDetail(OEMReferenceRow):
    sources = OEMReferenceSourceRow(many=True, read_only=True)
    supersedes = OEMReferenceLink(many=True, read_only=True, help_text='Números que este reemplaza.')
    cross_references = OEMCrossReferenceRow(many=True, read_only=True, source='cross_reference_rows',
                                            help_text='Todos los códigos de marcas de repuesto equivalentes al número.')

    class Meta(OEMReferenceRow.Meta):
        fields = OEMReferenceRow.Meta.fields + ['sources', 'supersedes', 'cross_references']
        read_only_fields = fields


class PartOEMNumber(serializers.Serializer):
    reference = OEMReferenceLink()
    via = serializers.ListField(child=serializers.ChoiceField(choices=['oem', *values(LINK_SOURCES)]),
                                help_text='oem: un alterno OEM del SKU; sku_name: el código del SKU nombra el número; catalog: un catálogo lo vinculó.')
    citations = serializers.ListField(child=serializers.CharField(), help_text='Catálogo, página y artículo de los vínculos por catálogo.')


class PartCrossReference(serializers.Serializer):
    brand = serializers.CharField()
    code = serializers.CharField()
    citations = serializers.ListField(child=serializers.CharField())
    numbers = serializers.ListField(child=serializers.UUIDField(), help_text='Los números OEM del SKU que tienen esta equivalencia.')
    on_sku = serializers.BooleanField(help_text='El SKU también guarda este código como alterno propio.')


class PartOEMEquivalentsResponse(serializers.Serializer):
    numbers = PartOEMNumber(many=True)
    cross_references = PartCrossReference(many=True)


class OEMReferenceCounts(serializers.Serializer):
    status = serializers.DictField(child=serializers.IntegerField())
    manufacturer = serializers.DictField(child=serializers.IntegerField())
    total = serializers.IntegerField()


class OEMReferenceListResponse(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = OEMReferenceRow(many=True)
    counts = OEMReferenceCounts(help_text='Toda la biblioteca, sin filtros.')


class OEMReferenceSourceInput(serializers.Serializer):
    kind = serializers.ChoiceField(choices=MANUAL_CHOICES)
    citation = serializers.CharField(max_length=500, help_text='URL, o nombre del documento y página.')
    verified = serializers.BooleanField(required=False, default=False, help_text='Solo registro manual: lo comprobaste en una fuente primaria.')

    def validate(self, data):
        if data.get('verified') and data['kind'] != 'manual':
            raise ValidationError({'verified': 'Solo un registro manual se marca como verificado: una lista de precios o el catálogo del fabricante ya lo verifican.'})
        return data


class OEMReferenceCreate(serializers.Serializer):
    manufacturer = UppercaseCharField(max_length=120)
    code = UppercaseCharField(max_length=120, help_text='En cualquier forma escrita (17801-30070): se guarda compacto (1780130070) y la forma escrita queda como evidencia.')
    description = UppercaseCharField(max_length=300, required=False, allow_blank=True)
    part_type = UppercaseCharField(max_length=60, required=False, allow_blank=True)
    applications = UppercaseCharField(max_length=4000, required=False, allow_blank=True)
    source = OEMReferenceSourceInput(required=False, help_text='Primera fuente; sin ella queda un registro manual a tu nombre.')

    def validate(self, data):
        if not manufacturer_name(data['manufacturer']):
            raise ValidationError({'manufacturer': 'Indica el fabricante del número OEM.'})
        if not 0 < len(compact(data['code'])) <= 60:
            raise ValidationError({'code': 'Escribe el número OEM: entre 1 y 60 letras o dígitos (guiones, puntos y espacios no cuentan).'})
        return data


class OEMReferenceEdit(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1, help_text='Versión que revisaste; si cambió, responde 409 con la referencia actual.')
    action = serializers.ChoiceField(choices=ACTIONS, default='edit', help_text='edit cambia description, part_type, applications o notes.')
    description = UppercaseCharField(max_length=300, required=False, allow_blank=True)
    part_type = UppercaseCharField(max_length=60, required=False, allow_blank=True)
    applications = UppercaseCharField(max_length=4000, required=False, allow_blank=True)
    notes = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    source = OEMReferenceSourceInput(required=False, help_text='add_source: la fuente nueva.')
    source_id = serializers.IntegerField(required=False, help_text='remove_source: la fuente que se quita.')
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, help_text='dispute: por qué el número está en disputa.')
    manufacturer = UppercaseCharField(max_length=120, required=False, allow_blank=True,
                                      help_text='set_superseded_by: fabricante del número que lo reemplaza (por defecto, el mismo).')
    code = UppercaseCharField(max_length=120, required=False, allow_blank=True, help_text='set_superseded_by: el número que lo reemplaza, en cualquier forma escrita.')


class OEMReferenceConflict(serializers.Serializer):
    detail = serializers.CharField()
    reference = OEMReferenceDetail()


def decorate(numbers, *, all_cross_references=False):
    """Source summary, lines that carry it and aftermarket codes for a page of numbers (LibraryNumbers): a few queries for the whole
    page, none per row."""
    linked = orf.linked_parts([number.ref for number in numbers])
    codes = {number.ref.pk: [] for number in numbers}
    for row in OEMCrossReference.objects.filter(reference_id__in=list(codes)).order_by('brand', 'number', 'pk'):
        codes[row.reference_id].append(row)
    for number in numbers:
        sources = list(number.sources)
        number.source_count, number.source_kinds = len(sources), dict(Counter(source.kind for source in sources))
        found = linked[number.ref.pk]
        number.linked_skus, number.linked_count = found[:orf.LINKED_LIMIT], len(found)
        number.cross_reference_count, number.cross_reference_preview = len(codes[number.ref.pk]), codes[number.ref.pk][:CROSS_PREVIEW]
        if all_cross_references:
            number.cross_reference_rows = codes[number.ref.pk]
    return numbers


def locate(pk, *, lock=False):
    """The library's Reference with this id (uid); NotFound otherwise. lock: for update."""
    rows = Reference.objects.filter(references.library_q())
    try:
        ref = (rows.select_for_update(of=('self',)) if lock else rows).filter(uid=pk).first()
    except (ValueError, ValidationError):
        ref = None
    if ref is None:
        raise NotFound(MISSING)
    return ref


def detail(pk):
    ref = orf.by_uid(pk)
    if ref is None:
        raise NotFound(MISSING)
    return OEMReferenceDetail(decorate(orf.wrap([ref]), all_cross_references=True)[0]).data


def stamped(ref, user, detail_changes=None):
    """The number's detail with the changes and who made them (updated_by: the Biblioteca shows it)."""
    return {**(ref.detail or {}), **(detail_changes or {}), 'updated_by': getattr(user, 'username', '') or ''}


def save(ref, user, *, detail_changes=None, **fields):
    """Write a number's detail texts and fields (mall.references.update), who changed it, and its line's texts when the number is the
    main of a line in referencia."""
    ref = references.update(ref, actor=user, detail=stamped(ref, user, detail_changes), **fields)
    if detail_changes and ref.is_main:
        line = Part.objects.select_for_update().get(pk=ref.part_id)
        if line.estado == 'referencia':
            sync_line(line, ref, detail_changes)
    return ref


def sync_line(line, ref, changes):
    """A line in referencia follows its number's texts: description, applications and the subgrupo its part type maps to (A6)."""
    fields, sources = [], dict(line.field_sources or {})
    if 'description' in changes and line.description != changes['description']:
        line.description = changes['description']
        fields.append('description')
        sources['description'] = {'source': references.LIBRARY_ORIGIN}
    if 'applications' in changes and line.aplicaciones_texto != changes['applications']:
        line.aplicaciones_texto = changes['applications']
        fields.append('aplicaciones_texto')
        sources['aplicaciones_texto'] = {'source': references.LIBRARY_ORIGIN}
    if 'part_type' in changes:
        mapped = references.library_line_fields(part_type=changes['part_type'])
        category, subcategory = mapped.get('category', ''), mapped.get('subcategory', '')
        if (line.category, line.subcategory) != (category, subcategory):
            line.category, line.subcategory = category, subcategory
            fields += ['category', 'subcategory', 'part_type']
        sources.pop('subgrupo', None)
        if 'subgrupo' in mapped['field_sources']:
            sources['subgrupo'] = mapped['field_sources']['subgrupo']
    if fields or sources != (line.field_sources or {}):
        line.field_sources = sources
        line.save(update_fields=[*fields, 'field_sources'])


def creates_cycle(ref, target):
    """Would ref -> target close a loop: does the chain of numbers that replace target lead back to ref?"""
    node, seen = target, set()
    while node is not None and node.pk not in seen and len(seen) < MAX_CHAIN:
        if node.pk == ref.pk:
            return True
        seen.add(node.pk)
        node = Reference.all_objects.only('id', 'reemplazado_por').filter(pk=node.reemplazado_por_id).first() if node.reemplazado_por_id else None
    return False


def lock_chains():
    if connection.vendor == 'postgresql':
        # Serializes supersession edits, so two of them can never close a loop concurrently.
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [REFERENCE_LOCK])


def plan(ref, data, user):
    """The action as a callable that applies it to the locked number (its Reference), or None when it is already in place; invalid
    requests raise."""
    action = data['action']
    current = orf.LibraryNumber(ref)
    if action == 'edit':
        given = {field: data[field] for field in EDITABLE if field in data}
        if not given:
            raise ValidationError('Indica la descripción, el tipo de pieza, las aplicaciones o las notas que cambias.')
        changes = {field: value for field, value in given.items() if getattr(current, field) != value}
        if not changes:
            return None
        texts = {field: value for field, value in changes.items() if field != 'notes'}
        return lambda: save(ref, user, detail_changes=texts, **({'notes': changes['notes']} if 'notes' in changes else {}))
    if action == 'add_source':
        source = data.get('source')
        if not source:
            raise ValidationError({'source': 'Indica el tipo de fuente y su cita.'})
        extra = {'verified': source['verified']} if source['kind'] == 'manual' else {}
        existing = next((row for row in current.sources if row.kind == source['kind'] and row.citation == source['citation']), None)
        if existing and orf.merge_detail(existing.detail, extra) == existing.detail:
            return None

        def add_source():
            references.cite_library(ref, source['kind'], source['citation'], extra, user)
            references.refresh_library_estado(save(ref, user), user)
        return add_source
    if action == 'remove_source':
        if data.get('source_id') is None:
            raise ValidationError({'source_id': 'Indica la fuente que quitas.'})
        sources = list(current.sources)
        source = next((row for row in sources if row.id == data['source_id']), None)
        if source is None:
            return None
        if not source.removable:
            raise ValidationError({'source_id': 'Las fuentes del catálogo, del buscador OEM y de la búsqueda con IA siguen a los alternos del catálogo: '
                                                'retira el alterno desde el inventario.'})
        if len(sources) <= 1:
            raise ValidationError({'source_id': 'La referencia necesita al menos una fuente. Si el número no es correcto, márcalo en disputa.'})

        def remove():  # a source is retired, never deleted, and the estado follows the ones left
            ReferenceSource.objects.filter(pk=source.id).update(retirada=True, retired_by=user if getattr(user, 'pk', None) else None,
                                                                retired_at=timezone.now())
            references.refresh_library_estado(save(ref, user), user)
        return remove
    if action == 'dispute':
        note = (data.get('note') or '').strip()
        if not note:
            raise ValidationError({'note': 'Explica por qué el número está en disputa.'})
        if ref.estado == 'disputa' and ref.dispute_note == note:
            return None
        line = Part.objects.filter(pk=ref.part_id).only('sku', 'estado').first()
        if ref.is_main and line is not None and line.estado in ('publicada', 'retirada'):
            # In disputa a main frees its line's codigo for any other line (and the matching worker would propose a merge into it):
            # the SKU is corrected first, in the inventory.
            raise ValidationError({'note': f'{ref.brand} {compact(ref.code)} es el código principal del SKU {line.sku}: si el número no es '
                                           'correcto, cambia primero el código principal del SKU en el inventario.'})
        return lambda: save(ref, user, estado='disputa', dispute_note=note)  # in dispute it holds no key; no line is unmarked
    if action == 'clear_dispute':
        if ref.estado != 'disputa':
            return None
        return lambda: save(ref, user, estado=references.library_estado(ref), dispute_note='')  # its key again, as its sources say
    if action == 'clear_superseded_by':
        return (lambda: save(ref, user, reemplazado_por=None)) if ref.reemplazado_por_id else None
    manufacturer, number = manufacturer_name(data.get('manufacturer') or ref.brand), compact(data.get('code'))
    if not 0 < len(number) <= 60:
        raise ValidationError({'code': 'Escribe el número que lo reemplaza: entre 1 y 60 letras o dígitos.'})
    if (manufacturer, number) == (ref.brand, compact(ref.code)):
        raise ValidationError({'code': 'Un número no puede reemplazarse a sí mismo.'})
    target = (Reference.objects.filter(references.library_q(), references.compact_q([number]), brand=manufacturer)
              .order_by(references.exclusive_first(), 'pk').first())
    if target and ref.reemplazado_por_id == target.pk:
        return None
    if target and creates_cycle(ref, target):
        raise ValidationError({'code': f'{target.brand} {compact(target.code)} ya está reemplazado, directa o indirectamente, por {ref.brand} '
                                       f'{compact(ref.code)}: el reemplazo formaría un ciclo.'})

    def supersede():
        new = target or references.add_library_number(
            manufacturer, data['code'], printed=[data['code']], source_kind='manual', citation=f'Reemplaza a {ref.brand} {compact(ref.code)}',
            source_detail={'verified': False, 'supersedes': str(ref.uid)}, actor=user, part_type=current.part_type,
            description=current.description)[0]
        save(ref, user, reemplazado_por=new)
    return supersede


SORTABLE = {'code': ('clave',), 'manufacturer': ('brand', 'clave'), 'status': ('estado',), 'family': ('detail__family',),
            'part_type': ('detail__part_type',), 'description': ('detail__description',), 'updated_at': ('updated_at',)}


def ordering(value):
    """Grid sorting from ?ordering= (unknown names ignored), always ending in a unique order so page-sized blocks stay stable."""
    fields = []
    for item in (value or '').split(','):
        name = item.strip().lstrip('-')
        fields += [('-' if item.strip().startswith('-') else '') + field for field in SORTABLE.get(name, ())]
    return [*fields, 'brand', 'clave', 'id'] if fields else ['brand', 'clave', 'id']


def status_estado(value):
    """The Reference estado a status filter names: a legacy status or an estado (both accepted until S8); None when it is neither."""
    if value in orf.LIBRARY_ESTADO:
        return orf.LIBRARY_ESTADO[value]
    return value if value in orf.LEGACY_STATUS else None


def source_kind_q(value):
    """Q for the sources of one kind: an old kind (kept in detail.legacy_kind, or read back from the kind of a source without one) or a
    ReferenceSource kind. None when the value is neither."""
    legacy, kinds = orf.kinds_for(value)
    if not legacy and not kinds:
        return None
    match = Q(detail__legacy_kind__in=sorted(legacy)) | Q(kind__in=sorted(kinds))
    for old in legacy:  # sources written before S4 from a code's reference_source text, which keep no legacy kind
        new = [kind for kind, back in orf.LEGACY_KIND_OF.items() if back == old]
        if old == 'catalog_approval':
            match |= ~Q(detail__has_key='legacy_kind') & Q(kind='proveedor', citation__startswith='Declarado por')
        if new:
            plain = ~Q(detail__has_key='legacy_kind') & Q(kind__in=new)
            if old == 'supplier_declaration':
                plain &= ~Q(citation__startswith='Declarado por')
            match |= plain
    return match


class OEMReferenceList(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_references_list', responses=OEMReferenceListResponse, parameters=[
        OpenApiParameter('q', OpenApiTypes.STR, description='Inicio del número compacto, una forma impresa, la descripción, el tipo de pieza o un código de '
                                                       'marca de repuesto equivalente (desde 4 letras o dígitos).'),
        OpenApiParameter('manufacturer', OpenApiTypes.STR, description='Fabricante exacto (TOYOTA, HYUNDAI, KIA...).'),
        OpenApiParameter('status', OpenApiTypes.STR, enum=values(REFERENCE_STATUSES), description='También acepta el estado nuevo (verificado, declarado, inferido, disputa).'),
        OpenApiParameter('source_kind', OpenApiTypes.STR, enum=values(SOURCE_KINDS), description='Con al menos una fuente de ese tipo (también acepta el tipo nuevo: catalogo, biblioteca, proveedor, admin, ia, buscador).'),
        OpenApiParameter('linked', OpenApiTypes.STR, enum=['true', 'false'], description='true: alguna línea del catálogo lleva el número; false: ninguna.'),
        OpenApiParameter('ordering', OpenApiTypes.STR, description='Orden del grid, separado por comas, con - para descendente: code, manufacturer, status, family, part_type, description, updated_at.'),
        OpenApiParameter('ids', OpenApiTypes.STR, description=f'Hasta {MAX_ROW_IDS} referencias separadas por comas: las filas que un grid vuelve a leer.'),
        OpenApiParameter('page', OpenApiTypes.INT)],
        description='Biblioteca de números OEM (forma compacta) con sus fuentes y las líneas del catálogo que los llevan, más conteos por estado y fabricante.')
    def get(self, request):
        require_tables()
        params, rows = request.query_params, orf.library_rows()
        if params.get('ids'):  # the rows a grid shows, re-read to patch what changed elsewhere
            try:
                rows = rows.filter(uid__in=[uuid.UUID(value.strip()) for value in params['ids'].split(',') if value.strip()][:MAX_ROW_IDS])
            except ValueError:
                raise ValidationError({'ids': 'Usa identificadores de referencia separados por comas.'})
        text = ' '.join(params.get('q', '').upper().split())
        if text:
            match = (Q(code__icontains=text) | Q(printed_forms__icontains=text) | Q(detail__description__icontains=text)
                     | Q(detail__part_type__icontains=text))
            if compact(text):  # the compact number (a short one's clave is its printed spelling: OPEL 5 69 036)
                match |= Q(clave__startswith=compact(text)) | Q(StartsWith(orf.compact_sql('code'), compact(text)))
            if len(compact(text)) >= MIN_SEARCH:  # an aftermarket code filed under the number (GWT-41A -> TOYOTA 1610039315)
                match |= Q(pk__in=OEMCrossReference.objects.filter(number__contains=compact(text)).values('reference_id'))
            rows = rows.filter(match)
        if params.get('manufacturer'):
            rows = rows.filter(brand=manufacturer_name(params['manufacturer']))
        if params.get('status'):
            estado = status_estado(params['status'])
            if estado is None:
                raise ValidationError({'status': 'El filtro no es válido.'})
            rows = rows.filter(estado=estado)
        if params.get('source_kind'):
            match = source_kind_q(params['source_kind'])
            if match is None:
                raise ValidationError({'source_kind': 'El filtro no es válido.'})
            rows = rows.filter(Exists(ReferenceSource.objects.filter(match, reference=OuterRef('pk'), retirada=False)))
        linked = params.get('linked', '').lower()
        if linked not in ('', 'true', 'false'):
            raise ValidationError({'linked': 'Usa true o false.'})
        if linked:
            rows = rows.filter(orf.linked_q()) if linked == 'true' else rows.exclude(orf.linked_q())
        paginator = PageNumberPagination()
        page = decorate(orf.wrap(paginator.paginate_queryset(rows.order_by(*ordering(params.get('ordering', ''))), request, view=self)))
        every = Reference.objects.filter(references.library_q()).order_by()
        status = Counter()
        for estado, n in every.values('estado').annotate(n=Count('pk')).values_list('estado', 'n'):
            status[orf.LEGACY_STATUS.get(estado, estado)] += n
        counts = {'status': dict(status), 'manufacturer': dict(every.values('brand').annotate(n=Count('pk')).values_list('brand', 'n')),
                  'total': sum(status.values())}
        return Response({'count': paginator.page.paginator.count, 'next': paginator.get_next_link(), 'previous': paginator.get_previous_link(),
                         'results': OEMReferenceRow(page, many=True).data, 'counts': counts})

    @extend_schema(operation_id='v1_management_oem_references_create', request=OEMReferenceCreate, responses={
        201: OEMReferenceDetail, 200: OpenApiResponse(OEMReferenceDetail, description='El número ya estaba en la biblioteca: se unieron la forma escrita y la fuente.')},
        description='Agrega un número OEM escrito en cualquier forma (se guarda compacto) con su primera fuente: registro manual, lista de precios, '
                    'catálogo del fabricante, catálogo de marca de repuesto o declaración de proveedor. Va a la línea que ya lo lleva, o a una '
                    'línea propia en referencia. Repetirlo no duplica nada.')
    def post(self, request):
        require_tables()
        data = OEMReferenceCreate(data=request.data)
        data.is_valid(raise_exception=True)
        data = data.validated_data
        source = data.get('source') or {'kind': 'manual', 'citation': f'Registro manual de {request.user.username}', 'verified': False}
        try:
            ref, created = references.add_library_number(
                data['manufacturer'], data['code'], printed=[data['code']], source_kind=source['kind'], citation=source['citation'],
                source_detail={'verified': source['verified']} if source['kind'] == 'manual' else {}, actor=request.user,
                **{key: data[key] for key in orf.FILLABLE if data.get(key)})
        except references.ReferenceConflict as error:
            raise ValidationError({'code': error.message})
        return Response(detail(ref.uid), status=201 if created else 200)


class OEMReferenceItem(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_reference_detail', responses=OEMReferenceDetail,
                   description='Un número OEM con sus formas impresas, todas sus fuentes, las líneas del catálogo que lo llevan y sus reemplazos.')
    def get(self, request, pk):
        require_tables()
        return Response(detail(pk))

    @extend_schema(operation_id='v1_management_oem_reference_update', request=OEMReferenceEdit, responses={
        200: OEMReferenceDetail, 409: OpenApiResponse(OEMReferenceConflict, description='La referencia cambió desde que la revisaste.')},
        description='edit cambia descripción, tipo de pieza, aplicaciones o notas; add_source y remove_source manejan las fuentes registradas a mano; '
                    'dispute (con nota) y clear_dispute; set_superseded_by (crea el número de reemplazo si falta y rechaza ciclos) y clear_superseded_by.')
    def patch(self, request, pk):
        require_tables()
        data = OEMReferenceEdit(data=request.data)
        data.is_valid(raise_exception=True)
        data = data.validated_data
        with transaction.atomic():
            if data['action'] == 'set_superseded_by':
                lock_chains()
            line_id = Reference.objects.filter(references.library_q(), uid=pk).values_list('part_id', flat=True).first() if is_uuid(pk) else None
            if line_id is not None:  # the line first, then the number (the lock order of every writer)
                list(Part.objects.select_for_update().filter(pk=line_id).values_list('pk', flat=True))
            ref = locate(pk, lock=True)
            apply = plan(ref, data, request.user)
            if apply is not None:
                if ref.version != data['expected_version']:
                    return Response({'detail': CONFLICT, 'reference': detail(ref.uid)}, status=409)
                try:
                    apply()
                except references.ReferenceConflict as error:
                    raise ValidationError({'detail': error.message})
        return Response(detail(ref.uid))


def is_uuid(value):
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


class PartOEMEquivalents(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_oem_equivalents', responses=PartOEMEquivalentsResponse,
                   description='Solo lectura: los números OEM de un SKU del catálogo (los suyos, y los que un catálogo de marca de repuesto o '
                               'su código le vinculó) y los códigos de marcas de repuesto equivalentes a esos números.')
    def get(self, request, pk):
        require_tables()
        part = Part.objects.filter(pk=pk).first()
        if part is None:
            raise NotFound('Ese SKU no está en el catálogo.')
        return Response(PartOEMEquivalentsResponse(part_equivalents(part)).data)


def list_rows(ids):
    """Numbers as the library grid lists them (sources, lines, aftermarket codes), for the given ids (uids)."""
    return OEMReferenceRow(decorate(orf.wrap(orf.library_rows().filter(uid__in=list(ids)).order_by('brand', 'clave'))), many=True).data


def part_types():
    """What Editar celdas offers as a number's part type: the subgrupos' names, and the library's texts no subgrupo maps (A6)."""
    names = set(PartType.objects.values_list('name', flat=True))
    texts = Reference.objects.filter(references.library_q()).exclude(detail__part_type='').values_list('detail__part_type', flat=True).distinct()
    known = orf.subgrupo_names()
    names |= {text for text in texts if text and not orf.subgrupo_for(text, known)}
    return sorted(name for name in names if name)


class OEMPartTypes(serializers.Serializer):
    results = serializers.ListField(child=serializers.CharField())


class OEMReferencePartTypes(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_reference_part_types', responses=OEMPartTypes,
                   description='Los tipos de pieza de la biblioteca OEM: los subgrupos y los textos de la biblioteca que ningún subgrupo recoge '
                               '(Editar celdas los ofrece; un tipo nuevo se escribe en el detalle del número).')
    def get(self, request):
        require_tables()
        return Response({'results': part_types()})


class OEMBulkEditRow(serializers.Serializer):
    id = serializers.UUIDField()
    expected_version = serializers.IntegerField(min_value=1, help_text='La versión que el grid mostraba; si cambió, no se guarda nada (409).')
    description = UppercaseCharField(max_length=300, required=False, allow_blank=True)
    part_type = UppercaseCharField(max_length=60, required=False, allow_blank=True)


class OEMBulkEditRequest(serializers.Serializer):
    rows = OEMBulkEditRow(many=True, allow_empty=False, max_length=MAX_BULK_ROWS)


class OEMBulkEditResponse(serializers.Serializer):
    updated = OEMReferenceRow(many=True)


class OEMBulkEditConflict(serializers.Serializer):
    detail = serializers.CharField()
    rows = OEMReferenceRow(many=True, help_text='Las referencias como están ahora.')


class OEMReferenceBulkEdit(APIView):
    """Biblioteca OEM's Editar celdas: descriptions and part types of several numbers saved together, all or none, each under the version
    the grid showed (the edit action of OEMReferenceItem, so the same rules)."""
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_reference_bulk_edit', request=OEMBulkEditRequest, responses={
        200: OEMBulkEditResponse, 409: OpenApiResponse(OEMBulkEditConflict, description='Otra persona cambió alguna referencia: no se guardó nada.')},
        description='Guarda juntos los cambios de descripción y tipo de pieza de varios números OEM: si uno falla o cambió desde que se mostró, no se guarda ninguno.')
    def post(self, request):
        require_tables()
        data = OEMBulkEditRequest(data=request.data)
        data.is_valid(raise_exception=True)
        rows = data.validated_data['rows']
        known = set(part_types())
        with transaction.atomic():
            uids = [row['id'] for row in rows]
            line_ids = sorted(set(Reference.objects.filter(references.library_q(), uid__in=uids).values_list('part_id', flat=True)), key=str)
            list(Part.objects.select_for_update().filter(pk__in=line_ids).order_by('pk').values_list('pk', flat=True))
            refs = {ref.uid: ref for ref in Reference.objects.select_for_update(of=('self',)).filter(references.library_q(), uid__in=uids).order_by('pk')}
            if len(refs) != len(set(uids)):
                raise ValidationError({'detail': 'Uno de los números ya no está en la biblioteca OEM. Vuelve a cargar la tabla.'})
            stale = [refs[row['id']] for row in rows if refs[row['id']].version != row['expected_version']]
            if stale:
                names = ', '.join(f'{ref.brand} {compact(ref.code)}' for ref in stale[:5])
                return Response({'detail': f'No se guardó ningún cambio: otra persona cambió {names} mientras editabas. Revisa sus valores actuales.',
                                 'rows': list_rows(ref.uid for ref in stale)}, status=409)
            for row in rows:
                fields = {field: row[field] for field in ('description', 'part_type') if field in row}
                if fields.get('part_type') and fields['part_type'] not in known:
                    raise ValidationError({'detail': f'No se guardó ningún cambio: {fields["part_type"]} no es un tipo de pieza de la biblioteca. '
                                                     'Escribe un tipo nuevo en el detalle del número.'})
                apply = plan(refs[row['id']], {'action': 'edit', **fields}, request.user)
                if apply is not None:
                    apply()
        return Response({'updated': list_rows(refs)})


class OEMCrossReferenceInput(serializers.Serializer):
    brand = UppercaseCharField(max_length=120, help_text='La marca de repuesto (AISIN, GMB, FEBEST...).')
    code = UppercaseCharField(max_length=120)


class OEMReferenceCrossReferences(APIView):
    """An aftermarket code added by hand to a number (Biblioteca OEM, Alternos): it takes a manual citation, and a code retired before
    comes back. From S9 (mall.merge_pass.code_added) the code is also a Reference: of the number's line when the code sits on that line
    only (declarado, with an admin source); when the code puts two or more lines together a merge case lists them and the code waits
    there (3f: a code typed by hand never merges lines on its own)."""
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_cross_reference_create', request=OEMCrossReferenceInput, responses={201: OEMCrossReferenceRow},
                   description='Agrega a mano un alterno de posventa al número; todas las líneas que lo llevan lo muestran.')
    def post(self, request, pk):
        require_tables()
        data = OEMCrossReferenceInput(data=request.data)
        data.is_valid(raise_exception=True)
        brand, code = manufacturer_name(data.validated_data['brand']), ' '.join(data.validated_data['code'].split())
        number = compact(code)
        if not brand:
            raise ValidationError({'brand': 'Indica la marca del código.'})
        if not number or not any(char.isdigit() for char in number):
            raise ValidationError({'code': 'El código necesita al menos un dígito.'})
        with transaction.atomic():
            ref = locate(pk, lock=True)
            if brand == ref.brand:
                raise ValidationError({'brand': f'Un código de {brand} es un número OEM, no un alterno de posventa: usa «Reemplazado por» en el detalle.'})
            OEMCrossReferenceRetired.objects.filter(reference=ref, brand=brand, number=number).delete()
            cite = f'Registro manual · {request.user.username}'
            row = OEMCrossReference.objects.filter(reference=ref, brand=brand, number=number).first()
            if row is None:
                row = OEMCrossReference.objects.create(reference=ref, brand=brand, code=code, citations=[cite])
                from .search_document import refresh_reach
                refresh_reach([ref.pk])  # the lines that reach the number find the code (S7)
            elif cite not in row.citations:
                row.citations = citation_list(row.citations, cite)
                row.save(update_fields=['citations', 'updated_at'])
            from .merge_pass import code_added
            code_added(ref, brand, row.code, cite, actor=request.user)
        return Response(OEMCrossReferenceRow(row).data, status=201)


class OEMReferenceCrossReference(APIView):
    """Retire an aftermarket code from a number: every line that carries it stops showing it, and no catalog import files it again.
    From S9 (mall.merge_pass.retire_citation) the sources it gave the code's Reference are retired, an inferido Reference left with
    none, or a declarado one the merge pass made from catalog or Biblioteca rows, goes to disputa (so no line shows it), and a merge
    case lists the lines of an automatic merge the code backed."""
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_cross_reference_retire', responses={204: None},
                   description='Retira el alterno de posventa del número; la importación de catálogos no lo vuelve a agregar (agregarlo a mano lo recupera).')
    def delete(self, request, pk, code_id):
        require_tables()
        with transaction.atomic():
            ref = locate(pk, lock=True)
            row = OEMCrossReference.objects.select_for_update().filter(pk=code_id, reference=ref).first()
            if row is None:
                raise NotFound('Ese alterno ya no está en el número.')
            OEMCrossReferenceRetired.objects.update_or_create(reference=ref, brand=row.brand, number=row.number,
                                                              defaults={'code': row.code, 'citations': row.citations, 'retired_by': request.user})
            from .merge_pass import retire_citation
            retire_citation(ref, row.brand, row.code, row.citations, actor=request.user, exclude_row=row)
            row.delete()
            from .search_document import refresh_reach
            refresh_reach([ref.pk])  # the lines that reach the number no longer find the code (S7)
        return Response(status=204)
