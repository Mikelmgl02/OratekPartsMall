"""Marcas and Grupos' management API (inventory refactor S7, plan §3 S7; superusers only).

    brands/                     GET search (nombre or alias), tipo, familia (the brand heading it: its members and itself); paginated.
                                POST {nombre, tipo, familia, notas}: a new brand (400 when the spelling already names one)
    brands/<id>/                GET; PATCH {nombre} renames it (mall.brands.rename: its codes follow, the old spelling stays as an alias),
                                {merge_into} folds it into another brand (mall.brands.merge: the answer is that brand), {tipo}, {familia}
                                (an id or null: mall.brands.set_familia), {notas}
    brands/<id>/aliases/        POST {alias}: another spelling (mall.brands.add_alias: the codes carrying it take the nombre)
    brands/<id>/aliases/<id>/   DELETE the alias
    grupos/                     GET every grupo with its subgrupos and their line counts; POST {nombre}
    grupos/<id>/                GET; PATCH {nombre}: renamed through Grupo.save (its subgrupos' and lines' text follow)"""
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.db.models.functions import Lower
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_field
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import brands
from .brand_models import Brand, BrandAlias
from .line_views import ErrorBodies
from .lines import catalog_q
from .management import IsSuperuser
from .models import Grupo, PartType, Reference


# --- brands --------------------------------------------------------------------------------------------------------------------------

class AliasSerializer(serializers.ModelSerializer):
    class Meta:
        model = BrandAlias
        fields = ['id', 'alias']


class BrandRefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    nombre = serializers.CharField()


class BrandSerializer(serializers.ModelSerializer):
    familia = serializers.SerializerMethodField()
    aliases = AliasSerializer(many=True, read_only=True)
    miembros = serializers.SerializerMethodField(help_text='Las marcas de su familia, si la encabeza.')
    reference_count = serializers.SerializerMethodField(help_text='Referencias vivas con esta marca.')

    class Meta:
        model = Brand
        fields = ['id', 'nombre', 'tipo', 'familia', 'aliases', 'miembros', 'notas', 'reference_count', 'created_at', 'updated_at']
        read_only_fields = fields

    @extend_schema_field(BrandRefSerializer(allow_null=True))
    def get_familia(self, brand):
        return {'id': brand.familia_id, 'nombre': brand.familia.nombre} if brand.familia_id else None

    @extend_schema_field(BrandRefSerializer(many=True))
    def get_miembros(self, brand):
        return [{'id': member.pk, 'nombre': member.nombre} for member in brand.miembros.all()]

    def get_reference_count(self, brand) -> int:
        counts = self.context.get('reference_counts')
        return counts.get(brand.nombre, 0) if counts is not None else Reference.objects.filter(brand=brand.nombre).count()


def brand_queryset():
    return Brand.objects.select_related('familia').prefetch_related('aliases', 'miembros').order_by('nombre', 'id')


def serialize_brands(rows):
    rows = list(rows)
    counts = dict(Reference.objects.filter(brand__in=[brand.nombre for brand in rows]).order_by().values('brand')
                  .annotate(n=Count('pk')).values_list('brand', 'n'))
    return BrandSerializer(rows, many=True, context={'reference_counts': counts}).data


def serialize_brand(brand):
    return serialize_brands([brand_queryset().get(pk=brand.pk)])[0]


def brand_or_404(pk):
    brand = Brand.objects.filter(pk=pk).first()
    if brand is None:
        raise NotFound('No existe esa marca.')
    return brand


def familia_of(value):
    if value is None:
        return None
    familia = Brand.objects.filter(pk=value).first()
    if familia is None:
        raise ValidationError({'familia': 'Elige una marca que exista.'})
    return familia


class BrandPagination(PageNumberPagination):
    page_size = 100


class BrandCreateRequest(serializers.Serializer):
    nombre = serializers.CharField(max_length=120)
    tipo = serializers.ChoiceField(choices=Brand.TIPOS, required=False, allow_blank=True)
    familia = serializers.IntegerField(required=False, allow_null=True, help_text='La marca que encabeza su familia (KAIYI → CHERY).')
    notas = serializers.CharField(required=False, allow_blank=True)


