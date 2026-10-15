import io
import re
import tempfile
from pathlib import Path

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from .brand_models import Brand
from .models import Part
from .refactor import snapshot
from .test_factories import (Fresh, legacy_mirror, make_catalog, make_client, make_legacy_number, make_line, make_reference, make_supplier, make_user,
                             make_variant)

LOCKS = re.compile(r'\bFOR (NO KEY )?UPDATE\b', re.I)


@override_settings(ALLOWED_HOSTS=['localhost'])
class CaptureTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user('snapshot-root', superuser=True)
        buyer, seller = make_user('snapshot-buyer'), make_user('snapshot-seller')
        self.client_account = make_client('CLIENTE INSTANTANEA', buyer)
        self.supplier = make_supplier('PROVEEDOR INSTANTANEA', seller)
        self.lines = make_catalog([('17801-30070', 'FILTRO AIRE TOY HILUX'), ('48654-0K050', 'BASE AMORT TOY HILUX'),
                                   ('28113-F2000', 'FILTRO AIRE HYU ELANTRA')])
        self.line = self.lines['17801-30070']
        legacy_mirror(make_reference(self.line, '17801-30070', brand='TOYOTA', tipo='oem', source='CATÁLOGO'))  # as the base read the library
        Brand.objects.create(nombre='TOYOTA')  # every brand a code carries is a brand (I12: the S1 seed, then S2a's backfill)
        make_variant(self.supplier, 'S-1', '17801-30070', 4, actor=seller, brand='KSM', line=self.line)
        duplicate = make_line('17801-30070-G', description='FILTRO AIRE TOY')
        duplicate.merged_into = self.line
        duplicate.save(update_fields=['merged_into'])  # unificada, and its main retired (I3) as a merge leaves it
        make_legacy_number('TOYOTA', '486540K050', printed_forms=['48654-0K050'], citation='LISTA 2026')  # the library as the base snapshot read it

    def test_a_capture_reads_every_screen_and_writes_nothing(self):
        with CaptureQueriesContext(connection) as queries:
            snap = snapshot.capture()
        self.assertEqual(snap['errors'], [])
        self.assertEqual([q['sql'] for q in queries.captured_queries if snapshot.WRITES.match(q['sql']) or LOCKS.search(q['sql'])], [])
        sections, sample, line = snap['sections'], snap['sample'], str(self.line.pk)
        self.assertEqual(list(sections), ['catalog', 'suggest', 'offers', 'availability', 'equivalents', 'admin_grid', 'biblioteca', 'productos_nuevos',
                                          'supplier_inventory', 'matching', 'merged', 'split', 'alternates', 'oem_equivalents', 'technical',
                                          'import_preview'])
        self.assertEqual(sample['users']['admin'], self.root.pk)
        self.assertEqual(sample['users']['suppliers'], {str(self.supplier.pk): self.supplier.memberships.get().user_id})
        self.assertEqual((sample['offers'], sample['split']), ([line], [line]))
        self.assertIn('17801-30070', [row['sku'] for row in sections['catalog']['FILTRO AIRE']['results']])
        self.assertEqual(sections['availability'][line]['supplier_count'], 1)
        self.assertEqual([offer['supplier_name'] for offer in sections['offers'][line]], ['PROVEEDOR INSTANTANEA'])
        self.assertEqual(sections['admin_grid']['sku p1']['count'], 3)
        self.assertEqual(sections['admin_grid']['sku p2']['status'], 404)  # one page of rows: pages 2 and 3 do not exist
        self.assertEqual(sections['import_preview']['summary']['valid_rows'], len(sample['import']['rows']))
        self.assertEqual(sections['supplier_inventory'][str(self.supplier.pk)]['count'], 1)

    def test_the_same_sample_reads_the_same_and_a_change_shows_its_key(self):
        base = snapshot.capture()
        again = snapshot.capture(base['sample'])
        self.assertEqual(snapshot.compare(base, again)['unexpected'], [])
        Part.objects.filter(pk=self.line.pk).update(description='FILTRO DE AIRE TOY HILUX')
        changed = snapshot.capture(base['sample'])
        paths = [path for path, _, _ in snapshot.compare(base, changed)['unexpected']]
        self.assertIn(f'admin_grid/sku p1/results[id={self.line.pk}]/description', paths)
        allowed = snapshot.compare(base, changed, allow=['*description*'])
        self.assertNotIn(f'admin_grid/sku p1/results[id={self.line.pk}]/description', [path for path, _, _ in allowed['unexpected']])
        self.assertIn(f'admin_grid/sku p1/results[id={self.line.pk}]/description', [path for path, _, _ in allowed['expected']])

    def test_checks_run_what_today_has_and_skip_what_no_stage_added_yet(self):
        from .oem_reference_models import OEMReference
        make_line('17801-30070-OLD', active=False)  # C1 (S1): publicada, unificada and retirada lines, as on the live catalog
        OEMReference.objects.all().delete()  # C4 (S4): every number of the OEM table has its Reference once S4's backfill ran
        from . import search_document
        search_document.rebuild()  # I8 (S7): the documents are written when a transaction commits, which a TestCase never does
        results = {(check_id, outcome) for check_id, _, outcome, _ in snapshot.run_checks()}
        self.assertIn(('C1', 'INFO'), results)  # from S4 the lines in referencia make more groups: it only informs
        self.assertIn(('C4', 'OK'), results)
        self.assertIn(('I6', 'OK'), results)
        self.assertIn(('C0', 'INFO'), results)
        self.assertIn(('I1', 'OK'), results)  # every line has its main since S2b (the Part.save shim makes it)
        self.assertIn(('I8', 'OK'), results)
        self.assertNotIn('FALLA', {outcome for _, outcome in results})


