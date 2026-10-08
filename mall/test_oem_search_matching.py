from rest_framework.test import APITestCase

from .matching_engine import MatchIndex, reconcile_items
from .models import Account, Membership, MatchingCase, Part, PartCode, SupplierItem, User
from .oem_links import store_catalog_links, store_cross_references
from .oem_reference import upsert_reference
from .test_oem_finder import Fresh

PUMP = 'BOMBA AGUA TOY 22R HILUX'


def number(manufacturer, code):
    return upsert_reference(manufacturer, code, source_kind='aftermarket_catalog', citation='GMB 2016')[0]


class OEMLibraryTests(Fresh, APITestCase):
    """A SKU reached through the OEM library: its code names TOYOTA 16100-39315, and the GMB catalog ties it to the sister 16100-39317."""

    def setUp(self):
        super().setUp()
        self.main, self.sister = number('TOYOTA', '16100-39315'), number('TOYOTA', '16100-39317')
        store_cross_references({(self.main.pk, 'GMB', 'GWT-41A'): ['GMB 2016 · pág. 212'], (self.main.pk, 'NPW', 'T-16'): ['GMB 2016 · pág. 557'],
                                (self.main.pk, 'AISIN', 'WPT-111'): ['GMB 2016 · pág. 212'], (self.sister.pk, 'GMB', 'GWT-41A'): ['GMB 2016 · pág. 212']})
        self.pump = Part.objects.create(sku='16100-39315-RAZ', description=PUMP)  # name link to the main number
        store_catalog_links({(self.pump.pk, self.sister.pk): ['GMB 2016 · pág. 212 · GWT-41A']})
        self.supplier = Account.objects.create(name='PROVEEDOR NUEVO')

    def item(self, codigo, brand='', description=PUMP, references=()):
        return SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id=f'ID-{codigo}-{brand}', codigo=codigo, brand=brand,
                                           description=description, references=list(references), reported_quantity=3)

    def resolve(self, item):
        candidates, method, strong, reason = MatchIndex().resolve(item)
        return [row['sku'] for row in candidates], method, strong, reason


