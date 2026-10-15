"""Matching on References (inventory refactor S6.1, plan §3 S6): the rules in order (mall.reference_matching.resolve), synchronous
ingestion, the cases and their decisions, the worker and the line_merge additions."""
import threading
import time
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APITestCase

from . import reference_matching as rm
from . import references
from .brand_models import Brand
from .catalog_split_models import CatalogSeparation
from .line_merge import MergeGuard, merge_lines
from .lines import create_line
from .matching_models import MatchingCase
from .models import Part, Reference, StockEntry, SupplierItem
from .test_factories import Fresh, make_library_number, make_supplier, make_user
from .services import ingest_inventory


def brands():
    chery = Brand.objects.create(nombre='CHERY', tipo='vehiculo')
    Brand.objects.create(nombre='KAIYI', tipo='vehiculo', familia=chery)
    for name, tipo in (('GMB', 'repuesto'), ('AISIN', 'repuesto'), ('HYUNDAI', 'vehiculo'), ('TOYOTA', 'vehiculo'), ('DENSO', 'repuesto')):
        Brand.objects.create(nombre=name, tipo=tipo)


def line(sku, description='', brand='', tipo=None, estado='publicada'):
    return create_line(sku, description=description, main_brand=brand, main_tipo=tipo, estado=estado)


class Matching(Fresh):
    def setUp(self):
        super().setUp()
        brands()
        self.admin = make_user('matching-admin', superuser=True)
        self.supplier = make_supplier('PROVEEDOR UNO')
        self.other = make_supplier('PROVEEDOR DOS')
        self.disc = line('58411-1R000', 'DISCO FRENO HYUNDAI ACCENT DEL', 'HYUNDAI', 'oem')
        self.chery = line('J69-3502010', 'TAMBOR FRENO CHERY TIGGO', 'CHERY', 'oem')
        self.mirror = line('EC-01', 'ESPEJO RETROVISOR')
        self.plug = line('90915-YZZE1', 'FILTRO ACEITE TOYOTA', 'TOYOTA', 'oem')
        self.denso = line('DENSO-FA-1', 'FILTRO ACEITE TOYOTA DENSO')
        references.add(self.denso, '90915-YZZE1', 'DENSO', tipo='aftermarket')

    def subject(self, code, brand='', description='', supplier=None):
        supplier = supplier or self.supplier
        return rm.subject_of(None, {'codigo': code, 'brand': brand, 'description': description}, supplier)

    def ingest(self, code, *, brand='', description='', supplier=None, stable=None, update=None, quantity=5, references_=None):
        data = {'update_id': update or f'{stable or code}-{code}-{quantity}-{description}-{brand}-{references_}', 'supplier_invent_id': stable or code,
                'codigo': code, 'brand': brand, 'description': description, 'source': 'upload', 'quantity': quantity}
        if references_ is not None:
            data['references'] = references_
        return ingest_inventory(supplier=supplier or self.supplier, actor=self.admin, data=data)


class ResolveTests(Matching, TestCase):
    def test_the_rules_in_order(self):
        """Table-driven: (code, brand, description) -> (action, rule, the line it ends on)."""
        rows = [
            # Rules 1 and 2: a brandless item joins the one line its clave is on, whatever brand holds it.
            ('58411-1R000', '', 'DISCO FRENO DEL', ('attach', 'marca_de_la_linea', self.disc)),
            ('584111R000', '', 'DISCO FRENO DEL', ('attach', 'marca_de_la_linea', self.disc)),
            ('58411-1R000', 'HYUNDAI', '', ('attach', 'exacta', self.disc)),
            # D2: a dash plus an auto-eligible brand TAG strips to the base when the part name agrees; -L is a position, never stripped.
            ('58411-1R000-GMB', '', 'DISCO DE FRENO DEL GMB', ('new_reference_on_line', '3', self.disc)),
            ('58411-1R000-L', '', 'DISCO FRENO DEL', ('no_line', '4', None)),
            # D2's guards: another part name, or a contradiction, waits for a click.
            ('58411-1R000-GMB', '', 'BOMBA AGUA', ('case', '3', None)),
            ('58411-1R000-GMB', '', 'DISCO FRENO TRAS', ('case', '3', None)),
            # D3: KAIYI is under CHERY: the same code is the same part, never the same brand alone.
            ('J69-3502010', 'KAIYI', 'TAMBOR FRENO', ('attach', 'familia', self.chery)),
            ('J69-3502011', 'KAIYI', 'TAMBOR FRENO CHERY TIGGO', ('no_line', '4', None)),
            # A short code keeps its printing: EC-01 is not EC01.
            ('EC-01', '', '', ('attach', 'sin_marca', self.mirror)),
            ('EC01', '', '', ('no_line', '4', None)),
            # A brandless code on two lines waits in a line case with no Reference; its brand picks the line.
            ('90915-YZZE1', '', '', ('case', 'sin_marca', None)),
            ('90915-YZZE1', 'TOYOTA', '', ('attach', 'exacta', self.plug)),
            ('90915-YZZE1', 'DENSO', '', ('attach', 'exacta', self.denso)),
            # A brand that names no line's Reference of the clave is no match (different brands, different parts).
            ('90915-YZZE1', 'AISIN', '', ('no_line', '4', None)),
        ]
        for code, brand, description, expected in rows:
            with self.subTest(code=code, brand=brand, description=description):
                found = rm.resolve(self.subject(code, brand, description))
                self.assertEqual((found.action, found.rule, found.line), expected)
        line_case = rm.resolve(self.subject('90915-YZZE1'))
        self.assertEqual(sorted(line_case.cases[0]['lines'], key=str), sorted([self.plug.pk, self.denso.pk], key=str))
        d2 = rm.resolve(self.subject('58411-1R000-GMB', '', 'DISCO FRENO DEL'))
        self.assertEqual((d2.new['code'], d2.new['brand'], d2.new['tipo'], d2.new['estado']), ('58411-1R000-GMB', 'GMB', 'aftermarket', 'declarado'))

    def test_a_reference_in_disputa_never_takes_a_new_variant(self):
        held = line('77777-12345', 'BOMBA AGUA')
        other = line('OTHER-1', 'BOMBA AGUA')
        Reference.objects.create(part=other, code='7777712345', brand='', tipo='otro', ref_type='unknown', estado='disputa',
                                 dispute_note='Código duplicado: caso de unificación.')
        Reference.objects.filter(part=held, is_main=True).update(estado='disputa', dispute_note='Código duplicado: caso de unificación.')
        found = rm.resolve(self.subject('77777-12345'))
        self.assertEqual((found.action, found.cases[0]['guard']), ('case', 'disputa'))

    def test_a_suppliers_interno_code_is_its_own(self):
        local = line('LOCAL-LINE', 'SENSOR')
        references.add(local, 'LOC-9', '', tipo='interno', supplier=self.supplier)
        self.assertEqual(rm.resolve(self.subject('LOC-9')).line, local)
        self.assertEqual(rm.resolve(self.subject('LOC-9', supplier=self.other)).action, 'no_line')

    def test_3e_lazy_files_a_catalogs_code_on_the_one_line_of_its_numbers(self):
        from .oem_reference_models import OEMCrossReference
        pump = line('16100-39315', 'BOMBA AGUA TOYOTA', 'TOYOTA', 'oem')
        number = references.main_of(pump)
        gmb = 'GMB Water Pump Catalog 2016 · pág. 5'
        OEMCrossReference.objects.create(reference=number, brand='GMB', code='GWT-41A', citations=[gmb])
        OEMCrossReference.objects.create(reference=number, brand='AISIN', code='WPT-111', citations=[gmb])
        OEMCrossReference.objects.create(reference=number, brand='DENSO', code='DW-1', citations=['Otro Catálogo 2019 · pág. 1'])
        own = rm.resolve(self.subject('GWT-41A', 'GMB', 'BOMBA AGUA'))
        self.assertEqual((own.action, own.line, own.new['estado'], own.new['tipo']), ('new_reference_on_line', pump, 'declarado', 'aftermarket'))
        competitor = rm.resolve(self.subject('WPT-111', 'AISIN', 'BOMBA AGUA'))
        self.assertEqual((competitor.action, competitor.new['estado']), ('new_reference_on_line', 'inferido'))
        unchecked = rm.resolve(self.subject('DW-1', 'DENSO', 'BOMBA AGUA'))
        self.assertEqual((unchecked.action, unchecked.cases[0]['guard']), ('case', 'catalogo'))
        self.assertEqual(rm.resolve(self.subject('GWT41A', 'GMB')).action, 'no_line')  # a short code keeps its printing
        # Its numbers on two lines: a line case, and a merge case for the lines (OD15: a supplier sells the code).
        twin = line('16100-39316', 'BOMBA AGUA TOYOTA', 'TOYOTA', 'oem')
        OEMCrossReference.objects.create(reference=references.main_of(twin), brand='GMB', code='GWT-41A', citations=[gmb])
        split = rm.resolve(self.subject('GWT-41A', 'GMB', 'BOMBA AGUA'))
        self.assertEqual([spec['kind'] for spec in split.cases], ['line', 'merge'])
        item = self.ingest('GWT-41A', brand='GMB', description='BOMBA AGUA')
        self.assertIsNone(item.reference_id)
        merge = MatchingCase.objects.get(kind='merge')
        self.assertEqual(set(merge.lines.values_list('pk', flat=True)), {pump.pk, twin.pk})

    def test_where_a_new_product_line_keeps_its_main(self):
        """Rule 4 (OD6, OD20): interno for brandless or placeholder short codes and the supplier's own name; shared otherwise."""
        motion = make_supplier('MOTION MOTORS')
        rows = [('AB-12', '', self.supplier, ('interno', '')), ('AB-12', 'GENERICO', self.supplier, ('interno', '')),
                ('12345-67890', '', self.supplier, ('otro', '')), ('12345-67890', 'S/M', self.supplier, ('otro', '')),
                ('ZX-998877', 'MOTION  MOTORS', motion, ('interno', 'MOTION MOTORS')), ('F16-4121020', 'KAIYI', self.supplier, ('oem', 'KAIYI')),
                ('GWT-99', 'GMB', self.supplier, ('aftermarket', 'GMB'))]
        for code, brand, supplier, expected in rows:
            with self.subTest(code=code, brand=brand):
                spec = rm.placement_of(self.subject(code, brand, supplier=supplier), rm.Lookups())
                self.assertEqual((spec['tipo'], spec['brand']), expected)
                self.assertEqual(spec['supplier'] is not None, expected[0] == 'interno')


