"""Durable matching work, review evidence and supplier-specific learned mappings."""
import uuid
from django.conf import settings
from django.db import models


class MatchingQueue(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    requested = models.PositiveIntegerField(default=0)
    completed = models.PositiveIntegerField(default=0)
    requested_at = models.DateTimeField(auto_now=True)
    lease = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    summary = models.JSONField(default=dict)
    stage = models.CharField(max_length=30, blank=True, default='')
    started_at = models.DateTimeField(null=True)
    error = models.TextField(blank=True)


class MatchingCase(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=240, unique=True)
    kind = models.CharField(max_length=20)  # supplier or catalog
    item = models.ForeignKey('mall.SupplierItem', null=True, on_delete=models.CASCADE)
    fingerprint = models.CharField(max_length=64)
    sources = models.JSONField(default=list)
    candidates = models.JSONField(default=list)
    target_sku = models.CharField(max_length=200, blank=True)
    method = models.CharField(max_length=40)
    reason = models.TextField()
    status = models.CharField(max_length=20, default='review', db_index=True)
    ai = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', 'id']


class SupplierCodeMapping(models.Model):
    """A reviewed local code is not automatically a universal OEM alias."""
    supplier = models.ForeignKey('mall.Account', on_delete=models.CASCADE)
    code = models.CharField(max_length=120)
    brand = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    part = models.ForeignKey('mall.Part', on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['supplier', 'code', 'brand'], name='unique_supplier_code_mapping')]


class MatchingDecision(models.Model):
    case = models.ForeignKey(MatchingCase, null=True, on_delete=models.SET_NULL)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=30)
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
