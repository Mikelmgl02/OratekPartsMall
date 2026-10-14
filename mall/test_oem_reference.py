from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from . import oem_apply as oa
from . import oem_reference as orf
from . import references
from .models import Part, PartCode, Reference, User
from .oem_reference import (classify_number, compact, derived_status, linked_parts, refresh_status, source_of,
                            upsert_reference)
from .oem_reference_models import OEMReference, OEMReferenceSource
from .test_oem_apply import FLAG, RENAME, Catalog
from .test_factories import historical_apps, migration_module, run_migration_function
from .test_oem_finder import Fresh

URL = '/api/v1/management/oem-references/'
ALGO = 'algo:oem-finder:v2:current:2'


def oem(part, code, brand='TOYOTA', source=ALGO, ref_type='oem'):
    return PartCode.objects.create(part=part, code=code, brand=brand, ref_type=ref_type, reference_source=source)


def state():
    """The whole library as comparable data: every reference with its sources."""
    return sorted((r.manufacturer, r.code, tuple(r.printed_forms), r.system, r.family, r.part_type, r.description, r.status,
                   tuple(sorted((s.kind, s.citation, repr(sorted(s.detail.items()))) for s in r.sources.all())))
                  for r in OEMReference.objects.prefetch_related('sources'))


class CompactAndModelTests(Fresh, TestCase):
    def test_compact_keeps_uppercase_letters_and_digits_only(self):
        for written, stored in (('17801-30070', '1780130070'), (' mr-968365 ', 'MR968365'), ('19200-PNA-003', '19200PNA003'),
                                ('17801 30070/a.1_', '1780130070A1'), ('(90385) T0002+', '90385T0002'), ('', ''), (None, ''), ('ñ-°', '')):
            self.assertEqual(compact(written), stored, written)

    def test_save_normalizes_and_takes_the_next_version(self):
        ref = OEMReference.objects.create(manufacturer='  toyota  motor ', code='17801-30070',
                                          printed_forms=['17801-30070', '1780130070', '17801 30070', 'OTRO-123', '17801-30070'])
        self.assertEqual((ref.manufacturer, ref.code, ref.printed_forms, ref.version), ('TOYOTA MOTOR', '1780130070', ['17801-30070', '17801 30070'], 1))
        ref.notes = 'REVISAR'
        ref.save(update_fields=['notes'])
        ref.refresh_from_db()
        self.assertEqual((ref.version, ref.notes), (2, 'REVISAR'))

    def test_constraints(self):
        ref = OEMReference.objects.create(manufacturer='TOYOTA', code='17801-30070')
        source = OEMReferenceSource.objects.create(reference=ref, kind='manual', citation='LISTA 2026')
        for update in ({'code': '17801-30070'}, {'code': '1780130070a'}, {'code': ''}, {'manufacturer': 'toyota'}, {'manufacturer': ''},
                       {'status': 'disputed'}, {'status': 'unknown'}, {'superseded_by': F('id')}):
            with self.assertRaises(IntegrityError, msg=update), transaction.atomic():
                OEMReference.objects.filter(pk=ref.pk).update(**update)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OEMReference.objects.create(manufacturer='Toyota', code='1780130070')  # unique once compacted
        with self.assertRaises(IntegrityError), transaction.atomic():
            OEMReferenceSource.objects.create(reference=ref, kind='manual', citation='LISTA 2026')
        with self.assertRaises(IntegrityError), transaction.atomic():
            OEMReferenceSource.objects.filter(pk=source.pk).update(kind='rumor')
        OEMReference.objects.filter(pk=ref.pk).update(status='disputed', dispute_note='NO ES FILTRO')
        self.assertTrue(OEMReference.objects.create(manufacturer='LEXUS', code='1780130070').pk)  # another manufacturer, another fact


class RuleTests(Fresh, SimpleTestCase):
    def test_status_follows_the_strongest_source(self):
        for sources, status in (([('price_list', {})], 'verified'), ([('manufacturer_catalog', {})], 'verified'),
                                ([('manual', {'verified': True})], 'verified'), ([('manual', {'verified': False})], 'declared'),
                                ([('ai_lookup', {'approved': True})], 'verified'), ([('ai_lookup', {})], 'inferred'),
                                ([('aftermarket_catalog', {})], 'declared'), ([('supplier_declaration', {})], 'declared'),
                                ([('catalog_approval', {})], 'declared'), ([('oem_finder', {'run': 2})], 'inferred'),
                                ([('oem_finder', {}), ('catalog_approval', {})], 'declared'), ([('oem_finder', {}), ('price_list', {})], 'verified'),
                                ([], 'inferred')):
            self.assertEqual(derived_status(sources), status, sources)

    def test_reference_source_names_the_source_kind(self):
        self.assertEqual(source_of(ALGO), ('oem_finder', ALGO, {'run': 2, 'rule': 'current', 'rules_version': 'v2'}))
        self.assertEqual(source_of('algo:oem-finder:v3:strong-SPK/WAP-NP:15')[2], {'run': 15, 'rule': 'strong-SPK/WAP-NP', 'rules_version': 'v3'})
        self.assertEqual(source_of('algo:otro')[:2], ('oem_finder', 'algo:otro'))
        self.assertEqual(source_of(' https://parts.example/MR968365 '), ('ai_lookup', 'https://parts.example/MR968365', {'approved': True}))
        self.assertEqual(source_of('REVISADO A MANO'), ('catalog_approval', 'REVISADO A MANO', {}))
        self.assertEqual(source_of(''), ('catalog_approval', '', {}))

    def test_family_from_the_manufacturer_numbering_system(self):
        from .catalog_suffixes import SuffixTable
        table = SuffixTable.from_fixture()
        for manufacturer, code, forms, expected in (('TOYOTA', '1780130070', [], ('TOYOTA', '17801')), ('HONDA', '12341R70A00', [], ('HONDA', '12341')),
                                                    ('HYUNDAI', '28113F2000', [], ('HMG', '28113')), ('KIA', '28113F2000', [], ('HMG', '28113')),
                                                    ('NISSAN', '55046VW000', [], ('NISSAN', '55046')), ('MITSUBISHI', 'MR968365', [], ('MITSU_OLD', '')),
                                                    ('MAZDA', 'LF0114302', ['LF01-14-302'], ('MAZDA', '14-30')), ('ACME', '1780130070', [], ('', '')),
                                                    ('TOYOTA', 'MR968365', [], ('', ''))):
            self.assertEqual(classify_number(manufacturer, code, forms, table), expected, (manufacturer, code))


