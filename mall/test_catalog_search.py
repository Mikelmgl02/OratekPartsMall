from unittest.mock import patch

from rest_framework.test import APITestCase

from .models import Account, Membership, Part, PartCode, Role, SupplierItem, User
from . import references
from .references import add_library_number


class CatalogSearchTests(APITestCase):
    url = '/api/v1/catalog/'
    suggest = '/api/v1/catalog/suggest/'

    def setUp(self):
        self.user = User.objects.create_user('search-buyer', 'search-buyer@example.invalid', 'StrongPassword123!')
        buyer = Account.objects.create(name='CLIENTE BUSCA')
        buyer.roles.add(Role.objects.get(code='client_business'))
        Membership.objects.create(user=self.user, account=buyer)
        self.supplier = Account.objects.create(name='PROVEEDOR BUSCA')
        self.supplier.roles.add(*Role.objects.filter(capability='supplier'))
        self.client.force_authenticate(self.user)

    def part(self, sku, description, **fields):
        return Part.objects.create(sku=sku, description=description, **fields)

    def skus(self, search, **params):
        response = self.client.get(self.url, {'search': search, **params})
        self.assertEqual(response.status_code, 200)
        return [row['sku'] for row in response.data['results']]

    def test_every_word_matches_in_any_order_and_filler_words_are_ignored(self):
        self.part('15400-PLM-A02', 'FILTRO DE ACEITE HON CIVIC 06-11')
        self.part('90915-YZZE1', 'FILTRO ACEITE TOY COROLLA')
        self.part('17801-0T030', 'FILTRO AIRE TOY COROLLA')
        both = ['15400-PLM-A02', '90915-YZZE1']
        for search in ['FILTRO ACEITE', 'aceite filtro', 'filtro de aceite', '  Filtro   aceite ']:
            self.assertEqual(self.skus(search), both, search)
        self.assertEqual(self.skus('filtro aceite para toy'), ['90915-YZZE1'])
        self.assertEqual(self.skus('corolla filtro'), ['17801-0T030', '90915-YZZE1'])
        self.assertEqual(self.skus('filtro aceite mazda'), [])

    def test_accents_and_case_are_ignored_on_both_sides(self):
        self.part('BKR6E', 'Bujía NGK cobre')
        self.part('IFR6T11', 'BUJIA IRIDIO NGK')
        self.part('PIN-1', 'PIÑÓN DE ARRANQUE')
        for search in ['bujia', 'BUJÍA', 'Bujía']:
            self.assertEqual(self.skus(search), ['BKR6E', 'IFR6T11'], search)
        self.assertEqual(self.skus('bujia cobre'), ['BKR6E'])
        self.assertEqual(self.skus('pinon arranque'), ['PIN-1'])

    def test_codes_match_without_separators_on_skus_alternos_and_supplier_codes(self):
        self.part('51750-J9000', 'CUBO DE RUEDA DEL KIA')
        PartCode.objects.create(part=self.part('PAD-1', 'PASTILLA'), code='GWT-41A', brand='GMB')
        sensor = self.part('SENSOR-1', 'SENSOR ABS')
        SupplierItem.objects.create(part=sensor, supplier=self.supplier, supplier_invent_id='INV-1', codigo='58411-1R000-G', brand='HYUNDAI',
                                    source='upload', reported_quantity=3, matching_status='matched')
        for search, expected in [('51750J9000', ['51750-J9000']), ('51750 J9000', ['51750-J9000']), ('51750-j9000', ['51750-J9000']),
                                 ('gwt41a', ['PAD-1']), ('GWT 41A', ['PAD-1']), ('584111R000G', ['SENSOR-1']), ('58411 1R000 G', ['SENSOR-1'])]:
            self.assertEqual(self.skus(search), expected, search)

    def test_a_code_search_lists_the_exact_code_first_then_codes_containing_it_then_the_rest(self):
        self.part('0-0-MENCION', 'REEMPLAZA AL 51750-J9000')
        self.part('0-51750-J9000-KIT', 'KIT CUBO')
        self.part('51750-J9000', 'CUBO DE RUEDA')
        PartCode.objects.create(part=self.part('0-1-ALTERNO', 'CUBO GENERICO'), code='51750 J9000', brand='KIA')
        self.assertEqual(self.skus('51750-J9000'), ['0-1-ALTERNO', '51750-J9000', '0-51750-J9000-KIT', '0-0-MENCION'])
        self.assertEqual(self.skus('cubo'), ['0-1-ALTERNO', '0-51750-J9000-KIT', '51750-J9000'])  # words only: SKU order

    def test_an_oem_number_the_sku_reaches_counts_as_an_exact_code(self):
        add_library_number('TOYOTA', '16100-29155', source_kind='aftermarket_catalog', citation='CATÁLOGO GMB 2016')
        pump = self.part('BOMBA-AGUA', 'BOMBA DE AGUA')
        references.add(pump, '16100-29155', 'TOYOTA', tipo='oem')  # S9: the number on the line (a catalog link no longer reaches it)
        self.part('0-MENCION', 'EQUIVALE A 16100 29155')
        self.assertEqual(self.skus('16100 29155'), ['BOMBA-AGUA', '0-MENCION'])
        self.assertEqual(self.skus('1610029155'), ['BOMBA-AGUA'])  # descriptions match word by word, as written

    def test_reusing_the_matches_lists_and_counts_like_the_full_search(self):
        for index in range(5):
            self.part(f'DISCO-{index}', 'DISCO FRENO DEL', category='FRENOS' if index % 2 else '')
        self.part('PASTILLA-1', 'PASTILLA FRENO', category='FRENOS')
        reused = self.client.get(self.url, {'search': 'freno disco', 'include_facets': '1'}).data
        with patch('mall.views.REUSED_MATCHES', 2):  # more matches than that: every query runs the search again
            full = self.client.get(self.url, {'search': 'freno disco', 'include_facets': '1'}).data
        self.assertEqual(reused['count'], 5)
        self.assertEqual((reused['results'], reused['facets']), (full['results'], full['facets']))

    def test_suggestions_preview_the_first_results_of_the_search(self):
        for index in range(8):
            self.part(f'DISCO-{index}', 'DISCO FRENO', category='FRENOS' if index < 3 else '')
        self.part('51750-J9000', 'DISCO CUBO')
        response = self.client.get(self.suggest, {'search': 'disco freno'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['sku'] for row in response.data], [f'DISCO-{index}' for index in range(6)])
        self.assertIn('availability', response.data[0])
        self.assertEqual([row['sku'] for row in self.client.get(self.suggest, {'search': 'disco', 'category': 'FRENOS'}).data],
                         ['DISCO-0', 'DISCO-1', 'DISCO-2'])
        self.assertEqual(self.client.get(self.suggest, {'search': '51750j9000 disco'}).data[0]['sku'], '51750-J9000')
        self.assertEqual(self.client.get(self.suggest, {'search': '  '}).data, [])
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.suggest, {'search': 'disco'}).status_code, (401, 403))
