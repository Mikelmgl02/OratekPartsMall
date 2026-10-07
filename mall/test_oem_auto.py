import io
import threading
import uuid
from collections import Counter
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from . import oem_apply as oa
from . import oem_auto as au
from . import oem_finder as of
from . import oem_review as rv
from .matching_queue import MATCHING_LOCK
from .models import CatalogIdentityChange, Part, PartCode, User
from .oem_finder_models import OEMAutoCandidate, OEMFinderChange, OEMFinderRun, OEMReviewCase
from .test_oem_apply import CONFIRMED, FLAG, RENAME, Catalog

URL = '/api/v1/management/oem-finder/pending/'


class Pending(Catalog):
    def row(self, sku):
        return OEMAutoCandidate.objects.get(part=self.parts[sku])

    def statuses(self):
        return dict(Counter(OEMAutoCandidate.objects.values_list('status', flat=True)))

    def assert_mirrors_apply(self):
        """The pending rows of each apply tier are exactly what apply_auto would take now."""
        for tier in oa.APPLY_TIERS:
            outcome = oa.apply_auto(tier=tier, read_only=True)
            pending = set(OEMAutoCandidate.objects.filter(status='pending', apply_tier=tier).values_list('part_id', flat=True))
            self.assertEqual({str(p) for p in pending}, {pid for pid, _ in outcome['rows']}, tier)