class IngestTests(Matching, TestCase):
    def test_a_new_product_gets_its_line_and_same_key_items_share_it(self):
        item = self.ingest('F16-4121020', brand='KAIYI', description='FAROL KAIYI X3')
        new = item.part
        self.assertEqual((new.estado, new.origen, new.sku, item.matching_status), ('preparacion', 'proveedor', 'F16-4121020', 'pending'))
        main = references.main_of(new)
        self.assertEqual((main.code, main.brand, main.tipo, main.supplier_id, item.reference_id), ('F16-4121020', 'KAIYI', 'oem', None, main.pk))
        other = self.ingest('F16-4121020', brand='KAIYI', supplier=self.other)
        self.assertEqual(other.part_id, new.pk)  # rule 1: the same key joins the same line
        short = self.ingest('LOC-1', description='SENSOR')
        again = self.ingest('LOC-1', stable='SECOND', description='SENSOR')
        elsewhere = self.ingest('LOC-1', supplier=self.other)
        self.assertEqual(again.part_id, short.part_id)
        self.assertNotEqual(elsewhere.part_id, short.part_id)  # OD6: a short brandless code is each supplier's own
        self.assertEqual(references.main_of(short.part).tipo, 'interno')
        self.assertFalse(MatchingCase.objects.filter(kind='line').exists())

    def test_an_exact_code_attaches_and_counts_and_a_contradiction_waits_in_a_variant_case(self):
        item = self.ingest('58411-1R000', description='DISCO FRENO HYUNDAI DEL')
        self.assertEqual((item.part_id, item.matching_status), (self.disc.pk, 'matched'))
        wrong = self.ingest('58411-1R000', stable='WRONG', description='DISCO FRENO HYUNDAI TRAS')
        self.assertEqual((wrong.part_id, wrong.matching_status), (self.disc.pk, 'review'))
        case = MatchingCase.objects.get(kind='variant')
        self.assertEqual((case.key, case.guard, case.item_id), (f'variant:{wrong.pk}:{self.disc.pk}', 'spec_conflict', wrong.pk))
        # The supplier fixes its description: the contradiction is gone and the item counts.
        fixed = self.ingest('58411-1R000', stable='WRONG', description='DISCO FRENO HYUNDAI DEL', quantity=6)
        case.refresh_from_db()
        self.assertEqual((fixed.matching_status, case.status), ('matched', 'applied'))

    def test_a_line_case_keeps_the_item_without_a_reference_until_its_lines_become_one(self):
        item = self.ingest('90915-YZZE1', description='FILTRO ACEITE')
        self.assertEqual((item.reference_id, item.part_id, item.matching_status), (None, None, 'pending'))
        case = MatchingCase.objects.get(key=f'line:{item.pk}')
        self.assertEqual((case.kind, case.status, case.lines.count()), ('line', 'review', 2))
        again = self.ingest('90915-YZZE1', description='FILTRO ACEITE', quantity=7)
        self.assertEqual(MatchingCase.objects.filter(kind='line').count(), 1)
        self.assertEqual(MatchingCase.objects.get(pk=case.pk).fingerprint, case.fingerprint)
        merge_lines(self.plug, [self.denso], actor=self.admin)
        self.assertIn(self.plug.pk, set(MatchingCase.objects.get(pk=case.pk).lines.values_list('pk', flat=True)))
        rm.backlog()
        again.refresh_from_db()
        self.assertEqual(again.part_id, self.plug.pk)
        self.assertEqual(MatchingCase.objects.get(pk=case.pk).status, 'applied')

    def test_code_changes_apply_on_the_same_line_and_wait_otherwise(self):
        item = self.ingest('58411-1R000', stable='STABLE', description='DISCO FRENO DEL')
        same = self.ingest('584111R000', stable='STABLE', description='DISCO FRENO DEL')
        self.assertEqual((same.codigo, same.codigo_pendiente, same.matching_status), ('584111R000', None, 'matched'))
        moved = self.ingest('J69-3502010', stable='STABLE', description='DISCO FRENO DEL')
        self.assertEqual((moved.codigo, moved.codigo_pendiente, moved.part_id, moved.matching_status),
                         ('584111R000', 'J69-3502010', self.disc.pk, 'review'))
        case = MatchingCase.objects.get(key=f'code_change:{item.pk}')
        self.assertEqual((case.new_code, case.reference_id, case.lines.count()), ('J69-3502010', moved.reference_id, 2))
        self.assertEqual(StockEntry.objects.filter(item=item).count(), 1)
        # The supplier sends the old code again: the change is dropped, and so is its undecided case.
        back = self.ingest('584111R000', stable='STABLE', description='DISCO FRENO DEL', quantity=6)
        self.assertEqual((back.codigo_pendiente, back.matching_status), (None, 'matched'))
        self.assertFalse(MatchingCase.objects.filter(pk=case.pk).exists())

    def test_declared_codes_open_one_reference_case_each_idempotently(self):
        declared = [{'code': 'GWT-41A', 'brand': 'GMB'}, {'code': '58411-1R000', 'brand': ''}]
        item = self.ingest('58411-1R000', description='DISCO FRENO DEL', references_=declared)
        self.assertEqual(item.matching_status, 'matched')  # a declared code never holds the item (rule 5b)
        cases = MatchingCase.objects.filter(kind='reference')
        self.assertEqual([case.new_code for case in cases], ['GWT-41A'])  # the line's own code is no case
        case = cases.get()
        self.assertEqual(case.key, f'reference:{item.pk}:GWT-41A|GMB')
        for quantity in (6, 7):  # apiag sends the same codes on every pull
            self.ingest('58411-1R000', description='DISCO FRENO DEL', references_=declared, quantity=quantity)
        self.assertEqual(MatchingCase.objects.filter(kind='reference').count(), 1)
        MatchingCase.objects.filter(pk=case.pk).update(status='dismissed')
        self.ingest('58411-1R000', description='DISCO FRENO DEL', references_=declared, quantity=8)
        self.assertEqual(MatchingCase.objects.get(pk=case.pk).status, 'dismissed')
        self.ingest('58411-1R000', description='DISCO FRENO DEL', references_=[], quantity=9)
        self.assertEqual(MatchingCase.objects.get(pk=case.pk).status, 'dismissed')  # a decided case stays as history

    def test_a_variant_joining_a_line_in_referencia_moves_it_to_preparacion(self):
        number = make_library_number('TOYOTA', '16100-39999', description='BOMBA AGUA')
        library = number.part
        self.assertEqual(library.estado, 'referencia')
        item = self.ingest('16100-39999', description='BOMBA AGUA')
        library.refresh_from_db()
        self.assertEqual((item.part_id, item.matching_status, library.estado), (library.pk, 'pending', 'preparacion'))

    def test_a_new_product_takes_the_loads_code_and_counts_the_items_that_join_it(self):
        # The S6.1/S6.2 interface: place() writes the load's item as it arrives (its new code still unsaved), and an item joining the
        # line in preparación later counts there (mall.new_products.joined: version + 1, the more detailed description).
        waiting = self.ingest('90915-YZZE1', stable='W', description='FILTRO ACEITE')  # its clave is on two lines: a line case
        self.assertIsNone(waiting.reference_id)
        moved = self.ingest('ZQ-55501234', stable='W', brand='GMB', description='BOMBA AGUA')
        new = moved.part
        self.assertEqual((new.estado, new.sku, references.main_of(new).code, references.main_of(new).brand, moved.codigo),
                         ('preparacion', 'ZQ-55501234', 'ZQ-55501234', 'GMB', 'ZQ-55501234'))
        self.assertEqual(MatchingCase.objects.get(key=f'line:{waiting.pk}').status, 'applied')
        new.refresh_from_db()
        version = new.version
        other = self.ingest('ZQ-55501234', brand='GMB', supplier=self.other, description='BOMBA DE AGUA TOYOTA HILUX')
        new.refresh_from_db()
        self.assertEqual((other.part_id, other.matching_status), (new.pk, 'pending'))
        self.assertEqual((new.version, new.description), (version + 1, 'BOMBA DE AGUA TOYOTA HILUX'))

    def test_the_excel_preview_says_what_the_import_does(self):
        from .supplier_import import stock_plan
        self.ingest('58411-1R000', stable='OLD', description='DISCO FRENO DEL')
        rows = [{'row': 2, 'data': {'supplier_invent_id': 'OLD', 'codigo': 'J69-3502010', 'brand': '', 'description': 'DISCO FRENO DEL', 'quantity': 3}},
                {'row': 3, 'data': {'supplier_invent_id': 'NEW', 'codigo': 'ZZ-NEW-77', 'brand': '', 'description': 'BOMBA', 'quantity': 1}},
                {'row': 4, 'data': {'supplier_invent_id': 'TWO', 'codigo': '90915-YZZE1', 'brand': '', 'description': 'FILTRO', 'quantity': 1}},
                {'row': 5, 'data': {'supplier_invent_id': 'D2', 'codigo': '58411-1R000-GMB', 'brand': '', 'description': 'DISCO FRENO DEL', 'quantity': 1}}]
        plan, _ = stock_plan(self.supplier, rows)
        preview = {row['row']: (row['resolucion'], row['matching_status'], row['part_sku']) for row in plan['preview']}
        self.assertEqual(preview, {2: ('pendiente', 'review', '58411-1R000'), 3: ('no_line', 'pending', None), 4: ('case', 'pending', None),
                                   5: ('new_reference_on_line', 'matched', '58411-1R000')})
        for row in rows:
            item = ingest_inventory(supplier=self.supplier, actor=self.admin, data={**row['data'], 'source': 'upload', 'update_id': f'x-{row["row"]}'})
            expected = next(entry for entry in plan['preview'] if entry['row'] == row['row'])
            from .supplier_import import same_outcome
            self.assertTrue(same_outcome(item, expected), row)


