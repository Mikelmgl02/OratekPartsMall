"""Inventory refactor S2b, review fixes: matching still reads whose code a branded main is, Editar saves a line whose main waits in
disputa, a SKU longer than a Reference is refused as a row error, ensure_mains waits for the backfill, survives one bad line and opens the
merge case a disputa main lacks, prefer_oem opens the case set_main promises, supplier families never leave a main in disputa, the
backfill's catch-up never reopens a decided case, its rollback leaves a merge it cannot undo and deletes the mains the code made, Separar
gives an OEM review case back whole, and the matching service's account stays disabled."""
import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from . import line_merge, references
from .catalog_identity import prefer_oem
from .lines import create_line
from .matching_engine import part_row
from . import reference_matching as rm
from .matching_models import MatchingCase
from .models import CatalogMerge, Part, Reference
from .oem_finder_models import OEMReviewCase
from .test_factories import Fresh, make_line, make_reference, make_supplier, make_user, make_variant
from .test_main_references import broken_invariants

TIGHTEN = {'mall_part': ('line_oem_iff_tipo',), 'mall_partcode': ('reference_one_main_per_line', 'reference_main_not_retired')}


def drop_tighten_constraints():
    """0076's constraints when they exist, inside a test's transaction (they come back when it rolls back): the state the backfill
    runs in."""
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
        for table, names in TIGHTEN.items():
            present = connection.introspection.get_constraints(cursor, table)
            for name in names:
                if name in present:
                    if present[name].get('check') or not present[name].get('index'):
                        cursor.execute(f'ALTER TABLE {table} DROP CONSTRAINT {name}')
                    else:
                        cursor.execute(f'DROP INDEX {name}')


def main(line):
    return references.main_of(line)


class MatchIndexMainTests(TestCase):
    def test_a_branded_main_still_says_whose_code_the_codigo_is(self):
        oem, company = make_line('16100-39315', is_OEM=True), make_line('GWT-41A')
        references.retype_main(oem, 'oem', brand='TOYOTA')
        references.retype_main(company, 'aftermarket', brand='GMB')
        supplier = make_supplier('S2B MAIN')
        subject = lambda code, brand: rm.subject_of(None, {'codigo': code, 'brand': brand}, supplier)
        # S6 (rules 1 and 2): a branded main is that brand's code; another brand's item with it finds no line ...
        self.assertEqual(rm.resolve(subject('16100-39315', 'HONDA')).action, 'no_line')
        self.assertEqual(rm.resolve(subject('16100-39315', 'TOYOTA')).line, oem)
        self.assertEqual(rm.resolve(subject('GWT-41A', 'GMB')).line, company)
        self.assertEqual(part_row(Part.objects.prefetch_related('codes').get(pk=oem.pk))['codes'], [])  # fingerprints stay
        bare = make_line('16100-39316', is_OEM=True)  # ... while a brandless main reserves its code for every brand
        self.assertEqual(rm.resolve(subject('16100-39316', 'HONDA')).line, bare)

    def test_retype_main_makes_its_brand_a_brand(self):
        from .brand_models import Brand
        line = make_line('0110-XYZ123-QQQ')
        references.retype_main(line, 'aftermarket', brand='MARCA NUEVA S2B')
        self.assertTrue(Brand.objects.filter(nombre='MARCA NUEVA S2B').exists())  # I12


