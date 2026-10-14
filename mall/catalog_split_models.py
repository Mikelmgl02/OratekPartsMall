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
    """Two lines kept apart (first < second): a split separated them, or a person dismissed a compact_duplicate or shared_oem_number
    case (inventory refactor S2b: then case says which, and split is empty). The matching worker and mall.line_merge never merge them on
    their own again, checked on canonical lines (mall.line_merge.separated_pairs). A person still can, with Agrupar SKU, which shows
    the separation as a warning."""
    first = models.ForeignKey('mall.Part', on_delete=models.CASCADE, related_name='+')
    second = models.ForeignKey('mall.Part', on_delete=models.CASCADE, related_name='+')
    split = models.ForeignKey(CatalogSplit, on_delete=models.CASCADE, null=True, blank=True, related_name='separations')
    case = models.ForeignKey('mall.MatchingCase', on_delete=models.SET_NULL, null=True, blank=True, related_name='separations',
                             verbose_name='caso descartado')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                              verbose_name='decidido por')
    reason = models.CharField(max_length=40, blank=True, default='', db_default='', verbose_name='motivo')  # separar | compact_duplicate | shared_oem_number
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['first', 'second'], name='catalog_separation_unique'),
                       models.CheckConstraint(condition=Q(first__lt=F('second')), name='catalog_separation_ordered')]
