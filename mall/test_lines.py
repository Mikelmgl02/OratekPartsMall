"""The OEM line's lifecycle (inventory refactor S1): estado and active kept in step by Part.save and the bulk writers, the readers'
querysets, the writers that merge, split, retire and create lines, and the backfill CLI (mall.refactor.s1_lifecycle)."""
import io
import json
import os
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from . import lines
from .brand_models import Brand, BrandAlias
from .catalog_grouping import merge_catalog_parts
from .catalog_import import build_plan, save_plan
from .matching_engine import create_supplier_families
from .models import Part, SupplierItem
from .refactor import s1_lifecycle
from .sku_proposal_models import SkuProposal
from .test_factories import make_line, make_reference, make_supplier, make_user

# 0072's CHECKs, which a test of the backfill drops inside its own transaction to stage the rows 0071 leaves (merged lines still
# publicada) before the backfill runs.
LIFECYCLE_CHECKS = ('line_valid_estado', 'line_valid_origen', 'line_unificada_iff_merged', 'line_rejected_has_reason',
                    'line_active_iff_publicada')


def drop_lifecycle_checks():
    """Inside a test's transaction, so they come back when it rolls back."""
    with connection.cursor() as cursor:
        present = connection.introspection.get_constraints(cursor, Part._meta.db_table)
        for name in LIFECYCLE_CHECKS:
            if name in present:
                cursor.execute(f'ALTER TABLE {Part._meta.db_table} DROP CONSTRAINT {name}')


def stored(line):
    return Part.objects.filter(pk=line.pk).values_list('estado', 'active').get()


class LinePairTests(TestCase):
    """Part.save keeps estado and active in step: whichever changed since the line was loaded wins, merged_into forces unificada, and
    update_fields naming either writes both."""

    def test_a_new_line_is_publicada_from_the_catalog_and_stamped(self):
        line = make_line('16100-39315')
        self.assertEqual((line.estado, line.active, line.origen, line.version), ('publicada', True, 'catalogo', 1))
        self.assertIsNotNone(line.created_at)
        self.assertIsNotNone(line.updated_at)
        self.assertIsNone(line.changed_at)
        self.assertEqual(stored(line), ('publicada', True))

    def test_old_writers_that_only_know_active_retire_and_publish(self):
        retired = make_line('RET-1', active=False)
        self.assertEqual(stored(retired), ('retirada', False))
        line = Part.objects.get(pk=retired.pk)
        line.active = True
        line.save(update_fields=['active'])
        self.assertEqual(stored(line), ('publicada', True))
        line.active = False
        line.save()
        self.assertEqual(stored(line), ('retirada', False))

    def test_estado_wins_when_it_is_what_changed(self):
        line = make_line('REF-1', estado='referencia')
        self.assertEqual(stored(line), ('referencia', False))
        line = Part.objects.get(pk=line.pk)
        line.estado = 'publicada'
        line.save(update_fields=['estado'])
        self.assertEqual(stored(line), ('publicada', True))
        both = make_line('REF-2', estado='preparacion', active=True)  # both differ from the defaults: estado wins
        self.assertEqual(stored(both), ('preparacion', False))

    def test_merged_into_forces_unificada_and_leaving_it_follows_active(self):
        target = make_line('16100-39316')
        merged = make_line('16100-39316-GMB', merged_into=target)
        self.assertEqual(stored(merged), ('unificada', False))
        line = Part.objects.get(pk=make_line('16100-39316-AS').pk)
        line.merged_into = target
        line.save(update_fields=['merged_into'])
        self.assertEqual(stored(line), ('unificada', False))
        line.merged_into, line.active = None, True
        line.save(update_fields=['merged_into', 'active'])
        self.assertEqual(stored(line), ('publicada', True))

    def test_a_stored_pair_out_of_step_follows_active_on_the_next_save(self):
        line = make_line('DRIFT-1')
        Part.objects.filter(pk=line.pk).update(estado='retirada', active=True)  # what a queryset writer without estado leaves
        line = Part.objects.get(pk=line.pk)
        line.description = 'BOMBA DE AGUA'
        line.save()
        self.assertEqual(stored(line), ('publicada', True))

    def test_a_save_of_other_fields_leaves_the_pair_and_stamps_updated_at(self):
        line = make_line('STAMP-1')
        before = line.updated_at
        Part.objects.filter(pk=line.pk).update(updated_at=before - timedelta(days=1))
        line = Part.objects.get(pk=line.pk)
        line.description = 'FILTRO'
        line.save(update_fields=['description'])
        line.refresh_from_db()
        self.assertGreaterEqual(line.updated_at, before)
        self.assertEqual((line.estado, line.active), ('publicada', True))

    def test_refresh_from_db_reloads_what_save_compares_against(self):
        line = make_line('REFRESH-1')
        Part.objects.filter(pk=line.pk).update(estado='retirada', active=False)
        line.refresh_from_db()
        line.description = 'CAMBIO'
        line.save()
        self.assertEqual(stored(line), ('retirada', False))

    def test_repaired_estado_is_the_rule_old_writers_leave(self):
        cases = {(True, None, 'publicada'): ('publicada', True), (False, None, 'publicada'): ('retirada', False),
                 (True, 'X', 'publicada'): ('unificada', False), (False, None, 'unificada'): ('retirada', False),
                 (True, None, 'unificada'): ('publicada', True), (True, None, 'retirada'): ('publicada', True),
                 (False, None, 'referencia'): ('referencia', False), (True, None, 'preparacion'): None, (True, None, 'otro'): None}
        for (active, merged, estado), expected in cases.items():
            with self.subTest(active=active, merged=merged, estado=estado):
                self.assertEqual(lines.repaired_estado(active, merged, estado), expected)