class RefreshTests(Pending, TestCase):
    def test_a_dry_run_fills_the_pending_rows_and_repeating_it_writes_nothing(self):
        self.stock('54830-2H000-MOBIS', 2)
        self.assertFalse(OEMAutoCandidate.objects.exists())
        run = of.execute(limit=1)['run']  # the scope narrows review cases only: AUTO tiers are always global
        rows = {c.part.sku: c for c in OEMAutoCandidate.objects.select_related('part')}
        self.assertEqual(len(rows), 13)
        self.assertEqual(Counter(c.tier for c in rows.values()), {FLAG: 11, RENAME: 2})
        self.assertEqual(Counter(c.apply_tier for c in rows.values()), {FLAG: 11, RENAME: 1, CONFIRMED: 1})
        g, mobis = rows['48654-0K030-G'], rows['54830-2H000-MOBIS']
        self.assertEqual((g.candidate, g.brand, g.chain, g.owner_tags, g.status, g.run, g.system), ('48654-0K030', 'TOYOTA', 'G', ['G'], 'pending', run, 'TOYOTA'))
        self.assertEqual((g.chain_tokens[0]['tok'], g.chain_tokens[0]['class']), ('G', 'TAG'))
        self.assertEqual((mobis.candidate, mobis.owner_tags, mobis.in_stock, mobis.available_quantity, mobis.makes), ('54830-2H000', [], True, 2, ['HYUNDAI']))
        self.assertEqual(rows['48654-30030'].evidence['primary']['lexicon']['result'], 'agree_strong')
        self.assertEqual((rows['48654-30030'].rules_version, rows['48654-30030'].snapshot['sku']), (of.OEM_FINDER_VERSION, '48654-30030'))
        self.assertEqual(run.counts['auto']['statuses'], {'pending': 13})
        self.assertEqual(run.counts['auto']['pending'], {FLAG: 11, RENAME: 1, CONFIRMED: 1})
        self.assert_mirrors_apply()
        self.assertFalse(OEMReviewCase.objects.filter(part__in=[c.part for c in rows.values()]).exists())  # AUTO rows are never review cases
        stamps = dict(OEMAutoCandidate.objects.values_list('pk', 'updated_at'))
        again = of.execute()['counts']['auto']
        self.assertEqual((again['created'], again['updated'], again['unchanged'], again['moved']), (0, 0, 13, 0))
        self.assertEqual(dict(OEMAutoCandidate.objects.values_list('pk', 'updated_at')), stamps)

    def test_a_read_only_dry_run_writes_nothing(self):
        of.execute(store=False)
        self.assertFalse(OEMAutoCandidate.objects.exists())

    def test_an_apply_run_moves_its_rows_out_of_the_pending_list(self):
        outcome = self.canary(FLAG, 3)  # no dry run before: the apply run's own refresh fills the table
        statuses = outcome['auto']['statuses']
        self.assertEqual((statuses['applied'], sum(statuses.values()), outcome['run'].counts['auto']['statuses']), (3, 13, statuses))
        applied = set(OEMAutoCandidate.objects.filter(status='applied').values_list('part_id', flat=True))
        self.assertEqual(applied, set(outcome['run'].changes.values_list('part_id', flat=True)))
        self.assertEqual(set(OEMAutoCandidate.objects.filter(status='applied').values_list('run', flat=True)), {outcome['run'].pk})
        # graded again after the writes: the flagged SKUs still teach the class lexicon, so no neighbour leaves AUTO
        self.assertEqual(statuses, {'applied': 3, 'pending': 10})
        self.assert_mirrors_apply()
        of.execute()  # the applied SKUs are OEM now: the dry run no longer grades them, they stay applied
        self.assertEqual(self.statuses(), statuses)

    def test_a_reverted_row_never_comes_back_as_pending(self):
        run = self.canary(RENAME)['run']
        self.assertEqual(self.row('54830-2H000-MOBIS').status, 'applied')
        oa.revert_run(run.pk)
        row = self.row('54830-2H000-MOBIS')
        self.assertEqual((row.status, row.reason), ('excluded', 'previously_reverted'))
        of.execute()
        row = self.row('54830-2H000-MOBIS')
        self.assertEqual((row.status, row.reason), ('excluded', 'previously_reverted'))
        preview = au.preview(RENAME, 'canary')
        self.assertEqual((preview['selected'], preview['excluded']), (0, {'previously_reverted': 1}))
        self.assertEqual(self.canary(RENAME)['summary']['applied'], 0)

    def test_reverting_a_large_run_excludes_every_row(self):
        run, now = OEMFinderRun.objects.create(mode='apply_auto', rules_version=of.OEM_FINDER_VERSION), timezone.now()
        parts = Part.objects.bulk_create([Part(sku=f'90000-{i:05d}', description='PRUEBA') for i in range(450)])
        changes = CatalogIdentityChange.objects.bulk_create([CatalogIdentityChange(part=p, previous_sku=p.sku, sku=p.sku) for p in parts])
        links = OEMFinderChange.objects.bulk_create([OEMFinderChange(change=c, run=run, part=p, tier=FLAG, method='flag_current', code=p.sku, brand='TOYOTA',
                                                                     reverted_at=now) for p, c in zip(parts, changes)])
        OEMAutoCandidate.objects.bulk_create([OEMAutoCandidate(part=p, tier=FLAG, apply_tier=FLAG, sku=p.sku, candidate=p.sku, fingerprint='x', rules_version='v',
                                                               status='applied' if i % 2 else 'pending') for i, p in enumerate(parts)])
        self.assertEqual(au.mark_reverted([link.pk for link in links]), 450)  # chunked: one OR chain of 450 pairs is never built
        self.assertEqual((self.statuses(), set(OEMAutoCandidate.objects.values_list('reason', flat=True))), ({'excluded': 450}, {'previously_reverted'}))

    def test_an_open_apply_conflict_is_excluded(self):
        other = Part.objects.create(sku='KIT-OTRO', description='KIT VARIOS')
        PartCode.objects.create(part=other, code='54830-2H000MOBIS', brand='ACME', ref_type='company')  # the old SKU, normalized
        self.assertEqual(self.canary(RENAME)['summary']['conflicts'], 1)
        row = self.row('54830-2H000-MOBIS')
        self.assertEqual((row.status, row.reason), ('excluded', 'open_conflict'))
        of.execute()
        self.assertEqual(self.row('54830-2H000-MOBIS').status, 'excluded')

    def test_a_rules_bump_refreshes_every_graded_row_and_keeps_its_status(self):
        run = self.canary(FLAG, 2)['run']
        reverted = run.changes.order_by('pk').first().part
        oa.revert_run(run.pk, part=reverted.pk)
        au.send_to_review([self.parts['48654-30030'].pk], self.root, note='REVISAR')
        Part.objects.filter(pk=self.parts['48654-42010'].pk).update(description='BASE AMORT TOY RAV4 COMPLETO')  # CURRENT_REVIEW: stale
        of.execute()
        before = dict(OEMAutoCandidate.objects.values_list('part__sku', 'status'))
        self.assertEqual(Counter(before.values()), {'pending': 9, 'applied': 1, 'excluded': 1, 'sent_to_review': 1, 'stale': 1})
        with patch.object(of, 'OEM_FINDER_VERSION', 'oem-finder-9'):
            stats = of.execute()['counts']['auto']
        self.assertEqual((dict(OEMAutoCandidate.objects.values_list('part__sku', 'status')), stats['created']), (before, 0))
        versions = dict(OEMAutoCandidate.objects.values_list('part__sku', 'rules_version'))
        self.assertEqual({sku for sku, v in versions.items() if v == 'oem-finder-9'}, {sku for sku, st in before.items() if st not in ('applied', 'stale')})
        self.assertEqual((self.row(reverted.sku).reason, self.row('48654-30030').note), ('previously_reverted', 'REVISAR'))
        case = OEMReviewCase.objects.get(part=self.parts['48654-30030'])
        self.assertEqual((case.status, case.decision['action'], case.run.rules_version), ('review', 'sent_to_review', 'oem-finder-9'))

    def test_rows_that_leave_the_auto_tiers_become_stale_but_sent_rows_stay_sent(self):
        of.execute()
        au.send_to_review([self.parts['48654-0K010'].pk], self.root)
        Part.objects.filter(pk__in=[self.parts['48654-30030'].pk, self.parts['48654-0K010'].pk]).update(active=False)
        stats = of.execute()['counts']['auto']
        row = self.row('48654-30030')
        self.assertEqual((row.status, row.reason, stats['moved']), ('stale', 'left_auto', 1))
        self.assertEqual(self.row('48654-0K010').status, 'sent_to_review')

    def test_the_worker_stage_and_the_command_line_refresh_it(self):
        self.assertEqual(of.worker_stage(self.root)['status'], 'completed')
        self.assertEqual(OEMAutoCandidate.objects.count(), 13)
        out = io.StringIO()
        self.assertEqual(of.main(['--dry-run'], stdout=out), 0)
        self.assertIn('Pendientes de aplicación automática: 13 (AUTO_FLAG_CURRENT 11, AUTO_RENAME_BASE 1, STRONG_PENDING_OWNER_TAGS 1)', out.getvalue())

    def test_a_failed_refresh_never_fails_the_dry_run(self):
        with patch.object(au, 'refresh', side_effect=RuntimeError('boom')), self.assertLogs('mall.oem_auto', 'ERROR'):
            outcome = of.execute()
            self.assertEqual(of.worker_stage(self.root)['status'], 'completed')
        self.assertEqual((outcome['run'].status, outcome['counts']['auto']), ('completed', {'status': 'failed'}))
        self.assertIn('no se pudieron actualizar', of.render(outcome))

    def test_before_migration_0038_nothing_breaks(self):
        with patch('mall.oem_auto.auto_tables_ready', return_value=False):
            outcome = of.execute()
            self.assertEqual(outcome['counts']['auto'], {'status': 'not_migrated'})
            self.assertIn('falta la migración 0038', of.render(outcome))
            applied = self.canary(FLAG, 2)
            self.assertEqual((applied['summary']['applied'], applied['auto']), (2, {'status': 'not_migrated'}))
            self.assertEqual(oa.revert_run(applied['run'].pk)['reverted'], 2)
        self.assertFalse(OEMAutoCandidate.objects.exists())


