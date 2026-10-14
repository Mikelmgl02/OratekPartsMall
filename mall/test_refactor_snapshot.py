import io
import re
import tempfile
from pathlib import Path

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from .models import Part
from .refactor import snapshot
from .test_factories import (Fresh, make_catalog, make_client, make_library_number, make_line, make_reference, make_supplier, make_user,
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
        make_reference(self.line, '17801-30070', brand='TOYOTA', tipo='oem', source='CATÁLOGO')
        make_variant(self.supplier, 'S-1', '17801-30070', 4, actor=seller, brand='KSM', line=self.line)
        duplicate = make_line('17801-30070-G', description='FILTRO AIRE TOY')
        Part.objects.filter(pk=duplicate.pk).update(active=False, merged_into=self.line)
        make_library_number('TOYOTA', '48654-0K050', citation='LISTA 2026')

    def test_a_capture_reads_every_screen_and_writes_nothing(self):
        with CaptureQueriesContext(connection) as queries:
            snap = snapshot.capture()
        self.assertEqual(snap['errors'], [])
        self.assertEqual([q['sql'] for q in queries.captured_queries if snapshot.WRITES.match(q['sql']) or LOCKS.search(q['sql'])], [])
        sections, sample, line = snap['sections'], snap['sample'], str(self.line.pk)
        self.assertEqual(list(sections), ['catalog', 'suggest', 'offers', 'availability', 'equivalents', 'admin_grid', 'biblioteca', 'productos_nuevos',
                                          'supplier_inventory', 'oem_review', 'matching', 'merged', 'split', 'alternates', 'oem_equivalents',
                                          'mark_oem', 'technical', 'import_preview'])
        self.assertEqual(sample['users']['admin'], self.root.pk)
        self.assertEqual(sample['users']['suppliers'], {str(self.supplier.pk): self.supplier.memberships.get().user_id})
        self.assertEqual((sample['offers'], sample['split']), ([line], [line]))
        # mark-oem reads the SKUs whose code is a library number first, then the others
        self.assertEqual(sample['mark_oem'][2], str(self.lines['28113-F2000'].pk))
        self.assertIn('17801-30070', [row['sku'] for row in sections['catalog']['FILTRO AIRE']['results']])
        self.assertEqual(sections['availability'][line]['supplier_count'], 1)
        self.assertEqual([offer['supplier_name'] for offer in sections['offers'][line]], ['PROVEEDOR INSTANTANEA'])
        self.assertEqual(sections['admin_grid']['sku p1']['count'], 3)
        self.assertEqual(sections['admin_grid']['sku p2']['status'], 404)  # one page of rows: pages 2 and 3 do not exist
        self.assertEqual(len(sections['mark_oem']['plan']['rows']), 3)
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
        results = {(check_id, outcome) for check_id, _, outcome, _ in snapshot.run_checks()}
        self.assertIn(('I6', 'OK'), results)
        self.assertIn(('C0', 'INFO'), results)
        self.assertIn(('I1', 'OMITIDA'), results)
        self.assertNotIn('FALLA', {outcome for _, outcome in results})


class DiffTests(SimpleTestCase):
    def snap(self, **sections):
        return {'meta': {'created_at': '2026-10-13T00:00:00', 'migration': '0070_deal_progress'}, 'sample': {}, 'sections': sections, 'errors': []}

    def test_rows_with_ids_are_compared_by_id_with_their_order_apart(self):
        flat = snapshot.flatten({'grid': {'results': [{'id': 'b', 'sku': 'B'}, {'id': 'a', 'sku': 'A'}], 'facets': [{'value': 'X'}], 'none': []}})
        self.assertEqual(flat, {'grid/results[order]': ['b', 'a'], 'grid/results[id=b]/sku': 'B', 'grid/results[id=a]/sku': 'A',
                                'grid/facets[0]/value': 'X', 'grid/none': []})

    def test_changes_are_unexpected_unless_allowed_and_the_workers_state_is_ignored(self):
        base = self.snap(matching={'review': {'state': 'idle', 'count': 2}}, grid={'p1': {'results': [{'id': 1, 'sku': 'A'}]}})
        current = self.snap(matching={'review': {'state': 'running', 'count': 3}}, grid={'p1': {'results': [{'id': 1, 'sku': 'B'}, {'id': 2, 'sku': 'C'}]}})
        result = snapshot.compare(base, current, allow=['grid/p1/results[[]order]'])
        self.assertEqual(result['ignored'], [('matching/review/state', 'idle', 'running')])
        self.assertEqual(result['expected'], [('grid/p1/results[order]', ['1'], ['1', '2'])])
        self.assertEqual(result['unexpected'], [('grid/p1/results[id=1]/sku', 'A', 'B'), ('grid/p1/results[id=2]/sku', snapshot.MISSING, 'C'),
                                                ('matching/review/count', 2, 3)])
        text = snapshot.render_diff(result)
        self.assertIn('Cambios no previstos: 3 (grid 2, matching 1)', text)
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
