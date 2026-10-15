from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from .models import Account, Membership, Part, Reference, StockEntry, SupplierItem
from .media_serializers import PartImageSerializer
from .reference_serializers import NonMainReferences

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
    apiag_connected = serializers.SerializerMethodField()

    def get_apiag_connected(self, obj) -> bool:
        """Synced with apiag-cloud: its stock loads from there, so the Excel import is not offered."""
        if hasattr(obj, 'apiag_linked'):  # AccountList annotates it: one query for the whole list
            return obj.apiag_linked
        from .apiag_models import ApiagConnection
        return ApiagConnection.objects.filter(account=obj).exists()
    @extend_schema_field(serializers.ChoiceField(choices=Membership._meta.get_field('permission').choices))
    def get_permission(self, obj):
        # Only the caller's own membership, prefetched by AccountList; never other members' permissions.
        return obj.own_memberships[0].permission
    class Meta:
        model = Account
        fields = ['id', 'name', 'roles', 'capabilities', 'permission', 'apiag_connected']

def line_keys(line):
    """The keys the JSON keeps for a line from before S10 (inventory refactor S10): name is its description, category and subcategory
    its subgrupo's grupo and name, is_OEM and active its tipo and estado. Load the line with select_related('part_type__grupo')."""
    from .technical_data import classification
    grupo, subgrupo = classification(line)
    return {'name': line.description or '', 'category': grupo, 'subcategory': subgrupo, 'is_OEM': line.tipo == 'OEM',
            'active': line.estado == 'publicada'}


class PartCodeSerializer(serializers.ModelSerializer):
    # tipo (oem, aftermarket, otro) and estado (verificado, declarado, inferido, disputa) from S7, beside the legacy kind and ref_type
    # (derived from tipo, S10): the storefront labels OEM and Aftermarket codes and hides the ones in disputa.
    kind = serializers.CharField(read_only=True)
    ref_type = serializers.SerializerMethodField()

    def get_ref_type(self, obj) -> str:
        from .references import legacy_ref_type
        return legacy_ref_type(obj.tipo)

    class Meta:
        model = Reference
        fields = ['brand', 'code', 'kind', 'ref_type', 'tipo', 'estado']
        list_serializer_class = NonMainReferences  # the line's main is its codigo (S2b): code lists leave it out

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
    # S7 (additive): the main Reference's brand (the part's maker; '' when the codigo has none), the line's tipo (OEM, AFTERMARKET,
    # UNKNOWN) and estado (publicada on the storefront). sku is the main Reference's code, so it holds 120 characters at most.
    sku = serializers.CharField(max_length=120, read_only=True)
    # The line's keys from before S10, derived (line_keys): name is its description, category and subcategory its subgrupo's.
    is_OEM = serializers.SerializerMethodField(help_text='La referencia principal es OEM (tipo OEM).')
    name = serializers.SerializerMethodField(help_text='La descripción.')
    category = serializers.SerializerMethodField(help_text='El grupo de su subgrupo.')
    subcategory = serializers.SerializerMethodField(help_text='Su subgrupo.')
    brand = serializers.SerializerMethodField(help_text='La marca de la referencia principal ("" si no tiene).')
    codes = PartCodeSerializer(many=True, read_only=True)
    images = PartImageSerializer(many=True, read_only=True)
    availability = serializers.SerializerMethodField()
    equivalents = serializers.SerializerMethodField()

    def get_brand(self, obj) -> str:
        from .references import main_in
        main = main_in(obj.codes.all())  # prefetched by the lists: no query per row
        return main.brand if main is not None else ''

    @extend_schema_field(PartEquivalentSerializer(many=True))
    def get_equivalents(self, obj):
        """Aftermarket codes the SKU reaches through its OEM numbers (mall.oem_links); lists pass them for the whole page."""
        by_part = self.context.get('equivalents_by_part')
        if by_part is None:
            from .oem_links import equivalents_for
            by_part = equivalents_for([obj.pk])
        return by_part.get(str(obj.pk), [])

    def get_is_OEM(self, obj) -> bool:
        return obj.tipo == 'OEM'

    def get_name(self, obj) -> str:
        return obj.description or ''

    def get_category(self, obj) -> str:
        return line_keys(obj)['category']

    def get_subcategory(self, obj) -> str:
        return line_keys(obj)['subcategory']

    @extend_schema_field(CatalogAvailabilitySerializer)
    def get_availability(self, obj):
        value = self.context.get('availability_by_part', {}).get(obj.pk, {
            'status': 'unknown', 'supplier_count': 0, 'updated_at': None,
        })
        return CatalogAvailabilitySerializer(value).data

    class Meta:
        model = Part
        fields = ['id', 'sku', 'is_OEM', 'name', 'description', 'category', 'subcategory', 'part_type', 'codes', 'equivalents', 'availability', 'images',
                  'brand', 'tipo', 'estado']
        read_only_fields = ['tipo', 'estado']
        list_serializer_class = PartListSerializer

