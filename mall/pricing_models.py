"""Supplier-private pricing configuration and audit.

Nothing here is registered in Django admin, exposed to management endpoints or read by client-facing
serializers: prices and their configuration are visible only to members of the supplier account.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q

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


PRICE_LIST_CODE = r'^[A-Z0-9_]{1,30}$'
PRICE_WRITE_SOURCES = [('manual', 'Manual'), ('import', 'Importación'), ('api', 'Integración')]
PRICE_CHANGE_KINDS = [('list_price', 'Precio de lista'), ('floor_price', 'Precio mínimo'), ('discount_group', 'Línea')]


class PriceList(models.Model):
    """A named supplier price list. Codes are immutable (they key import columns PRECIO_<CODE>); the currency is fixed once it has prices."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='price_lists')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='USD')
    is_default = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', 'code']
        constraints = [
            models.UniqueConstraint(fields=['supplier', 'code'], name='unique_price_list_code'),
            models.UniqueConstraint(fields=['supplier'], condition=Q(is_default=True), name='one_default_price_list'),
            models.CheckConstraint(condition=~Q(is_default=True, active=False), name='default_price_list_active'),
            models.CheckConstraint(condition=Q(currency__in=choice_values(CURRENCY_CHOICES)), name='valid_price_list_currency'),
        ]


class PriceListEntry(models.Model):
    """One item's price in one list. Invariant kept by pricing_services: item.supplier_id == price_list.supplier_id."""
    id = models.BigAutoField(primary_key=True)
    price_list = models.ForeignKey(PriceList, on_delete=models.PROTECT, related_name='entries')
    item = models.ForeignKey('mall.SupplierItem', on_delete=models.PROTECT, related_name='price_entries')
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    revision = models.PositiveIntegerField(default=1)
    source = models.CharField(max_length=10, choices=PRICE_WRITE_SOURCES, default='manual')
    reference = models.CharField(max_length=120, blank=True, default='')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    updated_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['price_list', 'item'], name='unique_price_list_item'),
                       models.CheckConstraint(condition=Q(unit_price__gt=0), name='positive_list_price')]
        indexes = [models.Index(fields=['item'], name='price_entry_item_idx')]


class SupplierItemPricing(models.Model):
    """Commercial attributes of an item, kept apart from SupplierItem so price writes never touch stock rows or their timestamps."""
    item = models.OneToOneField('mall.SupplierItem', on_delete=models.PROTECT, primary_key=True, related_name='pricing')
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='+')
    discount_group = models.CharField(max_length=60, blank=True, default='', help_text='LINEA o familia propia del proveedor.')
    floor_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    updated_at = models.DateTimeField()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(floor_price__isnull=True) | Q(floor_price__gte=0), name='valid_item_floor_price')]
        indexes = [models.Index(fields=['supplier', 'discount_group'], name='item_pricing_group_idx')]


class PriceChange(models.Model):
    """Append-only history of every list price, floor price and LINEA change. Null values mean none or removed."""
    id = models.BigAutoField(primary_key=True)
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='price_changes')
    item = models.ForeignKey('mall.SupplierItem', on_delete=models.PROTECT, related_name='price_changes')
    price_list = models.ForeignKey(PriceList, on_delete=models.PROTECT, null=True, blank=True, related_name='changes')
    kind = models.CharField(max_length=20, choices=PRICE_CHANGE_KINDS)
    old_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    new_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    old_text = models.CharField(max_length=60, blank=True, default='')
    new_text = models.CharField(max_length=60, blank=True, default='')
    source = models.CharField(max_length=10, choices=PRICE_WRITE_SOURCES)
    reference = models.CharField(max_length=120, blank=True, default='')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.CheckConstraint(condition=Q(kind__in=choice_values(PRICE_CHANGE_KINDS)), name='valid_price_change_kind')]
        indexes = [models.Index(fields=['supplier', 'item', '-created_at'], name='price_change_item_idx'),
                   models.Index(fields=['price_list', '-created_at'], name='price_change_list_idx')]


RULE_SCOPES = [('all', 'Todos tus clientes'), ('client', 'Un cliente')]
RULE_TARGETS = [('all', 'Todos los artículos'), ('line', 'Línea'), ('brand', 'Marca'), ('item', 'Un artículo')]
RULE_KINDS = [('discount', 'Descuento %'), ('net_price', 'Precio neto')]
MAX_ACTIVE_RULES = 2000


