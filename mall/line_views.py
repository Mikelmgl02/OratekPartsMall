"""The OEM line's management API (inventory refactor S7, plan §3 S7; superusers only, through the Next proxy's management allowlist).

    lines/                      GET the lines: estado (comma list; publicada,retirada by default), tipo, origen, grupo, subgrupo (ids,
                                'none' for a line without one), brand (the main's), search (as the Inventario grid: its columns, live
                                codes and the OEM numbers it reaches), ids (comma list), ordering; paginated
    lines/<id>/                 GET one line; PATCH description, name, aplicaciones_texto, subgrupo with expected_version (409 when
                                the line moved meanwhile: mall.lines.edit_line)
    lines/<id>/transition/      POST {estado, reason, note, expected_version}: mall.lines.transition
    lines/<id>/references/      GET every Reference of the line (retired ones too), main first; POST {code, brand, tipo, estado, source,
                                notes, supplier}: mall.references.add
    lines/<id>/main/            GET the main Reference; POST {reference, expected_version}: mall.references.set_main (the codigo follows)
    references/<id>/            PATCH code, brand, tipo, estado, dispute_note, notes with expected_version (the Reference's);
                                DELETE retires it (mall.references.retire: a library number goes back to a line of its own)
    merges/                     GET the automatic merges (the matching service's CatalogMerge rows), newest first, each with the
                                management route of Separar SKU for its line; since (a date), search; paginated
    inventory/<id>/separate/    POST Separar referencia: the item leaves its line for one of its own in preparación (409 with the reason
                                when it cannot: a code change waiting)
The JSON shapes are the contract of S8's screens (OEMLine, Reference). Every error body of the S7 routes is an object (ErrorBodies):
{detail, ...} (a 409 adds version, line or owners), or a serializer's {field: [messages]} for a field it refused."""
import uuid
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import Case, Count, F, Prefetch, Q, When
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_field
from rest_framework import generics, serializers
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import lines, references
from .list_ordering import StableOrdering
from .management import CatalogSearch, IsSuperuser, ManagedInventorySerializer
from .media_serializers import PartImageSerializer
from .models import Account, CatalogMerge, Part, PartType, Reference, ReferenceSource, SupplierItem


class ErrorBodies:
    """The S7 routes' error bodies are objects (the S8 contract): a bare list, as a DRF ValidationError of one message gives it
    (ReferenceConflict, ReferenceHasVariants, a choke point's ValidationError('…')), becomes {detail}; {detail, ...} and a serializer's
    {field: [messages]} stay as they are."""

    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if isinstance(getattr(response, 'data', None), list):
            response.data = {'detail': ' '.join(str(item) for item in response.data)}
        return response


class Conflict(APIException):
    status_code = 409
    default_detail = 'Los datos cambiaron: vuelve a cargarlos.'


MAX_IDS = 100
LIVE_ESTADOS = [(value, label) for value, label in Reference.ESTADOS if value != 'retirada']  # retiring is DELETE


# --- serializers (the S8 contract) ---------------------------------------------------------------------------------------------------

class NamedSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()


class ReferenceSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReferenceSource
        fields = ['kind', 'citation', 'marks', 'retirada', 'created_at']


class SupplierNameSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class LineReferenceSerializer(serializers.ModelSerializer):
    """One Reference of a line, as S8's References editor reads it."""
    supplier = serializers.SerializerMethodField(help_text='Solo en un código interno: el proveedor que lo usa.')
    line = serializers.UUIDField(source='part_id', read_only=True)
    variant_count = serializers.SerializerMethodField(help_text='Artículos de proveedor que cuelgan de esta referencia.')
    sources = ReferenceSourceSerializer(many=True, read_only=True)

    class Meta:
        model = Reference
        fields = ['id', 'uid', 'line', 'code', 'clave', 'brand', 'tipo', 'estado', 'is_main', 'supplier', 'printed_forms', 'dispute_note',
                  'notes', 'detail', 'reemplazado_por', 'version', 'variant_count', 'sources', 'created_at', 'updated_at']
        read_only_fields = fields

    @extend_schema_field(SupplierNameSerializer(allow_null=True))
    def get_supplier(self, ref):
        return {'id': ref.supplier_id, 'name': ref.supplier.name} if ref.supplier_id else None

    def get_variant_count(self, ref) -> int:
        counts = self.context.get('variant_counts')
        if counts is not None:
            return counts.get(ref.pk, 0)
        return SupplierItem.objects.filter(reference_id=ref.pk).count()


class MainSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    tipo = serializers.CharField()
    estado = serializers.CharField()


class LineCodeSerializer(MainSerializer):
    """A live Reference of the line other than its main, as the Inventario grid's code chips read it (S8)."""


class GrupoRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    nombre = serializers.CharField()


class SubgrupoRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class DecisionSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=True, help_text='duplicate, not_a_part, bad_data u other (rechazada).')
    note = serializers.CharField(allow_blank=True)
    decided_by = serializers.CharField(allow_null=True)
    decided_at = serializers.DateTimeField(allow_null=True)


class LineSerializer(serializers.ModelSerializer):
    """An OEM line (Part) as S8's screens read it: its lifecycle, its main Reference and its subgrupo."""
    brand = serializers.SerializerMethodField(help_text='La marca de la referencia principal ("" si no tiene).')
    main = serializers.SerializerMethodField()
    grupo = serializers.SerializerMethodField()
    subgrupo = serializers.SerializerMethodField()
    decision = serializers.SerializerMethodField()
    reference_count = serializers.SerializerMethodField(help_text='Referencias vivas (no retiradas), la principal incluida.')
    variant_count = serializers.SerializerMethodField(help_text='Artículos de proveedor en la línea (el stock_record_count del Inventario).')
    merged_count = serializers.SerializerMethodField(help_text='Líneas unificadas en esta (Separar SKU las devuelve).')
    codes = serializers.SerializerMethodField(help_text='Las referencias vivas de la línea sin la principal (su código es el SKU), como las '
                                                        'muestra el Inventario: id, code, brand, tipo y estado (S8).')
    images = PartImageSerializer(many=True, read_only=True)

    class Meta:
        model = Part
        fields = ['id', 'sku', 'name', 'description', 'estado', 'origen', 'tipo', 'version', 'brand', 'main', 'grupo', 'subgrupo',
                  'category', 'subcategory', 'aplicaciones_texto', 'merged_into', 'decision', 'reference_count', 'variant_count',
                  'merged_count', 'codes', 'images', 'field_sources', 'technical_revision', 'created_at', 'updated_at', 'changed_at']
        read_only_fields = fields

    def _main(self, line):
        return references.main_in(line.codes.all())  # prefetched: no query per row

    def get_brand(self, line) -> str:
        main = self._main(line)
        return main.brand if main is not None else ''

    @extend_schema_field(MainSerializer(allow_null=True))
    def get_main(self, line):
        main = self._main(line)
        return {'id': main.pk, 'code': main.code, 'brand': main.brand, 'tipo': main.tipo, 'estado': main.estado} if main else None

    @extend_schema_field(GrupoRefSerializer(allow_null=True))
    def get_grupo(self, line):
        part_type = line.part_type if line.part_type_id else None
        return {'id': part_type.grupo_id, 'nombre': part_type.grupo.nombre} if part_type is not None and part_type.grupo_id else None

    @extend_schema_field(SubgrupoRefSerializer(allow_null=True))
    def get_subgrupo(self, line):
        return {'id': line.part_type_id, 'name': line.part_type.name} if line.part_type_id else None

    @extend_schema_field(DecisionSerializer)
    def get_decision(self, line):
        return {'reason': line.decision_reason, 'note': line.decision_note,
                'decided_by': line.decided_by.username if line.decided_by_id else None, 'decided_at': line.decided_at}

    def get_reference_count(self, line) -> int:
        return len(line.codes.all())

    def get_variant_count(self, line) -> int:
        if line.__dict__.get('variant_count') is not None:  # annotated when the list is ordered by it
            return line.__dict__['variant_count']
        counts = self.context.get('variant_counts')
        return counts.get(line.pk, 0) if counts is not None else line.supplier_items.count()

    @extend_schema_field(LineCodeSerializer(many=True))
    def get_codes(self, line):
        return [{'id': ref.pk, 'code': ref.code, 'brand': ref.brand, 'tipo': ref.tipo, 'estado': ref.estado}
                for ref in line.codes.all() if not ref.is_main]  # prefetched (live, main first): no query per row

    def get_merged_count(self, line) -> int:
        counts = self.context.get('merged_counts')
        return counts.get(line.pk, 0) if counts is not None else Part.objects.filter(merged_into=line.pk).count()


