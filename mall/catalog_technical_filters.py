"""Public technical-sheet filters, applied to the scoped catalog before pagination."""
import json

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .catalog_scope import request_scope
from .lines import storefront_lines
from .models import PartSpecification, PartType, TechnicalField
from .technical_api import TechnicalFieldSerializer
from .technical_data import grupo_nombre


class RuleSerializer(serializers.Serializer):
    key = serializers.RegexField(r'^[a-z][a-z0-9_]{0,59}$')
    operator = serializers.ChoiceField(choices=['eq', 'gt', 'gte', 'lt', 'lte', 'between', 'contains'])
    value = serializers.CharField(max_length=120)
    value_to = serializers.CharField(max_length=120, required=False, default='', allow_blank=True)


class FilterSerializer(serializers.Serializer):
    part_type = serializers.UUIDField()
    rules = RuleSerializer(many=True, max_length=8, default=list)


def technical_filter(raw):
    if not raw:
        return None
    if len(raw) > 6000:
        raise ValidationError('El filtro técnico es demasiado largo.')
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        raise ValidationError('El filtro técnico no es válido.')
    serializer = FilterSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    fields = {field.key: field for field in TechnicalField.objects.filter(template_id=data['part_type'])} if data['part_type'] else {}
    if data['part_type'] and not PartType.objects.filter(pk=data['part_type']).exists():
        raise ValidationError('El subgrupo técnico ya no existe.')
    seen = set()
    for rule in data['rules']:
        field = fields.get(rule['key'])
        if not field or rule['key'] in seen:
            raise ValidationError('Selecciona campos distintos de la plantilla del subgrupo.')
        seen.add(rule['key'])
        rule['field_id'] = field.pk
        numeric = field.kind in ('number', 'integer')
        allowed = ['eq', 'gt', 'gte', 'lt', 'lte', 'between'] if numeric else ['eq', 'contains'] if field.kind == 'text' else ['eq']
        if rule['operator'] not in allowed:
            raise ValidationError(f'{field.label}: la comparación no es válida.')
        if numeric:
            decimal = serializers.DecimalField(max_digits=20, decimal_places=6)
            rule['value'] = decimal.run_validation(rule['value'])
            if rule['operator'] == 'between':
                rule['value_to'] = decimal.run_validation(rule['value_to'])
                if rule['value'] > rule['value_to']:
                    raise ValidationError(f'{field.label}: el mínimo debe ser menor o igual al máximo.')
            rule['column'] = 'number_value'
        elif field.kind == 'boolean':
            if rule['value'] not in ('true', 'false'):
                raise ValidationError(f'{field.label}: selecciona Sí o No.')
            rule['value'] = rule['value'] == 'true'
            rule['column'] = 'boolean_value'
        else:
            rule['value'] = rule['value'].strip().upper()
            if field.kind == 'choice' and rule['value'] not in field.options:
                raise ValidationError(f'{field.label}: selecciona una opción de la plantilla.')
            rule['column'] = 'text_value'
    return data


def apply_technical_filter(query, data):
    if not data:
        return query
    if data['part_type']:
        query = query.filter(part_type_id=data['part_type'])
    for rule in data['rules']:
        column, operator = rule['column'], rule['operator']
        lookup = {'eq': 'iexact' if column == 'text_value' else 'exact', 'gt': 'gt', 'gte': 'gte', 'lt': 'lt', 'lte': 'lte', 'between': 'range', 'contains': 'icontains'}[operator]
        value = (rule['value'], rule['value_to']) if operator == 'between' else rule['value']
        matches = PartSpecification.objects.filter(field_id=rule['field_id'], **{f'{column}__{lookup}': value})
        query = query.filter(pk__in=matches.values('part_id'))
    return query


class CatalogTechnicalFilters(APIView):
    @extend_schema(operation_id='v1_catalog_technical_filters', responses=OpenApiTypes.OBJECT,
                   description='Los subgrupos con ficha técnica de las líneas que ve el catálogo, con sus campos filtrables.')
    def get(self, request):
        parts = request_scope(request).parts(storefront_lines())
        types = (PartType.objects.filter(pk__in=parts.values('part_type_id'), template__fields__isnull=False)
                 .select_related('grupo').prefetch_related('template__fields').distinct().order_by('grupo__nombre', 'name', 'id'))
        return Response({'part_types': [{'id': str(kind.pk), 'category': grupo_nombre(kind), 'name': kind.name,
                                         'fields': TechnicalFieldSerializer(kind.template.fields.all(), many=True).data} for kind in types]})
