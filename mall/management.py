from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Value
from django.db.models.functions import Greatest
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import filters, generics, permissions, serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from .list_ordering import StableOrdering
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError

from .models import Account, Invitation, Membership, Part, PartCode, PartType, Role, SupplierItem, User
from .serializers import SupplierItemSerializer
from .media_serializers import PartImageSerializer


class IsSuperuser(permissions.BasePermission):
    message = 'Esta sección está disponible únicamente para superusuarios.'

    def has_permission(self, request, view):
        return bool(request.user.is_authenticated and request.user.is_active and request.user.is_superuser)


class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'is_superuser']
        read_only_fields = fields


class ProfileView(generics.RetrieveAPIView):
    serializer_class = ProfileSerializer

    def get_object(self):
        return self.request.user


class MembershipSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.EmailField(source='user.email', read_only=True)
    account_name = serializers.CharField(source='account.name', read_only=True)
    user_active = serializers.BooleanField(source='user.is_active', read_only=True)
    permission_label = serializers.CharField(source='get_permission_display', read_only=True)

    class Meta:
        model = Membership
        fields = ['id', 'user', 'account', 'permission', 'username', 'email', 'account_name', 'user_active', 'permission_label']
        validators = []

    def validate(self, data):
        user = data.get('user', getattr(self.instance, 'user', None))
        account = data.get('account', getattr(self.instance, 'account', None))
        existing = Membership.objects.filter(user=user, account=account)
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError('Este usuario ya pertenece a esta cuenta.')
        return data


class ManagedAccountSerializer(serializers.ModelSerializer):
    roles = serializers.SlugRelatedField(queryset=Role.objects.all(), slug_field='code', many=True, required=False)
    member_count = serializers.IntegerField(read_only=True, default=0)
    members = MembershipSerializer(source='memberships', many=True, read_only=True)

    class Meta:
        model = Account
        fields = ['id', 'name', 'active', 'roles', 'member_count', 'members']


class ManagedUserSerializer(serializers.ModelSerializer):
    memberships = MembershipSerializer(source='membership_set', many=True, read_only=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'is_active', 'is_superuser', 'is_staff', 'date_joined', 'memberships']
        read_only_fields = ['id', 'username', 'is_superuser', 'is_staff', 'date_joined', 'memberships']

    def validate_email(self, email):
        email = email.strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError('Este correo electrónico ya está registrado.')
        return email

    def validate(self, data):
        if {'is_superuser', 'is_staff', 'password', 'user_permissions', 'groups'} & set(self.initial_data):
            raise serializers.ValidationError('Los permisos administrativos y las contraseñas no se modifican desde esta sección.')
        if data.get('is_active') is False and self.instance.is_superuser:
            raise serializers.ValidationError('Los superusuarios deben mantenerse activos.')
        return data

    @transaction.atomic
    def update(self, instance, validated_data):
        instance = super().update(instance, validated_data)
        if not instance.is_active:
            Token.objects.filter(user=instance).delete()
        return instance


class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ['code', 'name', 'capability']


