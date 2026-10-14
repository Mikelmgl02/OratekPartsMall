"""Superuser API of the OEM reference library: list with search, filters and counts; create or merge a number written in any form
with its first source; one reference with its sources, linked SKUs, aftermarket codes and supersession; edits and actions under the
version the reviewer saw (409 with the current reference otherwise). Repeating an action that is already in place returns the
reference unchanged. A catalog SKU's OEM numbers and the aftermarket codes it reaches through them are read-only here.
"""
import uuid
from collections import Counter

from django.db import connection, transaction
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import oem_reference as orf
from .catalog_identity import mark_exact_matches, unmark_disputed
from .catalog_suffix_models import values
from .management import IsSuperuser, UppercaseCharField
from .lines import catalog_lines
from .models import Part
from .oem_links import MIN_SEARCH, part_equivalents
from .oem_links import tables_ready as oem_links_ready
from .oem_reference_models import (LINK_SOURCES, MANUAL_KINDS, REFERENCE_STATUSES, SOURCE_KINDS, OEMCrossReference, OEMCrossReferenceRetired,
                                   OEMReference, OEMReferenceSource, citation_list, compact, manufacturer_name)

REFERENCE_LOCK = 724631112  # pg advisory lock key of supersession edits (matching 724631109, quote assistant 724631110, suffixes 724631111)
ACTIONS = ['edit', 'add_source', 'remove_source', 'dispute', 'clear_dispute', 'set_superseded_by', 'clear_superseded_by']
EDITABLE = ('description', 'part_type', 'applications', 'notes')
MANUAL_CHOICES = [(kind, label) for kind, label in SOURCE_KINDS if kind in MANUAL_KINDS]
VIA = ['sku', 'oem', 'company', 'unknown', *values(LINK_SOURCES)]
CROSS_PREVIEW = 3
CONFLICT = 'La referencia cambió desde que la revisaste. Revisa sus valores actuales antes de continuar.'
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
    via = serializers.ChoiceField(choices=VIA, help_text='sku: el SKU principal es el número; oem, company o unknown: un alterno de ese tipo; '
                                                       'sku_name: el código del SKU nombra el número; catalog: un catálogo de marca de repuesto lo vinculó.')
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


def rows_query():
    return (OEMReference.objects.select_related('superseded_by', 'created_by', 'updated_by')
            .prefetch_related(Prefetch('sources', queryset=OEMReferenceSource.objects.select_related('created_by').order_by('created_at', 'id'))))


def decorate(refs, *, all_cross_references=False):
    """Source summary, linked SKUs and aftermarket codes for a page of references: four queries for the whole page, none per row."""
    linked = orf.linked_parts(refs)
    codes = {ref.pk: [] for ref in refs}
    for row in OEMCrossReference.objects.filter(reference_id__in=list(codes)).order_by('brand', 'number', 'pk'):
        codes[row.reference_id].append(row)
    for ref in refs:
        sources = list(ref.sources.all())
        ref.source_count, ref.source_kinds = len(sources), dict(Counter(source.kind for source in sources))
        ref.linked_skus, ref.linked_count = linked[ref.pk][:orf.LINKED_LIMIT], len(linked[ref.pk])
        ref.cross_reference_count, ref.cross_reference_preview = len(codes[ref.pk]), codes[ref.pk][:CROSS_PREVIEW]
        if all_cross_references:
            ref.cross_reference_rows = codes[ref.pk]
    return refs


def detail(pk):
    ref = rows_query().prefetch_related('supersedes').filter(pk=pk).first()
    if ref is None:
        raise NotFound('Ese número no está en la biblioteca OEM.')
    return OEMReferenceDetail(decorate([ref], all_cross_references=True)[0]).data


def save(ref, user, **fields):
    for field, value in fields.items():
        setattr(ref, field, value)
    ref.updated_by = user
    ref.save()


