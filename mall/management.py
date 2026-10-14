import uuid
from datetime import timedelta

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Value
from django.db.models.functions import Greatest
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import filters, generics, permissions, serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from .list_ordering import StableOrdering
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError

from .apiag_tokens import ConnectionData, connection_data
from .lines import all_lines, catalog_lines, phrase, refuse_unificada, stage_estado
from . import references
from .models import Account, Invitation, Membership, Part, PartType, Reference, Role, SupplierItem, User
from .serializers import SupplierItemSerializer
from .media_serializers import PartImageSerializer


class IsSuperuser(permissions.BasePermission):
    message = 'Esta sección está disponible únicamente para superusuarios.'

    def has_permission(self, request, view):
        return bool(request.user.is_authenticated and request.user.is_active and request.user.is_superuser)


class ProfileSerializer(serializers.ModelSerializer):
    """The signed-in person's own profile ("Mi perfil"): they change their names freely, and their email (where notices go) only with
    their current password. The username, used to sign in, stays as it is."""
    current_password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)

    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'email', 'is_superuser', 'current_password']
        read_only_fields = ['id', 'username', 'is_superuser']
        extra_kwargs = {'email': {'required': False, 'allow_blank': False}}

    def validate_email(self, email):
        email = email.strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError('Este correo electrónico ya está registrado.')
        return email

    def validate(self, data):
        if 'email' in data and data['email'] != (self.instance.email or '').lower():
            if not self.instance.check_password(data.get('current_password') or ''):
                raise serializers.ValidationError({'current_password': ['Escribe tu contraseña actual para cambiar tu correo.']})
        data.pop('current_password', None)
        return data


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = ProfileSerializer
    http_method_names = ['get', 'patch', 'head', 'options']

    def get_object(self):
        return self.request.user


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(trim_whitespace=False)
    new_password = serializers.CharField(trim_whitespace=False)

    def validate(self, data):
        user = self.context['request'].user
        if not user.check_password(data['current_password']):
            raise serializers.ValidationError({'current_password': ['Tu contraseña actual no es correcta.']})
        try:
            validate_password(data['new_password'], user)
        except DjangoValidationError as error:
            raise serializers.ValidationError({'new_password': error.messages})
        return data


