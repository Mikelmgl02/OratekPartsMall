"""Productos nuevos as lines in preparación (inventory refactor S6.2, mall.new_products, mall.lines' transitions and the data step
mall.refactor.s6_new_products)."""
import uuid

from django.test import TestCase
from rest_framework.exceptions import ValidationError

from . import lines, new_products
from .brand_models import Brand, BrandAlias
from .lines import LineStateError
from .matching_engine import NO_CODE_EVIDENCE, item_row
from .matching_models import MatchingCase, MatchingDecision
from .models import Part, Reference, SupplierItem
from .new_products import PLACED_REASON, PlaceRefused, ProposalConflict
from .test_factories import make_legacy_case, make_library_number, make_line, make_reference, make_supplier, make_user


def seed_brands():
    chery = Brand.objects.create(nombre='CHERY', tipo='vehiculo')
    Brand.objects.create(nombre='KAIYI', tipo='vehiculo', familia=chery)
    toyota = Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
    BrandAlias.objects.create(alias='TOY', brand=toyota)
    for name in ('WURTH', 'MOTION MOTORS', 'GMB'):
        Brand.objects.create(nombre=name, tipo='repuesto')
    Brand.objects.create(nombre='ARMORALL')


class Helpers:
    def setUp(self):
        super().setUp()
        seed_brands()
        self.root = make_user('np-root', superuser=True)
        self.dealer = make_supplier('Motion Motors')
        self.other = make_supplier('Repuestos Chinos')

    def item(self, supplier, invent_id, codigo, brand='', description='', case='unmatched', **fields):
        item = SupplierItem.objects.create(supplier=supplier, supplier_invent_id=invent_id, codigo=codigo, brand=brand,
                                           description=description, source='apiag', **fields)
        if case:
            make_legacy_case(item, case, method=NO_CODE_EVIDENCE)
        return item

    def main(self, line):
        return Reference.all_objects.get(part=line, is_main=True)