class LineReaderTests(TestCase):
    def setUp(self):
        self.publicada = make_line('L-PUB')
        self.retirada = make_line('L-RET', active=False)
        self.referencia = make_line('L-REF', estado='referencia')
        self.preparacion = make_line('L-PREP', estado='preparacion')
        self.unificada = make_line('L-UNI', merged_into=self.publicada)
        self.rechazada = lines.set_estado(make_line('L-REJ', estado='preparacion'), 'rechazada', reason='duplicate')

    def skus(self, queryset):
        return set(queryset.values_list('sku', flat=True))

    def test_the_storefront_shows_only_publicada_lines(self):
        self.assertEqual(self.skus(lines.storefront_lines()), {'L-PUB'})
        self.assertEqual(self.skus(lines.storefront_lines(Part.objects.filter(sku__startswith='L-R'))), set())

    def test_the_catalog_shows_publicada_and_retirada_unless_asked(self):
        self.assertEqual(self.skus(lines.catalog_lines()), {'L-PUB', 'L-RET'})
        self.assertEqual(self.skus(lines.catalog_lines(('referencia', 'preparacion'))), {'L-REF', 'L-PREP'})
        self.assertEqual(self.skus(lines.catalog_lines(('unificada',))), {'L-UNI'})
        with self.assertRaises(ValueError):
            lines.catalog_lines(('borrada',))

    def test_a_codigo_names_the_catalog_line_then_the_line_a_merged_one_went_into(self):
        self.assertEqual(lines.lines_by_codigo(' l-pub '), [self.publicada])
        self.assertEqual(lines.lines_by_codigo('L-UNI'), [self.publicada])
        self.assertEqual(lines.lines_by_codigo('L-REF'), [])
        self.assertEqual(lines.lines_by_codigo('L-REF', include=('referencia',)), [self.referencia])
        found = lines.lines_by_codigos(['L-RET', 'L-UNI', 'NADA'])
        self.assertEqual(found, {'L-RET': [self.retirada], 'L-UNI': [self.publicada]})

    def test_canonical_follows_the_merge(self):
        self.assertEqual(lines.canonical(self.unificada), self.publicada)
        self.assertEqual(lines.canonical(str(self.unificada.pk)), self.publicada)
        self.assertEqual(lines.canonical(self.retirada), self.retirada)
        self.assertEqual(lines.canonical_ids([self.unificada.pk, self.retirada.pk]),
                         {str(self.unificada.pk): str(self.publicada.pk), str(self.retirada.pk): str(self.retirada.pk)})
        self.assertTrue(lines.same_line(self.unificada, self.publicada.pk))
        self.assertFalse(lines.same_line(self.retirada, self.publicada))
        self.assertIsNone(lines.canonical('00000000-0000-0000-0000-000000000000'))

    def test_the_readers_helpers(self):
        self.assertEqual(self.skus(lines.all_lines()), {'L-PUB', 'L-RET', 'L-REF', 'L-PREP', 'L-UNI', 'L-REJ'})
        self.assertEqual({line.sku for line in Part.objects.all() if lines.on_storefront(line)}, {'L-PUB'})
        self.assertFalse(lines.on_storefront(None))
        self.assertEqual(self.skus(Part.objects.filter(lines.catalog_q(lines.LIVE))), {'L-PUB', 'L-RET', 'L-REF', 'L-PREP', 'L-REJ'})
        # include may name the catalog's estados too: they keep their rank, before what include adds.
        self.assertEqual(lines.lines_by_codigos(['L-PUB', 'L-PREP'], include=lines.LIVE), {'L-PUB': [self.publicada], 'L-PREP': [self.preparacion]})
        with self.assertNumQueries(0):
            self.assertTrue(lines.same_line(self.publicada.pk, str(self.publicada.pk)))
            self.assertFalse(lines.same_line(None, self.publicada))
        self.assertEqual(lines.detail_line(self.preparacion.pk), self.preparacion)
        self.assertEqual(lines.detail_line(self.unificada.pk), self.unificada)
        with self.assertRaises(lines.LineMerged) as refused:
            lines.detail_line(self.unificada.pk, write=True)
        self.assertEqual(str(refused.exception.detail), 'El SKU L-UNI se agrupó en L-PUB: edita ese SKU.')

    def test_merged_into_decides_unificada_before_the_backfill_too(self):
        with connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')  # setUp's deferred checks, so the table can be altered
        drop_lifecycle_checks()  # a merged row as 0071 leaves it: estado still at its default
        Part.objects.filter(pk=self.unificada.pk).update(estado='publicada')
        Part.objects.filter(pk=self.retirada.pk).update(estado='publicada')
        self.assertEqual(self.skus(lines.catalog_lines(('unificada',))), {'L-UNI'})
        self.assertEqual(self.skus(lines.catalog_lines()), {'L-PUB', 'L-RET'})
        self.assertEqual(self.skus(lines.storefront_lines()), {'L-PUB'})