class PasswordChangeView(APIView):
    @extend_schema(request=PasswordChangeSerializer, responses=OpenApiTypes.OBJECT, description='Cambia la contraseña de la persona con la actual. '
                   'Su token se reemplaza: las sesiones de sus otros dispositivos se cierran y esta recibe el nuevo.')
    def post(self, request):
        data = PasswordChangeSerializer(data=request.data, context={'request': request})
        data.is_valid(raise_exception=True)
        user = request.user
        with transaction.atomic():
            user.set_password(data.validated_data['new_password'])
            user.save(update_fields=['password'])
            Token.objects.filter(user=user).delete()
            token = Token.objects.create(user=user)
        return Response({'token': token.key})


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
    apiag = serializers.SerializerMethodField()

    class Meta:
        model = Account
        fields = ['id', 'name', 'active', 'roles', 'member_count', 'members', 'apiag']

    @extend_schema_field(ConnectionData(allow_null=True))
    def get_apiag(self, account):
        return connection_data(getattr(account, 'apiag_connection', None))


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
    id = serializers.IntegerField(read_only=True, help_text='El alterno, para editarlo o retirarlo en management/alternates/.')
    brand = UppercaseCharField(max_length=120, required=False, allow_blank=True, default='')
    code = UppercaseCharField(max_length=120)
    kind = LegacyReferenceKind()
    ref_type = serializers.ChoiceField(choices=Reference.REF_TYPES, required=False)
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
    """Refuse a code another line holds: one of its live References under the same key (mall.references.validate: the clave, and a
    brandless code reserves it for every brand), or its codigo."""
    message = f'El código {code} ya pertenece a otro SKU. Si son equivalentes, usa Agrupar SKU para unificarlos primero.'
    held = references.conflicts(code, brand, line=part)
    if (held.exclude(part=part) if part else held).exists():
        raise serializers.ValidationError(message)
    skus = all_lines().filter(sku=code)  # a line in any estado holds its codigo: retired, merged elsewhere or not yet published
    if part:
        skus = skus.exclude(Q(pk=part.pk) | Q(merged_into=part))
    if skus.exists():
        raise serializers.ValidationError(message)


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
    part_type = serializers.PrimaryKeyRelatedField(queryset=PartType.objects.select_related('grupo'), required=False, allow_null=True)
    stock_record_count = serializers.IntegerField(read_only=True, default=0)
    merged_count = serializers.IntegerField(read_only=True, default=0, help_text='SKU agrupados en este SKU (Separar SKU los devuelve).')

    class Meta:
        model = Part
        fields = ['id', 'sku', 'is_OEM', 'name', 'description', 'category', 'subcategory', 'part_type', 'active', 'estado', 'origen', 'codes', 'stock_record_count', 'merged_count', 'images', 'identity', 'main_reference']
        # estado and origen are the line's lifecycle (mall.lines): `active` still turns a line publicada or retirada here.
        read_only_fields = ['id', 'estado', 'origen', 'stock_record_count', 'merged_count']

    def validate_sku(self, sku):
        parts = all_lines().filter(sku__iexact=sku)  # every estado: UNIQUE(sku) holds every line, merged ones too, until S4
        codes = Reference.objects.filter(code=sku)  # live References: a retired code no longer names its line
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
            from .technical_data import grupo_nombre
            part_type = data['part_type']
            expected = {'category': grupo_nombre(part_type) if part_type else '', 'subcategory': part_type.name if part_type else ''}
            if any(key in data and data[key] != value for key, value in expected.items()):
                raise serializers.ValidationError('El grupo y subgrupo deben corresponder al tipo de repuesto seleccionado.')
            data.update(expected)
        return data

    def validate_codes(self, codes):
        pairs = {(code['brand'], code['code']) for code in codes}
        if len(pairs) != len(codes):
            raise serializers.ValidationError('No repitas la misma combinación de marca y código.')
        if len({(brand, references.clave(code)) for brand, code in pairs}) != len(pairs):
            raise serializers.ValidationError('No repitas un código con otra grafía (con o sin guiones) para la misma marca.')
        for brand, code in pairs:
            validate_code_mapping(part=self.instance, brand=brand, code=code)
        return codes

    def actor(self):
        user = getattr(self.context.get('request'), 'user', None)
        return user if getattr(user, 'is_authenticated', False) else None

    def save_codes(self, part, codes):
        """The line's References become exactly these (mall.references): each one found by its key (a new spelling replaces the
        printed one) or added, its type and source as sent; the ones not sent are retired, never deleted."""
        actor = self.actor()
        keep = []
        for code in codes:
            extra = {key: value for key, value in code.items() if key not in ('brand', 'code')}
            entry, _ = references.add(part, code['code'], code['brand'], ref_type=extra.get('ref_type'), source=extra.get('reference_source', ''),
                                      actor=actor)
            changes = {key: value for key, value in {**extra, 'code': code['code']}.items() if getattr(entry, key) != value}
            if changes:
                references.update(entry, actor=actor, **changes)
            keep.append(entry.pk)
        references.retire(part.codes.exclude(pk__in=keep), actor=actor, reason='Quitada al editar los códigos del SKU')

    def create(self, validated_data):
        codes = validated_data.pop('codes', [])
        main = validated_data.pop('main_reference', None)
        validated_data.setdefault('estado', 'publicada' if validated_data.get('active', True) else 'retirada')  # unless a caller chose one
        try:
            with transaction.atomic():
                part = Part.objects.create(**validated_data)
                self.save_codes(part, codes)
                from .catalog_identity import company_reference, preserve_sku_reference, prefer_oem
                if not part.is_OEM and company_reference(part.sku):
                    preserve_sku_reference(part, part.sku, actor=self.actor())
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
                if 'active' in validated_data and validated_data['active'] != instance.active:
                    if instance.estado not in ('publicada', 'retirada'):
                        raise serializers.ValidationError({'active': f'Esta línea está {phrase(instance.estado)}: solo una línea '
                                                                     'publicada o retirada se activa o desactiva aquí.'})
                    # A person publishes or retires it: the decision is recorded as mall.lines.set_estado records it, saved below.
                    user = getattr(self.context.get('request'), 'user', None)
                    stage_estado(instance, 'publicada' if validated_data['active'] else 'retirada',
                                 user=user if getattr(user, 'is_authenticated', False) else None)
                old_sku = instance.sku
                old_is_oem = instance.is_OEM
                # The flag describes this exact main code, not every alternate.
                if validated_data.get('sku', old_sku) != old_sku:
                    validated_data.setdefault('is_OEM', False)
                elif old_is_oem and validated_data.get('is_OEM') is False:
                    from .catalog_oem_mark import listed_refusal
                    problem = listed_refusal(instance)  # its code is in the OEM list: dispute the number instead
                    if problem:
                        raise serializers.ValidationError({'is_OEM': problem})
                instance = super().update(instance, validated_data)
                if codes is not None:
                    self.save_codes(instance, codes)
                from .catalog_identity import company_reference, preserve_sku_reference, prefer_oem
                if old_sku != instance.sku or (not old_is_oem and company_reference(old_sku)):
                    preserve_sku_reference(instance, old_sku, is_oem=old_is_oem, actor=self.actor())
                # Explicit flag-only edits should not rename the SKU as a side effect.
                if main or 'is_OEM' not in validated_data or codes is not None:
                    instance = prefer_oem(instance, actor=getattr(self.context.get('request'), 'user', None), selected=main,
                                          confirm_current=codes is not None and 'is_OEM' not in validated_data)
                if instance.is_OEM and not old_is_oem:  # a SKU is OEM when its code is in the OEM list (an OEM alterno saved here files it)
                    from .catalog_oem_mark import unlisted_refusal
                    problem = unlisted_refusal(instance)
                    if problem:
                        raise serializers.ValidationError({'is_OEM': problem})
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
    """One Reference of a line, as the Alternos screen edits it; mall.references writes it and DELETE retires it."""
    kind = LegacyReferenceKind()
    ref_type = serializers.ChoiceField(choices=Reference.REF_TYPES, required=False)

    def to_internal_value(self, data):
        return validate_reference_type(data, super().to_internal_value(data), self.instance)

    code = UppercaseCharField(max_length=120)
    brand = UppercaseCharField(max_length=120, required=False, allow_blank=True, default='')
    part_name = serializers.CharField(source='part.name', read_only=True)
    part_sku = serializers.CharField(source='part.sku', read_only=True)

    class Meta:
        model = Reference
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
        existing = Reference.objects.filter(clave=references.clave(code), brand=brand, supplier__isnull=True)
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError('Este alterno ya está registrado para este SKU y marca.')
        return data

    def actor(self):
        user = getattr(self.context.get('request'), 'user', None)
        return user if getattr(user, 'is_authenticated', False) else None

    def create(self, validated_data):
        try:
            with transaction.atomic():
                part = Part.objects.select_for_update().get(pk=validated_data['part'].pk)
                if part.merged_into_id:
                    raise serializers.ValidationError({'part': 'Selecciona el SKU principal; este SKU ya fue unificado.'})
                validate_code_mapping(part=part, brand=validated_data.get('brand', ''), code=validated_data['code'])
                entry, added = references.add(part, validated_data['code'], validated_data.get('brand', ''), ref_type=validated_data.get('ref_type'),
                                              source=validated_data.get('reference_source', ''), actor=self.actor())
                if not added:
                    raise serializers.ValidationError('Este alterno ya está registrado para este SKU y marca.')
                return entry
        except IntegrityError:
            raise serializers.ValidationError('Este alterno ya está registrado.')

    def update(self, instance, validated_data):
        try:
            with transaction.atomic():
                part = Part.objects.select_for_update().get(pk=validated_data.get('part', instance.part).pk)
                if part.merged_into_id:
                    raise serializers.ValidationError({'part': 'Selecciona el SKU principal; este SKU ya fue unificado.'})
                instance = Reference.objects.select_for_update().get(pk=instance.pk)
                validate_code_mapping(part=part, brand=validated_data.get('brand', instance.brand), code=validated_data.get('code', instance.code))
                return references.update(instance, actor=self.actor(), **validated_data)
        except IntegrityError:
            raise serializers.ValidationError('Este alterno ya está registrado.')


