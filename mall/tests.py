from datetime import timedelta
from unittest.mock import patch
from django.db import DatabaseError
from django.utils import timezone
from rest_framework.test import APITestCase
from .models import Account, Invitation, Membership, Part, PartCode, Role, StockEntry, SupplierItem, User

class HealthTests(APITestCase):
    def test_health_is_available_without_authentication(self):
        response = self.client.get('/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_health_fails_when_database_is_unavailable(self):
        with patch('config.health.connection.cursor', side_effect=DatabaseError('private database error')):
            response = self.client.get('/health/')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})

class LanguageTests(APITestCase):
    def test_validation_is_spanish_even_for_an_english_browser(self):
        response = self.client.post('/api/v1/auth/signup/', {}, format='json', HTTP_ACCEPT_LANGUAGE='en-US')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(str(response.data['email'][0]), 'Este campo es requerido.')

class FoundationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('supplier', 'supplier@example.com', 'VeryStrongPass123!')
        self.other = User.objects.create_user('other', 'other@example.com', 'VeryStrongPass123!')
        self.account = Account.objects.create(name='Supplier A')
        self.account.roles.add(Role.objects.get(code='supplier_retail'))
        Membership.objects.create(user=self.user, account=self.account, permission='owner')
        self.part = Part.objects.create(sku='FILTER', name='Filter')
        PartCode.objects.create(part=self.part, brand='acme', code='abc')
        self.client.force_authenticate(self.user)
        self.url = f'/api/v1/accounts/{self.account.pk}/inventory/ingest/'

    def ingest(self, **changes):
        data = {'update_id': 'event-1', 'supplier_invent_id': 'stable-1', 'codigo': 'abc', 'brand': 'acme', 'source': 'apiag', 'quantity': 10}
        data.update(changes)
        return self.client.post(self.url, data, format='json')

    def test_inventory_list_searches_sorts_and_filters_by_match(self):
        self.ingest(update_id='a', supplier_invent_id='stable-1', codigo='abc', quantity=10)
        self.ingest(update_id='b', supplier_invent_id='stable-2', codigo='ZZZ-NO-MATCH', brand='OTRA', quantity=3)
        url = f'/api/v1/accounts/{self.account.pk}/inventory/'
        ids = lambda **params: [row['supplier_invent_id'] for row in self.client.get(url, params).data['results']]
        self.assertEqual(ids(), ['stable-1', 'stable-2'])
        self.assertEqual(ids(ordering='available'), ['stable-2', 'stable-1'])
        self.assertEqual(ids(search='zzz'), ['stable-2'])
        self.assertEqual(ids(matching_status='matched'), ['stable-1'])
        self.assertEqual(ids(matching_status='unmatched'), ['stable-2'])
        self.assertEqual(self.client.get(url, {'matching_status': 'other'}).status_code, 400)

    def test_ledger_adjustments_and_idempotency(self):
        self.assertEqual(self.ingest().status_code, 200)
        self.assertEqual(self.ingest().status_code, 200)
        self.assertEqual(StockEntry.objects.count(), 1)
        self.assertEqual(self.ingest(quantity=20).status_code, 400)
        self.assertEqual(self.ingest(update_id='event-2', quantity=7).status_code, 200)
        self.assertEqual(list(StockEntry.objects.values_list('quantity', 'direction')), [(10, 'credit'), (3, 'debit')])
        self.assertEqual(SupplierItem.objects.get().reported_quantity, 7)

    def test_changed_code_does_not_silently_remap(self):
        self.ingest()
        different = Part.objects.create(sku='OTHER PART', name='Other part')
        PartCode.objects.create(part=different, brand='ACME', code='DEF')
        response = self.ingest(update_id='event-2', codigo='DEF')
        self.assertEqual(response.status_code, 200)
        item = SupplierItem.objects.get()
        self.assertEqual(item.part, self.part)
        self.assertEqual(item.matching_status, 'review')
        self.assertEqual(item.supplier_invent_id, 'stable-1')

    def test_equivalent_codes_from_different_brands_share_one_sku_and_expose_all_items(self):
        sku = Part.objects.create(sku='58411-1R000')
        for code in ['58411-1R000-G', '58-1R0', 'D-HYU-1R']:
            PartCode.objects.create(part=sku, code=code)
        first = self.ingest(codigo='58411-1R000-G', brand='GENERIC', supplier_invent_id='sku-1', description='Primer artículo').data
        second = self.ingest(update_id='event-2', codigo='58-1R0', brand='OTHER', supplier_invent_id='sku-2').data
        third = self.ingest(update_id='event-3', codigo='D-HYU-1R', brand='OEM', supplier_invent_id='sku-3').data
        for item in [first, second, third]:
            self.assertEqual(str(item['part']), str(sku.pk))
            self.assertEqual(item['matching_status'], 'matched')
        other_supplier = Account.objects.create(name='Supplier B')
        other_supplier.roles.add(Role.objects.get(code='supplier_wholesale'))
        Membership.objects.create(user=self.user, account=other_supplier)
        other = self.client.post(f'/api/v1/accounts/{other_supplier.pk}/inventory/ingest/', {'update_id': 'b-1', 'supplier_invent_id': 'b-1', 'codigo': sku.sku, 'brand': 'OEM', 'source': 'upload', 'quantity': 4}, format='json')
        self.assertEqual(other.status_code, 200)
        self.assertEqual(str(other.data['part']), str(sku.pk))
        self.account.roles.add(Role.objects.get(code='client_business'))
        offers = self.client.get(f'/api/v1/catalog/{sku.pk}/suppliers/').data
        self.assertEqual(len(offers), 2)
        self.assertEqual({item['codigo'] for item in offers[0]['items']}, {'58411-1R000-G', '58-1R0', 'D-HYU-1R'})
        self.assertEqual({item['brand'] for item in offers[0]['items']}, {'GENERIC', 'OTHER', 'OEM'})
        self.assertEqual(len({item['id'] for item in offers[0]['items']}), 3)
        self.assertEqual(set(offers[0]['items'][0]), {'id', 'codigo', 'brand', 'description'})
        search = self.client.get('/api/v1/catalog/?search=58-1R0').data
        self.assertEqual(search['count'], 1)
        self.assertEqual(search['results'][0]['sku'], '58411-1R000')
        self.assertNotIn('brand', search['results'][0])
        self.assertEqual(StockEntry.objects.count(), 4)

    def test_code_changes_inside_the_same_sku_keep_the_mapping_and_ledger(self):
        sku = Part.objects.create(sku='58411-1R000')
        PartCode.objects.create(part=sku, code='58-1R0')
        first = self.ingest(codigo='58411-1R000', brand='OEM').data
        second = self.ingest(update_id='event-2', codigo='58-1R0', brand='OTHER').data
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(str(second['part']), str(sku.pk))
        self.assertEqual(second['matching_status'], 'matched')
        self.assertEqual(StockEntry.objects.count(), 1)

    def test_ambiguous_legacy_code_mappings_require_review(self):
        sku = Part.objects.create(sku='AMBIGUOUS')
        PartCode.objects.create(part=self.part, brand='ACME', code=sku.sku)
        response = self.ingest(codigo='AMBIGUOUS').data
        self.assertEqual(response['matching_status'], 'pending')
        self.assertIsNone(response['part'])

    def test_unknown_code_is_pending_without_catalog_creation(self):
        response = self.ingest(codigo='UNKNOWN')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SupplierItem.objects.get().matching_status, 'pending')
        self.assertEqual(Part.objects.count(), 1)

    def test_supplier_isolation(self):
        self.ingest()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.ingest(update_id='event-2').status_code, 404)
        self.assertEqual(self.client.get(f'/api/v1/accounts/{self.account.pk}/inventory/').status_code, 404)

    def test_account_exposes_explicit_capabilities_for_frontend(self):
        self.account.roles.add(Role.objects.get(code='client_business'))
        response = self.client.get('/api/v1/accounts/')
        self.assertEqual(response.data['results'][0]['capabilities'], ['client', 'supplier'])
        self.assertEqual(len(response.data['results'][0]['roles']), 2)

    def test_source_ownership(self):
        self.ingest()
        self.assertEqual(self.ingest(update_id='event-2', source='upload').status_code, 400)
        self.assertEqual(StockEntry.objects.count(), 1)

    def test_offers_aggregate_without_stock_or_prices(self):
        self.ingest()
        self.ingest(update_id='event-2', supplier_invent_id='stable-2')
        buyer = Account.objects.create(name='Buyer')
        buyer.roles.add(Role.objects.get(code='client_walkin'))
        Membership.objects.create(user=self.other, account=buyer)
        self.client.force_authenticate(self.other)
        response = self.client.get(f'/api/v1/catalog/{self.part.pk}/suppliers/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(set(response.data[0]), {'supplier_id', 'supplier_name', 'in_stock', 'items'})

    def test_review_and_reserved_stock_excluded_from_offers(self):
        self.ingest()
        self.account.roles.add(Role.objects.get(code='client_business'))
        item = SupplierItem.objects.get()
        item.reserved_quantity = 10
        item.save()
        url = f'/api/v1/catalog/{self.part.pk}/suppliers/'
        self.assertEqual(self.client.get(url).data, [])
        self.ingest(update_id='event-2', codigo='UNKNOWN', quantity=20)
        self.assertEqual(self.client.get(url).data, [])

    def test_invitation_signup_is_single_use_and_needs_setup(self):
        invitation = Invitation.objects.create(email='new@example.com', created_by=self.user, expires_at=timezone.now()+timedelta(days=1))
        self.client.force_authenticate(None)
        data = {'token': str(invitation.token), 'username': 'new', 'email': invitation.email, 'password': 'LongRandomPassword82!'}
        url = '/api/v1/auth/signup/'
        self.assertEqual(self.client.post(url, data).status_code, 201)
        self.assertEqual(self.client.post(url, data).status_code, 400)
        user = User.objects.get(username='new')
        self.assertFalse(Membership.objects.filter(user=user).exists())
        self.client.force_authenticate(user)
        self.assertEqual(self.client.get('/api/v1/catalog/').status_code, 403)

    def test_expired_invitation_is_rejected(self):
        invitation = Invitation.objects.create(email='new@example.com', created_by=self.user, expires_at=timezone.now()-timedelta(seconds=1))
        self.client.force_authenticate(None)
        response = self.client.post('/api/v1/auth/signup/', {'token': str(invitation.token), 'username': 'new', 'email': invitation.email, 'password': 'LongRandomPassword82!'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username='new').exists())

    def test_unauthenticated_inventory_rejected(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.ingest().status_code, 401)
