"""Supplier-only pricing endpoints. Nothing here feeds catalog, offers, request or deal payloads, and nothing is reachable by
Oratek staff (no admin, no management endpoint): only members of the supplier account read or write these rows."""
import hashlib
import io
import json
import re
import uuid
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, F, Max, OuterRef, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.utils import get_column_letter
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Account, SupplierItem
from .pricing_engine import ENGINE_VERSION, client_profile, price_items
from .pricing_models import (CURRENCY_CHOICES, MAX_ACTIVE_RULES, OWNER_ONLY_SETTINGS, PERMISSION_CHOICES, POLICY_CHOICES, PREFILL_QUANTITY_CHOICES,
                             PRICE_CHANGE_KINDS, PRICE_LIST_CODE, PRICE_WRITE_SOURCES, RULE_KINDS, RULE_SCOPES, RULE_TARGETS, SHORTFALL_POLICY_CHOICES,
                             ClientPricingProfile, PriceChange, PriceList, PriceListEntry, PricingAuditEvent, PricingRule, SupplierItemPricing, choice_values,
                             pricing_settings, record_pricing_event)
from .pricing_services import FOREIGN_ITEM, PriceConflict, set_prices
from .request_models import SupplierRequest
from .request_views import RequestConflict, WholeRequestQuantity
from .views import PERMISSION_RANK, membership_for

SETTINGS_FIELDS = ('default_currency', 'usd_pab_parity', 'config_min_permission', 'publish_min_permission', 'over_request_policy',
                   'over_stock_policy', 'accept_shortfall_policy', 'prefill_quantity', 'assistant_enabled')


class PricingSettingsFields(serializers.Serializer):
    default_currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES))
    usd_pab_parity = serializers.BooleanField()
    config_min_permission = serializers.ChoiceField(choices=PERMISSION_CHOICES)
    publish_min_permission = serializers.ChoiceField(choices=PERMISSION_CHOICES)
    over_request_policy = serializers.ChoiceField(choices=choice_values(POLICY_CHOICES))
    over_stock_policy = serializers.ChoiceField(choices=choice_values(POLICY_CHOICES))
    accept_shortfall_policy = serializers.ChoiceField(choices=choice_values(SHORTFALL_POLICY_CHOICES))
    prefill_quantity = serializers.ChoiceField(choices=choice_values(PREFILL_QUANTITY_CHOICES))
    assistant_enabled = serializers.BooleanField()


class MemberName(serializers.Serializer):
    name = serializers.CharField()


class PricingSettingsSerializer(PricingSettingsFields):
    version = serializers.IntegerField()
    updated_at = serializers.DateTimeField(allow_null=True)
    updated_by = MemberName(allow_null=True)
    permission = serializers.ChoiceField(choices=PERMISSION_CHOICES, help_text='Tu permiso en la cuenta.')
    can_configure = serializers.BooleanField()
    can_manage_permissions = serializers.BooleanField()


class PricingSettingsUpdate(PricingSettingsFields):
    expected_version = serializers.IntegerField(min_value=1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in SETTINGS_FIELDS:
            self.fields[field].required = False


class PricingSettingsConflict(serializers.Serializer):
    detail = serializers.CharField()
    settings = PricingSettingsSerializer()


def settings_data(row, membership):
    rank = PERMISSION_RANK.get(membership.permission, -1)
    user = row.updated_by
    return PricingSettingsSerializer({**{field: getattr(row, field) for field in SETTINGS_FIELDS}, 'version': row.version,
        'updated_at': row.updated_at, 'updated_by': {'name': user.get_full_name() or user.username} if user else None,
        'permission': membership.permission, 'can_configure': rank >= PERMISSION_RANK[row.config_min_permission],
        'can_manage_permissions': membership.permission == 'owner'}).data


class PricingSettingsView(APIView):
    @extend_schema(operation_id='v1_accounts_pricing_settings_retrieve', responses=PricingSettingsSerializer,
                   description='Configuración privada de precios del proveedor. Leerla no crea registros.')
    def get(self, request, account_id):
        account, membership = membership_for(request.user, account_id, 'supplier')
        return Response(settings_data(pricing_settings(account), membership))

    @extend_schema(operation_id='v1_accounts_pricing_settings_update', request=PricingSettingsUpdate,
                   responses={200: PricingSettingsSerializer, 409: PricingSettingsConflict},
                   description='Actualización parcial con expected_version. Los permisos y el asistente de IA solo los cambia el propietario.')
    @transaction.atomic
    def post(self, request, account_id):
        account, membership = membership_for(request.user, account_id, 'supplier')
        row = pricing_settings(account, lock=True)
        if PERMISSION_RANK.get(membership.permission, -1) < PERMISSION_RANK[row.config_min_permission]:
            raise PermissionDenied('Tu permiso en la cuenta no permite cambiar esta configuración. Pide a un administrador de tu cuenta que la cambie.')
        serializer = PricingSettingsUpdate(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        changes = {field: data[field] for field in SETTINGS_FIELDS if field in data and getattr(row, field) != data[field]}
        if membership.permission != 'owner' and set(changes) & set(OWNER_ONLY_SETTINGS):
            raise PermissionDenied('Solo el propietario de la cuenta puede cambiar quién configura precios, quién envía cotizaciones y el asistente de IA.')
        # Values already in place are a no-op, so a retry after a lost response never conflicts.
        if changes and row.version != data['expected_version']:
            return Response({'detail': 'Otro miembro de tu equipo cambió esta configuración. Revisa los valores actuales.',
                             'settings': settings_data(row, membership)}, status=409)
        if changes:
            old = {field: getattr(row, field) for field in changes}
            for field, value in changes.items():
                setattr(row, field, value)
            row.version += 1
            row.updated_by = request.user
            row.save()
            record_pricing_event(account, request.user, 'settings_changed', payload={'version': row.version, 'old': old, 'new': changes})
        return Response(settings_data(row, membership))


# ---------------------------------------------------------------------------------------------------------------------------
# Price lists, manual price editing, history and export (S3).

CONFIG_DENIED = 'Tu permiso en la cuenta no permite cambiar precios. Pide a un administrador de tu cuenta que lo haga.'
XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
# PRECIO_MINIMO and PRECIO_PISO are the floor-price columns of the import and export files, so no list may own those headers.
RESERVED_LIST_CODES = ('MINIMO', 'PISO')


def can_configure(account, membership, settings_row=None):
    settings_row = settings_row or pricing_settings(account)
    return PERMISSION_RANK.get(membership.permission, -1) >= PERMISSION_RANK[settings_row.config_min_permission]


def configurer(user, account_id):
    """Members at or above config_min_permission (staff by default) may write prices and lists."""
    account, membership = membership_for(user, account_id, 'supplier')
    if not can_configure(account, membership):
        raise PermissionDenied(CONFIG_DENIED)
    return account, membership


def user_name(user):
    return {'name': user.get_full_name() or user.username} if user else None


class PricePagination(PageNumberPagination):
    page_size = 100


def page_serializer(name, item):
    return type(name, (serializers.Serializer,), {'count': serializers.IntegerField(), 'next': serializers.CharField(allow_null=True),
                                                  'previous': serializers.CharField(allow_null=True), 'results': item(many=True)})


class PriceListSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()
    currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES))
    is_default = serializers.BooleanField()
    active = serializers.BooleanField()
    version = serializers.IntegerField()
    priced_count = serializers.IntegerField(help_text='Artículos con precio en esta lista.')
    missing_count = serializers.IntegerField(help_text='Artículos de tu inventario sin precio en esta lista.')
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class PriceListsEnvelope(serializers.Serializer):
    can_configure = serializers.BooleanField()
    item_count = serializers.IntegerField()
    results = PriceListSerializer(many=True)