class SendToReviewTests(Pending, TestCase):
    def setUp(self):
        super().setUp()
        of.execute()

    def test_a_sent_row_leaves_automatic_application_and_a_person_decides_in_the_review(self):
        part = self.parts['48654-30030']
        out = au.send_to_review([part.pk], self.root, '  REVISAR EL LADO ')
        case = OEMReviewCase.objects.get(part=part)
        self.assertEqual(out, {'sent': [{'part_id': str(part.pk), 'sku': '48654-30030', 'case': case.pk}], 'already': [], 'skipped': []})
        row = self.row('48654-30030')
        self.assertEqual((row.status, row.note, row.decided_by), ('sent_to_review', 'REVISAR EL LADO', self.root))
        self.assertEqual((case.tier, case.underlying_tier, case.status, case.candidate, case.blockers, case.decision['action'], case.decided_by),
                         ('CURRENT_REVIEW', FLAG, 'review', '48654-30030', ['sent_to_review'], 'sent_to_review', self.root))
        fingerprint = case.fingerprint
        stats = of.execute()['counts']  # the dry run keeps the case open and unchanged, and the row stays out of automatic application
        case.refresh_from_db()
        self.assertEqual((case.status, case.fingerprint, stats['cases']['resolved'], stats['cases']['updated']), ('review', fingerprint, 0, 0))
        self.assertEqual(self.row('48654-30030').status, 'sent_to_review')
        preview = au.preview(FLAG, 'canary')
        self.assertEqual((preview['selected'], preview['excluded']), (10, {'sent_to_review': 1}))
        self.assertNotIn(str(part.pk), {r['part_id'] for r in preview['rows']})
        outcome = self.canary(FLAG, 2)
        self.assertEqual(outcome['summary']['excluded'], {'sent_to_review': 1})
        self.assertNotIn(part.pk, set(outcome['run'].changes.values_list('part_id', flat=True)))
        self.assertFalse(self.part('48654-30030').is_OEM)
        run, link = rv.approve(case.pk, fingerprint, self.root)  # the O4 path: an undoable run of its own
        self.assertEqual((self.part('48654-30030').is_OEM, link.tier, link.run_id), (True, FLAG, run))
        self.assertEqual(au.send_to_review([part.pk], self.root)['already'], [{'part_id': str(part.pk), 'sku': '48654-30030'}])

    def test_a_rename_goes_to_its_review_tier_and_approving_it_renames(self):
        part = self.parts['48654-0K030-G']
        au.send_to_review([part.pk], self.root)
        case = OEMReviewCase.objects.get(part=part)
        self.assertEqual((case.tier, case.underlying_tier, case.chain), (CONFIRMED, RENAME, 'G'))
        rv.approve(case.pk, case.fingerprint, self.root)
        self.assertEqual((self.part('48654-0K030-G').sku, self.part('48654-0K030-G').is_OEM), ('48654-0K030', True))
        au.send_to_review([self.parts['54830-2H000-MOBIS'].pk], self.root)
        self.assertEqual(OEMReviewCase.objects.get(part=self.parts['54830-2H000-MOBIS']).tier, 'PROBABLE_BASE')

    def test_an_existing_case_is_reopened_and_rows_that_are_not_pending_are_skipped(self):
        part = self.parts['54830-1R000']
        OEMReviewCase.objects.create(part=part, fingerprint='x' * 64, tier='CURRENT_REVIEW', status='dismissed', decision={'action': 'dismiss'})
        au.send_to_review([part.pk], self.root)
        case = OEMReviewCase.objects.get(part=part)
        self.assertEqual((case.status, case.decision['action'], case.underlying_tier), ('review', 'sent_to_review', FLAG))
        self.assertEqual(OEMReviewCase.objects.filter(part=part).count(), 1)
        OEMAutoCandidate.objects.filter(part=self.parts['54830-3X000']).update(status='applied')
        out = au.send_to_review([self.parts['54830-3X000'].pk, self.parts['MR-968365'].pk], self.root)
        self.assertEqual([(r['sku'], r['reason']) for r in out['skipped']], [('54830-3X000', 'applied'), ('', 'not_found')])
        self.assertFalse(OEMReviewCase.objects.filter(part=self.parts['54830-3X000']).exists())


