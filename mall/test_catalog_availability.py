from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.dateparse import parse_datetime
from rest_framework.test import APITestCase
from .models import Account, Membership, Part, PartCode, Role, SupplierItem, User


@override_settings(CATALOG_LOW_STOCK_THRESHOLD=5)
class CatalogAvailabilityTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('catalog-buyer', 'catalog-buyer@example.invalid', 'StrongPassword123!')
        buyer = Account.objects.create(name='CLIENTE')
        buyer.roles.add(Role.objects.get(code='client_business'))
        Membership.objects.create(user=self.user, account=buyer)
        self.supplier = Account.objects.create(name='PROVEEDOR')
        self.supplier.roles.add(*Role.objects.filter(capability='supplier'))
        self.client.force_authenticate(self.user)
        self.url = '/api/v1/catalog/'

    def item(self, part, units, reserved=0, **changes):
        return SupplierItem.objects.create(part=part, supplier=changes.pop('supplier', self.supplier),
            supplier_invent_id=f'ITEM-{SupplierItem.objects.count()}', codigo=part.sku, brand='MARCA',
            source='upload', reported_quantity=units, reserved_quantity=reserved,
            matching_status=changes.pop('matching_status', 'matched'), **changes)

    def test_bands_boundaries_and_unknown_without_disclosing_quantities(self):
        parts = {sku: Part.objects.create(sku=sku) for sku in ['UNKNOWN', 'ZERO', 'ONE', 'FIVE', 'SIX', 'OVERRESERVED']}
        for sku, units, reserved in [('ZERO', 0, 0), ('ONE', 1, 0), ('FIVE', 5, 0), ('SIX', 6, 0), ('OVERRESERVED', 2, 4)]:
            self.item(parts[sku], units, reserved)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        results = {row['sku']: row['availability'] for row in response.data['results']}
        self.assertEqual({sku: row['status'] for sku, row in results.items()}, {
            'UNKNOWN': 'unknown', 'ZERO': 'sold_out', 'ONE': 'low', 'FIVE': 'low', 'SIX': 'high', 'OVERRESERVED': 'sold_out',
        })
        self.assertEqual(results['UNKNOWN'], {'status': 'unknown', 'supplier_count': 0, 'updated_at': None})
        self.assertEqual(results['ZERO']['supplier_count'], 0)
        self.assertIsNotNone(results['ZERO']['updated_at'])
        for row in response.data['results']:
            self.assertEqual(set(row['availability']), {'status', 'supplier_count', 'updated_at'})
            self.assertNotIn('reported_quantity', row)
            self.assertNotIn('reserved_quantity', row)
            self.assertNotIn('available_quantity', row)

    def test_alternate_search_multiple_items_and_roles_do_not_multiply_stock(self):
        part = Part.objects.create(sku='58411-1R000', description='DISCO VENTILADO', category='FRENOS', subcategory='DISCOS')
        PartCode.objects.create(part=part, code='ALT-ONE')
        PartCode.objects.create(part=part, code='ALT-TWO')
        self.item(part, 4, 2)
        self.item(part, 2)
        other = Account.objects.create(name='SEGUNDO PROVEEDOR')
        other.roles.add(Role.objects.get(code='supplier_retail'))
        last = self.item(part, 1, supplier=other)
        self.item(part, 10, 20)
        row = self.client.get(self.url, {'search': 'ALT-'}).data['results'][0]
        self.assertEqual(row['availability']['status'], 'low')
        self.assertEqual(row['availability']['supplier_count'], 2)
        self.assertEqual(row['category'], 'FRENOS')
        self.assertEqual(row['subcategory'], 'DISCOS')
        self.assertEqual(row['description'], 'DISCO VENTILADO')
        self.assertEqual(self.client.get(self.url, {'search': 'DISCOS'}).data['count'], 1)
        last.reserved_quantity = 1
        last.save()
        changed = self.client.get(self.url).data['results'][0]['availability']
        self.assertEqual(changed['supplier_count'], 1)
        self.assertEqual(parse_datetime(changed['updated_at']), last.updated_at)

    def test_ineligible_or_unreviewed_stock_does_not_claim_availability(self):
        part = Part.objects.create(sku='REVIEW')
        self.item(part, 200, matching_status='review')
        self.item(part, 200, matching_status='pending')
        inactive = Account.objects.create(name='INACTIVO', active=False)
        inactive.roles.add(Role.objects.get(code='supplier_retail'))
        self.item(part, 200, supplier=inactive)
        client_only = Account.objects.create(name='SIN ROL DE PROVEEDOR')
        client_only.roles.add(Role.objects.get(code='client_business'))
        self.item(part, 200, supplier=client_only)
        row = self.client.get(self.url).data['results'][0]
        self.assertEqual(row['availability']['status'], 'unknown')
        self.assertEqual(row['availability']['supplier_count'], 0)

    def test_stock_import_balances_and_reservations_change_public_band(self):
        part = Part.objects.create(sku='CHANGING')
        item = self.item(part, 5)
        def status():
            return self.client.get(self.url).data['results'][0]['availability']['status']
        self.assertEqual(status(), 'low')
        item.reported_quantity = 10
        item.save()
        self.assertEqual(status(), 'high')
        item.reserved_quantity = 6
        item.save()
        self.assertEqual(status(), 'low')
        item.reported_quantity = 0
        item.save()
        self.assertEqual(status(), 'sold_out')
        with override_settings(CATALOG_LOW_STOCK_THRESHOLD=20):
            item.reported_quantity = 25
            item.save()
            self.assertEqual(status(), 'low')

    def test_catalog_stock_queries_are_batched_per_page(self):
        part = Part.objects.create(sku='FIRST')
        self.item(part, 1)
        with CaptureQueriesContext(connection) as first_queries:
            self.client.get(self.url)
        Part.objects.bulk_create([Part(sku=f'ITEM-{index:03}') for index in range(60)])
        with CaptureQueriesContext(connection) as page_queries:
            response = self.client.get(self.url)
        self.assertEqual(len(response.data['results']), 50)
        self.assertEqual(len(page_queries), len(first_queries))
        self.assertLessEqual(len(page_queries), 6)
