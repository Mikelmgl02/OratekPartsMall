import io
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APITestCase

from . import link_oem_numbers, oem_links
from .models import Part, PartCode, User
from .oem_links import name_key, part_equivalents, refresh_name_links, store_catalog_links, store_cross_references
from .oem_reference import linked_parts, upsert_reference
from .oem_reference_models import OEMCrossReference, OEMReference, PartOEMLink
from .test_oem_finder import Fresh

URL = '/api/v1/management/oem-references/'


def number(manufacturer, code, citation='CATÁLOGO GMB 2016'):
    return upsert_reference(manufacturer, code, source_kind='aftermarket_catalog', citation=citation)[0]


def links(part, source='sku_name'):
    return sorted(str(ref) for ref in OEMReference.objects.filter(part_links__part=part, part_links__source=source))


class NameKeyTests(Fresh, TestCase):
    def test_the_code_or_the_code_before_its_tags_names_the_number(self):
        for sku, key in (('16100-29155', '1610029155'), ('16100-79445-NPW', '1610079445'), ('16100-39466-G', '1610039466'),
                         ('90919-01164 DENSO', '9091901164'), ('04465-0K240-AISIN', '044650K240')):
            self.assertEqual(name_key(sku), key, sku)

    def test_variants_retired_skus_and_codes_without_digits_name_nothing(self):
        for sku in ('DAC47880055-KIT-FEB', '43512-52120-ANULADO', '43512-52120_7_DELETED', 'ABCDEF', '123', '', None):
            self.assertEqual(name_key(sku), '', sku)


class NameLinkTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.ref = number('TOYOTA', '16100-79445')

    def test_a_new_sku_links_to_the_number_its_code_names_without_becoming_oem(self):
        part = Part.objects.create(sku='16100-79445-NPW', description='BOMBA AGUA TOY HILUX')
        self.assertEqual(links(part), ['TOYOTA 1610079445'])
        part.refresh_from_db()
        self.assertEqual((part.sku, part.is_OEM), ('16100-79445-NPW', False))
        self.assertFalse(part.codes.exists())  # no OEM alterno: reconcile_identities has nothing to promote

    def test_renaming_or_grouping_a_sku_refreshes_its_link(self):
        part = Part.objects.create(sku='16100-79445-NPW', description='BOMBA AGUA TOY HILUX')
        number('TOYOTA', '16100-69205')
        part.sku = '16100-69205-RAZ'
        part.save()
        self.assertEqual(links(part), ['TOYOTA 1610069205'])
        target = Part.objects.create(sku='BOMBA-AGUA-1', description='BOMBA AGUA')
        part.merged_into, part.active = target, False
        part.save(update_fields=['merged_into', 'active'])
        self.assertEqual(links(part), [])

    def test_saves_that_touch_neither_the_code_nor_the_grouping_skip_the_refresh(self):
        part = Part.objects.create(sku='16100-79445-NPW', description='BOMBA AGUA TOY HILUX')
        with patch('mall.oem_links.refresh_name_links') as refresh:
            part.description = 'BOMBA AGUA TOY HILUX 2KD'
            part.save()
            part.save(update_fields=['description'])
        refresh.assert_not_called()

    def test_numbers_appear_later_and_the_refresh_adds_and_drops_links(self):
        part = Part.objects.create(sku='17801-30070-G', description='FILTRO AIRE TOY HILUX')
        self.assertEqual(links(part), [])
        number('TOYOTA', '17801-30070')
        self.assertEqual(refresh_name_links(apply=False), {'skus': 1, 'links': 1, 'new': 1, 'removed': 0})
        self.assertEqual(links(part), [])  # a dry run writes nothing
        self.assertEqual(refresh_name_links()['new'], 1)
        self.assertEqual(refresh_name_links(), {'skus': 1, 'links': 1, 'new': 0, 'removed': 0})  # idempotent
        Part.objects.filter(pk=part.pk).update(sku='17801-OTRO')  # a queryset update skips the signal: the next refresh fixes it
        self.assertEqual(refresh_name_links()['removed'], 1)
        self.assertEqual(links(part), [])

    def test_the_command_refreshes_every_sku(self):
        part = Part.objects.create(sku='17801-30070-G', description='FILTRO AIRE TOY HILUX')
        number('TOYOTA', '17801-30070')
        out = io.StringIO()
        link_oem_numbers.main(['--dry-run'], stdout=out)
        self.assertIn('1 vínculos (1 nuevos, 0 retirados)', out.getvalue())
        self.assertEqual(links(part), [])
        link_oem_numbers.main(['--apply'], stdout=io.StringIO())
        self.assertEqual(links(part), ['TOYOTA 1780130070'])

    def test_a_number_under_sister_makes_links_each_unless_the_description_rules_one_out(self):
        number('DODGE', '05085702AJ')
        number('CHRYSLER', '05085702AJ')
        number('NISSAN', '05085702AJ')
        plain = Part.objects.create(sku='05085702AJ-G', description='SENSOR')
        dodge = Part.objects.create(sku='05085702AJ-NPW', description='SENSOR DODGE RAM')
        self.assertEqual(links(plain), ['CHRYSLER 05085702AJ', 'DODGE 05085702AJ', 'NISSAN 05085702AJ'])
        self.assertEqual(links(dodge), ['CHRYSLER 05085702AJ', 'DODGE 05085702AJ'])

    def test_a_failed_refresh_is_logged_and_never_breaks_the_save(self):
        with patch('mall.oem_links.name_targets', side_effect=RuntimeError('fallo')), self.assertLogs('mall.oem_links', level='ERROR'):
            part = Part.objects.create(sku='16100-79445-NPW', description='BOMBA AGUA')
        self.assertTrue(Part.objects.filter(pk=part.pk).exists())

    def test_before_the_migration_the_save_works_and_warns_once(self):
        oem_links._state['warned'] = False
        with patch.object(PartOEMLink._meta, 'db_table', 'mall_partoemlink_not_migrated'):
            with self.assertLogs('mall.oem_links', level='WARNING') as logs:
                Part.objects.create(sku='16100-79445-NPW', description='BOMBA AGUA')
                Part.objects.create(sku='16100-79445-RAZ', description='BOMBA AGUA')
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(Part.objects.count(), 2)


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
        self.assertEqual((aisin.code, aisin.number, aisin.citations), ('WPT-111', 'WPT111', ['GMB 2016 · pág. 5', 'ASVA 2017 · pág. 9']))
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
        with self.assertRaises(IntegrityError), transaction.atomic():
            OEMCrossReference.objects.create(reference=self.ref, brand='AISIN', code='WPT 111')
        part = Part.objects.create(sku='BOMBA-1', description='BOMBA AGUA')
        PartOEMLink.objects.create(part=part, reference=self.ref, source='catalog')
        with self.assertRaises(IntegrityError), transaction.atomic():
            PartOEMLink.objects.create(part=part, reference=self.ref, source='catalog')
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
        PartCode.objects.create(part=self.part, brand='GMB', code='GWT 101A', ref_type='company', reference_source='GMB 2016 · pág. 5')
        store_catalog_links({(self.part.pk, self.sister.pk): ['GMB 2016 · pág. 5 · GWT-101A'], (self.part.pk, self.main.pk): ['GMB 2016 · pág. 5 · GWT-101A']})

    def test_a_sku_reaches_the_codes_of_all_its_numbers_once_each(self):
        data = part_equivalents(self.part)
        self.assertEqual([(str(n['reference']), n['via']) for n in data['numbers']],
                         [('TOYOTA 1610029155', ['sku_name', 'catalog']), ('TOYOTA 1610029156', ['catalog'])])  # strongest first
        self.assertEqual(data['numbers'][1]['citations'], ['GMB 2016 · pág. 5 · GWT-101A'])
        codes = {(c['brand'], c['code']): c for c in data['cross_references']}
        self.assertEqual(sorted(codes), [('AIRTEX', 'AW-1698'), ('AISIN', 'WPT-111'), ('GMB', 'GWT-101A')])
        self.assertEqual(codes[('AISIN', 'WPT-111')]['numbers'], [self.main.pk, self.sister.pk])
        self.assertTrue(codes[('GMB', 'GWT-101A')]['on_sku'])  # the copy the catalog import also wrote as a company alterno
        self.assertFalse(codes[('AISIN', 'WPT-111')]['on_sku'])

    def test_an_oem_alterno_counts_and_a_grouped_sku_reaches_nothing(self):
        PartCode.objects.create(part=self.part, brand='TOYOTA', code='16100-29155', ref_type='oem', reference_source='CATÁLOGO')
        self.assertEqual(part_equivalents(self.part)['numbers'][0]['via'], ['oem', 'sku_name', 'catalog'])
        target = Part.objects.create(sku='BOMBA-2', description='BOMBA AGUA')
        Part.objects.filter(pk=self.part.pk).update(merged_into=target, active=False)
        self.part.refresh_from_db()
        self.assertEqual(part_equivalents(self.part), {'numbers': [], 'cross_references': []})

    def test_the_endpoint_is_read_only_and_for_superusers(self):
        url = f'/api/v1/management/catalog/{self.part.pk}/oem-equivalents/'
        data = self.client.get(url).json()
        self.assertEqual([(n['reference']['code'], n['via']) for n in data['numbers']], [('1610029155', ['sku_name', 'catalog']), ('1610029156', ['catalog'])])
        self.assertEqual(len(data['cross_references']), 3)
        self.assertEqual(self.client.post(url, {}, format='json').status_code, 405)
        self.assertEqual(self.client.get('/api/v1/management/catalog/00000000-0000-4000-8000-000000000001/oem-equivalents/').status_code, 404)
        staff = User.objects.create_user('oem-links-staff', 'oem-links-staff@example.invalid', 'Example938!')
        for user in (None, staff):
            self.client.force_authenticate(user)
            self.assertIn(self.client.get(url).status_code, (401, 403))

    def test_the_library_shows_the_codes_and_the_skus_linked_by_name_or_catalog(self):
        rows = {row['code']: row for row in self.client.get(URL).json()['results']}
        self.assertEqual((rows['1610029155']['cross_reference_count'], [c['code'] for c in rows['1610029155']['cross_reference_preview']]),
                         (2, ['WPT-111', 'GWT-101A']))
        self.assertEqual([(s['sku'], s['via']) for s in rows['1610029155']['linked_skus']], [('16100-29155-NPW', 'sku_name')])
        self.assertEqual([(s['sku'], s['via']) for s in rows['1610029156']['linked_skus']], [('16100-29155-NPW', 'catalog')])
        detail = self.client.get(f'{URL}{self.sister.pk}/').json()
        self.assertEqual([(c['brand'], c['code'], c['citations']) for c in detail['cross_references']],
                         [('AIRTEX', 'AW-1698', ['ASVA 2017 · pág. 9']), ('AISIN', 'WPT-111', ['GMB 2016 · pág. 5'])])
        self.assertEqual(sorted(r['code'] for r in self.client.get(URL, {'q': 'wpt 111'}).json()['results']), ['1610029155', '1610029156'])
        self.assertEqual([r['code'] for r in self.client.get(URL, {'q': 'GWT-101A'}).json()['results']], ['1610029155'])
        lone = number('TOYOTA', '16100-00001')
        self.assertEqual([r['code'] for r in self.client.get(URL, {'linked': 'false'}).json()['results']], [lone.code])
        self.assertEqual(linked_parts([lone])[lone.pk], [])
