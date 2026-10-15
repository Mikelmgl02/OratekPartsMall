import io
import json
import os
import tempfile

from django.test import TestCase

from . import supplier_catalog_import as sci
from .models import Part, Reference
from .oem_links import part_equivalents
from . import references
from .models import Reference, ReferenceSource
from .oem_reference import LibraryNumber
from .technical_models import PartSpecification, PartType, TechnicalField
from .test_oem_finder import Fresh
from .test_factories import make_code, make_line


def filed(result, number=None):
    """The aftermarket codes an import filed under its numbers, sorted: (number clave, brand, code, citations). From S10 they are the
    rows it read, in memory (result['rows']), never stored."""
    claves = dict(Reference.all_objects.filter(pk__in={row[1] for row in result['rows']}).values_list('pk', 'clave'))
    rows = sorted((claves[row[1]], row[2], row[3], tuple(row[5])) for row in result['rows'])
    return [row for row in rows if number is None or row[0] == number]


def filed_codes(result, number, brand=None):
    return sorted((row[1], row[2]) if brand is None else row[2] for row in filed(result, number) if brand is None or row[1] == brand)


def linked(result, part):
    """The numbers (claves) the import matched a SKU's catalog item to (result['links'], in memory from S10)."""
    claves = dict(Reference.all_objects.filter(pk__in={ref for _, ref in result['links']}).values_list('pk', 'clave'))
    return sorted({claves[ref] for pk, ref in result['links'] if str(pk) == str(part.pk)})


def reached(part):
    """The aftermarket codes a SKU reaches through its OEM numbers, as (brand, code)."""
    return {(code['brand'], code['code']) for code in part_equivalents(part)['cross_references']}


def library(**filters):
    """The library's rows (S4: References of tipo oem with a brand)."""
    return Reference.objects.filter(references.library_q(), **filters)


def number(manufacturer, code):
    """A library number as the Biblioteca shows it (the OEM table's names)."""
    return LibraryNumber(library().get(brand=manufacturer, clave=code))


def approve_link_case(*lines):
    """S9, rule 3f: an import that ties a SKU to a catalog item's numbers in referencia opens a merge case (a published line is
    involved, so nothing merges on its own); approving it puts the numbers, and the codes filed under them, on the SKU. Returns the
    case."""
    from .matching_models import MatchingCase
    from .models import User
    from .reference_matching import decide
    ids = {str(line.pk) for line in lines}
    case = next(case for case in MatchingCase.objects.filter(kind='merge', status='review')
                if ids <= {str(pk) for pk in case.lines.values_list('pk', flat=True)})
    root = User.objects.filter(username='catalog-import-root').first() or User.objects.create_superuser(
        'catalog-import-root', 'catalog-import-root@example.invalid', 'Example938!')
    decide(case, 'merge', actor=root, line=lines[0].pk)
    return case


def source(number, kind='aftermarket_catalog'):
    return next(row for row in number.sources if row.kind == kind)


def extract(items, key='test_cat', title='CATÁLOGO DE PRUEBA', brand='GMB', product_line='water_pump'):
    return {'catalog': {'key': key, 'title': title, 'brand': brand, 'product_line': product_line, 'file': '', 'pages': 10, 'year': '2016'},
            'items': items, 'stats': {}}


def item(brand_code, product_type, oem=(), cross_refs=(), specs=(), name_es='BOMBA DE AGUA', apps=()):
    return {'brand_code': brand_code, 'product_type': product_type, 'name': '', 'name_es': name_es, 'position': '',
            'oem': [{'code': c, 'manufacturer': m, 'page': 5} for c, m in oem],
            'cross_refs': [{'brand': b, 'code': c, 'page': 7} for b, c in cross_refs],
            'applications': [{'make': a[0], 'model': a[1], 'chassis': '', 'engine': '', 'cc': '', 'years': '', 'transmission': '', 'position': '',
                              'page': 5} for a in apps],
            'specs': [{'key': k, 'label_es': label, 'value': v, 'unit': u, 'raw': '', 'page': 6} for k, label, v, u in specs], 'pages': [5]}


class SupplierCatalogImportTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.pump = Part.objects.create(sku='16100-39466-G', description='BOMBA AGUA TOY HIACE 2KD')
        self.joint = make_line(sku='43460-69115', description='PUNTA FLECHA TOY LAND CRUISER 200', is_OEM=True)
        self.filter = Part.objects.create(sku='16100-80007', description='FILTRO ACEITE TOY HIACE')
        self.owner = Part.objects.create(sku='AW-OWNED-SKU', description='BOMBA AGUA NIS URVAN')
        Reference.objects.create(part=self.owner, code='AW9999', brand='AIRTEX')
        self.data = [
            extract([
                item('GWT-142A', 'water_pump', oem=[('16100-39466', 'TOYOTA'), ('16100-39465', 'TOYOTA')],
                     cross_refs=[('AISIN', 'WPT-142'), ('AIRTEX', 'AW9999')], apps=[('TOYOTA', 'HIACE 2KD')]),
                item('GWT-100A', 'water_pump', oem=[('16100-80007', 'TOYOTA')]),
            ]),
            extract([item('TY-LC200', 'cv_joint_outer', oem=[('43460-69115', 'TOYOTA'), ('43460-69116', 'TOYOTA')], name_es='JUNTA HOMOCINÉTICA EXTERIOR',
                          specs=[('outer_splines', 'ESTRÍAS EXTERIORES', '30', ''), ('seal_diameter', 'DIÁMETRO DEL RETÉN', '68,5', 'mm')])],
                    key='asva_cv', title='ASVA CV', brand='ASVA', product_line='cv_joint'),
        ]
        self.files = []
        for data in self.data:
            handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
            json.dump(data, handle, ensure_ascii=False)
            handle.close()
            self.files.append(handle.name)

    def tearDown(self):
        for path in self.files:
            os.unlink(path)
        super().tearDown()

    def run_import(self, apply):
        return sci.run(self.files, apply=apply, stdout=io.StringIO())

    def state(self):
        return (Reference.objects.count(), library().count(), ReferenceSource.objects.count(), PartSpecification.objects.count(),
                PartType.objects.count(), TechnicalField.objects.count(), list(Part.objects.order_by('sku').values_list('sku', 'is_OEM', 'part_type_id')))

    def test_dry_run_writes_nothing_and_reports_the_plan(self):
        before = self.state()
        result = self.run_import(apply=False)
        self.assertEqual(self.state(), before)
        gmb, asva = (c['counts'] for c in result['catalogs'])
        self.assertEqual((gmb['linked_parts'], gmb['oem_numbers'], gmb['codes_carried_by_numbers'], gmb['cross_reference_rows']), (1, 3, 1, 7))
        self.assertEqual((gmb.get('brand_code_new', 0), gmb.get('cross_ref_new', 0)), (0, 0))  # the numbers carry the codes: no copies
        self.assertEqual(asva['linked_parts'], 1)
        self.assertEqual(asva['subgroups_assigned'], 1)

    def test_apply_creates_oem_table_links_and_measurements_without_copying_codes(self):
        self.run_import(apply=True)
        ref = number('TOYOTA', '1610039466')
        self.assertEqual(ref.status, 'declared')
        self.assertEqual(ref.part_type, 'BOMBAS DE AGUA')
        cited = source(ref)
        self.assertEqual(cited.citation, 'CATÁLOGO DE PRUEBA (2016)')
        self.assertEqual(cited.detail['brand_codes'], ['GWT-142A'])
        self.assertEqual(ref.ref.part.estado, 'referencia')  # S4: no line carries it, so it has a line of its own
        self.assertTrue(library(brand='TOYOTA', clave='1610039465').exists())  # printed, carried by no SKU
        self.assertFalse(self.pump.codes.filter(is_main=False).exists())  # alternos step 2c: the codes reach the pump through its numbers, never as copies
        self.assertEqual(reached(self.pump), set())  # S9: a catalog link no longer reaches the numbers; 3f opens a merge case
        case = approve_link_case(self.pump, ref.ref.part, library().get(brand='TOYOTA', clave='1610039465').part)
        self.assertEqual(case.evidence['regla'], '3f')
        # S10: what the line reaches is its References: the catalog's own code; a competitor's only from a PDF-checked catalog (OD17).
        self.assertEqual(reached(self.pump), {('GMB', 'GWT-142A')})
        # 3e on the merged line: the catalog's own code is declarado; a competitor's needs a catalog checked against its PDF (OD17), and
        # AW9999 is another SKU's alterno (rule 8).
        self.assertEqual(set(self.pump.codes.filter(tipo='aftermarket').values_list('brand', 'code', 'estado')), {('GMB', 'GWT-142A', 'declarado')})
        self.assertEqual(Reference.objects.get(code='AW9999').part, self.owner)  # another SKU's alterno is never moved
        self.assertFalse(self.filter.codes.filter(is_main=False, tipo='aftermarket').exists())  # the description is an oil filter, not the catalog's water pump
        self.assertEqual(number('TOYOTA', '1610080007').ref.part_id, self.filter.pk)  # S4: its codigo carries the number, which is library data
        # The joint carries its own number (S4) and, already marked OEM, takes the sister number as an OEM alterno.
        self.assertEqual(set(self.joint.codes.filter(is_main=False).values_list('brand', 'code', 'tipo')),
                         {('TOYOTA', '43460-69115', 'oem'), ('TOYOTA', '43460-69116', 'oem'), ('ASVA', 'TY-LC200', 'aftermarket')})  # S9's 3e
        self.assertIn(('ASVA', 'TY-LC200'), reached(self.joint))
        self.joint.refresh_from_db()
        self.assertEqual((self.joint.category, self.joint.subcategory), ('TRANSMISIÓN', 'JUNTAS HOMOCINÉTICAS'))
        values = {s.field.key: s for s in self.joint.specifications.select_related('field')}
        self.assertEqual(values['outer_splines'].number_value, 30)
        self.assertEqual(values['outer_splines'].field.kind, 'integer')
        self.assertEqual(str(values['seal_diameter'].number_value.normalize()), '68.5')
        self.assertEqual(values['seal_diameter'].field.unit, 'mm')
        self.assertEqual(self.joint.technical_revision, 1)

    def test_apply_files_aftermarket_codes_under_every_number_and_links_the_skus_to_them(self):
        result = self.run_import(apply=True)
        for number in ('1610039466', '1610039465'):  # the second is carried by no SKU: its codes are read all the same
            self.assertEqual(filed_codes(result, number), [('AIRTEX', 'AW9999'), ('AISIN', 'WPT-142'), ('GMB', 'GWT-142A')])
        self.assertEqual([row[3] for row in filed(result, '1610039466') if row[1] == 'AISIN'], [('CATÁLOGO DE PRUEBA (2016) · pág. 7',)])
        self.assertEqual(linked(result, self.pump), ['1610039465', '1610039466'])
        self.assertEqual(linked(result, self.owner), [])  # owning AW9999 as an alterno ties no SKU to the numbers
        gmb = result['catalogs'][0]['counts']
        self.assertEqual((gmb['cross_reference_rows'], gmb['catalog_links']), (7, 2))

    def test_duplicate_skus_of_one_item_get_no_alternos_but_both_reach_its_numbers(self):
        twin = Part.objects.create(sku='16100-39465-RAZ', description='BOMBA AGUA TOY HIACE')
        result = self.run_import(apply=True)
        self.assertEqual(result['catalogs'][0]['counts']['duplicate_part_items'], 1)
        self.assertFalse(Reference.objects.filter(part__in=[self.pump, twin], brand='AISIN').exists())  # a code has one owner
        for part in (self.pump, twin):
            self.assertEqual(linked(result, part), ['1610039465', '1610039466'])

    def test_no_oem_alterno_on_a_sku_not_marked_oem(self):
        self.run_import(apply=True)
        self.assertFalse(Reference.objects.filter(part=self.pump, tipo='oem').exists())

    def test_reapplying_changes_nothing(self):
        self.run_import(apply=True)
        once = self.state()
        result = self.run_import(apply=True)
        self.assertEqual(self.state(), once)
        self.assertEqual(result['catalogs'][0]['counts']['codes_carried_by_numbers'], 1)

    def test_a_dry_run_after_an_apply_reports_stored_measurements_as_present(self):
        self.run_import(apply=True)
        asva = self.run_import(apply=False)['catalogs'][1]['counts']
        self.assertEqual((asva.get('specs_new', 0), asva['specs_present']), (0, 2))

    def test_a_different_stored_measurement_is_kept_and_reported(self):
        self.run_import(apply=True)
        spec = PartSpecification.objects.get(part=self.joint, field__key='outer_splines')
        spec.number_value = 33
        spec.save()
        result = self.run_import(apply=True)
        spec.refresh_from_db()
        self.assertEqual(spec.number_value, 33)
        self.assertEqual(result['catalogs'][1]['counts']['specs_conflict'], 1)

    def test_a_retired_sku_neither_takes_the_codes_nor_makes_the_live_sku_a_duplicate(self):
        retired = Part.objects.create(sku='16100-39466-G-ANULADO', description='BOMBA AGUA TOY HIACE 2KD')
        result = self.run_import(apply=True)
        self.assertEqual(result['catalogs'][0]['counts']['retired_skus_skipped'], 1)
        self.assertFalse(retired.codes.filter(is_main=False).exists() or linked(result, retired))
        case = approve_link_case(self.pump, library().get(brand='TOYOTA', clave='1610039466').part)
        self.assertNotIn(retired.pk, set(case.lines.values_list('pk', flat=True)))
        self.assertIn(('GMB', 'GWT-142A'), reached(self.pump))

    def test_a_sku_matching_two_items_of_one_catalog_is_left_alone(self):
        Reference.objects.create(part=self.pump, code='16100-80007', brand='TOYOTA')
        result = self.run_import(apply=True)
        self.assertEqual(result['catalogs'][0]['counts']['ambiguous_parts'], 1)
        self.assertFalse(self.pump.codes.filter(brand='GMB').exclude(detail__has_key='s9').exists())  # no copy (S9's 3e: the number's own)

    def test_a_sku_classified_elsewhere_keeps_its_subgroup_and_gets_no_measurements(self):
        from .lines import set_subgrupo
        from .technical_data import part_type_for
        self.joint.save(update_fields=set_subgrupo(self.joint, part_type_for('TRANSMISIÓN', 'SEMIEJES')))
        result = self.run_import(apply=True)
        self.joint.refresh_from_db()
        self.assertEqual(self.joint.subcategory, 'SEMIEJES')
        self.assertFalse(self.joint.specifications.exists())
        self.assertEqual(result['catalogs'][1]['counts'].get('linked_parts', 0), 0)  # the product rules disagree with SEMIEJES


