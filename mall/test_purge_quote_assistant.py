"""S8 · retention cleanup (python -m mall.purge_quote_assistant): assistant text is blanked after 30 days, keeping what the trace links,
and expired price import staging is removed. The provider is always patched; no test calls Gemini."""
import io
import json
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from . import test_quote_assistant as assistant_tests
from .price_import_models import PriceImportBatch, PriceImportJob
from .purge_quote_assistant import main, purge
from .quote_assistant import PURGED_KEY, applied_runs
from .quote_assistant_models import QuoteAssistantRun
from .quote_draft_models import DealQuotationLineAudit
from .quote_drafts import PURGED_RUN_DETAIL


@override_settings(**assistant_tests.ENABLED)
class PurgeQuoteAssistantTests(APITestCase):
    Tests = assistant_tests.QuoteAssistantTests
    account, item, url, payload, submit, review, action = Tests.account, Tests.item, Tests.url, Tests.payload, Tests.submit, Tests.review, Tests.action
    messages, draft_url, save, by_code, setUp, get_draft, provider = Tests.messages, Tests.draft_url, Tests.save, Tests.by_code, Tests.setUp, Tests.get_draft, Tests.provider
    reviewed, assistant_url, start, decide, proposals, completed = Tests.reviewed, Tests.assistant_url, Tests.start, Tests.decide, Tests.proposals, Tests.completed
    del Tests

    def age(self, run_id, days):
        QuoteAssistantRun.objects.filter(pk=run_id).update(created_at=timezone.now() - timedelta(days=days))

    def text(self, run):
        run.refresh_from_db()
        return [run.output['summary']] + [value for proposal in run.output['proposals'] for value in (proposal['text'], proposal['evidence'])]

    def test_text_older_than_30_days_is_blanked_keeping_trace_links_decisions_and_metrics(self):
        order = self.reviewed()
        source_id = self.completed(order).data['latest_run']['id']
        found = {kind: proposal['id'] for kind, proposal in self.proposals(self.start(order)).items()}
        draft = self.decide(order, source_id, 0, [(found['quantity_change'], 'apply'), (found['line_note'], 'apply')]).data
        draft = self.save(order, draft['draft_version'], lines=[{'order_line_id': line['order_line_id'], 'unit_price': '5.00'} for line in draft['lines']]).data
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.action(order, 'quote', currency=draft['currency'], terms=draft['terms'], draft_version=draft['draft_version'],
                                     lines=[{'order_line_id': line['order_line_id'], 'quantity': line['quantity'], 'unit_price': '5.00'} for line in draft['lines']]).status_code, 200)
        source = QuoteAssistantRun.objects.get(pk=source_id)
        copy = QuoteAssistantRun.objects.get(cached=True)
        young = QuoteAssistantRun.objects.create(order=order, supplier=self.supplier_a, input_fingerprint='otra', status='completed', created_by=self.seller_a,
                                                 output={'summary': 'TEXTO RECIENTE', 'proposals': [{**source.output['proposals'][0], 'text': '', 'evidence': 'SOLO NECESITO 2'}]})
        # The source is 31 days old; its cached copy is younger but repeats the same text, so it goes with it.
        self.age(source_id, 31)
        self.age(copy.pk, 10)
        before = {run.pk: (run.output, run.decisions, run.metrics) for run in QuoteAssistantRun.objects.all()}
        links = applied_runs(order, None)
        self.assertEqual(purge(dry_run=True), {'runs': 2, 'batches': 0, 'jobs': 0})
        self.assertEqual({run.pk: (run.output, run.decisions, run.metrics) for run in QuoteAssistantRun.objects.all()}, before)
        self.assertEqual(purge(), {'runs': 2, 'batches': 0, 'jobs': 0})
        for run in (source, copy):
            self.assertEqual(set(self.text(run)), {''})
            self.assertEqual([{key: value for key, value in proposal.items() if key not in ('text', 'evidence')} for proposal in run.output['proposals']],
                             [{key: value for key, value in proposal.items() if key not in ('text', 'evidence')} for proposal in before[run.pk][0]['proposals']])
            self.assertEqual((run.decisions, {key: value for key, value in run.metrics.items() if key != PURGED_KEY}), before[run.pk][1:])
            self.assertIn(PURGED_KEY, run.metrics)
        self.assertNotIn('NECESITO', json.dumps([source.output, copy.output]))
        self.assertEqual(self.text(young), ['TEXTO RECIENTE', '', 'SOLO NECESITO 2'])
        # Nothing the publication trace links is deleted: the runs stay, and the trace still names the run that changed each line.
        self.assertEqual(QuoteAssistantRun.objects.count(), 3)
        self.assertEqual(applied_runs(order, None), links)
        self.assertEqual(set(DealQuotationLineAudit.objects.exclude(assistant_run=None).values_list('assistant_run_id', flat=True)), {source.pk})
        quotation = order.quotations.get()
        trace = self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/requests/{order.pk}/quotations/{quotation.pk}/trace/').data
        self.assertIn(str(source.pk), {line['assistant_run_id'] for line in trace['lines']})
        # Idempotent: a second purge finds nothing and changes nothing.
        after = {run.pk: (run.output, run.metrics) for run in QuoteAssistantRun.objects.all()}
        self.assertEqual(purge(), {'runs': 0, 'batches': 0, 'jobs': 0})
        self.assertEqual({run.pk: (run.output, run.metrics) for run in QuoteAssistantRun.objects.all()}, after)

    def test_a_purged_run_is_no_longer_shown_or_applied_and_the_ttl_is_respected(self):
        order = self.reviewed()
        run_id = self.completed(order).data['latest_run']['id']
        self.age(run_id, 29)
        self.assertEqual(purge(), {'runs': 0, 'batches': 0, 'jobs': 0}, 'A run younger than 30 days keeps its text.')
        self.assertEqual(self.get_draft(order)['assistant']['latest_run']['id'], run_id)
        self.age(run_id, 31)
        self.assertEqual(purge()['runs'], 1)
        draft = self.get_draft(order)
        self.assertIsNone(draft['assistant']['latest_run'])
        self.assertNotIn('client_price_request', [item['code'] for line in draft['lines'] for item in line['exceptions']])
        terms = next(proposal['id'] for proposal in QuoteAssistantRun.objects.get(pk=run_id).output['proposals'] if proposal['kind'] == 'terms')
        refused = self.decide(order, run_id, 0, [(terms, 'apply')])
        self.assertEqual((refused.status_code, refused.data['detail']), (409, PURGED_RUN_DETAIL))
        # A late retry of the same run_id does not bring the blanked run back either.
        replayed = self.start(order, 0, run_id)
        self.assertEqual((replayed.status_code, replayed.data['latest_run']), (200, None))

    def test_a_purged_output_is_never_served_from_the_cache(self):
        order = self.reviewed()
        run_id = self.completed(order).data['latest_run']['id']
        # Purged early (a retention window shorter than the cache's): the same texts are interpreted again, never answered with blank text.
        self.assertEqual(purge(timezone.now() + timedelta(days=31))['runs'], 1)
        self.calls = []
        fresh = self.completed(order)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(fresh.data['latest_run']['cached'])
        self.assertNotEqual(fresh.data['latest_run']['id'], run_id)
        self.assertTrue(fresh.data['latest_run']['summary'])

    def test_expired_price_import_staging_is_removed_and_the_command_prints_counts_only(self):
        now = timezone.now()
        def job(expires_days_ago):
            row = PriceImportJob.objects.create(owner=self.seller_a, supplier=self.supplier_a, filename='PRECIOS-SECRETO.xlsx', expires_at=now - timedelta(days=expires_days_ago),
                                                rejected_rows=[{'row': 3, 'values': {'PRECIO': '777.77'}}], total_batches=2)
            PriceImportBatch.objects.bulk_create([PriceImportBatch(job=row, index=index, data=[{'PRECIO': '777.77'}], fingerprint='f') for index in range(2)])
            return row
        active, expired, old = job(-3), job(2), job(31)
        out = io.StringIO()
        self.assertEqual(main(['--dry-run'], stdout=out), 0)
        self.assertEqual(out.getvalue().strip(), 'Por limpiar: 0 interpretaciones del asistente con texto de más de 30 días · 4 lotes de importaciones de precios '
                                                 'vencidas · 1 importaciones vencidas hace más de 30 días.')
        self.assertEqual(PriceImportBatch.objects.count(), 6)
        out = io.StringIO()
        main([], stdout=out)
        self.assertTrue(out.getvalue().startswith('Limpieza completada: 0 interpretaciones'))
        self.assertNotIn('777.77', out.getvalue())
        # The active job keeps its batches; an expired one keeps only its correction rows until 30 days after it expired.
        self.assertEqual(set(PriceImportJob.objects.values_list('pk', flat=True)), {active.pk, expired.pk})
        self.assertEqual(set(PriceImportBatch.objects.values_list('job_id', flat=True)), {active.pk})
        self.assertEqual(PriceImportJob.objects.get(pk=expired.pk).rejected_rows, [{'row': 3, 'values': {'PRECIO': '777.77'}}])
        self.assertFalse(PriceImportJob.objects.filter(pk=old.pk).exists())
        self.assertEqual(purge(), {'runs': 0, 'batches': 0, 'jobs': 0})