class LineWriterTests(TestCase):
    def setUp(self):
        self.root = make_user('lines-root', superuser=True)

    def test_set_estado_records_the_decision_and_refuses_what_other_tools_own(self):
        line = make_line('W-1', estado='preparacion')
        lines.set_estado(line, 'publicada', user=self.root)
        line.refresh_from_db()
        self.assertEqual((line.estado, line.active, line.decided_by, line.version), ('publicada', True, self.root, 2))
        self.assertIsNotNone(line.decided_at)
        self.assertEqual(line.changed_at, line.decided_at)
        with self.assertRaises(lines.LineStateError):
            lines.set_estado(line, 'rechazada')
        lines.set_estado(line, 'rechazada', user=self.root, reason='not_a_part', note='Es una herramienta')
        line.refresh_from_db()
        self.assertEqual((line.estado, line.active, line.decision_reason, line.decision_note), ('rechazada', False, 'not_a_part', 'Es una herramienta'))
        lines.set_estado(line, 'retirada')
        line.refresh_from_db()
        self.assertEqual((line.estado, line.decision_reason), ('retirada', ''))
        with self.assertRaises(lines.LineStateError):
            lines.set_estado(line, 'unificada')
        merged = make_line('W-2', merged_into=line)
        with self.assertRaises(lines.LineStateError):
            lines.set_estado(merged, 'publicada')

    def test_create_and_bulk_writers_keep_the_pair(self):
        created = lines.create_line('W-3', origen='proveedor', decided_by=self.root, decided_at=timezone.now(), description='X')
        self.assertEqual((created.origen, created.estado, created.decided_by), ('proveedor', 'publicada', self.root))
        with self.assertRaises(lines.LineStateError):
            lines.create_line('W-4', estado='unificada')
        with self.assertRaises(lines.LineStateError):
            lines.create_line('W-4', origen='otro')
        new = lines.bulk_create_lines([Part(sku=' w-5 ', active=False), Part(sku='W-6', estado='referencia')])
        self.assertEqual([stored(line) for line in new], [('retirada', False), ('referencia', False)])
        self.assertEqual(Part.objects.get(pk=new[0].pk).sku, 'W-5')
        loaded = list(Part.objects.filter(pk__in=[line.pk for line in new]).order_by('sku'))
        loaded[0].active = True
        lines.bulk_update_lines(loaded, ['active'])
        self.assertEqual(stored(loaded[0]), ('publicada', True))

    def test_touch_visible_stamps_changed_at_only(self):
        line = make_line('W-7')
        at = lines.touch_visible(line)
        self.assertEqual(Part.objects.get(pk=line.pk).changed_at, at)
        lines.touch_visible([line.pk])
        self.assertGreaterEqual(Part.objects.get(pk=line.pk).changed_at, at)


