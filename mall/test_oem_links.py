import io

from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APITestCase

from . import link_oem_numbers, references
from .models import Part, User
from .oem_links import name_key, name_targets, part_equivalents, refresh_name_links, store_catalog_links, store_cross_references
from .oem_reference import linked_parts
from .oem_reference_models import OEMCrossReference, PartOEMLink
from .test_oem_finder import Fresh

URL = '/api/v1/management/oem-references/'


def number(manufacturer, code, citation='CATÁLOGO GMB 2016'):
    """A library number (S4: a Reference, on a line of its own in referencia when no line carries it)."""
    return references.add_library_number(manufacturer, code, source_kind='aftermarket_catalog', citation=citation)[0]


class NameKeyTests(Fresh, TestCase):
    def test_the_code_or_the_code_before_its_tags_names_the_number(self):
        for sku, key in (('16100-29155', '1610029155'), ('16100-79445-NPW', '1610079445'), ('16100-39466-G', '1610039466'),
                         ('90919-01164 DENSO', '9091901164'), ('04465-0K240-AISIN', '044650K240')):
            self.assertEqual(name_key(sku), key, sku)

    def test_variants_retired_skus_and_codes_without_digits_name_nothing(self):
        for sku in ('DAC47880055-KIT-FEB', '43512-52120-ANULADO', '43512-52120_7_DELETED', 'ABCDEF', '123', '', None):
            self.assertEqual(name_key(sku), '', sku)


class RetiredNameLinkTests(Fresh, TestCase):
    """S4 retires the name links: a number a line's codigo names is one of its References, or rule 3 proposes it (S6)."""

    def test_saving_a_sku_links_nothing_and_the_refresh_writes_nothing(self):
        ref = number('TOYOTA', '16100-79445')
        part = Part.objects.create(sku='16100-79445-NPW', description='BOMBA AGUA TOY HILUX')
        self.assertFalse(PartOEMLink.objects.exists())
        self.assertEqual(refresh_name_links(), {'skus': 0, 'links': 0, 'new': 0, 'removed': 0, 'retired': True})
        self.assertFalse(PartOEMLink.objects.exists())
        # name_targets still says which number a codigo names (read-only: S4's backfill lists the links it would have added).
        self.assertEqual(name_targets([(part.pk, part.sku, part.description, '')]), {str(part.pk): {str(ref.pk)}})

    def test_the_command_says_it_is_retired(self):
        out = io.StringIO()
        link_oem_numbers.main(['--dry-run'], stdout=out)
        self.assertIn('retirado con S4', out.getvalue())

    def test_a_number_under_sister_makes_is_named_under_each_unless_the_description_rules_one_out(self):
        refs = {maker: number(maker, '05085702AJ') for maker in ('DODGE', 'CHRYSLER', 'NISSAN')}
        rows = [('1', '05085702AJ-G', 'SENSOR', ''), ('2', '05085702AJ-NPW', 'SENSOR DODGE RAM', '')]
        found = name_targets(rows)
        self.assertEqual(found['1'], {str(ref.pk) for ref in refs.values()})
        self.assertEqual(found['2'], {str(refs['DODGE'].pk), str(refs['CHRYSLER'].pk)})


class StoreTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.ref = number('TOYOTA', '16100-29155')

    def test_cross_references_merge_spellings_and_citations(self):
        counts = store_cross_references({(self.ref.pk, 'aisin', 'wpt-111'): ['GMB 2016 · pág. 5'], (self.ref.pk, 'AISIN', 'WPT 111'): ['ASVA 2017 · pág. 9'],
                                         (self.ref.pk, 'GMB', 'GWT-101A'): ['GMB 2016 · pág. 5'], (self.ref.pk, 'AIRTEX', 'N/A'): ['GMB 2016'],
                                         (self.ref.pk, '', 'AW1698'): ['GMB 2016']})
        self.assertEqual(counts, {'new': 2, 'cited': 0, 'present': 0})
        aisin = OEMCrossReference.objects.get(brand='AISIN')
        self.assertEqual((aisin.code, aisin.number, aisin.citations, aisin.reference_id, aisin.legacy_reference_id),
                         ('WPT-111', 'WPT111', ['GMB 2016 · pág. 5', 'ASVA 2017 · pág. 9'], self.ref.pk, None))
        again = store_cross_references({(self.ref.pk, 'AISIN', 'WPT-111'): ['GMB 2016 · pág. 5', 'ATE 2017 · pág. 2']})
        self.assertEqual(again, {'new': 0, 'cited': 1, 'present': 0})
        self.assertEqual(OEMCrossReference.objects.get(brand='AISIN').citations, ['GMB 2016 · pág. 5', 'ASVA 2017 · pág. 9', 'ATE 2017 · pág. 2'])
        self.assertEqual(store_cross_references({(self.ref.pk, 'AISIN', 'WPT-111'): ['ATE 2017 · pág. 2']}), {'new': 0, 'cited': 0, 'present': 1})

    def test_dry_runs_count_without_writing(self):
        self.assertEqual(store_cross_references({(self.ref.pk, 'AISIN', 'WPT-111'): ['GMB']}, apply=False)['new'], 1)
        part = Part.objects.create(sku='BOMBA-1', description='BOMBA AGUA')
        self.assertEqual(store_catalog_links({(part.pk, self.ref.pk): ['GMB · GWT-101A']}, apply=False)['new'], 1)
        self.assertFalse(OEMCrossReference.objects.exists() or PartOEMLink.objects.exists())

    def test_constraints(self):
        row = OEMCrossReference.objects.create(reference=self.ref, brand=' aisin ', code=' wpt-111 ')
        self.assertEqual((row.brand, row.code, row.number), ('AISIN', 'WPT-111', 'WPT111'))
        for update in ({'number': 'WPT-111'}, {'brand': 'aisin'}, {'brand': ''}):
            with self.assertRaises(IntegrityError, msg=update), transaction.atomic():
                OEMCrossReference.objects.filter(pk=row.pk).update(**update)
        part = Part.objects.create(sku='BOMBA-1', description='BOMBA AGUA')
        with self.assertRaises(IntegrityError), transaction.atomic():
            PartOEMLink.objects.create(part=part, reference=self.ref, source='otro')


