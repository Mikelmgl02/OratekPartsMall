"""Durable, account-bound supplier balance import drafts."""
import uuid

from django.conf import settings
from django.db import models


class SupplierInventoryImportJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT)
    filename = models.CharField(max_length=255)
    status = models.CharField(max_length=20, default='ready')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    summary = models.JSONField(default=dict)
    applied_summary = models.JSONField(default=dict)
    preview = models.JSONField(default=dict)
    rejected_rows = models.JSONField(default=list)
    completed_batches = models.PositiveIntegerField(default=0)
    total_batches = models.PositiveIntegerField(default=0)
    total_rows = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)


class SupplierInventoryImportBatch(models.Model):
    job = models.ForeignKey(SupplierInventoryImportJob, on_delete=models.CASCADE, related_name='batches')
    index = models.PositiveIntegerField()
    data = models.JSONField(default=list)
    fingerprint = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['job', 'index'], name='unique_supplier_inventory_import_batch')]