class SharedNumberTests(Fresh, TestCase):
    def test_a_number_printed_for_two_parts_files_no_codes_and_ties_no_sku(self):
        pump = Part.objects.create(sku='16100-79445-NPW', description='BOMBA AGUA TOY HIACE 2TR')
        data = extract([item('GWT-71A', 'water_pump', oem=[('16100-79445', 'TOYOTA'), ('16100-69185', 'TOYOTA')], cross_refs=[('AISIN', 'WPT-071')]),
                        item('GWT-131A', 'water_pump', oem=[('16100-79445', 'TOYOTA'), ('16100-79465', 'TOYOTA')], cross_refs=[('AISIN', 'WPT-131')]),
                        item('GWT-116A', 'water_pump', oem=[('16100-09010', 'TOYOTA')]),
                        item('GWT-116AH', 'water_pump', oem=[('16100-09010', 'TOYOTA')], cross_refs=[('AISIN', 'WPT-116')])])
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
        try:
            result = sci.run([handle.name], apply=True, stdout=io.StringIO())
        finally:
            os.unlink(handle.name)
        codes = lambda number: sorted(row[2] for row in filed(result, number))
        self.assertEqual(codes('1610079445'), [])  # GMB prints it for two pumps: it cannot say which one replaces it
        self.assertEqual(codes('1610069185'), ['GWT-71A', 'WPT-071'])
        self.assertEqual(codes('1610009010'), ['GWT-116A', 'GWT-116AH', 'WPT-116'])  # variants of one pump count as one part
        self.assertEqual(linked(result, pump), [])  # matched only through the shared number
        self.assertFalse(pump.codes.filter(is_main=False).exists())  # nor copies of either pump's codes
        counts = result['catalogs'][0]['counts']
        self.assertEqual((counts['shared_numbers'], counts['matches_through_shared_numbers']), (1, 2))
        self.assertEqual(result['catalogs'][0]['samples']['shared_number_matches'][0]['numbers'], ['1610079445'])


