"""AI quotation assistant runs and AI usage. Supplier-private: never read by client-facing payloads and never registered in
Django admin. A run stores validated proposals only (no price field exists anywhere in them); usage rows feed the AI budget."""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from .pricing_models import choice_values

RUN_STATUS_CHOICES = [('claimed', 'En curso'), ('completed', 'Completada'), ('failed', 'Fallida')]


class QuoteAssistantRun(models.Model):
    """One interpretation of the client's texts for a draft revision. The id is the client's run_id (retries are idempotent); a completed
    run with the same input fingerprint younger than 30 days serves as the cache, so there is no separate cache table."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey('mall.SupplierRequest', on_delete=models.PROTECT, related_name='assistant_runs')
    supplier = models.ForeignKey('mall.Account', on_delete=models.PROTECT, related_name='+')
    draft_version = models.PositiveIntegerField(default=0)
    base_quotation = models.ForeignKey('mall.DealQuotation', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    input_fingerprint = models.CharField(max_length=64)
    engine_version = models.CharField(max_length=30, default='quote-assistant-v1')
    model = models.CharField(max_length=120, blank=True, default='')
    status = models.CharField(max_length=10, choices=RUN_STATUS_CHOICES, default='claimed')
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    # Served from an earlier completed run with the same input: no provider call, no cost, outside the run caps.
    cached = models.BooleanField(default=False)
    # The provider answered or may have billed the call (a truncated answer is charged at the estimate): counts toward the run caps.
    billable = models.BooleanField(default=False)
    output = models.JSONField(default=dict, blank=True)
    decisions = models.JSONField(default=dict, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    estimated_cost_micro_usd = models.BigIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', 'id']
        constraints = [models.CheckConstraint(condition=Q(status__in=choice_values(RUN_STATUS_CHOICES)), name='valid_quote_assistant_run_status'),
                       models.CheckConstraint(condition=Q(estimated_cost_micro_usd__gte=0), name='valid_quote_assistant_run_cost')]
        indexes = [models.Index(fields=['order', '-created_at'], name='assistant_run_order_idx'),
                   models.Index(fields=['input_fingerprint', 'status'], name='assistant_run_cache_idx')]


class AIUsageRecord(models.Model):
    """Daily AI usage per account and feature (account null: platform-level features). Updated with F() expressions."""
    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey('mall.Account', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    feature = models.CharField(max_length=40)
    day = models.DateField()
    calls = models.PositiveIntegerField(default=0)
    cached_calls = models.PositiveIntegerField(default=0)
    failed_calls = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    thinking_tokens = models.PositiveBigIntegerField(default=0)
    cost_micro_usd = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'feature', 'day'], condition=Q(account__isnull=False), name='unique_ai_usage_account_day'),
                       models.UniqueConstraint(fields=['feature', 'day'], condition=Q(account__isnull=True), name='unique_ai_usage_platform_day')]
        indexes = [models.Index(fields=['feature', 'day'], name='ai_usage_feature_day_idx')]
