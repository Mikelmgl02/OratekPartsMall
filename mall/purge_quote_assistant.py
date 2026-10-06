"""Run with python -m mall.purge_quote_assistant [--dry-run], daily (see README): the retention cleanup of private pricing data.

1. Quotation assistant text: after 30 days a completed run keeps only its structure (proposal ids, kinds, lines, quantities and decisions,
   which the publication trace links) and its metrics; its summary, notes, terms texts, questions and the client's quoted evidence are
   blanked. A cached copy follows its source, so no copy outlives the 30 days of the text it repeats. Runs are never deleted.
2. Price imports: the staged batches of an expired job are deleted (they can no longer be applied), and the whole job 30 days after it
   expired (its correction workbook stays downloadable until then).
Idempotent: a second run finds nothing to do. Prints counts only, never text or prices.
"""
import argparse
import os
import sys
from datetime import timedelta

TEXT_DAYS = 30
IMPORT_KEEP_DAYS = 30
CHUNK = 500


def blanked(output):
    return {**output, 'summary': '', 'proposals': [{**proposal, 'text': '', 'evidence': ''} for proposal in output.get('proposals', [])]}


def purge(now=None, *, dry_run=False):
    from django.db import transaction
    from django.utils import timezone
    from .price_import_models import PriceImportBatch, PriceImportJob
    from .quote_assistant import PURGED_KEY
    from .quote_assistant_models import QuoteAssistantRun
    now = now or timezone.now()
    cutoff = now - timedelta(days=TEXT_DAYS)
    unpurged = QuoteAssistantRun.objects.filter(status='completed').exclude(metrics__has_key=PURGED_KEY)
    expired = set(unpurged.filter(created_at__lt=cutoff).values_list('pk', flat=True))
    # A younger cached copy repeats the text of its source, so it is purged with the source.
    copies = {run.pk: run.metrics.get('cached_from') for run in unpurged.filter(cached=True, created_at__gte=cutoff).only('id', 'metrics')}
    old_sources = {str(pk) for pk in QuoteAssistantRun.objects.filter(pk__in={source for source in copies.values() if source}, created_at__lt=cutoff)
                   .values_list('pk', flat=True)}
    expired |= {pk for pk, source in copies.items() if source in old_sources}
    jobs = PriceImportJob.objects.filter(expires_at__lt=now - timedelta(days=IMPORT_KEEP_DAYS))
    batches = PriceImportBatch.objects.filter(job__expires_at__lt=now)
    counts = {'runs': len(expired), 'batches': batches.count(), 'jobs': jobs.count()}
    if dry_run:
        return counts
    ids = sorted(expired)
    for start in range(0, len(ids), CHUNK):
        with transaction.atomic():
            runs = list(QuoteAssistantRun.objects.select_for_update().filter(pk__in=ids[start:start + CHUNK]).exclude(metrics__has_key=PURGED_KEY))
            for run in runs:
                run.output, run.metrics = blanked(run.output), {**run.metrics, PURGED_KEY: now.isoformat()}
            QuoteAssistantRun.objects.bulk_update(runs, ['output', 'metrics'])
    with transaction.atomic():
        batches.delete()
        jobs.delete()
    return counts


def render(counts, dry_run=False):
    prefix = 'Por limpiar' if dry_run else 'Limpieza completada'
    return (f"{prefix}: {counts['runs']} interpretaciones del asistente con texto de más de {TEXT_DAYS} días · {counts['batches']} lotes de "
            f"importaciones de precios vencidas · {counts['jobs']} importaciones vencidas hace más de {IMPORT_KEEP_DAYS} días.")


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Borra el texto de las interpretaciones del asistente de más de 30 días y las importaciones de precios vencidas.')
    parser.add_argument('--dry-run', action='store_true', help='Solo cuenta lo que se limpiaría.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    print(render(purge(dry_run=options.dry_run), options.dry_run), file=stdout or sys.stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