class DecisionTests(Matching, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def decide(self, case, action, **data):
        return self.client.post(f'/api/v1/management/matching/{case.pk}/', {'action': action, 'fingerprint': case.fingerprint, **data},
                                format='json')

    def test_a_line_case_is_decided_by_line_id_or_sku_or_separated(self):
        item = self.ingest('90915-YZZE1', description='FILTRO ACEITE')
        case = MatchingCase.objects.get(kind='line')
        listed = self.client.get('/api/v1/management/matching/', {'kind': 'line'}).data
        self.assertEqual((listed['count'], listed['kinds']['line'], len(listed['results'][0]['lines'])), (1, 1, 2))
        response = self.decide(case, 'approve', line=str(self.denso.pk))
        self.assertEqual(response.status_code, 200, response.data)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.reference.brand, item.matching_status, response.data['status']), (self.denso.pk, 'DENSO', 'matched', 'applied'))
        self.assertEqual(self.decide(case, 'approve').status_code, 200)  # the same decision again
        second = self.ingest('90915-YZZE1', stable='SECOND', description='FILTRO ACEITE')
        case = MatchingCase.objects.get(key=f'line:{second.pk}')
        self.assertEqual(self.decide(case, 'approve', target_sku=self.plug.sku).status_code, 200)
        second.refresh_from_db()
        self.assertEqual(second.part_id, self.plug.pk)
        third = self.ingest('90915-YZZE1', stable='THIRD', description='FILTRO ACEITE')
        case = MatchingCase.objects.get(key=f'line:{third.pk}')
        response = self.decide(case, 'separar_referencia')
        self.assertEqual(response.status_code, 200, response.data)
        third.refresh_from_db()
        main = references.main_of(third.part)
        self.assertEqual((third.part.estado, main.tipo, main.supplier_id, third.reference_id), ('preparacion', 'interno', self.supplier.pk, main.pk))

    def test_a_stale_fingerprint_is_refused(self):
        self.ingest('90915-YZZE1', description='FILTRO ACEITE')
        case = MatchingCase.objects.get(kind='line')
        response = self.client.post(f'/api/v1/management/matching/{case.pk}/', {'action': 'approve', 'fingerprint': 'x' * 64,
                                                                                 'line': str(self.plug.pk)}, format='json')
        self.assertEqual(response.status_code, 409)

    def test_a_variant_case_is_accepted_moved_or_separated(self):
        item = self.ingest('58411-1R000', stable='A', description='DISCO FRENO HYUNDAI TRAS')
        case = MatchingCase.objects.get(kind='variant')
        self.assertEqual(self.decide(case, 'dismiss').status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.matching_status, 'matched')  # the contradiction was not one
        other = self.ingest('58411-1R000', stable='B', description='DISCO FRENO HYUNDAI TRAS')
        case = MatchingCase.objects.get(key=f'variant:{other.pk}:{self.disc.pk}')
        rear = line('58411-1R100', 'DISCO FRENO HYUNDAI ACCENT TRAS', 'HYUNDAI', 'oem')
        response = self.decide(case, 'approve', line=str(rear.pk))
        self.assertEqual(response.status_code, 200, response.data)
        other.refresh_from_db()
        self.assertEqual((other.part_id, other.matching_status, other.reference.tipo), (rear.pk, 'matched', 'interno'))

    def test_a_code_change_is_approved_onto_the_line_of_its_new_key_or_rejected(self):
        item = self.ingest('58411-1R000', stable='S', description='FRENO')
        self.ingest('J69-3502010', stable='S', description='FRENO')
        case = MatchingCase.objects.get(kind='code_change')
        response = self.decide(case, 'approve')
        self.assertEqual(response.status_code, 200, response.data)
        item.refresh_from_db()
        self.assertEqual((item.codigo, item.codigo_pendiente, item.part_id, item.matching_status), ('J69-3502010', None, self.chery.pk, 'matched'))
        self.ingest('EC-01', stable='S', description='FRENO', quantity=6)
        case = MatchingCase.objects.get(kind='code_change', status='review')
        self.assertEqual(self.decide(case, 'dismiss').status_code, 200)
        item.refresh_from_db()
        self.assertEqual((item.codigo, item.codigo_pendiente, item.matching_status), ('J69-3502010', None, 'matched'))
        again = self.ingest('EC-01', stable='S', description='FRENO', quantity=7)  # a rejected change sent again changes nothing
        self.assertEqual((again.codigo, again.codigo_pendiente, again.matching_status), ('J69-3502010', None, 'matched'))
        self.assertEqual(MatchingCase.objects.get(pk=case.pk).status, 'dismissed')

    def test_a_declared_code_becomes_a_reference_or_moves_the_item(self):
        self.ingest('58411-1R000', description='DISCO FRENO DEL', references_=[{'code': 'GWT-77B', 'brand': 'GMB', 'ref_type': 'company'}])
        case = MatchingCase.objects.get(kind='reference')
        self.assertEqual(self.decide(case, 'approve').status_code, 200)
        ref = Reference.objects.get(part=self.disc, code='GWT-77B')
        self.assertEqual((ref.brand, ref.tipo, ref.estado), ('GMB', 'aftermarket', 'declarado'))
        from .brand_models import BrandAlias
        BrandAlias.objects.create(alias='TOY', brand=Brand.objects.get(nombre='TOYOTA'))
        self.ingest('58411-1R000', stable='ALIAS', description='DISCO FRENO DEL', references_=[{'code': '58411-1R099', 'brand': 'TOY'}])
        alias_case = MatchingCase.objects.get(kind='reference', new_code='58411-1R099')
        self.assertEqual(self.decide(alias_case, 'approve').status_code, 200)
        self.assertEqual(Reference.objects.get(part=self.disc, code='58411-1R099').brand, 'TOYOTA')  # I12: the canonical nombre
        other = self.ingest('ZZ-OTRO-1', supplier=self.other, description='TAMBOR', references_=[{'code': 'J69-3502010', 'brand': 'CHERY'}])
        case = MatchingCase.objects.get(key__startswith=f'reference:{other.pk}:')
        self.assertEqual(set(case.lines.values_list('pk', flat=True)), {other.part_id, self.chery.pk})
        response = self.decide(case, 'approve')
        self.assertEqual(response.status_code, 409)  # the code is another line's: choose that line, or merge
        response = self.decide(case, 'approve', line=str(self.chery.pk))
        self.assertEqual(response.status_code, 200, response.data)
        other.refresh_from_db()
        self.assertEqual(other.part_id, self.chery.pk)

    def test_a_merge_case_merges_into_the_chosen_line_or_keeps_them_apart(self):
        a = line('MERGE-A1', 'BOMBA AGUA')
        b = line('MERGE-B1', 'BOMBA AGUA')
        c = line('MERGE-C1', 'BOMBA AGUA')
        spec = rm.merge_case_spec([a, b], evidence={'code': 'X'}, reason='prueba')
        case = rm.open_case(spec)
        response = self.decide(case, 'merge', line=str(b.pk))
        self.assertEqual(response.status_code, 200, response.data)
        a.refresh_from_db()
        self.assertEqual((a.estado, a.merged_into_id), ('unificada', b.pk))
        case = rm.open_case(rm.merge_case_spec([b, c], evidence={'code': 'Y'}, reason='prueba'))
        self.assertEqual(self.decide(case, 'keep_apart').status_code, 200)
        self.assertTrue(CatalogSeparation.objects.filter(case=case).exists())
        self.assertIsNone(rm.merge_case_spec([b, c], evidence={}, reason=''))  # a pair kept apart is never proposed again


    def test_a_legacy_auto_merge_batch_case_keeps_the_merge_or_takes_it_apart(self):
        """OD18: a doubtful automatic merge of the code before S6 waits in the legacy-auto-merge batch: Unificar keeps it, Mantener
        separadas brings the merged line back (Separar SKU, as the merge recorded) and keeps the two apart."""
        kept, gone = line('LEG-KEEP-1', 'BOMBA AGUA'), line('LEG-KEEP-1-NP', 'BOMBA AGUA')
        target, child = line('LEG-SPLIT-1', 'BOMBA AGUA'), line('LEG-SPLIT-1-AS', 'BOMBA AGUA')
        for into, source in ((kept, gone), (target, child)):
            merge_lines(into, [source], actor=self.admin, automatic=True)
        cases = []
        for into, source in ((kept, gone), (target, child)):
            spec = {'kind': 'merge', 'lines': [into.pk, source.pk], 'line_rows': [into, source], 'batch_key': 'legacy-auto-merge',
                    'route': 'lote', 'key': f'merge:legacy-auto-merge:{source.pk}', 'evidence': {'audit': 'S0.8'}, 'reason': 'Unificación antigua dudosa.'}
            cases.append(rm.open_case(spec))
        self.assertEqual((cases[0].route, cases[0].batch_key), ('lote', 'legacy-auto-merge'))
        self.assertEqual(self.decide(cases[0], 'merge').status_code, 200)
        gone.refresh_from_db()
        self.assertEqual(gone.merged_into_id, kept.pk)
        response = self.decide(cases[1], 'keep_apart')
        self.assertEqual(response.status_code, 200, response.data)
        child.refresh_from_db()
        self.assertEqual((child.merged_into_id, child.estado), (None, 'publicada'))
        self.assertEqual(rm.merge_case_spec([target, child], evidence={}, reason=''), None)  # kept apart from here


