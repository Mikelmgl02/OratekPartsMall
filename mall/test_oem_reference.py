from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from . import references
from .models import Part, PartCode, Reference, ReferenceSource, User
from .oem_reference import classify_number, compact, derived_status, linked_parts, source_of
from .test_factories import make_code, migration_module
from .test_oem_finder import Fresh

URL = '/api/v1/management/oem-references/'
ALGO = 'algo:oem-finder:v2:current:2'


# From S2a one OEM number lives on one line (0074's reference_one_live_line): a second line's alterno of it is in dispute.
SHARED = {'estado': 'disputa', 'dispute_note': 'OTRO SKU TIENE ESTE NÚMERO'}


def oem(part, code, brand='TOYOTA', source=ALGO, ref_type='oem', **fields):
    return make_code(part=part, code=code, brand=brand, ref_type=ref_type, reference_source=source, **fields)


class CompactTests(Fresh, TestCase):
    def test_compact_keeps_uppercase_letters_and_digits_only(self):
        for written, stored in (('17801-30070', '1780130070'), (' mr-968365 ', 'MR968365'), ('19200-PNA-003', '19200PNA003'),
                                ('17801 30070/a.1_', '1780130070A1'), ('(90385) T0002+', '90385T0002'), ('', ''), (None, ''), ('ñ-°', '')):
            self.assertEqual(compact(written), stored, written)

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


class MirrorRetiredTests(Fresh, TestCase):
    """S4: the OEM table is frozen; an OEM alterno is a library row itself (a Reference of tipo oem with a brand), described from its
    line the way the mirror described the number, and nothing writes OEMReference any more."""

    def setUp(self):
        super().setUp()
        self.part = Part.objects.create(sku='17801-30070', description='FILTRO AIRE TOY HILUX 2.7')

    def test_an_oem_alterno_is_the_library_row_and_takes_the_texts_the_mirror_gave(self):
        code, _ = references.add(self.part, '17801-30070', 'TOYOTA', ref_type='oem', source=ALGO)
        code.refresh_from_db()
        self.assertEqual((code.estado, code.detail['description'], code.detail['part_type'], code.detail['system'], code.detail['family']),
                         ('inferido', 'FILTRO AIRE TOY HILUX 2.7', 'FILTRO AIRE', 'TOYOTA', '17801'))
        self.assertEqual(code.sources.get().kind, 'buscador')
        approved, _ = references.add(Part.objects.create(sku='MR-968365', description='BASE MIT MONTERO'), 'MR-968365', 'MITSUBISHI',
                                     ref_type='oem', source='https://parts.example/MR968365')
        self.assertEqual(approved.estado, 'verificado')  # an AI lookup approved into the catalog, as the library ruled it

    def test_other_reference_types_and_brandless_oem_alternos_are_no_library_rows(self):
        with CaptureQueriesContext(connection) as queries:
            company = oem(self.part, 'ALT-17801', 'ACME', '', ref_type='company')
            company.code = 'ALT-17802'
            company.save()
            references.retire(company)
        self.assertFalse(any('oemreference' in q['sql'].lower() for q in queries.captured_queries))
        references.add(self.part, '1780130070X', '', ref_type='oem')
        self.assertFalse(Reference.objects.filter(references.library_q()).exists())


