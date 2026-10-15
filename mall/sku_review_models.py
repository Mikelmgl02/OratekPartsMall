"""Revisión IA of Productos nuevos (mall.sku_review): a run over the products waiting for a decision and the verdict it gave each one.
From inventory refactor S6 a product is a line in preparación (mall.new_products): a run's proposal_ids are line ids (the S6 data step
kept each pending proposal's id as its line's) and a verdict hangs from its line."""
import uuid

from django.conf import settings
from django.db import models


class SkuReviewRun(models.Model):
    """One review over a frozen list of products waiting for a decision (line ids from S6, proposal ids before), run batch by batch
    from the administrator's browser. Every superuser sees every run. claim_id/claimed_at hold the batch waiting for the AI, with its
    estimated cost against the monthly cap."""
    id = models.UUIDField(primary_key=True, editable=False, help_text='Lo genera el navegador: repetir la creación devuelve la misma revisión.')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+', verbose_name='iniciada por')
    search = models.CharField(max_length=200, blank=True, default='', verbose_name='búsqueda')
    only_unreviewed = models.BooleanField(default=True, verbose_name='solo las que no tenían revisión')
    proposal_ids = models.JSONField(default=list, verbose_name='propuestas a revisar')
    batch_size = models.PositiveIntegerField(default=40, verbose_name='propuestas por lote')
    completed_batches = models.PositiveIntegerField(default=0, verbose_name='lotes revisados')
    claim_id = models.UUIDField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    estimated_cost_micro_usd = models.BigIntegerField(default=0, verbose_name='costo estimado del lote en curso')
    model = models.CharField(max_length=120, blank=True, default='', verbose_name='modelo de IA')
    metrics = models.JSONField(default=dict, verbose_name='métricas')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='creada el')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='terminada el')

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'revisión IA de productos nuevos'
        verbose_name_plural = 'revisiones IA de productos nuevos'


class SkuReviewVerdict(models.Model):
    """What a run concluded about one product: good (approve it), bad (reject or link it) or unsure (a person decides), why, and what
    it filled in. It holds for the product's version after that fill (proposal_version: the line's version from S6); once the product
    changes it is stale («cambió»). From S6 it hangs from the line (line); the proposal the verdicts given before S6 named left with
    S10 (its table is archived; each of those verdicts has its line)."""
    VERDICTS = [('good', 'Buena'), ('bad', 'Mala'), ('unsure', 'Dudosa')]
    METHODS = [('rule', 'Regla'), ('ai', 'IA')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(SkuReviewRun, on_delete=models.CASCADE, related_name='verdicts', verbose_name='revisión')
    line = models.ForeignKey('mall.Part', on_delete=models.SET_NULL, null=True, blank=True, related_name='review_verdicts', verbose_name='línea')
    position = models.PositiveIntegerField(verbose_name='posición en la revisión')
    proposal_version = models.PositiveIntegerField(verbose_name='versión revisada')
    method = models.CharField(max_length=10, choices=METHODS, verbose_name='método')
    verdict = models.CharField(max_length=10, choices=VERDICTS, verbose_name='veredicto')
    reason = models.CharField(max_length=20, blank=True, default='', verbose_name='motivo de rechazo sugerido')
    confidence = models.FloatField(default=0, verbose_name='confianza')
    reasons = models.JSONField(default=list, verbose_name='razones')
    suggestions = models.JSONField(default=dict, verbose_name='sugerencias')
    applied = models.JSONField(default=dict, verbose_name='campos completados')
    evidence = models.JSONField(default=dict, verbose_name='evidencia revisada')
    link_part = models.ForeignKey('mall.Part', on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name='SKU existente sugerido')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='creado el')

    class Meta:
        ordering = ['position']
        verbose_name = 'veredicto de la revisión IA'
        verbose_name_plural = 'veredictos de la revisión IA'
        constraints = [
            models.UniqueConstraint(fields=['run', 'line'], name='sku_review_one_verdict_per_run_line'),
            models.CheckConstraint(condition=models.Q(verdict__in=['good', 'bad', 'unsure']), name='sku_review_valid_verdict'),
        ]
        indexes = [models.Index(fields=['line', '-created_at'], name='sku_review_line_latest')]