class EquivalentsTests(Fresh, APITestCase):
    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser('oem-links-root', 'oem-links-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)
        self.main, self.sister = number('TOYOTA', '16100-29155'), number('TOYOTA', '16100-29156')
        store_cross_references({(self.main.pk, 'AISIN', 'WPT-111'): ['GMB 2016 · pág. 5'], (self.main.pk, 'GMB', 'GWT-101A'): ['GMB 2016 · pág. 5'],
                                (self.sister.pk, 'AISIN', 'WPT-111'): ['GMB 2016 · pág. 5'], (self.sister.pk, 'AIRTEX', 'AW-1698'): ['ASVA 2017 · pág. 9']})
        self.part = Part.objects.create(sku='16100-29155-NPW', description='BOMBA AGUA TOY')
        references.add(self.part, 'GWT 101A', 'GMB', ref_type='company', source='GMB 2016 · pág. 5')
        store_catalog_links({(self.part.pk, self.sister.pk): ['GMB 2016 · pág. 5 · GWT-101A'], (self.part.pk, self.main.pk): ['GMB 2016 · pág. 5 · GWT-101A']})

    def test_a_sku_reaches_the_codes_of_all_its_numbers_once_each(self):
        data = part_equivalents(self.part)
        self.assertEqual([(str(n['reference']), n['via']) for n in data['numbers']],
                         [('TOYOTA 1610029155', ['catalog']), ('TOYOTA 1610029156', ['catalog'])])
        self.assertEqual(data['numbers'][1]['citations'], ['GMB 2016 · pág. 5 · GWT-101A'])
        codes = {(c['brand'], c['code']): c for c in data['cross_references']}
        self.assertEqual(sorted(codes), [('AIRTEX', 'AW-1698'), ('AISIN', 'WPT-111'), ('GMB', 'GWT-101A')])
        self.assertEqual(codes[('AISIN', 'WPT-111')]['numbers'], [self.main.uid, self.sister.uid])
        self.assertTrue(codes[('GMB', 'GWT-101A')]['on_sku'])  # the copy the catalog import also wrote as a company alterno
        self.assertFalse(codes[('AISIN', 'WPT-111')]['on_sku'])

    def test_a_number_on_the_line_counts_and_a_grouped_sku_reaches_nothing(self):
        references.add(self.part, '16100-29155', 'TOYOTA', tipo='oem', source='CATÁLOGO')  # absorbs the number's referencia line (3f)
        self.assertEqual(part_equivalents(self.part)['numbers'][0]['via'], ['oem', 'catalog'])
        target = Part.objects.create(sku='BOMBA-2', description='BOMBA AGUA')
        Part.objects.filter(pk=self.part.pk).update(merged_into=target, active=False, estado='unificada')
        self.part.refresh_from_db()
        self.assertEqual(part_equivalents(self.part), {'numbers': [], 'cross_references': []})

    def test_the_endpoint_is_read_only_and_for_superusers(self):
        url = f'/api/v1/management/catalog/{self.part.pk}/oem-equivalents/'
        data = self.client.get(url).json()
        self.assertEqual([(n['reference']['code'], n['reference']['id'], n['via']) for n in data['numbers']],
                         [('1610029155', str(self.main.uid), ['catalog']), ('1610029156', str(self.sister.uid), ['catalog'])])
        self.assertEqual(len(data['cross_references']), 3)
        self.assertEqual(self.client.post(url, {}, format='json').status_code, 405)
        self.assertEqual(self.client.get('/api/v1/management/catalog/00000000-0000-4000-8000-000000000001/oem-equivalents/').status_code, 404)
        staff = User.objects.create_user('oem-links-staff', 'oem-links-staff@example.invalid', 'Example938!')
        for user in (None, staff):
            self.client.force_authenticate(user)
            self.assertIn(self.client.get(url).status_code, (401, 403))

    def test_the_library_shows_the_codes_and_the_lines_linked_by_catalog(self):
        rows = {row['code']: row for row in self.client.get(URL).json()['results']}
        self.assertEqual((rows['1610029155']['cross_reference_count'], [c['code'] for c in rows['1610029155']['cross_reference_preview']]),
                         (2, ['WPT-111', 'GWT-101A']))
        self.assertEqual([(s['sku'], s['via']) for s in rows['1610029155']['linked_skus']], [('16100-29155-NPW', 'catalog')])
        self.assertEqual([(s['sku'], s['via']) for s in rows['1610029156']['linked_skus']], [('16100-29155-NPW', 'catalog')])
        detail = self.client.get(f'{URL}{self.sister.uid}/').json()
        self.assertEqual([(c['brand'], c['code'], c['citations']) for c in detail['cross_references']],
                         [('AIRTEX', 'AW-1698', ['ASVA 2017 · pág. 9']), ('AISIN', 'WPT-111', ['GMB 2016 · pág. 5'])])
        self.assertEqual(sorted(r['code'] for r in self.client.get(URL, {'q': 'wpt 111'}).json()['results']), ['1610029155', '1610029156'])
        self.assertEqual([r['code'] for r in self.client.get(URL, {'q': 'GWT-101A'}).json()['results']], ['1610029155'])
        lone = number('TOYOTA', '16100-00001')
        self.assertEqual([r['code'] for r in self.client.get(URL, {'linked': 'false'}).json()['results']], [lone.clave])
        self.assertEqual(linked_parts([lone])[lone.pk], [])
