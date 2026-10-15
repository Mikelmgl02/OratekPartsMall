"""Variants hang from References (inventory refactor S3, mall.variants): which Reference of its line an item takes, the save() shim, the
code changes a load applies on its own or leaves waiting (rule 5b, OD5, OD7), Vincular, the refusal to retire a Reference items hang
from, and Reference moves, merges and Separar carrying their items along."""
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from . import line_merge, references, variants
from .brand_models import Brand
from .catalog_suffix_models import CatalogCodeSuffix
from .models import Part, Reference, ReferenceSource, SupplierItem
from .services import ingest_inventory
from .test_factories import (Fresh, drop_variant_constraints, make_line, make_reference, make_supplier, make_user, make_variant,
                             run_migration_function)
from .test_main_references import broken_invariants

VARIANT_INVARIANTS = {
    'I4': 'SELECT count(*) FROM mall_supplieritem s JOIN mall_partcode r ON r.id = s.reference_id WHERE s.part_id IS DISTINCT FROM r.part_id',
    'I7': "SELECT count(*) FROM mall_supplieritem s JOIN mall_partcode r ON r.id = s.reference_id WHERE r.tipo = 'interno' "
          'AND r.supplier_id <> s.supplier_id',
    'linea_sin_referencia': 'SELECT count(*) FROM mall_supplieritem WHERE (reference_id IS NULL) <> (part_id IS NULL)',
    'sin_referencia_no_pendiente': "SELECT count(*) FROM mall_supplieritem WHERE reference_id IS NULL AND matching_status <> 'pending'",
}


def broken_variants():
    """{invariant: rows breaking it} for I4, I7 and 0078's CHECKs, empty when they all hold."""
    found = {}
    with connection.cursor() as cursor:
        for name, query in VARIANT_INVARIANTS.items():
            cursor.execute(query)
            count = cursor.fetchone()[0]
            if count:
                found[name] = count
    return found


