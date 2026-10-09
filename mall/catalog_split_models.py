"""Separar SKU (mall.catalog_split): the audit of each split and the pairs of SKUs it keeps apart."""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q


class CatalogSplit(models.Model):
    """One split: the retired SKUs brought back out of a canonical SKU and the rows each took back, so it can be traced."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.ForeignKey('mall.Part', on_delete=models.PROTECT, related_name='catalog_splits', verbose_name='SKU separado')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    restored = models.JSONField(default=list)  # [{'id', 'sku'}]
    # {'parts': {part id: {'codes': [...], 'items': [...], 'images': [...]}}, 'aliases_removed': [{'brand', 'code'}], 'links': {...}}
    moved = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
    reason = models.CharField(max_length=500, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'separación de SKU'
        verbose_name_plural = 'separaciones de SKU'


class CatalogSeparation(models.Model):
    """Two SKUs a split separated (first < second): the matching worker never merges them on its own again. A person still can, with
    Agrupar SKU, which shows the separation as a warning."""
    first = models.ForeignKey('mall.Part', on_delete=models.CASCADE, related_name='+')
    second = models.ForeignKey('mall.Part', on_delete=models.CASCADE, related_name='+')
    split = models.ForeignKey(CatalogSplit, on_delete=models.CASCADE, related_name='separations')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['first', 'second'], name='catalog_separation_unique'),
                       models.CheckConstraint(condition=Q(first__lt=F('second')), name='catalog_separation_ordered')]
