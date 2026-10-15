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
from . import references
from .models import Part, PartCode, Reference, ReferenceSource
from .test_oem_finder import Fresh
from .test_supplier_catalog_import import extract, item
from .test_factories import make_code

CITE = 'CATÁLOGO DE PRUEBA (2016)'


def library(**filters):
    """The library's rows (S4: References of tipo oem with a brand)."""
    return Reference.objects.filter(references.library_q(), **filters)


def catalog_sources(**filters):
    """The library numbers' live catalog sources (S9's 3e also cites the catalog on the aftermarket codes it turns into References)."""
    return ReferenceSource.objects.filter(kind='catalogo', retirada=False, reference__tipo='oem', **filters)


class SupplierCatalogReplaceTests(Fresh, TestCase):
    """The old extract read a 4th-column code with the 3rd column's item (AIRTEX AW1, QHL QCP9 on GWT-1A) and filed a number under the
    wrong item (TOYOTA 16100-33333 on the Nissan pump GWN-2A); the corrected one does not. From S10 what the catalog filed is what its
    sources back: the library's numbers and the aftermarket References the merge pass's 3e made (a competitor's code only from a
    PDF-checked catalog, which this test catalog is not)."""

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
        self.copied = make_code(part=self.misled, brand='AISIN', code='WPN-002', ref_type='company', reference_source=f'{CITE} · pág. 7')
        self.kept = make_code(part=self.toyota, brand='AISIN', code='WPT 001', ref_type='company', reference_source=f'{CITE} · pág. 7')

    def write(self, data):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(data, handle, ensure_ascii=False)
        handle.close()
        return handle.name

    def codes(self):
        """The aftermarket References the catalog backs: (line, brand, code, its live citations of the catalog)."""
        rows = ReferenceSource.objects.filter(kind='catalogo', retirada=False, reference__tipo='aftermarket', citation__startswith=CITE)
        found = {}
        for sku, brand, code, citation in rows.values_list('reference__part__sku', 'reference__brand', 'reference__code', 'citation'):
            found.setdefault((sku, brand, code), []).append(citation)
        return sorted((*key, tuple(sorted(cited))) for key, cited in found.items())

    def state(self):
        return (self.codes(), sorted(library().values_list('brand', 'clave', 'part__estado', 'estado')),
                sorted(catalog_sources().values_list('reference__clave', 'detail')), PartCode.objects.count())

    def test_the_old_extract_imported_its_misreadings(self):
        self.assertEqual(library(clave='1610033333').get().part_id, self.misled.pk)  # S4: its codigo carries the number
        self.assertTrue(catalog_sources(reference__clave='1610033333').exists())  # filed under GWN-2A by the misreading
        self.assertEqual(self.codes(), [('16100-11111', 'GMB', 'GWT-1A', (f'{CITE} · pág. 5',)),  # 3e: the catalog's own code
                                        ('16100-11111-G', 'AISIN', 'WPT 001', (f'{CITE} · pág. 7',)),
                                        ('16100-33333', 'AISIN', 'WPN-002', (f'{CITE} · pág. 7',))])

    def test_dry_run_writes_nothing_and_lists_every_change(self):
        before = self.state()
        with tempfile.TemporaryDirectory() as directory:
            result = scr.run(self.new, report_dir=directory, stdout=io.StringIO())
            with open(os.path.join(directory, 'codes.csv'), encoding='utf-8') as handle:
                codes = list(csv.DictReader(handle))
            with open(os.path.join(directory, 'alternos-review.csv'), encoding='utf-8') as handle:
                review = list(csv.DictReader(handle))
        self.assertEqual(self.state(), before)
        # S4: 16100-33333 is one of the misled SKU's codes (its codigo carries it), so it stays, without the catalog's source. The
        # codes the catalog backs on SKU lines it no longer files there lose its citations (the References stay: a person's codes).
        self.assertEqual(result['changes'], {'numbers': {'detail': 1, 'drop_source': 1}, 'codes': {'retire': 2}})
        self.assertEqual(result['import'], {'oem_numbers': 2, 'oem_new': 0, 'cross_reference_rows': 5, 'catalog_links': 1})
        self.assertEqual(sorted((c['accion'], c['sku'], c['codigo']) for c in codes),
                         [('retire', '16100-11111-G', 'WPT 001'), ('retire', '16100-33333', 'WPN-002')])
        self.assertEqual([(r['sku'], r['codigo'], r['articulos_del_sku'], r['articulos_que_lo_imprimen']) for r in review],
                         [('16100-33333', 'WPN-002', '', 'GWN-2A')])

    def test_apply_leaves_what_the_corrected_extract_files_and_retires_the_rest(self):
        result = scr.run(self.new, apply=True, username='root', stdout=io.StringIO())
        self.assertEqual(result['alternos_to_review'], 1)
        self.assertEqual(self.codes(), [  # GWN-2A now prints the Nissan number alone: its numbers sit on one line, and 3e makes it its code
            ('16100-11111', 'GMB', 'GWT-1A', (f'{CITE} · pág. 5',)),
            ('21010-22222', 'GMB', 'GWN-2A', (f'{CITE} · pág. 5',)),
        ])
        misfiled = library(clave='1610033333').get()  # only the misreading printed it, but it is the misled SKU's own code
        self.assertEqual((misfiled.part_id, misfiled.sources.filter(retirada=False).count()), (self.misled.pk, 0))
        self.assertEqual(catalog_sources().get(reference__clave='1610011111').detail['pages'], [6])
        self.assertEqual(set(PartCode.objects.filter(is_main=False, tipo='aftermarket').exclude(detail__has_key='s9').values_list('pk', flat=True)),
                         {self.copied.pk, self.kept.pk})  # alternos are only reported (S9's 3e turns codes on one line into References)

        before = self.state()
        again = scr.run(self.new, apply=True, stdout=io.StringIO())
        self.assertEqual(again['changes'], {'numbers': {}, 'codes': {}})
        self.assertEqual(again['import']['oem_new'], 0)
        self.assertEqual(self.state(), before)

        sci.run([self.old], apply=True, stdout=io.StringIO())  # the old extract again: it files the misread number again
        misfiled = library(clave='1610033333').get()
        self.assertEqual((misfiled.estado, misfiled.sources.filter(retirada=False).count()), ('declarado', 1))


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
        self.assertEqual(result['import']['cross_reference_rows'], 1)


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
        self.assertEqual(library().count(), 2)

    def test_all_the_extracts_together_change_nothing_in_any_order(self):
        self.assertEqual(scr.run(list(reversed(self.paths)), stdout=io.StringIO())['changes'], {'numbers': {}, 'codes': {}})
        result = scr.run(self.paths, apply=True, stdout=io.StringIO())
        self.assertEqual(result['changes'], {'numbers': {}, 'codes': {}})
        self.assertEqual(sorted(catalog_sources().values_list('reference__clave', 'detail__catalog')),
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
        from .oem_reference import LibraryNumber
        return LibraryNumber(library().get(clave=code))

    def test_a_number_that_becomes_a_shaft_is_retyped_unless_its_text_came_from_elsewhere(self):
        self.assertEqual((self.ref('MR453384').description, self.ref('MR453384').part_type), ('JUNTA HOMOCINÉTICA EXTERIOR', 'JUNTAS HOMOCINÉTICAS'))
        shaft = Part.objects.create(sku='SEM-MIT-01', description='EJE COMPLETO MIT LANCER 05')
        references.add(shaft, 'MR453384', 'MITSUBISHI', ref_type='oem')  # S4: the numbers' lines in referencia are absorbed (rule 3f)
        references.add(shaft, 'MR980744', 'MITSUBISHI', ref_type='oem')
        number = library().get(clave='MR980744')
        Reference.objects.filter(pk=number.pk).update(detail={**number.detail, 'description': 'JUNTA EXT MIT MONTERO'})  # someone wrote it
        with tempfile.TemporaryDirectory() as directory:
            result = scr.run(self.path, report_dir=directory, stdout=io.StringIO())
            with open(os.path.join(directory, 'numbers.csv'), encoding='utf-8') as handle:
                rows = [row for row in csv.DictReader(handle) if row['accion'] == 'retype']
        # S9 (3f): the import put MT-99A50's two numbers on one line (both in referencia, joined by the item's own code), so the shaft
        # that absorbed MR453384's line took MB526438 too, and both are retyped.
        self.assertEqual(result['changes']['numbers'], {'detail': 3, 'retype': 3})
        self.assertEqual(sorted((row['numero'], row['detalle_despues']) for row in rows),
                         [('MB526438', '{"description": "SEMIEJE (EJE COMPLETO)", "part_type": "SEMIEJES"}'),
                          ('MR453384', '{"description": "SEMIEJE (EJE COMPLETO)", "part_type": "SEMIEJES"}'), ('MR980744', '{"part_type": "SEMIEJES"}')])
        scr.run(self.path, apply=True, username='root', stdout=io.StringIO())
        shaft_ref, kept = self.ref('MR453384'), self.ref('MR980744')
        self.assertEqual((shaft_ref.description, shaft_ref.part_type), ('SEMIEJE (EJE COMPLETO)', 'SEMIEJES'))
        self.assertTrue(next(row for row in shaft_ref.sources if row.kind == 'aftermarket_catalog').detail['assembly_number_listed_for_component'])
        self.assertIsNone(shaft_ref.updated_by)  # what the import writes: no one's edit
        self.assertEqual((kept.description, kept.part_type), ('JUNTA EXT MIT MONTERO', 'SEMIEJES'))
        self.assertEqual(scr.run(self.path, stdout=io.StringIO())['changes'], {'numbers': {}, 'codes': {}})


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
        imported = sci.run([handle.name], apply=True, stdout=io.StringIO())
        numbers = ['B00525400', 'G06425500', 'GA2A2560X']
        filed = dict(Reference.objects.filter(pk__in=[row[1] for row in imported['rows']]).values_list('pk', 'clave'))
        self.assertEqual(sorted(filed[row[1]] for row in imported['rows'] if row[3] == 'MZ-10'), numbers)  # the joint's code under each
        # What the earlier rules filed: each number typed as the shaft, flagged as listed for the joint.
        for number in library():
            Reference.objects.filter(pk=number.pk).update(detail={**number.detail, 'description': sci.ASSEMBLY_TEXT[0], 'part_type': sci.ASSEMBLY_TEXT[1]})
        for source in catalog_sources():
            source.detail = {**source.detail, 'assembly_number_listed_for_component': True}
            source.save(update_fields=['detail'])
        result = scr.run(handle.name, stdout=io.StringIO())
        self.assertEqual((result['changes']['numbers'], result['changes']['codes'], result['import']['cross_reference_rows']),
                         ({'detail': 3, 'retype': 3}, {}, 3))
        scr.run(handle.name, apply=True, stdout=io.StringIO())
        for ref in library():
            self.assertEqual((ref.detail['description'], ref.detail['part_type']), ('JUNTA HOMOCINÉTICA EXTERIOR', 'JUNTAS HOMOCINÉTICAS'), ref.clave)
            self.assertNotIn('assembly_number_listed_for_component', ref.sources.get(kind='catalogo').detail)
        self.assertEqual(scr.run(handle.name, stdout=io.StringIO())['changes'], {'numbers': {}, 'codes': {}})