class LineWriterFlowTests(APITestCase):
    """The writers the plan names: merging makes a line unificada, Separar SKU makes it publicada again, the grid's batch edit and a
    catalog import with ACTIVO=0 retire it, an approved proposal and a supplier family come from the supplier."""

    def setUp(self):
        self.root = make_user('flows-root', superuser=True)
        self.client.force_authenticate(self.root)

    def test_merge_makes_the_source_unificada_and_split_makes_it_publicada(self):
        target, source = make_line('16100-29085', description='BOMBA DE AGUA'), make_line('16100-29085-GMB', description='BOMBA DE AGUA')
        merge_catalog_parts(actor=self.root, target_sku=target.sku, source_skus=[source.sku])
        self.assertEqual(stored(source), ('unificada', False))
        plan = self.client.get(f'/api/v1/management/catalog/{target.pk}/split/').json()
        child = next(child for child in plan['children'] if child['sku'] == source.sku)
        codes = [row['id'] for row in plan['codes'] if row['suggested'] == child['id'] and not row.get('alias_of')]
        response = self.client.post(f'/api/v1/management/catalog/{target.pk}/split/', {'restore': [{'id': child['id'], 'codes': codes, 'items': []}]},
                                    format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(stored(source), ('publicada', True))
        self.assertEqual(stored(target), ('publicada', True))

    def test_the_batch_edit_retires_and_publishes_and_refuses_other_estados(self):
        line, draft = make_line('BULK-1'), make_line('BULK-2', estado='preparacion')
        post = lambda rows: self.client.post('/api/v1/management/catalog/bulk-edit/', {'rows': rows}, format='json')
        response = post([{'id': str(line.pk), 'active': False}])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['updated'][0]['estado'], 'retirada')
        self.assertEqual(stored(line), ('retirada', False))
        self.assertEqual(post([{'id': str(line.pk), 'active': True}]).json()['updated'][0]['estado'], 'publicada')
        refused = post([{'id': str(draft.pk), 'active': True}])
        self.assertEqual(refused.status_code, 400)
        self.assertIn('en preparación', refused.json()['detail'])
        self.assertEqual(stored(draft), ('preparacion', False))

    def test_the_part_api_shows_estado_and_origen_read_only(self):
        line = make_line('API-1')
        response = self.client.patch(f'/api/v1/management/catalog/{line.pk}/', {'estado': 'rechazada', 'origen': 'proveedor'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()['estado'], response.json()['origen']), ('publicada', 'catalogo'))
        created = self.client.post('/api/v1/management/catalog/', {'sku': 'API-2', 'active': False}, format='json')
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()['estado'], 'retirada')

    def test_an_import_with_activo_0_retires_and_activo_1_publishes(self):
        def run(active):
            groups = {'IMP-1': {'sku': 'IMP-1', 'row': 2, 'fields': {'description': 'FILTRO', 'active': active}, 'codes': {}}}
            plan, _ = build_plan(groups, [], 1)
            self.assertTrue(plan['valid'], plan)
            save_plan(groups)
            return Part.objects.get(sku='IMP-1')
        self.assertEqual(stored(run(False)), ('retirada', False))
        self.assertEqual(stored(run(True)), ('publicada', True))
        self.assertEqual(stored(run(False)), ('retirada', False))

    def test_an_approved_proposal_creates_a_line_from_the_supplier(self):
        from .sku_proposals import approve, propose
        supplier = make_supplier('PROVEEDOR S1')
        from .matching_engine import item_row, save_case
        item = SupplierItem.objects.create(supplier=supplier, supplier_invent_id='P1', codigo='025CHA-1502607', brand='WURTH',
                                           description='CLIP DE COVER', source='apiag')
        save_case(key=f'supplier:{item.pk}', kind='supplier', sources=[item_row(item)], candidates=[], item=item, status='unmatched')
        propose()
        proposal = SkuProposal.objects.get(sku='025CHA-1502607')
        approve(proposal.pk, self.root, proposal.version)
        proposal.refresh_from_db()
        line = proposal.part
        self.assertEqual((line.origen, line.estado, line.decided_by, line.decided_at), ('proveedor', 'publicada', self.root, proposal.decided_at))

    def test_a_supplier_family_parent_comes_from_the_supplier(self):
        supplier = make_supplier('PROVEEDOR FAMILIAS')
        for code in ('58411-07500-K', '58411-07500-NP'):
            SupplierItem.objects.create(supplier=supplier, supplier_invent_id=code, codigo=code, brand='', source='upload',
                                        description='DISCO KIA PICANTO TRAS 220 MM 04-11', matching_status='pending')
        self.assertEqual(create_supplier_families(), 1)
        line = Part.objects.get(sku='58411-07500')
        self.assertEqual((line.origen, line.estado), ('proveedor', 'publicada'))