def creates_cycle(ref, target):
    """Would ref -> target close a loop: does the chain of numbers that replace target lead back to ref?"""
    node, seen = target, set()
    while node is not None and node.pk not in seen and len(seen) < MAX_CHAIN:
        if node.pk == ref.pk:
            return True
        seen.add(node.pk)
        node = OEMReference.objects.only('id', 'superseded_by').filter(pk=node.superseded_by_id).first() if node.superseded_by_id else None
    return False


def lock_chains():
    if connection.vendor == 'postgresql':
        # Serializes supersession edits, so two of them can never close a loop concurrently.
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [REFERENCE_LOCK])


def plan(ref, data, user):
    """The action as a callable that applies it to the locked reference, or None when it is already in place; invalid requests raise."""
    action = data['action']
    if action == 'edit':
        given = {field: data[field] for field in EDITABLE if field in data}
        if not given:
            raise ValidationError('Indica la descripción, el tipo de pieza, las aplicaciones o las notas que cambias.')
        changes = {field: value for field, value in given.items() if getattr(ref, field) != value}
        return (lambda: save(ref, user, **changes)) if changes else None
    if action == 'add_source':
        source = data.get('source')
        if not source:
            raise ValidationError({'source': 'Indica el tipo de fuente y su cita.'})
        extra = {'verified': source['verified']} if source['kind'] == 'manual' else {}
        existing = ref.sources.filter(kind=source['kind'], citation=source['citation']).first()
        if existing and orf.merge_detail(existing.detail, extra) == existing.detail:
            return None
        return lambda: orf.upsert_reference(ref.manufacturer, ref.code, source_kind=source['kind'], citation=source['citation'], detail=extra,
                                            actor=user)
    if action == 'remove_source':
        if data.get('source_id') is None:
            raise ValidationError({'source_id': 'Indica la fuente que quitas.'})
        source = ref.sources.filter(pk=data['source_id']).first()
        if source is None:
            return None
        if not source.removable:
            raise ValidationError({'source_id': 'Las fuentes del catálogo, del buscador OEM y de la búsqueda con IA siguen a los alternos del catálogo: '
                                                'retira el alterno desde el inventario.'})
        if not ref.sources.exclude(pk=source.pk).exists():
            raise ValidationError({'source_id': 'La referencia necesita al menos una fuente. Si el número no es correcto, márcalo en disputa.'})

        def remove():
            source.delete()
            orf.refresh_status(ref, user, changed=True)
        return remove
    if action == 'dispute':
        note = (data.get('note') or '').strip()
        if not note:
            raise ValidationError({'note': 'Explica por qué el número está en disputa.'})
        if ref.status == 'disputed' and ref.dispute_note == note:
            return None

        def dispute():
            save(ref, user, status='disputed', dispute_note=note)
            unmark_disputed(ref, user)  # a number in dispute no longer makes its SKUs OEM
        return dispute
    if action == 'clear_dispute':
        if ref.status != 'disputed':
            return None

        def clear():
            ref.status, ref.dispute_note = 'inferred', ''  # refresh_status re-derives it from the sources
            orf.refresh_status(ref, user, changed=True)
            mark_exact_matches(user, codes=[ref.code])
        return clear
    if action == 'clear_superseded_by':
        return (lambda: save(ref, user, superseded_by=None)) if ref.superseded_by_id else None
    manufacturer, number = manufacturer_name(data.get('manufacturer') or ref.manufacturer), compact(data.get('code'))
    if not 0 < len(number) <= 60:
        raise ValidationError({'code': 'Escribe el número que lo reemplaza: entre 1 y 60 letras o dígitos.'})
    if (manufacturer, number) == (ref.manufacturer, ref.code):
        raise ValidationError({'code': 'Un número no puede reemplazarse a sí mismo.'})
    target = OEMReference.objects.filter(manufacturer=manufacturer, code=number).first()
    if target and ref.superseded_by_id == target.pk:
        return None
    if target and creates_cycle(ref, target):
        raise ValidationError({'code': f'{target.manufacturer} {target.code} ya está reemplazado, directa o indirectamente, por {ref.manufacturer} '
                                       f'{ref.code}: el reemplazo formaría un ciclo.'})

    def supersede():
        new = target or orf.upsert_reference(manufacturer, data['code'], printed=[data['code']], source_kind='manual',
                                             citation=f'Reemplaza a {ref.manufacturer} {ref.code}', detail={'verified': False, 'supersedes': str(ref.pk)},
                                             actor=user, defaults={'part_type': ref.part_type, 'description': ref.description})[0]
        save(ref, user, superseded_by=new)
    return supersede


