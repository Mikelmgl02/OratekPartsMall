from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import filters, generics, serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .management import IsSuperuser, UppercaseCharField
from .models import Application, ItemApplication, Membership, Part, PartSpecification, PartType, TechnicalField, TechnicalTemplate
from .technical_data import normalized_label

# Multipliers to a common unit within each dimension. Never convert across dimensions.
UNITS = {'mm': ('length', '1'), 'cm': ('length', '10'), 'm': ('length', '1000'), 'in': ('length', '25.4'),
         'g': ('mass', '1'), 'kg': ('mass', '1000'), 'lb': ('mass', '453.59237'),
         'bar': ('pressure', '100'), 'kPa': ('pressure', '1'), 'psi': ('pressure', '6.894757'),
         'V': ('voltage', '1'), 'W': ('power', '1'), 'Nm': ('torque', '1'),
         'ml': ('volume', '1'), 'l': ('volume', '1000'), '°C': ('temperature', '1')}


class PartTypeSerializer(serializers.ModelSerializer):
    category = UppercaseCharField(max_length=120)
    name = UppercaseCharField(max_length=120)
    part_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = PartType
        fields = ['id', 'category', 'name', 'part_count']
        validators = []

    def validate(self, data):
        for key in ('category', 'name'):
            if key in data:
                data[key] = normalized_label(data[key])
        category = data.get('category', getattr(self.instance, 'category', ''))
        name = data.get('name', getattr(self.instance, 'name', ''))
        duplicates = PartType.objects.filter(category__iexact=category, name__iexact=name)
        if self.instance:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise ValidationError('Este subgrupo ya existe en el grupo seleccionado.')
        return data

    @transaction.atomic
    def create(self, data):
        try:
            with transaction.atomic():
                result = super().create(data)
                TechnicalTemplate.objects.create(part_type=result)
                return result
        except IntegrityError:
            raise ValidationError('Este subgrupo ya existe.')

    @transaction.atomic
    def update(self, instance, data):
        instance = PartType.objects.select_for_update().get(pk=instance.pk)
        try:
            with transaction.atomic():
                instance = super().update(instance, data)
                # Legacy strings remain a compatibility projection for import/filter APIs.
                Part.objects.filter(part_type=instance).update(category=instance.category, subcategory=instance.name)
        except IntegrityError:
            raise ValidationError('Este subgrupo ya existe.')
        return instance


class PartTypeList(generics.ListCreateAPIView):
    permission_classes = [IsSuperuser]
    serializer_class = PartTypeSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['category', 'name']
    # Meta.ordering is ignored on GROUP BY queries; pages need an explicit, unique order.
    queryset = PartType.objects.annotate(part_count=Count('parts', filter=Q(parts__merged_into__isnull=True))).order_by('category', 'name', 'id')


class PartTypeDetail(generics.RetrieveUpdateAPIView):
    permission_classes = [IsSuperuser]
    serializer_class = PartTypeSerializer
    queryset = PartTypeList.queryset
    http_method_names = ['get', 'patch', 'head', 'options']


class TechnicalFieldSerializer(serializers.ModelSerializer):
    key = serializers.RegexField(r'^[a-z][a-z0-9_]{0,59}$')
    label = UppercaseCharField(max_length=120)
    section = UppercaseCharField(max_length=120, default='CARACTERÍSTICAS')
    unit = serializers.ChoiceField(choices=['', *UNITS], default='')
    options = serializers.ListField(child=UppercaseCharField(max_length=120), max_length=100, default=list)

    class Meta:
        model = TechnicalField
        fields = ['key', 'label', 'section', 'kind', 'unit', 'options', 'required', 'position']
        validators = []

    def validate(self, data):
        if data['unit'] and data['kind'] not in ['number', 'integer']:
            raise ValidationError('Solo las medidas y los números pueden tener unidad.')
        options = data['options']
        if data['kind'] == 'choice' and (not options or len(set(options)) != len(options)):
            raise ValidationError('Define opciones distintas para este campo de selección.')
        if data['kind'] != 'choice' and options:
            raise ValidationError('Las opciones solo corresponden a campos de selección.')
        return data


class TemplateWriteSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=1)
    fields = TechnicalFieldSerializer(many=True, allow_empty=True)

    def validate_fields(self, fields):
        if len(fields) > 100 or len({f['key'] for f in fields}) != len(fields):
            raise ValidationError('Usa hasta 100 campos con identificadores distintos.')
        return fields


def template_data(template):
    return {'part_type': str(template.part_type_id), 'revision': template.revision,
            'fields': TechnicalFieldSerializer(template.fields.all(), many=True).data}


class TemplateDetail(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(responses=OpenApiTypes.OBJECT, description='Plantilla del subgrupo por su ID permanente, con campos, unidades y revisión.')
    def get(self, request, pk):
        return Response(template_data(get_object_or_404(TechnicalTemplate.objects.prefetch_related('fields'), pk=pk)))

    @extend_schema(request=TemplateWriteSerializer, responses=OpenApiTypes.OBJECT)
    @transaction.atomic
    def patch(self, request, pk):
        template = get_object_or_404(TechnicalTemplate.objects.select_for_update(), pk=pk)
        serializer = TemplateWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data['revision'] != template.revision:
            raise ValidationError('La plantilla cambió. Vuelve a abrirla antes de guardar.')
        current = {f.key: f for f in template.fields.all()}
        used = set(PartSpecification.objects.filter(field__template=template).values_list('field_id', flat=True))
        incoming = {f['key']: f for f in data['fields']}
        for key, field in current.items():
            if field.pk not in used:
                continue
            replacement = incoming.get(key)
            if not replacement or any(getattr(field, k) != replacement[k] for k in ['kind', 'unit']):
                raise ValidationError(f'{field.label} tiene valores guardados. No se puede eliminar ni cambiar su tipo o unidad.')
            if field.kind == 'choice':
                values = set(field.values.values_list('text_value', flat=True))
                if values - set(replacement['options']):
                    raise ValidationError(f'{field.label} tiene opciones en uso que debes conservar.')
        template.fields.exclude(key__in=incoming).delete()
        for index, field in enumerate(data['fields']):
            TechnicalField.objects.update_or_create(template=template, key=field['key'], defaults={**field, 'position': index})
        template.revision += 1
        template.save(update_fields=['revision', 'updated_at'])
        return Response(template_data(template))


class ApplicationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Application
        fields = ['id', 'make', 'model', 'generation', 'year_from', 'year_to', 'engine', 'trim', 'transmission', 'market']
        validators = []

    def validate(self, data):
        for key in self.Meta.fields:
            if key in data and key not in ['id', 'year_from', 'year_to']:
                data[key] = normalized_label(data[key])
        start = data.get('year_from', getattr(self.instance, 'year_from', 0))
        end = data.get('year_to', getattr(self.instance, 'year_to', 0))
        if not 1886 <= start <= end <= 2200:
            raise ValidationError('Indica un rango de años válido (desde ≤ hasta).')
        identity = {key: data.get(key, getattr(self.instance, key, '')) for key in self.Meta.fields if key != 'id'}
        duplicates = Application.objects.filter(**identity)
        if self.instance:
            duplicates = duplicates.exclude(pk=self.instance.pk)
            if self.instance.item_applications.exists() and any(getattr(self.instance, k) != v for k, v in data.items()):
                raise ValidationError('Esta aplicación ya está vinculada a SKU. Crea otra configuración para conservar sus compatibilidades.')
        if duplicates.exists():
            raise ValidationError('Esta aplicación vehicular ya existe.')
        return data

    def create(self, data):
        try:
            with transaction.atomic():
                return super().create(data)
        except IntegrityError:
            raise ValidationError('Esta aplicación vehicular ya existe.')

    def update(self, instance, data):
        try:
            with transaction.atomic():
                return super().update(instance, data)
        except IntegrityError:
            raise ValidationError('Esta aplicación vehicular ya existe.')


class ApplicationList(generics.ListCreateAPIView):
    permission_classes = [IsSuperuser]
    serializer_class = ApplicationSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['make', 'model', 'generation', 'engine', 'trim', 'transmission', 'market']
    queryset = Application.objects.all()


class ApplicationDetail(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsSuperuser]
    serializer_class = ApplicationSerializer
    queryset = Application.objects.all()
    http_method_names = ['get', 'patch', 'delete', 'head', 'options']

    @transaction.atomic
    def patch(self, request, *args, **kwargs):
        self.queryset = self.queryset.select_for_update()
        return super().patch(request, *args, **kwargs)

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError:
            raise ValidationError('Esta aplicación está vinculada a SKU. Retira esos vínculos antes de eliminarla.')


class ItemApplicationWriteSerializer(serializers.Serializer):
    application = serializers.PrimaryKeyRelatedField(queryset=Application.objects.all())
    position = UppercaseCharField(max_length=120, allow_blank=True, default='')
    notes = UppercaseCharField(max_length=2000, allow_blank=True, default='')
    source = serializers.CharField(max_length=500, allow_blank=True, default='')
    verified = serializers.BooleanField(default=False)


class SpecificationInputSerializer(serializers.Serializer):
    value = serializers.JSONField(allow_null=True)
    unit = serializers.ChoiceField(choices=['', *UNITS], required=False)
    source = serializers.CharField(max_length=500, allow_blank=True, default='')


class SheetWriteSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=0)
    part_type = serializers.UUIDField(allow_null=True)
    template_revision = serializers.IntegerField(min_value=1, allow_null=True)
    values = serializers.DictField(child=SpecificationInputSerializer(), required=False)
    applications = ItemApplicationWriteSerializer(many=True, required=False)

    def validate_applications(self, rows):
        if len(rows) > 500 or len({(r['application'].pk, r['position']) for r in rows}) != len(rows):
            raise ValidationError('Usa hasta 500 aplicaciones sin repetir aplicación y posición.')
        return rows


