from rest_framework import serializers


class LegacyReferenceKind(serializers.ChoiceField):
    """Map deprecated kind onto ref_type without storing contradictory fields."""
    def __init__(self, **kwargs):
        super().__init__(choices=['alias', 'oem', 'manufacturer'], source='ref_type', required=False, **kwargs)

    def to_internal_value(self, value):
        value = super().to_internal_value(value)
        return {'alias': 'unknown', 'manufacturer': 'company'}.get(value, value)

    def to_representation(self, value):
        return {'unknown': 'alias', 'company': 'manufacturer'}.get(value, value)


class CitationField(serializers.CharField):
    """reference_source in the JSON (inventory refactor S10: no column any more): read, a Reference's newest live source's citation
    (mall.references.source_text: prefetch_related('sources') for a list); written, a source mall.references cites."""
    def __init__(self, **kwargs):
        kwargs.setdefault('max_length', 500)
        kwargs.setdefault('required', False)
        kwargs.setdefault('allow_blank', True)
        super().__init__(**kwargs)

    def get_attribute(self, instance):
        from .models import Reference
        if isinstance(instance, Reference):
            from .references import source_text
            return source_text(instance)
        return super().get_attribute(instance)


def validate_reference_type(initial, data, instance=None):
    if 'kind' in initial and 'ref_type' in initial:
        old = {'alias': 'unknown', 'manufacturer': 'company'}.get(initial['kind'], initial['kind'])
        if old != initial['ref_type']:
            raise serializers.ValidationError({'ref_type': 'ref_type y kind deben indicar el mismo tipo.'})
    ref_type = data.get('ref_type', getattr(instance, 'ref_type', 'unknown'))
    brand = data.get('brand', getattr(instance, 'brand', ''))
    # Legacy data may lack a company; new explicitly typed company refs may not.
    if 'ref_type' in initial and ref_type == 'company' and not brand:
        raise serializers.ValidationError({'brand': 'Indica la empresa o fabricante, por ejemplo FEBEST.'})
    return data


class NonMainReferences(serializers.ListSerializer):
    """A line's code list without its main Reference (inventory refactor S2b): the codigo already shows it. Writes are unchanged."""
    def to_representation(self, data):
        rows = data.all() if hasattr(data, 'all') else data
        return super().to_representation([row for row in rows if not getattr(row, 'is_main', False)])


class MainReferenceSerializer(serializers.Serializer):
    """The line's main Reference, as the admin reads it (read-only): its id, code, brand ('' any brand), tipo and estado."""
    id = serializers.IntegerField()
    code = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    tipo = serializers.CharField()
    estado = serializers.CharField()
