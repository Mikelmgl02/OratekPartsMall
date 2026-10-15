"""Durable matching work, review evidence and supplier-specific learned mappings."""
import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


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
    """A decision the matching leaves to a person (inventory refactor S6, plan §2; mall.reference_matching writes the new kinds).

    kind: reference (a code the supplier declares for its item, rule 5b), code_change (the supplier changed its item's code, rule 5b),
    variant (an item contradicts the line its code joins, rule 5), line (the item's code is on two or more lines, or on a Reference in
    disputa: the item waits without a Reference, rule 2), merge (two or more lines may be one part: rule 8, catalog references, OD15,
    the legacy-auto-merge batch). S10's 0086 converted the kinds before S6 (supplier, catalog, catalog_reference, seed).
    status: review, dismissed, applied (unmatched and resolved converted by 0086). key (unique) makes every writer idempotent:
    reference:<item>:<clave>|<brand> · code_change:<item> · variant:<item>:<line> · line:<item> · merge:<sorted line ids> ·
    merge:<batch_key>. A dismissed case whose fingerprint has not changed stays dismissed."""
    KINDS = ('reference', 'code_change', 'variant', 'line', 'merge')
    LEGACY_KINDS = ('supplier', 'catalog', 'catalog_reference', 'seed')
    STATUSES = ('review', 'dismissed', 'applied')
    LEGACY_STATUSES = ('unmatched', 'resolved')
    ROUTES = [('lote', 'Lote'), ('caso', 'Caso')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=240, unique=True)
    kind = models.CharField(max_length=20)  # mall.reference_matching.KINDS (a CHECK from 0089)
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
    # S6 (0083): the target's fields.
    reference = models.ForeignKey('mall.Reference', null=True, blank=True, on_delete=models.SET_NULL, related_name='cases',
                                  verbose_name='referencia')
    new_code = models.CharField(max_length=120, blank=True, default='', db_default='', verbose_name='código propuesto')
    new_brand = models.CharField(max_length=120, blank=True, default='', db_default='', verbose_name='marca propuesta')
    lines = models.ManyToManyField('mall.Part', blank=True, related_name='matching_cases', verbose_name='líneas')
    evidence = models.JSONField(default=dict, blank=True, db_default={}, verbose_name='evidencia')
    guard = models.CharField(max_length=40, blank=True, default='', db_default='', verbose_name='control que lo detuvo')
    route = models.CharField(max_length=5, blank=True, default='', db_default='', choices=ROUTES, verbose_name='ruta')
    batch_key = models.CharField(max_length=120, blank=True, default='', db_default='', db_index=True, verbose_name='lote')
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                   verbose_name='decidido por')
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name='decidido el')
    created_at = models.DateTimeField(null=True, blank=True, default=timezone.now, verbose_name='creado el')

    class Meta:
        ordering = ['-updated_at', 'id']
        indexes = [models.Index(fields=['kind', 'status'], name='matching_case_kind_status')]
        # From 0089 (S10): the S6 kinds and statuses only (0086 converted the legacy ones).
        constraints = [models.CheckConstraint(condition=models.Q(kind__in=['reference', 'code_change', 'variant', 'line', 'merge']),
                                              name='matching_case_valid_kind'),
                       models.CheckConstraint(condition=models.Q(status__in=['review', 'dismissed', 'applied']), name='matching_case_valid_status')]


class MatchingDecision(models.Model):
    case = models.ForeignKey(MatchingCase, null=True, on_delete=models.SET_NULL)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=30)
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