class PriceListFields(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES))
    is_default = serializers.BooleanField()

    def validate_name(self, value):
        value = value.strip().upper()
        if not value:
            raise serializers.ValidationError('Escribe el nombre de la lista.')
        return value


class PriceListCreate(PriceListFields):
    code = serializers.CharField(max_length=30)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['currency'].required = self.fields['is_default'].required = False

    def validate_code(self, value):
        value = value.strip().upper()
        if not re.fullmatch(PRICE_LIST_CODE, value):
            raise serializers.ValidationError('Usa de 1 a 30 letras sin tilde, números o guiones bajos (A-Z, 0-9, _).')
        if value in RESERVED_LIST_CODES:
            raise serializers.ValidationError(f'El código {value} está reservado para el precio mínimo (PRECIO_{value}); elige otro.')
        return value


class PriceListUpdate(PriceListFields):
    expected_version = serializers.IntegerField(min_value=1)
    active = serializers.BooleanField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in ('name', 'currency', 'is_default', 'active'):
            self.fields[field].required = False


class PriceListConflict(serializers.Serializer):
    detail = serializers.CharField()
    price_list = PriceListSerializer()


def price_list_rows(account, lists=None):
    rows = lists if lists is not None else PriceList.objects.filter(supplier=account)
    items = SupplierItem.objects.filter(supplier=account).count()
    return [{**{field: getattr(row, field) for field in ('id', 'code', 'name', 'currency', 'is_default', 'active', 'version', 'created_at', 'updated_at')},
             'priced_count': row.priced, 'missing_count': max(0, items - row.priced)}
            for row in rows.annotate(priced=Count('entries')).order_by('-is_default', 'code')], items


def demote(account, keep=None):
    # The previous default changes too: its version moves, so an edit based on what it was before is refused (409), not applied.
    PriceList.objects.filter(supplier=account, is_default=True).exclude(pk=getattr(keep, 'pk', None)).update(
        is_default=False, version=F('version') + 1, updated_at=timezone.now())


def price_list_data(row):
    return PriceListSerializer(price_list_rows(row.supplier, PriceList.objects.filter(pk=row.pk))[0][0]).data


class PriceListsView(APIView):
    @extend_schema(operation_id='v1_accounts_price_lists_list', responses=PriceListsEnvelope,
                   description='Listas de precios privadas del proveedor, con artículos con y sin precio.')
    def get(self, request, account_id):
        account, membership = membership_for(request.user, account_id, 'supplier')
        rows, items = price_list_rows(account)
        return Response(PriceListsEnvelope({'can_configure': can_configure(account, membership), 'item_count': items, 'results': rows}).data)

    @extend_schema(operation_id='v1_accounts_price_lists_create', request=PriceListCreate, responses={200: PriceListSerializer, 201: PriceListSerializer},
                   description='Crea una lista. La primera lista del proveedor queda como predeterminada. Repetir la misma creación devuelve la lista.')
    @transaction.atomic
    def post(self, request, account_id):
        account, _ = configurer(request.user, account_id)
        serializer = PriceListCreate(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        Account.objects.select_for_update().get(pk=account.pk)
        currency = data.get('currency') or pricing_settings(account).default_currency
        existing = PriceList.objects.filter(supplier=account, code=data['code']).first()
        if existing:
            # A retry after a lost response finds the list it created; any other reuse of the code is refused.
            if existing.name == data['name'] and existing.currency == currency:
                return Response(price_list_data(existing))
            raise ValidationError({'code': [f'Ya tienes una lista con el código {data["code"]}.']})
        first = not PriceList.objects.filter(supplier=account).exists()
        default = first or data.get('is_default', False)
        if default:
            demote(account)
        try:
            with transaction.atomic():
                row = PriceList.objects.create(supplier=account, code=data['code'], name=data['name'], currency=currency, is_default=default, created_by=request.user)
        except IntegrityError:
            raise ValidationError('La lista no cumple las reglas de listas de precios. Actualiza la página e inténtalo de nuevo.')
        record_pricing_event(account, request.user, 'price_list_changed', object_id=str(row.pk), payload={
            'action': 'created', 'code': row.code, 'new': {'name': row.name, 'currency': row.currency, 'is_default': row.is_default}})
        return Response(price_list_data(row), status=201)


class PriceListDetail(APIView):
    @extend_schema(operation_id='v1_accounts_price_lists_update', request=PriceListUpdate, responses={200: PriceListSerializer, 409: PriceListConflict},
                   description='Cambia nombre, moneda (solo sin precios), predeterminada o activa, con expected_version. Valores iguales no cambian nada.')
    @transaction.atomic
    def post(self, request, account_id, pk):
        account, _ = configurer(request.user, account_id)
        serializer = PriceListUpdate(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        Account.objects.select_for_update().get(pk=account.pk)
        row = get_object_or_404(PriceList.objects.select_for_update().filter(supplier=account), pk=pk)
        changes = {field: data[field] for field in ('name', 'currency', 'is_default', 'active') if field in data and getattr(row, field) != data[field]}
        if not changes:
            return Response(price_list_data(row))
        if row.version != data['expected_version']:
            return Response({'detail': 'Otro usuario actualizó esta lista. Revisa sus valores actuales.', 'price_list': price_list_data(row)}, status=409)
        active, default = changes.get('active', row.active), changes.get('is_default', row.is_default)
        if 'currency' in changes and row.entries.exists():
            raise ValidationError({'currency': ['No puedes cambiar la moneda de una lista que ya tiene precios.']})
        if row.is_default and changes.get('is_default') is False:
            raise ValidationError({'is_default': ['Marca otra lista como predeterminada; siempre debe haber una.']})
        if default and not active:
            raise ValidationError({'active': ['Elige otra lista predeterminada antes de archivar esta.' if row.is_default
                                              else 'Activa la lista antes de marcarla como predeterminada.']})
        if changes.get('is_default'):
            demote(account, row)
        old = {field: getattr(row, field) for field in changes}
        for field, value in changes.items():
            setattr(row, field, value)
        row.version += 1
        try:
            with transaction.atomic():
                row.save()
        except IntegrityError:
            raise ValidationError('La lista no cumple las reglas de listas de precios. Actualiza la página e inténtalo de nuevo.')
        record_pricing_event(account, request.user, 'price_list_changed', object_id=str(row.pk), payload={'action': 'updated', 'code': row.code,
                                                                                                           'old': old, 'new': changes})
        return Response(price_list_data(row))


class PriceCell(serializers.Serializer):
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    revision = serializers.IntegerField()
    updated_at = serializers.DateTimeField()


class PriceRow(serializers.Serializer):
    supplier_item_id = serializers.UUIDField()
    supplier_invent_id = serializers.CharField()
    codigo = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    description = serializers.CharField(allow_blank=True)
    matching_status = serializers.ChoiceField(choices=SupplierItem._meta.get_field('matching_status').choices)
    discount_group = serializers.CharField(allow_blank=True, help_text='LINEA propia del proveedor.')
    floor_price = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    item_pricing_revision = serializers.IntegerField(allow_null=True)
    prices = serializers.DictField(child=PriceCell(), help_text='Precio por código de lista; una lista sin precio no aparece.')
    updated_at = serializers.DateTimeField(allow_null=True, help_text='Último cambio de precio, mínimo o línea.')


PricePage = page_serializer('PricePage', PriceRow)


class PriceFilters(serializers.Serializer):
    list = serializers.UUIDField(required=False)
    search = serializers.CharField(required=False, allow_blank=True, max_length=200)
    status = serializers.ChoiceField(required=False, choices=['priced', 'missing', 'below_floor'])


MONEY_ERRORS = {'invalid': 'Escribe un importe válido, por ejemplo 12.50.', 'max_decimal_places': 'Usa un máximo de dos decimales.',
                'max_digits': 'Usa como máximo 10 dígitos enteros.', 'max_whole_digits': 'Usa como máximo 10 dígitos enteros.'}


class PriceChangeInput(serializers.Serializer):
    list_id = serializers.UUIDField()
    supplier_item_id = serializers.UUIDField()
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'), allow_null=True, help_text='null elimina el precio.',
                                          error_messages={**MONEY_ERRORS, 'min_value': 'El precio de lista debe ser mayor que 0.'})
    expected_revision = serializers.IntegerField(min_value=1, allow_null=True, help_text='La revisión que viste; null si no tenía precio.')


class ItemPricingInput(serializers.Serializer):
    supplier_item_id = serializers.UUIDField()
    discount_group = serializers.CharField(max_length=60, allow_blank=True, required=False)
    floor_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0'), allow_null=True, required=False,
                                           error_messages={**MONEY_ERRORS, 'min_value': 'El precio mínimo no puede ser negativo.'})
    expected_revision = serializers.IntegerField(min_value=1, allow_null=True)

    def validate_discount_group(self, value):
        return value.strip().upper()

    def validate(self, data):
        if 'discount_group' not in data and 'floor_price' not in data:
            raise serializers.ValidationError('Indica la línea o el precio mínimo.')
        return data