class BrandList(ErrorBodies, generics.ListAPIView):
    permission_classes = [IsSuperuser]
    serializer_class = BrandSerializer
    pagination_class = BrandPagination

    @extend_schema(operation_id='v1_management_brands_list', parameters=[
        OpenApiParameter('search', OpenApiTypes.STR, description='Nombre o alias.'),
        OpenApiParameter('tipo', OpenApiTypes.STR, enum=['vehiculo', 'repuesto', 'none']),
        OpenApiParameter('familia', OpenApiTypes.INT, description='La marca que encabeza la familia: ella y sus miembros.')],
        description='Las marcas con sus alias y su familia (S7).')
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(serialize_brands(page))

    def get_queryset(self):
        params = self.request.query_params
        rows = brand_queryset()
        search = params.get('search', '').strip()
        if search:
            rows = rows.filter(Q(nombre__icontains=search) | Q(aliases__alias__icontains=search)).distinct()
        tipo = params.get('tipo')
        if tipo:
            rows = rows.filter(tipo='' if tipo == 'none' else tipo)
        familia = params.get('familia')
        if familia:
            try:
                rows = rows.filter(Q(pk=int(familia)) | Q(familia_id=int(familia)))
            except ValueError:
                raise ValidationError({'familia': 'Usa el id de una marca.'})
        return rows

    @extend_schema(operation_id='v1_management_brands_create', request=BrandCreateRequest, responses={201: BrandSerializer},
                   description='Crea una marca; 400 si ese nombre ya es una marca o un alias.')
    def post(self, request):
        data = BrandCreateRequest(data=request.data)
        data.is_valid(raise_exception=True)
        data = data.validated_data
        if brands.lookup(data['nombre']) is not None:
            raise ValidationError({'nombre': f'{brands.normalize(data["nombre"])} ya es una marca o un alias de una.'})
        try:
            with transaction.atomic():
                brand, _ = brands.get_or_create(data['nombre'], tipo=data.get('tipo') or '')
                if data.get('familia') is not None:
                    brands.set_familia(brand, familia_of(data['familia']))
                if data.get('notas'):
                    Brand.objects.filter(pk=brand.pk).update(notas=data['notas'].strip())
        except brands.BrandConflict as error:
            raise ValidationError({'detail': str(error)})
        return Response(serialize_brand(brand), status=201)


class BrandEditRequest(serializers.Serializer):
    nombre = serializers.CharField(max_length=120, required=False, help_text='Renombra la marca (sus códigos la siguen).')
    merge_into = serializers.IntegerField(required=False, help_text='Une esta marca a otra: la respuesta es esa marca.')
    tipo = serializers.ChoiceField(choices=Brand.TIPOS, required=False, allow_blank=True)
    familia = serializers.IntegerField(required=False, allow_null=True)
    notas = serializers.CharField(required=False, allow_blank=True)