class APITests(Pending, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.root)
        self.stock('54830-1R000', 5)
        self.stock('48654-0K030-G', 1)
        self.dry = of.execute()['run']

    def get(self, **params):
        response = self.client.get(URL, params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def post(self, path, data):
        return self.client.post(URL + path, data, format='json')

    def apply(self, tier, mode, operation=None):
        return self.post('apply/', {'tier': tier, 'mode': mode, 'operation_id': str(operation or uuid.uuid4())})

    def test_list_filters_counts_and_state(self):
        data = self.get()
        self.assertEqual((data['count'], data['tiers'], data['statuses']), (13, {FLAG: 11, RENAME: 2}, {'pending': 13}))
        self.assertEqual([r['sku'] for r in data['results'][:2]], ['54830-1R000', '48654-0K030-G'])  # in stock first, most units
        self.assertEqual(data['apply'][FLAG], {'pending': 11, 'in_stock': 1, 'canary_done': False})
        self.assertEqual(data['apply'][CONFIRMED], {'pending': 1, 'in_stock': 1, 'canary_done': False})
        self.assertEqual((data['halt'], data['busy'], data['last_refresh']['id'], data['sizes']), (None, {'lock': False, 'running': None}, self.dry.pk,
                                                                                                 {'canary': 50, 'batch': 100}))
        self.assertEqual((data['rules_version'], data['last_refresh']['rules_version']), (of.OEM_FINDER_VERSION, of.OEM_FINDER_VERSION))
        g = next(r for r in data['results'] if r['sku'] == '48654-0K030-G')
        self.assertEqual((g['candidate'], g['method'], g['owner_tags'], g['chain_tokens'][0]['tok'], g['apply_tier'], g['stale']),
                         ('48654-0K030', 'rename', ['G'], 'G', CONFIRMED, ''))
        self.assertEqual((g['evidence']['lexicon']['result'], g['available_quantity'], g['grade']), ('agree_strong', 1, 'F4'))
        renames = self.get(tier=RENAME)
        self.assertEqual(({r['sku'] for r in renames['results']}, renames['tiers'], renames['apply_tiers']),
                         ({'48654-0K030-G', '54830-2H000-MOBIS'}, {FLAG: 11, RENAME: 2}, {RENAME: 1, CONFIRMED: 1}))
        self.assertEqual(renames['facets']['chains'], [['G', 1], ['MOBIS', 1]])
        for params, skus in (({'apply_tier': CONFIRMED}, {'48654-0K030-G'}), ({'chain': 'mobis'}, {'54830-2H000-MOBIS'}),
                             ({'search': '0k030'}, {'48654-0K030-G'}), ({'in_stock': 'true'}, {'54830-1R000', '48654-0K030-G'})):
            self.assertEqual({r['sku'] for r in self.get(**params)['results']}, skus, params)
        self.assertEqual(self.get(make='HYUNDAI')['count'], 6)
        self.assertEqual(self.get(system='HMG', tier=FLAG)['count'], 5)
        self.assertEqual(self.get(ordering='sku')['results'][0]['sku'], '48654-0K010')
        self.assertEqual(self.get(status='applied')['count'], 0)
        for bad in ({'tier': 'CONFLICT'}, {'status': 'nada'}, {'in_stock': 'si'}, {'ordering': 'precio'}, {'apply_tier': 'PROBABLE_BASE'}):
            self.assertEqual(self.client.get(URL, bad).status_code, 400, bad)
        Part.objects.filter(pk=self.parts['54830-1R000'].pk).update(description='TERM ESTAB HYU ACCENT EDITADO')
        self.assertEqual(self.get()['results'][0]['stale'], 'snapshot_changed')

    def test_preview_lists_the_exact_rows_and_writes_nothing(self):
        runs = OEMFinderRun.objects.count()
        data = self.post('preview/', {'tier': FLAG, 'mode': 'canary'}).data
        self.assertEqual((data['size'], data['eligible'], data['selected'], data['would_apply'], data['conflicts'], data['canary_done']), (50, 11, 11, 11, 0, False))
        self.assertEqual(data['rows'][0], {**data['rows'][0], 'sku': '54830-1R000', 'oem': '54830-1R000', 'method': 'flag', 'outcome': 'applied', 'in_stock': True})
        batch = self.post('preview/', {'tier': CONFIRMED, 'mode': 'batch'}).data
        self.assertEqual((batch['size'], batch['selected'], batch['rows'][0]['oem'], batch['rows'][0]['method']), (100, 1, '48654-0K030', 'rename'))
        self.assertEqual((OEMFinderRun.objects.count(), Part.objects.filter(is_OEM=True).count(), PartCode.objects.count()), (runs, 0, 0))
        for bad in ({'tier': 'CONFLICT', 'mode': 'canary'}, {'tier': FLAG, 'mode': 'todo'}, {}):
            self.assertEqual(self.post('preview/', bad).status_code, 400, bad)

    def test_canary_gate_then_batches_with_idempotent_operations(self):
        refused = self.apply(FLAG, 'batch')
        self.assertEqual((refused.status_code, refused.data['reason']), (409, 'needs_canary'))
        self.assertIn('canario de 50 de «SKU ya es el OEM»', refused.data['detail'])
        operation = uuid.uuid4()
        with patch.dict(au.MODES, {'canary': 2}):
            self.assertIn('canario de 2', self.apply(FLAG, 'batch').data['detail'])
            first = self.apply(FLAG, 'canary', operation)
        self.assertEqual(first.status_code, 201, first.data)
        run = first.data['run']
        self.assertEqual((run['scope']['canary'], run['scope']['stage'], run['scope']['operation_id'], run['applied']['applied'], run['actor'], first.data['replayed']),
                         (2, 'admin', str(operation), 2, 'oem-root', False))
        runs = OEMFinderRun.objects.count()
        again = self.apply(FLAG, 'canary', operation)  # a retried click: the same run, nothing applied twice
        self.assertEqual((again.status_code, again.data['run']['id'], again.data['replayed'], OEMFinderRun.objects.count()), (200, run['id'], True, runs))
        reused = self.apply(FLAG, 'batch', operation)
        self.assertEqual((reused.status_code, reused.data['reason']), (409, 'operation_reused'))
        state = self.get()
        pending = state['apply'][FLAG]['pending']
        self.assertEqual((state['apply'][FLAG]['canary_done'], state['statuses']['applied'], state['statuses']['pending'] + state['statuses'].get('stale', 0)), (True, 2, 11))
        self.assertEqual(pending, oa.apply_auto(tier=FLAG, read_only=True)['eligible'])
        batch = self.apply(FLAG, 'batch')
        self.assertEqual((batch.status_code, batch.data['run']['scope']['limit'], batch.data['run']['applied']['applied']), (201, 100, pending))
        self.assertEqual(self.get(tier=FLAG)['count'], 0)
        self.assertEqual(self.get(status='applied', tier=FLAG)['count'], 2 + pending)
        self.assertEqual(self.apply(CONFIRMED, 'batch').data['reason'], 'needs_canary')  # the canary credit is per apply tier
        history = self.client.get('/api/v1/management/oem-finder/runs/', {'mode': 'apply_auto', 'stage': 'auto'}).data
        self.assertEqual([r['id'] for r in history['results']][:2], [batch.data['run']['id'], run['id']])

    def test_a_failure_before_the_run_starts_writes_nothing_and_says_so(self):
        with patch('mall.catalog_suffixes.suffix_table', side_effect=RuntimeError('tabla')):
            failed = self.apply(FLAG, 'canary')
        self.assertEqual(failed.status_code, 500)
        self.assertIn('no se aplicó nada', failed.data['detail'])
        self.assertFalse(OEMFinderRun.objects.filter(mode='apply_auto').exists() or oa.active_halt())

    def test_halt_lock_import_and_failures_refuse_or_stop(self):
        oa.halt('MUESTRA CON ERRORES', actor=self.root)
        halted = self.apply(FLAG, 'canary')
        self.assertEqual((halted.status_code, halted.data['reason']), (409, 'halted'))
        self.assertIn('MUESTRA CON ERRORES', halted.data['detail'])
        self.assertEqual(self.get()['halt']['reason'], 'MUESTRA CON ERRORES')
        oa.resume(actor=self.root)
        with patch('mall.oem_finder.import_running', return_value=True):
            self.assertEqual(self.apply(FLAG, 'canary').status_code, 409)
        with patch.object(rv, 'LOCK_WAIT', 0), patch('mall.oem_finder.matching_lock') as lock:
            lock.return_value.__enter__.return_value = False
            self.assertEqual(self.apply(FLAG, 'canary').status_code, 409)
            self.assertEqual(self.post('send-to-review/', {'parts': [str(self.parts['48654-30030'].pk)]}).status_code, 409)
        self.assertFalse(OEMFinderRun.objects.filter(mode='apply_auto').exists())
        with patch.object(oa, 'execute_batches', side_effect=RuntimeError('boom')):
            failed = self.apply(FLAG, 'canary')
        self.assertEqual(failed.status_code, 500)
        self.assertIn('regla de detención', failed.data['detail'])
        self.assertIn(f'#{OEMFinderRun.objects.get(mode="apply_auto").pk}', failed.data['detail'])
        self.assertEqual((OEMFinderRun.objects.get(mode='apply_auto').status, oa.active_halt().reason[:12]), ('failed', 'La ejecución'))
        self.assertEqual(self.apply(FLAG, 'canary').data['reason'], 'halted')

    def test_send_to_review_takes_rows_out_of_automatic_application(self):
        g, other = str(self.parts['48654-0K030-G'].pk), str(self.parts['MR-968365'].pk)
        data = self.post('send-to-review/', {'parts': [g, other], 'note': 'CONFIRMAR G'}).data
        self.assertEqual(([r['sku'] for r in data['sent']], [r['reason'] for r in data['skipped']]), (['48654-0K030-G'], ['not_found']))
        self.assertEqual(self.post('send-to-review/', {'parts': [g]}).data['already'], [{'part_id': g, 'sku': '48654-0K030-G'}])
        sent = self.get(status='sent_to_review')['results']
        self.assertEqual([(r['sku'], r['note'], r['decided_by']) for r in sent], [('48654-0K030-G', 'CONFIRMAR G', 'oem-root')])
        self.assertEqual(self.get()['apply'][CONFIRMED]['pending'], 0)
        preview = self.post('preview/', {'tier': CONFIRMED, 'mode': 'canary'}).data
        self.assertEqual((preview['selected'], preview['excluded']), (0, {'sent_to_review': 1}))
        queue = self.client.get('/api/v1/management/oem-review/', {'blocker': 'sent_to_review'}).data
        self.assertEqual([(r['sku'], r['tier'], r['actions']) for r in queue['results']], [('48654-0K030-G', CONFIRMED, ['approve', 'dismiss'])])
        for bad in ({'parts': []}, {'parts': ['no-es-uuid']}, {'parts': [g] * 101}, {}):
            self.assertEqual(self.post('send-to-review/', bad).status_code, 400, bad)

    def test_superusers_only_and_unmigrated(self):
        calls = (('get', URL, {}), ('post', URL + 'preview/', {'tier': FLAG, 'mode': 'canary'}),
                 ('post', URL + 'apply/', {'tier': FLAG, 'mode': 'canary', 'operation_id': str(uuid.uuid4())}),
                 ('post', URL + 'send-to-review/', {'parts': [str(self.parts['48654-30030'].pk)]}))
        self.client.force_authenticate(User.objects.create_user('lector', 'lector@example.invalid', 'Example938!'))
        for method, url, data in calls:
            self.assertEqual(getattr(self.client, method)(url, data, format='json').status_code, 403, url)
        self.client.force_authenticate(None)
        for method, url, data in calls:
            self.assertIn(getattr(self.client, method)(url, data, format='json').status_code, (401, 403), url)
        self.client.force_authenticate(self.root)
        with patch('mall.oem_auto.auto_tables_ready', return_value=False):
            for method, url, data in calls:
                self.assertEqual(getattr(self.client, method)(url, data, format='json').status_code, 503, url)
        self.assertFalse(OEMFinderRun.objects.filter(mode='apply_auto').exists() or Part.objects.filter(is_OEM=True).exists())


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL advisory locks.')
class LockTests(Pending, TransactionTestCase):
    client_class = APIClient

    def test_apply_waits_for_a_matching_pass_and_reports_the_lock(self):
        self.client.force_authenticate(self.root)
        of.execute()
        held, release = threading.Event(), threading.Event()

        def hold():
            from django.db import connection as thread_connection
            try:
                with thread_connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_lock(%s)', [MATCHING_LOCK])
                    held.set()
                    release.wait(10)
                    cursor.execute('SELECT pg_advisory_unlock(%s)', [MATCHING_LOCK])
            finally:
                thread_connection.close()
        holder = threading.Thread(target=hold)
        holder.start()
        held.wait(10)
        body = {'tier': FLAG, 'mode': 'canary', 'operation_id': str(uuid.uuid4())}
        try:
            self.assertEqual(self.client.get(URL).data['busy']['lock'], True)
            with patch.object(rv, 'LOCK_WAIT', 0.3):
                busy = self.client.post(URL + 'apply/', body, format='json')
            self.assertEqual(busy.status_code, 409)
            self.assertIn('en curso', busy.data['detail'])
            self.assertFalse(OEMFinderRun.objects.filter(mode='apply_auto').exists())
            threading.Timer(0.5, release.set).start()
            with patch.object(rv, 'LOCK_WAIT', 5):
                self.assertEqual(self.client.post(URL + 'apply/', body, format='json').status_code, 201)  # waited for the pass to finish
        finally:
            release.set()
            holder.join()
        self.assertEqual(self.client.get(URL).data['busy']['lock'], False)
        self.assertEqual(Part.objects.filter(is_OEM=True).count(), 11)
