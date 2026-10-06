"""Durable AI proposals. Catalog writes still use the reviewed import plan."""
import uuid

from django.db import models


class CatalogClassificationState(models.Model):
    job = models.OneToOneField('mall.CatalogImportJob', on_delete=models.CASCADE, related_name='classification')
    source_fingerprint = models.CharField(max_length=64)
    source_skus = models.JSONField(default=list)
    model = models.CharField(max_length=120)
    total_batches = models.PositiveIntegerField(default=0)
    completed_batches = models.PositiveIntegerField(default=0)
    claim_id = models.UUIDField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CatalogClassificationSuggestion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey('mall.CatalogImportJob', on_delete=models.CASCADE, related_name='classification_suggestions')
    source_sku = models.CharField(max_length=200)
    source_row = models.PositiveIntegerField()
    target_sku = models.CharField(max_length=200)
    category = models.CharField(max_length=120, blank=True)
    subcategory = models.CharField(max_length=120, blank=True)
    reason = models.TextField()
    confidence = models.FloatField(default=0)
    needs_review = models.BooleanField(default=True)
    evidence = models.JSONField(default=dict)
    candidate_skus = models.JSONField(default=list)
    applied = models.BooleanField(default=False)
    applied_decision = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['source_row', 'source_sku']
        constraints = [models.UniqueConstraint(fields=['job', 'source_sku'], name='unique_import_ai_source')]