class WorkerTests(Matching, TestCase):
    def test_the_backlog_places_what_no_load_placed_and_keeps_waiting_codes_cased(self):
        loose = SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id='LOOSE', codigo='ZZ-LOOSE-1', description='SENSOR')
        from .matching_worker import process_queue
        from .matching_queue import matching_work
        with matching_work():
            summary = process_queue(force=True, use_ai=False)
        loose.refresh_from_db()
        self.assertEqual((loose.part.estado, loose.matching_status, summary['placed_items']), ('preparacion', 'pending', 1))
        waiting = self.ingest('58411-1R000', stable='W', description='DISCO FRENO DEL')
        SupplierItem.objects.filter(pk=waiting.pk).update(codigo_pendiente='EC-01', matching_status='review')
        rm.backlog()
        self.assertTrue(MatchingCase.objects.filter(key=f'code_change:{waiting.pk}', status='review').exists())

    @override_settings(GEMINI_API_KEY='test-key')
    def test_the_ai_ranks_only_line_and_merge_cases(self):
        from .matching_worker import analyze_ambiguous
        self.ingest('58411-1R000', stable='V', description='DISCO FRENO HYUNDAI TRAS')  # a variant case
        item = self.ingest('90915-YZZE1', description='FILTRO ACEITE')  # a line case
        result = {'suggestions': [{'source_sku': item.codigo, 'target_sku': self.plug.sku, 'confidence': 0.7, 'reason': 'Marca del filtro.'}]}
        with patch('mall.matching_worker.call_provider', return_value=result) as provider:
            self.assertEqual(analyze_ambiguous(), 1)
        sent = provider.call_args.args[0]
        self.assertEqual([row['sku'] for row in sent], ['90915-YZZE1'])
        self.assertEqual(MatchingCase.objects.get(kind='line').ai['target_sku'], self.plug.sku)
        self.assertEqual(MatchingCase.objects.get(kind='variant').ai, {})

    def test_catalog_references_open_merge_cases_never_merges(self):
        a = line('CR-TIGGO-1', 'BOMBA AGUA CHERY')
        b = line('CR-TIGGO-2', 'BOMBA AGUA KAIYI')
        references.add(a, '4G15-1307010', 'CHERY', tipo='oem')
        references.add(b, '4G15-1307010', 'KAIYI', tipo='oem')  # one familia: maybe one part
        c = line('CR-OTHER-1', 'BOMBA AGUA')
        d = line('CR-OTHER-2', 'BOMBA AGUA')
        references.add(c, '55555-1307010', 'GMB', tipo='aftermarket')
        references.add(d, '55555-1307010', 'AISIN', tipo='aftermarket')  # two makers' codes: different parts
        self.assertEqual(rm.open_reference_merge_cases(), 1)
        case = MatchingCase.objects.get(kind='merge')
        self.assertEqual(set(case.lines.values_list('pk', flat=True)), {a.pk, b.pk})
        self.assertEqual(rm.open_reference_merge_cases(), 0)  # idempotent
        self.assertEqual(Part.objects.filter(estado='unificada').count(), 0)  # estado: the worker merges nothing on its own


    def test_a_catalog_line_whose_code_less_its_tag_is_another_lines_alterno_opens_a_merge_case(self):
        holder = line('TB-HOLDER-1', 'DISCO FRENO HYUNDAI')
        references.add(holder, '58411-1R555', '', tipo='otro')
        tagged = line('58411-1R555-GMB', 'DISCO FRENO HYUNDAI GMB')
        self.assertEqual(rm.open_reference_merge_cases(), 1)
        case = MatchingCase.objects.get(kind='merge')
        self.assertEqual((set(case.lines.values_list('pk', flat=True)), case.evidence['base'], case.guard), ({holder.pk, tagged.pk}, '58411-1R555', ''))
        self.assertEqual(rm.open_reference_merge_cases(), 0)