class VariantTestCase(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user('var-root', superuser=True)
        self.supplier = make_supplier('VAR PROVEEDOR', self.root)
        self.other = make_supplier('VAR OTRO')
        Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
        Brand.objects.create(nombre='GMB', tipo='repuesto')
        Brand.objects.create(nombre='ENDO', tipo='repuesto')
        self.plain = make_line('04111-22152', description='BOMBA AGUA')
        self.branded = make_line('43512-60150', description='DISCO FRENO', is_OEM=True)
        references.retype_main(self.branded, 'oem', brand='TOYOTA')
        self.alias = make_reference(self.plain, '04111-22152-AS')
        self.updates = 0

    def loose(self, invent_id, codigo, supplier=None):
        """An item with no line and no Reference, written without a load (from S6 a load gives every item a line: one of its own in
        Productos nuevos when no line has its code)."""
        return SupplierItem.objects.create(supplier=supplier or self.supplier, supplier_invent_id=invent_id, codigo=codigo, brand='',
                                           source='upload')

    def load(self, invent_id, codigo, brand='', quantity=1, supplier=None, **data):
        self.updates += 1
        return ingest_inventory(supplier=supplier or self.supplier, actor=self.root, data={
            'update_id': f'var-{self.updates}', 'supplier_invent_id': invent_id, 'codigo': codigo, 'brand': brand, 'source': 'upload',
            'quantity': quantity, **data})

    def tearDown(self):
        self.assertEqual(broken_variants(), {})
        self.assertEqual(broken_invariants(), {})
        super().tearDown()


class WhichReferenceTests(VariantTestCase):
    def test_an_exact_load_hangs_from_its_code(self):
        main = references.main_of(self.plain)
        item = self.load('A-1', '04111-22152')
        self.assertEqual((item.part_id, item.reference_id, item.matching_status), (self.plain.pk, main.pk, 'matched'))
        alias = self.load('A-2', '04111-22152-AS')
        self.assertEqual((alias.part_id, alias.reference_id), (self.plain.pk, self.alias.pk))

    def test_a_brandless_item_joins_the_branded_reference_of_its_clave(self):
        item = self.load('B-1', '43512-60150')
        self.assertEqual((item.reference_id, item.reference.brand, item.matching_status),
                         (references.main_of(self.branded).pk, 'TOYOTA', 'matched'))

    def test_a_branded_item_joins_the_brandless_reference_and_spaces_do_not_count(self):
        item = self.load('C-1', '04111-22152', brand='GMB')
        self.assertEqual(item.reference_id, references.main_of(self.plain).pk)
        spaced = make_line('AL-X7-5P', description='ALARMA')
        own = make_reference(spaced, 'AL-X7-5P', brand='MOTION MOTORS', tipo='aftermarket')
        rows = list(Reference.objects.filter(part=spaced))
        self.assertEqual(variants.choose(rows, spaced.sku, 'AL-X7-5P', 'MOTION  MOTORS', self.supplier.pk), (own, 'exacta'))

    def test_what_the_line_lacks_becomes_a_public_or_an_interno_reference(self):
        CatalogCodeSuffix.objects.create(token='ZQX', cls='TAG', tag_kind='brand', attribution='brand: GMB', confidence='high',
                                         status='owner_confirmed', owner_confirmed=True)
        rows = list(Reference.objects.filter(part=self.branded))
        self.assertEqual(variants.choose(rows, self.branded.sku, '43512-60150', 'ENDO', self.supplier.pk), (None, 'nueva'))
        self.assertEqual(variants.choose(rows, self.branded.sku, '43512-60150-ZQX', '', self.supplier.pk), (None, 'nueva'))
        self.assertEqual(variants.choose(rows, self.branded.sku, '43512-60150-L', '', self.supplier.pk), (None, 'nueva_interna'))
        self.assertEqual(variants.choose(rows, self.branded.sku, 'DFSK-0716', 'DFSK', self.supplier.pk), (None, 'nueva_interna'))
        # Linking an item never publishes its code (only the S3 backfill applies the B19 rules): the supplier's own interno code.
        endo = make_variant(self.supplier, 'D-1', 'ZZ-NOMATCH-1', 2, actor=self.root, brand='ENDO')
        endo.codigo = '43512-60150'
        endo.save(update_fields=['codigo'])
        variants.vincular(endo, self.branded, actor=self.root)
        own = endo.reference
        self.assertEqual((own.code, own.brand, own.tipo, own.supplier_id, own.estado), ('43512-60150', 'ENDO', 'interno', self.supplier.pk, 'declarado'))
        self.assertTrue(ReferenceSource.objects.filter(reference=own, kind='proveedor').exists())
        self.assertNotIn(own, self.branded.codes.all())  # readers never see it
        self.assertEqual(variants.reference_for(self.branded, '43512-60150', 'GMB', self.other, public=True), (None, 'nueva'))
        local = make_variant(self.supplier, 'D-2', 'DFSK-0716', 1, actor=self.root, brand='DFSK', line=self.branded)
        self.assertEqual((local.reference.tipo, local.reference.supplier_id, local.reference.part_id), ('interno', self.supplier.pk, self.branded.pk))
        # Another supplier's item with the same code gets its own interno code, never this supplier's (I7).
        theirs = make_variant(self.other, 'D-3', 'DFSK-0716', 1, actor=self.root, brand='DFSK', line=self.branded)
        self.assertNotEqual(theirs.reference_id, local.reference_id)
        self.assertEqual(theirs.reference.supplier_id, self.other.pk)

    def test_an_item_on_a_line_outside_the_catalog_is_pending(self):
        prepared = make_line('PREP-0001', estado='referencia', active=False)
        item = self.loose('E-1', 'PREP-0001')
        self.assertIsNone(item.part_id)
        variants.vincular(item, prepared, status='matched', actor=self.root)
        self.assertEqual((item.part_id, item.matching_status), (prepared.pk, 'pending'))
        Part.objects.filter(pk=prepared.pk).update(estado='publicada')
        prepared.refresh_from_db()
        self.assertEqual(variants.recompute_status(prepared), 1)
        self.assertEqual(SupplierItem.objects.get(pk=item.pk).matching_status, 'matched')


class ShimTests(VariantTestCase):
    def test_code_that_writes_part_gets_the_reference_and_vincular_follows_a_new_part(self):
        item = self.loose('S-1', 'NO-MATCH-1')
        self.assertEqual((item.part_id, item.reference_id, item.matching_status), (None, None, 'pending'))
        item.part, item.matching_status = self.plain, 'matched'
        item.save(update_fields=['part', 'matching_status'])
        item.refresh_from_db()
        self.assertEqual((item.reference.part_id, item.reference.tipo, item.reference.supplier_id), (self.plain.pk, 'interno', self.supplier.pk))
        interno = item.reference_id
        item.part = self.branded
        item.save()
        item.refresh_from_db()
        self.assertEqual((item.reference_id, item.reference.part_id), (interno, self.branded.pk))  # its only item's code goes with it
        item.part = None
        item.save()
        item.refresh_from_db()
        self.assertEqual((item.reference_id, item.matching_status), (None, 'pending'))

    def test_a_save_that_does_not_touch_the_line_reads_nothing(self):
        item = self.load('S-2', '04111-22152')
        item.reserved_quantity = 1
        with self.assertNumQueries(1):
            item.save(update_fields=['reserved_quantity'])


class CodeChangeTests(VariantTestCase):
    def test_a_brand_after_no_brand_stays_matched_on_its_own_key(self):
        item = self.load('G-1', '04111-22152', quantity=5)
        main = item.reference
        item = self.load('G-1', '04111-22152', brand='GMB', quantity=5)
        self.assertEqual((item.brand, item.codigo_pendiente, item.matching_status, item.part_id), ('GMB', None, 'matched', self.plain.pk))
        self.assertEqual((item.reference.clave, item.reference.brand, item.reference.estado), (main.clave, 'GMB', 'declarado'))
        self.assertTrue(ReferenceSource.objects.filter(reference=item.reference, kind='proveedor').exists())
        onto_main = self.load('G-2', '43512-60150', quantity=1)
        onto_main = self.load('G-2', '43512-60150', brand='TOYOTA', quantity=1)
        self.assertEqual((onto_main.reference_id, onto_main.matching_status), (references.main_of(self.branded).pk, 'matched'))

    def test_a_formatting_change_applies_and_keeps_the_spelling(self):
        line = make_line('13780-61J00', description='FILTRO AIRE')
        item = self.load('F-1', '13780-61J00')
        item = self.load('F-1', '1378061J00')
        self.assertEqual((item.codigo, item.codigo_pendiente, item.matching_status, item.reference_id),
                         ('1378061J00', None, 'matched', references.main_of(line).pk))
        self.assertIn('1378061J00', references.main_of(line).printed_forms)

    def test_a_code_already_on_the_same_line_applies_on_its_own(self):
        item = self.load('O-1', '04111-22152')
        item = self.load('O-1', '04111-22152-AS')
        self.assertEqual((item.codigo, item.codigo_pendiente, item.reference_id, item.matching_status),
                         ('04111-22152-AS', None, self.alias.pk, 'matched'))

    def test_any_other_change_waits_out_of_stock_with_the_old_code(self):
        make_line('OTHER-123', description='OTRA COSA')
        item = self.load('P-1', '04111-22152', quantity=3)
        reference = item.reference_id
        item = self.load('P-1', 'OTHER-123', quantity=3)
        self.assertEqual((item.codigo, item.brand, item.codigo_pendiente, item.brand_pendiente, item.matching_status, item.reference_id),
                         ('04111-22152', '', 'OTHER-123', '', 'review', reference))
        again = self.load('P-1', 'OTHER-123', quantity=4)
        self.assertEqual((again.codigo, again.codigo_pendiente, again.matching_status, again.reported_quantity), ('04111-22152', 'OTHER-123', 'review', 4))
        back = self.load('P-1', '04111-22152', quantity=4)
        self.assertEqual((back.codigo_pendiente, back.matching_status), (None, 'matched'))

    def test_an_unattached_item_takes_the_new_code(self):
        # S6: an item without a Reference waits in a line case (its code on two lines); a new code is simply its code, resolved again.
        for sku, brand in (('UNK-A', 'TOYOTA'), ('UNK-B', 'GMB')):
            make_reference(make_line(sku), 'UNKNOWN-1', brand=brand)
        item = self.load('U-1', 'UNKNOWN-1')
        self.assertEqual((item.reference_id, item.matching_status), (None, 'pending'))
        item = self.load('U-1', 'UNKNOWN-2')
        self.assertEqual((item.codigo, item.codigo_pendiente, item.part.estado, item.part.sku), ('UNKNOWN-2', None, 'preparacion', 'UNKNOWN-2'))

    def test_the_excel_preview_shows_the_waiting_code(self):
        from .supplier_import import stock_plan
        make_line('OTHER-456', description='OTRA')
        self.load('X-1', '04111-22152')
        rows = [{'row': 2, 'data': {'supplier_invent_id': 'X-1', 'codigo': 'OTHER-456', 'brand': '', 'quantity': 1}},
                {'row': 3, 'data': {'supplier_invent_id': 'X-2', 'codigo': '04111-22152-AS', 'brand': '', 'quantity': 1}}]
        plan, _ = stock_plan(self.supplier, rows)
        first, second = plan['preview']
        self.assertEqual((first['codigo_pendiente'], first['matching_status'], first['part_id']), ('OTHER-456', 'review', str(self.plain.pk)))
        self.assertEqual((second['codigo_pendiente'], second['matching_status']), (None, 'matched'))
        self.assertIn('pendiente de revisión', plan['warnings'][0]['message'])

    def test_vincular_applies_the_waiting_code(self):
        other = make_line('OTHER-789', description='OTRA')
        item = self.load('V-1', '04111-22152')
        item = self.load('V-1', 'OTHER-789')
        variants.vincular(item, other, actor=self.root)
        item.refresh_from_db()
        self.assertEqual((item.codigo, item.codigo_pendiente, item.part_id, item.reference_id, item.matching_status),
                         ('OTHER-789', None, other.pk, references.main_of(other).pk, 'matched'))


    def test_the_ledger_records_the_waiting_code(self):
        from .inventory_history import field_changes, snapshot
        make_line('OTHER-321', description='OTRA')
        item = self.load('L-1', '04111-22152')
        before = snapshot(item)
        item = self.load('L-1', 'OTHER-321')
        self.assertEqual(field_changes(before, item), {'codigo_pendiente': [None, 'OTHER-321'], 'brand_pendiente': [None, '']})

    def test_the_matching_only_proposes_a_waiting_code_and_its_approval_applies_it(self):
        from . import reference_matching
        from .matching_models import MatchingCase
        other = make_line('OTHER-999', description='BOMBA AGUA')
        item = self.load('W-1', '04111-22152', description='BOMBA AGUA')
        item = self.load('W-1', 'OTHER-999', description='BOMBA AGUA')
        case = MatchingCase.objects.get(item=item)  # S6: a code_change case, opened by the load itself
        item.refresh_from_db()
        self.assertEqual((case.kind, case.status, case.new_code, item.matching_status, item.codigo_pendiente, item.part_id),
                         ('code_change', 'review', 'OTHER-999', 'review', 'OTHER-999', self.plain.pk))
        self.assertEqual(case.sources[0]['codigo_pendiente'], 'OTHER-999')
        self.assertEqual(set(case.lines.values_list('pk', flat=True)), {self.plain.pk, other.pk})
        reference_matching.decide(case, 'approve', actor=self.root)
        item.refresh_from_db()
        self.assertEqual((item.codigo, item.codigo_pendiente, item.part_id, item.reference_id, item.matching_status),
                         ('OTHER-999', None, other.pk, references.main_of(other).pk, 'matched'))


class InventoryMatchTests(VariantTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.root)

    def url(self, item):
        return f'/api/v1/management/inventory/{item.pk}/'

    def test_vincular_unlink_and_status(self):
        item = self.load('M-1', 'LOCAL-77')
        response = self.client.patch(self.url(item), {'part': str(self.plain.pk), 'matching_status': 'matched'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['reference'], SupplierItem.objects.get(pk=item.pk).reference_id)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.reference.tipo, item.matching_status), (self.plain.pk, 'interno', 'matched'))
        source = ReferenceSource.objects.get(reference=item.reference, kind='proveedor', citation__startswith='Código aprendido')
        self.assertEqual(source.detail['supplier'], str(item.supplier_id))  # S10: no SupplierCodeMapping row behind it any more
        kept = item.reference_id
        self.assertEqual(self.client.patch(self.url(item), {'matching_status': 'review'}, format='json').status_code, 200)
        item.refresh_from_db()
        self.assertEqual((item.reference_id, item.matching_status), (kept, 'review'))
        self.assertEqual(self.client.patch(self.url(item), {'part': None, 'matching_status': 'review'}, format='json').status_code, 400)
        with patch('mall.request_models.StockReservation.objects') as held:  # units a quotation or an order holds
            held.filter.return_value.exists.return_value = True
            refused = self.client.patch(self.url(item), {'part': None, 'matching_status': 'pending'}, format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('apartadas', str(refused.data))
        self.assertEqual(self.client.patch(self.url(item), {'part': None, 'matching_status': 'pending'}, format='json').status_code, 200)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.reference_id, item.matching_status), (None, None, 'pending'))

    def test_a_unificada_line_is_refused(self):
        item = self.load('M-2', 'LOCAL-78')
        merged = make_line('MERGED-1')
        line_merge.merge_lines(self.plain, [merged], actor=self.root)
        response = self.client.patch(self.url(item), {'part': str(merged.pk), 'matching_status': 'matched'}, format='json')
        self.assertEqual(response.status_code, 400)


