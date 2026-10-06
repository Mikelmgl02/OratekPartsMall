import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .catalog_families import family_candidates
from .catalog_import import load_job_groups, replace_job_groups
from .models import Account, CatalogImportJob, CatalogMerge, Membership, Part, PartCode, PartImage, Role, StockEntry, SupplierItem, User
from .services import ingest_inventory


@override_settings(GEMINI_API_KEY='test-key')
class MissingCatalogParentTests(APITestCase):
    base = '58411-07000'
    assistant = '/api/v1/management/catalog/assistant/'
    grouping = '/api/v1/management/catalog/grouping/'

    def setUp(self):
        self.root = User.objects.create_superuser('family-root', 'family@example.invalid', 'Family938!')
        self.client.force_authenticate(self.root)
        self.parts = [Part.objects.create(sku=f'{self.base}-{suffix}', name=f'{self.base}-{suffix}',
                      description='TAMBOR KIA PICANTO ' + ('04-10' if suffix == 'KSM-NP' else '04-11'))
                      for suffix in ['K', 'KSM-NP', 'NP', 'RAZ']]

    def provider(self, source, candidates, allowed, **kwargs):
        self.assertTrue(candidates[self.base]['proposed_parent'])
        for row in source:
            self.assertEqual(allowed[row['sku']][0], self.base)
        return {'suggestions': [{'source_sku': row['sku'], 'target_sku': self.base,
            'category': 'FRENOS', 'subcategory': 'TAMBORES DE FRENO', 'confidence': 0.95,
            'reason': 'Código base completo compartido; revisar los años de aplicación.',
            'source_reference': self.base, 'target_reference': self.base} for row in source]}

    def analyze(self, search=''):
        started = self.client.post(self.assistant, {'id': str(uuid.uuid4()), 'scope': 'all', 'search': search}, format='json')
        self.assertEqual(started.status_code, 201, started.data)
        url = f'{self.assistant}{started.data["id"]}/'
        with patch('mall.catalog_assistant.call_provider', side_effect=self.provider):
            response = self.client.post(url, {'mode': 'classify', 'batch_index': 0}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        return url, response.data

    def decisions(self, data):
        return [{key: item[key] for key in ['id', 'target_sku', 'category', 'subcategory']} for item in data['results']]

    def test_missing_base_is_reviewable_with_all_four_children_and_year_warning(self):
        response = self.client.get(self.grouping, {'search': self.parts[1].sku})
        self.assertEqual(response.status_code, 200, response.data)
        family = response.data['results'][0]
        self.assertEqual(family['target_sku'], self.base)
        self.assertTrue(family['target']['proposed_parent'])
        self.assertEqual(set(family['source_skus']), {part.sku for part in self.parts})
        self.assertEqual(family['source_count'], 4)
        self.assertIn('años', family['warnings'][0])
        with patch('mall.catalog_grouping_suggestions.call_provider', side_effect=self.provider):
            result = self.client.post(self.grouping + 'classify/', {'target_sku': self.base, 'source_skus': family['source_skus']}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(len(result.data['source_skus']), 4)
        self.assertFalse(Part.objects.filter(sku=self.base).exists())

    def test_ai_sees_family_outside_filter_and_marks_year_difference(self):
        _, data = self.analyze(search=self.parts[1].sku)
        self.assertEqual(len(data['results']), 1)
        row = data['results'][0]
        self.assertEqual(row['target_sku'], self.base)
        self.assertIn('años de aplicación difieren', row['reason'])
        self.assertLessEqual(row['confidence'], 0.8)
        self.assertTrue(row['candidates'][0]['proposed_parent'])
        self.assertFalse(Part.objects.filter(sku=self.base).exists())

    def test_review_creates_one_parent_across_batches_and_preserves_stock_photos_and_aliases(self):
        supplier = Account.objects.create(name='FAMILY SUPPLIER')
        supplier.roles.add(Role.objects.get(code='supplier_retail'))
        Membership.objects.create(user=self.root, account=supplier)
        for i, part in enumerate(self.parts):
            ingest_inventory(supplier=supplier, actor=self.root, data={'update_id': f'family-{i}',
                'supplier_invent_id': f'STABLE-{i}', 'codigo': part.sku, 'brand': '', 'source': 'upload', 'quantity': i + 2})
        photo = PartImage.objects.create(part=self.parts[0], image='catalog/test/photo.webp', thumbnail='catalog/test/thumb.webp',
                                         width=1, height=1, size_bytes=1)
        stock = list(SupplierItem.objects.order_by('pk').values())
        ledger = list(StockEntry.objects.order_by('pk').values())
        url, data = self.analyze()
        decisions = self.decisions(data)
        first = self.client.post(url, {'mode': 'apply', 'decisions': decisions[:2]}, format='json')
        self.assertEqual(first.status_code, 200, first.data)
        second = self.client.post(url, {'mode': 'apply', 'decisions': decisions[2:]}, format='json')
        self.assertEqual(second.status_code, 200, second.data)
        target = Part.objects.get(sku=self.base)
        self.assertEqual(target.description, 'TAMBOR KIA PICANTO 04-11')
        self.assertEqual((target.category, target.subcategory), ('FRENOS', 'TAMBORES DE FRENO'))
        self.assertEqual(target.merged_parts.count(), 4)
        self.assertEqual(set(target.codes.values_list('code', flat=True)), {part.sku for part in self.parts})
        self.assertEqual(list(SupplierItem.objects.order_by('pk').values()), [{**item, 'part_id': target.pk} for item in stock])
        self.assertEqual(list(StockEntry.objects.order_by('pk').values()), ledger)
        photo.refresh_from_db()
        self.assertEqual(photo.part_id, target.pk)
        self.assertEqual(photo.image.name, 'catalog/test/photo.webp')
        self.assertEqual(self.client.get('/api/v1/catalog/', {'search': self.base}).data['count'], 1)
        retried = self.client.post(url, {'mode': 'apply', 'decisions': decisions}, format='json')
        self.assertEqual(retried.status_code, 200, retried.data)
        self.assertEqual(CatalogMerge.objects.count(), 2)

    def test_concurrent_unrelated_parent_creation_blocks_old_review(self):
        url, data = self.analyze()
        Part.objects.create(sku=self.base, description='MANUAL EDIT')
        response = self.client.post(url, {'mode': 'apply', 'decisions': self.decisions(data)}, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(CatalogMerge.objects.exists())
        self.assertEqual(Part.objects.filter(active=True).count(), 5)

    def test_manual_creation_requires_explicit_flag_and_base_alias_collisions_are_blocked(self):
        payload = {'target_sku': self.base, 'source_skus': [part.sku for part in self.parts]}
        self.assertEqual(self.client.post(self.grouping + 'merge/', payload, format='json').status_code, 400)
        unrelated = Part.objects.create(sku='OTHER')
        PartCode.objects.create(part=unrelated, code=self.base)
        self.assertEqual(self.client.get(self.grouping).data['count'], 0)
        self.assertEqual(self.client.post(self.grouping + 'merge/', {**payload, 'create_target': True}, format='json').status_code, 400)
        self.assertFalse(Part.objects.filter(sku=self.base).exists())
        unrelated.codes.all().delete()
        response = self.client.post(self.grouping + 'merge/', {**payload, 'create_target': True}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['summary']['target_created'])
        self.assertEqual(response.data['summary']['merged_skus'], 4)

    def test_numeric_position_and_conflicting_application_variants_do_not_create_a_parent(self):
        for suffixes, descriptions in [(['LH', 'RH'], ['SAME', 'SAME']), (['20', '21'], ['SAME', 'SAME']),
                (['G', 'K'], ['TAMBOR KIA PICANTO 04-11', 'TAMBOR KIA PICANTO 12-18']),
                (['G', 'K'], ['TAMBOR KIA PICANTO 04-11', 'DISCO KIA PICANTO 04-11']),
                (['G', 'K'], ['TAMBOR 180MM KIA PICANTO 04-11', 'TAMBOR 200MM KIA PICANTO 04-10'])]:
            rows = {f'{self.base}-{suffix}': {'sku': f'{self.base}-{suffix}', 'description': description}
                    for suffix, description in zip(suffixes, descriptions)}
            self.assertEqual(family_candidates(rows), {})

    def test_import_review_can_stage_a_new_parent_without_creating_catalog_rows(self):
        groups = {part.sku: {'sku': part.sku, 'row': i + 2, 'fields': {'name': part.name, 'description': part.description}, 'codes': {}}
                  for i, part in enumerate(self.parts)}
        Part.objects.all().delete()
        job = CatalogImportJob.objects.create(owner=self.root, filename='family.xlsx', expires_at=timezone.now() + timedelta(days=7))
        replace_job_groups(job, groups)
        url = f'/api/v1/management/catalog/import/jobs/{job.pk}/classification/'
        with patch('mall.catalog_classification.call_provider', side_effect=self.provider):
            response = self.client.post(url, {'mode': 'classify', 'batch_index': 0}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        decisions = [{key: item[key] for key in ['sku', 'target_sku', 'category', 'subcategory']} for item in response.data['results']]
        result = self.client.post(url, {'mode': 'apply', 'decisions': decisions}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data['valid'])
        staged = load_job_groups(job)
        self.assertEqual(list(staged), [self.base])
        self.assertEqual(len(staged[self.base]['codes']), 4)
        self.assertEqual(staged[self.base]['fields']['description'], 'TAMBOR KIA PICANTO 04-11')
        self.assertFalse(Part.objects.exists())