class PriceWrite(serializers.Serializer):
    changes = PriceChangeInput(many=True, required=False, max_length=500)
    items = ItemPricingInput(many=True, required=False, max_length=500)

    def validate(self, data):
        changes, items = data.get('changes', []), data.get('items', [])
        if not changes and not items:
            raise serializers.ValidationError('No hay cambios de precio para guardar.')
        if len({(row['list_id'], row['supplier_item_id']) for row in changes}) != len(changes) or len({row['supplier_item_id'] for row in items}) != len(items):
            raise serializers.ValidationError('Incluye cada precio una sola vez en cada guardado.')
        return data


class PriceWriteSummary(serializers.Serializer):
    created = serializers.IntegerField()
    updated = serializers.IntegerField()
    removed = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    floor_updates = serializers.IntegerField()
    line_updates = serializers.IntegerField()


class PriceWriteResult(serializers.Serializer):
    summary = PriceWriteSummary()
    rows = PriceRow(many=True, help_text='Valores actuales de los artículos incluidos.')


class PriceConflictItem(serializers.Serializer):
    supplier_item_id = serializers.UUIDField()
    list_id = serializers.UUIDField(allow_null=True, help_text='null para línea o precio mínimo.')
    current = serializers.DictField()


class PriceWriteConflict(serializers.Serializer):
    detail = serializers.CharField()
    conflicts = PriceConflictItem(many=True)
    rows = PriceRow(many=True)


def item_pricing(item):
    try:
        return item.pricing
    except SupplierItem.pricing.RelatedObjectDoesNotExist:
        return None


def price_rows(account, items):
    """Grid rows for these items: every list's price keyed by code, plus LINEA, floor and their revisions."""
    items = list(items)
    codes = dict(PriceList.objects.filter(supplier=account).values_list('pk', 'code'))
    prices = {}
    for entry in PriceListEntry.objects.filter(price_list_id__in=codes, item__in=items):
        prices.setdefault(entry.item_id, {})[codes[entry.price_list_id]] = entry
    rows = []
    for item in items:
        pricing, cells = item_pricing(item), prices.get(item.pk, {})
        stamps = [entry.updated_at for entry in cells.values()] + ([pricing.updated_at] if pricing else [])
        rows.append({'supplier_item_id': item.pk, 'supplier_invent_id': item.supplier_invent_id, 'codigo': item.codigo, 'brand': item.brand,
                     'description': item.description, 'matching_status': item.matching_status, 'discount_group': pricing.discount_group if pricing else '',
                     'floor_price': pricing.floor_price if pricing else None, 'item_pricing_revision': pricing.revision if pricing else None,
                     'prices': {code: {'unit_price': entry.unit_price, 'revision': entry.revision, 'updated_at': entry.updated_at} for code, entry in cells.items()},
                     'updated_at': max(stamps) if stamps else None})
    return PriceRow(rows, many=True).data


def supplier_items(account):
    return SupplierItem.objects.filter(supplier=account).select_related('pricing').order_by('supplier_invent_id', 'id')


class PricesView(APIView):
    @extend_schema(operation_id='v1_accounts_prices_list', responses=PricePage, parameters=[
        OpenApiParameter('list', OpenApiTypes.UUID, description='Lista para filtrar por estado (predeterminada si se omite).'),
        OpenApiParameter('search', OpenApiTypes.STR), OpenApiParameter('status', OpenApiTypes.STR, enum=['priced', 'missing', 'below_floor']),
        OpenApiParameter('page', OpenApiTypes.INT)], description='Una fila por artículo de tu inventario, vinculado o no, con sus precios privados.')
    def get(self, request, account_id):
        account, _ = membership_for(request.user, account_id, 'supplier')
        filters = PriceFilters(data=request.query_params)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data
        rows = supplier_items(account)
        if search := data.get('search', '').strip():
            rows = rows.filter(Q(supplier_invent_id__icontains=search) | Q(codigo__icontains=search) | Q(brand__icontains=search)
                               | Q(description__icontains=search) | Q(pricing__discount_group__icontains=search))
        target = get_object_or_404(PriceList.objects.filter(supplier=account), pk=data['list']) if 'list' in data else \
            PriceList.objects.filter(supplier=account, is_default=True).first()
        if data.get('status'):
            entries = PriceListEntry.objects.filter(price_list=target, item=OuterRef('pk'))
            rows = rows.filter(Exists(entries)) if data['status'] == 'priced' else rows.exclude(Exists(entries)) if data['status'] == 'missing' \
                else rows.filter(Exists(entries.filter(unit_price__lt=OuterRef('pricing__floor_price'))))
        paginator = PricePagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(price_rows(account, page))

    @extend_schema(operation_id='v1_accounts_prices_update', request=PriceWrite, responses={200: PriceWriteResult, 409: PriceWriteConflict},
                   description='Edición masiva con revisión esperada, todo o nada. Una fila que ya tiene el valor pedido no cambia (reintento seguro).')
    def post(self, request, account_id):
        account, _ = configurer(request.user, account_id)
        serializer = PriceWrite(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = set_prices(account, request.user, changes=data.get('changes', []), items=data.get('items', []), source='manual',
                                reference=f'op:{uuid.uuid4()}')
        except PriceConflict as conflict:
            touched = {row['supplier_item_id'] for row in conflict.conflicts}
            return Response({'detail': 'Otro usuario cambió algunos precios.', 'conflicts': conflict.conflicts,
                             'rows': price_rows(account, supplier_items(account).filter(pk__in=touched))}, status=409)
        return Response({'summary': result['summary'], 'rows': price_rows(account, supplier_items(account).filter(pk__in=result['item_ids']))})


class PriceHistoryEntry(serializers.Serializer):
    id = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=choice_values(PRICE_CHANGE_KINDS))
    price_list = serializers.DictField(allow_null=True, help_text='{id, code} de la lista; null para línea o precio mínimo.')
    old_value = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    new_value = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    old_text = serializers.CharField(allow_blank=True)
    new_text = serializers.CharField(allow_blank=True)
    source = serializers.ChoiceField(choices=choice_values(PRICE_WRITE_SOURCES))
    reference = serializers.CharField(allow_blank=True)
    actor = MemberName()
    created_at = serializers.DateTimeField()


PriceHistoryPage = page_serializer('PriceHistoryPage', PriceHistoryEntry)