class RetireAndMoveTests(VariantTestCase):
    def test_a_reference_items_hang_from_is_not_retired(self):
        item = self.load('R-1', '04111-22152-AS')
        with self.assertRaises(references.ReferenceHasVariants) as refused:
            references.retire(self.alias)
        self.assertEqual(refused.exception.status_code, 409)
        client = APIClient()
        client.force_authenticate(self.root)
        response = client.delete(f'/api/v1/management/alternates/{self.alias.pk}/')
        self.assertEqual(response.status_code, 409)
        self.assertIn('tiene artículos de proveedor', str(response.data))
        self.assertEqual(Reference.objects.get(pk=self.alias.pk).estado, 'declarado')
        self.assertEqual(SupplierItem.objects.get(pk=item.pk).reference_id, self.alias.pk)

    def test_moving_a_reference_moves_its_items(self):
        item = self.load('R-2', '04111-22152-AS')
        references.update(self.alias, part=self.branded)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.reference_id), (self.branded.pk, self.alias.pk))

    def test_merge_and_separar_carry_items_with_their_references(self):
        source = make_line('04111-22152-K', description='BOMBA AGUA')
        item = self.load('R-3', '04111-22152-K')
        former = item.reference
        line_merge.merge_lines(self.plain, [source], actor=self.root)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.reference_id), (self.plain.pk, former.pk))
        # Separar refuses to leave the item behind while its Reference goes back with its line.
        with self.assertRaises(Exception) as refused:
            line_merge.split_line(self.plain.pk, [{'id': str(source.pk), 'codes': [], 'items': [], 'images': []}], actor=self.root)
        self.assertIn('cuelga de una referencia', str(refused.exception))
        line_merge.split_line(self.plain.pk, [{'id': str(source.pk), 'codes': [], 'items': [str(item.pk)], 'images': []}], actor=self.root)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.reference_id), (source.pk, former.pk))

    def test_an_absorbed_reference_hands_its_items_over_and_takes_them_back(self):
        source = make_line('SRC-55555', description='BOMBA AGUA')
        held = make_reference(self.plain, 'ABS-777')
        waiting = make_reference(source, 'ABS-777', estado='disputa', dispute_note='Código duplicado de 04111-22152: caso de unificación.')
        item = make_variant(self.supplier, 'R-4', 'ABS-777', 1, actor=self.root)
        variants.vincular(item, source, actor=self.root)
        self.assertEqual(item.reference_id, waiting.pk)
        line_merge.merge_lines(self.plain, [source], actor=self.root)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.reference_id), (self.plain.pk, held.pk))
        self.assertEqual(Reference.all_objects.get(pk=waiting.pk).estado, 'retirada')
        line_merge.split_line(self.plain.pk, [{'id': str(source.pk), 'codes': [], 'items': [str(item.pk)], 'images': []}], actor=self.root)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.reference_id), (source.pk, waiting.pk))