class ClientPricingProfile(models.Model):
    """The private commercial profile of one supplier–client pair. Only for clients that ordered from the supplier; never deleted;
    each save bumps version, which feeds the engine fingerprint (open drafts show stale_price instead of changing silently)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='client_profiles')
    client = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='supplier_profiles')
    price_list = models.ForeignKey(PriceList, on_delete=models.PROTECT, null=True, blank=True, related_name='client_profiles',
                                   help_text='Null: la lista predeterminada del proveedor.')
    fallback_to_default = models.BooleanField(default=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    preferred_currency = models.CharField(max_length=3, blank=True, default='')
    default_terms = models.TextField(blank=True, default='')
    internal_notes = models.TextField(blank=True, default='')
    customer_code = models.CharField(max_length=40, blank=True, default='')
    version = models.PositiveIntegerField(default=1)
    last_operation_id = models.UUIDField(null=True, blank=True)
    last_payload_hash = models.CharField(max_length=64, blank=True, default='')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['supplier', 'client'], name='unique_client_pricing_profile'),
            models.CheckConstraint(condition=~Q(supplier=F('client')), name='profile_distinct_accounts'),
            models.CheckConstraint(condition=Q(discount_percent__gte=0, discount_percent__lt=100), name='valid_profile_discount'),
            models.CheckConstraint(condition=Q(preferred_currency__in=['', *choice_values(CURRENCY_CHOICES)]), name='valid_profile_currency'),
        ]


class PricingRule(models.Model):
    """A supplier commercial rule: a discount or a net price for all clients or one pair, on all items, a LINEA, a brand or one item,
    from a minimum quantity and inside an inclusive America/Panama date window. Archived (active=False), never deleted."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='pricing_rules')
    name = models.CharField(max_length=120)
    scope = models.CharField(max_length=10, choices=RULE_SCOPES)
    profile = models.ForeignKey(ClientPricingProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='rules')
    target = models.CharField(max_length=10, choices=RULE_TARGETS)
    target_value = models.CharField(max_length=120, blank=True, default='', help_text='LINEA o marca, en mayúsculas.')
    item = models.ForeignKey('mall.SupplierItem', on_delete=models.PROTECT, null=True, blank=True, related_name='pricing_rules')
    kind = models.CharField(max_length=10, choices=RULE_KINDS)
    value = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, blank=True, default='')
    min_quantity = models.PositiveSmallIntegerField(default=1)
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True, default='')
    revision = models.PositiveIntegerField(default=1)
    # The create retry key: the same operation with the same payload returns the rule it created.
    operation_id = models.UUIDField(null=True, blank=True)
    payload_hash = models.CharField(max_length=64, blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-active', 'name', 'created_at', 'id']
        constraints = [
            models.CheckConstraint(condition=Q(scope='client', profile__isnull=False) | Q(scope='all', profile__isnull=True), name='pricing_rule_scope'),
            models.CheckConstraint(condition=Q(target='item', item__isnull=False, target_value='') | Q(target__in=['line', 'brand'], item__isnull=True) & ~Q(target_value='')
                                   | Q(target='all', item__isnull=True, target_value=''), name='pricing_rule_target'),
            models.CheckConstraint(condition=Q(kind='discount', value__gt=0, value__lt=100, currency='')
                                   | Q(kind='net_price', value__gt=0, target='item', currency__in=choice_values(CURRENCY_CHOICES)), name='pricing_rule_value'),
            models.CheckConstraint(condition=Q(min_quantity__gte=1, min_quantity__lte=9999), name='pricing_rule_quantity'),
            models.CheckConstraint(condition=Q(valid_from__isnull=True) | Q(valid_until__isnull=True) | Q(valid_until__gte=F('valid_from')), name='pricing_rule_dates'),
            models.UniqueConstraint(fields=['supplier', 'operation_id'], condition=Q(operation_id__isnull=False), name='unique_pricing_rule_operation'),
        ]
        indexes = [models.Index(fields=['supplier', 'active'], name='pricing_rule_supplier_idx'),
                   models.Index(fields=['profile'], name='pricing_rule_profile_idx'), models.Index(fields=['item'], name='pricing_rule_item_idx')]


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
