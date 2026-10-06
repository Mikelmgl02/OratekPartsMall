from rest_framework.test import APITestCase

from .models import Account, Alternate, CatalogMerge, InventoryUpdate, Membership, Part, PartCode, Role, StockEntry, SupplierItem, User
from .services import ingest_inventory, matching_part


class CatalogGroupingMergeTests(APITestCase):
    url = '/api/v1/management/catalog/grouping/merge/'

    def setUp(self):
        self.root = User.objects.create_superuser('merge-root', 'merge-root@example.invalid', 'MergeRoot938!')
        self.staff = User.objects.create_user('merge-staff', 'merge-staff@example.invalid', 'MergeStaff938!', is_staff=True)
        self.client.force_authenticate(self.root)
        self.target = Part.objects.create(sku='58411-1R000', name='TAMBOR', description='ORIGINAL', category='FRENOS')
        self.first = Part.objects.create(sku='58411-1R000-KSM', name='KSM')
        self.second = Part.objects.create(sku='58411-1R000-RAZ-NP', name='RAZ')

    def merge(self, target=None, sources=None, **extra):
        return self.client.post(self.url, {'target_sku': target or self.target.sku,
                                          'source_skus': sources if sources is not None else [self.first.sku, self.second.sku], **extra}, format='json')

    def test_merge_keeps_canonical_metadata_supplier_id_and_complete_stock_history(self):
        supplier = Account.objects.create(name='PROVEEDOR')
        supplier.roles.add(Role.objects.get(code='supplier_retail'))
        Membership.objects.create(user=self.root, account=supplier)
        alternate = PartCode.objects.create(part=self.first, brand='KSM', code='KSM-BRAKE')
        item = ingest_inventory(supplier=supplier, actor=self.root,
                                data={'update_id': 'first', 'supplier_invent_id': 'SUPPLIER-STABLE-8',
                                      'codigo': self.first.sku, 'brand': 'KSM', 'source': 'apiag', 'quantity': 12})
        item.reserved_quantity = 3
        item.save()
        original_item = SupplierItem.objects.values().get(pk=item.pk)
        original_ledger = list(StockEntry.objects.values())
        original_updates = list(InventoryUpdate.objects.values())
        result = self.merge()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data['summary']['merged_skus'], 2)
        self.assertEqual(result.data['summary']['supplier_items_relinked'], 1)
        self.assertEqual(result.data['summary']['codes_transferred'], 1)
        self.assertEqual(result.data['summary']['sku_aliases_created'], 2)
        self.assertEqual(result.data['target']['id'], str(self.target.pk))
        self.target.refresh_from_db()
        self.assertEqual((self.target.name, self.target.description, self.target.category), ('TAMBOR', 'ORIGINAL', 'FRENOS'))
        alternate.refresh_from_db()
        self.assertEqual(alternate.part_id, self.target.pk)
        after_item = SupplierItem.objects.values().get(pk=item.pk)
        self.assertEqual(after_item, {**original_item, 'part_id': self.target.pk})
        self.assertEqual(list(StockEntry.objects.values()), original_ledger)
        self.assertEqual(list(InventoryUpdate.objects.values()), original_updates)
        for source in [self.first, self.second]:
            source.refresh_from_db()
            self.assertFalse(source.active)
            self.assertEqual(source.merged_into_id, self.target.pk)
            self.assertTrue(self.target.codes.filter(brand='', code=source.sku).exists())
            self.assertEqual(matching_part(source.sku, 'UNRELATED-BRAND'), self.target)
        audit = CatalogMerge.objects.get(pk=result.data['merge_id'])
        self.assertEqual(audit.actor, self.root)
        self.assertEqual(audit.target, self.target)
        self.assertEqual({row['id'] for row in audit.sources}, {str(self.first.pk), str(self.second.pk)})
        catalog = self.client.get('/api/v1/management/catalog/', {'search': '58411-1R000'}).data
        self.assertEqual(catalog['count'], 1)
        public = self.client.get('/api/v1/catalog/', {'search': '58411-1R000'}).data
        self.assertEqual(public['count'], 1)
        new_item = ingest_inventory(supplier=supplier, actor=self.root,
                                    data={'update_id': 'new', 'supplier_invent_id': 'SUPPLIER-STABLE-9',
                                          'codigo': self.second.sku, 'brand': 'RAZ', 'source': 'apiag', 'quantity': 2})
        self.assertEqual(new_item.part_id, self.target.pk)

    def test_merge_requires_authenticated_current_superuser(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.merge().status_code, 401)
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.merge().status_code, 403)
        self.assertFalse(CatalogMerge.objects.exists())
        self.assertEqual(Part.objects.filter(active=True).count(), 3)

    def test_exact_retry_does_not_move_stock_or_add_more_codes_or_audits(self):
        first = self.merge()
        codes = list(PartCode.objects.values())
        again = self.merge()
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.data['retry'])
        self.assertEqual(again.data['already_merged_skus'], [self.first.sku, self.second.sku])
        self.assertEqual(again.data['summary']['merged_skus'], 0)
        self.assertEqual(again.data['summary']['sku_aliases_created'], 0)
        self.assertIsNone(again.data['merge_id'])
        self.assertEqual(first.data['target'], again.data['target'])
        self.assertEqual(list(PartCode.objects.values()), codes)
        self.assertEqual(CatalogMerge.objects.count(), 1)

    def test_input_limits_distinctness_and_missing_skus_make_no_changes(self):
        for sources in [[], [self.first.sku] * 2, [self.target.sku], ['MISSING'], ['A' * 121], [f'SKU-{i}' for i in range(51)]]:
            with self.subTest(sources=sources):
                self.assertEqual(self.merge(sources=sources).status_code, 400)
                self.assertEqual(Part.objects.filter(active=True).count(), 3)
                self.assertFalse(PartCode.objects.exists())
        self.assertEqual(self.merge(category='A' * 121).status_code, 400)
        self.assertFalse(CatalogMerge.objects.exists())

    def test_target_and_new_sources_must_be_active_canonical_skus(self):
        self.target.active = False
        self.target.save()
        self.assertEqual(self.merge().status_code, 400)
        self.target.active = True
        self.target.save()
        self.first.active = False
        self.first.save()
        self.assertEqual(self.merge().status_code, 400)
        self.first.active = True
        self.first.merged_into = self.second
        self.first.save()
        self.assertEqual(self.merge().status_code, 400)
        self.assertEqual(self.merge(target=self.first.sku, sources=[self.target.sku]).status_code, 400)
        self.assertFalse(CatalogMerge.objects.exists())

    def test_neutral_source_alias_conflicting_with_unrelated_branded_alias_rejects_atomically(self):
        unrelated = Part.objects.create(sku='DIFFERENT')
        conflicting = PartCode.objects.create(part=unrelated, brand='OTHER', code=self.first.sku)
        source_code = PartCode.objects.create(part=self.second, brand='', code='SECOND-ALIAS')
        failed = self.merge(category='UPDATED')
        self.assertEqual(failed.status_code, 400)
        self.assertIn(self.first.sku, str(failed.data))
        source_code.refresh_from_db()
        self.assertEqual(source_code.part, self.second)
        conflicting.refresh_from_db()
        self.assertEqual(conflicting.part, unrelated)
        self.target.refresh_from_db()
        self.assertEqual(self.target.category, 'FRENOS')
        self.assertEqual(Part.objects.filter(active=True).count(), 4)
        self.assertFalse(CatalogMerge.objects.exists())

    def test_existing_source_code_that_identifies_unrelated_sku_rejects_atomically(self):
        unrelated = Part.objects.create(sku='OTHER-CANONICAL')
        PartCode.objects.create(part=self.first, code=unrelated.sku)
        self.assertEqual(self.merge().status_code, 400)
        self.assertEqual(self.first.codes.count(), 1)
        self.assertEqual(self.target.codes.count(), 0)
        self.assertFalse(CatalogMerge.objects.exists())

    def test_reviewed_category_changes_are_normalized_and_blank_values_preserve(self):
        result = self.merge(target=' 58411-1r000 ', sources=[' 58411-1r000-ksm '],
                            category=' frenos ', subcategory=' tambores de freno ')
        self.assertEqual(result.status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual((self.target.category, self.target.subcategory), ('FRENOS', 'TAMBORES DE FRENO'))
        result = self.merge(sources=[self.second.sku], category='', subcategory='')
        self.assertEqual(result.status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual((self.target.category, self.target.subcategory), ('FRENOS', 'TAMBORES DE FRENO'))

    def test_historical_links_are_repointed_deduplicated_and_self_links_removed(self):
        outside = Part.objects.create(sku='OUTSIDE')
        preserved = Alternate.objects.create(part=self.target, replacement=outside, notes='EXISTING')
        Alternate.objects.create(part=self.first, replacement=outside, notes='FIRST')
        Alternate.objects.create(part=self.second, replacement=outside, notes='SECOND')
        Alternate.objects.create(part=outside, replacement=self.first, notes='REVERSE')
        Alternate.objects.create(part=self.first, replacement=self.second, notes='INTERNAL')
        Alternate.objects.create(part=self.second, replacement=self.target, notes='TARGET')
        result = self.merge()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Alternate.objects.count(), 2)
        preserved.refresh_from_db()
        self.assertEqual(preserved.notes, 'EXISTING\nFIRST\nSECOND')
        self.assertEqual(Alternate.objects.get(part=outside).replacement, self.target)
        self.assertEqual(result.data['summary']['historical_alternates_updated'], 5)
        self.assertEqual(result.data['summary']['historical_alternates_removed'], 4)

    def test_merging_a_previous_target_flattens_archived_sources_without_cycles(self):
        self.assertEqual(self.merge(sources=[self.first.sku]).status_code, 200)
        next_target = Part.objects.create(sku='NEXT-CANONICAL')
        next_merge = self.merge(target=next_target.sku, sources=[self.target.sku, self.second.sku])
        self.assertEqual(next_merge.status_code, 200)
        for part in [self.first, self.second, self.target]:
            part.refresh_from_db()
            self.assertEqual(part.merged_into_id, next_target.pk)
            self.assertFalse(part.active)
            self.assertEqual(matching_part(part.sku, ''), next_target)
        self.assertEqual(self.client.get('/api/v1/management/catalog/').data['count'], 1)
        self.assertEqual(self.merge(target=self.target.sku, sources=[next_target.sku]).status_code, 400)
        self.assertEqual(CatalogMerge.objects.count(), 2)

    def test_admin_cannot_reactivate_archived_sources_or_relink_inventory_to_them(self):
        supplier = Account.objects.create(name='PROVEEDOR')
        item = SupplierItem.objects.create(supplier=supplier, supplier_invent_id='KEEP-ID', codigo='UNKNOWN', brand='', source='upload')
        self.merge(sources=[self.first.sku])
        self.assertEqual(self.client.patch(f'/api/v1/management/catalog/{self.first.pk}/', {'active': True}, format='json').status_code, 404)
        self.assertEqual(self.client.post('/api/v1/management/alternates/', {'part': str(self.first.pk), 'code': 'NEW'}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(f'/api/v1/management/inventory/{item.pk}/',
                                           {'part': str(self.first.pk), 'matching_status': 'matched'}, format='json').status_code, 400)
        codes = list(self.target.codes.values('brand', 'code'))
        self.assertEqual(self.client.patch(f'/api/v1/management/catalog/{self.target.pk}/', {'codes': codes}, format='json').status_code, 200)
        alias = self.target.codes.get(code=self.first.sku)
        self.assertEqual(self.client.patch(f'/api/v1/management/alternates/{alias.pk}/', {'brand': ''}, format='json').status_code, 200)