class FrozenSeedRuleTests(SimpleTestCase):
    """The frozen copy in migration 0039 keeps giving the answers the live helpers gave when live applied it (2026-10-07), whatever the
    refactor does to mall.oem_reference, mall.oem_finder and mall.catalog_suffixes afterwards."""
    def setUp(self):
        self.seed = migration_module('0039_oem_reference')

    def test_family_from_the_manufacturer_numbering_system(self):
        for manufacturer, code, forms, expected in (
                ('TOYOTA', '1780130070', [], ('TOYOTA', '17801')), ('LEXUS', '1780130070', [], ('TOYOTA', '17801')),
                ('HONDA', '12341R70A00', [], ('HONDA', '12341')), ('HYUNDAI', '28113F2000', [], ('HMG', '28113')),
                ('KIA', '28113F2000', [], ('HMG', '28113')), ('HYUNDAI/KIA', '28113F2000', [], ('HMG', '28113')),
                ('KIA', '0K2N133140', ['0K2N1-33-140'], ('KIA_LEGACY', '33-14')), ('NISSAN', '55046VW000', [], ('NISSAN', '55046')),
                ('MITSUBISHI', 'MR968365', [], ('MITSU_OLD', '')), ('MITSUBISHI', '1500A098', [], ('MITSU_NEW', '1500A')),
                ('MAZDA', 'LF0114302', ['LF01-14-302'], ('MAZDA', '14-30')),
                ('MAZDA', 'LF01143O2', ['LF01-14-3O2'], ('MAZDA', '14-30')),  # a letter O in a digit group reads as zero
                ('FORD', '6L8Z3078AA', ['6L8Z-30-78AA'], ('FORD', '3078')),  # a Ford number typed in Mazda form
                ('HINO', 'S2340-1234', ['S2340-1234'], ('HINO54', '')), ('ACME', '1780130070', [], ('', '')),
                ('TOYOTA', 'MR968365', [], ('', '')), ('TOYOTA', '17801-30070-X', ['17801-30070-X'], ('', ''))):
            self.assertEqual(self.seed.classify_number(manufacturer, code, forms), expected, (manufacturer, code))

    def test_a_colour_group_that_is_a_packaged_suffix_is_not_part_of_the_number(self):
        for manufacturer, code, expected in (('MITSUBISHI', 'MB699175XX', ('', '')), ('MITSUBISHI', 'MB699175QQ', ('MITSU_OLD', '')),
                                             ('MITSUBISHI', '1500A098HA', ('MITSU_NEW', '1500A')), ('HONDA', '12341R70A00ZZ', ('', '')),
                                             ('HONDA', '12341R70A00YR', ('HONDA', '12341'))):
            self.assertEqual(self.seed.classify_number(manufacturer, code), expected, code)
        self.assertEqual(len(self.seed.SUFFIX_PAIRS), 105)

    def test_part_type_and_description_from_the_line(self):
        for description, part_type in (('FILTRO AIRE TOY HILUX 2.7', 'FILTRO AIRE'), ('BASE MIT MONTERO', 'BASE'), ('TOY HILUX FILTRO', ''),
                                       ('SOPORTE MOTOR TOY', 'BASE MOTOR'), ('CORREA ALT HYU', 'CORREA ACCESORIO'),
                                       ('BOMBA DE GASOLINA', 'BOMBA GAS'), ('SENSOR OXIGENO', 'SENSOR OXIG'), ('ÁRBOL LEVAS', 'ARBOL'),
                                       ('X', ''), ('', '')):
            self.assertEqual(self.seed.part_defaults(description), {'description': description, 'part_type': part_type}, description)
        self.assertEqual(self.seed.part_defaults('  amortiguador  del. toy '), {'description': 'AMORTIGUADOR DEL. TOY', 'part_type': 'AMORT'})

    def test_status_and_source_rules(self):
        for sources, status in (([('price_list', {})], 'verified'), ([('manual', {'verified': False})], 'declared'),
                                ([('ai_lookup', {'approved': True})], 'verified'), ([('ai_lookup', {})], 'inferred'),
                                ([('oem_finder', {}), ('catalog_approval', {})], 'declared'), ([], 'inferred')):
            self.assertEqual(self.seed.derived_status(sources), status, sources)
        self.assertEqual(self.seed.source_of(ALGO), ('oem_finder', ALGO, {'run': 2, 'rule': 'current', 'rules_version': 'v2'}))
        self.assertEqual(self.seed.source_of(' https://parts.example/MR968365 '), ('ai_lookup', 'https://parts.example/MR968365', {'approved': True}))
        self.assertEqual(self.seed.source_of('REVISADO A MANO'), ('catalog_approval', 'REVISADO A MANO', {}))


class SyncTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.part = Part.objects.create(sku='17801-30070', description='FILTRO AIRE TOY HILUX 2.7')
        self.other = Part.objects.create(sku='0110-17801-FEB', description='FILTRO AIRE TOY')

    def ref(self, number='1780130070', manufacturer='TOYOTA'):
        return OEMReference.objects.filter(manufacturer=manufacturer, code=number).first()

    def test_an_oem_finder_alterno_becomes_an_inferred_reference_with_its_run(self):
        code = oem(self.part, '17801-30070')
        ref = self.ref()
        self.assertEqual((ref.printed_forms, ref.system, ref.family, ref.part_type, ref.description, ref.status, ref.version),
                         (['17801-30070'], 'TOYOTA', '17801', 'FILTRO AIRE', 'FILTRO AIRE TOY HILUX 2.7', 'inferred', 1))
        source = ref.sources.get()
        self.assertEqual((source.kind, source.citation, source.created_by), ('oem_finder', ALGO, None))
        self.assertEqual(source.detail, {'run': 2, 'rule': 'current', 'rules_version': 'v2',
                                         'part_codes': [{'id': code.pk, 'part': str(self.part.pk), 'code': '17801-30070'}]})
        code.save()  # an unchanged save changes nothing
        self.assertEqual((self.ref().version, OEMReferenceSource.objects.count()), (1, 1))

    def test_an_approved_ai_lookup_is_verified_and_a_catalog_approval_declared(self):
        part = Part.objects.create(sku='MR-968365', description='BASE MIT MONTERO')
        oem(part, 'MR-968365', 'MITSUBISHI', 'https://parts.example/MR968365')
        ref = self.ref('MR968365', 'MITSUBISHI')
        self.assertEqual((ref.status, ref.system, ref.sources.get().kind, ref.sources.get().detail['approved']), ('verified', 'MITSU_OLD', 'ai_lookup', True))
        oem(self.part, '17801-30070', source='')
        self.assertEqual((self.ref().status, self.ref().sources.get().kind, self.ref().sources.get().citation), ('declared', 'catalog_approval', ''))

    def test_a_new_source_text_moves_the_alterno_to_the_new_source(self):
        code = oem(self.part, '17801-30070')
        code.reference_source = 'CATÁLOGO TOYOTA EPC 2024'
        code.save()
        ref = self.ref()
        self.assertEqual([(s.kind, s.citation) for s in ref.sources.all()], [('catalog_approval', 'CATÁLOGO TOYOTA EPC 2024')])
        self.assertEqual((ref.status, ref.version), ('declared', 2))

    def test_retyping_renaming_or_retiring_the_alterno_removes_exactly_its_source(self):
        def numbers():
            return sorted((ref.manufacturer, ref.code, ref.sources.count()) for ref in OEMReference.objects.all())
        code = oem(self.part, '17801-30070')
        code.ref_type = 'unknown'
        code.save()
        self.assertEqual(numbers(), [('TOYOTA', '1780130070', 0)])  # its only source is gone; the number stays (S2a: never deleted)
        code.ref_type = 'oem'
        code.save()
        code.code = '17801-30080'
        code.save()
        self.assertEqual(numbers(), [('TOYOTA', '1780130070', 0), ('TOYOTA', '1780130080', 1)])
        code.brand = 'LEXUS'
        code.save()
        self.assertEqual(numbers(), [('LEXUS', '1780130080', 1), ('TOYOTA', '1780130070', 0), ('TOYOTA', '1780130080', 0)])
        references.retire(code)
        self.assertEqual(numbers(), [('LEXUS', '1780130080', 0), ('TOYOTA', '1780130070', 0), ('TOYOTA', '1780130080', 0)])
        self.assertFalse(OEMReferenceSource.objects.exists())

    def test_the_alterno_points_at_its_reference_through_every_change(self):
        code = oem(self.part, '17801-30070')
        code.refresh_from_db()
        self.assertEqual(code.oem_reference, self.ref())
        code.code = '17801-30080'
        code.save()
        code.refresh_from_db()
        self.assertEqual(code.oem_reference, self.ref('1780130080'))
        code.ref_type = 'company'
        code.save()
        code.refresh_from_db()
        self.assertIsNone(code.oem_reference)
        sister = oem(self.other, '17801-30070', source='LISTA TOYOTA')
        upsert_reference('TOYOTA', '1780130070', source_kind='price_list', citation='LISTA 2026')
        references.retire(sister)  # the reference keeps its price list and loses the source the alterno backed
        self.assertEqual([source.kind for source in self.ref().sources.all()], ['price_list'])
        sister.refresh_from_db()
        self.assertEqual((sister.estado, sister.oem_reference), ('retirada', self.ref()))  # a retired alterno keeps its number, as history

    def test_a_source_backed_by_two_alternos_stays_until_the_last_one_goes(self):
        first, second = oem(self.part, '17801-30070'), oem(self.other, '1780130070')
        ref = self.ref()
        source = ref.sources.get()
        self.assertEqual(([e['id'] for e in source.detail['part_codes']], ref.printed_forms), ([first.pk, second.pk], ['17801-30070']))
        references.retire(first)
        self.assertEqual([e['id'] for e in self.ref().sources.get().detail['part_codes']], [second.pk])
        references.retire(Reference.objects.filter(pk=second.pk))  # a queryset retires one row at a time: the same signal
        self.assertEqual((self.ref().sources.count(), self.ref().status), (0, 'inferred'))  # the number stays, with no source

    def test_a_reference_with_other_sources_or_human_input_outlives_the_alterno(self):
        code = oem(self.part, '17801-30070')
        upsert_reference('TOYOTA', '17801-30070', source_kind='manual', citation='LISTA DISTRIBUIDOR 2026 P. 4', detail={'verified': False})
        self.assertEqual(self.ref().status, 'declared')
        references.retire(code)
        ref = self.ref()
        self.assertEqual(([s.kind for s in ref.sources.all()], ref.status), (['manual'], 'declared'))
        other = oem(self.other, '17801-30080')
        disputed = self.ref('1780130080')
        disputed.status, disputed.dispute_note = 'disputed', 'ES UN FILTRO DE CABINA'
        disputed.save()
        references.retire(other)
        disputed = self.ref('1780130080')
        self.assertEqual((disputed.status, disputed.sources.count()), ('disputed', 0))

    def test_refresh_status_keeps_a_dispute_and_saves_only_on_change(self):
        ref, created = upsert_reference('TOYOTA', '17801-30070', source_kind='oem_finder', citation=ALGO)
        self.assertEqual((created, ref.status, ref.version), (True, 'inferred', 1))
        self.assertEqual((refresh_status(ref), OEMReference.objects.get(pk=ref.pk).version), ('inferred', 1))
        OEMReferenceSource.objects.create(reference=ref, kind='price_list', citation='LISTA 2026')
        self.assertEqual((refresh_status(ref), OEMReference.objects.get(pk=ref.pk).version), ('verified', 2))
        ref.status, ref.dispute_note = 'disputed', 'NO CORRESPONDE'
        ref.save()
        OEMReferenceSource.objects.create(reference=ref, kind='manufacturer_catalog', citation='EPC')
        self.assertEqual(refresh_status(ref, changed=True), 'disputed')
        self.assertEqual(upsert_reference('TOYOTA', '1780130070', source_kind='price_list', citation='LISTA 2026'), (OEMReference.objects.get(pk=ref.pk), False))

    def test_other_reference_types_and_brandless_oem_alternos_are_not_mirrored(self):
        with CaptureQueriesContext(connection) as queries:
            company = oem(self.part, 'ALT-17801', 'ACME', '', ref_type='company')
            company.code = 'ALT-17802'
            company.save()
            references.retire(company)
        self.assertFalse(any('oemreference' in q['sql'].lower() for q in queries.captured_queries))
        oem(self.part, '17801-30070', brand='')
        self.assertFalse(OEMReference.objects.exists())

    def test_alternos_saved_through_the_catalog_api(self):
        root = User.objects.create_superuser('oem-ref-api', 'oem-ref-api@example.invalid', 'Example938!')
        self.client.force_login(root)
        response = self.client.post('/api/v1/management/alternates/', {'part': str(self.part.pk), 'code': '17801-30070', 'brand': 'toyota',
                                                                      'ref_type': 'oem', 'reference_source': 'https://parts.example/17801-30070'},
                                    content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual((self.ref().status, self.ref().sources.get().kind), ('verified', 'ai_lookup'))
        self.assertEqual(self.client.delete(f'/api/v1/management/alternates/{response.json()["id"]}/').status_code, 204)
        self.assertEqual(Reference.all_objects.get(pk=response.json()['id']).estado, 'retirada')  # retired, not deleted
        self.assertEqual((OEMReference.objects.count(), OEMReferenceSource.objects.count()), (1, 0))  # the number stays without its source


class ApplyRevertSyncTests(Catalog, TestCase):
    def test_the_finder_apply_and_its_undo_reach_the_library(self):
        run = self.canary(FLAG)['run']
        refs = OEMReference.objects.prefetch_related('sources')
        self.assertEqual((refs.count(), {r.status for r in refs}), (11, {'inferred'}))
        ref = refs.get(manufacturer='TOYOTA', code='4865430030')
        self.assertEqual((ref.printed_forms, ref.system, ref.family, ref.part_type), (['48654-30030'], 'TOYOTA', '48654', 'BASE AMORT'))
        self.assertEqual((ref.sources.get().citation, ref.sources.get().detail['run']), (f'algo:oem-finder:v3:current:{run.pk}', run.pk))
        self.assertEqual(oa.revert_run(run.pk)['reverted'], 11)
        self.assertEqual((OEMReference.objects.count(), OEMReferenceSource.objects.count()), (11, 0))  # the numbers stay (S2a), unbacked

    def test_a_rename_and_the_restore_of_a_retyped_alterno(self):
        part = self.part('54830-2H000-MOBIS')
        PartCode.objects.create(part=part, code='54830-2H000', brand='HYUNDAI', ref_type='unknown')  # retyped by the apply, restored by the undo
        run = self.canary(RENAME)['run']
        ref = OEMReference.objects.get()
        self.assertEqual((ref.manufacturer, ref.code, ref.status, ref.sources.get().citation),
                         ('HYUNDAI', '548302H000', 'inferred', f'algo:oem-finder:v3:strong-MOBIS:{run.pk}'))
        self.assertEqual(len(self.link('54830-2H000-MOBIS').undo['updated_codes']), 1)
        self.assertEqual(oa.revert_run(run.pk)['reverted'], 1)
        self.assertEqual(PartCode.objects.get(code='54830-2H000').ref_type, 'unknown')
        self.assertFalse(OEMReference.objects.get().sources.exists())

    def test_a_disputed_number_is_never_applied_again(self):
        from .catalog_identity import unmark_disputed
        from . import oem_auto
        self.canary(FLAG)
        ref = OEMReference.objects.get(manufacturer='TOYOTA', code='4865430030')
        ref.status, ref.dispute_note = 'disputed', 'Es de otro modelo'
        ref.save()
        self.assertEqual(unmark_disputed(ref), ['48654-30030'])
        preview = oa.apply_auto(tier=FLAG, read_only=True)
        self.assertEqual(preview['excluded'].get('disputed'), 1)
        outcome = oa.apply_auto(tier=FLAG)
        self.assertEqual(outcome['summary']['excluded'].get('disputed'), 1)
        self.assertFalse(Part.objects.get(sku='48654-30030').is_OEM)  # the person's 'no' is not overridden by a bulk apply
        from .oem_finder_models import OEMAutoCandidate
        self.assertEqual(OEMAutoCandidate.objects.filter(part__sku='48654-30030').values_list('status', 'reason').first(), ('excluded', 'disputed'))

    def test_undoing_a_flag_keeps_oem_when_the_code_is_a_declared_number(self):
        run = self.canary(FLAG)['run']
        ref = OEMReference.objects.get(manufacturer='TOYOTA', code='4865430030')
        OEMReferenceSource.objects.create(reference=ref, kind='aftermarket_catalog', citation='ASVA · pág. 2')
        refresh_status(ref, changed=True)
        oa.revert_run(run.pk)
        part = Part.objects.get(sku='48654-30030')
        self.assertTrue(part.is_OEM)  # its code is a declared number of the OEM list
        self.assertEqual(part.identity_changes.latest('pk').reference['kept_oem'], ['TOYOTA 4865430030'])
        self.assertTrue(OEMReference.objects.filter(pk=ref.pk).exists())
        self.assertFalse(Part.objects.get(sku='48654-0K010').is_OEM)  # only inferred: the undo takes the flag off


class MissingTablesTests(Fresh, TestCase):
    def test_partcode_writes_and_the_api_work_before_the_migration(self):
        part = Part.objects.create(sku='17801-30070', description='FILTRO AIRE')
        root = User.objects.create_superuser('oem-ref-503', 'oem-ref-503@example.invalid', 'Example938!')
        self.client.force_login(root)
        orf._state['warned'] = False
        with patch.object(OEMReference._meta, 'db_table', 'mall_oemreference_not_migrated'), \
                patch.object(OEMReferenceSource._meta, 'db_table', 'mall_oemreferencesource_not_migrated'):
            with self.assertLogs('mall.oem_reference', level='WARNING') as logs:
                code = oem(part, '17801-30070')
            self.assertEqual(len(logs.records), 1)
            with self.assertNoLogs('mall.oem_reference', level='WARNING'):
                code.reference_source = 'OTRA FUENTE'
                code.save()
                references.retire(code)
            self.assertEqual(Part.objects.count(), 1)  # the failed sync ran in a savepoint: the transaction is still usable
            self.assertEqual((Reference.objects.count(), Reference.all_objects.get().estado), (0, 'retirada'))
            for response in (self.client.get(URL), self.client.post(URL, {'manufacturer': 'TOYOTA', 'code': '1'}, content_type='application/json'),
                             self.client.get(f'{URL}00000000-0000-4000-8000-000000000001/')):
                self.assertEqual(response.status_code, 503)
                self.assertIn('migraciones', response.json()['detail'])
        references.add(part, '17801-30070', 'TOYOTA', ref_type='oem', source=ALGO)  # added again: the retired alterno comes back
        self.assertEqual(OEMReference.objects.count(), 1)

    def test_an_unexpected_sync_error_is_logged_and_never_breaks_the_save(self):
        part = Part.objects.create(sku='17801-30070', description='FILTRO AIRE')
        with patch('mall.oem_reference.upsert_reference', side_effect=RuntimeError('fallo')), self.assertLogs('mall.oem_reference', level='ERROR'):
            oem(part, '17801-30070')
        self.assertTrue(PartCode.objects.filter(code='17801-30070').exists())


class SeedTests(Fresh, TestCase):
    """Migrations 0039 and 0040 seed and link the library from the catalog's OEM alternos, with frozen copies of the service on their
    historical models: the result must equal what the live signal handlers mirror."""
    SEED, LINK = '0039_oem_reference', '0040_partcode_oem_reference'

    def setUp(self):
        super().setUp()
        parts = [Part.objects.create(sku=sku, description=description) for sku, description in (
            ('17801-30070', 'FILTRO AIRE TOY HILUX'), ('0110-17801-FEB', 'FILTRO AIRE TOY'), ('28113-F2000', 'FILTRO AIRE HYU ELANTRA'),
            ('MR-968365', 'BASE MIT MONTERO'))]
        oem(parts[0], '17801-30070')
        oem(parts[1], '1780130070')
        oem(parts[2], '28113-F2000', 'HYUNDAI', 'algo:oem-finder:v2:current:3')
        oem(parts[3], 'MR-968365', 'MITSUBISHI', 'https://parts.example/MR968365')
        oem(parts[3], 'MR968365', '', '')  # brandless: not a fact
        oem(parts[0], 'ALT-1', 'ACME', ref_type='company')
        self.mirrored = state()

    def seed(self):
        old = historical_apps(self.SEED)
        return migration_module(self.SEED).seed_from_partcodes(old.get_model('mall', 'OEMReference'), old.get_model('mall', 'OEMReferenceSource'),
                                                              old.get_model('mall', 'PartCode'))

    def link(self):
        old = historical_apps(self.LINK)
        return migration_module(self.LINK).link_partcodes(old.get_model('mall', 'OEMReference'), old.get_model('mall', 'PartCode'))

    def test_the_seed_rebuilds_the_mirror_and_is_idempotent(self):
        self.assertEqual(len(self.mirrored), 3)
        OEMReference.objects.all().delete()
        self.assertEqual(self.seed(), {'references': 3, 'sources': 3})
        self.assertEqual(state(), self.mirrored)
        versions = dict(OEMReference.objects.values_list('code', 'version'))
        self.assertEqual(self.seed(), {'references': 0, 'sources': 0})
        self.assertEqual((state(), dict(OEMReference.objects.values_list('code', 'version'))), (self.mirrored, versions))

    def test_the_seed_merges_into_existing_references(self):
        ref = OEMReference.objects.get(code='1780130070')
        ref.sources.all().delete()
        upsert_reference('TOYOTA', '1780130070', source_kind='price_list', citation='LISTA 2026')
        self.assertEqual(self.seed(), {'references': 0, 'sources': 1})
        ref = OEMReference.objects.get(code='1780130070')
        self.assertEqual((sorted(ref.sources.values_list('kind', flat=True)), ref.status), (['oem_finder', 'price_list'], 'verified'))

    def test_the_link_backfill_is_idempotent_and_skips_other_alternos(self):
        PartCode.objects.update(oem_reference=None)
        self.assertEqual(self.link(), 4)  # two TOYOTA spellings, HYUNDAI, MITSUBISHI
        linked = dict(PartCode.objects.values_list('code', 'oem_reference__code'))
        self.assertEqual((linked['17801-30070'], linked['1780130070'], linked['MR968365'], linked['ALT-1']), ('1780130070', '1780130070', None, None))
        self.assertEqual(self.link(), 0)
        run_migration_function(self.LINK, 'backfill')
        self.assertEqual(dict(PartCode.objects.values_list('code', 'oem_reference__code')), linked)

    def test_the_migration_seed(self):
        OEMReference.objects.all().delete()
        run_migration_function(self.SEED, 'seed')
        self.assertEqual(state(), self.mirrored)


class APITests(Fresh, APITestCase):
    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser('oem-ref-root', 'oem-ref-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)
        self.part = Part.objects.create(sku='17801-30070', description='FILTRO AIRE TOY HILUX')
        self.code = oem(self.part, '17801-30070')
        self.ref = OEMReference.objects.get()

    def url(self, ref=None):
        return f'{URL}{(ref or self.ref).pk}/'

    def patch(self, payload, ref=None):
        ref = OEMReference.objects.get(pk=(ref or self.ref).pk)
        return self.client.patch(self.url(ref), {'expected_version': ref.version, **payload}, format='json')

    def test_the_list_sorts_on_whitelisted_columns(self):
        upsert_reference('HYUNDAI', '28113-F2000', source_kind='manual', citation='LISTA')
        upsert_reference('TOYOTA', '90915-YZZD2', source_kind='price_list', citation='LISTA 2026')
        codes = lambda ordering: [row['code'] for row in self.client.get(URL, {'ordering': ordering}).data['results']]
        self.assertEqual(codes(''), ['28113F2000', '1780130070', '90915YZZD2'])  # manufacturer, code
        self.assertEqual(codes('-code'), ['90915YZZD2', '28113F2000', '1780130070'])
        self.assertEqual(codes('status,code'), ['28113F2000', '1780130070', '90915YZZD2'])  # declared, inferred, verified
        self.assertEqual(codes('password'), codes(''))  # unknown names are ignored

    def test_only_superusers(self):
        staff = User.objects.create_user('oem-ref-staff', 'oem-ref-staff@example.invalid', 'Example938!')
        for user in (None, staff):
            self.client.force_authenticate(user)
            for response in (self.client.get(URL), self.client.post(URL, {'manufacturer': 'TOYOTA', 'code': '17801-30080'}, format='json'),
                             self.client.get(self.url()), self.client.patch(self.url(), {'expected_version': 1, 'action': 'dispute', 'note': 'X'}, format='json')):
                self.assertIn(response.status_code, (401, 403))
        self.assertEqual((OEMReference.objects.count(), OEMReference.objects.get().status), (1, 'inferred'))

    def test_list_search_filters_counts_and_pagination(self):
        hyundai = Part.objects.create(sku='0K2N1-13-Z40', description='EMP TAPA VALV KIA')
        oem(hyundai, '28113-F2000', 'HYUNDAI', 'https://parts.example/28113F2000')
        upsert_reference('MITSUBISHI', 'MR-968365', source_kind='aftermarket_catalog', citation='FEBEST 2025 P. 81', defaults={'part_type': 'BASE AMORT'})
        data = self.client.get(URL).data
        self.assertEqual((data['count'], [r['code'] for r in data['results']]), (3, ['28113F2000', 'MR968365', '1780130070']))
        self.assertEqual(data['counts'], {'status': {'inferred': 1, 'verified': 1, 'declared': 1}, 'manufacturer': {'TOYOTA': 1, 'HYUNDAI': 1, 'MITSUBISHI': 1},
                                          'total': 3})
        row = next(r for r in data['results'] if r['code'] == '1780130070')
        self.assertEqual((row['source_count'], row['source_kinds'], row['status_label'], row['family'], row['part_type']),
                         (1, {'oem_finder': 1}, 'Inferido', '17801', 'FILTRO AIRE'))
        self.assertEqual(row['linked_skus'], [{'id': str(self.part.pk), 'sku': '17801-30070', 'is_OEM': False, 'active': True, 'via': 'sku', 'code': '17801-30070'}])
        codes = lambda **params: [r['code'] for r in self.client.get(URL, params).data['results']]
        self.assertEqual(codes(q='17801-3'), ['1780130070'])
        self.assertEqual(codes(q='mr-968'), ['MR968365'])
        self.assertEqual(codes(q='hilux'), ['1780130070'])
        self.assertEqual(codes(q='base amort'), ['MR968365'])
        self.assertEqual(codes(manufacturer='hyundai'), ['28113F2000'])
        self.assertEqual(codes(status='verified'), ['28113F2000'])
        self.assertEqual(codes(source_kind='aftermarket_catalog'), ['MR968365'])
        self.assertEqual(codes(linked='true'), ['28113F2000', '1780130070'])  # the HYUNDAI number is an alterno of another SKU
        self.assertEqual(codes(linked='false'), ['MR968365'])
        for bad in ({'status': 'x'}, {'source_kind': 'x'}, {'linked': 'si'}):
            self.assertEqual(self.client.get(URL, bad).status_code, 400)
        for n in range(55):
            upsert_reference('NISSAN', f'16546-ED{n:03d}', source_kind='manual', citation='LISTA NISSAN')
        page = self.client.get(URL, {'manufacturer': 'NISSAN', 'page': 2}).data
        self.assertEqual((page['count'], len(page['results']), page['next']), (55, 5, None))

    def test_linked_skus_without_a_query_per_row(self):
        def listing():
            with CaptureQueriesContext(connection) as queries:
                data = self.client.get(URL).data
            return len(queries.captured_queries), data
        few, _ = listing()
        for n in range(20):
            part = Part.objects.create(sku=f'90385-T{n:04d}', description='BUJE BARRA ESTAB')
            oem(part, f'90385-T{n:04d}')
            PartCode.objects.create(part=Part.objects.create(sku=f'ALT-{n}'), code=f'90385T{n:04d}', brand='', ref_type='unknown')
        many, data = listing()
        self.assertEqual(few, many)
        self.assertLessEqual(many, 11)  # page-level only: linked SKUs (3 queries) and aftermarket codes (1) for the whole page
        row = next(r for r in data['results'] if r['code'] == '90385T0007')
        self.assertEqual(sorted((s['sku'], s['via'], s['code']) for s in row['linked_skus']), [('90385-T0007', 'sku', '90385-T0007'), ('ALT-7', 'unknown', '90385T0007')])
        other = Part.objects.create(sku='OTRO-1')
        oem(other, '17801 30070', brand='LEXUS')  # an OEM alterno of another manufacturer does not carry the TOYOTA number
        self.assertEqual([s['sku'] for s in linked_parts([self.ref])[self.ref.pk]], ['17801-30070'])
        Part.objects.filter(pk=self.part.pk).update(merged_into=other, active=False, estado='unificada')
        self.assertEqual(linked_parts([self.ref])[self.ref.pk], [])

    def test_create_is_idempotent_on_the_compact_number(self):
        payload = {'manufacturer': ' toyota ', 'code': '17801-30080', 'description': 'filtro aire', 'source': {'kind': 'price_list', 'citation': 'LISTA TOYOTA 2026 P. 4'}}
        first = self.client.post(URL, payload, format='json')
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual((first.data['manufacturer'], first.data['code'], first.data['printed_forms'], first.data['status'], first.data['description'],
                          first.data['family'], first.data['created_by']), ('TOYOTA', '1780130080', ['17801-30080'], 'verified', 'FILTRO AIRE', '17801', 'oem-ref-root'))
        again = self.client.post(URL, payload, format='json')
        self.assertEqual((again.status_code, again.data['id'], again.data['version'], len(again.data['sources'])), (200, first.data['id'], 1, 1))
        merged = self.client.post(URL, {'manufacturer': 'TOYOTA', 'code': '17801 30080'}, format='json')
        self.assertEqual((merged.status_code, merged.data['printed_forms'], [(s['kind'], s['citation']) for s in merged.data['sources']]),
                         (200, ['17801-30080', '17801 30080'], [('price_list', 'LISTA TOYOTA 2026 P. 4'), ('manual', 'Registro manual de oem-ref-root')]))
        manual = self.client.post(URL, {'manufacturer': 'HONDA', 'code': '19200-PNA-003', 'source': {'kind': 'manual', 'citation': 'EPC HONDA', 'verified': True}}, format='json')
        self.assertEqual((manual.data['code'], manual.data['status'], manual.data['system'], manual.data['sources'][0]['detail']),
                         ('19200PNA003', 'verified', 'HONDA', {'verified': True}))
        for bad in ({'manufacturer': 'TOYOTA', 'code': '--'}, {'manufacturer': ' ', 'code': '1'}, {'manufacturer': 'TOYOTA', 'code': 'X' * 61},
                    {'manufacturer': 'TOYOTA', 'code': '1', 'source': {'kind': 'price_list', 'citation': 'L', 'verified': True}},
                    {'manufacturer': 'TOYOTA', 'code': '1', 'source': {'kind': 'oem_finder', 'citation': 'L'}},
                    {'manufacturer': 'TOYOTA', 'code': '1', 'source': {'kind': 'manual', 'citation': ''}}):
            self.assertEqual(self.client.post(URL, bad, format='json').status_code, 400, bad)

    def test_edits_check_the_version_and_repeats_change_nothing(self):
        stale = self.ref.version
        response = self.patch({'description': 'filtro aire motor 2tr', 'notes': 'Confirmar con el EPC.'})
        self.assertEqual((response.status_code, response.data['description'], response.data['notes'], response.data['version'], response.data['updated_by']),
                         (200, 'FILTRO AIRE MOTOR 2TR', 'Confirmar con el EPC.', stale + 1, 'oem-ref-root'))
        conflict = self.client.patch(self.url(), {'expected_version': stale, 'description': 'OTRA'}, format='json')
        self.assertEqual((conflict.status_code, conflict.data['reference']['version'], conflict.data['reference']['description']), (409, stale + 1, 'FILTRO AIRE MOTOR 2TR'))
        self.assertIn('cambió', conflict.data['detail'])
        repeat = self.client.patch(self.url(), {'expected_version': stale, 'description': 'filtro aire motor 2tr'}, format='json')
        self.assertEqual((repeat.status_code, repeat.data['version']), (200, stale + 1))
        self.assertEqual(self.patch({}).status_code, 400)
        self.assertEqual(self.client.patch(f'{URL}00000000-0000-4000-8000-000000000001/', {'expected_version': 1, 'notes': 'X'}, format='json').status_code, 404)

    def test_sources_dispute_and_status(self):
        added = self.patch({'action': 'add_source', 'source': {'kind': 'manual', 'citation': 'EPC TOYOTA 2026', 'verified': True}})
        self.assertEqual((added.status_code, added.data['status'], [(s['kind'], s['removable']) for s in added.data['sources']]),
                         (200, 'verified', [('oem_finder', False), ('manual', True)]))
        self.assertEqual(self.patch({'action': 'add_source', 'source': {'kind': 'manual', 'citation': 'EPC TOYOTA 2026'}}).data['version'], added.data['version'])
        finder, manual = (s['id'] for s in added.data['sources'])
        self.assertEqual(self.patch({'action': 'remove_source', 'source_id': finder}).status_code, 400)
        disputed = self.patch({'action': 'dispute', 'note': 'Es filtro de cabina según el EPC.'})
        self.assertEqual((disputed.data['status'], disputed.data['status_label'], disputed.data['dispute_note']), ('disputed', 'En disputa', 'Es filtro de cabina según el EPC.'))
        self.assertEqual(self.patch({'action': 'remove_source', 'source_id': manual}).data['status'], 'disputed')  # a dispute outlives source changes
        self.assertEqual(self.patch({'action': 'dispute', 'note': ' '}).status_code, 400)
        cleared = self.patch({'action': 'clear_dispute'})
        self.assertEqual((cleared.data['status'], cleared.data['dispute_note']), ('inferred', ''))
        self.assertEqual(self.patch({'action': 'clear_dispute'}).data['version'], cleared.data['version'])
        references.retire(self.code)  # the last automatic source goes with its alterno; the number stays
        self.assertEqual(OEMReference.objects.get().sources.count(), 0)
        ref, _ = upsert_reference('TOYOTA', '17801-30070', source_kind='price_list', citation='LISTA 2026')
        only = ref.sources.get()
        self.assertEqual(self.patch({'action': 'remove_source', 'source_id': only.pk}, ref).status_code, 400)  # never the last one
        self.assertEqual(self.patch({'action': 'remove_source', 'source_id': 999999}, ref).status_code, 200)  # already gone
        self.assertEqual(self.patch({'action': 'add_source'}, ref).status_code, 400)

    def test_supersession_creates_the_target_and_rejects_cycles(self):
        response = self.patch({'action': 'set_superseded_by', 'code': '17801-30080'})
        self.assertEqual(response.status_code, 200, response.data)
        target = OEMReference.objects.get(code='1780130080')
        self.assertEqual(response.data['superseded_by'], {'id': str(target.pk), 'manufacturer': 'TOYOTA', 'code': '1780130080', 'status': 'declared'})
        self.assertEqual((target.part_type, target.sources.get().kind, target.sources.get().citation), ('FILTRO AIRE', 'manual', 'Reemplaza a TOYOTA 1780130070'))
        detail = self.client.get(self.url(target)).data
        self.assertEqual([r['code'] for r in detail['supersedes']], ['1780130070'])
        self.assertEqual(self.patch({'action': 'set_superseded_by', 'manufacturer': 'TOYOTA', 'code': '1780130080'}).data['version'], response.data['version'])
        third, _ = upsert_reference('TOYOTA', '17801-30090', source_kind='manual', citation='LISTA')
        self.assertEqual(self.patch({'action': 'set_superseded_by', 'code': '17801-30090'}, target).status_code, 200)
        cycle = self.patch({'action': 'set_superseded_by', 'code': '1780130070'}, third)
        self.assertEqual(cycle.status_code, 400)
        self.assertIn('ciclo', str(cycle.data))
        self.assertEqual(self.patch({'action': 'set_superseded_by', 'code': '17801-30070'}).status_code, 400)  # itself
        self.assertEqual(self.patch({'action': 'set_superseded_by', 'code': '--'}).status_code, 400)
        cleared = self.patch({'action': 'clear_superseded_by'})
        self.assertIsNone(cleared.data['superseded_by'])
        self.assertEqual(self.patch({'action': 'set_superseded_by', 'code': '1780130070'}, third).status_code, 200)  # no loop any more