class DiffTests(SimpleTestCase):
    def snap(self, **sections):
        return {'meta': {'created_at': '2026-10-13T00:00:00', 'migration': '0070_deal_progress'}, 'sample': {}, 'sections': sections, 'errors': []}

    def test_rows_with_ids_are_compared_by_id_with_their_order_apart(self):
        flat = snapshot.flatten({'grid': {'results': [{'id': 'b', 'sku': 'B'}, {'id': 'a', 'sku': 'A'}], 'facets': [{'value': 'X'}], 'none': []}})
        self.assertEqual(flat, {'grid/results[order]': ['b', 'a'], 'grid/results[id=b]/sku': 'B', 'grid/results[id=a]/sku': 'A',
                                'grid/facets[0]/value': 'X', 'grid/none': []})

    def test_changes_are_unexpected_unless_allowed_and_the_workers_state_is_ignored(self):
        base = self.snap(matching={'review': {'state': 'idle', 'count': 2, 'summary': {}, 'results': [{'id': 7, 'ai': {}, 'state': 'a'}]}},
                         grid={'p1': {'results': [{'id': 1, 'sku': 'A'}]}})
        current = self.snap(matching={'review': {'state': 'running', 'count': 3, 'summary': {'stage': 'ai', 'skus': [1]},
                                                 'results': [{'id': 7, 'ai': {'error': 'Sin cuota'}, 'state': 'b'}]}},
                            grid={'p1': {'results': [{'id': 1, 'sku': 'B'}, {'id': 2, 'sku': 'C'}]}})
        result = snapshot.compare(base, current, allow=['grid/p1/results[[]order]'])
        self.assertEqual(result['ignored'], [('matching/review/state', 'idle', 'running'), ('matching/review/summary', {}, snapshot.MISSING),
                                             ('matching/review/summary/skus[0]', snapshot.MISSING, 1),
                                             ('matching/review/summary/stage', snapshot.MISSING, 'ai')])
        self.assertEqual(result['expected'], [('grid/p1/results[order]', ['1'], ['1', '2'])])
        # a case's own keys are not the worker's state, even when they share a name
        self.assertEqual(result['unexpected'], [('grid/p1/results[id=1]/sku', 'A', 'B'), ('grid/p1/results[id=2]/sku', snapshot.MISSING, 'C'),
                                                ('matching/review/count', 2, 3), ('matching/review/results[id=7]/ai', {}, snapshot.MISSING),
                                                ('matching/review/results[id=7]/ai/error', snapshot.MISSING, 'Sin cuota'),
                                                ('matching/review/results[id=7]/state', 'a', 'b')])
        text = snapshot.render_diff(result)
        self.assertIn('Cambios no previstos: 6 (grid 2, matching 4)', text)
        self.assertIn('  + grid/p1/results[id=2]/sku: "C"', text)

    def test_two_saved_snapshots_compare_without_the_database(self):
        with tempfile.TemporaryDirectory() as folder:
            base = snapshot.save(self.snap(grid={'p1': {'count': 1}}), Path(folder) / 'base.json.gz')
            current = snapshot.save(self.snap(grid={'p1': {'count': 2}}), Path(folder) / 'current.json.gz')
            self.assertEqual(snapshot.load(base)['sections'], {'grid': {'p1': {'count': 1}}})
            out = io.StringIO()
            self.assertEqual(snapshot.main(['--diff', str(base), '--against', str(current)], stdout=out), 1)
            self.assertIn('~ grid/p1/count: 1 → 2', out.getvalue())
            self.assertEqual(snapshot.main(['--diff', str(base), '--against', str(current), '--allow', 'grid/*'], stdout=io.StringIO()), 0)

    def test_a_snapshot_never_replaces_another_unless_forced(self):
        with tempfile.TemporaryDirectory() as folder:
            path = snapshot.save(self.snap(grid={'p1': {'count': 1}}), Path(folder) / 'base.json.gz')
            with self.assertRaisesMessage(SystemExit, '--force'):
                snapshot.save(self.snap(grid={'p1': {'count': 2}}), path)
            with self.assertRaisesMessage(SystemExit, '--force'):  # refused before it reads anything
                snapshot.main(['--out', folder, '--name', 'base', '--diff', str(path)], stdout=io.StringIO())
            self.assertEqual(snapshot.load(path)['sections'], {'grid': {'p1': {'count': 1}}})
            snapshot.save(self.snap(grid={'p1': {'count': 2}}), path, force=True)
            self.assertEqual(snapshot.load(path)['sections'], {'grid': {'p1': {'count': 2}}})