class CatalogSearch(filters.SearchFilter):
    """The search fields, a live Reference of the line (its code or brand: a retired one finds nothing), or an OEM number the SKU
    reaches or an aftermarket code filed under one (mall.oem_links.search_q). Every term must match one of them."""

    def filter_queryset(self, request, queryset, view):
        from functools import reduce
        from operator import and_, or_
        from .oem_links import search_q
        terms = self.get_search_terms(request)
        if not terms:
            return queryset
        lookups = [self.construct_search(str(field), queryset) for field in self.get_search_fields(view, request) or []]
        matched = queryset.filter(reduce(and_, (reduce(or_, (Q(**{lookup: term}) for lookup in lookups),
                                                       Q(pk__in=Reference.objects.filter(Q(code__icontains=term) | Q(brand__icontains=term))
                                                         .values('part_id')))
                                                for term in terms)))
        library = search_q(' '.join(terms))
        # The line's own columns and pk__in subqueries (codes and the library alike) keep every row unique: no join, no distinct.
        return matched | queryset.filter(library) if library is not None else matched


def catalog_queryset(lines=None):
    """The Inventario grid's rows, the catalog's lines (mall.lines.catalog_lines: publicada and retirada) unless others are given,
    with their supplier stock rows counted."""
    return (catalog_lines() if lines is None else lines).annotate(stock_record_count=Count('supplier_items', distinct=True)).prefetch_related(
        'codes', 'images').order_by('sku', 'id')


