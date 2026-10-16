from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from . import references
from .models import Part, Reference
from .oem_reference import classify_number, compact, derived_status, source_of
from .test_factories import make_code, migration_module
from .test_oem_finder import Fresh

ALGO = 'algo:oem-finder:v2:current:2'


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