class NumberlessItemTests(Fresh, TestCase):
    def test_codes_no_number_can_carry_still_reach_the_sku_as_alternos(self):
        pump = Part.objects.create(sku='BOMBA-X', description='BOMBA AGUA TOY HIACE 3L')
        make_code(part=pump, brand='GMB', code='GWX-999', ref_type='company')
        data = extract([item('GWX-999', 'water_pump', cross_refs=[('AISIN', 'WPX-999')], apps=[('TOYOTA', 'HIACE')])])
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
        try:
            result = sci.run([handle.name], apply=True, stdout=io.StringIO())
        finally:
            os.unlink(handle.name)
        self.assertEqual(result['catalogs'][0]['counts']['cross_ref_new'], 1)
        self.assertTrue(pump.codes.filter(brand='AISIN', code='WPX-999', tipo='aftermarket').exists())  # the catalog prints no OEM number


class SupplierCatalogImportGuardTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.joint = Part.objects.create(sku='49501-2S300-PF', description='PUNTA FLECHA HYU TUCSON')
        data = extract([item('HY-IX35', 'cv_joint_outer', oem=[('49501-2S300', 'HYUNDAI')], name_es='JUNTA HOMOCINÉTICA EXTERIOR',
                             specs=[('seal_diameter', 'DIÁMETRO DEL RETÉN', '630', 'mm'), ('cv_boot', 'GUARDAPOLVO', '2071', ''),
                                    ('outer_splines', 'ESTRÍAS EXTERIORES', '27', '')])], key='asva', title='ASVA CV', brand='ASVA')
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(data, handle, ensure_ascii=False)
        handle.close()
        self.path = handle.name

    def tearDown(self):
        os.unlink(self.path)
        super().tearDown()

    def test_typos_are_reported_codes_stay_text_and_assembly_numbers_are_labelled(self):
        result = sci.run([self.path], apply=True, stdout=io.StringIO())
        counts = result['catalogs'][0]['counts']
        self.assertEqual(counts['specs_out_of_range'], 1)  # a 630 mm CV seal is a catalog typo
        values = {s.field.key: s for s in self.joint.specifications.select_related('field')}
        self.assertNotIn('seal_diameter', values)
        self.assertEqual((values['cv_boot'].field.kind, values['cv_boot'].text_value), ('text', '2071'))
        self.assertEqual(values['outer_splines'].number_value, 27)
        ref = number('HYUNDAI', '495012S300')
        self.assertEqual(ref.part_type, 'SEMIEJES')  # Hyundai 49501 is a drive-shaft assembly number, listed for the joint
        self.assertTrue(source(ref).detail['assembly_number_listed_for_component'])
        self.assertEqual(filed(result, '495012S300'), [])  # the joint's codes do not replace the whole drive shaft
        self.assertEqual(linked(result, self.joint), [])


class OEMOnlyItemTests(Fresh, TestCase):
    def test_items_without_a_brand_code_only_feed_the_oem_table(self):
        part = Part.objects.create(sku='43460-60010', description='PUNTA FLECHA TOY LAND CRUISER')
        data = extract([item('', 'cv_joint_outer', oem=[('43460-60010', 'TOYOTA')], name_es='JUNTA HOMOCINÉTICA EXTERIOR')],
                       key='asva_only', title='ASVA CV — OEM sin pieza ASVA', brand='ASVA')
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
        try:
            result = sci.run([handle.name], apply=True, stdout=io.StringIO())
        finally:
            os.unlink(handle.name)
        counts = result['catalogs'][0]['counts']
        self.assertEqual((counts['oem_only_items'], counts.get('linked_parts', 0)), (1, 0))
        ref = number('TOYOTA', '4346060010')
        self.assertEqual(source(ref).detail['brand_codes'], [])
        self.assertEqual(ref.ref.part_id, part.pk)  # S4: its codigo carries the number, library data beside its main
        self.assertFalse(part.codes.filter(is_main=False, tipo='aftermarket').exists())


