"""Durable, owner-bound supplier price import drafts.

Separate tables from the stock import on purpose: matching (apply_item, reconcile_items, supplier families) treats a running
SupplierInventoryImportJob as "supplier busy", and a price import must never pause matching. Supplier-private: not registered
in admin and never read by client-facing serializers.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

PRICE_IMPORT_STATUSES = [('ready', 'Lista para aplicar'), ('review', 'Sin cambios aplicables'), ('importing', 'Aplicando'), ('completed', 'Completada')]


class PriceImportJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='price_import_jobs')
    filename = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=PRICE_IMPORT_STATUSES, default='ready')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    summary = models.JSONField(default=dict)
    applied_summary = models.JSONField(default=dict)
    preview = models.JSONField(default=dict)
    rejected_rows = models.JSONField(default=list)
    # [{code, name, currency, is_default}]: created once, inside the transaction of batch 0, after confirm_new_lists.
    lists_to_create = models.JSONField(default=list)
    big_change_acknowledged = models.BooleanField(default=False)
    completed_batches = models.PositiveIntegerField(default=0)
    total_batches = models.PositiveIntegerField(default=0)
    total_rows = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(status__in=[value for value, _ in PRICE_IMPORT_STATUSES]), name='valid_price_import_status')]


class PriceImportBatch(models.Model):
    job = models.ForeignKey(PriceImportJob, on_delete=models.CASCADE, related_name='batches')
    index = models.PositiveIntegerField()
    data = models.JSONField(default=list)
    fingerprint = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['job', 'index'], name='unique_price_import_batch')]
