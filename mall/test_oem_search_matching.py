from rest_framework.test import APITestCase

from . import reference_matching as rm
from . import references
from .lines import create_line
from .models import Account, Membership, MatchingCase, Part, Role, SupplierItem, User
from .oem_links import store_cross_references
from .references import add_library_number
from .test_oem_finder import Fresh
from .services import ingest_inventory

PUMP = 'BOMBA AGUA TOY 22R HILUX'


def number(manufacturer, code):
    """A library number (S4: a Reference, on a line of its own in referencia when no line carries it)."""
    return add_library_number(manufacturer, code, source_kind='aftermarket_catalog', citation='GMB 2016')[0]


class OEMLibraryTests(Fresh, APITestCase):
    """A SKU reached through the OEM library: its code names TOYOTA 16100-39315, and the GMB catalog prints the sister 16100-39317 for
    the same item. From S9 (mall.merge_pass) the SKU holds both numbers (the pass merges a catalog item's numbers into its SKU's line);
    the name and catalog links are no longer read."""

    def setUp(self):
        super().setUp()
        self.main, self.sister = number('TOYOTA', '16100-39315'), number('TOYOTA', '16100-39317')
        store_cross_references({(self.main.pk, 'GMB', 'GWT-41A'): ['GMB 2016 · pág. 212'], (self.main.pk, 'NPW', 'T-16'): ['GMB 2016 · pág. 557'],
                                (self.main.pk, 'AISIN', 'WPT-111'): ['GMB 2016 · pág. 212'], (self.sister.pk, 'GMB', 'GWT-41A'): ['GMB 2016 · pág. 212']})
        self.pump = Part.objects.create(sku='16100-39315-RAZ', description=PUMP)
        for code in ('16100-39315', '16100-39317'):  # each absorbs its number's line in referencia (3f)
            references.add(self.pump, code, 'TOYOTA', tipo='oem')
        self.supplier = Account.objects.create(name='PROVEEDOR NUEVO')

    def item(self, codigo, brand='', description=PUMP, references=()):
        return SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id=f'ID-{codigo}-{brand}', codigo=codigo, brand=brand,
                                           description=description, references=list(references), reported_quantity=3)



GMB = 'GMB Water Pump Catalog 2016 · pág. 212'