class BrakeDiscSheetTests(Fresh, TestCase):
    def test_a_disc_hat_diameter_is_kept_and_the_brand_short_number_is_not_a_measurement(self):
        disc = make_line(sku='43512-60150', description='DISCO FRENO DEL TOY LAND CRUISER', is_OEM=True)
        data = extract([item('24.0132-0123.1', 'brake_disc', oem=[('43512-60150', 'TOYOTA')], name_es='DISCO DE FRENO',
                             specs=[('inner_diameter', 'DIÁMETRO INTERIOR', '210', 'mm'), ('short_number', 'NÚMERO CORTO', '410123', ''),
                                    ('diameter', 'DIÁMETRO', '354', 'mm')])], key='ate', title='ATE', brand='ATE', product_line='brake_disc')
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
        try:
            result = sci.run([handle.name], apply=True, stdout=io.StringIO())
        finally:
            os.unlink(handle.name)
        self.assertEqual(result['catalogs'][0]['counts'].get('specs_out_of_range', 0), 0)  # 210 mm is a disc hat, not a bushing bore
        values = {s.field.key: s for s in disc.specifications.select_related('field')}
        self.assertEqual((values['inner_diameter'].number_value, values['diameter'].number_value), (210, 354))
        self.assertNotIn('short_number', values)
        self.assertFalse(TechnicalField.objects.filter(key='short_number').exists())


class MergePassAfterImportTests(Fresh, TestCase):
    """S9, rule 3f (mall.merge_pass.join_numbers): the numbers an item prints merge on their own only while every line is in referencia
    without variants; with a SKU among them a merge case opens, and the codes on one line become References (3e)."""

    def test_an_item_s_numbers_in_referencia_merge_and_a_sku_among_them_opens_a_case(self):
        from .matching_models import MatchingCase
        sku = Part.objects.create(sku='21010-0W025', description='BOMBA AGUA NIS PATHFINDER')
        data = extract([item('GWT-500A', 'water_pump', oem=[('16100-50001', 'TOYOTA'), ('16100-50002', 'TOYOTA')], apps=[('TOYOTA', 'HILUX')]),
                        item('GWN-61A', 'water_pump', oem=[('21010-0W025', 'NISSAN'), ('21010-0W026', 'NISSAN')], apps=[('NISSAN', 'PATHFINDER')])])
        run_extract(data)
        first, second = library().get(clave='1610050001'), library().get(clave='1610050002')
        self.assertEqual(first.part_id, second.part_id)  # both lines were in referencia without variants: one line now
        line = Part.objects.get(pk=first.part_id)
        self.assertEqual((line.estado, Part._base_manager.filter(merged_into=line).count()), ('referencia', 1))
        own = Reference.objects.get(part=line, brand='GMB')
        self.assertEqual((own.code, own.tipo, own.estado), ('GWT-500A', 'aftermarket', 'declarado'))
        nissan = library().get(clave='210100W026')
        self.assertNotEqual(nissan.part_id, sku.pk)  # a published line is among them: nothing merges on its own
        case = MatchingCase.objects.get(kind='merge')
        self.assertEqual(({str(pk) for pk in case.lines.values_list('pk', flat=True)}, case.evidence['regla']),
                         ({str(sku.pk), str(nissan.part_id)}, '3f'))
        self.assertEqual(run_extract(data)['catalogs'][0].get('merge_pass', {}), {'references': 0})  # again: the case stands, nothing new


class HubAndTensionerTests(Fresh, TestCase):
    def test_a_sku_takes_the_compatible_subgroup_its_own_description_names(self):
        hub = make_line(sku='44300-SDA-A51', description='HUB DEL HON ACCORD 03-07', is_OEM=True)
        bearing = Part.objects.create(sku='13505-54020-KOYO', description='BALINERA TENSOR TOY 2L 3L')
        locking = Part.objects.create(sku='43530-60010', description='HUB LIBRE TOY LAND CRUISER')
        data = extract([item('HNWH-CM5F', 'hub_bearing', oem=[('44300-SDA-A51', 'HONDA')], name_es='RODAMIENTO DE RUEDA',
                             specs=[('abs_teeth', 'DIENTES ABS', '48', ''), ('outer_diameter', 'DIÁMETRO EXTERIOR', '84', 'mm')]),
                        item('TYBP-001', 'tensioner', oem=[('13505-54020', 'TOYOTA')], name_es='TENSOR DE CORREA'),
                        item('TYWH-FJ80F', 'wheel_hub', oem=[('43530-60010', 'TOYOTA')], name_es='CUBO DE RUEDA')],
                       key='asva_hub', title='ASVA HUB', brand='ASVA', product_line='wheel_hub')
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
        try:
            sci.run([handle.name], apply=True, stdout=io.StringIO())
        finally:
            os.unlink(handle.name)
        hub.refresh_from_db()
        self.assertEqual((hub.category, hub.subcategory), ('RODAMIENTOS', 'CUBOS DE RUEDA'))  # its description, not the item's bearing subgrupo
        self.assertEqual(hub.specifications.get(field__key='abs_teeth').number_value, 48)
        approve_link_case(bearing, library().get(brand='TOYOTA', clave='1350554020').part)  # S9, 3f: the catalog link is a merge case
        self.assertIn(('ASVA', 'TYBP-001'), reached(bearing))
        # A free-wheel locking hub is not a wheel hub: the import copies nothing onto it (the code it reaches through the number its
        # codigo carries is that number's, which S9's 3e makes a Reference of the line the number sits on).
        self.assertFalse(locking.codes.filter(is_main=False, tipo='aftermarket').exclude(detail__has_key='s9').exists())