class LineMergeTests(Matching, TestCase):
    def test_specs_move_when_the_target_has_no_subgrupo_and_separar_takes_them_back(self):
        from .technical_models import Grupo, PartSpecification, PartType
        grupo = Grupo.objects.create(nombre='FRENOS')
        discs = PartType.objects.create(grupo=grupo, name='DISCOS DE FRENO', category='FRENOS')
        pads = PartType.objects.create(grupo=grupo, name='PASTILLAS DE FRENO', category='FRENOS')
        source = line('SHEET-SRC-1', 'DISCO FRENO')
        Part.objects.filter(pk=source.pk).update(part_type=discs)
        field = self.field(discs)
        PartSpecification.objects.create(part=source, field=field, number_value=220)
        target = line('SHEET-TGT-1', 'DISCO FRENO')
        merge_lines(target, [source], actor=self.admin)
        target.refresh_from_db()
        self.assertEqual((target.part_type_id, PartSpecification.objects.get().part_id), (discs.pk, target.pk))
        from .line_merge import split_line
        split_line(target.pk, [{'id': str(source.pk)}], actor=self.admin)
        self.assertEqual(PartSpecification.objects.get().part_id, source.pk)
        other = line('SHEET-TGT-2', 'PASTILLA FRENO')
        Part.objects.filter(pk=other.pk).update(part_type=pads)
        with self.assertRaises(MergeGuard) as refused:
            merge_lines(other, [source], actor=self.admin)
        self.assertEqual(refused.exception.guard, 'subgrupo')

    def field(self, part_type):
        from .technical_models import TechnicalField, TechnicalTemplate
        template, _ = TechnicalTemplate.objects.get_or_create(part_type=part_type)
        return TechnicalField.objects.create(template=template, key='diametro', label='Diámetro', kind='number')

    def test_a_merge_into_a_published_line_counts_the_variants_of_a_line_in_preparacion(self):
        item = self.ingest('ZZ-NUEVO-9', brand='GMB', description='BOMBA AGUA')
        new = item.part
        self.assertEqual((new.estado, item.matching_status), ('preparacion', 'pending'))
        target = line('PUMP-PUBLISHED', 'BOMBA AGUA')
        merge_lines(target, [new], actor=self.admin)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.matching_status), (target.pk, 'matched'))


