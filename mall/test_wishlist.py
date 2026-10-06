from uuid import uuid4
from django.db import IntegrityError, transaction
from django.test import override_settings
from rest_framework.test import APITestCase

from .catalog_grouping import merge_catalog_parts
from .models import Account, Membership, Part, PartCode, Role, StockEntry, SupplierItem, User, WishlistItem


@override_settings(CATALOG_LOW_STOCK_THRESHOLD=5)
class WishlistTests(APITestCase):
    url = '/api/v1/wishlist/'

    def setUp(self):
        self.user = User.objects.create_user('wishlist-user', 'wishlist@example.invalid', 'StrongPassword123!')
        self.other = User.objects.create_user('other-user', 'other@example.invalid', 'StrongPassword123!')
        self.account = Account.objects.create(name='CLIENTE')
        for user in [self.user, self.other]:
            Membership.objects.create(user=user, account=self.account)
        self.part = Part.objects.create(sku='58411-1R000', description='TAMBOR HYU ACCENT 11-16', category='FRENOS', subcategory='TAMBORES')
        self.client.force_authenticate(self.user)

    def detail(self, part=None):
        return f'{self.url}{(part or self.part).pk}/'

    def test_save_remove_are_idempotent_and_do_not_change_stock(self):
        supplier = Account.objects.create(name='PROVEEDOR')
        supplier.roles.add(Role.objects.get(code='supplier_retail'))
        item = SupplierItem.objects.create(supplier=supplier, part=self.part, supplier_invent_id='ONE', codigo=self.part.sku,
                                          source='upload', reported_quantity=5, reserved_quantity=2, matching_status='matched')
        first = self.client.put(self.detail(), {'user': self.other.pk}, format='json')
        self.assertEqual(first.status_code, 200)
        second = self.client.put(self.detail())
        self.assertEqual(first.data['saved_at'], second.data['saved_at'])
        self.assertEqual(WishlistItem.objects.count(), 1)
        row = self.client.get(self.url).data['results'][0]
        self.assertEqual(row['part']['availability']['status'], 'low')
        self.assertNotIn('reported_quantity', str(row))
        item.refresh_from_db()
        self.assertEqual((item.reported_quantity, item.reserved_quantity), (5, 2))
        self.assertEqual(StockEntry.objects.count(), 0)
        self.assertEqual(self.client.delete(self.detail()).status_code, 204)
        self.assertEqual(self.client.delete(self.detail()).status_code, 204)
        self.assertEqual(WishlistItem.objects.count(), 0)

    def test_personal_ownership_even_with_shared_account(self):
        WishlistItem.objects.create(user=self.other, part=self.part)
        self.assertEqual(self.client.get(self.url).data['count'], 0)
        self.assertEqual(self.client.get(f'{self.url}state/').data, {'part_ids': [], 'count': 0})
        self.client.delete(self.detail())
        self.assertTrue(WishlistItem.objects.filter(user=self.other).exists())
        self.client.put(self.detail())
        self.assertEqual(self.client.get(self.url, {'user': self.other.pk}).data['count'], 1)
        self.assertEqual(WishlistItem.objects.count(), 2)

    def test_access_requires_authentication_and_active_membership(self):
        self.account.active = False
        self.account.save()
        for endpoint, method in [(self.url, 'get'), (f'{self.url}state/', 'get'), (self.detail(), 'put'), (self.detail(), 'delete')]:
            self.assertEqual(getattr(self.client, method)(endpoint).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_inactive_items_remain_removable_but_cannot_be_saved(self):
        self.client.put(self.detail())
        self.part.active = False
        self.part.save()
        row = self.client.get(self.url).data['results'][0]
        self.assertFalse(row['available'])
        self.assertEqual(self.client.put(self.detail()).status_code, 400)
        self.assertEqual(self.client.delete(self.detail()).status_code, 204)
        self.assertEqual(self.client.put(f'{self.url}{uuid4()}/').status_code, 404)

    def test_search_alternates_metadata_and_paginate_without_duplicate_rows(self):
        WishlistItem.objects.create(user=self.user, part=self.part)
        for code in ['58411-1R000-G', '58411-1R000-KSM']:
            PartCode.objects.create(part=self.part, code=code)
        for term in ['58411-1r000-g', 'ACCENT', 'FRENOS', 'TAMBORES']:
            data = self.client.get(self.url, {'search': term}).data
            self.assertEqual(data['count'], 1)
            self.assertEqual(data['results'][0]['part']['id'], str(self.part.pk))
        parts = Part.objects.bulk_create([Part(sku=f'SAVED-{i:03}') for i in range(52)])
        WishlistItem.objects.bulk_create([WishlistItem(user=self.user, part=part) for part in parts])
        self.assertEqual(len(self.client.get(self.url).data['results']), 50)
        self.assertEqual(len(self.client.get(self.url, {'page': 2}).data['results']), 3)
        self.assertEqual(self.client.get(f'{self.url}state/').data['count'], 53)

    def test_merge_preserves_favorites_and_deduplicates_per_user(self):
        source = Part.objects.create(sku='58411-1R000-G')
        first = WishlistItem.objects.create(user=self.user, part=source)
        WishlistItem.objects.create(user=self.user, part=self.part)
        WishlistItem.objects.create(user=self.other, part=source)
        merge_catalog_parts(actor=self.user, target_sku=self.part.sku, source_skus=[source.sku])
        self.assertEqual(WishlistItem.objects.count(), 2)
        retained = WishlistItem.objects.get(user=self.user)
        self.assertEqual(retained.part_id, self.part.pk)
        self.assertEqual(retained.saved_at, first.saved_at)
        self.assertEqual(self.client.put(self.detail(source)).data['part_id'], self.part.pk)
        self.assertEqual(WishlistItem.objects.count(), 2)
        self.client.delete(self.detail(source))
        self.assertFalse(WishlistItem.objects.filter(user=self.user).exists())
        self.assertTrue(WishlistItem.objects.filter(user=self.other).exists())

    def test_database_rejects_duplicate_saves(self):
        WishlistItem.objects.create(user=self.user, part=self.part)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WishlistItem.objects.create(user=self.user, part=self.part)
