"""OEM finder runs, the 'Revisión OEM' queue (phase 2) and the auto-apply audit (phase 2B). New tables only.

OEMFinderRun records one pass (mode, rules and suffix table versions, scope, tier counts, timings). OEMReviewCase holds one
review-tier proposal per Part; its fingerprint covers the finder version, the Part snapshot, the candidate, the tier, the
blockers and the suffix entries the decision read, so a re-run keeps every decision whose evidence did not change.
A review candidate is never a PartCode: reconcile_identities would promote a verified OEM PartCode on its own.
"""
from django.conf import settings
from django.db import models

RUN_MODES = [('dry_run', 'Simulación'), ('apply_auto', 'Aplicación automática'), ('ai', 'IA')]
RUN_STATUSES = [('running', 'En curso'), ('completed', 'Completada'), ('failed', 'Fallida')]
CASE_STATUSES = [('review', 'Por revisar'), ('applied', 'Aplicado'), ('dismissed', 'Descartado'), ('resolved', 'Resuelto')]


class OEMFinderRun(models.Model):
    mode = models.CharField(max_length=12, choices=RUN_MODES, default='dry_run')
    status = models.CharField(max_length=10, choices=RUN_STATUSES, default='running')
    rules_version = models.CharField(max_length=40)
    suffix_table_version = models.CharField(max_length=40, blank=True, default='')
    # {in_stock_first, limit, tier, stage}: the selection whose review cases this run synced; tiers are always computed globally.
    scope = models.JSONField(default=dict, blank=True)
    # {tiers: {T: [all, in_stock]}, scope: {T: n}, scenario: {...}, cases: {created, updated, unchanged, resolved}, ...}
    counts = models.JSONField(default=dict, blank=True)
    applied = models.JSONField(default=dict, blank=True)  # apply_auto (phase 2B): per-tier applied rows; empty for a dry run
    errors = models.JSONField(default=list, blank=True)
    timings = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at', '-id']


class OEMReviewCase(models.Model):
    part = models.OneToOneField('mall.Part', on_delete=models.CASCADE, related_name='oem_review')
    run = models.ForeignKey(OEMFinderRun, on_delete=models.SET_NULL, null=True, blank=True, related_name='cases')
    fingerprint = models.CharField(max_length=64)
    tier = models.CharField(max_length=30, db_index=True)
    tier_rank = models.PositiveSmallIntegerField(default=0)
    underlying_tier = models.CharField(max_length=30, blank=True, default='')
    candidate = models.CharField(max_length=200, blank=True, default='')  # proposed MAIN, manufacturer canonical form
    written_form = models.CharField(max_length=200, blank=True, default='')  # as the catalog writes it (kept as an alterno)
    brand = models.CharField(max_length=40, blank=True, default='')
    system = models.CharField(max_length=20, blank=True, default='')
    grade = models.PositiveSmallIntegerField(default=0)
    chain = models.CharField(max_length=300, blank=True, default='')
    blockers = models.JSONField(default=list, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    snapshot = models.JSONField(default=dict, blank=True)  # the Part as evaluated; an apply re-checks it
    in_stock = models.BooleanField(default=False)
    suffix_table_version = models.CharField(max_length=40, blank=True, default='')
    status = models.CharField(max_length=10, choices=CASE_STATUSES, default='review', db_index=True)
    ai = models.JSONField(default=dict, blank=True)  # reserved: AI fallback payload (phase 2D)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision = models.JSONField(default=dict, blank=True)  # reserved: review action and note (phase 2C)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-in_stock', 'tier_rank', 'id']
        indexes = [models.Index(fields=['status', 'tier_rank', '-in_stock'], name='oem_case_queue_idx'),
                   models.Index(fields=['tier', 'brand', 'chain'], name='oem_case_batch_idx')]


# Phase 2B (auto-apply with audit and undo). New tables only: CatalogIdentityChange, Part and PartCode are never altered.
APPLY_TIERS = [('AUTO_FLAG_CURRENT', 'El SKU ya es el OEM'), ('AUTO_RENAME_BASE', 'Renombrar a la base OEM'),
               ('STRONG_PENDING_OWNER_TAGS', 'Renombrar con etiquetas confirmadas por el propietario')]
APPLY_METHODS = [('flag_current', 'Marcar el SKU actual como OEM'), ('rename_base', 'Renombrar a la base OEM'),
                 ('rename_confirmed_tags', 'Renombrar a la base OEM (etiquetas confirmadas)')]


class OEMFinderChange(models.Model):
    """One catalog identity change written by an apply_auto run: the CatalogIdentityChange it links, the run, the method, the
    finder's evidence and what undoing it needs (undo: the old SKU, name and OEM flag plus the PartCodes the run created or
    retyped). A revert writes a reverse CatalogIdentityChange and stamps reverted_at; it never deletes this row."""
    change = models.OneToOneField('mall.CatalogIdentityChange', on_delete=models.PROTECT, related_name='oem_finder')
    run = models.ForeignKey(OEMFinderRun, on_delete=models.PROTECT, related_name='changes')
    part = models.ForeignKey('mall.Part', on_delete=models.PROTECT, related_name='oem_finder_changes')
    tier = models.CharField(max_length=30, choices=APPLY_TIERS)
    method = models.CharField(max_length=24, choices=APPLY_METHODS)
    batch = models.PositiveIntegerField(default=1)
    code = models.CharField(max_length=120)  # the OEM MAIN written (manufacturer form)
    brand = models.CharField(max_length=120)
    evidence = models.JSONField(default=dict, blank=True)
    undo = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reverted_at = models.DateTimeField(null=True, blank=True)
    reverted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    revert_change = models.OneToOneField('mall.CatalogIdentityChange', on_delete=models.PROTECT, null=True, blank=True,
                                         related_name='oem_finder_revert')

    class Meta:
        ordering = ['run', 'batch', 'id']
        indexes = [models.Index(fields=['part', 'reverted_at'], name='oem_change_part_idx')]


class OEMApplyHalt(models.Model):
    """Ops flag of the halt rule: while one is active (cleared_at empty) no apply_auto run starts and a running one stops before
    its next batch. A run with unexpected errors raises one itself."""
    reason = models.CharField(max_length=300)
    run = models.ForeignKey(OEMFinderRun, on_delete=models.SET_NULL, null=True, blank=True, related_name='halts')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    cleared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