class MakeGroupTests(Fresh, TestCase):
    def test_catalog_manufacturers_meet_the_finder_makes_of_the_skus(self):
        group = sci.CatalogImport.make_group
        self.assertEqual({group(m) for m in ['VOLKSWAGEN', 'AUDI', 'SKODA', 'SEAT', 'VAG']}, {'VAG'})
        self.assertEqual((group('MERCEDES-BENZ'), group('MB')), ('MB', 'MB'))
        self.assertEqual({group(m) for m in ['JEEP', 'DODGE', 'CHRYSLER', 'MOPAR']}, {'MOPAR'})
        self.assertEqual({group(m) for m in ['CHEVROLET', 'CADILLAC', 'GM', 'DAEWOO']}, {'GM'})
        self.assertEqual({group(m) for m in ['TOYOTA', 'LEXUS', 'DAIHATSU']}, {'TOYOTA'})
        self.assertEqual({group(m) for m in ['PEUGEOT', 'VOLVO', 'LAND ROVER', 'OTHER']}, {''})  # no make evidence

    def test_a_volkswagen_disc_links_to_a_vw_sku(self):
        disc = make_line(sku='1K0615301AA', description='DISCO FRENO DEL VW GOLF JETTA', is_OEM=True)
        data = extract([item('24.0125-0111.1', 'brake_disc', oem=[('1K0615301AA', 'VOLKSWAGEN'), ('1K0615301AA', 'AUDI')], name_es='DISCO DE FRENO',
                             apps=[('VOLKSWAGEN', 'GOLF V'), ('SEAT', 'LEON')])], key='ate', title='ATE', brand='ATE', product_line='brake_disc')
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
        try:
            result = sci.run([handle.name], apply=True, stdout=io.StringIO())
        finally:
            os.unlink(handle.name)
        self.assertEqual(result['catalogs'][0]['counts'].get('linked_parts', 0), 1)
        self.assertIn(('ATE', '24.0125-0111.1'), reached(disc))


class ChainedMatchTests(Fresh, TestCase):
    def test_codes_a_catalog_wrote_never_link_another_item(self):
        bearing = Part.objects.create(sku='DAC4072W-3CS35', description='BALINERA DEL SUZ SWIFT 04-10')
        data = extract([item('DAC35620040', 'hub_bearing', oem=[('43440-78A00', 'SUZUKI')], cross_refs=[('SNR', 'R177.27')], name_es='RODAMIENTO DE RUEDA',
                             specs=[('inner_diameter', 'DIÁMETRO INTERIOR', '35', 'mm')]),
                        item('DAC40723336', 'hub_bearing', oem=[('09267-40001', 'SUZUKI')], cross_refs=[('KOYO', 'DAC4072W-3CS35'), ('SNR', 'R177.27')],
                             name_es='RODAMIENTO DE RUEDA')],
                       key='asva_hub', title='ASVA HUB', brand='ASVA', product_line='wheel_hub')
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
        try:
            sci.run([handle.name], apply=True, stdout=io.StringIO())
            # S9, 3f: the SKU and the line of DAC40723336's number (the item that printed the SKU itself) wait in one merge case, and
            # DAC35620040's number is in none of them.
            from .matching_models import MatchingCase
            lines = [set(case.lines.values_list('pk', flat=True)) for case in MatchingCase.objects.filter(kind='merge')]
            self.assertEqual(lines, [{bearing.pk, library().get(brand='SUZUKI', clave='0926740001').part_id}])
            again = sci.run([handle.name], apply=True, stdout=io.StringIO())
        finally:
            os.unlink(handle.name)
        self.assertEqual(again['catalogs'][0]['counts']['linked_parts'], 1)  # R177.27 is no matching key: DAC35620040 stays unlinked
        self.assertNotIn(('ASVA', 'DAC35620040'), reached(bearing))
        self.assertFalse(bearing.codes.filter(is_main=False).exists() or bearing.specifications.exists())


def run_extract(data, apply=True):
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False)
    try:
        return sci.run([handle.name], apply=apply, stdout=io.StringIO())
    finally:
        os.unlink(handle.name)