class PriceHistory(APIView):
    @extend_schema(operation_id='v1_accounts_prices_history_list', responses=PriceHistoryPage, parameters=[OpenApiParameter('page', OpenApiTypes.INT)],
                   description='Historial de precios, mínimo y línea de un artículo, del más reciente al más antiguo.')
    def get(self, request, account_id, item_id):
        account, _ = membership_for(request.user, account_id, 'supplier')
        item = get_object_or_404(SupplierItem.objects.filter(supplier=account), pk=item_id)
        rows = PriceChange.objects.filter(supplier=account, item=item).select_related('price_list', 'actor')
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(PriceHistoryEntry([{
            **{field: getattr(row, field) for field in ('id', 'kind', 'old_value', 'new_value', 'old_text', 'new_text', 'source', 'reference', 'created_at')},
            'price_list': {'id': str(row.price_list_id), 'code': row.price_list.code} if row.price_list_id else None,
            'actor': user_name(row.actor)} for row in page], many=True).data)


class PricingAuditEntry(serializers.Serializer):
    id = serializers.IntegerField()
    kind = serializers.CharField()
    actor = MemberName()
    client = serializers.DictField(allow_null=True, help_text='{id, name} del cliente, si aplica.')
    order = serializers.DictField(allow_null=True, help_text='{id, reference} de la orden, si aplica.')
    object_id = serializers.CharField(allow_blank=True)
    payload = serializers.DictField()
    created_at = serializers.DateTimeField()


PricingAuditPage = page_serializer('PricingAuditPage', PricingAuditEntry)


class PricingHistory(APIView):
    @extend_schema(operation_id='v1_accounts_pricing_history_list', responses=PricingAuditPage,
                   parameters=[OpenApiParameter('search', OpenApiTypes.STR), OpenApiParameter('page', OpenApiTypes.INT)],
                   description='Registro privado de cambios de precios, listas, borradores y cotizaciones de tu cuenta.')
    def get(self, request, account_id):
        account, _ = membership_for(request.user, account_id, 'supplier')
        search = str(request.query_params.get('search', '')).strip()[:200]
        rows = PricingAuditEvent.objects.filter(supplier=account).select_related('actor', 'client', 'order')
        if search:
            rows = rows.filter(Q(kind__icontains=search) | Q(object_id__icontains=search) | Q(order__reference__icontains=search)
                               | Q(client__name__icontains=search) | Q(actor__username__icontains=search))
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(PricingAuditEntry([{
            'id': row.pk, 'kind': row.kind, 'actor': user_name(row.actor), 'object_id': row.object_id, 'payload': row.payload, 'created_at': row.created_at,
            'client': {'id': str(row.client_id), 'name': row.client.name} if row.client_id else None,
            'order': {'id': str(row.order_id), 'reference': row.order.reference} if row.order_id else None} for row in page], many=True).data)


class PricesExport(APIView):
    @extend_schema(operation_id='v1_accounts_prices_export', responses={(200, XLSX_TYPE): bytes},
                   parameters=[OpenApiParameter('list', OpenApiTypes.UUID, description='Solo esta lista; sin ella, todas las listas activas.')],
                   description='Tus precios en el formato de importación: PRECIO es la lista predeterminada y PRECIO_<CÓDIGO> las demás.')
    def get(self, request, account_id):
        account, _ = membership_for(request.user, account_id, 'supplier')
        filters = PriceFilters(data=request.query_params)
        filters.is_valid(raise_exception=True)
        lists = PriceList.objects.filter(supplier=account)
        lists = list(lists.filter(pk=filters.validated_data['list']) if 'list' in filters.validated_data else lists.filter(active=True))
        if 'list' in filters.validated_data and not lists:
            raise ValidationError({'list': ['La lista de precios no existe.']})
        book = Workbook(write_only=True)
        sheet = book.create_sheet('PRECIOS')
        sheet.freeze_panes = 'A2'
        for index, width in enumerate([30, 26, 22, 60, 20, 16] + [16] * len(lists)):
            sheet.column_dimensions[get_column_letter(index + 1)].width = width
        sheet.append(['ID_INVENTARIO_PROVEEDOR', 'CODIGO', 'MARCA', 'DESCRIPCION', 'LINEA', 'PRECIO_MINIMO']
                     + ['PRECIO' if row.is_default else f'PRECIO_{row.code}' for row in lists])
        def cell(value, money=False):
            # Identifiers stay text (initial zeros, no formula); prices stay numbers with two decimals.
            result = WriteOnlyCell(sheet, value=value)
            result.number_format = '0.00' if money else '@'
            if not money and value is not None:
                result.data_type = 's'
            return result
        items = SupplierItem.objects.filter(supplier=account).select_related('pricing').order_by('supplier_invent_id', 'id')
        for offset in range(0, items.count(), 2000):
            chunk = list(items[offset:offset + 2000])
            prices = {}
            for entry in PriceListEntry.objects.filter(price_list__in=lists, item__in=chunk).values_list('item_id', 'price_list_id', 'unit_price'):
                prices[entry[:2]] = entry[2]
            for item in chunk:
                pricing = item_pricing(item)
                sheet.append([cell(item.supplier_invent_id), cell(item.codigo), cell(item.brand), cell(item.description),
                              cell(pricing.discount_group if pricing and pricing.discount_group else None),
                              cell(pricing.floor_price if pricing else None, money=True)]
                             + [cell(prices.get((item.pk, row.pk)), money=True) for row in lists])
        output = io.BytesIO()
        book.save(output)
        response = HttpResponse(output.getvalue(), content_type=XLSX_TYPE)
        response['Content-Disposition'] = 'attachment; filename="precios-proveedor.xlsx"'
        response['Cache-Control'] = 'private, no-store'
        return response


# ---------------------------------------------------------------------------------------------------------------------------
# Client pricing profiles, commercial rules and the simulator (S5). Pair privacy: a supplier only ever names clients that ordered
# from it; any other account is 404, so the platform's accounts can never be enumerated through these endpoints.

PROFILE_FIELDS = ('price_list_id', 'fallback_to_default', 'discount_percent', 'preferred_currency', 'default_terms', 'internal_notes', 'customer_code')
PROFILE_DEFAULTS = {'price_list_id': None, 'fallback_to_default': True, 'discount_percent': Decimal('0.00'), 'preferred_currency': '', 'default_terms': '',
                    'internal_notes': '', 'customer_code': ''}
RULE_INPUTS = ('name', 'scope', 'client_id', 'target', 'target_value', 'item_id', 'kind', 'value', 'currency', 'min_quantity', 'valid_from', 'valid_until',
               'note', 'active')
RULE_VALIDITY = ['active', 'scheduled', 'expired', 'archived']
REUSED_OPERATION = 'Este identificador ya se usó para otro cambio. Actualiza la página e inténtalo de nuevo.'


def plain(values):
    """Audit payloads stay JSON: money and percentages as strings with two decimals, ids and dates as text."""
    return {key: f'{value:.2f}' if isinstance(value, Decimal) else value.isoformat() if hasattr(value, 'isoformat') else str(value) if isinstance(value, uuid.UUID)
            else value for key, value in values.items()}


def payload_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def related_clients(account):
    """Accounts with at least one order to this supplier, never the supplier itself."""
    return Account.objects.filter(pk__in=SupplierRequest.objects.filter(supplier=account).values('client_id')).exclude(pk=account.pk)


def related_client(account, client_id):
    return get_object_or_404(related_clients(account), pk=client_id)


def list_ref(row):
    return {'id': row.pk, 'code': row.code, 'name': row.name, 'currency': row.currency, 'active': row.active} if row else None


class PriceListRef(serializers.Serializer):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()
    currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES))
    active = serializers.BooleanField()