class WaitingCodeTests(VariantTestCase):
    """A code change waiting for review (rule 5b, OD7) is applied only by an approval: Revisar coincidencia's matched, an approved
    case, Vincular with matched. A status of review or pending, a merge or an apiag-cloud withdrawal leave it waiting."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.root)
        self.other = make_line('OTHER-555', description='OTRA')
        self.item = self.load('WC-1', '04111-22152')
        self.main = self.item.reference_id
        self.item = self.load('WC-1', 'OTHER-555')

    def patch_item(self, data):
        return self.client.patch(f'/api/v1/management/inventory/{self.item.pk}/', data, format='json')

    def assert_waiting(self, status):
        self.item.refresh_from_db()
        self.assertEqual((self.item.codigo, self.item.codigo_pendiente, self.item.reference_id, self.item.part_id, self.item.matching_status),
                         ('04111-22152', 'OTHER-555', self.main, self.plain.pk, status))
        self.assertFalse(Reference.all_objects.filter(code='OTHER-555', part=self.plain).exists())

    def test_revisar_coincidencia_on_its_own_line_keeps_the_code_waiting_until_matched(self):
        # The dialog sends the item's line with the status it picked.
        for status in ('review', 'pending'):
            response = self.patch_item({'part': str(self.plain.pk), 'matching_status': status})
            self.assertEqual(response.status_code, 200, response.data)
            self.assert_waiting(status)
        response = self.patch_item({'matching_status': 'matched'})
        self.assertEqual(response.status_code, 200, response.data)
        self.item.refresh_from_db()
        self.assertEqual((self.item.codigo, self.item.codigo_pendiente, self.item.part_id, self.item.matching_status),
                         ('OTHER-555', None, self.plain.pk, 'matched'))
        self.assertEqual((self.item.reference.code, self.item.reference.tipo, self.item.reference.detail['s3']['como']),
                         ('OTHER-555', 'interno', 'creada'))

    def test_vincular_onto_another_line_in_review_hangs_by_the_old_code(self):
        response = self.patch_item({'part': str(self.branded.pk), 'matching_status': 'review'})
        self.assertEqual(response.status_code, 200, response.data)
        self.item.refresh_from_db()
        self.assertEqual((self.item.codigo, self.item.codigo_pendiente, self.item.part_id, self.item.matching_status),
                         ('04111-22152', 'OTHER-555', self.branded.pk, 'review'))
        self.assertEqual(self.item.reference.code, '04111-22152')

    def test_a_merge_carries_the_waiting_code_without_applying_it(self):
        target = make_line('TGT-55555', description='BOMBA AGUA')
        line_merge.settle_variants({self.plain.pk}, target, self.root)
        self.item.refresh_from_db()
        self.assertEqual((self.item.codigo, self.item.codigo_pendiente, self.item.part_id, self.item.matching_status),
                         ('04111-22152', 'OTHER-555', target.pk, 'review'))
        self.assertFalse(Reference.all_objects.filter(code='OTHER-555').exclude(part=self.other).exists())

    def test_an_item_with_no_line_is_only_pending(self):
        loose = self.loose('WC-2', 'NADIE-123')
        url = f'/api/v1/management/inventory/{loose.pk}/'
        for status in ('review', 'matched'):
            response = self.client.patch(url, {'matching_status': status}, format='json')
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.client.patch(url, {'matching_status': 'pending'}, format='json').status_code, 200)
        from django.contrib import admin
        from django.test import RequestFactory
        request = RequestFactory().post('/')
        request.user = self.root
        form_class = admin.site._registry[SupplierItem].get_form(request, loose)
        fields = {'matching_status': 'review', 'references': '[]', 'field_sources': '{}', 'material': '', 'origen': '', 'garantia': '',
                  'calidad': '', 'condicion': '', 'unidades_por_empaque': ''}
        form = form_class(data=fields, instance=loose)
        self.assertFalse(form.is_valid())
        self.assertIn('matching_status', form.errors)
        form = form_class(data={**fields, 'matching_status': 'matched'}, instance=SupplierItem.objects.get(pk=self.item.pk))
        self.assertFalse(form.is_valid())
        self.assertIn('código pendiente', str(form.errors))

    def test_moving_to_a_line_outside_the_catalog_takes_items_out_of_stock_until_it_enters(self):
        from .lines import create_line, set_estado
        library = create_line('LIB-0001', estado='referencia', origen='biblioteca')
        item = self.load('WC-3', '04111-22152-AS')
        references.update(self.alias, part=library)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.matching_status), (library.pk, 'pending'))
        self.assert_waiting('review')  # a review stays review wherever it goes
        set_estado(library, 'publicada')
        self.assertEqual(variants.recompute_status(library), 1)
        item.refresh_from_db()
        self.assertEqual(item.matching_status, 'matched')


class VariantDataTests(VariantTestCase):
    """S3.2 (rule 10): the optional data a supplier sends about an article: written only when sent, stamped in field_sources."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.root)

    def test_the_api_writes_what_it_is_sent_and_a_replay_is_the_same_load(self):
        url = f'/api/v1/accounts/{self.supplier.pk}/inventory/ingest/'
        payload = {'update_id': 'data-1', 'supplier_invent_id': 'DATA-1', 'codigo': '04111-22152', 'brand': '', 'source': 'upload',
                   'quantity': 2, 'calidad': 'Original', 'condicion': 'NUEVO', 'unidades_por_empaque': 4, 'garantia': ' 1 AÑO '}
        self.assertEqual(self.client.post(url, payload, format='json').status_code, 200)
        item = SupplierItem.objects.get(supplier_invent_id='DATA-1')
        self.assertEqual((item.calidad, item.condicion, item.unidades_por_empaque, item.garantia, item.material),
                         ('original', 'nuevo', 4, '1 AÑO', ''))
        self.assertEqual((item.field_sources['calidad']['source'], item.field_sources['calidad']['update']), ('proveedor', 'data-1'))
        self.assertNotIn('material', item.field_sources)
        self.assertEqual(self.client.post(url, payload, format='json').status_code, 200)  # the same update id and payload
        later = self.client.post(url, {**{k: v for k, v in payload.items() if k not in ('calidad', 'condicion', 'unidades_por_empaque', 'garantia')},
                                       'update_id': 'data-2', 'quantity': 3}, format='json')
        self.assertEqual(later.status_code, 200)
        item.refresh_from_db()
        self.assertEqual((item.calidad, item.unidades_por_empaque, item.reported_quantity), ('original', 4, 3))  # not sent: kept
        refused = self.client.post(url, {**payload, 'update_id': 'data-3', 'calidad': 'PIRATA'}, format='json')
        self.assertEqual(refused.status_code, 400)

    def test_the_excel_reads_the_optional_columns(self):
        from openpyxl import Workbook
        from .supplier_import import read_supplier_excel, stock_plan
        import io
        book = Workbook()
        sheet = book.active
        sheet.title = 'EXISTENCIAS'
        sheet.append(['ID_INVENTARIO_PROVEEDOR', 'CODIGO', 'EXISTENCIAS', 'CALIDAD', 'UNIDADES_POR_EMPAQUE', 'GARANTÍA'])
        sheet.append(['XL-1', '04111-22152', 2, 'alternativo', 6, ''])
        sheet.append(['XL-2', '04111-22152-AS', 1, '', '0', '6 MESES'])
        raw = io.BytesIO()
        book.save(raw)
        records, _ = read_supplier_excel(raw.getvalue())
        first, second = records
        self.assertEqual((first['data']['calidad'], first['data']['unidades_por_empaque'], 'garantia' in first['data']), ('ALTERNATIVO', 6, False))
        self.assertEqual([error['column'] for error in second['errors']], ['UNIDADES_POR_EMPAQUE'])
        plan, _ = stock_plan(self.supplier, [first])
        self.assertEqual((plan['preview'][0]['calidad'], plan['preview'][0]['unidades_por_empaque']), ('alternativo', 6))


