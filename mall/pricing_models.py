"""Supplier-private pricing configuration and audit.

Nothing here is registered in Django admin, exposed to management endpoints or read by client-facing
serializers: prices and their configuration are visible only to members of the supplier account.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

CURRENCY_CHOICES = [('USD', 'USD'), ('PAB', 'PAB')]
# Identical to Membership.permission; the shared OpenAPI enum (MembershipPermissionEnum) depends on it.
PERMISSION_CHOICES = [('owner', 'Propietario'), ('manager', 'Administrador'), ('staff', 'Empleado')]
POLICY_CHOICES = [('confirm', 'Pedir confirmación'), ('block', 'Bloquear')]
SHORTFALL_POLICY_CHOICES = [('block', 'Bloquear la confirmación'), ('allow', 'Permitir y avisar al proveedor')]
PREFILL_QUANTITY_CHOICES = [('requested', 'Solicitadas'), ('available', 'Hasta las existencias')]
OWNER_ONLY_SETTINGS = ('config_min_permission', 'publish_min_permission', 'assistant_enabled')
PRICING_AUDIT_KINDS = {'settings_changed', 'price_list_changed', 'prices_edited', 'price_import_applied', 'profile_changed',
                       'rule_created', 'rule_updated', 'rule_archived', 'draft_discarded', 'draft_review_requested',
                       'quotation_published', 'accept_blocked_shortfall', 'accept_with_shortfall', 'assistant_run', 'assistant_applied'}


def choice_values(choices):
    return [value for value, _ in choices]


class SupplierPricingSettings(models.Model):
    supplier = models.OneToOneField('mall.Account', on_delete=models.PROTECT, primary_key=True, related_name='pricing_settings')
    default_currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='USD')
    usd_pab_parity = models.BooleanField(default=True)
    config_min_permission = models.CharField(max_length=10, choices=PERMISSION_CHOICES, default='staff')
    publish_min_permission = models.CharField(max_length=10, choices=PERMISSION_CHOICES, default='staff')
    over_request_policy = models.CharField(max_length=10, choices=POLICY_CHOICES, default='confirm')
    over_stock_policy = models.CharField(max_length=10, choices=POLICY_CHOICES, default='confirm')
    accept_shortfall_policy = models.CharField(max_length=10, choices=SHORTFALL_POLICY_CHOICES, default='block')
    prefill_quantity = models.CharField(max_length=10, choices=PREFILL_QUANTITY_CHOICES, default='requested')
    assistant_enabled = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(default_currency__in=choice_values(CURRENCY_CHOICES)), name='valid_pricing_default_currency'),
            models.CheckConstraint(condition=Q(config_min_permission__in=choice_values(PERMISSION_CHOICES)), name='valid_pricing_config_permission'),
            models.CheckConstraint(condition=Q(publish_min_permission__in=choice_values(PERMISSION_CHOICES)), name='valid_pricing_publish_permission'),
            models.CheckConstraint(condition=Q(over_request_policy__in=choice_values(POLICY_CHOICES)), name='valid_pricing_over_request_policy'),
            models.CheckConstraint(condition=Q(over_stock_policy__in=choice_values(POLICY_CHOICES)), name='valid_pricing_over_stock_policy'),
            models.CheckConstraint(condition=Q(accept_shortfall_policy__in=choice_values(SHORTFALL_POLICY_CHOICES)), name='valid_pricing_shortfall_policy'),
            models.CheckConstraint(condition=Q(prefill_quantity__in=choice_values(PREFILL_QUANTITY_CHOICES)), name='valid_pricing_prefill_quantity'),
        ]


class PricingAuditEvent(models.Model):
    """Supplier-private audit, used instead of DealEvent for anything the client must not see."""
    id = models.BigAutoField(primary_key=True)
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='pricing_audit_events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    kind = models.CharField(max_length=40)
    client = models.ForeignKey('mall.Account', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    order = models.ForeignKey('mall.SupplierRequest', on_delete=models.PROTECT, null=True, blank=True, related_name='pricing_audit_events')
    object_id = models.CharField(max_length=40, blank=True, default='')
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['supplier', '-created_at'], name='pricing_audit_supplier_idx')]


def pricing_settings(account, *, lock=False):
    """Read paths get unsaved defaults and write nothing; write paths create the row lazily and lock it."""
    if not lock:
        return SupplierPricingSettings.objects.select_related('updated_by').filter(supplier=account).first() or SupplierPricingSettings(supplier=account)
    SupplierPricingSettings.objects.get_or_create(supplier=account)
    return SupplierPricingSettings.objects.select_for_update().get(supplier=account)


def record_pricing_event(supplier, actor, kind, **fields):
    if kind not in PRICING_AUDIT_KINDS:
        raise ValueError(f'Unknown pricing audit kind: {kind}')
    return PricingAuditEvent.objects.create(supplier=supplier, actor=actor, kind=kind, **fields)