def specification(field, payload, part):
    value = payload['value']
    if value is None or value == '':
        return None
    row = PartSpecification(part=part, field=field, source=payload['source'])
    if field.kind not in ['number', 'integer'] and payload.get('unit'):
        raise ValidationError(f'{field.label}: este campo no usa unidades.')
    if field.kind in ['number', 'integer']:
        try:
            if isinstance(value, (bool, dict, list)):
                raise ValueError()
            number = Decimal(str(value))
            source_unit = payload.get('unit', field.unit)
            if source_unit != field.unit:
                if source_unit not in UNITS or field.unit not in UNITS or UNITS[source_unit][0] != UNITS[field.unit][0]:
                    raise ValueError()
                number = number * Decimal(UNITS[source_unit][1]) / Decimal(UNITS[field.unit][1])
            if not number.is_finite() or abs(number) >= Decimal('100000000000000'):
                raise ValueError()
            if field.kind == 'integer' and number != number.to_integral_value():
                raise ValueError()
            rounded = number.quantize(Decimal('0.000001'))
            if rounded != number:
                raise ValueError()
            row.number_value = rounded
        except (InvalidOperation, ValueError, TypeError):
            raise ValidationError(f'{field.label}: indica un número válido y una unidad compatible (hasta 6 decimales).')
    elif field.kind == 'boolean':
        if type(value) is not bool:
            raise ValidationError(f'{field.label}: selecciona Sí, No o deja el dato sin especificar.')
        row.boolean_value = value
    else:
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValidationError(f'{field.label}: indica un texto válido de hasta 2000 caracteres.')
        row.text_value = normalized_label(value)
        if field.kind == 'choice' and row.text_value not in field.options:
            raise ValidationError(f'{field.label}: selecciona una de las opciones de la plantilla.')
    return row


