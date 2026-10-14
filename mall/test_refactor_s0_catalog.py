"""S0.10 tools (target data step 1): the own-code audit and the number twins."""
import io
import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings

from . import supplier_catalog_replace as scr
from .models import Part, PartCode
from .oem_reference import upsert_reference
from .oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, OEMReference, OEMReferenceSource, PartOEMLink
from .refactor import s0_normalize_numbers as s0n, s0_own_codes
from .supplier_catalog_import import canonical_number, run as import_catalog
from .test_oem_finder import Fresh
from .test_supplier_catalog_import import extract, item


def write(data):
    handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
    json.dump(data, handle, ensure_ascii=False)
    handle.close()
    return handle.name


class OwnCodeAuditTests(Fresh, TestCase):
    def test_each_item_says_whether_its_code_is_filed_would_be_or_why_not(self):
        joint = lambda code, oem: item(code, 'cv_joint_outer', oem=oem, name_es='JUNTA HOMOCINÉTICA EXTERIOR')
        data = extract([joint('TY-01', [('43460-69115', 'TOYOTA')]),
                        joint('TY-02', [('43460-69125', 'TOYOTA'), ('43460-69135', 'TOYOTA')]), joint('TY-03', [('43460-69135', 'TOYOTA')]),
                        joint('TY-04', [('43410-60010', 'TOYOTA')]),  # a drive-shaft number
                        joint('TY-05', [('43460-69145', 'TOYOTA')]), joint('X', [('43460-69155', 'TOYOTA')])],
                       key='asva_cv', title='ASVA CV', brand='ASVA', product_line='cv_joint')
        path = write(data)
        self.addCleanup(os.unlink, path)
        import_catalog([path], apply=True, stdout=io.StringIO())
        OEMCrossReference.objects.filter(code='TY-05').delete()
        OEMCrossReference.objects.filter(code='TY-02').delete()
        ref = OEMReference.objects.get(code='4346069145')
        OEMCrossReferenceRetired.objects.create(reference=ref, brand='ASVA', number='TY05', code='TY-05')
        with tempfile.TemporaryDirectory() as directory:
            result = s0_own_codes.run([path], report_path=os.path.join(directory, 'own.json'), stdout=io.StringIO())
            with open(os.path.join(directory, 'own.json'), encoding='utf-8') as handle:
                self.assertEqual(json.load(handle), result)
        counts = result['catalogs'][0]['counts']
        self.assertEqual(counts, {'filed': 1, 'refile': 1, 'retire': 0, 'shared': 1, 'assembly': 1, 'no_key': 1, 'retired': 1})
        self.assertEqual(result['catalogs'][0]['samples'], {'refile': ['TY-02'], 'shared': ['TY-03'], 'assembly': ['TY-04'], 'no_key': ['X'],
                                                            'retired': ['TY-05']})

    def test_a_code_filed_only_under_numbers_that_no_longer_take_it_is_to_retire_and_a_twin_counts_as_its_number(self):
        joint = lambda code, oem: item(code, 'cv_joint_outer', oem=oem, name_es='JUNTA HOMOCINÉTICA EXTERIOR')
        data = extract([joint('MT-08', [('MR222052', 'MITSUBISHI')]), joint('DH-01', [('43460-87Z01-000', 'DAIHATSU')])],
                       key='asva_cv', title='ASVA CV', brand='ASVA', product_line='cv_joint')
        path = write(data)
        self.addCleanup(os.unlink, path)
        import_catalog([path], apply=True, stdout=io.StringIO())
        shaft = Part.objects.create(sku='SEM-MIT-01', description='EJE COMPLETO MIT LANCER')  # MR222052 turns out to be a shaft's
        PartCode.objects.create(part=shaft, brand='MITSUBISHI', code='MR222052', ref_type='oem')
        OEMReference.objects.filter(code='4346087Z01000').update(code='4346087Z01')  # the library keeps the other spelling
        counts = s0_own_codes.run([path], stdout=io.StringIO())['catalogs'][0]['counts']
        self.assertEqual((counts['retire'], counts['filed']), (1, 1))  # MT-08 goes with the replace; DH-01 is filed under the twin