class ChecksTests(SimpleTestCase):
    TODAY = {'mall_part.active', 'mall_part.merged_into_id', 'mall_supplieritem.matching_status', 'mall_partcode.ref_type',
             'mall_matchingcase.kind', 'mall_oemreference.id'}

    def run_checks(self, present, rows=()):
        asked = []
        results = snapshot.run_checks(present, lambda sql: asked.append(sql) or list(rows))
        return {(check_id, outcome) for check_id, _, outcome, _ in results}, asked

    def test_a_check_waits_for_its_own_stage_not_for_the_columns_an_earlier_stage_added(self):
        s3 = self.TODAY | {snapshot.STAGE_COLUMNS[stage] for stage in ('S1', 'S2a', 'S2b', 'S3')} | {
            'mall_part.origen', 'mall_partcode.is_main', 'mall_partcode.estado', 'mall_partcode.tipo', 'mall_partcode.supplier_id',
            'mall_partcode.uid', 'mall_brand.nombre', 'mall_brandalias.alias'}
        results, asked = self.run_checks(s3, [(0,)])
        self.assertTrue({('I10', 'OMITIDA'), ('I9', 'OMITIDA'), ('C6', 'OMITIDA'), ('C4', 'OMITIDA'), ('I8', 'OMITIDA')} <= results)
        self.assertTrue({('I1', 'OK'), ('I4', 'OK'), ('I7', 'OK'), ('I12', 'OK'), ('I6', 'OK')} <= results)
        self.assertFalse([sql for sql in asked if 'mall_matchingcase' in sql or 'mall_oemcrossreference' in sql or 'r.uid' in sql])
        # S1 alone: the brands exist, but I12 holds only once S2a has seeded the missing spellings
        s1, _ = self.run_checks(self.TODAY | {'mall_part.estado', 'mall_part.origen', 'mall_brand.nombre', 'mall_brandalias.alias'}, [(5,)])
        self.assertTrue({('I12', 'OMITIDA'), ('I1', 'OMITIDA'), ('C1', 'FALLA'), ('I6', 'FALLA')} <= s1)

    def test_c1_wants_exactly_three_groups_until_s4_brings_referencia_lines(self):
        check = next(check for check in snapshot.CHECKS if check.id == 'C1')
        groups = [('publicada', True, False, 31526), ('retirada', False, False, 232), ('unificada', False, True, 2472)]
        s1 = {'mall_part.estado'}
        self.assertEqual(snapshot.judge(check, groups, s1)[0], 'OK')
        self.assertEqual(snapshot.judge(check, groups[:2], s1)[0], 'FALLA')
        self.assertEqual(snapshot.judge(check, [*groups, ('referencia', False, False, 14886)], s1)[0], 'FALLA')
        self.assertEqual(snapshot.judge(check, [*groups, ('referencia', False, False, 14886)], s1 | {snapshot.STAGE_COLUMNS['S4']})[0], 'INFO')