class AccountRef(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class ClientProfileSummary(serializers.Serializer):
    exists = serializers.BooleanField()
    version = serializers.IntegerField()
    customer_code = serializers.CharField(allow_blank=True)
    price_list = PriceListRef(allow_null=True, help_text='Lista asignada; null usa tu lista predeterminada.')
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2)
    preferred_currency = serializers.CharField(allow_blank=True)
    rule_count = serializers.IntegerField(help_text='Reglas activas propias de este cliente.')


class PricingClientRow(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    order_count = serializers.IntegerField()
    last_order_at = serializers.DateTimeField()
    profile = ClientProfileSummary()


class PricingClientPage(page_serializer('PricingClientPageBase', PricingClientRow)):
    default_list = PriceListRef(allow_null=True, help_text='Tu lista predeterminada, la de los clientes sin lista asignada.')


def profile_summary(profile):
    return {'exists': profile is not None, 'version': profile.version if profile else 0, 'customer_code': profile.customer_code if profile else '',
            'price_list': list_ref(profile.price_list) if profile else None, 'discount_percent': profile.discount_percent if profile else Decimal('0'),
            'preferred_currency': profile.preferred_currency if profile else '', 'rule_count': getattr(profile, 'rule_count', 0) if profile else 0}


def client_orders(account):
    orders = Q(sent_supplier_requests__supplier=account)
    return related_clients(account).annotate(order_count=Count('sent_supplier_requests', filter=orders),
                                             last_order_at=Max('sent_supplier_requests__created_at', filter=orders))


class PricingClients(APIView):
    @extend_schema(operation_id='v1_accounts_pricing_clients_list', responses=PricingClientPage,
                   parameters=[OpenApiParameter('search', OpenApiTypes.STR), OpenApiParameter('page', OpenApiTypes.INT)],
                   description='Clientes que te han enviado al menos una orden, con el resumen de su perfil comercial privado.')
    def get(self, request, account_id):
        account, _ = membership_for(request.user, account_id, 'supplier')
        search = str(request.query_params.get('search', '')).strip()[:200]
        rows = client_orders(account).order_by('-last_order_at', 'name', 'id')
        if search:
            rows = rows.filter(Q(name__icontains=search) | Exists(ClientPricingProfile.objects.filter(supplier=account, client=OuterRef('pk'), customer_code__icontains=search)))
        paginator = PricePagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        profiles = {profile.client_id: profile for profile in ClientPricingProfile.objects.filter(supplier=account, client__in=page).select_related('price_list')
                    .annotate(rule_count=Count('rules', filter=Q(rules__active=True)))}
        response = paginator.get_paginated_response(PricingClientRow([{'id': row.pk, 'name': row.name, 'order_count': row.order_count, 'last_order_at': row.last_order_at,
                                                                         'profile': profile_summary(profiles.get(row.pk))} for row in page], many=True).data)
        default = list_ref(PriceList.objects.filter(supplier=account, is_default=True).first())
        response.data['default_list'] = PriceListRef(default).data if default else None
        return response


class ClientProfileSerializer(serializers.Serializer):
    client = AccountRef()
    exists = serializers.BooleanField(help_text='False hasta el primer guardado: los valores son los predeterminados.')
    id = serializers.UUIDField(allow_null=True)
    version = serializers.IntegerField(help_text='0 mientras no existe; envíalo como expected_version.')
    price_list_id = serializers.UUIDField(allow_null=True)
    price_list = PriceListRef(allow_null=True)
    fallback_to_default = serializers.BooleanField()
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2)
    preferred_currency = serializers.CharField(allow_blank=True)
    default_terms = serializers.CharField(allow_blank=True)
    internal_notes = serializers.CharField(allow_blank=True, help_text='Solo para tu equipo; nunca sale de tu cuenta.')
    customer_code = serializers.CharField(allow_blank=True)
    effective_list = PriceListRef(allow_null=True, help_text='La lista que usan sus precios sugeridos: la asignada si está activa, si no tu predeterminada.')
    effective_currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES))
    order_count = serializers.IntegerField()
    last_order_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField(allow_null=True)
    updated_by = MemberName(allow_null=True)
    can_configure = serializers.BooleanField()


PERCENT_ERRORS = {'invalid': 'Escribe un porcentaje válido, por ejemplo 5.50.', 'max_decimal_places': 'Usa un máximo de dos decimales.',
                  'max_digits': 'El descuento debe ser menor que 100.', 'max_whole_digits': 'El descuento debe ser menor que 100.',
                  'min_value': 'El descuento no puede ser negativo.', 'max_value': 'El descuento debe ser menor que 100.'}


class ClientProfileUpdate(serializers.Serializer):
    operation_id = serializers.UUIDField()
    expected_version = serializers.IntegerField(min_value=0, help_text='0 crea el perfil.')
    price_list_id = serializers.UUIDField(allow_null=True, required=False, help_text='null usa tu lista predeterminada.')
    fallback_to_default = serializers.BooleanField(required=False)
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('99.99'), required=False,
                                                error_messages=PERCENT_ERRORS)
    preferred_currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES), allow_blank=True, required=False)
    default_terms = serializers.CharField(max_length=5000, allow_blank=True, required=False, trim_whitespace=False)
    internal_notes = serializers.CharField(max_length=4000, allow_blank=True, required=False, trim_whitespace=False)
    customer_code = serializers.CharField(max_length=40, allow_blank=True, required=False)

    def validate_default_terms(self, value):
        return value.strip().upper()

    def validate_internal_notes(self, value):
        return value.strip().upper()

    def validate_customer_code(self, value):
        return value.strip()


class ClientProfileConflict(serializers.Serializer):
    detail = serializers.CharField()
    profile = ClientProfileSerializer()


def profile_values(profile):
    return {field: getattr(profile, field) for field in PROFILE_FIELDS} if profile else dict(PROFILE_DEFAULTS)


def profile_data(account, membership, client):
    profile = ClientPricingProfile.objects.select_related('price_list', 'updated_by').filter(supplier=account, client=client).first()
    settings_row = pricing_settings(account)
    default = PriceList.objects.filter(supplier=account, is_default=True, active=True).first()
    assigned = profile.price_list if profile and profile.price_list_id else None
    orders = client_orders(account).get(pk=client.pk)
    return ClientProfileSerializer({
        'client': {'id': client.pk, 'name': client.name}, 'exists': profile is not None, 'id': profile.pk if profile else None,
        'version': profile.version if profile else 0, **profile_values(profile), 'price_list': list_ref(assigned),
        'effective_list': list_ref(assigned if assigned and assigned.active else default),
        'effective_currency': profile.preferred_currency if profile and profile.preferred_currency else settings_row.default_currency,
        'order_count': orders.order_count, 'last_order_at': orders.last_order_at, 'updated_at': profile.updated_at if profile else None,
        'updated_by': user_name(profile.updated_by) if profile else None, 'can_configure': can_configure(account, membership, settings_row)}).data