def line_queryset():
    return (lines.all_lines().select_related('part_type__grupo', 'decided_by')
            .prefetch_related(Prefetch('codes', queryset=Reference.objects.order_by('-is_main', 'pk')), 'images'))


def serialize_lines(rows, request=None):
    rows = list(rows)
    ids = [row.pk for row in rows]
    counts = dict(SupplierItem.objects.filter(part_id__in=ids).order_by().values('part_id').annotate(n=Count('pk')).values_list('part_id', 'n'))
    merged = dict(Part.objects.filter(merged_into__in=ids).order_by().values('merged_into').annotate(n=Count('pk'))
                  .values_list('merged_into', 'n'))
    return LineSerializer(rows, many=True, context={'request': request, 'variant_counts': counts, 'merged_counts': merged}).data


def serialize_line(line, request=None):
    return serialize_lines([line_queryset().get(pk=line.pk)], request)[0]


def serialize_references(rows, request=None):
    rows = list(rows)
    counts = dict(SupplierItem.objects.filter(reference_id__in=[row.pk for row in rows]).order_by().values('reference_id')
                  .annotate(n=Count('pk')).values_list('reference_id', 'n'))
    return LineReferenceSerializer(rows, many=True, context={'request': request, 'variant_counts': counts}).data


def serialize_reference(ref, request=None):
    ref = Reference.all_objects.select_related('supplier').prefetch_related('sources').get(pk=ref.pk)
    return serialize_references([ref], request)[0]


def as_uuid_list(value, name):
    wanted = [part.strip() for part in (value or '').split(',') if part.strip()][:MAX_IDS]
    try:
        return [uuid.UUID(part) for part in wanted]
    except ValueError:
        raise ValidationError({name: 'Usa identificadores separados por comas.'})


def version_conflict(error):
    """409 with the message and the version the line has now (mall.lines.LineVersionConflict)."""
    return Response({'detail': str(error.detail), 'version': error.version}, status=409)


def version_of(data, name='expected_version'):
    value = data.get(name)
    if value is None or isinstance(value, bool):
        raise ValidationError({name: 'Envía la versión que leíste (expected_version).'})
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError({name: 'La versión no es válida.'})


# --- lines ---------------------------------------------------------------------------------------------------------------------------

class LinePagination(PageNumberPagination):
    page_size = 50


LINE_PARAMETERS = [
    OpenApiParameter('estado', OpenApiTypes.STR, description='Estados separados por comas (referencia, preparacion, publicada, rechazada, '
                     'retirada, unificada); publicada,retirada si se omite.'),
    OpenApiParameter('tipo', OpenApiTypes.STR, enum=['OEM', 'AFTERMARKET', 'UNKNOWN']),
    OpenApiParameter('origen', OpenApiTypes.STR, enum=list(lines.ORIGENES)),
    OpenApiParameter('grupo', OpenApiTypes.STR, description='Id del grupo, o none.'),
    OpenApiParameter('subgrupo', OpenApiTypes.STR, description='Id del subgrupo (PartType), o none.'),
    OpenApiParameter('brand', OpenApiTypes.STR, description='La marca de la referencia principal (cualquier grafía).'),
    OpenApiParameter('search', OpenApiTypes.STR, description='Como el Inventario: sus columnas, sus códigos vivos y los números OEM que alcanza.'),
    OpenApiParameter('ids', OpenApiTypes.STR, description=f'Hasta {MAX_IDS} ids separados por comas.'),
]