class BrandDetail(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_brands_retrieve', responses=BrandSerializer, description='Una marca.')
    def get(self, request, pk):
        return Response(serialize_brand(brand_or_404(pk)))

    @extend_schema(operation_id='v1_management_brands_partial_update', request=BrandEditRequest, responses=BrandSerializer,
                   description='Renombra (mall.brands.rename), une (merge_into: mall.brands.merge) o cambia el tipo, la familia o las notas.')
    def patch(self, request, pk):
        data = BrandEditRequest(data=request.data)
        data.is_valid(raise_exception=True)
        data = data.validated_data
        brand = brand_or_404(pk)
        if 'merge_into' in data and len(data) > 1:
            raise ValidationError({'merge_into': 'Une las marcas en un cambio aparte.'})
        try:
            with transaction.atomic():
                if 'merge_into' in data:
                    target = Brand.objects.filter(pk=data['merge_into']).first()
                    if target is None:
                        raise ValidationError({'merge_into': 'Elige una marca que exista.'})
                    return Response(serialize_brand(brands.merge(brand, target)['brand']))
                if data.get('nombre') is not None and brands.normalize(data['nombre']) != brand.nombre:
                    brand = brands.rename(brand, data['nombre'])['brand']
                if 'tipo' in data and data['tipo'] != brand.tipo:
                    Brand.objects.filter(pk=brand.pk).update(tipo=data['tipo'])
                if 'familia' in data:
                    brands.set_familia(brand, familia_of(data['familia']))
                if 'notas' in data:
                    Brand.objects.filter(pk=brand.pk).update(notas=data['notas'].strip())
        except brands.BrandConflict as error:
            raise ValidationError({'detail': str(error)})
        return Response(serialize_brand(brand))


class AliasRequest(serializers.Serializer):
    alias = serializers.CharField(max_length=120)


class AliasResponse(serializers.Serializer):
    alias = AliasSerializer()
    moved = serializers.IntegerField(help_text='Códigos que tenían esa grafía y pasaron al nombre de la marca.')
    cases = serializers.ListField(child=serializers.UUIDField(), help_text='Casos de unificación abiertos por códigos repetidos.')
    brand = BrandSerializer()


class BrandAliases(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_brands_aliases_create', request=AliasRequest, responses={201: AliasResponse},
                   description='Agrega otra grafía de la marca (mall.brands.add_alias).')
    def post(self, request, pk):
        data = AliasRequest(data=request.data)
        data.is_valid(raise_exception=True)
        brand = brand_or_404(pk)
        try:
            alias, moved = brands.add_alias(brand, data.validated_data['alias'])
        except brands.BrandConflict as error:
            raise ValidationError({'alias': str(error)})
        return Response({'alias': AliasSerializer(alias).data, 'moved': moved['moved'], 'cases': moved['cases'],
                         'brand': serialize_brand(brand)}, status=201)


class BrandAliasDetail(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_brands_aliases_destroy', request=None, responses={204: None},
                   description='Quita un alias de la marca.')
    def delete(self, request, pk, alias_id):
        alias = BrandAlias.objects.filter(pk=alias_id, brand_id=pk).first()
        if alias is None:
            raise NotFound('No existe ese alias en esta marca.')
        brands.remove_alias(alias)
        return Response(status=204)


# --- grupos --------------------------------------------------------------------------------------------------------------------------

class SubgrupoSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    line_count = serializers.IntegerField(help_text='Líneas del catálogo (publicadas y retiradas) en el subgrupo.')


class GrupoSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    nombre = serializers.CharField()
    subgrupos = SubgrupoSerializer(many=True)
    line_count = serializers.IntegerField()


def serialize_grupos(grupos):
    grupos = list(grupos)
    subgrupos = (PartType.objects.filter(grupo__in=grupos).annotate(line_count=Count('parts', filter=catalog_q(prefix='parts__'), distinct=True))
                 .order_by(Lower('name'), 'id'))
    by_grupo = {}
    for subgrupo in subgrupos:
        by_grupo.setdefault(subgrupo.grupo_id, []).append({'id': subgrupo.pk, 'name': subgrupo.name, 'line_count': subgrupo.line_count})
    return [{'id': grupo.pk, 'nombre': grupo.nombre, 'subgrupos': by_grupo.get(grupo.pk, []),
             'line_count': sum(row['line_count'] for row in by_grupo.get(grupo.pk, []))} for grupo in grupos]


class GrupoRequest(serializers.Serializer):
    nombre = serializers.CharField(max_length=120)


def save_grupo(grupo, nombre):
    from .technical_data import normalized_label
    nombre = normalized_label(nombre)
    if not nombre:
        raise ValidationError({'nombre': 'Escribe el nombre del grupo.'})
    others = Grupo.objects.alias(key=Lower('nombre')).filter(key=nombre.lower())
    if grupo.pk:
        others = others.exclude(pk=grupo.pk)
    if others.exists():
        raise ValidationError({'nombre': f'Ya existe el grupo {nombre}.'})
    grupo.nombre = nombre
    try:
        with transaction.atomic():
            grupo.save()  # a rename reaches its subgrupos' and lines' text (mall.technical_data.project_grupo)
    except IntegrityError:
        raise ValidationError({'nombre': f'Ya existe el grupo {nombre}.'})
    return grupo


class GrupoList(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_grupos_list', responses=GrupoSerializer(many=True),
                   description='Los grupos con sus subgrupos y cuántas líneas del catálogo tiene cada uno (S7).')
    def get(self, request):
        return Response(serialize_grupos(Grupo.objects.order_by(Lower('nombre'), 'id')))

    @extend_schema(operation_id='v1_management_grupos_create', request=GrupoRequest, responses={201: GrupoSerializer},
                   description='Crea un grupo; 400 si ya existe (sin distinguir mayúsculas).')
    def post(self, request):
        data = GrupoRequest(data=request.data)
        data.is_valid(raise_exception=True)
        grupo = save_grupo(Grupo(), data.validated_data['nombre'])
        return Response(serialize_grupos([grupo])[0], status=201)


class GrupoDetail(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    def grupo(self, pk):
        grupo = Grupo.objects.filter(pk=pk).first()
        if grupo is None:
            raise NotFound('No existe ese grupo.')
        return grupo

    @extend_schema(operation_id='v1_management_grupos_retrieve', responses=GrupoSerializer, description='Un grupo con sus subgrupos.')
    def get(self, request, pk):
        return Response(serialize_grupos([self.grupo(pk)])[0])

    @extend_schema(operation_id='v1_management_grupos_partial_update', request=GrupoRequest, responses=GrupoSerializer,
                   description='Renombra el grupo: el texto de sus subgrupos y de sus líneas lo sigue.')
    def patch(self, request, pk):
        data = GrupoRequest(data=request.data)
        data.is_valid(raise_exception=True)
        grupo = save_grupo(self.grupo(pk), data.validated_data['nombre'])
        return Response(serialize_grupos([grupo])[0])
