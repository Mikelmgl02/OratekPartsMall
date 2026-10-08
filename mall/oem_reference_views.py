"""Superuser API of the OEM reference library: list with search, filters and counts; create or merge a number written in any form
with its first source; one reference with its sources, linked SKUs and supersession; edits and actions under the version the
reviewer saw (409 with the current reference otherwise). Repeating an action that is already in place returns the reference unchanged.
"""
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
from .catalog_suffix_models import values
from .management import IsSuperuser, UppercaseCharField
from .oem_reference_models import (MANUAL_KINDS, REFERENCE_STATUSES, SOURCE_KINDS, OEMReference, OEMReferenceSource, compact,
                                   manufacturer_name)

REFERENCE_LOCK = 724631112  # pg advisory lock key of supersession edits (matching 724631109, quote assistant 724631110, suffixes 724631111)
ACTIONS = ['edit', 'add_source', 'remove_source', 'dispute', 'clear_dispute', 'set_superseded_by', 'clear_superseded_by']
EDITABLE = ('description', 'part_type', 'applications', 'notes')
MANUAL_CHOICES = [(kind, label) for kind, label in SOURCE_KINDS if kind in MANUAL_KINDS]
VIA = ['sku', 'oem', 'company', 'unknown']
CONFLICT = 'La referencia cambió desde que la revisaste. Revisa sus valores actuales antes de continuar.'
MAX_CHAIN = 50


class OEMReferenceUnavailable(APIException):
    status_code = 503
    default_detail = 'La biblioteca OEM aún no está disponible: faltan migraciones por aplicar.'


def require_tables():
    if not orf.tables_ready():
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
    via = serializers.ChoiceField(choices=VIA, help_text='sku: el SKU principal es el número; oem, company o unknown: un alterno de ese tipo.')
    code = serializers.CharField(help_text='Como lo escribe el catálogo.')


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

    class Meta:
        model = OEMReference
        fields = ['id', 'manufacturer', 'code', 'printed_forms', 'system', 'family', 'part_type', 'description', 'applications', 'status',
                  'status_label', 'dispute_note', 'superseded_by', 'notes', 'source_count', 'source_kinds', 'linked_skus', 'linked_count',
                  'created_by', 'updated_by', 'created_at', 'updated_at', 'version']
        read_only_fields = fields


class OEMReferenceDetail(OEMReferenceRow):
    sources = OEMReferenceSourceRow(many=True, read_only=True)
    supersedes = OEMReferenceLink(many=True, read_only=True, help_text='Números que este reemplaza.')

    class Meta(OEMReferenceRow.Meta):
        fields = OEMReferenceRow.Meta.fields + ['sources', 'supersedes']
        read_only_fields = fields


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


def decorate(refs):
    """Source summary and linked SKUs for a page of references: two catalog queries for the whole page, none per row."""
    linked = orf.linked_parts(refs)
    for ref in refs:
        sources = list(ref.sources.all())
        ref.source_count, ref.source_kinds = len(sources), dict(Counter(source.kind for source in sources))
        ref.linked_skus, ref.linked_count = linked[ref.pk][:orf.LINKED_LIMIT], len(linked[ref.pk])
    return refs


def detail(pk):
    ref = rows_query().prefetch_related('supersedes').filter(pk=pk).first()
    if ref is None:
        raise NotFound('Ese número no está en la biblioteca OEM.')
    return OEMReferenceDetail(decorate([ref])[0]).data


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
        return None if ref.status == 'disputed' and ref.dispute_note == note else lambda: save(ref, user, status='disputed', dispute_note=note)
    if action == 'clear_dispute':
        if ref.status != 'disputed':
            return None

        def clear():
            ref.status, ref.dispute_note = 'inferred', ''  # refresh_status re-derives it from the sources
            orf.refresh_status(ref, user, changed=True)
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
            'part_type': ('part_type',), 'updated_at': ('updated_at',)}


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
        OpenApiParameter('q', OpenApiTypes.STR, description='Inicio del número compacto, una forma impresa, la descripción o el tipo de pieza.'),
        OpenApiParameter('manufacturer', OpenApiTypes.STR, description='Fabricante exacto (TOYOTA, HYUNDAI, KIA...).'),
        OpenApiParameter('status', OpenApiTypes.STR, enum=values(REFERENCE_STATUSES)),
        OpenApiParameter('source_kind', OpenApiTypes.STR, enum=values(SOURCE_KINDS), description='Con al menos una fuente de ese tipo.'),
        OpenApiParameter('linked', OpenApiTypes.STR, enum=['true', 'false'], description='true: algún SKU del catálogo lleva el número; false: ninguno.'),
        OpenApiParameter('ordering', OpenApiTypes.STR, description='Orden del grid, separado por comas, con - para descendente: code, manufacturer, status, family, part_type, updated_at.'),
        OpenApiParameter('page', OpenApiTypes.INT)],
        description='Biblioteca de números OEM (forma compacta) con sus fuentes y los SKU del catálogo que los llevan, más conteos por estado y fabricante.')
    def get(self, request):
        require_tables()
        params, rows = request.query_params, rows_query()
        text = ' '.join(params.get('q', '').upper().split())
        if text:
            match = Q(printed_forms__icontains=text) | Q(description__icontains=text) | Q(part_type__icontains=text)
            rows = rows.filter(match | Q(code__startswith=compact(text)) if compact(text) else match)
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
            ref = OEMReference.objects.select_for_update().filter(pk=pk).first()
            if ref is None:
                raise NotFound('Ese número no está en la biblioteca OEM.')
            apply = plan(ref, data, request.user)
            if apply is not None:
                if ref.version != data['expected_version']:
                    return Response({'detail': CONFLICT, 'reference': detail(ref.pk)}, status=409)
                apply()
        return Response(detail(ref.pk))