SORTABLE = {'code': ('code',), 'manufacturer': ('manufacturer', 'code'), 'status': ('status',), 'family': ('family',),
            'part_type': ('part_type',), 'description': ('description',), 'updated_at': ('updated_at',)}


def ordering(value):
    """Grid sorting from ?ordering= (unknown names ignored), always ending in a unique order so page-sized blocks stay stable."""
    fields = []
    for item in (value or '').split(','):
        name = item.strip().lstrip('-')
        fields += [('-' if item.strip().startswith('-') else '') + field for field in SORTABLE.get(name, ())]
    return [*fields, 'manufacturer', 'code', 'id'] if fields else ['manufacturer', 'code']


class OEMReferenceList(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_references_list', responses=OEMReferenceListResponse, parameters=[
        OpenApiParameter('q', OpenApiTypes.STR, description='Inicio del número compacto, una forma impresa, la descripción, el tipo de pieza o un código de '
                                                       'marca de repuesto equivalente (desde 4 letras o dígitos).'),
        OpenApiParameter('manufacturer', OpenApiTypes.STR, description='Fabricante exacto (TOYOTA, HYUNDAI, KIA...).'),
        OpenApiParameter('status', OpenApiTypes.STR, enum=values(REFERENCE_STATUSES)),
        OpenApiParameter('source_kind', OpenApiTypes.STR, enum=values(SOURCE_KINDS), description='Con al menos una fuente de ese tipo.'),
        OpenApiParameter('linked', OpenApiTypes.STR, enum=['true', 'false'], description='true: algún SKU del catálogo lleva el número; false: ninguno.'),
        OpenApiParameter('ordering', OpenApiTypes.STR, description='Orden del grid, separado por comas, con - para descendente: code, manufacturer, status, family, part_type, description, updated_at.'),
        OpenApiParameter('ids', OpenApiTypes.STR, description=f'Hasta {MAX_ROW_IDS} referencias separadas por comas: las filas que un grid vuelve a leer.'),
        OpenApiParameter('page', OpenApiTypes.INT)],
        description='Biblioteca de números OEM (forma compacta) con sus fuentes y los SKU del catálogo que los llevan, más conteos por estado y fabricante.')
    def get(self, request):
        require_tables()
        params, rows = request.query_params, rows_query()
        if params.get('ids'):  # the rows a grid shows, re-read to patch what changed elsewhere
            try:
                rows = rows.filter(pk__in=[uuid.UUID(value.strip()) for value in params['ids'].split(',') if value.strip()][:MAX_ROW_IDS])
            except ValueError:
                raise ValidationError({'ids': 'Usa identificadores de referencia separados por comas.'})
        text = ' '.join(params.get('q', '').upper().split())
        if text:
            match = Q(printed_forms__icontains=text) | Q(description__icontains=text) | Q(part_type__icontains=text)
            if compact(text):
                match |= Q(code__startswith=compact(text))
            if len(compact(text)) >= MIN_SEARCH:  # an aftermarket code filed under the number (GWT-41A -> TOYOTA 1610039315)
                match |= Q(pk__in=OEMCrossReference.objects.filter(number__contains=compact(text)).values('reference_id'))
            rows = rows.filter(match)
        if params.get('manufacturer'):
            rows = rows.filter(manufacturer=manufacturer_name(params['manufacturer']))
        for key, choices in (('status', REFERENCE_STATUSES), ('source_kind', SOURCE_KINDS)):
            if params.get(key) and params[key] not in values(choices):
                raise ValidationError({key: 'El filtro no es válido.'})
        if params.get('status'):
            rows = rows.filter(status=params['status'])
        if params.get('source_kind'):
            rows = rows.filter(Exists(OEMReferenceSource.objects.filter(reference=OuterRef('pk'), kind=params['source_kind'])))
        linked = params.get('linked', '').lower()
        if linked not in ('', 'true', 'false'):
            raise ValidationError({'linked': 'Usa true o false.'})
        if linked:
            rows = rows.annotate(pair=orf.reference_pair())
            rows = rows.filter(orf.linked_q()) if linked == 'true' else rows.exclude(orf.linked_q())
        paginator = PageNumberPagination()
        page = decorate(paginator.paginate_queryset(rows.order_by(*ordering(params.get('ordering', ''))), request, view=self))
        every = OEMReference.objects.order_by()
        status = dict(every.values('status').annotate(n=Count('pk')).values_list('status', 'n'))
        counts = {'status': status, 'manufacturer': dict(every.values('manufacturer').annotate(n=Count('pk')).values_list('manufacturer', 'n')),
                  'total': sum(status.values())}
        return Response({'count': paginator.page.paginator.count, 'next': paginator.get_next_link(), 'previous': paginator.get_previous_link(),
                         'results': OEMReferenceRow(page, many=True).data, 'counts': counts})

    @extend_schema(operation_id='v1_management_oem_references_create', request=OEMReferenceCreate, responses={
        201: OEMReferenceDetail, 200: OpenApiResponse(OEMReferenceDetail, description='El número ya estaba en la biblioteca: se unieron la forma escrita y la fuente.')},
        description='Agrega un número OEM escrito en cualquier forma (se guarda compacto) con su primera fuente: registro manual, lista de precios, '
                    'catálogo del fabricante, catálogo de marca de repuesto o declaración de proveedor. Repetirlo no duplica nada.')
    def post(self, request):
        require_tables()
        data = OEMReferenceCreate(data=request.data)
        data.is_valid(raise_exception=True)
        data = data.validated_data
        source = data.get('source') or {'kind': 'manual', 'citation': f'Registro manual de {request.user.username}', 'verified': False}
        ref, created = orf.upsert_reference(data['manufacturer'], data['code'], printed=[data['code']], source_kind=source['kind'],
                                            citation=source['citation'], detail={'verified': source['verified']} if source['kind'] == 'manual' else {},
                                            actor=request.user, defaults={key: data[key] for key in orf.FILLABLE if data.get(key)})
        mark_exact_matches(request.user, codes=[ref.code])  # the SKUs with that code are OEM now
        return Response(detail(ref.pk), status=201 if created else 200)