class LifecycleBackfillTests(TestCase):
    """python -m mall.refactor.s1_lifecycle on rows as 0071 leaves them: every line publicada and from the catalog."""

    def setUp(self):
        self.root = make_user('backfill-root', superuser=True)
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        patcher = patch.dict(os.environ, {'REFACTOR_OUTPUT': self.output.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        drop_lifecycle_checks()
        self.live, self.target = make_line('B-LIVE'), make_line('B-TARGET')
        self.merged, self.retired = make_line('B-MERGED'), make_line('B-RETIRED')
        self.from_proposal = make_line('B-PROPOSED')
        when = timezone.now() - timedelta(days=3)
        SkuProposal.objects.create(key='BPROPOSED|', sku='B-PROPOSED', fingerprint='x', status='approved', part=self.from_proposal,
                                   decided_by=self.root, decided_at=when)
        self.decided_at = when
        # 0071 leaves the old pair as it was and every estado at its default.
        Part.objects.filter(pk=self.merged.pk).update(merged_into=self.target, active=False, estado='publicada')
        Part.objects.filter(pk=self.retired.pk).update(active=False, estado='publicada')
        make_reference(self.live, 'B-LIVE', brand='MOTION  MOTORS', tipo='aftermarket')

    def run_cli(self, *args):
        out = io.StringIO()
        code = s1_lifecycle.main([*args], stdout=out)
        return code, out.getvalue()

    def manifest(self):
        with open(os.path.join(self.output.name, 'manifest.jsonl'), encoding='utf-8') as handle:
            return [json.loads(line) for line in handle]

    def test_a_dry_run_counts_and_writes_nothing(self):
        code, out = self.run_cli('--dry-run')
        self.assertEqual(code, 0, out)
        self.assertIn('Simulación', out)
        self.assertEqual(stored(self.merged), ('publicada', False))
        self.assertFalse(Brand.objects.exists())
        self.assertFalse(os.path.exists(os.path.join(self.output.name, 'manifest.jsonl')))

    def test_apply_sets_estado_origen_and_brands_once_and_logs_every_row(self):
        self.assertEqual(self.run_cli('--apply')[0], 1)  # --user is required
        code, out = self.run_cli('--apply', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        self.assertEqual(stored(self.merged), ('unificada', False))
        self.assertEqual(stored(self.retired), ('retirada', False))
        self.assertEqual(stored(self.live), ('publicada', True))
        line = Part.objects.get(pk=self.from_proposal.pk)
        self.assertEqual((line.origen, line.decided_by, line.decided_at), ('proveedor', self.root, self.decided_at))
        motion = Brand.objects.get(nombre='MOTION MOTORS')
        self.assertEqual(motion.tipo, 'repuesto')
        self.assertTrue(BrandAlias.objects.filter(alias='MOTION  MOTORS', brand=motion).exists())
        rows = self.manifest()
        self.assertEqual(rows[0]['action'], 'run')
        updates = {row['sku']: row for row in rows if row.get('model') == 'mall.Part'}
        self.assertEqual(set(updates), {'B-MERGED', 'B-RETIRED', 'B-PROPOSED'})
        self.assertEqual((updates['B-MERGED']['before']['estado'], updates['B-MERGED']['after']['estado']), ('publicada', 'unificada'))
        self.assertTrue(any(row.get('model') == 'mall.Brand' and row['after']['nombre'] == 'MOTION MOTORS' for row in rows))
        brands = Brand.objects.count()
        code, out = self.run_cli('--apply', '--user', self.root.username)  # the catch-up after landing: nothing new
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.manifest()), len(rows))
        self.assertEqual(Brand.objects.count(), brands)

    def test_rollback_puts_back_what_the_apply_changed(self):
        self.run_cli('--apply', '--user', self.root.username)
        Part.objects.filter(pk=self.retired.pk).update(description='EDITADA')  # edits outside the pair do not block it
        code, out = self.run_cli('--rollback', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        self.assertEqual(stored(self.merged), ('publicada', False))
        self.assertEqual(stored(self.retired), ('publicada', False))
        self.assertEqual(Part.objects.get(pk=self.from_proposal.pk).origen, 'catalogo')
        self.assertFalse(Brand.objects.exists())
        self.assertFalse(BrandAlias.objects.exists())
        code, out = self.run_cli('--rollback', '--user', self.root.username)  # again: nothing left to put back
        self.assertEqual(code, 0, out)
        self.assertIn('0 filas restauradas · 0 marcas o alias borrados · 0 cambiadas', out)

    def test_rollback_refuses_while_the_tighten_checks_are_in_place(self):
        with patch.object(s1_lifecycle, 'checks_in_place', return_value=True):
            code, out = self.run_cli('--rollback', '--user', self.root.username)
        self.assertEqual(code, 1)
        self.assertIn('0072', out)

    def test_a_manifest_of_another_database_is_refused(self):
        with open(os.path.join(self.output.name, 'manifest.jsonl'), 'w', encoding='utf-8') as handle:
            handle.write(json.dumps({'stage': 's1', 'database': 'partsmall_rehearsal', 'action': 'run'}) + '\n')
        code, out = self.run_cli('--apply', '--user', self.root.username)
        self.assertEqual(code, 1)
        self.assertIn('otra base', out)
        self.assertEqual(stored(self.merged), ('publicada', False))