def sheet_data(part, public=False):
    template = TechnicalTemplate.objects.filter(pk=part.part_type_id).prefetch_related('fields').first() if part.part_type_id else None
    values = {}
    for row in part.specifications.select_related('field').all():
        value = format(row.number_value.normalize(), 'f') if row.number_value is not None else row.boolean_value if row.boolean_value is not None else row.text_value
        values[row.field.key] = {'value': value, 'unit': row.field.unit}
        if not public:
            values[row.field.key]['source'] = row.source
    links = part.item_applications.select_related('application')
    if public:
        links = links.filter(verified=True)
    applications = [{'application': ApplicationSerializer(link.application).data, 'position': link.position,
                     'notes': link.notes, **({} if public else {'source': link.source, 'verified': link.verified})} for link in links]
    return {'part': str(part.pk), 'part_type': PartTypeSerializer(part.part_type).data if part.part_type_id else None,
            'revision': part.technical_revision, 'template': template_data(template) if template else None,
            'values': values, 'applications': applications,
            'missing_required': [f.key for f in template.fields.all() if f.required and f.key not in values] if template else []}


class ItemTechnicalDetail(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(responses=OpenApiTypes.OBJECT, description='Resuelve la plantilla del subgrupo del SKU y devuelve valores, campos faltantes y aplicaciones vinculadas.')
    def get(self, request, pk):
        return Response(sheet_data(get_object_or_404(Part.objects.select_related('part_type'), pk=pk, merged_into__isnull=True)))

    @extend_schema(request=SheetWriteSerializer, responses=OpenApiTypes.OBJECT,
                   description='Sustituye los valores o vínculos incluidos; conserva las secciones omitidas. Requiere las revisiones actuales. Los valores nulos significan desconocido.')
    @transaction.atomic
    def patch(self, request, pk):
        part = get_object_or_404(Part.objects.select_for_update(), pk=pk, merged_into__isnull=True)
        serializer = SheetWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        template = TechnicalTemplate.objects.select_for_update().filter(pk=part.part_type_id).first() if part.part_type_id else None
        if data['revision'] != part.technical_revision or data['part_type'] != part.part_type_id or data['template_revision'] != (template.revision if template else None):
            raise ValidationError('La ficha, el subgrupo o la plantilla cambió. Vuelve a abrir la ficha antes de guardar.')
        if 'values' in data:
            fields = {f.key: f for f in template.fields.all()} if template else {}
            if set(data['values']) - fields.keys():
                raise ValidationError('Hay campos que no pertenecen a la plantilla del subgrupo.')
            rows = [specification(fields[key], payload, part) for key, payload in data['values'].items()]
            part.specifications.all().delete()
            PartSpecification.objects.bulk_create([r for r in rows if r is not None])
        if 'applications' in data:
            ids = [r['application'].pk for r in data['applications']]
            if Application.objects.select_for_update().filter(pk__in=ids).count() != len(set(ids)):
                raise ValidationError('Una aplicación ya no existe. Actualiza la ficha.')
            part.item_applications.all().delete()
            ItemApplication.objects.bulk_create([ItemApplication(part=part, **row) for row in data['applications']])
        part.technical_revision += 1
        part.save(update_fields=['technical_revision'])
        return Response(sheet_data(part))


class CatalogTechnicalDetail(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='Ficha técnica del SKU activo y aplicaciones verificadas. No incluye fuentes internas.')
    def get(self, request, pk):
        if not Membership.objects.filter(user=request.user, account__active=True).exists():
            raise PermissionDenied('Tu administrador debe configurar tu cuenta.')
        part = get_object_or_404(Part.objects.select_related('part_type'), pk=pk, active=True, merged_into__isnull=True)
        return Response(sheet_data(part, public=True))