def with_merged_counts(parts):
    """The SKUs grouped into each of these rows (merged_count), in one query for the page instead of a subquery per catalog row."""
    parts = list(parts)
    counts = dict(Part.objects.filter(merged_into__in=[part.pk for part in parts]).order_by().values('merged_into')
                  .annotate(total=Count('pk')).values_list('merged_into', 'total'))
    for part in parts:
        part.merged_count = counts.get(part.pk, 0)
    return parts


class CatalogList(SuperuserMixin, generics.ListCreateAPIView):
    serializer_class = ManagedPartSerializer
    filter_backends = [CatalogSearch, StableOrdering]
    search_fields = ['sku', 'name', 'description']  # and the line's live References' codes and brands (CatalogSearch)
    ordering_fields = ['sku', 'name', 'description', 'category', 'subcategory', 'active', 'is_OEM', 'stock_record_count']
    queryset = catalog_queryset()

    def paginate_queryset(self, queryset):
        page = super().paginate_queryset(queryset)
        return None if page is None else with_merged_counts(page)

    def get_queryset(self):
        queryset = super().get_queryset()
        ids = self.request.query_params.get('ids')
        if ids:  # the rows a grid shows, re-read to patch what changed elsewhere
            wanted = [value.strip() for value in ids.split(',') if value.strip()][:MAX_ROW_IDS]
            try:
                queryset = queryset.filter(pk__in=[uuid.UUID(value) for value in wanted])
            except ValueError:
                raise serializers.ValidationError({'ids': 'Usa identificadores de SKU separados por comas.'})
        value = self.request.query_params.get('is_OEM')
        if value is not None:
            if value.lower() not in ['true', 'false']:
                raise serializers.ValidationError({'is_OEM': 'Usa true o false.'})
            queryset = queryset.filter(is_OEM=value.lower() == 'true')
        return queryset


MAX_ROW_IDS = 100
BULK_FIELDS = ('description', 'category', 'subcategory', 'active')
MAX_BULK_ROWS = 500


class BulkEditRow(serializers.Serializer):
    id = serializers.UUIDField()
    description = serializers.CharField(required=False, allow_blank=True)
    category = serializers.CharField(required=False, allow_blank=True)
    subcategory = serializers.CharField(required=False, allow_blank=True)
    active = serializers.BooleanField(required=False)


