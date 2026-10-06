"""Supplier-private quotation drafts. Never read by deal_data, request_summary or any client-facing payload, and never
registered in Django admin. Saves are serialized by the SupplierRequest row lock, so the last save id is enough for retries."""
from django.conf import settings
from django.db import models
from django.db.models import Q

from .pricing_models import CURRENCY_CHOICES, PERMISSION_CHOICES, choice_values

DRAFT_STATUS_CHOICES = [('editing', 'En edición'), ('review_requested', 'Listo para revisión')]
QUANTITY_SOURCE_CHOICES = [('requested', 'Solicitadas'), ('available', 'Hasta las existencias'), ('previous', 'Versión anterior'),
                           ('manual', 'Manual'), ('assistant', 'Asistente')]
# Deliberately no 'ai' value: prices come only from the engine, a previous revision or a person.
PRICE_SOURCE_CHOICES = [('engine', 'Lista o regla'), ('previous', 'Versión anterior'), ('manual', 'Manual'), ('none', 'Sin precio')]


class DealQuotationDraft(models.Model):
    order = models.OneToOneField('mall.SupplierRequest', on_delete=models.CASCADE, primary_key=True, related_name='quote_draft')
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='quote_drafts')
    base_quotation = models.ForeignKey('mall.DealQuotation', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='USD')
    terms = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=DRAFT_STATUS_CHOICES, default='editing')
    review_requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    review_requested_at = models.DateTimeField(null=True, blank=True)
    draft_version = models.PositiveIntegerField(default=1)
    last_save_id = models.UUIDField(null=True, blank=True)
    last_save_hash = models.CharField(max_length=64, blank=True, default='')
    order_acknowledgements = models.JSONField(default=list, blank=True)
    pricing_context = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(currency__in=choice_values(CURRENCY_CHOICES)), name='valid_quote_draft_currency'),
                       models.CheckConstraint(condition=Q(status__in=choice_values(DRAFT_STATUS_CHOICES)), name='valid_quote_draft_status')]


class DealQuotationDraftLine(models.Model):
    id = models.BigAutoField(primary_key=True)
    draft = models.ForeignKey(DealQuotationDraft, on_delete=models.CASCADE, related_name='lines')
    order_line = models.ForeignKey('mall.SupplierRequestLine', on_delete=models.PROTECT, related_name='+')
    quantity = models.PositiveSmallIntegerField(null=True, blank=True)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    quantity_source = models.CharField(max_length=10, choices=QUANTITY_SOURCE_CHOICES, default='requested')
    price_source = models.CharField(max_length=10, choices=PRICE_SOURCE_CHOICES, default='none')
    engine_unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    engine_fingerprint = models.CharField(max_length=64, blank=True, default='')
    explanation = models.JSONField(default=dict, blank=True)
    acknowledgements = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=500, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(fields=['draft', 'order_line'], name='unique_quote_draft_line'),
            models.CheckConstraint(condition=(Q(quantity__isnull=True) | Q(quantity__gte=0, quantity__lte=9999))
                                   & (Q(unit_price__isnull=True) | Q(unit_price__gte=0)), name='valid_quote_draft_line'),
            models.CheckConstraint(condition=Q(quantity_source__in=choice_values(QUANTITY_SOURCE_CHOICES))
                                   & Q(price_source__in=choice_values(PRICE_SOURCE_CHOICES)), name='valid_quote_draft_line_sources'),
        ]


# Published-quotation trace: supplier-only, never read by deal_data. 'unspecified' marks rows whose source cannot be derived.
AUDIT_PRICE_SOURCE_CHOICES = PRICE_SOURCE_CHOICES + [('unspecified', 'Sin especificar')]


class DealQuotationAudit(models.Model):
    quotation = models.OneToOneField('mall.DealQuotation', on_delete=models.PROTECT, primary_key=True, related_name='audit')
    draft_version = models.PositiveIntegerField(null=True, blank=True, help_text='Null when published without a draft (legacy path).')
    publisher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    publisher_permission = models.CharField(max_length=10, choices=PERMISSION_CHOICES)
    engine_version = models.CharField(max_length=20, blank=True, default='')
    profile_id = models.UUIDField(null=True, blank=True)
    profile_version = models.PositiveIntegerField(null=True, blank=True)
    settings_snapshot = models.JSONField(default=dict, blank=True)
    order_exceptions = models.JSONField(default=list, blank=True)
    # {result: ok|blocked|accepted_with_shortfall, lines, at}; rewritten by every accept attempt.
    accept_check = models.JSONField(null=True, blank=True)
    accept_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(publisher_permission__in=choice_values(PERMISSION_CHOICES)), name='valid_quote_audit_permission')]


class DealQuotationLineAudit(models.Model):
    line = models.OneToOneField('mall.DealQuotationLine', on_delete=models.PROTECT, primary_key=True, related_name='audit')
    available_at_quote = models.PositiveIntegerField(null=True, blank=True)
    identity_ok_at_quote = models.BooleanField()
    available_at_accept = models.PositiveIntegerField(null=True, blank=True)
    suggested_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_source = models.CharField(max_length=12, choices=AUDIT_PRICE_SOURCE_CHOICES)
    quantity_source = models.CharField(max_length=10, choices=QUANTITY_SOURCE_CHOICES)
    engine_fingerprint = models.CharField(max_length=64, blank=True, default='')
    explanation = models.JSONField(default=dict, blank=True)
    # [{code, severity, context, acknowledged_by, acknowledged_at}] as they stood when the revision was published.
    exceptions = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(price_source__in=choice_values(AUDIT_PRICE_SOURCE_CHOICES))
                                              & Q(quantity_source__in=choice_values(QUANTITY_SOURCE_CHOICES)), name='valid_quote_line_audit_sources')]
