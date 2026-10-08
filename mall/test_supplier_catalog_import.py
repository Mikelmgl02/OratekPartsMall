import io
import json
import os
import tempfile

from django.test import TestCase

from . import supplier_catalog_import as sci
from .catalog_identity import reconcile_identities
from .models import Part, PartCode
from .oem_reference_models import OEMCrossReference, OEMReference, OEMReferenceSource, PartOEMLink
from .technical_models import PartSpecification, PartType, TechnicalField
from .test_oem_finder import Fresh


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
        self.joint = Part.objects.create(sku='43460-69115', description='PUNTA FLECHA TOY LAND CRUISER 200', is_OEM=True)
        self.filter = Part.objects.create(sku='16100-80007', description='FILTRO ACEITE TOY HIACE')
        self.owner = Part.objects.create(sku='AW-OWNED-SKU', description='BOMBA AGUA NIS URVAN')
        PartCode.objects.create(part=self.owner, code='AW9999', brand='AIRTEX')
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
        return (PartCode.objects.count(), OEMReference.objects.count(), OEMReferenceSource.objects.count(), PartSpecification.objects.count(),
                PartType.objects.count(), TechnicalField.objects.count(), list(Part.objects.order_by('sku').values_list('sku', 'is_OEM', 'part_type_id')),
                sorted(OEMCrossReference.objects.values_list('reference__code', 'brand', 'code', 'citations')),
                sorted(PartOEMLink.objects.values_list('part__sku', 'reference__code', 'source', 'citations')))

    def links(self, part, source):
        return sorted(PartOEMLink.objects.filter(part=part, source=source).values_list('reference__code', flat=True))

    def test_dry_run_writes_nothing_and_reports_the_plan(self):
        before = self.state()
        result = self.run_import(apply=False)
        self.assertEqual(self.state(), before)
        gmb, asva = (c['counts'] for c in result['catalogs'])
        self.assertEqual((gmb['linked_parts'], gmb['oem_numbers'], gmb['brand_code_new'], gmb['cross_ref_new'], gmb['cross_ref_conflict']), (1, 3, 1, 1, 1))
        self.assertEqual(asva['linked_parts'], 1)
        self.assertEqual(asva['subgroups_assigned'], 1)

    def test_apply_creates_oem_table_company_alternos_and_measurements(self):
        self.run_import(apply=True)
        ref = OEMReference.objects.get(manufacturer='TOYOTA', code='1610039466')
        self.assertEqual(ref.status, 'declared')
        self.assertEqual(ref.part_type, 'BOMBAS DE AGUA')
        source = ref.sources.get(kind='aftermarket_catalog')
        self.assertEqual(source.citation, 'CATÁLOGO DE PRUEBA (2016)')
        self.assertEqual(source.detail['brand_codes'], ['GWT-142A'])
        self.assertTrue(OEMReference.objects.filter(manufacturer='TOYOTA', code='1610039465').exists())  # printed, carried by no SKU
        pump_codes = set(self.pump.codes.values_list('brand', 'code', 'ref_type'))
        self.assertEqual(pump_codes, {('GMB', 'GWT-142A', 'company'), ('AISIN', 'WPT-142', 'company')})
        self.assertIn('CATÁLOGO DE PRUEBA', self.pump.codes.get(code='GWT-142A').reference_source)
        self.assertEqual(PartCode.objects.get(code='AW9999').part, self.owner)  # owned elsewhere: reported, never moved
        self.assertFalse(self.filter.codes.exists())  # the description is an oil filter, not the catalog's water pump
        joint_codes = set(self.joint.codes.values_list('brand', 'code', 'ref_type'))
        self.assertIn(('ASVA', 'TY-LC200', 'company'), joint_codes)
        self.assertIn(('TOYOTA', '43460-69116', 'oem'), joint_codes)  # sister OEM only on a SKU already marked OEM
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
        for number in ('1610039466', '1610039465'):  # the second is carried by no SKU: its codes are kept all the same
            ref = OEMReference.objects.get(manufacturer='TOYOTA', code=number)
            self.assertEqual(sorted(ref.cross_references.values_list('brand', 'code')), [('AIRTEX', 'AW9999'), ('AISIN', 'WPT-142'), ('GMB', 'GWT-142A')])
        aisin = OEMCrossReference.objects.get(reference__code='1610039466', brand='AISIN')
        self.assertEqual(aisin.citations, ['CATÁLOGO DE PRUEBA (2016) · pág. 7'])
        self.assertEqual(self.links(self.pump, 'catalog'), ['1610039465', '1610039466'])
        self.assertEqual(PartOEMLink.objects.get(part=self.pump, reference__code='1610039466', source='catalog').citations,
                         ['CATÁLOGO DE PRUEBA (2016) · pág. 5 · GWT-142A'])
        self.assertEqual(self.links(self.pump, 'sku_name'), ['1610039466'])  # its own code names the number once the library holds it
        self.assertFalse(PartOEMLink.objects.filter(part=self.owner).exists())  # owning AW9999 as an alterno ties no SKU to the numbers
        gmb = result['catalogs'][0]['counts']
        self.assertEqual((gmb['cross_reference_new'], gmb['catalog_link_new']), (7, 2))
        self.assertEqual(result['name_links']['new'], 3)

    def test_duplicate_skus_of_one_item_get_no_alternos_but_both_reach_its_numbers(self):
        twin = Part.objects.create(sku='16100-39465-RAZ', description='BOMBA AGUA TOY HIACE')
        result = self.run_import(apply=True)
        self.assertEqual(result['catalogs'][0]['counts']['duplicate_part_items'], 1)
        self.assertFalse(PartCode.objects.filter(part__in=[self.pump, twin], brand='AISIN').exists())  # a code has one owner
        for part in (self.pump, twin):
            self.assertEqual(self.links(part, 'catalog'), ['1610039465', '1610039466'])

    def test_no_oem_alterno_on_a_sku_not_marked_oem_so_reconciliation_renames_nothing(self):
        self.run_import(apply=True)
        self.assertFalse(PartCode.objects.filter(part=self.pump, ref_type='oem').exists())
        skus = list(Part.objects.order_by('pk').values_list('sku', flat=True))
        reconcile_identities()
        self.assertEqual(list(Part.objects.order_by('pk').values_list('sku', flat=True)), skus)

    def test_reapplying_changes_nothing(self):
        self.run_import(apply=True)
        once = self.state()
        result = self.run_import(apply=True)
        self.assertEqual(self.state(), once)
        gmb = result['catalogs'][0]['counts']
        self.assertEqual((gmb.get('brand_code_new', 0), gmb.get('brand_code_present', 0)), (0, 1))

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
        self.assertFalse(retired.codes.exists())
        self.assertTrue(self.pump.codes.filter(brand='GMB', code='GWT-142A').exists())

    def test_a_sku_matching_two_items_of_one_catalog_is_left_alone(self):
        PartCode.objects.create(part=self.pump, code='16100-80007', brand='TOYOTA')
        result = self.run_import(apply=True)
        self.assertEqual(result['catalogs'][0]['counts']['ambiguous_parts'], 1)
        self.assertFalse(self.pump.codes.filter(brand='GMB').exists())

    def test_a_sku_classified_elsewhere_keeps_its_subgroup_and_gets_no_measurements(self):
        self.joint.category, self.joint.subcategory = 'TRANSMISIÓN', 'SEMIEJES'
        from .technical_data import bind_part_types
        bind_part_types([self.joint])
        self.joint.save()
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
        codes = lambda number: sorted(OEMCrossReference.objects.filter(reference__code=number).values_list('code', flat=True))
        self.assertEqual(codes('1610079445'), [])  # GMB prints it for two pumps: it cannot say which one replaces it
        self.assertEqual(codes('1610069185'), ['GWT-71A', 'WPT-071'])
        self.assertEqual(codes('1610009010'), ['GWT-116A', 'GWT-116AH', 'WPT-116'])  # variants of one pump count as one part
        self.assertFalse(PartOEMLink.objects.filter(part=pump, source='catalog').exists())  # matched only through the shared number
        self.assertEqual(list(PartOEMLink.objects.filter(part=pump).values_list('reference__code', 'source')), [('1610079445', 'sku_name')])
        counts = result['catalogs'][0]['counts']
        self.assertEqual((counts['shared_numbers'], counts['matches_through_shared_numbers']), (1, 2))
        self.assertEqual(result['catalogs'][0]['samples']['shared_number_matches'][0]['numbers'], ['1610079445'])


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
        ref = OEMReference.objects.get(manufacturer='HYUNDAI', code='495012S300')
        self.assertEqual(ref.part_type, 'SEMIEJES')  # Hyundai 49501 is a drive-shaft assembly number, listed for the joint
        self.assertTrue(ref.sources.get(kind='aftermarket_catalog').detail['assembly_number_listed_for_component'])
        self.assertFalse(ref.cross_references.exists())  # the joint's codes do not replace the whole drive shaft
        self.assertFalse(PartOEMLink.objects.filter(part=self.joint, source='catalog').exists())


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
        ref = OEMReference.objects.get(manufacturer='TOYOTA', code='4346060010')
        self.assertEqual(ref.sources.get(kind='aftermarket_catalog').detail['brand_codes'], [])
        self.assertFalse(part.codes.exists())


