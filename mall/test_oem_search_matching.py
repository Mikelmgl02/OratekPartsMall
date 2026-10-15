from rest_framework.test import APITestCase

from . import reference_matching as rm
from . import references
from .lines import create_line
from .models import Account, Membership, MatchingCase, Part, Role, SupplierItem, User
from .references import add_library_number
from .test_oem_finder import Fresh
from .services import ingest_inventory

PUMP = 'BOMBA AGUA TOY 22R HILUX'


def number(manufacturer, code):
    """A library number (S4: a Reference, on a line of its own in referencia when no line carries it)."""
    return add_library_number(manufacturer, code, source_kind='aftermarket_catalog', citation='GMB 2016')[0]


class OEMLibraryTests(Fresh, APITestCase):
    """A SKU reached through the OEM library: its code names TOYOTA 16100-39315, and the GMB catalog prints the sister 16100-39317 for
    the same item. From S9 (mall.merge_pass) the SKU holds both numbers (the pass merges a catalog item's numbers into its SKU's line)
    and the codes the catalog files under them are its aftermarket References (S10: nothing else reaches it)."""

    def setUp(self):
        super().setUp()
        self.main, self.sister = number('TOYOTA', '16100-39315'), number('TOYOTA', '16100-39317')
        self.pump = Part.objects.create(sku='16100-39315-RAZ', description=PUMP)
        for code in ('16100-39315', '16100-39317'):  # each absorbs its number's line in referencia (3f)
            references.add(self.pump, code, 'TOYOTA', tipo='oem')
        for brand, code, citation in (('GMB', 'GWT-41A', 'GMB 2016 · pág. 212'), ('NPW', 'T-16', 'GMB 2016 · pág. 557'),
                                      ('AISIN', 'WPT-111', 'GMB 2016 · pág. 212')):
            references.add(self.pump, code, brand, tipo='aftermarket', source=citation)
        self.supplier = Account.objects.create(name='PROVEEDOR NUEVO')

    def item(self, codigo, brand='', description=PUMP, references=()):
        return SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id=f'ID-{codigo}-{brand}', codigo=codigo, brand=brand,
                                           description=description, references=list(references), reported_quantity=3)


GMB = 'GMB Water Pump Catalog 2016 · pág. 212'


class LibrarySearchTests(OEMLibraryTests):
    def setUp(self):
        super().setUp()
        self.buyer = User.objects.create_user('oem-search-buyer', 'oem-search-buyer@example.invalid', 'StrongPassword123!')
        buyer = Account.objects.create(name='CLIENTE')
        buyer.roles.add(Role.objects.get(code='client_business'))
        Membership.objects.create(user=self.buyer, account=buyer)
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
            # S10: T-16 is one of the line's own codes, so the storefront's document finds it; the admin grid looks for codes from 4 characters.
            self.assertEqual(self.skus(url, 'T16', user), ['16100-39315-RAZ'] if user == self.buyer else [])

    def test_the_catalog_lists_the_codes_a_sku_reaches_through_its_numbers(self):
        self.client.force_authenticate(self.buyer)
        rows = {row['sku']: row for row in self.client.get('/api/v1/catalog/', {'search': 'BOMBA'}).json()['results']}
        self.assertEqual(rows['16100-39315-RAZ']['equivalents'], [{'brand': 'AISIN', 'code': 'WPT-111'}, {'brand': 'GMB', 'code': 'GWT-41A'},
                                                               {'brand': 'NPW', 'code': 'T-16'}])  # each code once, from both numbers
        self.assertEqual([code['code'] for code in rows['16100-39315-RAZ']['codes']],
                         ['16100-39315', '16100-39317', 'GWT-41A', 'T-16', 'WPT-111'])  # its OEM numbers and its aftermarket References

    def test_a_grouped_or_inactive_sku_is_not_found_by_the_customer(self):
        Part.objects.filter(pk=self.pump.pk).update(estado='retirada')
        self.assertEqual(self.skus('/api/v1/catalog/', 'GWT-41A', self.buyer), [])
        self.assertEqual(self.skus('/api/v1/management/catalog/', 'GWT-41A', self.root), ['16100-39315-RAZ'])  # admins see inactive SKUs
