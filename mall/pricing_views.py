"""Supplier-only pricing endpoints. Nothing here feeds catalog, offers, request or deal payloads."""
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from .pricing_models import (CURRENCY_CHOICES, OWNER_ONLY_SETTINGS, PERMISSION_CHOICES, POLICY_CHOICES, PREFILL_QUANTITY_CHOICES,
                             SHORTFALL_POLICY_CHOICES, choice_values, pricing_settings, record_pricing_event)
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