class OEMReferenceItem(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_reference_detail', responses=OEMReferenceDetail,
                   description='Un número OEM con sus formas impresas, todas sus fuentes, los SKU del catálogo que lo llevan y sus reemplazos.')
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
            if data['action'] in ('dispute', 'clear_dispute'):  # it marks or unmarks the SKUs with its code: Parts before the item, like
                code = OEMReference.objects.filter(pk=pk).values_list('code', flat=True).first()  # every writer (Editar SKU, the alterno signals)
                if code:
                    list(catalog_lines(queryset=Part.objects.select_for_update()).annotate(compact=orf.compact_sql('sku'))
                         .filter(compact=code).order_by('pk').values_list('pk', flat=True))
            ref = OEMReference.objects.select_for_update().filter(pk=pk).first()
            if ref is None:
                raise NotFound('Ese número no está en la biblioteca OEM.')
            apply = plan(ref, data, request.user)
            if apply is not None:
                if ref.version != data['expected_version']:
                    return Response({'detail': CONFLICT, 'reference': detail(ref.pk)}, status=409)
                apply()
        return Response(detail(ref.pk))


class PartOEMEquivalents(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_oem_equivalents', responses=PartOEMEquivalentsResponse,
                   description='Solo lectura: los números OEM de un SKU del catálogo (sus alternos OEM, el número que nombra su código y los que un '
                               'catálogo de marca de repuesto le vinculó) y los códigos de marcas de repuesto equivalentes a esos números.')
    def get(self, request, pk):
        require_tables()
        part = Part.objects.filter(pk=pk).first()
        if part is None:
            raise NotFound('Ese SKU no está en el catálogo.')
        return Response(PartOEMEquivalentsResponse(part_equivalents(part)).data)