class CVAssemblyCheckTests(Fresh, TestCase):
    """Target data step 1: the shaft numbers a CV catalog lists for its joints, beyond the finder's own assembly classes."""

    def joint(self, brand_code, oem, apps=(), product_type='cv_joint_outer'):
        return item(brand_code, product_type, oem=oem, name_es='JUNTA HOMOCINÉTICA EXTERIOR', apps=apps)

    def assembly(self, manufacturer, number):
        ref = library().get(brand=manufacturer, clave=number)
        cited = ref.sources.get(kind='catalogo')
        return ref.detail.get('part_type') == 'SEMIEJES' and cited.detail.get('assembly_number_listed_for_component', False)

    def test_mazda_25_40_50_60_numbers_are_the_joint_and_shaft_skus_finder_and_partner_classes_still_make_shafts(self):
        Part.objects.create(sku='28321-FG010', description='EJE COMPLETO SUB FORESTER 08-12 RH')  # a SKU sold as the whole shaft
        shaft = Part.objects.create(sku='SEM-MAZ-01', description='SEMIEJE MAZ 3 05 RH')
        make_code(part=shaft, brand='MAZDA', code='BP4K-25-50XA', ref_type='oem')
        result = run_extract(extract([
            # The owner, 2026-10-14: 25-40X/25-50X/25-60X name the joint, X variants included, under Ford, Kia (Mazda-era too) or Mazda.
            self.joint('MZ-056A44', [('G565-25-500B', 'FORD'), ('G572-22-510', 'FORD')]),
            self.joint('MZ-11', [('0K2N1-25-600', 'KIA'), ('B002-25-400B', 'KIA'), ('KK370-22-510', 'KIA')]),
            self.joint('MZ-38', [('GA2A-25-60X', 'MAZDA'), ('PP04-25-50XA', 'MAZDA'), ('BP4K-25-50XA', 'MAZDA')]),
            self.joint('SB-12', [('28321-FG010', 'SUBARU'), ('28391-FG010', 'SUBARU')]),
            self.joint('HY-IX35', [('49501-2S300', 'HYUNDAI')]),  # the finder's own class, as before
            self.joint('HY-GAL', [('3815A068', 'HYUNDAI')]),  # a Mitsubishi shaft class under its partner make
        ], key='asva_cv', title='ASVA CV', brand='ASVA', product_line='cv_joint'))
        shafts = {('SUBARU', 'sku', '28321FG010'), ('MAZDA', 'sku', 'BP4K2550XA'), ('HYUNDAI', 'finder', '495012S300'),
                  ('HYUNDAI', 'partner', '3815A068')}
        for manufacturer, why, number in shafts:  # the SKU's OEM alterno already filed BP4K-25-50XA with the SKU's text
            cited = ReferenceSource.objects.get(reference__brand=manufacturer, reference__clave=number, kind='catalogo')
            self.assertTrue(cited.detail.get('assembly_number_listed_for_component'), (why, number))
            self.assertEqual(filed(result, number), [], number)
        joints = [('FORD', 'G56525500B'), ('FORD', 'G57222510'), ('KIA', '0K2N125600'), ('KIA', 'B00225400B'), ('KIA', 'KK37022510'),
                  ('MAZDA', 'GA2A2560X'), ('MAZDA', 'PP042550XA'), ('SUBARU', '28391FG010')]
        for manufacturer, number in joints:
            self.assertFalse(self.assembly(manufacturer, number), number)  # joint numbers keep the joint's codes
            self.assertTrue(filed_codes(result, number, 'ASVA'), number)
            ref = LibraryNumber(library().get(brand=manufacturer, clave=number))
            self.assertEqual((ref.description, ref.part_type), ('JUNTA HOMOCINÉTICA EXTERIOR', 'JUNTAS HOMOCINÉTICAS'), number)

    def test_a_shaft_sku_makes_its_numbers_shafts_by_its_oem_alterno_its_link_or_its_subgrupo(self):
        alterno = Part.objects.create(sku='SEM-MIT-01', description='SEMIEJE MIT LANCER 05 LH')
        make_code(part=alterno, brand='MITSUBISHI', code='MR453384', ref_type='oem')
        make_line(sku='MR498974', description='FLECHA COMPLETA MIT MONTERO', category='TRANSMISIÓN', subcategory='SEMIEJES')
        boot = Part.objects.create(sku='BOT-MIT-01', description='BOTA SEMIEJE MIT LANCER')  # a part of the shaft, not the shaft
        make_code(part=boot, brand='MITSUBISHI', code='MR580474', ref_type='oem')
        self.assertTrue({'MR453384', 'MR498974'} <= sci.shaft_numbers())  # (a SKU's catalog link no longer says it: S10 stores none)
        self.assertNotIn('MR580474', sci.shaft_numbers())
        result = run_extract(extract([self.joint('MT-99A50', [('MR453384', 'MITSUBISHI'), ('MR453383', 'MITSUBISHI'), ('MR498974', 'MITSUBISHI'),
                                                      ('MR580474', 'MITSUBISHI')])],
                            key='asva_cv', title='ASVA CV', brand='ASVA', product_line='cv_joint'))
        for number in ('MR453384', 'MR498974'):
            self.assertEqual(filed(result, number), [], number)
        self.assertEqual(filed_codes(result, 'MR580474'), [('ASVA', 'MT-99A50')])

    def test_an_mr_number_is_the_joint_s_own_unless_a_shaft_sku_says_otherwise(self):
        joint = Part.objects.create(sku='MR-567160-NDK', description='PUNTA FLECHA EXT MIT LANCER 05')
        result = run_extract(extract([self.joint('MT-38A43', [('MR567160', 'MITSUBISHI'), ('MR580474', 'MITSUBISHI')], apps=[('MITSUBISHI', 'LANCER')]),
                                      self.joint('MT-08', [('MR222052', 'MITSUBISHI')], product_type='cv_joint_inner')],
                                     key='asva_cv', title='ASVA CV', brand='ASVA', product_line='cv_joint'))
        self.assertEqual(result['catalogs'][0]['counts']['linked_parts'], 1)
        self.assertEqual(linked(result, joint), ['MR567160', 'MR580474'])  # MR is a series, not a shaft code: the SKU is the joint these numbers name
        self.assertEqual(sorted((row[0], row[2]) for row in filed(result)),
                         [('MR222052', 'MT-08'), ('MR567160', 'MT-38A43'), ('MR580474', 'MT-38A43')])
        self.assertFalse(self.assembly('MITSUBISHI', 'MR222052'))

    def test_a_sku_in_the_cv_joint_subgrupo_is_a_joint(self):
        joint = make_line(sku='44010-S04-010-NDK', description='PUNTA FLECHA EXT HON CIVIC VTI 99', category='TRANSMISIÓN',
                                    subcategory='CV JOINT')  # the catalog's own subgrupo for CV joints
        result = run_extract(extract([self.joint('HO-28', [('44010-S04-010', 'HONDA'), ('44014-S04-000', 'HONDA')], apps=[('HONDA', 'CIVIC')])],
                                     key='asva_cv', title='ASVA CV', brand='ASVA', product_line='cv_joint'))
        self.assertEqual(linked(result, joint), ['44010S04010', '44014S04000'])

    def test_cv_axle_items_are_shafts_themselves_and_keep_their_numbers(self):
        result = run_extract(extract([item('CVSK-001R', 'cv_axle', oem=[('0K012-25-500D', 'KIA')], name_es='SEMIEJE')],
                                     key='asva_cv', title='ASVA CV', brand='ASVA', product_line='cv_joint'))
        self.assertEqual(filed_codes(result, '0K01225500D'), [('ASVA', 'CVSK-001R')])


