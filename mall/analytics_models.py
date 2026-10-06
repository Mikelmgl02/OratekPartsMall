"""First-party usage records, independent of inventory and request state."""
import uuid

from django.conf import settings
from django.db import models


class UsageVisit(models.Model):
    key = models.UUIDField()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='usage_visits')
    started_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'key'], name='unique_user_usage_visit')]
        indexes = [models.Index(fields=['started_at'], name='usage_visit_started_idx')]


class UsageEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    visit = models.ForeignKey(UsageVisit, on_delete=models.CASCADE, related_name='events')
    account = models.ForeignKey('mall.Account', on_delete=models.SET_NULL, null=True)
    kind = models.CharField(max_length=16, choices=[('search', 'Búsqueda'), ('part_view', 'Vista de repuesto'), ('basket_add', 'Agregar a cesta')])
    term = models.CharField(max_length=200, blank=True)
    result_count = models.PositiveIntegerField(null=True)
    part = models.ForeignKey('mall.Part', on_delete=models.SET_NULL, null=True)
    sku = models.CharField(max_length=200, blank=True)
    supplier_item = models.ForeignKey('mall.SupplierItem', on_delete=models.SET_NULL, null=True)
    quantity = models.PositiveSmallIntegerField(default=0)
    fingerprint = models.CharField(max_length=64)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['kind', 'occurred_at'], name='usage_event_kind_date_idx'),
                   models.Index(fields=['part', 'occurred_at'], name='usage_event_part_date_idx')]