class APITests(Fresh, APITestCase):
    """Biblioteca OEM on the library's References (S4): ids are uids, codes claves, status and source kinds the legacy values."""

    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser('oem-ref-root', 'oem-ref-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)
        self.part = Part.objects.create(sku='17801-30070', description='FILTRO AIRE TOY HILUX')
        self.code, _ = references.add(self.part, '17801-30070', 'TOYOTA', ref_type='oem', source=ALGO)
        self.ref = self.code

    def url(self, ref=None):
        return f'{URL}{(ref or self.ref).uid}/'

    def patch(self, payload, ref=None):
        ref = Reference.all_objects.get(pk=(ref or self.ref).pk)
        return self.client.patch(self.url(ref), {'expected_version': ref.version, **payload}, format='json')

    def test_the_list_sorts_on_whitelisted_columns(self):
        references.add_library_number('HYUNDAI', '28113-F2000', source_kind='manual', citation='LISTA')
        references.add_library_number('TOYOTA', '90915-YZZD2', source_kind='price_list', citation='LISTA 2026')
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
        library = Reference.objects.filter(references.library_q())
        self.assertEqual((library.count(), library.get().estado), (1, 'inferido'))

    def test_list_search_filters_counts_and_pagination(self):
        hyundai = Part.objects.create(sku='0K2N1-13-Z40', description='EMP TAPA VALV KIA')
        references.add(hyundai, '28113-F2000', 'HYUNDAI', ref_type='oem', source='https://parts.example/28113F2000')
        references.add_library_number('MITSUBISHI', 'MR-968365', source_kind='aftermarket_catalog', citation='FEBEST 2025 P. 81', part_type='BASE AMORT')
        data = self.client.get(URL).data
        self.assertEqual((data['count'], [r['code'] for r in data['results']]), (3, ['28113F2000', 'MR968365', '1780130070']))
        self.assertEqual(data['counts'], {'status': {'inferred': 1, 'verified': 1, 'declared': 1}, 'manufacturer': {'TOYOTA': 1, 'HYUNDAI': 1, 'MITSUBISHI': 1},
                                          'total': 3})
        row = next(r for r in data['results'] if r['code'] == '1780130070')
        self.assertEqual((row['source_count'], row['source_kinds'], row['status_label'], row['family'], row['part_type']),
                         (1, {'oem_finder': 1}, 'Inferido', '17801', 'FILTRO AIRE'))
        self.assertEqual(row['linked_skus'], [{'id': str(self.part.pk), 'sku': '17801-30070', 'is_OEM': False, 'active': True, 'via': 'linea', 'code': '17801-30070'}])
        codes = lambda **params: [r['code'] for r in self.client.get(URL, params).data['results']]
        self.assertEqual(codes(q='17801-3'), ['1780130070'])
        self.assertEqual(codes(q='mr-968'), ['MR968365'])
        self.assertEqual(codes(q='hilux'), ['1780130070'])
        self.assertEqual(codes(q='base amort'), ['MR968365'])
        self.assertEqual(codes(manufacturer='hyundai'), ['28113F2000'])
        self.assertEqual(codes(status='verified'), ['28113F2000'])
        self.assertEqual(codes(status='verificado'), ['28113F2000'])
        self.assertEqual(codes(source_kind='aftermarket_catalog'), ['MR968365'])
        self.assertEqual(codes(source_kind='buscador'), ['1780130070'])
        self.assertEqual(codes(linked='true'), ['28113F2000', '1780130070'])  # each one of a catalog line's codes
        self.assertEqual(codes(linked='false'), ['MR968365'])  # on its own line in referencia
        for bad in ({'status': 'x'}, {'source_kind': 'x'}, {'linked': 'si'}):
            self.assertEqual(self.client.get(URL, bad).status_code, 400)
        for n in range(55):
            references.add_library_number('NISSAN', f'16546-ED{n:03d}', source_kind='manual', citation='LISTA NISSAN')
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
            references.add(part, f'90385-T{n:04d}', 'TOYOTA', ref_type='oem', source=ALGO)
        many, data = listing()
        self.assertEqual(few, many)
        self.assertLessEqual(many, 16)  # page-level only: sources, lines, links and aftermarket codes once each for the whole page
        row = next(r for r in data['results'] if r['code'] == '90385T0007')
        self.assertEqual([(s['sku'], s['via'], s['code']) for s in row['linked_skus']], [('90385-T0007', 'linea', '90385-T0007')])
        other = Part.objects.create(sku='OTRO-1')
        Part.objects.filter(pk=self.part.pk).update(merged_into=other, active=False, estado='unificada')
        self.ref.refresh_from_db()
        self.assertEqual(linked_parts([self.ref])[self.ref.pk], [])

    def test_create_is_idempotent_on_the_compact_number(self):
        payload = {'manufacturer': ' toyota ', 'code': '17801-30080', 'description': 'filtro aire', 'source': {'kind': 'price_list', 'citation': 'LISTA TOYOTA 2026 P. 4'}}
        first = self.client.post(URL, payload, format='json')
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual((first.data['manufacturer'], first.data['code'], first.data['printed_forms'], first.data['status'], first.data['description'],
                          first.data['family'], first.data['created_by']), ('TOYOTA', '1780130080', ['17801-30080'], 'verified', 'FILTRO AIRE', '17801', 'oem-ref-root'))
        again = self.client.post(URL, payload, format='json')
        self.assertEqual((again.status_code, again.data['id'], again.data['version'], len(again.data['sources'])), (200, first.data['id'], first.data['version'], 1))
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
        stale = Reference.objects.get(pk=self.ref.pk).version
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
        self.part.refresh_from_db()
        self.assertEqual(self.part.description, 'FILTRO AIRE TOY HILUX')  # a published line keeps its own texts

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
        self.assertEqual(len(cleared.data['sources']), 1)  # the manual source was retired, never deleted
        self.assertTrue(ReferenceSource.objects.filter(pk=manual, retirada=True).exists())
        self.assertEqual(self.patch({'action': 'remove_source', 'source_id': finder}).status_code, 400)  # never the last one
        self.assertEqual(self.patch({'action': 'remove_source', 'source_id': 999999}).status_code, 200)  # already gone
        self.assertEqual(self.patch({'action': 'add_source'}).status_code, 400)

    def test_supersession_creates_the_target_and_rejects_cycles(self):
        response = self.patch({'action': 'set_superseded_by', 'code': '17801-30080'})
        self.assertEqual(response.status_code, 200, response.data)
        target = Reference.objects.get(references.library_q(), clave='1780130080')
        self.assertEqual(response.data['superseded_by'], {'id': str(target.uid), 'manufacturer': 'TOYOTA', 'code': '1780130080', 'status': 'declared'})
        source = target.sources.get()
        self.assertEqual((target.detail['part_type'], source.kind, source.detail['legacy_kind'], source.citation),
                         ('FILTRO AIRE', 'biblioteca', 'manual', 'Reemplaza a TOYOTA 1780130070'))
        detail = self.client.get(self.url(target)).data
        self.assertEqual([r['code'] for r in detail['supersedes']], ['1780130070'])
        self.assertEqual(self.patch({'action': 'set_superseded_by', 'manufacturer': 'TOYOTA', 'code': '1780130080'}).data['version'], response.data['version'])
        third, _ = references.add_library_number('TOYOTA', '17801-30090', source_kind='manual', citation='LISTA')
        self.assertEqual(self.patch({'action': 'set_superseded_by', 'code': '17801-30090'}, target).status_code, 200)
        cycle = self.patch({'action': 'set_superseded_by', 'code': '1780130070'}, third)
        self.assertEqual(cycle.status_code, 400)
        self.assertIn('ciclo', str(cycle.data))
        self.assertEqual(self.patch({'action': 'set_superseded_by', 'code': '17801-30070'}).status_code, 400)  # itself
        self.assertEqual(self.patch({'action': 'set_superseded_by', 'code': '--'}).status_code, 400)
        cleared = self.patch({'action': 'clear_superseded_by'})
        self.assertIsNone(cleared.data['superseded_by'])
        self.assertEqual(self.patch({'action': 'set_superseded_by', 'code': '1780130070'}, third).status_code, 200)  # no loop any more
