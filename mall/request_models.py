"""Supplier orders, immutable quotation revisions and private deal conversations."""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class ClientRequestSubmission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='request_submissions')
    submission_id = models.UUIDField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    payload_hash = models.CharField(max_length=64)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    result = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['client', 'submission_id'], name='unique_client_request_submission')]


class SupplierRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(ClientRequestSubmission, on_delete=models.PROTECT, related_name='requests')
    client = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='sent_supplier_requests')
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='received_supplier_requests')
    client_name = models.CharField(max_length=200)
    supplier_name = models.CharField(max_length=200)
    reference = models.CharField(max_length=32, unique=True)
    STATUS_CHOICES = [('pending', 'Enviada · por revisar'), ('reviewed', 'En revisión'),
                      ('quoted', 'Cotizada · por confirmar'), ('adjustment', 'Ajuste solicitado'),
                      ('handshaked', 'Acuerdo confirmado')]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    version = models.PositiveIntegerField(default=1)
    handshaked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name='reviewed_supplier_requests')

    class Meta:
        ordering = ['-created_at', 'id']
        constraints = [models.UniqueConstraint(fields=['submission', 'supplier'], name='unique_submission_supplier_request'),
                       models.CheckConstraint(condition=Q(status__in=['pending', 'reviewed', 'quoted', 'adjustment', 'handshaked']), name='valid_supplier_request_status')]


class SupplierRequestLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(SupplierRequest, on_delete=models.CASCADE, related_name='lines')
    supplier_item = models.ForeignKey('mall.SupplierItem', on_delete=models.PROTECT, related_name='request_lines')
    part = models.ForeignKey('mall.Part', on_delete=models.PROTECT, related_name='request_lines')
    supplier_invent_id = models.CharField(max_length=120)
    sku = models.CharField(max_length=200)
    name = models.CharField(max_length=200, blank=True)
    codigo = models.CharField(max_length=120)
    brand = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    quantity = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ['sku', 'codigo', 'id']
        constraints = [models.UniqueConstraint(fields=['request', 'supplier_item'], name='unique_supplier_request_item'),
                       models.CheckConstraint(condition=Q(quantity__gte=1, quantity__lte=9999), name='valid_supplier_request_quantity')]


class RequestContribution(models.Model):
    """The exact cart addition remains attributable to its original submission."""
    submission = models.ForeignKey(ClientRequestSubmission, on_delete=models.PROTECT, related_name='contributions')
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, related_name='contributions')
    lines = models.JSONField(default=list)
    appended = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['submission', 'order'], name='unique_submission_order_contribution')]


class DealQuotation(models.Model):
    """A published revision is never edited; changes create a new revision."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, related_name='quotations')
    revision = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default='USD')
    terms = models.TextField(blank=True)
    total = models.DecimalField(max_digits=16, decimal_places=2)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='published_deal_quotes')
    supplier_confirmed_at = models.DateTimeField()
    client_confirmed_at = models.DateTimeField(null=True, blank=True)
    client_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                           related_name='confirmed_deal_quotes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['revision']
        constraints = [models.UniqueConstraint(fields=['order', 'revision'], name='unique_deal_quote_revision'),
                       models.CheckConstraint(condition=Q(total__gte=0), name='positive_deal_quote_total')]


class DealQuotationLine(models.Model):
    quotation = models.ForeignKey(DealQuotation, on_delete=models.PROTECT, related_name='lines')
    order_line = models.ForeignKey(SupplierRequestLine, on_delete=models.PROTECT, related_name='quotation_lines')
    quantity = models.PositiveSmallIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ['order_line__sku', 'order_line__codigo', 'id']
        constraints = [models.UniqueConstraint(fields=['quotation', 'order_line'], name='unique_deal_quote_line'),
                       models.CheckConstraint(condition=Q(quantity__gte=0, quantity__lte=9999, unit_price__gte=0),
                                              name='valid_deal_quote_line')]


class DealEvent(models.Model):
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    account = models.ForeignKey('mall.Account', on_delete=models.PROTECT)
    kind = models.CharField(max_length=40)
    description = models.TextField(blank=True)
    quotation = models.ForeignKey(DealQuotation, on_delete=models.PROTECT, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class DealMessage(models.Model):
    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, related_name='messages')
    account = models.ForeignKey('mall.Account', on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    message_id = models.UUIDField()
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        constraints = [models.UniqueConstraint(fields=['order', 'account', 'message_id'], name='unique_deal_message_send')]


class DealCommand(models.Model):
    """Durable retry keys for quotation decisions, including lost responses."""
    order = models.ForeignKey(SupplierRequest, on_delete=models.PROTECT, related_name='commands')
    account = models.ForeignKey('mall.Account', on_delete=models.PROTECT)
    operation_id = models.UUIDField()
    payload_hash = models.CharField(max_length=64)
    result = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['order', 'account', 'operation_id'], name='unique_deal_command')]