class LineList(ErrorBodies, generics.ListAPIView):
    permission_classes = [IsSuperuser]
    serializer_class = LineSerializer
    pagination_class = LinePagination
    filter_backends = [CatalogSearch, StableOrdering]
    search_fields = ['sku', 'description']
    ordering_fields = ['sku', 'description', 'estado', 'origen', 'tipo', 'created_at', 'updated_at', 'changed_at', 'version', 'variant_count']

    @extend_schema(operation_id='v1_management_lines_list', parameters=LINE_PARAMETERS,
                   description='Las líneas OEM con su estado, su referencia principal y su subgrupo (S7).')
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(serialize_lines(page, request))

    def get_queryset(self):
        params = self.request.query_params
        estados = [value.strip() for value in params.get('estado', '').split(',') if value.strip()] or list(lines.CATALOG)
        try:
            queryset = lines.catalog_lines(estados, queryset=line_queryset())
        except ValueError as error:
            raise ValidationError({'estado': str(error)})
        tipo = params.get('tipo')
        if tipo:
            if tipo not in dict(Part.TIPOS):
                raise ValidationError({'tipo': 'Usa OEM, AFTERMARKET o UNKNOWN.'})
            queryset = queryset.filter(tipo=tipo)
        origen = params.get('origen')
        if origen:
            if origen not in lines.ORIGENES:
                raise ValidationError({'origen': f'Usa {", ".join(lines.ORIGENES)}.'})
            queryset = queryset.filter(origen=origen)
        for key, field in (('grupo', 'part_type__grupo_id'), ('subgrupo', 'part_type_id')):
            value = params.get(key)
            if value == 'none':
                queryset = queryset.filter(part_type__isnull=True)
            elif value:
                try:
                    queryset = queryset.filter(**{field: uuid.UUID(value)})
                except ValueError:
                    raise ValidationError({key: 'Usa el id o none.'})
        brand = params.get('brand', '').strip()
        if brand:
            from .brands import resolve
            queryset = queryset.filter(pk__in=Reference.objects.filter(is_main=True, brand=resolve(brand)).values('part_id'))
        if params.get('ids'):
            queryset = queryset.filter(pk__in=as_uuid_list(params['ids'], 'ids'))
        if 'variant_count' in params.get('ordering', ''):  # the Inventario's stock column, counted in the query only when it orders
            queryset = queryset.annotate(variant_count=Count('supplier_items', distinct=True))
        return queryset.order_by('sku', 'id')


class LineEditRequest(serializers.Serializer):
    expected_version = serializers.IntegerField()
    description = serializers.CharField(required=False, allow_blank=True)
    name = serializers.CharField(required=False, allow_blank=True, max_length=200,
                                 help_text='Desde S10 una línea no tiene nombre: es su descripción cuando no se envía description.')
    aplicaciones_texto = serializers.CharField(required=False, allow_blank=True)
    subgrupo = serializers.UUIDField(required=False, allow_null=True, help_text='Id del subgrupo (PartType), o null.')


class LineConflictResponse(serializers.Serializer):
    detail = serializers.CharField()
    version = serializers.IntegerField(required=False)