class SupplierItemSerializer(serializers.ModelSerializer):
    # reference: the line's Reference the item hangs from (S3); codigo_pendiente/brand_pendiente: a code change waiting for review.
    class Meta:
        model = SupplierItem
        fields = ['id', 'supplier_invent_id', 'part', 'codigo', 'brand', 'description', 'references', 'matching_status', 'source', 'reported_quantity', 'reserved_quantity', 'available_quantity', 'updated_at',
                  'reference', 'codigo_pendiente', 'brand_pendiente']
        read_only_fields = fields

class SupplierStockSerializer(SupplierItemSerializer):
    """The supplier's own inventory rows: plus the units per sucursal of an apiag-cloud item (prefetched as `branch_stock`) and what the
    supplier said about the variant (S3.2; S8's inventory grid shows it: '' or null when unknown)."""
    branch_stock = serializers.SerializerMethodField()

    class Meta(SupplierItemSerializer.Meta):
        fields = SupplierItemSerializer.Meta.fields + ['branch_stock', 'calidad', 'condicion', 'garantia', 'unidades_por_empaque', 'material', 'origen']
        read_only_fields = fields

    @extend_schema_field({'type': 'array', 'items': {'type': 'object', 'properties': {
        'sucursal': {'type': 'string'}, 'nombre': {'type': 'string'}, 'disponible': {'type': 'integer'}}}})
    def get_branch_stock(self, item):
        return [{'sucursal': row.sucursal.apiag_id, 'nombre': row.sucursal.nombre, 'disponible': row.quantity} for row in item.branch_stock.all()]

# What a supplier may say about a variant (rule 10, S3.2): each optional, so a load without them hashes as before (InventoryUpdate).
VARIANT_DATA = ('material', 'origen', 'garantia', 'calidad', 'condicion', 'unidades_por_empaque')
CALIDAD_WORDS = {'ORIGINAL': 'original', 'GENUINO': 'original', 'GENUINE': 'original', 'OES': 'oes', 'OE': 'oes', 'ALTERNATIVO': 'alternativo',
                 'AFTERMARKET': 'alternativo', 'GENERICO': 'alternativo', 'GENÉRICO': 'alternativo'}
CONDICION_WORDS = {'NUEVO': 'nuevo', 'NEW': 'nuevo', 'REMANUFACTURADO': 'remanufacturado', 'REMAN': 'remanufacturado',
                   'REMANUFACTURED': 'remanufacturado'}


def _word(value, words, label):
    text = ' '.join(str(value or '').strip().upper().split())
    if not text:
        return ''
    if text not in words:
        raise serializers.ValidationError(f'{label} no reconocida: usa {", ".join(sorted(set(words.values())))}.')
    return words[text]


class InventorySerializer(serializers.Serializer):
    update_id = serializers.CharField(max_length=120)
    supplier_invent_id = serializers.CharField(max_length=120)
    codigo = serializers.CharField(max_length=120)
    brand = serializers.CharField(max_length=120, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    references = serializers.JSONField(required=False)
    source = serializers.ChoiceField(choices=['upload', 'apiag'])
    quantity = serializers.IntegerField(min_value=0)
    material = serializers.CharField(max_length=120, required=False, allow_blank=True)
    origen = serializers.CharField(max_length=120, required=False, allow_blank=True, help_text='País de origen.')
    garantia = serializers.CharField(max_length=120, required=False, allow_blank=True)
    calidad = serializers.CharField(required=False, allow_blank=True, help_text='original, oes o alternativo.')
    condicion = serializers.CharField(required=False, allow_blank=True, help_text='nuevo o remanufacturado.')
    unidades_por_empaque = serializers.IntegerField(min_value=1, max_value=32767, required=False, allow_null=True)
    def validate_codigo(self, value):
        return value.strip().upper()
    def validate_brand(self, value):
        return value.strip().upper()
    def validate_references(self, value):
        from .part_references import normalize_references
        return normalize_references(value)
    def validate_material(self, value):
        return value.strip()
    def validate_origen(self, value):
        return value.strip()
    def validate_garantia(self, value):
        return value.strip()
    def validate_calidad(self, value):
        return _word(value, CALIDAD_WORDS, 'Calidad')
    def validate_condicion(self, value):
        return _word(value, CONDICION_WORDS, 'Condición')

class StockEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = StockEntry
        fields = ['id', 'item', 'kind', 'quantity', 'direction', 'balance_type', 'reserved_delta', 'reserved_direction', 'reported_after', 'reserved_after', 'reference', 'created_at']

class OfferReferenceSerializer(serializers.Serializer):
    """The line's Reference a variant hangs from (S7): codes[] has no ids and leaves out the main and the interno codes, so the offer says
    which code it is."""
    id = serializers.IntegerField()
    code = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    tipo = serializers.CharField()


class OfferItemSerializer(serializers.ModelSerializer):
    # S7 (additive): what the supplier says about the variant (S3.2, '' or null when unknown) and the line's Reference it hangs from
    # ({id, code, brand, tipo}, or null).
    reference = OfferReferenceSerializer(read_only=True, allow_null=True)

    class Meta:
        model = SupplierItem
        fields = ['id', 'codigo', 'brand', 'description', 'calidad', 'condicion', 'garantia', 'unidades_por_empaque', 'reference']
        read_only_fields = fields

class OfferSerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField()
    supplier_name = serializers.CharField()
    in_stock = serializers.BooleanField()
    items = OfferItemSerializer(many=True)