class TightenTests(VariantTestCase):
    """0078: the composite foreign key keeps each item on its Reference's line, the CHECKs, and the repair before them."""
    MIGRATION = '0078_variant_reference_checks'

    def test_the_composite_key_moves_items_with_their_reference(self):
        item = self.load('T-1', '04111-22152-AS')
        Reference.all_objects.filter(pk=self.alias.pk).update(part=self.branded)  # a plain UPDATE: the database moves the item along
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.reference_id), (self.branded.pk, self.alias.pk))

    def refused(self, write):
        with self.assertRaises(IntegrityError), transaction.atomic():
            write()
            with connection.cursor() as cursor:
                cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')  # the composite key is checked at commit
        with connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL DEFERRED')

    def test_the_constraints_refuse_what_s3_forbids(self):
        item = self.load('T-2', '04111-22152')
        loose = self.loose('T-3', 'NADA-1')
        rows = SupplierItem.objects.filter(pk=item.pk)
        self.refused(lambda: rows.update(reference=None))                      # a line without its Reference
        self.refused(lambda: rows.update(part=None))                           # a Reference without its line
        self.refused(lambda: rows.update(part=self.branded))                   # a line its Reference is not on (I4)
        self.refused(lambda: SupplierItem.objects.filter(pk=loose.pk).update(matching_status='matched'))  # unattached but counted
        self.assertEqual(SupplierItem.objects.get(pk=item.pk).part_id, self.plain.pk)

    def test_0078_applies_when_its_repair_reattaches_items(self):
        """0078 run by Django's migration executor, operations and all: the repair's writes leave deferred foreign key checks pending,
        and the composite key and the CHECKs (ALTER TABLE) still apply in the same transaction."""
        from django.db.migrations.executor import MigrationExecutor
        moved = self.load('T-8', '04111-22152-AS')
        bare = self.load('T-9', '04111-22152')
        with connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')  # this test's own writes checked now: DDL refuses pending trigger events
        MigrationExecutor(connection).migrate([('mall', '0077_variant_reference')])
        with connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL DEFERRED')  # as a migrate session has them: Django's foreign keys checked at commit
        SupplierItem.objects.filter(pk=moved.pk).update(part=self.branded)  # the code before S3 moved it; its Reference stayed
        SupplierItem.objects.filter(pk=bare.pk).update(reference=None)      # written before S3
        MigrationExecutor(connection).migrate([('mall', self.MIGRATION)])
        moved.refresh_from_db(); bare.refresh_from_db()
        self.assertEqual((moved.reference.part_id, moved.reference.tipo, moved.reference.detail['s3']['por']), (self.branded.pk, 'interno', '0078'))
        self.assertEqual(bare.reference_id, references.main_of(self.plain).pk)
        with connection.cursor() as cursor:
            present = connection.introspection.get_constraints(cursor, 'mall_supplieritem')
        self.assertTrue({'variant_line_follows_reference', 'variant_reference_iff_line', 'variant_unattached_is_pending'} <= set(present))

    def test_the_repair_attaches_what_moved_and_stops_for_a_unificada_line(self):
        drop_variant_constraints()
        moved = self.load('T-4', '04111-22152-AS')
        bare = self.load('T-5', '04111-22152')
        lost = self.loose('T-6', 'NADA-2')
        SupplierItem.objects.filter(pk=moved.pk).update(part=self.branded)      # a queryset moved it, its Reference stayed
        SupplierItem.objects.filter(pk=bare.pk).update(reference=None)          # written before S3
        SupplierItem.objects.filter(pk=lost.pk).update(matching_status='review')  # no line, yet in review
        run_migration_function(self.MIGRATION, 'repair_variants')
        moved.refresh_from_db(); bare.refresh_from_db(); lost.refresh_from_db()
        self.assertEqual((moved.part_id, moved.reference.part_id, moved.reference.tipo, moved.reference.supplier_id),
                         (self.branded.pk, self.branded.pk, 'interno', self.supplier.pk))
        self.assertEqual(bare.reference_id, references.main_of(self.plain).pk)
        self.assertEqual(lost.matching_status, 'pending')
        run_migration_function(self.MIGRATION, 'repair_variants')  # idempotent
        merged = make_line('MERGED-9')
        stranded = self.loose('T-7', 'NADA-3')
        SupplierItem.objects.filter(pk=stranded.pk).update(part=merged)
        Part.objects.filter(pk=merged.pk).update(merged_into=self.plain, estado='unificada')
        with self.assertRaisesMessage(RuntimeError, 'líneas unificadas'):
            run_migration_function(self.MIGRATION, 'repair_variants')
        SupplierItem.objects.filter(pk=stranded.pk).update(part=None, matching_status='pending')
        Reference.all_objects.filter(part=merged).update(estado='retirada', is_main=False)