class OwnItemCodeTests(Fresh, TestCase):
    """Target data step 1: the catalog's own item code is filed under its numbers wherever one can take it."""

    def codes(self, number):
        return filed_codes(self.result, number)

    def test_an_own_code_without_a_digit_is_filed_and_a_competitor_one_is_not(self):
        data = extract([item('FDWH-FOCMF', 'wheel_hub', oem=[('1223640', 'FORD')], cross_refs=[('SNR', 'R152.62'), ('BENDIX', 'HUB')], name_es='CUBO DE RUEDA'),
                        item('AB', 'wheel_hub', oem=[('1230907', 'FORD')], name_es='CUBO DE RUEDA')],
                       key='asva_hub', title='ASVA HUB', brand='ASVA', product_line='wheel_hub')
        self.result = first = run_extract(data)
        self.assertEqual(self.codes('1223640'), [('ASVA', 'FDWH-FOCMF'), ('SNR', 'R152.62')])  # BENDIX HUB names nothing
        self.assertEqual(self.codes('1230907'), [])  # two letters are no code
        self.assertEqual(first['catalogs'][0]['counts']['cross_reference_rows'], 2)

    def test_ate_powerdisc_twins_are_one_disc_and_file_both_codes(self):
        self.result = run_extract(extract([item('24.0108-0111.1', 'brake_disc', oem=[('168 423 02 12', 'MERCEDES-BENZ')], name_es='DISCO DE FRENO'),
                             item('24.0308-0111.1', 'brake_disc', oem=[('168 423 02 12', 'MERCEDES-BENZ')], name_es='DISCO DE FRENO'),
                             item('24.0109-0114.1', 'brake_disc', oem=[('203 423 01 12', 'MERCEDES-BENZ')], name_es='DISCO DE FRENO'),
                             item('24.0110-0115.1', 'brake_disc', oem=[('203 423 01 12', 'MERCEDES-BENZ')], name_es='DISCO DE FRENO')],
                            key='ate', title='ATE', brand='ATE', product_line='brake_disc'))
        self.assertEqual(self.codes('1684230212'), [('ATE', '24.0108-0111.1'), ('ATE', '24.0308-0111.1')])
        self.assertEqual(self.codes('2034230112'), [])  # two different discs: the number cannot say which one replaces it
        self.assertTrue(sci.one_part(['24.0108-0111.1', '24.0308-0111.1'], 'ATE'))
        self.assertFalse(sci.one_part(['24.0108-0111.1', '24.0308-0111.1']))  # only ATE codes its PowerDisc that way