class LibraryMatchingTests(Fresh, APITestCase):
    """S6's 3e-lazy (until S9): a branded aftermarket code a catalog files under OEM numbers joins the one line its numbers' References sit
    on, as a Reference of that line (declarado for the catalog's own brand, inferido for a competitor's from a PDF-checked catalog);
    the numbers on two or more lines wait in a line case (and a merge case). The SKU links of the library (PartOEMLink) are S9's."""

    def setUp(self):
        super().setUp()
        self.pump = create_line('16100-39315-RAZ', description=PUMP)
        references.add(self.pump, '16100-39315', 'TOYOTA', tipo='oem')  # the line carries the number: add_library_number files it there
        self.main = number('TOYOTA', '16100-39315')
        store_cross_references({(self.main.pk, 'GMB', 'GWT-41A'): [GMB], (self.main.pk, 'NPW', 'T-16'): ['GMB Water Pump Catalog 2016 · pág. 557'],
                                (self.main.pk, 'AISIN', 'WPT-111'): [GMB], (self.main.pk, 'DENSO', 'DW-9'): ['OTRO CATALOGO 2019 · pág. 1']})
        self.supplier = Account.objects.create(name='PROVEEDOR NUEVO')
        self.actor = User.objects.create_user('lib-loader', 'lib-loader@example.invalid', 'Example938!')

    def resolve(self, codigo, brand='', description=PUMP):
        return rm.resolve(rm.subject_of(None, {'codigo': codigo, 'brand': brand, 'description': description}, self.supplier))

    def test_an_aftermarket_code_joins_the_one_line_of_its_numbers(self):
        self.assertEqual(self.main.part_id, self.pump.pk)
        found = self.resolve('GWT-41A', 'GMB')
        self.assertEqual((found.action, found.rule, found.line, found.new['estado']), ('new_reference_on_line', '3e', self.pump, 'declarado'))
        competitor = self.resolve('WPT-111', 'AISIN')
        self.assertEqual((competitor.line, competitor.new['estado']), (self.pump, 'inferido'))  # a PDF-checked catalog (OD17)
        self.assertEqual(self.resolve('DW-9', 'DENSO').action, 'case')  # an unchecked catalog's competitor code waits for a person
        item = ingest_inventory(supplier=self.supplier, actor=self.actor, data={'update_id': 'u-1', 'supplier_invent_id': 'GWT', 'codigo': 'GWT-41A',
                                                                           'brand': 'GMB', 'description': PUMP, 'source': 'upload', 'quantity': 3})
        self.assertEqual((item.part_id, item.matching_status, item.reference.code, item.reference.tipo), (self.pump.pk, 'matched', 'GWT-41A', 'aftermarket'))
        self.assertTrue(item.reference.sources.filter(kind='catalogo', citation=GMB).exists())

    def test_a_short_code_must_be_printed_the_same_way_and_by_that_brand(self):
        self.assertEqual(self.resolve('T-16', 'NPW').line, self.pump)
        self.assertEqual(self.resolve('T16', 'NPW').action, 'no_line')
        self.assertEqual(self.resolve('GWT-41A', 'AISIN').action, 'no_line')  # AISIN never printed GWT-41A

    def test_numbers_on_two_lines_wait_in_a_line_case_and_a_merge_case(self):
        sister = number('TOYOTA', '16100-39317')  # a line of its own in referencia
        store_cross_references({(sister.pk, 'GMB', 'GWT-41A'): [GMB]})
        found = self.resolve('GWT-41A', 'GMB')
        self.assertEqual((found.action, [spec['kind'] for spec in found.cases]), ('case', ['line', 'merge']))
        self.assertEqual(set(found.cases[1]['lines']), {self.pump.pk, sister.part_id})

    def test_a_reference_of_a_line_wins_over_the_library(self):
        owner = create_line('BOMBA-GMB-41', description=PUMP)
        references.add(owner, 'GWT-41A', 'GMB', tipo='aftermarket')
        self.assertEqual((self.resolve('GWT-41A', 'GMB').action, self.resolve('GWT-41A', 'GMB').line), ('attach', owner))

    def test_declared_references_are_cases_never_links(self):
        item = ingest_inventory(supplier=self.supplier, actor=self.actor, data={'update_id': 'u-2', 'supplier_invent_id': 'X-1', 'codigo': 'X-1',
                                                                           'brand': 'OTRA', 'description': PUMP, 'source': 'upload', 'quantity': 1,
                                                                           'references': [{'brand': 'AISIN', 'code': 'WPT-111', 'ref_type': 'company'}]})
        self.assertNotEqual(item.part_id, self.pump.pk)
        self.assertTrue(MatchingCase.objects.filter(item=item, kind='reference', new_code='WPT-111').exists())

    def test_a_contradiction_hangs_the_item_in_review(self):
        found = self.resolve('GWT-41A', 'GMB', description='KIT BOMBA AGUA TOY 22R')
        self.assertEqual((found.action, found.status, [spec['kind'] for spec in found.cases]), ('new_reference_on_line', 'review', ['variant']))

    def test_a_grouped_line_is_never_a_target(self):
        from .line_merge import merge_lines
        target = create_line('16100-39315-X', description=PUMP)
        merge_lines(target, [self.pump], actor=User.objects.create_superuser('lib-root', 'lib-root@example.invalid', 'Example938!'))
        self.assertEqual(self.resolve('GWT-41A', 'GMB').line, target)  # the number went with its line


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
            self.assertEqual(self.skus(url, 'T16', user), [])  # three characters do not look through the library

    def test_the_catalog_lists_the_codes_a_sku_reaches_through_its_numbers(self):
        self.client.force_authenticate(self.buyer)
        rows = {row['sku']: row for row in self.client.get('/api/v1/catalog/', {'search': 'BOMBA'}).json()['results']}
        self.assertEqual(rows['16100-39315-RAZ']['equivalents'], [{'brand': 'AISIN', 'code': 'WPT-111'}, {'brand': 'GMB', 'code': 'GWT-41A'},
                                                               {'brand': 'NPW', 'code': 'T-16'}])  # each code once, from both numbers
        self.assertEqual([code['code'] for code in rows['16100-39315-RAZ']['codes']], ['16100-39315', '16100-39317'])  # its OEM numbers

    def test_a_grouped_or_inactive_sku_is_not_found_by_the_customer(self):
        Part.objects.filter(pk=self.pump.pk).update(active=False, estado='retirada')
        self.assertEqual(self.skus('/api/v1/catalog/', 'GWT-41A', self.buyer), [])
        self.assertEqual(self.skus('/api/v1/management/catalog/', 'GWT-41A', self.root), ['16100-39315-RAZ'])  # admins see inactive SKUs