class ClientProfileView(APIView):
    @extend_schema(operation_id='v1_accounts_clients_profile_retrieve', responses=ClientProfileSerializer,
                   description='Perfil comercial privado de un cliente que te ha enviado órdenes. Sin perfil guardado devuelve los valores predeterminados '
                               '(exists=false, version=0) sin crear nada. Cualquier otra cuenta: 404.')
    def get(self, request, account_id, client_id):
        account, membership = membership_for(request.user, account_id, 'supplier')
        return Response(profile_data(account, membership, related_client(account, client_id)))

    @extend_schema(operation_id='v1_accounts_clients_profile_update', request=ClientProfileUpdate, responses={200: ClientProfileSerializer, 409: ClientProfileConflict},
                   description='Guardado parcial con operation_id y expected_version (0 crea el perfil). Valores iguales no cambian nada; '
                               'repetir la misma operación devuelve el perfil.')
    @transaction.atomic
    def post(self, request, account_id, client_id):
        account, membership = configurer(request.user, account_id)
        client = related_client(account, client_id)
        serializer = ClientProfileUpdate(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        Account.objects.select_for_update().get(pk=account.pk)
        profile = ClientPricingProfile.objects.select_for_update().filter(supplier=account, client=client).first()
        fingerprint = payload_hash(data)
        if profile and profile.last_operation_id == data['operation_id']:
            if profile.last_payload_hash != fingerprint:
                raise RequestConflict(REUSED_OPERATION)
            return Response(profile_data(account, membership, client))
        current = profile_values(profile)
        changes = {field: data[field] for field in PROFILE_FIELDS if field in data and current[field] != data[field]}
        if profile and not changes:
            return Response(profile_data(account, membership, client))
        if data['expected_version'] != (profile.version if profile else 0):
            return Response({'detail': 'Otro usuario actualizó este perfil. Revisa sus valores actuales.', 'profile': profile_data(account, membership, client)}, status=409)
        if changes.get('price_list_id'):
            chosen = PriceList.objects.filter(supplier=account, pk=changes['price_list_id']).first()
            if chosen is None or not chosen.active:
                raise ValidationError({'price_list_id': ['La lista de precios no existe.' if chosen is None else 'Esa lista está archivada; elige una lista activa.']})
        created = profile is None
        profile = profile or ClientPricingProfile(supplier=account, client=client, version=0)
        for field, value in changes.items():
            setattr(profile, field, value)
        profile.version += 1
        profile.last_operation_id, profile.last_payload_hash, profile.updated_by = data['operation_id'], fingerprint, request.user
        try:
            with transaction.atomic():
                profile.save()
        except IntegrityError:
            raise ValidationError('El perfil no cumple las reglas de perfiles comerciales. Actualiza la página e inténtalo de nuevo.')
        record_pricing_event(account, request.user, 'profile_changed', client=client, object_id=str(profile.pk), payload={
            'action': 'created' if created else 'updated', 'version': profile.version, 'old': plain({field: current[field] for field in changes}), 'new': plain(changes)})
        return Response(profile_data(account, membership, client))


def ensure_profile(account, client, user):
    """Client rules hang from the pair's profile: the first one creates it with default values (audited like any profile change)."""
    profile = ClientPricingProfile.objects.select_for_update().filter(supplier=account, client=client).first()
    if profile is None:
        profile = ClientPricingProfile.objects.create(supplier=account, client=client, updated_by=user)
        record_pricing_event(account, user, 'profile_changed', client=client, object_id=str(profile.pk), payload={
            'action': 'created', 'version': profile.version, 'old': {}, 'new': {}, 'reason': 'rule'})
    return profile


class RuleItem(serializers.Serializer):
    id = serializers.UUIDField()
    supplier_invent_id = serializers.CharField()
    codigo = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    description = serializers.CharField(allow_blank=True)


class PricingRuleSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    scope = serializers.ChoiceField(choices=choice_values(RULE_SCOPES))
    client = AccountRef(allow_null=True, help_text='El cliente de una regla propia; null para todos tus clientes.')
    target = serializers.ChoiceField(choices=choice_values(RULE_TARGETS))
    target_value = serializers.CharField(allow_blank=True, help_text='La línea o la marca.')
    item = RuleItem(allow_null=True)
    kind = serializers.ChoiceField(choices=choice_values(RULE_KINDS))
    value = serializers.DecimalField(max_digits=12, decimal_places=2, help_text='Porcentaje de descuento o precio neto.')
    currency = serializers.CharField(allow_blank=True, help_text='Moneda del precio neto; vacía en descuentos.')
    min_quantity = serializers.IntegerField()
    valid_from = serializers.DateField(allow_null=True)
    valid_until = serializers.DateField(allow_null=True)
    active = serializers.BooleanField()
    validity = serializers.ChoiceField(choices=RULE_VALIDITY, help_text='Hoy (America/Panama): vigente, programada, vencida o archivada.')
    note = serializers.CharField(allow_blank=True, help_text='Nota interna; nunca se muestra al cliente.')
    revision = serializers.IntegerField(help_text='Envíala como expected_version.')
    created_by = MemberName()
    updated_by = MemberName()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class PricingRulePage(page_serializer('PricingRulePageBase', PricingRuleSerializer)):
    can_configure = serializers.BooleanField()


RULE_VALUE_ERRORS = {**MONEY_ERRORS, 'min_value': 'El valor debe ser mayor que 0.'}


class PricingRuleFields(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    scope = serializers.ChoiceField(choices=choice_values(RULE_SCOPES))
    client_id = serializers.UUIDField(allow_null=True, required=False, help_text='Obligatorio para una regla de un cliente.')
    target = serializers.ChoiceField(choices=choice_values(RULE_TARGETS))
    target_value = serializers.CharField(max_length=120, allow_blank=True, required=False, help_text='La línea o la marca.')
    item_id = serializers.UUIDField(allow_null=True, required=False, help_text='El artículo de tu inventario.')
    kind = serializers.ChoiceField(choices=choice_values(RULE_KINDS))
    value = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'), error_messages=RULE_VALUE_ERRORS)
    currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES), allow_blank=True, required=False,
                                       help_text='Moneda del precio neto (tu moneda predeterminada si se omite); vacía en descuentos.')
    min_quantity = WholeRequestQuantity(min_value=1, max_value=9999, required=False)
    valid_from = serializers.DateField(allow_null=True, required=False)
    valid_until = serializers.DateField(allow_null=True, required=False)
    note = serializers.CharField(max_length=200, allow_blank=True, required=False)

    def validate_name(self, value):
        value = value.strip().upper()
        if not value:
            raise serializers.ValidationError('Escribe el nombre de la regla.')
        return value

    def validate_target_value(self, value):
        return value.strip().upper()

    def validate_note(self, value):
        return value.strip().upper()


class PricingRuleCreate(PricingRuleFields):
    operation_id = serializers.UUIDField()