class EditarTests(APITestCase):
    def setUp(self):
        self.root = make_user('s2b-review-admin', superuser=True)
        self.client.force_authenticate(self.root)

    def test_a_line_whose_main_waits_in_disputa_still_saves_its_other_fields(self):
        keeper = make_line('13780-75F00')
        duplicate = create_line('13780-75F00_')  # its key is the keeper's: the main waits in disputa
        self.assertEqual(main(duplicate).estado, 'disputa')
        url = f'/api/v1/management/catalog/{duplicate.pk}/'
        response = self.client.patch(url, {'sku': '13780-75F00_', 'description': 'BOMBA DE AGUA'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Part.objects.get(pk=duplicate.pk).description, 'BOMBA DE AGUA')
        renamed = self.client.patch(url, {'sku': '1378075F00'}, format='json')  # another spelling of the keeper's key is still refused
        self.assertEqual(renamed.status_code, 400)
        self.assertEqual(Part.objects.get(pk=keeper.pk).sku, '13780-75F00')


class LongSkuTests(TestCase):
    def test_a_reference_longer_than_its_column_is_refused_before_the_database(self):
        line = make_line('LONG-1')
        with self.assertRaises(references.ReferenceConflict):
            references.build(line, 'X' * 121)
        long = Part(sku='Y' * 121, estado='publicada', origen='catalogo')
        with self.assertRaises(references.ReferenceConflict):
            references.bulk_mains([long])

    def test_the_catalog_import_refuses_a_sku_longer_than_120_as_a_row_error(self):
        from openpyxl import Workbook
        from .catalog_import import read_excel
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Inventario'
        for row in (['SKU', 'NOMBRE'], ['A' * 121, 'LARGO'], ['CORTO-1', 'CORTO']):
            sheet.append(row)
        stream = io.BytesIO()
        workbook.save(stream)
        groups, errors, rows = read_excel(stream.getvalue())
        self.assertEqual((sorted(groups), rows), (['CORTO-1'], 2))
        self.assertEqual([(error['row'], error['column']) for error in errors], [(2, 'SKU')])
        self.assertIn('120 caracteres', errors[0]['message'])


class EnsureMainsTests(TestCase):
    def test_it_waits_for_the_backfill_survives_a_bad_line_and_opens_the_case_a_disputa_main_lacks(self):
        lines = [make_line(f'ENSR-{n}') for n in range(3)]
        references.purge(Reference.all_objects.filter(part__in=lines).values_list('pk', flat=True))
        with patch.object(references, 'BACKFILL_PENDING_LINES', 2):
            self.assertEqual(references.ensure_mains()['status'], 'backfill_pending')
        self.addCleanup(references.FAILED_MAINS.clear)
        real = references.ensure_main

        def flaky(line, **kwargs):
            if line.pk == lines[0].pk:
                raise references.ReferenceConflict('una línea rota')
            return real(line, **kwargs)
        with patch.object(references, 'ensure_main', side_effect=flaky):
            result = references.ensure_mains()
        self.assertEqual((result['status'], result['lines'], result['failed']), ('ok', 2, 1))
        self.assertIn(lines[0].pk, references.FAILED_MAINS)
        self.assertEqual(references.ensure_mains()['lines'], 0)  # left out of this process's later passes
        keeper = make_line('13780-75F00')
        duplicate = create_line('13780-75F00_')
        first = references.ensure_mains()
        case = MatchingCase.objects.get(guard='main_codigo')  # S10: a merge case (a catalog case before)
        self.assertEqual((first['cases'], case.kind, case.target_sku, [row['id'] for row in case.candidates]),
                         (1, 'merge', keeper.sku, [str(duplicate.pk)]))
        case.status = 'dismissed'
        case.save(update_fields=['status'])
        self.assertEqual(references.ensure_mains()['cases'], 0)  # a person decided: never reopened
        self.assertEqual(MatchingCase.objects.get(pk=case.pk).status, 'dismissed')

class PreferOemTests(TestCase):
    def test_an_oem_that_is_another_lines_codigo_opens_a_merge_case(self):
        holder = make_line('MR-968365')  # its brandless main reserves the key: the line's OEM code waits in disputa (the backfill's note)
        line = make_line('0110-MR968365-FEB')
        make_reference(line, 'MR968365', brand='MITSUBISHI', tipo='oem', source='CATÁLOGO REVISADO', estado='disputa',
                       dispute_note='El código MR968365 es el SKU MR-968365: caso de unificación.')
        prefer_oem(line)
        case = MatchingCase.objects.get(guard='main_codigo')
        self.assertEqual(([row['id'] for row in case.candidates], case.target_sku), ([str(line.pk)], holder.sku))
        prefer_oem(line)  # every pass: still one case
        self.assertEqual(MatchingCase.objects.filter(guard='main_codigo').count(), 1)
        self.assertEqual(Part.objects.get(pk=line.pk).sku, '0110-MR968365-FEB')


class SupplierFamilyTests(TestCase):
    def test_a_parent_whose_key_another_line_holds_is_never_made(self):
        root = make_user('s2b-family')
        supplier = make_supplier('S2B FAMILIAS')
        holder = make_line('OTHER')
        make_reference(holder, '23300-79185', brand='TOYOTA', tipo='oem')
        for n, code in enumerate(('23300-79185-A', '23300-79185-B')):
            make_variant(supplier, f'FAM-{n}', code, 1, actor=root, description='FILTRO COMBUSTIBLE TOY')
        # S6: no parent is invented from suffixed codes (create_supplier_families is gone): each code is a new product of its own.
        self.assertFalse(Part.objects.filter(sku='23300-79185').exists())  # estado: no line in any estado
        self.assertFalse(Reference.objects.filter(estado='disputa').exists())


class RollbackHookTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user('s2b-hook', superuser=True)
        self.keeper = make_line('16100-39315', description='BOMBA AGUA TOY HILUX')
        self.duplicate = make_line('16100-39315_', description='BOMBA AGUA TOY HILUX')

    def test_the_service_account_an_automatic_merge_creates_stays_disabled(self):
        get_user_model().objects.filter(username=line_merge.SERVICE_ACCOUNT).delete()
        line_merge.merge_lines(self.keeper, [self.duplicate], actor=self.root, reason='compact_duplicate', automatic=True)
        actor = get_user_model().objects.get(username=line_merge.SERVICE_ACCOUNT)
        self.assertEqual((actor.is_active, actor.has_usable_password()), (False, False))

    def test_separar_gives_an_oem_review_case_back_whole(self):
        decided = timezone.now() - timedelta(days=3)
        review = OEMReviewCase.objects.create(part=self.duplicate, fingerprint='f', tier='REVIEW', decision={'note': 'mirar'},
                                              decided_at=decided, decided_by=self.root)
        line_merge.merge_lines(self.keeper, [self.duplicate], actor=self.root, reason='prueba')
        line_merge.split_line(self.keeper.pk, [{'id': str(self.duplicate.pk), 'codes': [], 'items': [], 'images': []}], actor=self.root)
        review.refresh_from_db()
        self.assertEqual((review.status, review.decision, review.decided_at, review.decided_by_id), ('review', {'note': 'mirar'}, decided, self.root.pk))


class ExpandMigrationTests(TestCase):
    def test_0075_reverses_with_the_pairs_a_dismissed_case_wrote_and_applies_again(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from .catalog_split_models import CatalogSeparation
        first, second = make_line('SEP-A'), make_line('SEP-B')
        self.assertEqual(line_merge.keep_apart([first.pk, second.pk], reason='shared_oem_number'), 1)  # no split: a dismissed case's pairs
        with connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')  # this test's own writes checked now: DDL refuses pending trigger events
        executor = MigrationExecutor(connection)
        leaf = executor.loader.graph.leaf_nodes('mall')
        executor.migrate([('mall', '0082_grupo_checks')])  # 0076 (when applied) and 0075 reversed
        with connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM mall_catalogseparation WHERE split_id IS NULL')
            self.assertEqual(cursor.fetchone()[0], 1)  # the pair stays, kept apart, as the code before S2b reads it
        MigrationExecutor(connection).migrate(leaf)
        self.assertEqual(CatalogSeparation.objects.filter(split__isnull=True).count(), 1)
        self.assertEqual(broken_invariants(), {})