GMB = 'GMB Water Pump Catalog 2016'
ASVA = 'ASVA Tensioner & Idler Bearings Catalogue 2017-2018'


def number(manufacturer, code, citation=GMB, codes=(), **detail):
    ref = upsert_reference(manufacturer, code, printed=[code], source_kind='aftermarket_catalog', citation=citation, detail=detail,
                           defaults={'description': 'BOMBA DE AGUA', 'part_type': 'BOMBAS DE AGUA'})[0]
    for brand, aftermarket, cites in codes:
        OEMCrossReference.objects.create(reference=ref, brand=brand, code=aftermarket, citations=cites)
    return ref


class TwinNumberTests(Fresh, TestCase):
    """The pairs of target data step 1: Daihatsu's -000 and Opel's leading zero."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pump = Part.objects.create(sku='16100-59145-GMB', description='BOMBA AGUA DAI TERIOS')
        self.daihatsu = number('DAIHATSU', '16100-59145', codes=[('GMB', 'GWD-55A', [f'{GMB} · pág. 40'])], brand_codes=['GWD-55A'], pages=[40],
                               catalog='gmb_wp_2016')
        self.zeros = number('DAIHATSU', '16100-59145-000', codes=[('GMB', 'GWD-55A', [f'{GMB} · pág. 41']), ('AISIN', 'WPD-001', [f'{GMB} · pág. 550'])],
                            brand_codes=['GWD-55A', 'GWD-55AH'], pages=[41, 550], catalog='gmb_wp_2016')
        PartOEMLink.objects.create(part=self.pump, reference=self.daihatsu, source='catalog', citations=[f'{GMB} · pág. 40 · GWD-55A'])
        PartOEMLink.objects.create(part=self.pump, reference=self.zeros, source='catalog', citations=[f'{GMB} · pág. 41 · GWD-55A'])
        self.lone = number('DAIHATSU', '16100-28030-000')  # no twin: stays as printed
        self.opel = number('OPEL', '06 36 160', citation=ASVA)
        self.opel_short = number('OPEL', '636160', citation=ASVA, codes=[('INA', '532 0418 10', [f'{ASVA} · pág. 300'])])
        self.vauxhall = number('VAUXHALL', '636160', citation=ASVA)
        self.extra = number('OPEL', '02898355')
        self.plain = number('OPEL', '2898355', citation='OTRO CATÁLOGO')

    def state(self):
        return (sorted(OEMReference.objects.values_list('manufacturer', 'code', 'version')), sorted(OEMReferenceSource.objects.values_list('reference__manufacturer', 'reference__code', 'citation', 'detail'), key=str),
                sorted(OEMCrossReference.objects.values_list('reference__manufacturer', 'reference__code', 'brand', 'number', 'citations')),
                sorted(PartOEMLink.objects.values_list('reference__code', 'source', 'citations')))

    def test_canonical_spellings(self):
        self.assertEqual(canonical_number('DAIHATSU', '16100-59145-000'), '1610059145')
        self.assertEqual(canonical_number('SUZUKI', '17400-60000'), '1740060000')  # ten characters: no suffix to drop
        self.assertEqual(canonical_number('NISSAN', '21010-ED000-000'), '21010ED000000')  # only Daihatsu, Suzuki and Toyota
        self.assertEqual((canonical_number('VAUXHALL', '636160'), canonical_number('OPEL', '02898355'), canonical_number('GM', '09128451')),
                         ('0636160', '2898355', '9128451'))
        self.assertEqual(canonical_number('CHEVROLET', '963522'), '963522')  # only Opel catalog numbers take the zero back

    def test_a_dry_run_lists_the_groups_and_writes_nothing(self):
        before = self.state()
        result = s0n.run(stdout=io.StringIO())
        self.assertEqual(self.state(), before)
        self.assertEqual(result['groups'], 3)
        actions = sorted((a['action'], a['manufacturer'], a['code']) for g in result['plan'] for a in g['actions'])
        self.assertEqual(actions, [('merge', 'DAIHATSU', '1610059145000'), ('merge', 'OPEL', '02898355'), ('merge', 'OPEL', '636160'),
                                   ('rename', 'VAUXHALL', '636160')])
        self.assertEqual({k: result['counts'][k] for k in ('merged', 'renamed', 'sources', 'sources_joined', 'codes', 'codes_joined', 'links', 'links_joined')},
                         {'merged': 3, 'renamed': 1, 'sources': 3, 'sources_joined': 2, 'codes': 3, 'codes_joined': 1, 'links': 1, 'links_joined': 1})

    def test_apply_absorbs_each_twin_in_place_and_renames_a_lone_spelling(self):
        get_user_model().objects.create_user('miguel', 'miguel@example.invalid', 'x')
        result = s0n.run(apply=True, username='miguel', manifest_dir=os.path.join(self.tmp, 'normalize_numbers'), stdout=io.StringIO())
        with open(os.path.join(self.tmp, 'normalize_numbers', 'manifest.jsonl'), encoding='utf-8') as handle:
            manifest = [json.loads(line) for line in handle]
        self.assertEqual((manifest[0]['user'], manifest[0]['groups'], result['rows_logged']), ('miguel', 3, len(manifest) - 1))
        self.assertIn({'table': 'mall_oemreference', 'action': 'delete', 'id': str(self.zeros.pk)},
                      [{k: row.get(k) for k in ('table', 'action', 'id')} for row in manifest])
        self.assertFalse(OEMReference.objects.filter(pk__in=[self.zeros.pk, self.opel_short.pk, self.extra.pk]).exists())
        self.assertEqual(sorted(OEMCrossReference.objects.filter(reference=self.daihatsu).values_list('number', 'citations')),
                         [('GWD55A', [f'{GMB} · pág. 40', f'{GMB} · pág. 41']), ('WPD001', [f'{GMB} · pág. 550'])])
        self.assertEqual(PartOEMLink.objects.get(part=self.pump).citations, [f'{GMB} · pág. 40 · GWD-55A', f'{GMB} · pág. 41 · GWD-55A'])
        source = self.daihatsu.sources.get()
        self.assertEqual([e['code'] for e in source.detail['absorbed_numbers']], ['1610059145000'])
        # the joined source lists what both spellings' pages printed, as a re-import files it under the kept number
        self.assertEqual((source.detail['pages'], source.detail['brand_codes'], source.detail['catalog']), ([40, 41, 550], ['GWD-55A', 'GWD-55AH'], 'gmb_wp_2016'))
        logged = {(row['table'], row['action'], row['id']): row for row in manifest[1:]}
        moved = logged[('mall_oemreferencesource', 'update', self.extra.sources.model.objects.get(reference=self.plain, citation=GMB).pk)]
        self.assertEqual(moved['before']['reference_id'], str(self.extra.pk))  # a moved row records where it was
        self.assertEqual(logged[('mall_oemreference', 'update', str(self.vauxhall.pk))]['before']['code'], '636160')
        self.assertEqual(manifest[0]['database'], connection.settings_dict['NAME'])
        self.daihatsu.refresh_from_db()
        self.assertEqual(self.daihatsu.version, 2)  # the merge takes the next version
        self.assertEqual(sorted(self.plain.sources.values_list('citation', flat=True)), [GMB, 'OTRO CATÁLOGO'])  # moved, not joined
        self.assertTrue(OEMCrossReference.objects.filter(reference=self.opel, number='532041810').exists())
        self.vauxhall.refresh_from_db()
        self.assertEqual(self.vauxhall.code, '0636160')  # the same row, now spelled like its Opel twin
        self.assertTrue(OEMReference.objects.filter(pk=self.lone.pk).exists())
        once = self.state()
        again = s0n.run(apply=True, manifest_dir=os.path.join(self.tmp, 'again'), stdout=io.StringIO())
        self.assertEqual((again['groups'], self.state()), (0, once))

    def test_a_twin_that_needs_a_person_is_left_and_listed(self):
        PartCode.objects.create(part=self.pump, brand='DAIHATSU', code='16100-59145-000', ref_type='oem')  # an OEM alterno files under it
        self.opel_short.status, self.opel_short.dispute_note = 'disputed', 'otra pieza'
        self.opel_short.save()
        OEMCrossReferenceRetired.objects.create(reference=self.plain, brand='GMB', number='GWO1A', code='GWO-1A')
        OEMCrossReference.objects.create(reference=self.extra, brand='GMB', code='GWO-1A', citations=[GMB])
        result = s0n.run(apply=True, manifest_dir=os.path.join(self.tmp, 'refused'), stdout=io.StringIO())
        refused = {(a['manufacturer'], a['code']): a['refused'] for g in result['plan'] for a in g['actions'] if a['refused']}
        self.assertEqual(refused, {('DAIHATSU', '1610059145000'): 'alterno', ('OPEL', '636160'): 'curated', ('OPEL', '02898355'): 'retired_conflict'})
        self.assertTrue(OEMReference.objects.filter(pk__in=[self.zeros.pk, self.opel_short.pk, self.extra.pk]).count() == 3)
        self.vauxhall.refresh_from_db()
        self.assertEqual(self.vauxhall.code, '0636160')

    def test_a_twin_merged_into_a_keeper_that_could_not_be_renamed_or_chaining_a_supersession_is_left(self):
        doubled = number('VAUXHALL', '00636160', citation=ASVA)  # VAUXHALL holds two other spellings: 00636160 is renamed, 636160 joins it
        PartCode.objects.create(part=self.pump, brand='VAUXHALL', code='00636160', ref_type='oem')  # ...but an alterno files under it
        self.extra.superseded_by = number('OPEL', '9128451')
        self.extra.save()
        result = s0n.run(apply=True, manifest_dir=os.path.join(self.tmp, 'refused'), stdout=io.StringIO())
        refused = {(a['manufacturer'], a['code']): a['refused'] for g in result['plan'] for a in g['actions'] if a['refused']}
        self.assertEqual(refused, {('VAUXHALL', '00636160'): 'alterno', ('VAUXHALL', '636160'): 'keeper', ('OPEL', '02898355'): 'supersession'})
        self.assertTrue(OEMReference.objects.filter(pk__in=[doubled.pk, self.vauxhall.pk, self.extra.pk]).count() == 3)

    def test_on_the_live_database_an_apply_needs_the_s0_11_guard_and_the_manifest_folder(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL only: the live database is the PostgreSQL one')
        before, frozen = self.state(), []
        guard = SimpleNamespace(refusal=lambda applying=True, alias='default': None if frozen else 'Base de datos en vivo sin el congelamiento del catálogo')
        with override_settings(LIVE_DATABASE_NAME=connection.settings_dict['NAME']):
            with mock.patch.dict(sys.modules, {'mall.refactor.guard': None}):  # before S0.11 lands the live database is refused outright
                out = io.StringIO()
                self.assertIsNone(s0n.run(apply=True, manifest_dir=os.path.join(self.tmp, 'm'), stdout=out))
                self.assertIn('integra S0.11 primero', out.getvalue())
                self.assertEqual(s0n.run(stdout=io.StringIO())['groups'], 3)  # a dry run never needs it
            with mock.patch.dict(sys.modules, {'mall.refactor.guard': guard}):  # then its guard decides
                out = io.StringIO()
                self.assertIsNone(s0n.run(apply=True, manifest_dir=os.path.join(self.tmp, 'm'), stdout=out))
                self.assertIn('sin el congelamiento del catálogo', out.getvalue())
                frozen.append(True)
                self.assertIsNone(s0n.run(apply=True, manifest_dir=os.path.join(self.tmp, 'missing', 'm'), stdout=io.StringIO()))
                self.assertEqual(self.state(), before)
                self.assertEqual(s0n.run(apply=True, manifest_dir=os.path.join(self.tmp, 'm'), stdout=io.StringIO())['counts']['merged'], 3)

    def test_a_manifest_that_cannot_be_written_changes_nothing(self):
        before = self.state()
        with mock.patch.object(s0n.os, 'fsync', side_effect=OSError(28, 'No space left on device')):
            out = io.StringIO()
            self.assertIsNone(s0n.run(apply=True, manifest_dir=os.path.join(self.tmp, 'full'), stdout=out))
        self.assertIn('No se pudo escribir el registro de cambios', out.getvalue())
        self.assertEqual(self.state(), before)
        self.assertEqual(os.listdir(os.path.join(self.tmp, 'full')), [])  # no half-written manifest or temporary copy

    def test_after_the_merge_the_import_files_a_printed_twin_under_the_kept_number(self):
        data = extract([item('GWD-55A', 'water_pump', oem=[('16100-59145-000', 'DAIHATSU'), ('16100-59145', 'DAIHATSU')], cross_refs=[('AISIN', 'WPD-001')])],
                       title='GMB Water Pump Catalog', brand='GMB')
        path = write(data)
        self.addCleanup(os.unlink, path)
        before = import_catalog([path], stdout=io.StringIO())['catalogs'][0]['counts']
        self.assertEqual(before['oem_numbers'], 2)  # both spellings are in the library: each is filed as printed
        s0n.run(apply=True, manifest_dir=os.path.join(self.tmp, 'import'), stdout=io.StringIO())
        after = import_catalog([path], apply=True, stdout=io.StringIO())['catalogs'][0]['counts']
        self.assertEqual((after['oem_numbers'], after['oem_new']), (1, 0))
        self.assertFalse(OEMReference.objects.filter(code='1610059145000').exists())

    def test_after_a_gm_rename_an_import_files_the_printed_spelling_under_the_make_s_canonical_one(self):
        data = extract([item('VKBA-1', 'tensioner', oem=[('636160', 'VAUXHALL'), ('16100-28030', 'DAIHATSU')], name_es='TENSOR')],
                       title='ASVA Tensioner & Idler Bearings Catalogue', brand='ASVA', product_line='tensioner')
        path = write(data)
        self.addCleanup(os.unlink, path)
        s0n.run(apply=True, manifest_dir=os.path.join(self.tmp, 'gm'), stdout=io.StringIO())
        self.vauxhall.delete()  # only OPEL holds 0636160 now
        import_catalog([path], apply=True, stdout=io.StringIO())
        self.assertEqual(sorted(OEMReference.objects.filter(manufacturer='VAUXHALL').values_list('code', flat=True)), ['0636160'])
        # a lone 16100-28030-000 takes the printed 16100-28030: no pair comes back
        self.assertEqual(sorted(OEMReference.objects.filter(code__startswith='1610028030').values_list('code', flat=True)), ['1610028030000'])
        self.assertEqual(s0n.run(stdout=io.StringIO())['groups'], 0)

    def test_after_the_merge_a_replace_of_the_catalog_that_printed_both_spellings_changes_nothing(self):
        Part.objects.create(sku='17400-77E00', description='BOMBA AGUA SUZ VITARA')
        data = extract([item('GWS-10A', 'water_pump', oem=[('17400-77E00', 'SUZUKI')], cross_refs=[('AISIN', 'WPS-010')]),
                        item('GWS-10AH', 'water_pump', oem=[('17400-77E00-000', 'SUZUKI')], cross_refs=[('AISIN', 'WPS-011')])],
                       title='GMB Water Pump Catalog', brand='GMB')
        data['items'][1]['oem'][0]['page'] = 6
        path = write(data)
        self.addCleanup(os.unlink, path)
        import_catalog([path], apply=True, stdout=io.StringIO())
        self.assertEqual(OEMReference.objects.filter(code__startswith='1740077E00').count(), 2)
        s0n.run(apply=True, manifest_dir=os.path.join(self.tmp, 'replace'), stdout=io.StringIO())
        kept = OEMReference.objects.get(code__startswith='1740077E00')
        self.assertEqual((kept.code, kept.sources.get().detail['pages'], kept.sources.get().detail['brand_codes']), ('1740077E00', [5, 6], ['GWS-10A', 'GWS-10AH']))
        self.assertEqual(scr.run(path, stdout=io.StringIO())['changes'], {'numbers': {}, 'codes': {}, 'ties': {}})

    def test_one_spelling_under_several_makes_is_no_twin(self):
        number('OPEL', '569000', citation=ASVA)
        number('VAUXHALL', '569000', citation=ASVA)
        result = s0n.run(stdout=io.StringIO())
        self.assertNotIn('GM 0569000', [g['group'] for g in result['plan']])
