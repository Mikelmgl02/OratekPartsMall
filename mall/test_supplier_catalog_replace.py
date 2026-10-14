import copy
import csv
import io
import json
import os
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase

from . import supplier_catalog_import as sci
from . import supplier_catalog_replace as scr
from .models import Part, PartCode
from .oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, OEMReference, OEMReferenceSource, PartOEMLink
from .test_oem_finder import Fresh
from .test_supplier_catalog_import import extract, item

CITE = 'CATÁLOGO DE PRUEBA (2016)'


class SupplierCatalogReplaceTests(Fresh, TestCase):
    """The old extract read a 4th-column code with the 3rd column's item (AIRTEX AW1, QHL QCP9 on GWT-1A) and filed a number under the
    wrong item (TOYOTA 16100-33333 on the Nissan pump GWN-2A); the corrected one does not."""

    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_superuser('root', 'root@example.invalid', 'x')
        self.toyota = Part.objects.create(sku='16100-11111-G', description='BOMBA AGUA TOY HIACE 2KD')
        self.misled = Part.objects.create(sku='16100-33333', description='BOMBA AGUA TOY HIACE 2KD')  # matched only through the misfiled number
        old = extract([
            item('GWT-1A', 'water_pump', oem=[('16100-11111', 'TOYOTA')], cross_refs=[('AISIN', 'WPT-001'), ('AIRTEX', 'AW1'), ('QHL', 'QCP9')]),
            item('GWN-2A', 'water_pump', oem=[('21010-22222', 'NISSAN'), ('16100-33333', 'TOYOTA')], cross_refs=[('AISIN', 'WPN-002')]),
        ])
        new = copy.deepcopy(old)
        a, b = new['items']
        a['oem'][0]['page'] = 6
        a['cross_refs'] = [{'brand': 'AISIN', 'code': 'WPT-001', 'page': 8}]
        b['oem'] = b['oem'][:1]
        b['cross_refs'].append({'brand': 'AIRTEX', 'code': 'AW1', 'page': 7})
        self.old, self.new = self.write(old), self.write(new)
        self.addCleanup(os.unlink, self.old)
        self.addCleanup(os.unlink, self.new)
        sci.run([self.old], apply=True, stdout=io.StringIO())
        shared = OEMCrossReference.objects.get(reference__code='1610011111', number='AW1')
        shared.citations = [*shared.citations, 'OTRO CATÁLOGO · pág. 1']
        shared.save(update_fields=['citations'])
        self.copied = PartCode.objects.create(part=self.misled, brand='AISIN', code='WPN-002', ref_type='company', reference_source=f'{CITE} · pág. 7')
        self.kept = PartCode.objects.create(part=self.toyota, brand='AISIN', code='WPT 001', ref_type='company', reference_source=f'{CITE} · pág. 7')

    def write(self, data):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(data, handle, ensure_ascii=False)
        handle.close()
        return handle.name

    def codes(self):
        return sorted((row.reference.code, row.brand, row.number, tuple(row.citations)) for row in OEMCrossReference.objects.select_related('reference'))

    def state(self):
        return (self.codes(), sorted(OEMReference.objects.values_list('manufacturer', 'code')), sorted(OEMCrossReferenceRetired.objects.values_list('number', flat=True)),
                sorted(PartOEMLink.objects.values_list('part__sku', 'reference__code', 'source', 'citations')),
                sorted(OEMReferenceSource.objects.values_list('reference__code', 'detail')), PartCode.objects.count())

    def test_the_old_extract_imported_its_misreadings(self):
        self.assertIn(('1610033333', 'AISIN', 'WPN002', (f'{CITE} · pág. 7',)), self.codes())
        self.assertEqual(sorted(PartOEMLink.objects.filter(part=self.misled).values_list('reference__code', 'source')),
                         [('1610033333', 'catalog'), ('1610033333', 'sku_name')])

    def test_dry_run_writes_nothing_and_lists_every_change(self):
        before = self.state()
        with tempfile.TemporaryDirectory() as directory:
            result = scr.run(self.new, report_dir=directory, stdout=io.StringIO())
            with open(os.path.join(directory, 'codes.csv'), encoding='utf-8') as handle:
                codes = list(csv.DictReader(handle))
            with open(os.path.join(directory, 'alternos-review.csv'), encoding='utf-8') as handle:
                review = list(csv.DictReader(handle))
        self.assertEqual(self.state(), before)
        self.assertEqual(result['changes'], {'numbers': {'detail': 1, 'delete': 1}, 'codes': {'drop_citation': 1, 'recite': 1, 'retire': 1, 'with_number': 2},
                                             'ties': {'with_number': 1}})
        self.assertEqual(result['import']['cross_reference_new'], 1)  # AIRTEX AW1 under the Nissan number
        self.assertEqual(sorted((c['accion'], c['numero'], c['codigo']) for c in codes),
                         [('drop_citation', '1610011111', 'AW1'), ('recite', '1610011111', 'WPT-001'), ('retire', '1610011111', 'QCP9'),
                          ('with_number', '1610033333', 'GWN-2A'), ('with_number', '1610033333', 'WPN-002')])
        self.assertEqual([(r['sku'], r['codigo'], r['articulos_del_sku'], r['articulos_que_lo_imprimen']) for r in review],
                         [('16100-33333', 'WPN-002', '', 'GWN-2A')])

    def test_apply_leaves_what_the_corrected_extract_files_and_retires_the_rest(self):
        result = scr.run(self.new, apply=True, username='root', stdout=io.StringIO())
        self.assertEqual(result['alternos_to_review'], 1)
        self.assertEqual(self.codes(), [
            ('1610011111', 'AIRTEX', 'AW1', ('OTRO CATÁLOGO · pág. 1',)),  # another source still prints it
            ('1610011111', 'AISIN', 'WPT001', (f'{CITE} · pág. 8',)),
            ('1610011111', 'GMB', 'GWT1A', (f'{CITE} · pág. 5',)),
            ('2101022222', 'AIRTEX', 'AW1', (f'{CITE} · pág. 7',)),
            ('2101022222', 'AISIN', 'WPN002', (f'{CITE} · pág. 7',)),
            ('2101022222', 'GMB', 'GWN2A', (f'{CITE} · pág. 5',)),
        ])
        retired = OEMCrossReferenceRetired.objects.get()
        self.assertEqual((retired.reference.code, retired.brand, retired.number, retired.retired_by), ('1610011111', 'QHL', 'QCP9', self.root))
        self.assertFalse(OEMReference.objects.filter(code='1610033333').exists())  # only the misreading printed it
        self.assertFalse(PartOEMLink.objects.filter(part=self.misled).exists())
        self.assertEqual(list(PartOEMLink.objects.filter(source='catalog').values_list('part__sku', 'reference__code')), [('16100-11111-G', '1610011111')])
        self.assertEqual(OEMReferenceSource.objects.get(reference__code='1610011111').detail['pages'], [6])
        self.assertEqual(set(PartCode.objects.filter(is_main=False).values_list('pk', flat=True)), {self.copied.pk, self.kept.pk})  # alternos are only reported

        before = self.state()
        again = scr.run(self.new, apply=True, stdout=io.StringIO())
        self.assertEqual(again['changes'], {'numbers': {}, 'codes': {}, 'ties': {}})
        self.assertEqual((again['import']['oem_new'], again['import']['cross_reference_new'], again['import']['catalog_link_new']), (0, 0, 0))
        self.assertEqual(self.state(), before)

        sci.run([self.old], apply=True, stdout=io.StringIO())  # the old extract again: the retired code stays retired
        self.assertNotIn('QCP9', [number for _, _, number, _ in self.codes()])


