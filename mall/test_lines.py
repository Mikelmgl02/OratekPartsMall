"""The OEM line's lifecycle (inventory refactor S1, S10): estado decides and active mirrors it (S10), the readers' querysets, and the
writers that merge, split, retire and create lines."""
from datetime import timedelta

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from . import lines
from .catalog_grouping import merge_catalog_parts
from .catalog_import import build_plan, save_plan
from .models import Part, SupplierItem
from .test_factories import line_fields, make_line, make_supplier, make_user, run_migration_function

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


class LineMirrorTests(TestCase):
    """estado is the line's lifecycle and active its mirror (S10: a generated column from 0089; 0086's trigger wrote it until then)."""

    def test_a_new_line_is_publicada_from_the_catalog_and_stamped(self):
        line = make_line('16100-39315')
        self.assertEqual((line.estado, line.active, line.origen, line.version), ('publicada', True, 'catalogo', 1))
        self.assertIsNotNone(line.created_at)
        self.assertIsNotNone(line.updated_at)
        self.assertIsNone(line.changed_at)
        self.assertEqual(stored(line), ('publicada', True))

    def test_active_follows_estado_and_a_write_of_active_alone_changes_nothing(self):
        line = make_line('REF-1', estado='referencia')
        self.assertEqual(stored(line), ('referencia', False))
        line = Part.objects.get(pk=line.pk)
        line.estado = 'publicada'
        line.save(update_fields=['estado'])
        self.assertEqual((stored(line), line.active), (('publicada', True), True))
        both = make_line('REF-2', estado='preparacion', active=True)  # the test factory drops active: estado decides
        self.assertEqual(stored(both), ('preparacion', False))
        retired = make_line('RET-1', active=False)
        self.assertEqual(stored(retired), ('retirada', False))
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:  # a generated column (0089)
            cursor.execute('UPDATE mall_part SET active = true WHERE id = %s', [retired.pk])
        self.assertEqual(stored(retired), ('retirada', False))

    def test_a_merged_line_is_unificada(self):
        target = make_line('16100-39316')
        merged = make_line('16100-39316-GMB', merged_into=target)
        self.assertEqual(stored(merged), ('unificada', False))

    def test_a_save_of_other_fields_leaves_the_estado_and_stamps_updated_at(self):
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
        Part.objects.filter(pk=line.pk).update(estado='retirada')
        line.refresh_from_db()
        line.description = 'CAMBIO'
        line.save()
        self.assertEqual(stored(line), ('retirada', False))

    def test_a_deferred_estado_assigned_since_load_wins_and_save_loads_nothing_more(self):
        line = make_line('DEFER-1')
        loaded = Part.objects.only('pk', 'sku').get(pk=line.pk)
        loaded.estado = 'retirada'
        loaded.save()
        self.assertEqual(stored(line), ('retirada', False))
        loaded = Part.objects.only('pk', 'sku', 'description').get(pk=line.pk)
        loaded.description = 'SOLO LA DESCRIPCION'
        loaded.save(update_fields=['description'])
        self.assertNotIn('estado', loaded.__dict__)  # nothing is loaded to mirror
        self.assertEqual(stored(line), ('retirada', False))


