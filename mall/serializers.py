from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from .models import Account, Membership, Part, PartCode, StockEntry, SupplierItem
from .media_serializers import PartImageSerializer

class SignupSerializer(serializers.Serializer):
    token = serializers.UUIDField()
    email = serializers.EmailField()
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True)
    def validate_password(self, value):
        from .models import User
        user = User(username=self.initial_data.get('username', ''), email=self.initial_data.get('email', ''))
        try:
            validate_password(value, user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        return value

class SignupResponseSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    detail = serializers.CharField()

class AccountSerializer(serializers.ModelSerializer):
    capabilities = serializers.SerializerMethodField()
    def get_capabilities(self, obj) -> list[str]:
        return sorted({role.capability for role in obj.roles.all()})
    roles = serializers.SlugRelatedField(many=True, read_only=True, slug_field='code')
    permission = serializers.SerializerMethodField()
    @extend_schema_field(serializers.ChoiceField(choices=Membership._meta.get_field('permission').choices))
    def get_permission(self, obj):
        # Only the caller's own membership, prefetched by AccountList; never other members' permissions.
        return obj.own_memberships[0].permission
    class Meta:
        model = Account
        fields = ['id', 'name', 'roles', 'capabilities', 'permission']

class PartCodeSerializer(serializers.ModelSerializer):
    kind = serializers.CharField(read_only=True)
    class Meta:
        model = PartCode
        fields = ['brand', 'code', 'kind', 'ref_type']

class PartEquivalentSerializer(serializers.Serializer):
    brand = serializers.CharField()
    code = serializers.CharField()


class CatalogAvailabilitySerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=['unknown', 'sold_out', 'low', 'medium', 'high'])
    supplier_count = serializers.IntegerField(min_value=0)
    updated_at = serializers.DateTimeField(allow_null=True)


class PartListSerializer(serializers.ListSerializer):
    """Reads the page's aftermarket codes in one go when the caller did not pass them (equivalents_by_part)."""

    def to_representation(self, data):
        parts = list(data.all() if hasattr(data, 'all') else data)
        if 'equivalents_by_part' not in self.context:
            from .oem_links import equivalents_for
            self._context['equivalents_by_part'] = equivalents_for([part.pk for part in parts])
        return super().to_representation(parts)


class PartSerializer(serializers.ModelSerializer):
    codes = PartCodeSerializer(many=True, read_only=True)
    images = PartImageSerializer(many=True, read_only=True)
    availability = serializers.SerializerMethodField()
    equivalents = serializers.SerializerMethodField()

    @extend_schema_field(PartEquivalentSerializer(many=True))
    def get_equivalents(self, obj):
        """Aftermarket codes the SKU reaches through its OEM numbers (mall.oem_links); lists pass them for the whole page."""
        by_part = self.context.get('equivalents_by_part')
        if by_part is None:
            from .oem_links import equivalents_for
            by_part = equivalents_for([obj.pk])
        return by_part.get(str(obj.pk), [])

    @extend_schema_field(CatalogAvailabilitySerializer)
    def get_availability(self, obj):
        value = self.context.get('availability_by_part', {}).get(obj.pk, {
            'status': 'unknown', 'supplier_count': 0, 'updated_at': None,
        })
        return CatalogAvailabilitySerializer(value).data

    class Meta:
        model = Part
        fields = ['id', 'sku', 'is_OEM', 'name', 'description', 'category', 'subcategory', 'part_type', 'codes', 'equivalents', 'availability', 'images']
        list_serializer_class = PartListSerializer

class SupplierItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupplierItem
        fields = ['id', 'supplier_invent_id', 'part', 'codigo', 'brand', 'description', 'references', 'matching_status', 'source', 'reported_quantity', 'reserved_quantity', 'available_quantity', 'updated_at']
        read_only_fields = fields

class InventorySerializer(serializers.Serializer):
    update_id = serializers.CharField(max_length=120)
    supplier_invent_id = serializers.CharField(max_length=120)
    codigo = serializers.CharField(max_length=120)
    brand = serializers.CharField(max_length=120, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    references = serializers.JSONField(required=False)
    source = serializers.ChoiceField(choices=['upload', 'apiag'])
    quantity = serializers.IntegerField(min_value=0)
    def validate_codigo(self, value):
        return value.strip().upper()
    def validate_brand(self, value):
        return value.strip().upper()
    def validate_references(self, value):
        from .part_references import normalize_references
        return normalize_references(value)

class StockEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = StockEntry
        fields = ['id', 'item', 'kind', 'quantity', 'direction', 'balance_type', 'reserved_delta', 'reserved_direction', 'reported_after', 'reserved_after', 'reference', 'created_at']

class OfferItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupplierItem
        fields = ['id', 'codigo', 'brand', 'description']
        read_only_fields = fields

class OfferSerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField()
    supplier_name = serializers.CharField()
    in_stock = serializers.BooleanField()
    items = OfferItemSerializer(many=True)