class LibraryMatchingTests(OEMLibraryTests):
    def test_an_aftermarket_code_links_the_one_sku_that_reaches_its_number(self):
        item = self.item('GWT-41A', 'GMB')
        self.assertEqual(self.resolve(item)[:3], (['16100-39315-RAZ'], 'aftermarket_code', True))
        self.assertEqual(reconcile_items(MatchIndex()), 1)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.matching_status), (self.pump.pk, 'matched'))
        self.assertEqual(MatchingCase.objects.get(item=item).method, 'aftermarket_code')
        self.assertFalse(PartCode.objects.filter(code='GWT-41A').exists())  # the supplier's code never becomes a universal alterno

    def test_a_short_code_must_be_printed_the_same_way_and_by_that_brand(self):
        self.assertEqual(self.resolve(self.item('T-16', 'NPW'))[1:3], ('aftermarket_code', True))
        skus, method, strong, reason = self.resolve(self.item('T16', 'NPW'))
        self.assertEqual((skus[:1], strong), (['16100-39315-RAZ'], False))  # a candidate for review, not an automatic link
        self.assertIn('código corto', reason)
        self.assertEqual(self.resolve(self.item('GWT-41A', 'AISIN'))[2], False)  # AISIN never printed GWT-41A

    def test_an_oem_number_the_catalog_tied_to_the_sku_links_it(self):
        self.assertEqual(self.resolve(self.item('16100-39317', 'TOYOTA'))[:3], (['16100-39315-RAZ'], 'linked_oem_number', True))
        self.assertEqual(self.resolve(self.item('1610039315'))[:3], (['16100-39315-RAZ'], 'linked_oem_number', True))

    def test_duplicate_skus_of_a_number_go_to_review_with_both(self):
        twin = Part.objects.create(sku='16100-39315-GMB', description=PUMP)
        skus, method, strong, reason = self.resolve(self.item('GWT-41A', 'GMB'))
        self.assertEqual((sorted(skus[:2]), method, strong), (['16100-39315-GMB', '16100-39315-RAZ'], 'needs_review', False))
        self.assertIn('posibles duplicados', reason)
        self.assertTrue(twin.pk)

    def test_an_alterno_or_a_sku_code_still_wins_over_the_library(self):
        owner = Part.objects.create(sku='BOMBA-GMB-41', description=PUMP)
        PartCode.objects.create(part=owner, brand='GMB', code='GWT-41A', ref_type='company')
        self.assertEqual(self.resolve(self.item('GWT-41A', 'GMB'))[:3], (['BOMBA-GMB-41'], 'known_code', True))
        exact = Part.objects.create(sku='16100-39317', description=PUMP)  # the sister number is a SKU of its own
        self.assertEqual(self.resolve(self.item('16100-39317', 'TOYOTA'))[:3], (['16100-39317'], 'known_code', True))
        self.assertTrue(exact.pk)

    def test_declared_references_resolve_through_the_library_when_no_alterno_carries_them(self):
        company = self.item('X-1', 'OTRA', references=[{'brand': 'AISIN', 'code': 'WPT-111', 'ref_type': 'company'}])
        self.assertEqual(self.resolve(company)[:3], (['16100-39315-RAZ'], 'approved_reference', True))
        oem = self.item('X-2', 'OTRA', references=[{'brand': 'LEXUS', 'code': '16100-39317', 'ref_type': 'oem'}])
        self.assertEqual(self.resolve(oem)[:3], (['16100-39315-RAZ'], 'approved_reference', True))  # LEXUS and TOYOTA are one make
        other_make = self.item('X-3', 'OTRA', references=[{'brand': 'NISSAN', 'code': '16100-39317', 'ref_type': 'oem'}])
        self.assertNotEqual(self.resolve(other_make)[1], 'approved_reference')

    def test_a_contradiction_blocks_the_automatic_link(self):
        skus, method, strong, reason = self.resolve(self.item('GWT-41A', 'GMB', description='KIT BOMBA AGUA TOY 22R'))
        self.assertEqual((skus[:1], strong), (['16100-39315-RAZ'], False))
        self.assertIn('especificaciones', reason)

    def test_a_grouped_sku_is_never_a_target(self):
        target = Part.objects.create(sku='16100-39315', description=PUMP)
        Part.objects.filter(pk=self.pump.pk).update(merged_into=target, active=False)
        skus, method, strong, _ = self.resolve(self.item('GWT-41A', 'GMB'))
        self.assertNotIn('16100-39315-RAZ', skus)


class LibrarySearchTests(OEMLibraryTests):
    def setUp(self):
        super().setUp()
        self.buyer = User.objects.create_user('oem-search-buyer', 'oem-search-buyer@example.invalid', 'StrongPassword123!')
        Membership.objects.create(user=self.buyer, account=Account.objects.create(name='CLIENTE'))
        self.root = User.objects.create_superuser('oem-search-root', 'oem-search-root@example.invalid', 'Example938!')
        self.other = Part.objects.create(sku='90915-YZZD2', description='FILTRO ACEITE TOY')

    def skus(self, url, search, user):
        self.client.force_authenticate(user)
        response = self.client.get(url, {'search': search})
        self.assertEqual(response.status_code, 200)
        return [row['sku'] for row in response.json()['results']]

    def test_the_catalog_finds_a_sku_by_an_aftermarket_code_or_an_oem_number_it_reaches(self):
        for url, user in (('/api/v1/catalog/', self.buyer), ('/api/v1/management/catalog/', self.root)):
            for search in ('GWT-41A', 'gwt41a', 'WPT 111', '16100-39317', '16100 39317', '1610039315'):
                self.assertEqual(self.skus(url, search, user), ['16100-39315-RAZ'], (url, search))
            self.assertEqual(self.skus(url, 'YZZD2', user), ['90915-YZZD2'])  # the usual fields still match
            self.assertEqual(self.skus(url, 'T16', user), [])  # three characters do not look through the library

    def test_a_grouped_or_inactive_sku_is_not_found_by_the_customer(self):
        Part.objects.filter(pk=self.pump.pk).update(active=False)
        self.assertEqual(self.skus('/api/v1/catalog/', 'GWT-41A', self.buyer), [])
        self.assertEqual(self.skus('/api/v1/management/catalog/', 'GWT-41A', self.root), ['16100-39315-RAZ'])  # admins see inactive SKUs
