from rest_framework.test import APITestCase

from .catalog_split import pair
from .catalog_split_models import CatalogSeparation, CatalogSplit
from .matching_engine import reconcile_catalog
from .matching_queue import matching_work
from .models import Account, CatalogMerge, Part, PartCode, SupplierItem, User
from .oem_reference import upsert_reference
from .oem_reference_models import PartOEMLink
from .services import ingest_inventory

URL = '/api/v1/management/catalog/'


class CatalogSplitTests(APITestCase):
    """Separar SKU undoes a grouping: the retired SKUs come back with the rows they brought, and the worker never groups them again."""
    description = 'TAMBOR KIA PICANTO 04-11'

    def setUp(self):
        self.admin = User.objects.create_superuser('split-admin', 'split-admin@example.invalid', 'Testing938!')
        self.supplier = Account.objects.create(name='SUPPLIER SPLIT')
        self.service = User.objects.create(username='motionpartes-matching-service', is_active=False)  # the worker's actor
        self.client.force_authenticate(self.admin)

    def grouped(self):
        """Three SKUs of one base code, each with its supplier stock row, grouped by the matching worker under a new parent."""
        children = [Part.objects.create(sku=f'58411-08000-{suffix}', description=self.description) for suffix in ['K', 'NP', 'RAZ']]
        PartCode.objects.create(part=children[0], brand='KOREA', code='KX-100', ref_type='company')
        for index, part in enumerate(children):
            ingest_inventory(supplier=self.supplier, actor=self.admin, data={'update_id': f'ITEM-{index}', 'supplier_invent_id': f'ITEM-{index}',
                             'codigo': part.sku, 'brand': '', 'description': self.description, 'source': 'upload', 'quantity': 5})
        with matching_work():
            reconcile_catalog(self.service)
        return Part.objects.get(sku='58411-08000')

    def plan(self, parent):
        response = self.client.get(f'{URL}{parent.pk}/split/')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def restore(self, plan, *skus):
        """Each named SKU with the rows the plan suggests for it."""
        ids = {child['sku']: child['id'] for child in plan['children']}
        mine = lambda rows, pk: [row['id'] for row in rows if row['suggested'] == pk and not row.get('alias_of')]
        return [{'id': ids[sku], 'codes': mine(plan['codes'], ids[sku]), 'items': mine(plan['items'], ids[sku])} for sku in skus]

    def split(self, parent, restore):
        return self.client.post(f'{URL}{parent.pk}/split/', {'restore': restore}, format='json')

    def test_the_plan_suggests_where_each_alterno_and_stock_row_came_from(self):
        parent = self.grouped()
        plan = self.plan(parent)
        self.assertTrue(plan['target']['created_by_merge'])
        children = {child['sku']: child for child in plan['children']}
        self.assertEqual(sorted(children), ['58411-08000-K', '58411-08000-NP', '58411-08000-RAZ'])
        self.assertTrue(children['58411-08000-K']['automatic'])
        self.assertEqual(children['58411-08000-K']['codes'], [{'brand': 'KOREA', 'code': 'KX-100'}])  # as it was when merged
        codes = {code['code']: code for code in plan['codes']}
        self.assertEqual(codes['KX-100']['suggested'], children['58411-08000-K']['id'])
        self.assertEqual((codes['58411-08000-NP']['alias_of'], codes['58411-08000-NP']['suggested']), (children['58411-08000-NP']['id'],) * 2)
        self.assertEqual({item['codigo']: item['suggested'] for item in plan['items']}, {sku: child['id'] for sku, child in children.items()})

    def test_a_split_brings_the_sku_back_with_its_rows_and_the_worker_leaves_it_apart(self):
        parent = self.grouped()
        response = self.split(parent, self.restore(self.plan(parent), '58411-08000-K'))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body['summary']['codes_moved'], body['summary']['items_moved'], body['summary']['aliases_removed']), (1, 1, 1))
        self.assertEqual(body['target']['merged_count'], 2)
        part = Part.objects.get(sku='58411-08000-K')
        self.assertEqual((part.active, part.merged_into_id), (True, None))
        self.assertEqual(list(part.codes.values_list('code', flat=True)), ['KX-100'])
        self.assertEqual(list(part.supplier_items.values_list('codigo', flat=True)), ['58411-08000-K'])
        self.assertFalse(parent.codes.filter(code='58411-08000-K').exists())  # the alias the merge filed leaves with its SKU
        self.assertTrue(CatalogSeparation.objects.filter(first_id=pair(part.pk, parent.pk)[0], second_id=pair(part.pk, parent.pk)[1]).exists())
        self.assertEqual(CatalogSplit.objects.get().restored, [{'id': str(part.pk), 'sku': '58411-08000-K'}])
        with matching_work():
            reconcile_catalog(self.admin)
        part.refresh_from_db()
        self.assertIsNone(part.merged_into_id)  # never grouped again on its own
        self.assertEqual(parent.merged_parts.count(), 2)
        family = next(row for row in self.client.get(f'{URL}grouping/', {'search': '58411-08000'}).json()['results'] if row['target_sku'] == '58411-08000')
        self.assertIn('se separaron', ' '.join(family['warnings']))  # Agrupar SKU still offers it, saying so

    def test_splitting_every_sku_retires_the_empty_parent_and_nothing_regroups(self):
        parent = self.grouped()
        plan = self.plan(parent)
        response = self.split(parent, self.restore(plan, *[child['sku'] for child in plan['children']]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['summary']['target_retired'])
        parent.refresh_from_db()
        self.assertFalse(parent.active)
        with matching_work():
            reconcile_catalog(self.admin)
        self.assertEqual(Part.objects.filter(sku__startswith='58411-08000-', active=True, merged_into__isnull=True).count(), 3)
        self.assertEqual(CatalogMerge.objects.count(), 1)

    def test_a_split_refuses_rows_that_moved_or_belong_elsewhere(self):
        parent = self.grouped()
        plan = self.plan(parent)
        ids = {child['sku']: child['id'] for child in plan['children']}
        codes = {code['code']: code['id'] for code in plan['codes']}
        stranger = Part.objects.create(sku='OTRO-1')
        refused = [
            self.split(parent, [{'id': str(stranger.pk)}]),  # not grouped here
            self.split(parent, [{'id': ids['58411-08000-K'], 'codes': [999999]}]),  # no longer on the parent
            self.split(parent, [{'id': ids['58411-08000-K'], 'codes': [codes['58411-08000-NP']]}]),  # another SKU's own code
            self.split(parent, [{'id': ids['58411-08000-K'], 'codes': [codes['KX-100']]}, {'id': ids['58411-08000-NP'], 'codes': [codes['KX-100']]}]),
        ]
        self.assertEqual([response.status_code for response in refused], [400] * 4)
        self.assertIn('Vuelve a cargar', refused[1].json()['detail'])
        self.assertFalse(Part.objects.filter(sku__startswith='58411-08000-', merged_into__isnull=True).exists())  # nothing changed
        retired = Part.objects.get(sku='58411-08000-K')
        self.assertIn('58411-08000', self.client.get(f'{URL}{retired.pk}/split/').json()['detail'])
        self.client.force_authenticate(User.objects.create_user('split-staff', 'split-staff@example.invalid', 'Testing938!'))
        self.assertEqual(self.client.get(f'{URL}{parent.pk}/split/').status_code, 403)

    def test_catalog_links_follow_the_codes_that_made_them(self):
        parent = self.grouped()
        cite = 'CATÁLOGO KOREA 2024 · pág. 3 · KX-100'  # the catalog item KX-100, matched through the code the K SKU brought
        printed = upsert_reference('KIA', '58411-07000', source_kind='aftermarket_catalog', citation='CATÁLOGO KOREA 2024')[0]
        own = upsert_reference('KIA', '58411-08000', source_kind='aftermarket_catalog', citation='CATÁLOGO KOREA 2024')[0]  # the parent's code
        for reference in (printed, own):
            PartOEMLink.objects.create(part=parent, reference=reference, source='catalog', citations=[cite])
        summary = self.split(parent, self.restore(self.plan(parent), '58411-08000-K')).json()['summary']
        self.assertEqual((summary['links_moved'], summary['links_shared']), (1, 1))
        links = sorted(PartOEMLink.objects.filter(source='catalog').values_list('part__sku', 'reference__code'))
        self.assertEqual(links, [('58411-08000', '5841108000'), ('58411-08000-K', '5841107000'), ('58411-08000-K', '5841108000')])

    def test_the_worker_never_regroups_skus_a_split_separated_through_references(self):
        pairs = []
        for number in ['60150', '60160']:
            main = Part.objects.create(sku=f'43512-{number}', description='DISCO FRENO DEL TOY LAND CRUISER')
            other = Part.objects.create(sku=f'DF-{number}', description='DISCO FRENO DEL TOY LAND CRUISER')
            PartCode.objects.create(part=main, code=other.sku)  # the other SKU's code is an alterno of the main one
            pairs.append((main, other))
        main, other = pairs[0]
        split = CatalogSplit.objects.create(target=main, actor=self.admin)
        CatalogSeparation.objects.create(first_id=pair(main.pk, other.pk)[0], second_id=pair(main.pk, other.pk)[1], split=split)
        with matching_work():
            reconcile_catalog(self.admin)
        other.refresh_from_db()
        pairs[1][1].refresh_from_db()
        self.assertEqual((other.merged_into_id, pairs[1][1].merged_into_id), (None, pairs[1][0].pk))  # only the pair without a split groups

    def test_the_merged_list_and_the_grid_count_grouped_skus(self):
        parent = self.grouped()
        rows = self.client.get(f'{URL}merged/', {'search': 'RAZ'}).json()['results']
        self.assertEqual([(row['sku'], row['merged_count'], row['automatic']) for row in rows], [('58411-08000', 3, True)])
        self.assertEqual([row['sku'] for row in rows[0]['merged']], ['58411-08000-K', '58411-08000-NP', '58411-08000-RAZ'])
        self.assertEqual(self.client.get(f'{URL}merged/', {'origin': 'manual'}).json()['count'], 0)
        self.assertEqual(self.client.get(URL, {'ids': str(parent.pk)}).json()['results'][0]['merged_count'], 3)
        edited = self.client.post(f'{URL}bulk-edit/', {'rows': [{'id': str(parent.pk), 'description': 'TAMBOR KIA PICANTO 04-11 TRAS'}]}, format='json')
        self.assertEqual(edited.json()['updated'][0]['merged_count'], 3)  # a grid edit keeps the badge

    def test_a_merge_by_hand_records_what_each_sku_brought_so_its_split_is_exact(self):
        main = Part.objects.create(sku='43512-60150', description='DISCO FRENO DEL TOY LAND CRUISER')
        other = Part.objects.create(sku='DF-4512', description='DISCO FRENO DEL TOY LAND CRUISER')
        PartCode.objects.create(part=other, brand='GMB', code='GD-4512', ref_type='company')
        item = ingest_inventory(supplier=self.supplier, actor=self.admin, data={'update_id': 'X-1', 'supplier_invent_id': 'X-1', 'codigo': 'ZX 77',
                                'brand': '', 'description': 'DISCO', 'source': 'upload', 'quantity': 3})
        SupplierItem.objects.filter(pk=item.pk).update(part=other, matching_status='matched')  # a code that names neither SKU
        merged = self.client.post(f'{URL}grouping/merge/', {'target_sku': main.sku, 'source_skus': [other.sku]}, format='json')
        self.assertEqual(merged.status_code, 200, merged.content)
        self.assertEqual(CatalogMerge.objects.get().sources, [{'id': str(other.pk), 'sku': 'DF-4512', 'codes': [{'brand': 'GMB', 'code': 'GD-4512'}],
                                                              'items': [str(item.pk)], 'images': []}])
        plan = self.plan(main)
        self.assertEqual(plan['children'][0]['codes'], [{'brand': 'GMB', 'code': 'GD-4512'}])
        self.assertEqual({code['code']: code['suggested'] for code in plan['codes']}['GD-4512'], str(other.pk))
        self.assertEqual([entry['suggested'] for entry in plan['items']], [str(other.pk)])
        response = self.split(main, self.restore(plan, 'DF-4512'))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(list(SupplierItem.objects.filter(part=other).values_list('pk', flat=True)), [item.pk])