class RuleThreeGuardTests(Matching, TestCase):
    """Rule 3's negatives (the variant and year guards of the families before S6, test_catalog_family_parents): only a known brand
    suffix links on its own (D2); a position, size or year never strips, and a contradiction or another part name waits for a click."""

    def test_the_guards(self):
        base = line('58411-07000', 'TAMBOR 180MM KIA PICANTO 04-11')
        rows = [
            ('58411-07000-GMB', 'TAMBOR 180MM KIA PICANTO 04-11', ('new_reference_on_line', '')),  # the positive control
            ('58411-07000-LH', 'TAMBOR 180MM KIA PICANTO 04-11', ('no_line', '')),       # a position is no TAG
            ('58411-07000-RH', 'TAMBOR 180MM KIA PICANTO 04-11', ('no_line', '')),
            ('58411-07000-20', 'TAMBOR 180MM KIA PICANTO 04-11', ('no_line', '')),       # nor is a size
            ('58411-07000-GMB', 'TAMBOR 180MM KIA PICANTO 12-18', ('case', 'spec_conflict')),  # other years
            ('58411-07000-GMB', 'TAMBOR 200MM KIA PICANTO 04-11', ('case', 'spec_conflict')),  # another size
            ('58411-07000-GMB', 'DISCO 180MM KIA PICANTO 04-11', ('case', 'noun')),       # another part
            ('58411-07000-K', 'TAMBOR 180MM KIA PICANTO 04-11', ('case', 'sufijo')),      # an origin: the base's line, by a click
            ('58411-07000-NP', 'TAMBOR 180MM KIA PICANTO 04-11', ('case', 'sufijo')),
            ('58411-07000-G', 'TAMBOR 180MM KIA PICANTO 04-11', ('case', 'sufijo')),      # a genuine mark
        ]
        for code, description, expected in rows:
            with self.subTest(code=code, description=description):
                found = rm.resolve(self.subject(code, '', description))
                guard = found.cases[0]['guard'] if found.action == 'case' else ''
                self.assertEqual((found.action, guard), expected)
                if found.action == 'case':
                    self.assertEqual(found.cases[0]['lines'], [base.pk])