class LineDetail(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_lines_retrieve', responses=LineSerializer, description='Una línea OEM, en cualquier estado.')
    def get(self, request, pk):
        line = line_queryset().filter(pk=pk).first()
        if line is None:
            raise NotFound('No existe esa línea.')
        return Response(serialize_lines([line], request)[0])

    @extend_schema(operation_id='v1_management_lines_partial_update', request=LineEditRequest,
                   responses={200: LineSerializer, 409: LineConflictResponse},
                   description='Edita la línea con la versión que leíste: 409 con la versión actual si cambió.')
    def patch(self, request, pk):
        data = LineEditRequest(data=request.data)
        data.is_valid(raise_exception=True)
        data = dict(data.validated_data)
        unknown = set(request.data) - set(LineEditRequest().fields)
        if unknown:
            raise ValidationError({'detail': f'Solo se pueden cambiar: description, name, aplicaciones_texto y subgrupo.'})
        version = data.pop('expected_version')
        name = data.pop('name', None)
        if name is not None and 'description' not in data:  # S10: the name a line is given is its description
            data['description'] = name
        if 'subgrupo' in data:
            subgrupo = data.pop('subgrupo')
            data['part_type'] = PartType.objects.select_related('grupo').filter(pk=subgrupo).first() if subgrupo else None
            if subgrupo and data['part_type'] is None:
                raise ValidationError({'subgrupo': 'Elige un subgrupo que exista.'})
        try:
            line = lines.edit_line(pk, user=request.user, expected_version=version, **data)
        except lines.LineStateError as error:
            raise ValidationError({'detail': str(error)})
        except lines.LineVersionConflict as error:
            return version_conflict(error)
        return Response(serialize_line(line, request))


class TransitionRequest(serializers.Serializer):
    estado = serializers.ChoiceField(choices=Part.ESTADOS)
    expected_version = serializers.IntegerField()
    reason = serializers.ChoiceField(choices=Part.REASONS, required=False, allow_blank=True, help_text='Obligatorio para rechazar.')
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class LineTransition(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_lines_transition', request=TransitionRequest,
                   responses={200: LineSerializer, 400: OpenApiResponse(description='Transición no permitida, o el código ya es otro SKU '
                                                                                  '(owners: los SKU que lo tienen).'),
                              409: LineConflictResponse},
                   description='Mueve la línea a otro estado: referencia → publicada/retirada, preparación → publicada/rechazada '
                               '(Productos nuevos), publicada ↔ retirada, rechazada → preparación.')
    def post(self, request, pk):
        from .sku_proposals import DuplicateProposal
        data = TransitionRequest(data=request.data)
        data.is_valid(raise_exception=True)
        data = data.validated_data
        try:
            line = lines.transition(pk, data['estado'], user=request.user, expected_version=data['expected_version'],
                                    reason=data.get('reason', ''), note=data.get('note', ''))
        except lines.LineStateError as error:
            raise ValidationError({'detail': str(error)})
        except lines.LineVersionConflict as error:
            return version_conflict(error)
        except DuplicateProposal as refused:
            return Response({'detail': refused.detail, 'owners': refused.owners}, status=400)
        except APIException as error:
            if error.status_code == 409:  # Productos nuevos' version check
                current = Part.objects.filter(pk=pk).values_list('version', flat=True).first()
                return Response({'detail': error.detail if isinstance(error.detail, str) else str(error.detail), 'version': current},
                                status=409)
            raise
        return Response(serialize_line(line, request))


# --- a line's References -------------------------------------------------------------------------------------------------------------

def detail_line_or_404(pk, *, write=False):
    line = lines.all_lines().filter(pk=pk).first()
    if line is None:
        raise NotFound('No existe esa línea.')
    if write:
        lines.refuse_unificada(line)
    return line


class ReferenceAddRequest(serializers.Serializer):
    code = serializers.CharField(max_length=120)
    brand = serializers.CharField(max_length=120, required=False, allow_blank=True, default='', help_text='"" vale para cualquier marca.')
    tipo = serializers.ChoiceField(choices=Reference.TIPOS, required=False)
    estado = serializers.ChoiceField(choices=LIVE_ESTADOS, required=False)
    dispute_note = serializers.CharField(required=False, allow_blank=True)
    source = serializers.CharField(max_length=500, required=False, allow_blank=True, help_text='La fuente (catálogo y página, etc.).')
    notes = serializers.CharField(required=False, allow_blank=True)
    supplier = serializers.UUIDField(required=False, allow_null=True, help_text='Solo para un código interno: su proveedor.')


class ReferenceAddResponse(serializers.Serializer):
    added = serializers.BooleanField(help_text='false si la línea ya tenía esa referencia (se devuelve sin cambios).')
    reference = LineReferenceSerializer()


class LineReferences(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_lines_references_list', responses=LineReferenceSerializer(many=True),
                   description='Todas las referencias de la línea, retiradas incluidas: la principal primero.')
    def get(self, request, pk):
        line = detail_line_or_404(pk)
        rows = (Reference.all_objects.filter(part_id=line.pk).select_related('supplier').prefetch_related('sources')
                .order_by('-is_main', Case(When(estado='retirada', then=1), default=0), 'tipo', 'code', 'pk'))
        return Response(serialize_references(rows, request))

    @extend_schema(operation_id='v1_management_lines_references_create', request=ReferenceAddRequest,
                   responses={201: ReferenceAddResponse, 200: ReferenceAddResponse,
                              400: OpenApiResponse(description='El código ya es de otra línea, o la referencia no es válida.')},
                   description='Agrega una referencia a la línea (mall.references.add): la que ya tenía esa clave y marca se devuelve.')
    def post(self, request, pk):
        data = ReferenceAddRequest(data=request.data)
        data.is_valid(raise_exception=True)
        data = data.validated_data
        supplier = None
        if data.get('supplier'):
            supplier = Account.objects.filter(pk=data['supplier'], roles__capability='supplier').distinct().first()
            if supplier is None:
                raise ValidationError({'supplier': 'Elige un proveedor que exista.'})
        tipo = data.get('tipo') or ('interno' if supplier is not None else None)
        with transaction.atomic():
            line = Part.objects.select_for_update().filter(pk=detail_line_or_404(pk, write=True).pk).first()
            try:
                ref, added = references.add(line, data['code'], data.get('brand', ''), tipo=tipo, estado=data.get('estado'),
                                            supplier=supplier, source=data.get('source', ''), actor=request.user, notes=data.get('notes', ''),
                                            dispute_note=data.get('dispute_note', ''))
            except IntegrityError:
                raise ValidationError({'code': 'El código ya está registrado.'})
            if added:
                lines.touch_visible(line)
        return Response({'added': added, 'reference': serialize_reference(ref, request)}, status=201 if added else 200)


class MainRequest(serializers.Serializer):
    reference = serializers.IntegerField(help_text='El id de una referencia viva de la línea.')
    expected_version = serializers.IntegerField(required=False, help_text='La versión de la línea que leíste (409 si cambió).')


class MainConflictResponse(serializers.Serializer):
    detail = serializers.CharField()
    line = serializers.DictField(required=False, help_text='La línea que ya tiene ese código como SKU: id y sku.')


class LineMain(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_lines_main_retrieve', responses=LineReferenceSerializer(allow_null=True),
                   description='La referencia principal de la línea (su código es el SKU); null si no tiene.')
    def get(self, request, pk):
        line = detail_line_or_404(pk)
        main = references.main_of(line)
        return Response(serialize_reference(main, request) if main is not None else None)

    @extend_schema(operation_id='v1_management_lines_main_create', request=MainRequest,
                   responses={200: LineSerializer, 409: MainConflictResponse},
                   description='Hace principal una referencia de la línea (mall.references.set_main): el SKU pasa a ser su código. 409 si '
                               'otra línea del catálogo ya tiene ese código como SKU.')
    def post(self, request, pk):
        data = MainRequest(data=request.data)
        data.is_valid(raise_exception=True)
        data = data.validated_data
        try:
            with transaction.atomic():
                line = Part.objects.select_for_update().filter(pk=detail_line_or_404(pk, write=True).pk).first()
                if data.get('expected_version') is not None and data['expected_version'] != line.version:
                    raise lines.LineVersionConflict(line)
                ref = Reference.objects.filter(pk=data['reference'], part_id=line.pk).first()
                if ref is None:
                    raise ValidationError({'reference': 'Elige una referencia viva de esta línea.'})
                if not ref.is_main:
                    references.set_main(line, ref, actor=request.user, reason='Referencia principal (administrador)')
                    Part.objects.filter(pk=line.pk).update(version=F('version') + 1, changed_at=timezone.now())
        except lines.LineVersionConflict as error:
            return version_conflict(error)
        except references.MainConflict as error:
            holder = error.line
            return Response({'detail': error.message, 'line': {'id': str(holder.pk), 'sku': holder.sku} if holder is not None else None},
                            status=409)
        return Response(serialize_line(line, request))


class ReferenceEditRequest(serializers.Serializer):
    expected_version = serializers.IntegerField(help_text='La versión de la referencia que leíste (409 si cambió).')
    code = serializers.CharField(max_length=120, required=False)
    brand = serializers.CharField(max_length=120, required=False, allow_blank=True)
    tipo = serializers.ChoiceField(choices=Reference.TIPOS, required=False)
    estado = serializers.ChoiceField(choices=LIVE_ESTADOS, required=False, help_text='Para retirarla usa DELETE.')
    dispute_note = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class ReferenceDetail(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_references_partial_update', request=ReferenceEditRequest,
                   responses={200: LineReferenceSerializer, 409: LineConflictResponse},
                   description='Cambia una referencia (mall.references.update) con la versión que leíste.')
    def patch(self, request, pk):
        data = ReferenceEditRequest(data=request.data)
        data.is_valid(raise_exception=True)
        fields = dict(data.validated_data)
        unknown = set(request.data) - set(ReferenceEditRequest().fields)
        if unknown:
            raise ValidationError({'detail': 'Solo se pueden cambiar: code, brand, tipo, estado, dispute_note y notes.'})
        version = fields.pop('expected_version')
        with transaction.atomic():
            ref = Reference.all_objects.select_related('part').filter(pk=pk).first()
            if ref is None:
                raise NotFound('No existe esa referencia.')
            Part.objects.select_for_update().filter(pk=ref.part_id).first()  # the line first (plan §1.6.5), then the Reference
            ref = Reference.all_objects.select_for_update().select_related('part').get(pk=pk)
            if ref.version != version:
                return Response({'detail': 'La referencia cambió mientras la editabas: vuelve a cargarla.', 'version': ref.version}, status=409)
            if ref.estado == 'retirada':
                raise ValidationError({'detail': 'Esta referencia está retirada: agrégala de nuevo a su línea para recuperarla.'})
            lines.refuse_unificada(ref.part)
            ref = references.update(ref, actor=request.user, **fields)
            lines.touch_visible(ref.part_id)
        return Response(serialize_reference(ref, request))

    @extend_schema(operation_id='v1_management_references_destroy', request=None,
                   responses={200: LineReferenceSerializer, 400: OpenApiResponse(description='Es la referencia principal.'),
                              409: OpenApiResponse(description='Tiene artículos de proveedor: muévelos o sepáralos primero.')},
                   description='Retira la referencia de su línea (nunca se borra): un número de la Biblioteca vuelve a una línea propia en '
                               'referencia.')
    def delete(self, request, pk):
        with transaction.atomic():
            ref = Reference.all_objects.filter(pk=pk).first()
            if ref is None:
                raise NotFound('No existe esa referencia.')
            line_id = ref.part_id
            Part.objects.select_for_update().filter(pk=line_id).first()
            references.retire(ref, actor=request.user, reason='Retirada por un administrador')
            lines.touch_visible(line_id)
        return Response(serialize_reference(ref, request))


# --- the automatic merges ------------------------------------------------------------------------------------------------------------

class MergeLineSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    sku = serializers.CharField()
    still_merged = serializers.BooleanField(help_text='Sigue unificada en la línea (false si Separar SKU ya la devolvió).')


class MergeTargetSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    sku = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    estado = serializers.CharField()


class MergeDigestSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    created_at = serializers.DateTimeField()
    reason = serializers.CharField(allow_blank=True)
    target = MergeTargetSerializer()
    sources = MergeLineSerializer(many=True)
    split = serializers.CharField(help_text='La ruta de Separar SKU de la línea en el proxy de administración (management/...).')


class MergePagination(PageNumberPagination):
    page_size = 25


class MergeDigest(ErrorBodies, generics.ListAPIView):
    """The automatic merges (plan §3 S7 merges/): every CatalogMerge the matching service made (the worker's family merges before S6,
    S2b's compact duplicates), newest first, each with its line and the lines it joined, whether each is still merged there, and the
    route of Separar SKU (management/catalog/<line>/split) that takes one back out."""
    permission_classes = [IsSuperuser]
    serializer_class = MergeDigestSerializer
    pagination_class = MergePagination

    @extend_schema(operation_id='v1_management_merges_list', parameters=[
        OpenApiParameter('since', OpenApiTypes.DATE, description='Solo las unificaciones desde esta fecha (AAAA-MM-DD).'),
        OpenApiParameter('search', OpenApiTypes.STR, description='El SKU de la línea o de una línea unificada.')],
        description='El resumen de las unificaciones automáticas, enlazado a Separar SKU.')
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        from .line_merge import SERVICE_ACCOUNT
        rows = CatalogMerge.objects.filter(actor__username=SERVICE_ACCOUNT).select_related('target').order_by('-created_at', '-id')
        since = self.request.query_params.get('since', '').strip()
        if since:
            try:
                day = datetime.strptime(since, '%Y-%m-%d').date()
            except ValueError:
                raise ValidationError({'since': 'Usa una fecha AAAA-MM-DD.'})
            rows = rows.filter(created_at__date__gte=day)
        search = self.request.query_params.get('search', '').strip().upper()
        if search:
            rows = rows.filter(Q(target__sku__icontains=search) | Q(sources__icontains=search))
        return rows

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        source_ids = {str(row.get('id')) for merge in page for row in (merge.sources or []) if isinstance(row, dict) and row.get('id')}
        merged = dict(Part.objects.filter(pk__in=source_ids).values_list('pk', 'merged_into_id'))
        merged = {str(pk): str(target) if target else None for pk, target in merged.items()}
        data = []
        for merge in page:
            target = merge.target
            sources = [{'id': row['id'], 'sku': row.get('sku', ''), 'still_merged': merged.get(str(row['id'])) == str(target.pk)}
                       for row in (merge.sources or []) if isinstance(row, dict) and row.get('id')]
            data.append({'id': merge.pk, 'created_at': merge.created_at, 'reason': (merge.summary or {}).get('reason', '') or '',
                         'target': {'id': target.pk, 'sku': target.sku, 'description': target.description, 'estado': target.estado},
                         'sources': sources, 'split': f'catalog/{target.pk}/split'})
        return self.get_paginated_response(MergeDigestSerializer(data, many=True).data)


# --- Separar referencia --------------------------------------------------------------------------------------------------------------

class SeparateResponse(serializers.Serializer):
    item = ManagedInventorySerializer()
    line = LineSerializer()


class InventorySeparate(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_inventory_separate', request=None,
                   responses={200: SeparateResponse, 409: OpenApiResponse(description='No se puede separar ahora (el motivo en detail).')},
                   description='Separar referencia: el artículo deja su línea por una propia en preparación, cuyo código principal es el '
                               'código interno de su proveedor (mall.reference_matching.separate).')
    def post(self, request, pk):
        from .line_merge import importing
        from .matching_queue import enqueue_matching, matching_work
        from .new_products import PlaceRefused
        from . import reference_matching
        item = SupplierItem.objects.filter(pk=pk).first()
        if item is None:
            raise NotFound('No existe ese artículo.')
        try:
            with matching_work(), transaction.atomic():
                suppliers = sorted({item.supplier_id, *SupplierItem.objects.filter(part_id=item.part_id).values_list('supplier_id', flat=True)}
                                   if item.part_id else {item.supplier_id}, key=str)
                list(Account.objects.select_for_update().filter(pk__in=suppliers).order_by('pk').values_list('pk', flat=True))
                if importing(suppliers):
                    raise Conflict('Espera a que termine la importación de este proveedor.')
                if item.part_id:
                    Part.objects.select_for_update().filter(pk=item.part_id).first()
                item = SupplierItem.objects.select_for_update(of=('self',)).select_related('supplier', 'part', 'reference').get(pk=item.pk)
                line = reference_matching.separate(item, actor=request.user)
                reference_matching.close_item_cases(item, actor=request.user, note='Separar referencia desde Existencias.')
        except (ValidationError, PlaceRefused) as error:
            detail = error.detail if isinstance(error, ValidationError) else str(error)
            if isinstance(detail, (list, dict)):
                detail = ' '.join(str(value) for value in (detail.values() if isinstance(detail, dict) else detail))
            return Response({'detail': str(detail)}, status=409)
        enqueue_matching()
        item = SupplierItem.objects.select_related('supplier', 'part').get(pk=item.pk)
        return Response({'item': ManagedInventorySerializer(item).data, 'line': serialize_line(line, request)})
