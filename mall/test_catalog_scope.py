import uuid

from django.test import override_settings
from rest_framework.test import APITestCase

from .analytics_models import UsageEvent
from .models import Account, Membership, Part, Role, SupplierItem, User, WishlistItem


@override_settings(CATALOG_LOW_STOCK_THRESHOLD=5, CATALOG_HIGH_STOCK_THRESHOLD=20)
class CatalogScopeTests(APITestCase):
    """A supplier-only member browses only the SKUs its own matched items reached, with its own stock, and learns nothing of the other
    suppliers; a client member browses everything."""
    url = '/api/v1/catalog/'

    def account(self, name, *capabilities):
        account = Account.objects.create(name=name)
        account.roles.add(*Role.objects.filter(capability__in=capabilities))
        return account

    def member(self, username, *accounts):
        user = User.objects.create_user(username, f'{username}@example.invalid', 'StrongPassword123!')
        for account in accounts:
            Membership.objects.create(user=user, account=account)
        return user

    def item(self, supplier, part, codigo, units, status='matched'):
        return SupplierItem.objects.create(supplier=supplier, part=part if status == 'matched' else None, codigo=codigo, brand='MARCA',
                                           supplier_invent_id=f'ITEM-{SupplierItem.objects.count()}', source='upload',
                                           reported_quantity=units, matching_status=status)

    def setUp(self):
        self.own = self.account('PROVEEDOR PROPIO', 'supplier')
        self.rival = self.account('PROVEEDOR RIVAL', 'supplier')
        self.buyer = self.account('CLIENTE', 'client')
        self.shared = Part.objects.create(sku='SHARED-1', description='DISCO DE FRENO', category='FRENOS')
        self.mine = Part.objects.create(sku='MINE-1', description='FILTRO DE ACEITE', category='FILTROS')
        self.theirs = Part.objects.create(sku='THEIRS-1', description='PASTILLA DE FRENO', category='FRENOS')
        self.nobody = Part.objects.create(sku='NOBODY-1', description='BUJIA', category='ENCENDIDO')
        self.item(self.own, self.shared, 'OWN-SHARED', 2)
        self.item(self.own, self.mine, 'OWN-MINE', 0)
        self.item(self.own, None, 'OWN-PENDING', 9, status='pending')
        self.item(self.rival, self.shared, 'RIVAL-SHARED-77', 50)
        self.item(self.rival, self.theirs, 'RIVAL-THEIRS', 8)
        self.supplier_user = self.member('scope-supplier', self.own)
        self.client.force_authenticate(self.supplier_user)

    def skus(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.data)
        return [row['sku'] for row in response.data['results']]

    def test_a_supplier_only_member_lists_only_its_own_matched_skus(self):
        response = self.client.get(self.url, {'include_facets': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['sku'] for row in response.data['results']], ['MINE-1', 'SHARED-1'])
        self.assertEqual(response.data['count'], 2)
        facets = response.data['facets']
        self.assertEqual(facets['supplier'], [])
        self.assertEqual({row['value']: row['count'] for row in facets['category']}, {'FILTROS': 1, 'FRENOS': 1})
        self.assertEqual(self.skus(search='freno'), ['SHARED-1'])
        self.assertEqual(self.skus(search='bujia'), [])
        self.assertEqual(self.skus(category='ENCENDIDO'), [])

    def test_its_availability_is_its_own_stock_alone(self):
        response = self.client.get(self.url, {'include_facets': '1'})
        availability = {row['sku']: row['availability'] for row in response.data['results']}
        # The rival's 50 units on SHARED-1 would make it 'high' with two suppliers.
        self.assertEqual((availability['SHARED-1']['status'], availability['SHARED-1']['supplier_count']), ('low', 1))
        self.assertEqual((availability['MINE-1']['status'], availability['MINE-1']['supplier_count']), ('sold_out', 0))
        bands = {row['value']: row['count'] for row in response.data['facets']['availability']}
        self.assertEqual((bands['high'], bands['low'], bands['sold_out']), (0, 1, 1))
        self.assertEqual(self.skus(availability='high'), [])
        self.assertEqual(self.skus(availability='in_stock'), ['SHARED-1'])

    def test_another_suppliers_filter_and_codes_reveal_nothing(self):
        response = self.client.get(self.url, {'supplier': str(self.rival.pk), 'include_facets': '1'})
        self.assertEqual([row['sku'] for row in response.data['results']], ['MINE-1', 'SHARED-1'])
        self.assertEqual(response.data['facets']['supplier'], [])
        self.assertNotIn('PROVEEDOR RIVAL', str(response.data))
        self.assertEqual(self.skus(search='RIVAL-SHARED-77'), [])
        self.assertEqual(self.skus(search='RIVAL'), [])
        self.assertEqual(self.skus(search='OWN-SHARED'), ['SHARED-1'])
        suggestions = self.client.get('/api/v1/catalog/suggest/', {'search': 'freno'})
        self.assertEqual([row['sku'] for row in suggestions.data], ['SHARED-1'])
        self.assertEqual(self.client.get('/api/v1/catalog/suggest/', {'search': 'RIVAL-SHARED-77'}).data, [])

    def test_the_technical_sheet_outside_the_scope_does_not_exist(self):
        self.assertEqual(self.client.get(f'/api/v1/catalog/{self.mine.pk}/technical/').status_code, 200)
        self.assertEqual(self.client.get(f'/api/v1/catalog/{self.theirs.pk}/technical/').status_code, 404)
        self.assertEqual(self.client.get(f'/api/v1/catalog/{self.nobody.pk}/technical/').status_code, 404)

    def test_a_client_member_and_a_member_of_both_kinds_browse_everything(self):
        self.client.force_authenticate(self.member('scope-buyer', self.buyer))
        everything = ['MINE-1', 'NOBODY-1', 'SHARED-1', 'THEIRS-1']
        self.assertEqual(self.skus(), everything)
        response = self.client.get(self.url, {'include_facets': '1', 'search': 'SHARED-1'})
        self.assertEqual(response.data['results'][0]['availability']['supplier_count'], 2)
        self.assertEqual({row['label'] for row in response.data['facets']['supplier']}, {'PROVEEDOR PROPIO', 'PROVEEDOR RIVAL'})
        self.assertEqual(self.client.get(f'/api/v1/catalog/{self.theirs.pk}/technical/').status_code, 200)
        both = self.member('scope-both', self.buyer, self.own)
        self.client.force_authenticate(both)
        self.assertEqual(self.skus(), everything)
        # ?account= narrows to the catalog one of the user's accounts sees.
        self.assertEqual(self.skus(account=str(self.own.pk)), ['MINE-1', 'SHARED-1'])
        self.assertEqual(self.skus(account=str(self.buyer.pk)), everything)
        self.assertEqual(self.client.get(f'/api/v1/catalog/{self.theirs.pk}/technical/', {'account': str(self.own.pk)}).status_code, 404)

    def test_the_account_parameter_never_widens_the_scope(self):
        self.assertEqual(self.client.get(self.url, {'account': str(self.buyer.pk)}).status_code, 404)
        self.assertEqual(self.client.get(self.url, {'account': str(self.rival.pk)}).status_code, 404)
        self.assertEqual(self.client.get(self.url, {'account': 'not-a-uuid'}).status_code, 400)
        self.own.active = False
        self.own.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_an_account_without_a_role_or_no_account_sees_nothing(self):
        self.client.force_authenticate(self.member('scope-roleless', Account.objects.create(name='SIN ROL')))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data['detail']), 'Tu administrador debe configurar tu cuenta.')
        self.assertEqual(self.client.get('/api/v1/catalog/suggest/', {'search': 'freno'}).status_code, 403)
        self.assertEqual(self.client.get(f'/api/v1/catalog/{self.mine.pk}/technical/').status_code, 403)
        self.client.force_authenticate(self.member('scope-nobody'))
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_its_favorites_stay_within_its_own_skus(self):
        self.assertEqual(self.client.put(f'/api/v1/wishlist/{self.theirs.pk}/').status_code, 404)
        self.assertEqual(self.client.put(f'/api/v1/wishlist/{self.mine.pk}/').status_code, 200)
        WishlistItem.objects.create(user=self.supplier_user, part=self.nobody)  # saved before the scope existed
        listed = self.client.get('/api/v1/wishlist/')
        self.assertEqual([row['part']['sku'] for row in listed.data['results']], ['MINE-1'])
        self.assertEqual(self.client.get('/api/v1/wishlist/state/').data, {'part_ids': [self.mine.pk], 'count': 1})
        self.assertEqual(self.client.delete(f'/api/v1/wishlist/{self.nobody.pk}/').status_code, 204)

    def test_its_searches_and_views_are_no_demand(self):
        def post(kind, **values):
            return self.client.post('/api/v1/analytics/events/', {'event_id': str(uuid.uuid4()), 'visit_id': str(uuid.uuid4()),
                                    'account_id': str(self.own.pk), 'kind': kind, **values}, format='json')
        self.assertEqual(post('search', term='freno').data, {'recorded': False})
        self.assertEqual(post('part_view', part_id=str(self.theirs.pk)).data, {'recorded': False})
        self.assertEqual(post('visit').data, {'recorded': True})
        self.assertEqual(UsageEvent.objects.count(), 0)