class BrakeDiscSheetTests(Fresh, TestCase):
    def test_a_disc_hat_diameter_is_kept_and_the_brand_short_number_is_not_a_measurement(self):
        disc = Part.objects.create(sku='43512-60150', description='DISCO FRENO DEL TOY LAND CRUISER', is_OEM=True)
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


class HubAndTensionerTests(Fresh, TestCase):
    def test_a_sku_takes_the_compatible_subgroup_its_own_description_names(self):
        hub = Part.objects.create(sku='44300-SDA-A51', description='HUB DEL HON ACCORD 03-07', is_OEM=True)
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
        self.assertTrue(bearing.codes.filter(brand='ASVA', code='TYBP-001').exists())
        self.assertFalse(locking.codes.exists())  # a free-wheel locking hub is not a wheel hub


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
        disc = Part.objects.create(sku='1K0615301AA', description='DISCO FRENO DEL VW GOLF JETTA', is_OEM=True)
        data = extract([item('24.0125-0111.1', 'brake_disc', oem=[('1K0615301AA', 'VOLKSWAGEN'), ('1K0615301AA', 'AUDI')], name_es='DISCO DE FRENO',
                             apps=[('VOLKSWAGEN', 'GOLF V'), ('SEAT', 'LEON')])], key='ate', title='ATE', brand='ATE', product_line='brake_disc')
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
        try:
            result = sci.run([handle.name], apply=True, stdout=io.StringIO())
        finally:
            os.unlink(handle.name)
        self.assertEqual(result['catalogs'][0]['counts'].get('linked_parts', 0), 1)
        self.assertTrue(disc.codes.filter(brand='ATE', code='24.0125-0111.1').exists())


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
            self.assertTrue(bearing.codes.filter(code='R177.27').exists())  # written from DAC40723336, which printed the SKU itself
            again = sci.run([handle.name], apply=True, stdout=io.StringIO())
        finally:
            os.unlink(handle.name)
        self.assertEqual(again['catalogs'][0]['counts']['linked_parts'], 1)  # R177.27 is not a key: DAC35620040 stays unlinked
        self.assertFalse(bearing.codes.filter(code='DAC35620040').exists())
        self.assertFalse(bearing.specifications.exists())