class PlaceTests(Helpers, TestCase):
    def test_place_makes_a_line_in_preparacion_and_the_item_waits_pending(self):
        item = self.item(self.dealer, '10', '025CHA-1502606', 'KAIYI', 'O-RING KAIYI GS11', reported_quantity=4)
        line, created = new_products.place(item)
        self.assertTrue(created)
        line.refresh_from_db()
        item.refresh_from_db()
        main = self.main(line)
        self.assertEqual((line.estado, line.origen, line.sku, line.description, line.active), ('preparacion', 'proveedor', '025CHA-1502606',
                                                                                              'O-RING KAIYI GS11', False))
        self.assertEqual((main.code, main.brand, main.tipo, main.supplier_id), ('025CHA-1502606', 'KAIYI', 'oem', None))   # a dealer's code
        self.assertEqual((item.part, item.reference, item.matching_status), (line, main, 'pending'))
        self.assertIn(new_products.TAG, main.detail)
        self.assertIsNotNone(line.changed_at)
        case = MatchingCase.objects.get(key=f'supplier:{item.pk}')
        self.assertEqual((case.status, case.reason), ('dismissed', PLACED_REASON))
        self.assertFalse(lines.storefront_lines().filter(pk=line.pk).exists())

    def test_same_key_items_share_the_line_and_aliases_agree(self):
        first = self.item(self.dealer, '10', '90915-YZZE1', 'TOY', 'FILTRO')
        second = self.item(self.other, 'A7', '90915YZZE1', 'TOYOTA', 'FILTRO DE ACEITE TOYOTA')
        line, created = new_products.place(first)
        again, joined = new_products.place(second)
        self.assertEqual((created, joined, again.pk), (True, False, line.pk))
        line.refresh_from_db()
        self.assertEqual((self.main(line).brand, line.description, line.version), ('TOYOTA', 'FILTRO DE ACEITE TOYOTA', 2))
        self.assertEqual(SupplierItem.objects.filter(part=line).count(), 2)
        self.assertEqual(new_products.place(second), (again, False))   # placed already: nothing to do

    def test_the_main_follows_the_brand(self):
        cases = [('W-1', 'WURTH', 'aftermarket', False), ('MM-1', 'MOTION MOTORS', 'interno', True), ('AR-1', 'ARMORALL', 'otro', False),
                 ('EC-01', '', 'interno', True), ('EC-02', 'GENERICO', 'interno', True), ('12345678', '', 'otro', False),
                 ('NEWBRAND-1', 'ACME NUEVA', 'aftermarket', False)]
        for code, brand, tipo, internal in cases:
            with self.subTest(code):
                line, _ = new_products.place(self.item(self.dealer, code, code, brand))
                main = self.main(line)
                self.assertEqual((main.tipo, main.supplier_id == self.dealer.pk), (tipo, internal))
        self.assertEqual(self.main(Part.objects.get(sku='12345678')).brand, '')   # an OEM-like code with no brand is every brand's
        self.assertEqual(new_products.main_plan('X-1', 'KAIYI', self.dealer, internal=True), ('KAIYI', 'interno', self.dealer.pk))

    def test_an_interno_main_is_one_suppliers_code(self):
        mine = self.item(self.dealer, '1', 'EC-01', '')
        theirs = self.item(self.other, '2', 'EC-01', '')
        line, _ = new_products.place(mine)
        other, created = new_products.place(theirs)
        self.assertTrue(created)                       # OD6: a short brandless code says nothing across suppliers
        self.assertNotEqual(line.pk, other.pk)
        self.assertEqual(self.main(other).supplier, self.other)

    def test_a_library_number_line_moves_to_preparacion_rule_7(self):
        number = make_library_number('TOYOTA', '16100-39315')
        library = number.part
        self.assertEqual(library.estado, 'referencia')
        item = self.item(self.dealer, '1', '16100-39315', 'TOYOTA', 'BOMBA DE AGUA')
        line, created = new_products.place(item)
        self.assertEqual((line.pk, created), (library.pk, False))
        line.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual((line.estado, item.reference_id, item.matching_status), ('preparacion', number.pk, 'pending'))
        with self.assertRaises(LineStateError):
            lines.prepare(make_line('CAT-1'))

    def test_a_key_on_a_catalog_line_or_two_lines_is_left_to_the_engine(self):
        catalog = make_line('C-100')
        with self.assertRaises(PlaceRefused):
            new_products.place(self.item(self.dealer, '1', 'C-100', 'WURTH'))
        make_reference(make_line('Z-1', estado='preparacion'), '87654321', brand='GMB', tipo='aftermarket')
        make_reference(make_line('Z-2', estado='preparacion'), '87654321', brand='WURTH', tipo='aftermarket')
        loose = self.item(self.dealer, '2', '87654321', '')
        with self.assertRaises(PlaceRefused):
            new_products.place(loose)   # rule 2: a brandless code on two lines waits for a line case
        loose.refresh_from_db()
        self.assertEqual((loose.part, loose.reference, catalog.supplier_items.count()), (None, None, 0))

    def test_a_rejected_line_takes_the_same_code_again_with_no_notice(self):
        line, _ = new_products.place(self.item(self.dealer, '1', 'R-1', 'WURTH'))
        line.refresh_from_db()
        new_products.reject(line.pk, self.root, line.version, 'not_a_part', 'Es un servicio')
        line.refresh_from_db()
        version = line.version
        again, created = new_products.place(self.item(self.other, '2', 'R-1', 'WURTH'))
        again.refresh_from_db()
        self.assertEqual((again.pk, created, again.estado, again.version), (line.pk, False, 'rechazada', version))
        self.assertEqual(SupplierItem.objects.filter(part=line, matching_status='pending').count(), 2)

    def test_declared_codes_open_one_reference_case_each_and_writing_again_changes_nothing(self):
        item = self.item(self.dealer, '1', 'GWT-41A', 'GMB', 'BOMBA', references=[
            {'code': '16100-39315', 'brand': 'TOYOTA', 'ref_type': 'oem'}, {'code': 'gwt-41a', 'brand': 'gmb'}, {'code': 'GWT41A', 'brand': 'GMB'},
            {'code': 'BAD' * 50}])
        line, _ = new_products.place(item)
        cases = MatchingCase.objects.filter(kind='reference').order_by('key')
        # Its own code adds nothing; GWT41A is another code (a short code keeps its separators: EC-01 is not EC01).
        self.assertEqual([case.key for case in cases], [f'reference:{item.pk}:1610039315|TOYOTA', f'reference:{item.pk}:GWT41A|GMB'])
        case = cases.first()
        self.assertEqual((case.item_id, case.status, case.method, case.new_code, case.new_brand), (item.pk, 'review', new_products.DECLARED_METHOD,
                                                                                               '16100-39315', 'TOYOTA'))
        self.assertEqual(list(case.lines.all()), [line])   # the item's line: Aprobar adds the code there (mall.reference_matching.decide)
        self.assertEqual(new_products.declared_cases(item, line), [])
        MatchingCase.objects.filter(pk=case.pk).update(status='dismissed')
        self.assertEqual(new_products.declared_cases(item, line), [])
        self.assertEqual(MatchingCase.objects.get(pk=case.pk).status, 'dismissed')
        item.refresh_from_db()
        self.assertEqual(item.matching_status, 'pending')

    def test_the_matching_backlog_places_what_d1_left_for_productos_nuevos(self):
        # Items no line holds (D1's, whatever their legacy case says) are the matching worker's backlog: rule 4 places them, and their
        # legacy supplier cases are dismissed as placed (plan A5); a code a catalog line has attaches there instead.
        from .reference_matching import backlog
        first = self.item(self.dealer, '1', 'B-1000', 'WURTH', 'PIEZA')
        self.item(self.dealer, '2', 'B-1000', 'WURTH', 'PIEZA')
        reviewed = self.item(self.dealer, '3', 'B-2000', 'WURTH', 'PIEZA', case='review')
        self.item(self.dealer, '4', 'B-3000', 'WURTH', 'PIEZA', case=None)
        catalog = make_line('B-4000')
        sold = self.item(self.dealer, '5', 'B-4000', 'WURTH', 'PIEZA')
        stats = backlog()
        self.assertEqual((stats['placed'], stats['attached'], stats['failed']), (3, 2, 0))   # the second B-1000 joins the first's line
        first.refresh_from_db()
        self.assertEqual((first.part.estado, SupplierItem.objects.filter(part=first.part).count()), ('preparacion', 2))
        self.assertEqual(MatchingCase.objects.get(key=f'supplier:{reviewed.pk}').reason, PLACED_REASON)
        sold.refresh_from_db()
        self.assertEqual((sold.part, sold.matching_status), (catalog, 'matched'))
        self.assertEqual(backlog()['examined'], 0)   # nothing left without a Reference

    def placed(self, *rows):
        line = None
        for supplier, invent_id, code, brand, description in rows:
            line, _ = new_products.place(self.item(supplier, invent_id, code, brand, description, reported_quantity=2))
        line.refresh_from_db()
        return line

    def test_publish_puts_the_line_on_sale_and_its_items_in_stock(self):
        line = self.placed((self.dealer, '1', 'P-100', 'WURTH', 'CLIP'), (self.other, '2', 'P-100', 'WURTH', 'CLIP DE MOTOR'))
        with self.assertRaises(ProposalConflict):
            new_products.publish(line.pk, self.root, line.version + 1)
        new_products.publish(line.pk, self.root, line.version)
        line.refresh_from_db()
        self.assertEqual((line.estado, line.active, line.decided_by, line.origen), ('publicada', True, self.root, 'proveedor'))
        self.assertTrue(lines.storefront_lines().filter(pk=line.pk).exists())
        self.assertEqual(set(SupplierItem.objects.filter(part=line).values_list('matching_status', flat=True)), {'matched'})
        self.assertEqual(set(MatchingCase.objects.filter(evidence__s10__kind='supplier').values_list('status', 'target_sku')), {('applied', 'P-100')})
        self.assertEqual(MatchingDecision.objects.filter(action='proposal_approved').count(), 2)
        with self.assertRaises(ProposalConflict):
            new_products.publish(line.pk, self.root, line.version)   # decided: a second click refuses

    def test_a_declared_code_case_does_not_keep_items_out_of_stock(self):
        item = self.item(self.dealer, '1', 'D-100', 'WURTH', 'PIEZA', references=[{'code': 'X-99', 'brand': 'ACME'}])
        line, _ = new_products.place(item)
        line.refresh_from_db()
        new_products.publish(line.pk, self.root, line.version)
        item.refresh_from_db()
        self.assertEqual(item.matching_status, 'matched')
        self.assertEqual(MatchingCase.objects.get(kind='reference').status, 'review')

    def test_publishing_a_code_the_catalog_has_under_another_spelling_is_refused(self):
        holder = make_line('EC-01')
        line = self.placed((self.other, '1', 'EC01', 'WURTH', 'ARANDELA'))   # a short code: EC-01 is not EC01 (another clave), so it is placed
        with self.assertRaises(new_products.DuplicateProposal) as refused:
            new_products.publish(line.pk, self.root, line.version)
        self.assertEqual(refused.exception.owners, [{'id': str(holder.pk), 'sku': 'EC-01', 'active': True}])
        self.assertEqual(new_products.catalog_match(line), [{'id': str(holder.pk), 'sku': 'EC-01', 'code': 'EC01', 'active': True}])
        line.refresh_from_db()
        self.assertEqual(line.estado, 'preparacion')

    def test_reject_keeps_references_and_pending_items(self):
        line = self.placed((self.dealer, '1', 'J-1', 'WURTH', 'NADA'))
        with self.assertRaises(ValidationError):
            new_products.reject(line.pk, self.root, line.version, 'nope')
        new_products.reject(line.pk, self.root, line.version, 'bad_data', ' Datos malos ')
        line.refresh_from_db()
        self.assertEqual((line.estado, line.decision_reason, line.decision_note, line.decided_by), ('rechazada', 'bad_data', 'Datos malos', self.root))
        self.assertTrue(Reference.objects.filter(part=line, is_main=True, code='J-1').exists())
        self.assertEqual(SupplierItem.objects.get(part=line).matching_status, 'pending')

    def test_vincular_merges_into_a_catalog_line_and_its_items_count_there(self):
        target = make_line('MASTER-1', description='BOMBA DE AGUA')
        line = self.placed((self.dealer, '1', 'GWT-41A', 'GMB', 'BOMBA GMB'))
        with self.assertRaises(ValidationError):
            new_products.link(line.pk, self.root, line.version, make_line('HID-1', estado='preparacion').pk)
        new_products.link(line.pk, self.root, line.version, target.pk)
        line.refresh_from_db()
        item = SupplierItem.objects.get(supplier_invent_id='1')
        self.assertEqual((line.estado, line.merged_into_id, item.part_id, item.matching_status), ('unificada', target.pk, target.pk, 'matched'))
        self.assertTrue(Reference.objects.filter(part=target, code='GWT-41A', brand='GMB').exists())
        self.assertEqual(MatchingDecision.objects.filter(action='proposal_linked').count(), 1)

    def test_the_transitions_refuse_another_estado_and_a_codigo_the_catalog_has(self):
        make_line('Q-1')
        twin = make_line('q-1', estado='preparacion')     # its main waits in disputa: the catalog line holds the key
        with self.assertRaises(LineStateError):
            lines.publish(twin, user=self.root)
        self.assertEqual(lines.publish(make_line('Q-2', estado='preparacion'), user=self.root).estado, 'publicada')
        lines.reject(make_line('Q-3', estado='preparacion'), user=self.root, reason='other')
        with self.assertRaises(LineStateError):
            lines.reject(make_line('Q-4'), user=self.root, reason='other')
        with self.assertRaises(LineStateError):
            lines.publish(make_line('Q-5', estado='referencia', origen='biblioteca'), user=self.root)
        line = self.placed((self.dealer, '1', 'Q-6', 'WURTH', 'PIEZA'))
        Reference.all_objects.filter(part=line, is_main=True).update(estado='disputa', dispute_note='Otro SKU: caso de unificación.')
        with self.assertRaises(ValidationError):
            new_products.publish(line.pk, self.root, line.version)

    def test_ai_fill_never_moves_changed_at(self):
        from .category_suggestions import taxonomy
        line = self.placed((self.dealer, '1', 'KYJP000484', 'KAIYI', 'GORRA KAIYI'))
        changed, version = line.changed_at, line.version
        category, name = taxonomy()[0]
        result = new_products.ai_fill(line, {'description': ('GORRA KAIYI ROJA', 0.9), 'part_type': ((category, name), 0.8),
                                             'internal_code': (True, 0.95, 'Mercancía')}, note='', run_id=uuid.uuid4(), model='m')
        line.refresh_from_db()
        main = self.main(line)
        self.assertEqual(sorted(result['written']), ['description', 'internal_code', 'oem', 'part_type'])
        self.assertEqual((line.changed_at, line.version, line.description), (changed, version + 1, 'GORRA KAIYI ROJA'))
        self.assertEqual((main.tipo, main.supplier_id, line.part_type.name), ('interno', self.dealer.pk, name))
        self.assertEqual(SupplierItem.objects.get(part=line).reference_id, main.pk)
        self.assertEqual(line.field_sources['description']['from'], 'GORRA KAIYI')

    def test_an_administrators_corrections(self):
        line = self.placed((self.dealer, '1', '12-345-678', 'GMB', 'bomba de agua'))
        new_products.edit(line.pk, self.root, line.version, {'description': 'Bomba de agua', 'references': [{'code': 'X-1', 'brand': 'ACME'}]})
        line.refresh_from_db()
        self.assertEqual((line.description, line.field_sources['references']['source']), ('bomba de agua', 'admin'))
        self.assertEqual(line.field_sources['description']['source'], 'supplier')   # sent unchanged: still the supplier's
        self.assertTrue(Reference.objects.filter(part=line, code='X-1', brand='ACME', is_main=False).exists())
        new_products.edit(line.pk, self.root, line.version, {'description': 'Bomba de agua Toyota'})
        line.refresh_from_db()
        self.assertEqual((line.description, line.field_sources['description']['source']), ('BOMBA DE AGUA TOYOTA', 'admin'))
        new_products.edit(line.pk, self.root, line.version, {'sku': '12345678'})        # another spelling of the same key
        line.refresh_from_db()
        self.assertEqual((line.sku, self.main(line).code, self.main(line).printed_forms), ('12345678', '12345678', ['12-345-678']))
        new_products.edit(line.pk, self.root, line.version, {'oem_code': '16100-39315', 'oem_brand': 'toyota'})
        line.refresh_from_db()
        oem = new_products.oem_of(line)
        self.assertEqual((oem.code, oem.brand, line.field_sources['oem']['source']), ('16100-39315', 'TOYOTA', 'admin'))
        new_products.edit(line.pk, self.root, line.version, {'sku': 'BOMBA-GWT', 'references': []})   # a new codigo: the supplier's code stays
        line.refresh_from_db()
        self.assertEqual((line.sku, self.main(line).code), ('BOMBA-GWT', 'BOMBA-GWT'))
        self.assertEqual(SupplierItem.objects.get(part=line).reference.code, '12345678')   # the items' code, no alterno to remove
        self.assertFalse(Reference.objects.filter(part=line, code='X-1').exists())
        self.assertEqual(new_products.oem_of(line).code, '16100-39315')
        with self.assertRaises(ProposalConflict):
            new_products.edit(line.pk, self.root, line.version - 1, {'description': 'X'})
        with self.assertRaises(ValidationError):
            new_products.edit(line.pk, self.root, line.version, {'oem_code': 'X-2', 'oem_brand': ''})   # a number goes with its maker
        new_products.edit(line.pk, self.root, line.version, {'oem_code': '', 'oem_brand': ''})
        line.refresh_from_db()
        self.assertIsNone(new_products.oem_of(line))

    def test_internal_code_ticked_and_unticked(self):
        line = self.placed((self.dealer, '1', 'PELU-KY', 'KAIYI', 'OSITO KAIYI'))
        self.assertEqual(new_products.oem_of(line).code, 'PELU-KY')
        with self.assertRaises(ValidationError):
            new_products.edit(line.pk, self.root, line.version, {'internal_code': True, 'oem_code': 'X-1', 'oem_brand': 'GMB'})
        new_products.edit(line.pk, self.root, line.version, {'internal_code': True, 'oem_code': '', 'oem_brand': ''})   # as the drawer sends it
        line.refresh_from_db()
        main = self.main(line)
        self.assertEqual((main.tipo, main.supplier_id, new_products.oem_of(line), line.field_sources['internal_code']['source']),
                         ('interno', self.dealer.pk, None, 'admin'))
        self.assertEqual(SupplierItem.objects.get(part=line).reference_id, main.pk)
        new_products.edit(line.pk, self.root, line.version, {'internal_code': False})
        line.refresh_from_db()
        main = self.main(line)
        self.assertEqual((main.tipo, main.supplier_id, new_products.oem_of(line).code), ('oem', None, 'PELU-KY'))   # a make's code is its OEM number
        self.assertFalse(Reference.all_objects.filter(part=line, tipo='interno').exclude(estado='retirada').exists())


