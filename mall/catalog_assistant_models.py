"""Saved reviews for AI classification of the existing catalog."""
import uuid
from django.conf import settings
from django.db import models


class CatalogAssistantJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    scope = models.CharField(max_length=20, choices=[('unclassified', 'Sin clasificar'), ('all', 'Todo')])
    search = models.CharField(max_length=200, blank=True)
    instructions = models.CharField(max_length=2000, blank=True)
    task = models.CharField(max_length=20, default='grouping')
    batch_size = models.PositiveIntegerField(default=50)
    metrics = models.JSONField(default=dict)
    source_ids = models.JSONField(default=list)
    model = models.CharField(max_length=120)
    completed_batches = models.PositiveIntegerField(default=0)
    claim_id = models.UUIDField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class CatalogAssistantSuggestion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(CatalogAssistantJob, on_delete=models.CASCADE, related_name='suggestions')
    source = models.ForeignKey('mall.Part', on_delete=models.PROTECT)
    position = models.PositiveIntegerField()
    proposal = models.JSONField(default=dict)
    context = models.JSONField(default=dict)
    status = models.CharField(max_length=10, default='pending', choices=[('pending', 'Pendiente'), ('applied', 'Aplicado'), ('dismissed', 'Descartado')])
    decision = models.JSONField(default=dict)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['position']
        constraints = [models.UniqueConstraint(fields=['job', 'source'], name='unique_assistant_source')]


class CategorySuggestionCache(models.Model):
    """Validated category suggestions, never evidence that two parts are equivalent."""
    fingerprint = models.CharField(primary_key=True, max_length=64)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