class PricingRuleUpdate(PricingRuleFields):
    expected_version = serializers.IntegerField(min_value=1)
    active = serializers.BooleanField(required=False, help_text='true reactiva una regla archivada.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in ('name', 'scope', 'target', 'kind', 'value'):
            self.fields[field].required = False


class PricingRuleArchive(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)


class PricingRuleConflict(serializers.Serializer):
    detail = serializers.CharField()
    rule = PricingRuleSerializer()


def rule_validity(rule, today):
    return 'archived' if not rule.active else 'scheduled' if rule.valid_from and rule.valid_from > today else 'expired' if rule.valid_until and rule.valid_until < today \
        else 'active'


def rule_rows(rules):
    today = timezone.localdate()
    return PricingRuleSerializer([{
        **{field: getattr(rule, field) for field in ('id', 'name', 'scope', 'target', 'target_value', 'kind', 'value', 'currency', 'min_quantity', 'valid_from',
                                                      'valid_until', 'active', 'note', 'revision', 'created_at', 'updated_at')},
        'client': {'id': rule.profile.client_id, 'name': rule.profile.client.name} if rule.profile_id else None,
        'item': {field: getattr(rule.item, field) for field in ('id', 'supplier_invent_id', 'codigo', 'brand', 'description')} if rule.item_id else None,
        'validity': rule_validity(rule, today), 'created_by': user_name(rule.created_by), 'updated_by': user_name(rule.updated_by)} for rule in rules], many=True).data


def rule_queryset(account):
    return PricingRule.objects.filter(supplier=account).select_related('profile__client', 'item', 'created_by', 'updated_by')


def rule_data(rule):
    return rule_rows([rule_queryset(rule.supplier).get(pk=rule.pk)])[0]


def rule_inputs(rule):
    return {**{field: getattr(rule, field) for field in RULE_INPUTS if field not in ('client_id',)}, 'client_id': rule.profile.client_id if rule.profile_id else None}


def rule_values(account, user, data, settings_row):
    """Coherence of a rule's inputs -> model values (the client's profile and the item resolved). Spanish 400s; an unrelated client is 404."""
    errors, values = {}, {field: data.get(field) for field in ('name', 'scope', 'target', 'kind', 'value', 'valid_from', 'valid_until')}
    values.update(target_value=data.get('target_value') or '', note=data.get('note') or '', min_quantity=data.get('min_quantity') or 1, profile=None, item=None,
                  currency=data.get('currency') or '', active=data.get('active', True))
    if values['scope'] == 'client':
        if not data.get('client_id'):
            errors['client_id'] = ['Elige el cliente de esta regla.']
        else:
            values['profile'] = related_client(account, data['client_id'])
    elif data.get('client_id'):
        errors['client_id'] = ['Las reglas para todos tus clientes no llevan cliente.']
    if values['target'] == 'item':
        if not data.get('item_id'):
            errors['item_id'] = ['Elige el artículo.']
        elif not (item := SupplierItem.objects.filter(supplier=account, pk=data['item_id']).first()):
            errors['item_id'] = [FOREIGN_ITEM]
        else:
            values['item'] = item
        if values['target_value']:
            errors['target_value'] = ['Una regla de un artículo no lleva línea ni marca.']
    elif values['target'] in ('line', 'brand'):
        if not values['target_value']:
            errors['target_value'] = ['Escribe la línea.' if values['target'] == 'line' else 'Escribe la marca.']
        if data.get('item_id'):
            errors['item_id'] = ['Una regla de una línea o marca no lleva artículo.']
    elif values['target_value'] or data.get('item_id'):
        errors['target'] = ['Una regla para todos los artículos no lleva línea, marca ni artículo.']
    if values['kind'] == 'discount':
        if values['value'] is not None and values['value'] >= 100:
            errors['value'] = ['El descuento debe ser mayor que 0 y menor que 100.']
        if values['currency']:
            errors['currency'] = ['Un descuento no lleva moneda.']
    elif values['kind'] == 'net_price':
        values['currency'] = values['currency'] or settings_row.default_currency
        if values['target'] != 'item':
            errors['kind'] = ['El precio neto solo se aplica a un artículo.']
    if values['valid_from'] and values['valid_until'] and values['valid_until'] < values['valid_from']:
        errors['valid_until'] = ['La fecha final debe ser igual o posterior a la inicial.']
    if errors:
        raise ValidationError(errors)
    if values['profile'] is not None:
        values['profile'] = ensure_profile(account, values['profile'], user)
    return values


def check_active_rule(account, values, rule=None):
    """No two active rules may share scope, client, target, line or brand, item, kind and minimum quantity in overlapping windows (a certain
    rule_conflict); and a supplier keeps at most MAX_ACTIVE_RULES active rules."""
    if not values['active']:
        return
    others = PricingRule.objects.filter(supplier=account, active=True).exclude(pk=getattr(rule, 'pk', None))
    if others.count() >= MAX_ACTIVE_RULES:
        raise ValidationError(f'Llegaste al máximo de {MAX_ACTIVE_RULES:,} reglas activas. Archiva las que ya no uses.'.replace(',', '.'))
    start, end = values['valid_from'], values['valid_until']
    same = others.filter(scope=values['scope'], profile=values['profile'], target=values['target'], target_value=values['target_value'], item=values['item'],
                         kind=values['kind'], min_quantity=values['min_quantity'])
    if start:
        same = same.filter(Q(valid_until__isnull=True) | Q(valid_until__gte=start))
    if end:
        same = same.filter(Q(valid_from__isnull=True) | Q(valid_from__lte=end))
    if duplicate := same.order_by('created_at').first():
        raise ValidationError(f'Ya tienes una regla igual vigente en esas fechas: {duplicate.name}. Edítala o cambia sus fechas.')


def rule_audit(values):
    return plain({field: values[field] for field in RULE_INPUTS if field in values})


class PricingRulesView(APIView):
    @extend_schema(operation_id='v1_accounts_pricing_rules_list', responses=PricingRulePage, parameters=[
        OpenApiParameter('search', OpenApiTypes.STR), OpenApiParameter('status', OpenApiTypes.STR, enum=['active', 'archived']),
        OpenApiParameter('client', OpenApiTypes.UUID, description='Solo las reglas propias de este cliente.'),
        OpenApiParameter('scope', OpenApiTypes.STR, enum=choice_values(RULE_SCOPES)), OpenApiParameter('page', OpenApiTypes.INT)],
        description='Tus reglas comerciales privadas: activas primero, por nombre.')
    def get(self, request, account_id):
        account, membership = membership_for(request.user, account_id, 'supplier')
        params = request.query_params
        rows = rule_queryset(account)
        if params.get('status') in ('active', 'archived'):
            rows = rows.filter(active=params['status'] == 'active')
        if params.get('scope') in choice_values(RULE_SCOPES):
            rows = rows.filter(scope=params['scope'])
        if params.get('client'):
            try:
                client_id = uuid.UUID(str(params['client']))
            except ValueError:
                raise ValidationError({'client': ['Indica un cliente válido.']})
            rows = rows.filter(profile__client=related_client(account, client_id))
        if search := str(params.get('search', '')).strip()[:200]:
            rows = rows.filter(Q(name__icontains=search) | Q(target_value__icontains=search) | Q(note__icontains=search) | Q(item__codigo__icontains=search)
                               | Q(item__supplier_invent_id__icontains=search) | Q(profile__client__name__icontains=search))
        paginator = PricePagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        response = paginator.get_paginated_response(rule_rows(page))
        response.data['can_configure'] = can_configure(account, membership)
        return response

    @extend_schema(operation_id='v1_accounts_pricing_rules_create', request=PricingRuleCreate, responses={200: PricingRuleSerializer, 201: PricingRuleSerializer},
                   description='Crea una regla. Repetir la misma operación devuelve la regla creada. Una regla igual activa en fechas que se cruzan: 400.')
    @transaction.atomic
    def post(self, request, account_id):
        account, _ = configurer(request.user, account_id)
        serializer = PricingRuleCreate(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        Account.objects.select_for_update().get(pk=account.pk)
        fingerprint = payload_hash(data)
        if existing := PricingRule.objects.filter(supplier=account, operation_id=data['operation_id']).first():
            if existing.payload_hash != fingerprint:
                raise RequestConflict(REUSED_OPERATION)
            return Response(rule_data(existing))
        values = rule_values(account, request.user, data, pricing_settings(account))
        check_active_rule(account, values)
        try:
            with transaction.atomic():
                rule = PricingRule.objects.create(supplier=account, operation_id=data['operation_id'], payload_hash=fingerprint, created_by=request.user,
                                                  updated_by=request.user, **values)
        except IntegrityError:
            raise ValidationError('La regla no cumple las reglas de precios. Revisa sus valores e inténtalo de nuevo.')
        record_pricing_event(account, request.user, 'rule_created', client_id=rule.profile.client_id if rule.profile_id else None, object_id=str(rule.pk),
                             payload={'revision': rule.revision, 'new': rule_audit(rule_inputs(rule))})
        return Response(rule_data(rule), status=201)


class PricingRuleDetail(APIView):
    @extend_schema(operation_id='v1_accounts_pricing_rules_update', request=PricingRuleUpdate, responses={200: PricingRuleSerializer, 409: PricingRuleConflict},
                   description='Cambia una regla con expected_version (su revisión). Valores iguales no cambian nada; active=true la reactiva.')
    @transaction.atomic
    def post(self, request, account_id, pk):
        account, _ = configurer(request.user, account_id)
        serializer = PricingRuleUpdate(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        Account.objects.select_for_update().get(pk=account.pk)
        rule = get_object_or_404(rule_queryset(account).select_for_update(of=('self',)), pk=pk)
        current = rule_inputs(rule)
        # Moving a rule to another scope, target or kind drops the inputs that no longer apply unless they are sent.
        if data.get('scope') == 'all':
            data.setdefault('client_id', None)
        if data.get('target') in ('line', 'brand', 'all'):
            data.setdefault('item_id', None)
        if data.get('target') in ('item', 'all'):
            data.setdefault('target_value', '')
        if data.get('kind') == 'discount':
            data.setdefault('currency', '')
        changes = {field: data[field] for field in RULE_INPUTS if field in data and current[field] != data[field]}
        if not changes:
            return Response(rule_data(rule))
        if rule.revision != data['expected_version']:
            return Response({'detail': 'Otro usuario actualizó esta regla. Revisa sus valores actuales.', 'rule': rule_data(rule)}, status=409)
        values = rule_values(account, request.user, {**current, **changes}, pricing_settings(account))
        check_active_rule(account, values, rule)
        for field, value in values.items():
            setattr(rule, field, value)
        rule.revision += 1
        rule.updated_by = request.user
        try:
            with transaction.atomic():
                rule.save()
        except IntegrityError:
            raise ValidationError('La regla no cumple las reglas de precios. Revisa sus valores e inténtalo de nuevo.')
        record_pricing_event(account, request.user, 'rule_updated', client_id=rule.profile.client_id if rule.profile_id else None, object_id=str(rule.pk),
                             payload={'revision': rule.revision, 'name': rule.name, 'old': rule_audit({field: current[field] for field in changes}), 'new': rule_audit(changes)})
        return Response(rule_data(rule))


class PricingRuleArchiveView(APIView):
    @extend_schema(operation_id='v1_accounts_pricing_rules_archive', request=PricingRuleArchive, responses={200: PricingRuleSerializer, 409: PricingRuleConflict},
                   description='Archiva una regla (nunca se borra). Archivar una regla ya archivada no cambia nada.')
    @transaction.atomic
    def post(self, request, account_id, pk):
        account, _ = configurer(request.user, account_id)
        serializer = PricingRuleArchive(data=request.data)
        serializer.is_valid(raise_exception=True)
        Account.objects.select_for_update().get(pk=account.pk)
        rule = get_object_or_404(rule_queryset(account).select_for_update(of=('self',)), pk=pk)
        if not rule.active:
            return Response(rule_data(rule))
        if rule.revision != serializer.validated_data['expected_version']:
            return Response({'detail': 'Otro usuario actualizó esta regla. Revisa sus valores actuales.', 'rule': rule_data(rule)}, status=409)
        rule.active, rule.revision, rule.updated_by = False, rule.revision + 1, request.user
        rule.save(update_fields=['active', 'revision', 'updated_by', 'updated_at'])
        record_pricing_event(account, request.user, 'rule_archived', client_id=rule.profile.client_id if rule.profile_id else None, object_id=str(rule.pk),
                             payload={'revision': rule.revision, 'name': rule.name})
        return Response(rule_data(rule))


class SimulateItem(serializers.Serializer):
    supplier_item_id = serializers.UUIDField()
    quantity = WholeRequestQuantity(min_value=1, max_value=9999)


class SimulateRequest(serializers.Serializer):
    client_id = serializers.UUIDField(allow_null=True, required=False, help_text='null: solo tus reglas generales, sin perfil de cliente.')
    currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES), required=False,
                                       help_text='Moneda de la cotización (la preferida del cliente o tu predeterminada si se omite).')
    on_date = serializers.DateField(required=False, help_text='Fecha de cálculo (hoy en America/Panama si se omite).')
    items = SimulateItem(many=True, min_length=1, max_length=50)

    def validate_items(self, items):
        if len({item['supplier_item_id'] for item in items}) != len(items):
            raise serializers.ValidationError('Incluye cada artículo una sola vez.')
        return items


class SimulatedLine(serializers.Serializer):
    supplier_item_id = serializers.UUIDField()
    supplier_invent_id = serializers.CharField()
    codigo = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    description = serializers.CharField(allow_blank=True)
    discount_group = serializers.CharField(allow_blank=True)
    quantity = serializers.IntegerField()
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    line_total = serializers.DecimalField(max_digits=16, decimal_places=2, allow_null=True)
    status = serializers.CharField()
    explanation = serializers.DictField(help_text='Cómo se calculó: lista, regla aplicada, reglas descartadas y siguientes escalas. Solo para tu equipo.')


class SimulationProfile(serializers.Serializer):
    exists = serializers.BooleanField()
    version = serializers.IntegerField()
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2)


