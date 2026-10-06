"""AI spend controls: a pre-call estimate, run caps, monthly budgets and usage recording. Amounts are integer micro-USD.

The token prices (GEMINI_INPUT_USD_PER_MTOK / GEMINI_OUTPUT_USD_PER_MTOK) are placeholders until the owner sets them from Google's current
price sheet. A feature with a monthly budget of 0 is off (403); a reached cap answers 429; nothing here stores or logs text.
"""
import math
from decimal import ROUND_CEILING, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import APIException

from .quote_assistant_models import AIUsageRecord, QuoteAssistantRun

ESTIMATED_OUTPUT_TOKENS = 1500
FEATURE_BUDGETS = {'quote_assistant': 'QUOTE_ASSISTANT_MONTHLY_USD'}
USAGE_FIELDS = ('calls', 'cached_calls', 'failed_calls', 'input_tokens', 'output_tokens', 'thinking_tokens', 'cost_micro_usd')


class AssistantDisabled(APIException):
    status_code = 403
    default_detail = 'El asistente de IA no está activado para tu cuenta. Puedes preparar la cotización sin él.'


class AssistantUnavailable(APIException):
    status_code = 503
    default_detail = 'El asistente de IA no está configurado. Puedes preparar la cotización sin él.'


class AssistantLimit(APIException):
    status_code = 429
    default_detail = 'El asistente alcanzó su límite de uso. El borrador y tus precios siguen disponibles.'


def micro_usd(amount):
    return int((Decimal(amount) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def token_cost(input_tokens, output_tokens, thinking_tokens=0):
    # USD per million tokens is micro-USD per token; thinking tokens are counted at the output rate.
    cost = Decimal(input_tokens) * settings.GEMINI_INPUT_USD_PER_MTOK + Decimal(output_tokens + thinking_tokens) * settings.GEMINI_OUTPUT_USD_PER_MTOK
    return int(cost.to_integral_value(rounding=ROUND_CEILING))


def estimate_cost(characters, output_tokens=ESTIMATED_OUTPUT_TOKENS):
    """Before calling: characters / 4 input tokens plus the full output allowance, at the configured rates."""
    return token_cost(math.ceil(characters / 4), output_tokens)


def feature_budget(feature):
    return micro_usd(getattr(settings, FEATURE_BUDGETS[feature]))


def month_spend(feature=None, today=None):
    rows = AIUsageRecord.objects.filter(day__gte=(today or timezone.localdate()).replace(day=1))
    return (rows.filter(feature=feature) if feature else rows).aggregate(total=Sum('cost_micro_usd'))['total'] or 0


def in_flight(now):
    """Estimated cost of assistant runs still waiting for the provider."""
    return QuoteAssistantRun.objects.filter(status='claimed', claim_expires_at__gt=now).aggregate(total=Sum('estimated_cost_micro_usd'))['total'] or 0


def check_monthly(feature, estimate, now):
    """Refuses when this month's recorded spend plus in-flight claims plus the estimate exceeds the feature cap or the platform cap."""
    cap = feature_budget(feature)
    if cap <= 0:
        raise AssistantDisabled()
    pending = in_flight(now) + estimate
    if month_spend(feature) + pending > cap or month_spend() + pending > micro_usd(settings.AI_MONTHLY_BUDGET_USD):
        raise AssistantLimit()


def counted_runs(now):
    """Runs that count toward the caps: in flight, or answered by the provider (tokens consumed). Cached runs never count."""
    return QuoteAssistantRun.objects.filter(Q(status='claimed', claim_expires_at__gt=now) | Q(billable=True))


def revision_runs(order, base_quotation, now):
    return counted_runs(now).filter(order=order, base_quotation=base_quotation).count()


def check_run_caps(order, supplier, base_quotation, now):
    """At most QUOTE_ASSISTANT_RUNS_PER_REVISION runs per draft revision and QUOTE_ASSISTANT_DAILY_RUNS_PER_ACCOUNT per supplier and day."""
    if revision_runs(order, base_quotation, now) >= settings.QUOTE_ASSISTANT_RUNS_PER_REVISION:
        raise AssistantLimit(f'Ya usaste las {settings.QUOTE_ASSISTANT_RUNS_PER_REVISION} interpretaciones de esta versión. '
                             'El borrador y tus precios siguen disponibles.')
    start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if counted_runs(now).filter(supplier=supplier, created_at__gte=start).count() >= settings.QUOTE_ASSISTANT_DAILY_RUNS_PER_ACCOUNT:
        raise AssistantLimit('Tu cuenta alcanzó el límite diario del asistente. El borrador y tus precios siguen disponibles.')


def record_usage(feature, account=None, *, day=None, **values):
    """Adds to the day's usage row of (account, feature) with F() expressions in a short transaction; account None is platform usage."""
    values = {field: values.get(field, 0) for field in USAGE_FIELDS}
    increments = {field: F(field) + value for field, value in values.items()}
    day = day or timezone.localdate()
    with transaction.atomic():
        rows = AIUsageRecord.objects.filter(account=account, feature=feature, day=day)
        if rows.update(**increments):
            return
        try:
            with transaction.atomic():
                AIUsageRecord.objects.create(account=account, feature=feature, day=day, **values)
                return
        except IntegrityError:
            rows.update(**increments)