class BulkEditRequest(serializers.Serializer):
    rows = BulkEditRow(many=True, help_text=f'Hasta {MAX_BULK_ROWS} SKU, cada uno con los campos que cambian.')


class BulkEditError(serializers.Serializer):
    id = serializers.UUIDField()
    sku = serializers.CharField()
    detail = serializers.CharField()


class BulkEditFailure(serializers.Serializer):
    detail = serializers.CharField()
    errors = BulkEditError(many=True)


class BulkEditResponse(serializers.Serializer):
    updated = ManagedPartSerializer(many=True)


def flat_error(detail):
    if isinstance(detail, dict):
        return ' '.join(flat_error(value) for value in detail.values())
    if isinstance(detail, (list, tuple)):
        return ' '.join(flat_error(value) for value in detail)
    return str(detail)


class CatalogBulkEdit(SuperuserMixin, APIView):
    """The Inventario grid's batch: plain field edits of several SKUs, all saved or none. Each one keeps the SKU's OEM flag, so
    the OEM preference never renames a SKU here, and a group / subgroup must be one of the catalog's (no new part type by a typo)."""

    @extend_schema(operation_id='v1_management_catalog_bulk_edit', request=BulkEditRequest, responses={
        200: BulkEditResponse, 400: BulkEditFailure},
        description='Guarda juntos los cambios de descripción, grupo / subgrupo y estado de varios SKU: si uno falla no se guarda ninguno y '
                    'la respuesta dice por qué.')
    def post(self, request):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from .category_suggestions import normalized, taxonomy
        from .technical_data import classification
        data = BulkEditRequest(data=request.data)
        data.is_valid(raise_exception=True)
        rows = data.validated_data['rows']
        if not rows or len(rows) > MAX_BULK_ROWS:
            raise serializers.ValidationError({'rows': f'Envía entre 1 y {MAX_BULK_ROWS} SKU.'})
        known = {(normalized(category), normalized(subcategory)): (category, subcategory) for category, subcategory in taxonomy()}
        parts = {part.pk: part for part in catalog_lines().filter(pk__in=[row['id'] for row in rows]).select_related('part_type__grupo')}  # the grid's lines
        elsewhere = {part.pk: part for part in catalog_lines(('referencia', 'preparacion', 'rechazada')).filter(
            pk__in=[row['id'] for row in rows if row['id'] not in parts])}
        errors, saved = [], []
        with transaction.atomic():
            for row in rows:
                part = parts.get(row['id'])
                fields = {key: row[key] for key in BULK_FIELDS if key in row}
                if part is None and row['id'] in elsewhere:  # a line off the catalog: referencia, preparación, rechazada
                    other = elsewhere[row['id']]
                    errors.append({'id': other.pk, 'sku': other.sku, 'detail': f'Está {phrase(other.estado)} y no forma parte del catálogo.'})
                    continue
                if part is None:
                    errors.append({'id': row['id'], 'sku': '', 'detail': 'El SKU ya no está en el catálogo o se agrupó en otro.'})
                    continue
                if 'category' in fields or 'subcategory' in fields:
                    current = classification(part)  # the line's subgrupo by its FK (S5)
                    pair = (fields.get('category', current[0]), fields.get('subcategory', current[1]))
                    if any(pair):
                        match = known.get((normalized(pair[0]), normalized(pair[1])))
                        if match is None:
                            errors.append({'id': part.pk, 'sku': part.sku, 'detail': f'{pair[0]} / {pair[1]} no es un grupo / subgrupo del catálogo.'})
                            continue
                        fields['category'], fields['subcategory'] = match
                serializer = ManagedPartSerializer(part, data={**fields, 'is_OEM': part.is_OEM}, partial=True, context={'request': request})
                if not serializer.is_valid():
                    errors.append({'id': part.pk, 'sku': part.sku, 'detail': flat_error(serializer.errors)})
                    continue
                try:
                    with transaction.atomic():
                        saved.append(serializer.save())
                except (DjangoValidationError, serializers.ValidationError) as error:
                    errors.append({'id': part.pk, 'sku': part.sku, 'detail': flat_error(getattr(error, 'detail', None) or error.messages)})
            if errors:
                transaction.set_rollback(True)
        if errors:
            return Response({'detail': 'No se guardó ningún cambio: ' + ' '.join(f"{e['sku'] or 'SKU'}: {e['detail']}" for e in errors),
                             'errors': errors}, status=400)
        rows = with_merged_counts(catalog_queryset().filter(pk__in=[part.pk for part in saved]))  # as the grid reads them, counts included
        return Response({'updated': ManagedPartSerializer(rows, many=True, context={'request': request}).data})


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
    """One line in any estado (mall.lines.detail_line), so the item editors open the line an item sits on even in preparación; a
    unificada line is read only."""
    serializer_class = ManagedPartSerializer
    queryset = catalog_queryset(all_lines())
    http_method_names = ['get', 'patch', 'head', 'options']

    def get_object(self):
        line = super().get_object()
        return refuse_unificada(line) if self.request.method == 'PATCH' else line


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
    queryset = Reference.objects.select_related('part').order_by('part__sku', 'code', 'brand', 'id')