def list_rows(ids):
    """References as the library grid lists them (sources, linked SKUs, aftermarket codes), for the given ids."""
    return OEMReferenceRow(decorate(list(rows_query().filter(pk__in=list(ids)).order_by('manufacturer', 'code'))), many=True).data


def part_types():
    return sorted(set(OEMReference.objects.exclude(part_type='').values_list('part_type', flat=True).distinct()))


class OEMPartTypes(serializers.Serializer):
    results = serializers.ListField(child=serializers.CharField())


class OEMReferencePartTypes(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_reference_part_types', responses=OEMPartTypes,
                   description='Los tipos de pieza de la biblioteca OEM: los que Editar celdas ofrece (un tipo nuevo se escribe en el detalle del número).')
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
            refs = {ref.pk: ref for ref in OEMReference.objects.select_for_update().filter(pk__in=[row['id'] for row in rows]).order_by('pk')}
            if len(refs) != len({row['id'] for row in rows}):
                raise ValidationError({'detail': 'Uno de los números ya no está en la biblioteca OEM. Vuelve a cargar la tabla.'})
            stale = [refs[row['id']] for row in rows if refs[row['id']].version != row['expected_version']]
            if stale:
                names = ', '.join(f'{ref.manufacturer} {ref.code}' for ref in stale[:5])
                return Response({'detail': f'No se guardó ningún cambio: otra persona cambió {names} mientras editabas. Revisa sus valores actuales.',
                                 'rows': list_rows(ref.pk for ref in stale)}, status=409)
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
    comes back."""
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_cross_reference_create', request=OEMCrossReferenceInput, responses={201: OEMCrossReferenceRow},
                   description='Agrega a mano un alterno de posventa al número; todos los SKU vinculados lo muestran.')
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
            ref = OEMReference.objects.select_for_update().filter(pk=pk).first()
            if ref is None:
                raise NotFound('Ese número no está en la biblioteca OEM.')
            if brand == ref.manufacturer:
                raise ValidationError({'brand': f'Un código de {brand} es un número OEM, no un alterno de posventa: usa «Reemplazado por» en el detalle.'})
            OEMCrossReferenceRetired.objects.filter(reference=ref, brand=brand, number=number).delete()
            cite = f'Registro manual · {request.user.username}'
            row = OEMCrossReference.objects.filter(reference=ref, brand=brand, number=number).first()
            if row is None:
                row = OEMCrossReference.objects.create(reference=ref, brand=brand, code=code, citations=[cite])
            elif cite not in row.citations:
                row.citations = citation_list(row.citations, cite)
                row.save(update_fields=['citations', 'updated_at'])
        return Response(OEMCrossReferenceRow(row).data, status=201)


class OEMReferenceCrossReference(APIView):
    """Retire an aftermarket code from a number: every linked SKU stops showing it, and no catalog import files it again."""
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_oem_cross_reference_retire', responses={204: None},
                   description='Retira el alterno de posventa del número; la importación de catálogos no lo vuelve a agregar (agregarlo a mano lo recupera).')
    def delete(self, request, pk, code_id):
        require_tables()
        with transaction.atomic():
            row = OEMCrossReference.objects.select_for_update().filter(pk=code_id, reference_id=pk).first()
            if row is None:
                raise NotFound('Ese alterno ya no está en el número.')
            OEMCrossReferenceRetired.objects.update_or_create(reference_id=pk, brand=row.brand, number=row.number,
                                                              defaults={'code': row.code, 'citations': row.citations, 'retired_by': request.user})
            row.delete()
        return Response(status=204)