class SimulationResult(serializers.Serializer):
    client = AccountRef(allow_null=True)
    profile = SimulationProfile(allow_null=True, help_text='null sin cliente.')
    price_list = PriceListRef(allow_null=True, help_text='La lista que usa el cálculo.')
    currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES))
    on_date = serializers.DateField()
    engine = serializers.CharField()
    lines = SimulatedLine(many=True)


class PricingSimulate(APIView):
    @extend_schema(operation_id='v1_accounts_pricing_simulate', request=SimulateRequest, responses=SimulationResult,
                   description='Calcula precios sugeridos con tus listas, el perfil del cliente y tus reglas, sin guardar nada.')
    def post(self, request, account_id):
        account, _ = membership_for(request.user, account_id, 'supplier')
        serializer = SimulateRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        client = related_client(account, data['client_id']) if data.get('client_id') else None
        settings_row, profile = pricing_settings(account), client_profile(account, client.pk) if client else None
        identifiers = [item['supplier_item_id'] for item in data['items']]
        items = {item.pk: item for item in SupplierItem.objects.filter(supplier=account, pk__in=identifiers).select_related('pricing')}
        if len(items) != len(identifiers):
            raise ValidationError({'items': [FOREIGN_ITEM]})
        currency = data.get('currency') or (profile.preferred_currency if profile and profile.preferred_currency else settings_row.default_currency)
        on_date = data.get('on_date') or timezone.localdate()
        quantities = {item['supplier_item_id']: item['quantity'] for item in data['items']}
        results = price_items(account, client.pk if client else None, currency, [(pk, items[pk], quantities[pk], True) for pk in identifiers],
                              settings_row=settings_row, on_date=on_date, profile=profile)
        listed = next((result.explanation['price_list'] for result in results.values() if result.explanation['price_list']), None)
        return Response(SimulationResult({
            'client': {'id': client.pk, 'name': client.name} if client else None, 'currency': currency, 'on_date': on_date, 'engine': ENGINE_VERSION,
            'profile': {'exists': profile is not None, 'version': profile.version if profile else 0, 'discount_percent': profile.discount_percent if profile
                        else Decimal('0')} if client else None,
            'price_list': {**listed, 'active': True} if listed else None,
            'lines': [{'supplier_item_id': pk, 'supplier_invent_id': items[pk].supplier_invent_id, 'codigo': items[pk].codigo, 'brand': items[pk].brand,
                       'description': items[pk].description, 'discount_group': item_pricing(items[pk]).discount_group if item_pricing(items[pk]) else '',
                       'quantity': quantities[pk], 'unit_price': results[pk].unit_price, 'status': results[pk].status, 'explanation': results[pk].explanation,
                       'line_total': (results[pk].unit_price * quantities[pk]).quantize(Decimal('0.01')) if results[pk].unit_price is not None else None}
                      for pk in identifiers]}).data)