class OwnCodeReplaceTests(Fresh, TestCase):
    def test_a_replace_keeps_the_own_codes_without_a_digit_it_files(self):
        data = extract([item('FDWH-FOCMF', 'wheel_hub', oem=[('1223640', 'FORD')], name_es='CUBO DE RUEDA')],
                       key='asva_hub', title='ASVA HUB', brand='ASVA', product_line='wheel_hub')
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(data, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        sci.run([handle.name], apply=True, stdout=io.StringIO())
        result = scr.run(handle.name, stdout=io.StringIO())
        self.assertEqual(result['changes']['codes'], {})  # neither retired nor re-cited
        self.assertEqual(result['import']['cross_reference_present'], 1)


class SeveralExtractsReplaceTests(Fresh, TestCase):
    """ASVA CV was imported from two extracts under one citation: its items, and the OEM numbers printed with no ASVA part."""

    def setUp(self):
        super().setUp()
        joint = item('TY-LC200', 'cv_joint_outer', oem=[('43460-69115', 'TOYOTA')], name_es='JUNTA HOMOCINÉTICA EXTERIOR')
        lone = item('', 'cv_joint_outer', oem=[('43460-60010', 'TOYOTA'), ('43460-69115', 'TOYOTA')], name_es='JUNTA HOMOCINÉTICA EXTERIOR')
        self.paths = []
        for data in (extract([joint], key='cv', title='ASVA CV', brand='ASVA', product_line='cv_joint'),
                     extract([lone], key='cv_oem_only', title='ASVA CV', brand='ASVA', product_line='cv_joint')):
            self.paths.append(self.write(data))
        for path in self.paths:
            sci.run([path], apply=True, stdout=io.StringIO())

    def write(self, data):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(data, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_one_extract_alone_is_refused(self):
        for apply in (False, True):
            out = io.StringIO()
            self.assertIsNone(scr.run(self.paths[0], apply=apply, stdout=out))  # it would delete TOYOTA 4346060010
            self.assertIn('también se importó de cv_oem_only', out.getvalue())
        self.assertEqual(OEMReference.objects.count(), 2)

    def test_all_the_extracts_together_change_nothing_in_any_order(self):
        self.assertEqual(scr.run(list(reversed(self.paths)), stdout=io.StringIO())['changes'], {'numbers': {}, 'codes': {}, 'ties': {}})
        result = scr.run(self.paths, apply=True, stdout=io.StringIO())
        self.assertEqual(result['changes'], {'numbers': {}, 'codes': {}, 'ties': {}})
        self.assertEqual(sorted(OEMReferenceSource.objects.values_list('reference__code', 'detail__catalog')),
                         [('4346060010', 'cv_oem_only'), ('4346069115', 'cv')])  # each number keeps the extract that printed it first

    def test_extracts_of_different_catalogs_are_refused(self):
        other = self.write(extract([], key='gmb', title='GMB', brand='GMB'))
        out = io.StringIO()
        self.assertIsNone(scr.run([self.paths[0], other], stdout=out))
        self.assertIn('mismo catálogo', out.getvalue())
        out = io.StringIO()
        with self.assertRaises(SystemExit) as stop:
            scr.main(['--dry-run', self.paths[0], other], stdout=out)
        self.assertEqual(stop.exception.code, 1)
        self.assertIn('No se cambia nada', out.getvalue())


class ShaftRetypeReplaceTests(Fresh, TestCase):
    """A number a corrected rule makes a drive-shaft number listed for a joint is typed as the shaft, as a fresh import types it."""

    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_superuser('root', 'root@example.invalid', 'x')
        data = extract([item('MT-99A50', 'cv_joint_outer', oem=[('MR453384', 'MITSUBISHI'), ('MB526438', 'MITSUBISHI')], name_es='JUNTA HOMOCINÉTICA EXTERIOR'),
                        item('MT-41', 'cv_joint_outer', oem=[('MR980744', 'MITSUBISHI')], name_es='JUNTA HOMOCINÉTICA EXTERIOR')],
                       key='asva_cv', title='ASVA CV', brand='ASVA', product_line='cv_joint')
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(data, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        self.path = handle.name
        sci.run([self.path], apply=True, stdout=io.StringIO())

    def ref(self, code):
        return OEMReference.objects.get(code=code)

    def test_a_number_that_becomes_a_shaft_is_retyped_unless_its_text_came_from_elsewhere(self):
        self.assertEqual((self.ref('MR453384').description, self.ref('MR453384').part_type), ('JUNTA HOMOCINÉTICA EXTERIOR', 'JUNTAS HOMOCINÉTICAS'))
        shaft = Part.objects.create(sku='SEM-MIT-01', description='EJE COMPLETO MIT LANCER 05')
        PartCode.objects.create(part=shaft, brand='MITSUBISHI', code='MR453384', ref_type='oem')
        PartCode.objects.create(part=shaft, brand='MITSUBISHI', code='MR980744', ref_type='oem')
        OEMReference.objects.filter(code='MR980744').update(description='JUNTA EXT MIT MONTERO')  # someone wrote its description
        with tempfile.TemporaryDirectory() as directory:
            result = scr.run(self.path, report_dir=directory, stdout=io.StringIO())
            with open(os.path.join(directory, 'numbers.csv'), encoding='utf-8') as handle:
                rows = [row for row in csv.DictReader(handle) if row['accion'] == 'retype']
        self.assertEqual(result['changes']['numbers'], {'detail': 2, 'retype': 2})
        self.assertEqual(sorted((row['numero'], row['detalle_despues']) for row in rows),
                         [('MR453384', '{"description": "SEMIEJE (EJE COMPLETO)", "part_type": "SEMIEJES"}'), ('MR980744', '{"part_type": "SEMIEJES"}')])
        scr.run(self.path, apply=True, username='root', stdout=io.StringIO())
        shaft_ref, kept = self.ref('MR453384'), self.ref('MR980744')
        self.assertEqual((shaft_ref.description, shaft_ref.part_type), ('SEMIEJE (EJE COMPLETO)', 'SEMIEJES'))
        self.assertTrue(shaft_ref.sources.get(kind='aftermarket_catalog').detail['assembly_number_listed_for_component'])
        self.assertIsNone(shaft_ref.updated_by_id)  # what the import writes: no one's edit
        self.assertEqual((kept.description, kept.part_type), ('JUNTA EXT MIT MONTERO', 'SEMIEJES'))
        self.assertFalse(OEMCrossReference.objects.filter(reference__code__in=['MR453384', 'MR980744']).exists())
        self.assertEqual(scr.run(self.path, stdout=io.StringIO())['changes'], {'numbers': {}, 'codes': {}, 'ties': {}})


class MazdaJointReplaceTests(Fresh, TestCase):
    """The owner, 2026-10-14: Mazda's 25-40X/25-50X/25-60X name the CV joint, under Mazda, Ford or Kia. A number the earlier rules
    filed as a drive shaft listed for the joint takes the joint's type and code back."""

    def test_a_mazda_joint_number_filed_as_a_shaft_goes_back_to_the_joint(self):
        data = extract([item('MZ-10', 'cv_joint_outer', oem=[('G064-25-500', 'FORD'), ('GA2A-25-60X', 'MAZDA'), ('B005-25-400', 'KIA')],
                             name_es='JUNTA HOMOCINÉTICA EXTERIOR')], key='asva_cv', title='ASVA CV', brand='ASVA', product_line='cv_joint')
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(data, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        sci.run([handle.name], apply=True, stdout=io.StringIO())
        numbers = ['B00525400', 'G06425500', 'GA2A2560X']
        self.assertEqual(sorted(OEMCrossReference.objects.filter(code='MZ-10').values_list('reference__code', flat=True)), numbers)
        # What the earlier rules filed: each number typed as the shaft, flagged as listed for the joint, without the joint's code.
        OEMCrossReference.objects.all().delete()
        OEMReference.objects.update(description=sci.ASSEMBLY_TEXT[0], part_type=sci.ASSEMBLY_TEXT[1])
        for source in OEMReferenceSource.objects.all():
            source.detail = {**source.detail, 'assembly_number_listed_for_component': True}
            source.save(update_fields=['detail'])
        result = scr.run(handle.name, stdout=io.StringIO())
        self.assertEqual((result['changes']['numbers'], result['changes']['codes'], result['import']['cross_reference_new']),
                         ({'detail': 3, 'retype': 3}, {}, 3))
        scr.run(handle.name, apply=True, stdout=io.StringIO())
        for ref in OEMReference.objects.all():
            self.assertEqual((ref.description, ref.part_type), ('JUNTA HOMOCINÉTICA EXTERIOR', 'JUNTAS HOMOCINÉTICAS'), ref.code)
            self.assertNotIn('assembly_number_listed_for_component', ref.sources.get().detail)
        self.assertEqual(sorted(OEMCrossReference.objects.filter(code='MZ-10').values_list('reference__code', flat=True)), numbers)
        self.assertEqual(scr.run(handle.name, stdout=io.StringIO())['changes'], {'numbers': {}, 'codes': {}, 'ties': {}})