class LifecycleCheckTests(TestCase):
    """0072's CHECKs: an unknown estado or origen, unificada without its line (or the other way) and a rejection without its reason
    fail (active is the trigger's since S10: no writer can set it out of step)."""

    def assert_refused(self, **values):
        line = make_line(f'CHK-{len(values)}-{"-".join(map(str, values.values()))}'[:60])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Part.objects.filter(pk=line.pk).update(**values)

    def test_the_checks_refuse_half_a_pair_and_unknown_values(self):
        target = make_line('CHK-TARGET')
        for values in ({'merged_into': target}, {'estado': 'unificada'}, {'estado': 'borrada'}, {'origen': 'otro'}, {'estado': 'rechazada'}):
            with self.subTest(values=values):
                self.assert_refused(**values)
        line = make_line('CHK-OK')
        Part.objects.filter(pk=line.pk).update(estado='retirada')
        self.assertEqual(stored(line), ('retirada', False))
        Part.objects.filter(pk=line.pk).update(merged_into=target, estado='unificada')
        Part.objects.filter(pk=make_line('CHK-REJ').pk).update(estado='rechazada', decision_reason='other', decided_at=timezone.now())


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

    def test_create_and_bulk_writers_check_the_estado(self):
        created = lines.create_line('W-3', origen='proveedor', decided_by=self.root, decided_at=timezone.now(), description='X')
        self.assertEqual((created.origen, created.estado, created.decided_by), ('proveedor', 'publicada', self.root))
        with self.assertRaises(lines.LineStateError):
            lines.create_line('W-4', estado='unificada')
        with self.assertRaises(lines.LineStateError):
            lines.create_line('W-4', origen='otro')
        new = lines.bulk_create_lines([Part(**line_fields(dict(sku=' w-5 ', active=False))), Part(sku='W-6', estado='referencia')])
        self.assertEqual([stored(line) for line in new], [('retirada', False), ('referencia', False)])
        self.assertEqual(Part.objects.get(pk=new[0].pk).sku, 'W-5')
        loaded = list(Part.objects.filter(pk__in=[line.pk for line in new]).order_by('sku'))
        lines.bulk_update_lines(loaded, lines.stage_estado(loaded[0], 'publicada'))
        self.assertEqual(stored(loaded[0]), ('publicada', True))
        with self.assertRaises(TypeError):  # S10: active is no column a writer names
            lines.bulk_update_lines(loaded, ['active'])

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
        line.refresh_from_db()  # recorded as mall.lines.set_estado records a person's decision
        self.assertEqual((line.version, line.decided_by), (2, self.root))
        self.assertIsNotNone(line.decided_at)
        self.assertEqual(line.changed_at, line.decided_at)
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

    def test_a_caller_of_the_part_serializer_may_choose_the_estado(self):
        from types import SimpleNamespace
        from .management import ManagedPartSerializer
        serializer = ManagedPartSerializer(data={'sku': 'API-3'}, context={'request': SimpleNamespace(user=self.root)})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        line = serializer.save(estado='preparacion', origen='proveedor')
        self.assertEqual((stored(line), Part.objects.get(pk=line.pk).origen), (('preparacion', False), 'proveedor'))

    def test_an_import_never_moves_a_line_off_the_catalog(self):
        draft = make_line('IMP-P', estado='preparacion')
        for fields in ({'description': 'FILTRO', 'active': True}, {'description': 'FILTRO'}):
            groups = {draft.sku: {'sku': draft.sku, 'row': 2, 'fields': fields, 'codes': {}}}
            plan, _ = build_plan(groups, [], 1)
            self.assertFalse(plan['valid'])
            self.assertIn('está en preparación y no forma parte del catálogo', plan['errors'][0]['message'])
        self.assertEqual(stored(draft), ('preparacion', False))

    def test_an_import_issue_for_a_line_off_the_catalog_stays_pending(self):
        from .models import CatalogImportIssue, CatalogImportJob
        line = make_line('MR-968365', estado='preparacion', origen='proveedor')  # a line in referencia is promoted instead (S4)
        job = CatalogImportJob.objects.create(owner=self.root, filename='LINEAS.xlsx', status='completed', expires_at=timezone.now())
        issue = CatalogImportIssue.objects.create(job=job, row=7, values={**dict.fromkeys(['sku', 'name', 'description', 'code', 'brand', 'active',
                                                                                             'category', 'subcategory'], ''), 'sku': line.sku},
                                                  original={}, errors=[{'row': 7, 'column': 'SKU', 'message': 'Revisa la fila.'}])
        response = self.client.post(f'/api/v1/management/catalog/import/issues/{issue.pk}/', {'values': {'description': 'EJE', 'active': 'NO'}},
                                    format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['valid'])
        self.assertIn('en preparación', str(response.json()['errors']))
        issue.refresh_from_db()
        self.assertEqual((issue.status, issue.part_id), ('pending', None))
        self.assertEqual(stored(line), ('preparacion', False))

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

    def test_an_approved_new_product_is_a_line_from_the_supplier(self):
        from .new_products import place, publish
        supplier = make_supplier('PROVEEDOR S1')
        item = SupplierItem.objects.create(supplier=supplier, supplier_invent_id='P1', codigo='025CHA-1502607', brand='WURTH',
                                           description='CLIP DE COVER', source='apiag')
        line, _ = place(item)   # S6: Productos nuevos is a line in preparación
        line.refresh_from_db()
        self.assertEqual((line.origen, line.estado, line.active), ('proveedor', 'preparacion', False))
        publish(line.pk, self.root, line.version)
        line.refresh_from_db()
        self.assertEqual((line.origen, line.estado, line.decided_by, line.active), ('proveedor', 'publicada', self.root, True))
        self.assertIsNotNone(line.decided_at)

    def test_a_new_product_line_comes_from_the_supplier(self):
        # S6: no parent is invented from suffixed codes any more; a code no line has gets a line of its own in preparación.
        from .reference_matching import backlog
        supplier = make_supplier('PROVEEDOR FAMILIAS')
        for code in ('58411-07500-K', '58411-07500-NP'):
            SupplierItem.objects.create(supplier=supplier, supplier_invent_id=code, codigo=code, brand='', source='upload',
                                        description='DISCO KIA PICANTO TRAS 220 MM 04-11', matching_status='pending')
        backlog()
        self.assertFalse(Part.objects.filter(sku='58411-07500').exists())  # estado: no invented parent in any estado
        for line in Part.objects.filter(sku__in=['58411-07500-K', '58411-07500-NP']):  # estado: the new products, in preparación
            self.assertEqual((line.origen, line.estado), ('proveedor', 'preparacion'))


def suffix(token, attribution, *, confidence='low', status='owner_confirmed', kind='brand'):
    """A brand TAG row of the code suffix table as catalog_suffixes reads it."""
    return {'token': token, 'match': 'exact', 'cls': 'TAG', 'tag_kind': kind, 'variant_kind': '', 'attribution': attribution,
            'creates_company_reference': False, 'scope_overrides': [], 'confidence': confidence, 'status': status,
            'owner_confirmed': status == 'owner_confirmed', 'alias_of': None, 'notes': ''}