class ReviewFixTests(Matching, APITestCase):
    """The S6 review's findings, each kept from coming back."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def decide(self, case, action, **data):
        return self.client.post(f'/api/v1/management/matching/{case.pk}/', {'action': action, 'fingerprint': case.fingerprint, **data},
                                format='json')

    def test_a_code_with_no_letter_or_digit_waits_in_a_line_case_and_never_stops_the_pass(self):
        self.assertEqual(rm.Lookups().cross(''), [])
        found = rm.resolve(self.subject('.', 'KAIYI'))
        self.assertEqual((found.action, found.cases[0]['guard']), ('case', 'sin_codigo'))
        item = self.ingest('.', brand='KAIYI', description='TORNILLO')
        case = MatchingCase.objects.get(key=f'line:{item.pk}')
        self.assertEqual((item.reference_id, item.part_id, case.guard, case.lines.count()), (None, None, 'sin_codigo', 0))
        loose = SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id='LOOSE-2', codigo='ZZ-LOOSE-22', description='SENSOR')
        updated = SupplierItem.objects.get(pk=item.pk).updated_at
        rm.backlog()
        loose.refresh_from_db()
        self.assertEqual(loose.part.estado, 'preparacion')  # the items after it are placed
        self.assertEqual(MatchingCase.objects.get(pk=case.pk).fingerprint, case.fingerprint)
        self.assertEqual(SupplierItem.objects.get(pk=item.pk).updated_at, updated)  # not written on every pass (C6's I10 reads it)
        response = self.decide(case, 'separar_referencia')
        self.assertEqual(response.status_code, 409, response.data)  # no line of its own can be made of it: the reason, not a 500
        from .supplier_import import stock_plan
        plan, _ = stock_plan(self.supplier, [{'row': 2, 'data': {'supplier_invent_id': 'DOT', 'codigo': '.', 'brand': 'KAIYI',
                                                                     'description': 'TORNILLO', 'quantity': 1}}])
        self.assertEqual((plan['preview'][0]['resolucion'], plan['errors']), ('case', []))

    def test_one_items_failure_is_logged_and_the_pass_goes_on(self):
        boom = SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id='BOOM', codigo='ZZ-BOOM-11', description='SENSOR')
        after = SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id='AFTER', codigo='ZZ-AFTER-11', description='SENSOR')
        real = rm.resolve

        def flaky(subject, lookups=None):
            if subject.code == 'ZZ-BOOM-11':
                raise KeyError('')
            return real(subject, lookups)
        with patch.object(rm, 'resolve', side_effect=flaky), self.assertLogs('mall.reference_matching', 'ERROR'):
            counts = rm.backlog()
        boom.refresh_from_db()
        after.refresh_from_db()
        self.assertEqual((counts['failed'], boom.reference_id, after.part.estado), (1, None, 'preparacion'))

    def test_a_place_refusal_opens_a_line_case_instead_of_retrying_forever(self):
        from .new_products import PlaceRefused
        with patch.object(rm, 'place', side_effect=PlaceRefused('El código choca con otra línea.', 'conflicto')):
            item = self.ingest('ZZ-REFUSED-12', description='SENSOR')
        case = MatchingCase.objects.get(key=f'line:{item.pk}')
        self.assertEqual((item.reference_id, item.matching_status, case.guard), (None, 'pending', 'conflicto'))
        rm.backlog()  # the refusal is gone: the backlog places it and the case is applied
        item.refresh_from_db()
        self.assertEqual((item.part.estado, MatchingCase.objects.get(pk=case.pk).status), ('preparacion', 'applied'))

    def test_a_new_products_line_follows_its_items_text_and_never_puts_it_in_review(self):
        item = self.ingest('ZQ-77700001', stable='P', brand='GMB', description='BOMBA AGUA DEL')
        new = item.part
        version = new.version
        fixed = self.ingest('ZQ-77700001', stable='P', brand='GMB', description='BOMBA AGUA TRAS')
        new.refresh_from_db()
        self.assertEqual((fixed.matching_status, new.description, new.version), ('pending', 'BOMBA AGUA TRAS', version + 1))
        self.assertFalse(MatchingCase.objects.filter(kind='variant').exists())

    def test_two_codes_on_the_same_lines_never_rewrite_their_merge_case(self):
        from .oem_reference_models import OEMCrossReference
        pump = line('16100-39315', 'BOMBA AGUA TOYOTA', 'TOYOTA', 'oem')
        twin = line('16100-39316', 'BOMBA AGUA TOYOTA', 'TOYOTA', 'oem')
        gmb = 'GMB Water Pump Catalog 2016 · pág. 5'
        for number in (pump, twin):
            for code in ('GWT-41A', 'GWT-41B'):
                OEMCrossReference.objects.create(reference=references.main_of(number), brand='GMB', code=code, citations=[gmb])
        self.ingest('GWT-41A', brand='GMB', description='BOMBA AGUA')
        case = MatchingCase.objects.get(kind='merge')
        MatchingCase.objects.filter(pk=case.pk).update(ai={'target_sku': pump.sku})
        self.ingest('GWT-41B', brand='GMB', description='BOMBA AGUA')
        self.ingest('GWT-41A', brand='GMB', description='BOMBA AGUA', quantity=6)
        again = MatchingCase.objects.get(pk=case.pk)
        self.assertEqual((MatchingCase.objects.filter(kind='merge').count(), again.fingerprint, again.ai), (1, case.fingerprint, {'target_sku': pump.sku}))
        self.assertEqual({row['code'] for row in again.evidence['codes']}, {'GWT-41A', 'GWT-41B'})

    def test_two_spellings_of_one_declared_code_are_one_case(self):
        declared = [{'code': '04465-02220', 'brand': 'TOYOTA'}, {'code': '0446502220', 'brand': 'TOYOTA'}]
        self.ingest('58411-1R000', description='DISCO FRENO DEL', references_=declared, update='spellings-5')
        case = MatchingCase.objects.get(kind='reference')
        MatchingCase.objects.filter(pk=case.pk).update(status='dismissed')
        for quantity in (6, 7):
            self.ingest('58411-1R000', description='DISCO FRENO DEL', references_=declared, quantity=quantity, update=f'spellings-{quantity}')
        self.assertEqual((MatchingCase.objects.filter(kind='reference').count(), MatchingCase.objects.get(pk=case.pk).status), (1, 'dismissed'))

    def test_an_item_takes_the_status_its_line_has_under_the_lock(self):
        from . import new_products
        first = self.ingest('ZQ-88800001', brand='GMB', description='BOMBA AGUA')
        new = first.part
        late = SupplierItem.objects.create(supplier=self.other, supplier_invent_id='LATE', codigo='ZQ-88800001', brand='GMB',
                                           description='BOMBA AGUA')
        resolution = rm.resolve(rm.subject_of(late))
        self.assertEqual((resolution.action, resolution.status), ('attach', 'pending'))
        new.refresh_from_db()
        new_products.publish(new.pk, self.admin, new.version)  # published between resolve and the write
        rm.carry_out(late, resolution, save=True)
        late.refresh_from_db()
        self.assertEqual((late.part_id, late.matching_status), (new.pk, 'matched'))

    def test_moving_an_item_from_a_variant_case_keeps_its_waiting_code_waiting(self):
        item = self.ingest('58411-1R000', stable='VW', description='DISCO FRENO HYUNDAI TRAS')
        variant = MatchingCase.objects.get(kind='variant')
        self.ingest('J69-3502010', stable='VW', description='DISCO FRENO HYUNDAI TRAS', quantity=6)
        variant.refresh_from_db()
        rear = line('58411-1R100', 'DISCO FRENO HYUNDAI ACCENT TRAS', 'HYUNDAI', 'oem')
        response = self.decide(variant, 'approve', line=str(rear.pk))
        self.assertEqual(response.status_code, 200, response.data)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.codigo, item.codigo_pendiente, item.matching_status), (rear.pk, '58411-1R000', 'J69-3502010', 'review'))
        rm.recheck_cases()
        self.assertTrue(MatchingCase.objects.filter(key=f'code_change:{item.pk}', status='review').exists())  # rule 5b: still its decision
        with self.assertRaises(rm.ValidationError):
            rm.separate(item)

    def test_moving_a_new_products_only_item_merges_its_line_instead_of_leaving_it_empty(self):
        other = self.ingest('ZZ-OTRO-12345', supplier=self.other, brand='KAIYI', description='TAMBOR',
                            references_=[{'code': 'J69-3502010', 'brand': 'CHERY'}])
        own = other.part
        self.assertEqual((own.estado, references.main_of(own).tipo), ('preparacion', 'oem'))
        case = MatchingCase.objects.get(key__startswith=f'reference:{other.pk}:')
        response = self.decide(case, 'approve', line=str(self.chery.pk))
        self.assertEqual(response.status_code, 200, response.data)
        own.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((own.estado, own.merged_into_id), ('unificada', self.chery.pk))
        self.assertEqual((other.part_id, other.reference.code, other.reference.part_id, other.matching_status),
                         (self.chery.pk, 'ZZ-OTRO-12345', self.chery.pk, 'matched'))
        self.assertFalse(Part.objects.filter(estado='preparacion').exists())  # estado: no empty line keeps the code
        late = self.ingest('ZZ-OTRO-12345', brand='KAIYI', description='TAMBOR')
        self.assertEqual(late.part_id, self.chery.pk)

    def test_a_variant_case_follows_its_item_when_its_line_is_merged(self):
        item = self.ingest('58411-1R000', stable='MV', description='DISCO FRENO HYUNDAI TRAS')
        case = MatchingCase.objects.get(kind='variant')
        target = line('DISC-TARGET-1', 'DISCO FRENO HYUNDAI ACCENT DEL')
        merge_lines(target, [self.disc], actor=self.admin)
        case.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual((case.key, case.status, set(case.lines.values_list('pk', flat=True)), item.part_id),
                         (f'variant:{item.pk}:{target.pk}', 'review', {target.pk}, target.pk))
        self.assertEqual(rm.open_review_case(item, ('variant',)).pk, case.pk)
        rm.recheck_cases()
        self.assertEqual(MatchingCase.objects.get(pk=case.pk).status, 'review')
        response = self.decide(case, 'dismiss')
        self.assertEqual(response.status_code, 200, response.data)
        item.refresh_from_db()
        self.assertEqual(item.matching_status, 'matched')

    def test_a_line_rule_7_brought_stays_in_productos_nuevos_once_published(self):
        from . import lines
        number = make_library_number('TOYOTA', '16100-39999', description='BOMBA AGUA')
        self.ingest('16100-39999', description='BOMBA AGUA')
        library = Part.objects.get(pk=number.part_id)
        self.assertEqual((library.estado, library.origen, lines.prepared_by_rule_7(library)), ('preparacion', 'biblioteca', True))
        response = self.client.post(f'/api/v1/management/catalog/proposals/{library.pk}/',
                                    {'action': 'approve', 'expected_version': library.version}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        listed = self.client.get('/api/v1/management/catalog/proposals/', {'status': 'approved'})
        self.assertIn(str(library.pk), [row['id'] for row in listed.data['results']])

    def test_codes_the_s6_code_writes_carry_their_brands_canonical_name(self):
        from .brand_models import BrandAlias
        from .variants import reference_for
        BrandAlias.objects.create(alias='TOY', brand=Brand.objects.get(nombre='TOYOTA'))
        self.assertEqual((rm.written_brand('TOY'), rm.written_brand('GENERICO'), rm.written_brand('')), ('TOYOTA', '', ''))
        target = line('BRAND-LINE-1', 'FILTRO')
        alias, _ = reference_for(target, 'QQ-1', 'TOY', self.supplier.pk, create=True)
        placeholder, _ = reference_for(target, 'QQ-2', 'GENERICO', self.supplier.pk, create=True)
        self.assertEqual((alias.brand, alias.tipo, placeholder.brand), ('TOYOTA', 'interno', ''))
        self.assertFalse(Brand.objects.filter(nombre='GENERICO').exists())

    def test_the_excel_snapshot_reads_a_line_in_referencia_as_a_new_product(self):
        from .supplier_import import outcome_key
        number = make_library_number('TOYOTA', '16100-38888', description='BOMBA AGUA')
        library = Part.objects.get(pk=number.part_id)
        before = outcome_key('attach', library, 'pending')
        self.ingest('16100-38888', description='BOMBA AGUA')  # an earlier batch's item: rule 7
        library.refresh_from_db()
        self.assertEqual((before, outcome_key('attach', library, 'pending')), (('nuevo', 'pending'), ('nuevo', 'pending')))

    def test_revision_ias_codigo_interno_after_s6_makes_a_moved_items_code_its_own(self):
        from . import new_products
        item = self.ingest('ZQ-55500077', brand='GMB', description='BOMBA AGUA')
        self.assertFalse(rm.internal_code(item))
        new_products.make_interno(item.part, references.main_of(item.part), None)
        item.refresh_from_db()
        self.assertTrue(rm.internal_code(item))
        self.assertEqual(rm.placement(item)['tipo'], 'interno')

    def test_a_fingerprint_does_not_move_with_the_engine_version(self):
        spec = rm.merge_case_spec([self.disc, self.chery], evidence={'codes': [{'code': 'X', 'brand': ''}]}, reason='prueba')
        before = rm.fingerprint_of(spec)
        with patch('mall.matching_engine.VERSION', 'otra-version'):
            self.assertEqual(rm.fingerprint_of(spec), before)


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL.')
class DatabaseOnDeleteTests(Matching, TestCase):
    """0083/0084's keys act in the database too: the code before S6 deletes cases and lines without knowing them (a code-only revert)."""

    def test_a_case_or_a_line_deleted_by_code_that_does_not_know_the_s6_keys(self):
        import uuid
        from .sku_review_models import SkuReviewRun, SkuReviewVerdict
        from .lines import bulk_create_lines
        a, b = line('ODEL-A1', 'BOMBA'), line('ODEL-B1', 'BOMBA')
        case = rm.open_case(rm.merge_case_spec([a, b], evidence={}, reason='prueba'))
        [bare] = bulk_create_lines([Part(sku='ODEL-BARE-1', description='BOMBA', estado='preparacion', origen='proveedor')], mains=False)
        run = SkuReviewRun.objects.create(id=uuid.uuid4(), created_by=self.admin, proposal_ids=[str(bare.pk)])
        verdict = SkuReviewVerdict.objects.create(run=run, line=bare, position=0, proposal_version=1, method='rule', verdict='good')
        with connection.cursor() as cursor:
            cursor.execute('DELETE FROM mall_matchingcase WHERE id = %s', [case.pk])
            cursor.execute('SELECT count(*) FROM mall_matchingcase_lines WHERE matchingcase_id = %s', [case.pk])
            self.assertEqual(cursor.fetchone()[0], 0)
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')  # nothing left pointing at the deleted case
            cursor.execute('SET CONSTRAINTS ALL DEFERRED')
            cursor.execute('DELETE FROM mall_part WHERE id = %s', [bare.pk])  # a line with nothing else pointing at it
        verdict.refresh_from_db()
        self.assertIsNone(verdict.line_id)


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks.')
class LockOrderTests(TransactionTestCase):
    """A load and a merge of the line its code is on, at once: the load waits for the merge (the line FOR SHARE behind its FOR UPDATE),
    then reads the Reference where the merge put it. No deadlock, and the item is on the merged line (I4)."""

    def test_a_concurrent_merge_and_load(self):
        brands()
        admin = make_user('lock-admin', superuser=True)
        from .models import Account
        supplier = Account.objects.create(name='PROVEEDOR LOCK')  # a TransactionTestCase may run after a flush of the seeded roles
        source = line('LOCK-SRC-1', 'BOMBA AGUA')
        target = line('LOCK-TGT-1', 'BOMBA AGUA')
        references.add(source, '33333-44444', '', tipo='otro')
        locked, release, results = threading.Event(), threading.Event(), []

        def merge():
            from django.db import connection as thread_connection, transaction
            try:
                with transaction.atomic():
                    list(Part.objects.select_for_update().filter(pk__in=[source.pk, target.pk]).order_by('pk'))
                    locked.set()
                    release.wait(10)
                    merge_lines(target, [source], actor=admin)
            finally:
                thread_connection.close()

        def load():
            from django.db import connection as thread_connection
            try:
                results.append(ingest_inventory(supplier=supplier, actor=admin, data={
                    'update_id': 'lock-1', 'supplier_invent_id': 'LOCK-1', 'codigo': '33333-44444', 'brand': '', 'description': 'BOMBA AGUA',
                    'source': 'upload', 'quantity': 1}))
            finally:
                thread_connection.close()
        merger = threading.Thread(target=merge)
        merger.start()
        locked.wait(10)
        loader = threading.Thread(target=load)
        loader.start()
        time.sleep(0.5)
        release.set()
        merger.join(20)
        loader.join(20)
        self.assertFalse(merger.is_alive() or loader.is_alive())
        item = SupplierItem.objects.get(supplier_invent_id='LOCK-1')
        self.assertEqual((item.part_id, item.reference.part_id), (target.pk, target.pk))