class AlternateDetail(SuperuserMixin, generics.RetrieveUpdateDestroyAPIView):
    """DELETE retires the alterno (mall.references.retire): it leaves the line's codes and stays as history."""
    serializer_class = ManagedAlternateSerializer
    queryset = AlternateList.queryset
    http_method_names = ['get', 'patch', 'delete', 'head', 'options']

    def perform_destroy(self, instance):
        with transaction.atomic():
            Part.objects.select_for_update().filter(pk=instance.part_id).first()
            references.retire(instance, actor=self.request.user, reason='Retirada en Alternos')


class AccountList(SuperuserMixin, generics.ListCreateAPIView):
    serializer_class = ManagedAccountSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['name']
    queryset = Account.objects.annotate(member_count=Count('memberships')).select_related('apiag_connection__updated_by').prefetch_related('roles', 'memberships__user').order_by('name', 'id')


class AccountDetail(SuperuserMixin, generics.RetrieveUpdateAPIView):
    serializer_class = ManagedAccountSerializer
    queryset = AccountList.queryset
    http_method_names = ['get', 'patch', 'head', 'options']


class UserList(SuperuserMixin, generics.ListAPIView):
    serializer_class = ManagedUserSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['username', 'email', 'first_name', 'last_name']
    queryset = User.objects.prefetch_related('membership_set__account').order_by('username', 'id')


class ManagedUserDetailSerializer(ManagedUserSerializer):
    """One user, with how they get notices (read only: each channel is the person's own choice, and WhatsApp needs their own consent)."""
    notices = serializers.SerializerMethodField()

    class Meta(ManagedUserSerializer.Meta):
        fields = ManagedUserSerializer.Meta.fields + ['notices']
        read_only_fields = ManagedUserSerializer.Meta.read_only_fields + ['notices']

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_notices(self, user):
        from .email_notifications import email_enabled
        from .inbox_models import InboxCursor, PushSubscription
        from .push import push_enabled
        from .whatsapp import whatsapp_enabled
        cursor = InboxCursor.objects.filter(user=user).first()
        opted_in = cursor.whatsapp_opt_in_at if cursor else None
        return {
            'email': {'available': email_enabled(), 'address': user.email, 'messages': cursor.email_messages if cursor else True,
                      'updates': cursor.email_updates if cursor else True},
            'whatsapp': {'available': whatsapp_enabled(), 'number': cursor.whatsapp_number if cursor else '',
                         'opted_in_at': opted_in.isoformat() if opted_in else None,
                         'verified': bool(cursor and cursor.whatsapp_verified_at),
                         'messages': cursor.whatsapp_messages if cursor else True, 'updates': cursor.whatsapp_updates if cursor else True},
            'push': {'available': push_enabled(), 'devices': PushSubscription.objects.filter(user=user).count()},
            'last_seen_at': cursor.active_at.isoformat() if cursor and cursor.active_at else None,
        }


class UserDetail(SuperuserMixin, generics.RetrieveUpdateAPIView):
    serializer_class = ManagedUserDetailSerializer
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