class InvitationSerializer(serializers.ModelSerializer):
    days = serializers.IntegerField(min_value=1, max_value=30, default=7, write_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = Invitation
        fields = ['id', 'email', 'token', 'expires_at', 'accepted_at', 'status', 'days']
        read_only_fields = ['id', 'token', 'expires_at', 'accepted_at', 'status']

    def get_status(self, obj) -> str:
        if obj.accepted_at:
            return 'accepted'
        return 'expired' if obj.expires_at <= timezone.now() else 'pending'

    def validate_email(self, email):
        email = email.strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError('Este usuario ya está registrado. Asigna su cuenta desde la lista de usuarios.')
        return email

    def create(self, validated_data):
        days = validated_data.pop('days')
        return Invitation.objects.create(**validated_data, expires_at=timezone.now() + timedelta(days=days), created_by=self.context['request'].user)


class SuperuserMixin:
    permission_classes = [IsSuperuser]


class UppercaseCharField(serializers.CharField):
    def to_internal_value(self, data):
        return super().to_internal_value(data).upper()


from .reference_serializers import LegacyReferenceKind, validate_reference_type


class ManagedCodeSerializer(serializers.Serializer):
    brand = UppercaseCharField(max_length=120, required=False, allow_blank=True, default='')
    code = UppercaseCharField(max_length=120)
    kind = LegacyReferenceKind()
    ref_type = serializers.ChoiceField(choices=PartCode.REF_TYPES, required=False)
    reference_source = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def to_internal_value(self, data):
        result = super().to_internal_value(data)
        return validate_reference_type(data, result)

    def validate(self, data):
        data.setdefault('brand', '')
        if not data.get('code'):
            raise serializers.ValidationError({'code': 'Ingresa un código equivalente.'})
        return data


def validate_code_mapping(*, part, brand, code):
    existing = PartCode.objects.filter(code=code)
    if brand:
        existing = existing.filter(Q(brand=brand) | Q(brand=''))
    skus = Part.objects.filter(sku=code)
    if part:
        existing = existing.exclude(part=part)
        skus = skus.exclude(Q(pk=part.pk) | Q(merged_into=part))
    if existing.exists() or skus.exists():
        raise serializers.ValidationError(f'El código {code} ya pertenece a otro SKU. Si son equivalentes, usa Agrupar SKU para unificarlos primero.')


class ManagedPartSerializer(serializers.ModelSerializer):
    images = PartImageSerializer(many=True, read_only=True)
    identity = serializers.SerializerMethodField()
    main_reference = ManagedCodeSerializer(required=False, write_only=True)

    def get_identity(self, obj) -> dict:
        from .catalog_identity import identity_status
        return identity_status(obj)

    sku = UppercaseCharField(max_length=200)
    name = UppercaseCharField(max_length=200, required=False, allow_blank=True)
    description = UppercaseCharField(required=False, allow_blank=True)
    category = UppercaseCharField(max_length=120, required=False, allow_blank=True)
    subcategory = UppercaseCharField(max_length=120, required=False, allow_blank=True)
    codes = ManagedCodeSerializer(many=True, required=False)
    part_type = serializers.PrimaryKeyRelatedField(queryset=PartType.objects.all(), required=False, allow_null=True)
    stock_record_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Part
        fields = ['id', 'sku', 'is_OEM', 'name', 'description', 'category', 'subcategory', 'part_type', 'active', 'codes', 'stock_record_count', 'images', 'identity', 'main_reference']
        read_only_fields = ['id', 'stock_record_count']

    def validate_sku(self, sku):
        parts = Part.objects.filter(sku__iexact=sku)
        codes = PartCode.objects.filter(code=sku)
        if self.instance:
            parts = parts.exclude(pk=self.instance.pk)
            codes = codes.exclude(part=self.instance)
        if parts.exists() or codes.exists():
            raise serializers.ValidationError('Este SKU o código ya identifica otro repuesto del catálogo.')
        return sku

    def validate(self, data):
        if 'brand' in self.initial_data:
            raise serializers.ValidationError({'brand': 'El SKU interno no lleva marca. La marca es opcional en cada código equivalente.'})
        if 'part_type' in data:
            part_type = data['part_type']
            expected = {'category': part_type.category if part_type else '', 'subcategory': part_type.name if part_type else ''}
            if any(key in data and data[key] != value for key, value in expected.items()):
                raise serializers.ValidationError('El grupo y subgrupo deben corresponder al tipo de repuesto seleccionado.')
            data.update(expected)
        return data

    def validate_codes(self, codes):
        pairs = {(code['brand'], code['code']) for code in codes}
        if len(pairs) != len(codes):
            raise serializers.ValidationError('No repitas la misma combinación de marca y código.')
        for brand, code in pairs:
            validate_code_mapping(part=self.instance, brand=brand, code=code)
        return codes

    def save_codes(self, part, codes):
        keep = []
        for code in codes:
            entry, _ = PartCode.objects.update_or_create(part=part, brand=code['brand'], code=code['code'],
                defaults={k: v for k, v in code.items() if k not in ['brand', 'code']})
            keep.append(entry.pk)
        part.codes.exclude(pk__in=keep).delete()

    def create(self, validated_data):
        codes = validated_data.pop('codes', [])
        main = validated_data.pop('main_reference', None)
        try:
            with transaction.atomic():
                part = Part.objects.create(**validated_data)
                self.save_codes(part, codes)
                from .catalog_identity import company_reference, preserve_sku_reference, prefer_oem
                if not part.is_OEM and company_reference(part.sku):
                    preserve_sku_reference(part, part.sku)
                return prefer_oem(part, actor=getattr(self.context.get('request'), 'user', None), selected=main,
                                  confirm_current='is_OEM' not in validated_data)
        except IntegrityError:
            raise serializers.ValidationError('El SKU o uno de sus códigos ya está registrado.')

    def update(self, instance, validated_data):
        codes = validated_data.pop('codes', None)
        main = validated_data.pop('main_reference', None)
        try:
            with transaction.atomic():
                instance = Part.objects.select_for_update().get(pk=instance.pk)
                if instance.merged_into_id:
                    raise serializers.ValidationError('Este SKU ya fue unificado. Modifica el SKU principal.')
                old_sku = instance.sku
                old_is_oem = instance.is_OEM
                # The flag describes this exact main code, not every alternate.
                if validated_data.get('sku', old_sku) != old_sku:
                    validated_data.setdefault('is_OEM', False)
                instance = super().update(instance, validated_data)
                if codes is not None:
                    self.save_codes(instance, codes)
                from .catalog_identity import company_reference, preserve_sku_reference, prefer_oem
                if old_sku != instance.sku or (not old_is_oem and company_reference(old_sku)):
                    preserve_sku_reference(instance, old_sku, is_oem=old_is_oem)
                # Explicit flag-only edits should not rename the SKU as a side effect.
                if main or 'is_OEM' not in validated_data or codes is not None:
                    instance = prefer_oem(instance, actor=getattr(self.context.get('request'), 'user', None), selected=main,
                                          confirm_current=codes is not None and 'is_OEM' not in validated_data)
                instance.stock_record_count = instance.supplier_items.count()
                return instance
        except IntegrityError:
            raise serializers.ValidationError('El SKU o uno de sus códigos ya está registrado.')


class ManagedInventorySerializer(SupplierItemSerializer):
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    part_name = serializers.CharField(source='part.name', read_only=True, default=None)
    part_sku = serializers.CharField(source='part.sku', read_only=True, default=None)

    class Meta(SupplierItemSerializer.Meta):
        fields = SupplierItemSerializer.Meta.fields + ['supplier', 'supplier_name', 'part_name', 'part_sku']
        read_only_fields = fields


class InventoryMatchSerializer(ManagedInventorySerializer):
    class Meta(ManagedInventorySerializer.Meta):
        read_only_fields = [field for field in ManagedInventorySerializer.Meta.fields if field not in ['part', 'matching_status']]

    def validate(self, data):
        if set(self.initial_data) - {'part', 'matching_status'}:
            raise serializers.ValidationError('Desde aquí solo se modifica el vínculo con el catálogo y su estado.')
        part = data.get('part', self.instance.part)
        status = data.get('matching_status', self.instance.matching_status)
        if part and part.merged_into_id:
            raise serializers.ValidationError({'part': 'Selecciona el SKU principal; este SKU ya fue unificado.'})
        if status == 'matched' and not part:
            raise serializers.ValidationError({'part': 'Selecciona un repuesto para aprobar la coincidencia.'})
        return data

    @transaction.atomic
    def update(self, instance, validated_data):
        assigned = validated_data.get('part')
        if assigned:
            assigned = Part.objects.select_for_update().get(pk=assigned.pk)
            if assigned.merged_into_id:
                raise serializers.ValidationError({'part': 'Selecciona el SKU principal; este SKU ya fue unificado.'})
            validated_data['part'] = assigned
        instance = SupplierItem.objects.select_for_update().get(pk=instance.pk)
        # Recheck after locking, since a supplier sync can change the mapping.
        if validated_data.get('matching_status', instance.matching_status) == 'matched' and not validated_data.get('part', instance.part):
            raise serializers.ValidationError({'part': 'Selecciona un repuesto para aprobar la coincidencia.'})
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save(update_fields=[*validated_data.keys(), 'updated_at'])
        from .matching_engine import remember
        from .matching_queue import enqueue_matching
        if instance.matching_status == 'matched' and instance.part_id:
            remember(instance, instance.part, self.context['request'].user)
        enqueue_matching()
        return instance


class ManagedAlternateSerializer(serializers.ModelSerializer):
    kind = LegacyReferenceKind()
    ref_type = serializers.ChoiceField(choices=PartCode.REF_TYPES, required=False)

    def to_internal_value(self, data):
        return validate_reference_type(data, super().to_internal_value(data), self.instance)

    code = UppercaseCharField(max_length=120)
    brand = UppercaseCharField(max_length=120, required=False, allow_blank=True, default='')
    part_name = serializers.CharField(source='part.name', read_only=True)
    part_sku = serializers.CharField(source='part.sku', read_only=True)

    class Meta:
        model = PartCode
        fields = ['id', 'part', 'part_name', 'part_sku', 'code', 'brand', 'kind', 'ref_type', 'reference_source']
        read_only_fields = ['id', 'part_name', 'part_sku']
        validators = []

    def validate(self, data):
        if 'replacement' in self.initial_data or 'notes' in self.initial_data:
            raise serializers.ValidationError('Un alterno es una referencia equivalente del SKU maestro. Ingresa el código y su fabricante opcional.')
        part = data.get('part', getattr(self.instance, 'part', None))
        if part and part.merged_into_id:
            raise serializers.ValidationError({'part': 'Los alternos se asignan al SKU principal; este SKU ya fue unificado.'})
        code = data.get('code', getattr(self.instance, 'code', ''))
        brand = data.get('brand', getattr(self.instance, 'brand', ''))
        validate_code_mapping(part=part, brand=brand, code=code)
        existing = PartCode.objects.filter(brand=brand, code=code)
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError('Este alterno ya está registrado para este SKU y marca.')
        return data

    def create(self, validated_data):
        try:
            with transaction.atomic():
                part = Part.objects.select_for_update().get(pk=validated_data['part'].pk)
                if part.merged_into_id:
                    raise serializers.ValidationError({'part': 'Selecciona el SKU principal; este SKU ya fue unificado.'})
                validate_code_mapping(part=part, brand=validated_data.get('brand', ''), code=validated_data['code'])
                return super().create(validated_data)
        except IntegrityError:
            raise serializers.ValidationError('Este alterno ya está registrado.')

    def update(self, instance, validated_data):
        try:
            with transaction.atomic():
                part = Part.objects.select_for_update().get(pk=validated_data.get('part', instance.part).pk)
                if part.merged_into_id:
                    raise serializers.ValidationError({'part': 'Selecciona el SKU principal; este SKU ya fue unificado.'})
                instance = PartCode.objects.select_for_update().get(pk=instance.pk)
                validate_code_mapping(part=part, brand=validated_data.get('brand', instance.brand), code=validated_data.get('code', instance.code))
                return super().update(instance, validated_data)
        except IntegrityError:
            raise serializers.ValidationError('Este alterno ya está registrado.')


class CatalogSearch(filters.SearchFilter):
    """The search fields, or an OEM number the SKU reaches or an aftermarket code filed under one (mall.oem_links.search_q)."""

    def filter_queryset(self, request, queryset, view):
        from .oem_links import search_q
        terms = self.get_search_terms(request)
        matched = super().filter_queryset(request, queryset, view)
        library = search_q(' '.join(terms)) if terms else None
        # DRF removes duplicates with an Exists subquery, and the library adds pk__in subqueries: both sides stay row-unique.
        return matched | queryset.filter(library) if library is not None else matched


class CatalogList(SuperuserMixin, generics.ListCreateAPIView):
    serializer_class = ManagedPartSerializer
    filter_backends = [CatalogSearch, StableOrdering]
    search_fields = ['sku', 'name', 'description', 'codes__code', 'codes__brand']
    ordering_fields = ['sku', 'name', 'description', 'category', 'subcategory', 'active', 'is_OEM', 'stock_record_count']
    queryset = Part.objects.filter(merged_into__isnull=True).annotate(stock_record_count=Count('supplier_items', distinct=True)).prefetch_related('codes', 'images').order_by('sku', 'id')

    def get_queryset(self):
        queryset = super().get_queryset()
        value = self.request.query_params.get('is_OEM')
        if value is not None:
            if value.lower() not in ['true', 'false']:
                raise serializers.ValidationError({'is_OEM': 'Usa true o false.'})
            queryset = queryset.filter(is_OEM=value.lower() == 'true')
        return queryset


class TaxonomyPair(serializers.Serializer):
    category = serializers.CharField()
    subcategory = serializers.CharField()


class TaxonomyResponse(serializers.Serializer):
    results = TaxonomyPair(many=True)


class CatalogTaxonomy(SuperuserMixin, APIView):
    @extend_schema(operation_id='v1_management_catalog_taxonomy', responses=TaxonomyResponse,
                   description='Los grupos y subgrupos del catálogo (las reglas de clasificación y los tipos de repuesto existentes), para elegir uno sin '
                               'crear subgrupos nuevos por error.')
    def get(self, request):
        from .category_suggestions import taxonomy
        return Response({'results': [{'category': category, 'subcategory': subcategory} for category, subcategory in taxonomy()]})


class CatalogDetail(SuperuserMixin, generics.RetrieveUpdateAPIView):
    serializer_class = ManagedPartSerializer
    queryset = CatalogList.queryset
    http_method_names = ['get', 'patch', 'head', 'options']


class InventoryList(SuperuserMixin, generics.ListAPIView):
    serializer_class = ManagedInventorySerializer
    filter_backends = [filters.SearchFilter, StableOrdering]
    search_fields = ['codigo', 'brand', 'supplier_invent_id', 'supplier__name', 'part__sku', 'part__name']
    ordering_fields = ['supplier__name', 'codigo', 'brand', 'part__sku', 'supplier_invent_id', 'available', 'reserved_quantity', 'matching_status']
    # available mirrors SupplierItem.available_quantity (never below zero) so the grid can sort by it.
    queryset = (SupplierItem.objects.select_related('supplier', 'part')
                .annotate(available=Greatest(F('reported_quantity') - F('reserved_quantity'), Value(0)))
                .order_by('supplier__name', 'codigo', 'id'))


class InventoryDetail(SuperuserMixin, generics.RetrieveUpdateAPIView):
    serializer_class = InventoryMatchSerializer
    queryset = InventoryList.queryset
    http_method_names = ['get', 'patch', 'head', 'options']


class AlternateList(SuperuserMixin, generics.ListCreateAPIView):
    serializer_class = ManagedAlternateSerializer
    filter_backends = [filters.SearchFilter, StableOrdering]
    search_fields = ['part__sku', 'part__name', 'code', 'brand']
    ordering_fields = ['part__sku', 'code', 'brand', 'ref_type']
    queryset = PartCode.objects.select_related('part').order_by('part__sku', 'code', 'brand', 'id')


class AlternateDetail(SuperuserMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ManagedAlternateSerializer
    queryset = AlternateList.queryset
    http_method_names = ['get', 'patch', 'delete', 'head', 'options']


class AccountList(SuperuserMixin, generics.ListCreateAPIView):
    serializer_class = ManagedAccountSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['name']
    queryset = Account.objects.annotate(member_count=Count('memberships')).prefetch_related('roles', 'memberships__user').order_by('name', 'id')


class AccountDetail(SuperuserMixin, generics.RetrieveUpdateAPIView):
    serializer_class = ManagedAccountSerializer
    queryset = AccountList.queryset
    http_method_names = ['get', 'patch', 'head', 'options']


class UserList(SuperuserMixin, generics.ListAPIView):
    serializer_class = ManagedUserSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['username', 'email', 'first_name', 'last_name']
    queryset = User.objects.prefetch_related('membership_set__account').order_by('username', 'id')


class UserDetail(SuperuserMixin, generics.RetrieveUpdateAPIView):
    serializer_class = ManagedUserSerializer
    queryset = UserList.queryset
    http_method_names = ['get', 'patch', 'head', 'options']


class RoleList(SuperuserMixin, generics.ListAPIView):
    serializer_class = RoleSerializer
    queryset = Role.objects.order_by('capability', 'name')
    pagination_class = None


class MembershipCreate(SuperuserMixin, generics.CreateAPIView):
    serializer_class = MembershipSerializer

    @transaction.atomic
    def perform_create(self, serializer):
        # Serialize the uniqueness check with other admin writes to this account.
        Account.objects.select_for_update().get(pk=serializer.validated_data['account'].pk)
        if Membership.objects.filter(user=serializer.validated_data['user'], account=serializer.validated_data['account']).exists():
            raise ValidationError('Este usuario ya pertenece a esta cuenta.')
        serializer.save()


class MembershipDetail(SuperuserMixin, generics.UpdateAPIView, generics.DestroyAPIView):
    serializer_class = MembershipSerializer
    queryset = Membership.objects.select_related('user', 'account')
    http_method_names = ['patch', 'delete', 'options']

    def perform_update(self, serializer):
        if 'user' in serializer.validated_data or 'account' in serializer.validated_data:
            raise ValidationError('Para cambiar la cuenta o el usuario, elimina el acceso y crea uno nuevo.')
        serializer.save()


class InvitationList(SuperuserMixin, generics.ListCreateAPIView):
    serializer_class = InvitationSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['email']
    queryset = Invitation.objects.order_by('-id')


class InvitationRevoke(SuperuserMixin, generics.UpdateAPIView):
    serializer_class = InvitationSerializer
    queryset = Invitation.objects.all()
    http_method_names = ['patch', 'options']

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        invitation = Invitation.objects.select_for_update().get(pk=self.get_object().pk)
        if invitation.accepted_at:
            raise ValidationError('Esta invitación ya fue aceptada.')
        invitation.expires_at = timezone.now()
        invitation.save(update_fields=['expires_at'])
        from rest_framework.response import Response
        return Response(self.get_serializer(invitation).data)
