from django.test import override_settings
from rest_framework.test import APITestCase

from .models import Account, Membership, Part, PartCode, Role, SupplierItem, User


@override_settings(CATALOG_LOW_STOCK_THRESHOLD=5, CATALOG_HIGH_STOCK_THRESHOLD=20)
class CatalogFilterTests(APITestCase):
    url = '/api/v1/catalog/'

    def setUp(self):
        self.user = User.objects.create_user('filter-buyer', 'filter-buyer@example.invalid', 'StrongPassword123!')
        buyer = Account.objects.create(name='CLIENTE')
        Membership.objects.create(user=self.user, account=buyer)
        self.supplier = Account.objects.create(name='PROVEEDOR UNO')
        self.supplier.roles.add(*Role.objects.filter(capability='supplier'))
        self.other = Account.objects.create(name='PROVEEDOR DOS')
        self.other.roles.add(Role.objects.get(code='supplier_retail'))
        self.client.force_authenticate(self.user)

    def part(self, sku, category='FRENOS', subcategory='DISCOS'):
        return Part.objects.create(sku=sku, description='REPUESTO', category=category, subcategory=subcategory)

    def item(self, part, quantity, supplier=None, reserved=0, status='matched'):
        return SupplierItem.objects.create(part=part, supplier=supplier or self.supplier,
            supplier_invent_id=f'ITEM-{SupplierItem.objects.count()}', codigo=part.sku,
            source='upload', reported_quantity=quantity, reserved_quantity=reserved, matching_status=status)

    def test_groups_use_or_and_combine_with_subgroup_search_and_supplier(self):
        disc = self.part('DISC')
        pad = self.part('PAD', subcategory='PASTILLAS')
        oil = self.part('OIL', category='FILTROS', subcategory='ACEITE')
        self.part('BEARING', category='RODAMIENTOS', subcategory='RUEDA')
        self.item(disc, 2)
        self.item(pad, 10, self.other)
        self.item(oil, 0)
        rows = self.client.get(self.url, {'category': ['FRENOS', 'FILTROS']}).data['results']
        self.assertEqual({row['sku'] for row in rows}, {'DISC', 'PAD', 'OIL'})
        rows = self.client.get(self.url, {'category': 'FRENOS', 'subcategory': 'DISCOS', 'supplier': str(self.supplier.pk), 'search': 'REPUESTO'}).data['results']
        self.assertEqual([row['sku'] for row in rows], ['DISC'])
        self.assertEqual(self.client.get(self.url, {'category': 'FRENOS', 'subcategory': 'ACEITE'}).data['count'], 0)

    def test_stock_filters_match_public_bands_and_do_not_multiply_items(self):
        parts = {sku: self.part(sku) for sku in ['UNKNOWN', 'ZERO', 'LOW', 'MEDIUM-MIN', 'MEDIUM-MAX', 'HIGH']}
        self.item(parts['UNKNOWN'], 100, status='review')
        self.item(parts['ZERO'], 3, reserved=4)
        self.item(parts['LOW'], 2)
        self.item(parts['LOW'], 5, reserved=2)
        for code in ['ALT-ONE', 'ALT-TWO']:
            PartCode.objects.create(part=parts['LOW'], code=code)
            PartCode.objects.create(part=parts['MEDIUM-MAX'], code=f'MAX-{code}')
        self.item(parts['MEDIUM-MIN'], 6)
        self.item(parts['MEDIUM-MAX'], 15)
        self.item(parts['MEDIUM-MAX'], 5, self.other)
        self.item(parts['HIGH'], 21)
        for band, expected in [('unknown', ['UNKNOWN']), ('sold_out', ['ZERO']), ('low', ['LOW']), ('medium', ['MEDIUM-MAX', 'MEDIUM-MIN']),
                               ('high', ['HIGH']), ('in_stock', ['HIGH', 'LOW', 'MEDIUM-MAX', 'MEDIUM-MIN'])]:
            response = self.client.get(self.url, {'availability': band})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([row['sku'] for row in response.data['results']], expected)
        self.assertEqual(self.client.get(self.url, {'availability': 'low', 'search': 'ALT-'}).data['count'], 1)
        self.assertEqual(self.client.get(self.url, {'availability': 'medium', 'search': 'MAX-ALT'}).data['count'], 1)
        facets = self.client.get(self.url, {'include_facets': '1'}).data['facets']['availability']
        self.assertEqual([(row['value'], row['label'], row['count']) for row in facets], [
            ('in_stock', 'Con existencias', 4), ('high', 'Altas existencias', 1), ('medium', 'Existencias medias', 2),
            ('low', 'Bajas existencias', 1), ('sold_out', 'Agotado', 1), ('unknown', 'Sin existencias reportadas', 1)])
        self.assertTrue(all(set(row) == {'value', 'label', 'count'} for row in facets))

    def test_facets_count_all_pages_and_respect_search_with_disjunctive_counts(self):
        Part.objects.bulk_create([Part(sku=f'DISC-{i:03}', category='FRENOS', subcategory='DISCOS') for i in range(60)])
        self.part('PAD', subcategory='PASTILLAS')
        self.part('OIL', category='FILTROS', subcategory='ACEITE')
        response = self.client.get(self.url, {'include_facets': '1', 'category': 'FRENOS', 'subcategory': 'DISCOS'})
        self.assertEqual(response.data['count'], 60)
        self.assertEqual(len(response.data['results']), 50)
        self.assertEqual({row['value']: row['count'] for row in response.data['facets']['category']}, {'FILTROS': 1, 'FRENOS': 61})
        self.assertEqual({row['value']: row['count'] for row in response.data['facets']['subcategory']}, {'DISCOS': 60, 'PASTILLAS': 1})
        searched = self.client.get(self.url, {'include_facets': '1', 'search': 'OIL'}).data
        self.assertEqual(searched['facets']['category'], [{'value': 'FILTROS', 'label': 'FILTROS', 'count': 1}])

    def test_facets_exclude_inactive_merged_and_ineligible_supplier_records(self):
        part = self.part('CANONICAL')
        inactive = self.part('INACTIVE', category='HIDDEN')
        inactive.active = False
        inactive.save()
        merged = self.part('MERGED', category='HIDDEN')
        merged.merged_into = part
        merged.save()
        self.item(part, 1)
        self.item(part, 1)
        self.item(part, 0, self.other)
        inactive_supplier = Account.objects.create(name='HIDDEN', active=False)
        inactive_supplier.roles.add(Role.objects.get(code='supplier_retail'))
        self.item(part, 100, inactive_supplier)
        self.item(self.part('UNCLASSIFIED', category='', subcategory=''), 100, status='pending')
        data = self.client.get(self.url, {'include_facets': '1'}).data
        self.assertEqual(data['count'], 2)
        self.assertEqual({row['value']: row['count'] for row in data['facets']['supplier']}, {str(self.supplier.pk): 1, str(self.other.pk): 1})
        self.assertEqual({row['value']: row['count'] for row in data['facets']['availability']}, {'in_stock': 1, 'high': 0, 'medium': 0, 'low': 1, 'sold_out': 0, 'unknown': 1})
        self.assertEqual(self.client.get(self.url, {'category': ''}).data['results'][0]['sku'], 'UNCLASSIFIED')
        self.assertEqual(self.client.get(self.url, {'supplier': str(inactive_supplier.pk)}).data['count'], 0)
        self.assertNotIn('_stock_units', str(data))
        self.assertNotIn('reported_quantity', str(data))

    def test_filters_validate_and_retain_catalog_permissions(self):
        for params in [{'availability': 'invented'}, {'supplier': 'bad-id'}, {'category': 'X' * 121}]:
            self.assertEqual(self.client.get(self.url, params).status_code, 400)
        self.client.force_authenticate(User.objects.create_user('unassigned', 'unassigned@example.invalid', 'StrongPassword123!'))
        self.assertEqual(self.client.get(self.url, {'include_facets': '1'}).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url, {'include_facets': '1'}).status_code, 401)

    def test_selected_supplier_keeps_its_name_when_other_filters_have_no_matches(self):
        self.item(self.part('DISC'), 3)
        data = self.client.get(self.url, {'include_facets': '1', 'category': 'NO MATCH', 'supplier': str(self.supplier.pk)}).data
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['facets']['supplier'], [{'value': str(self.supplier.pk), 'label': 'PROVEEDOR UNO', 'count': 0}])
