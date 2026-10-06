import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless

from django.db import close_old_connections, connection
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase

from .models import Account, InventoryUpdate, Membership, Part, PartCode, Role, StockEntry, SupplierItem, User
from .request_models import ClientRequestSubmission, SupplierRequest, SupplierRequestLine
from .services import ingest_inventory


class SupplierRequestWorkflowTests(APITestCase):
    def setUp(self):
        self.buyer = User.objects.create_user('request-buyer', 'request-buyer@example.invalid', 'Request938!')
        self.seller_a = User.objects.create_user('request-seller-a', 'request-seller-a@example.invalid', 'Request938!')
        self.seller_b = User.objects.create_user('request-seller-b', 'request-seller-b@example.invalid', 'Request938!')
        self.root = User.objects.create_superuser('request-root', 'request-root@example.invalid', 'Request938!')
        self.client_account = self.account('CLIENTE ORIGINAL', 'client_business', self.buyer)
        self.supplier_a = self.account('PROVEEDOR A', 'supplier_retail', self.seller_a)
        self.supplier_b = self.account('PROVEEDOR B', 'supplier_wholesale', self.seller_b)
        self.part = Part.objects.create(sku='58411-1R000', name='TAMBOR ORIGINAL', description='DESCRIPCION ORIGINAL')
        for code in ['58411-1R000-G', '58411-1R000-KSM']:
            PartCode.objects.create(part=self.part, code=code)
        self.item_a = self.item(self.supplier_a, self.seller_a, 'A-001', '58411-1R000-G', 10)
        self.item_a.reserved_quantity = 2
        self.item_a.save()
        self.item_a2 = self.item(self.supplier_a, self.seller_a, 'A-002', '58411-1R000-KSM', 10)
        self.item_b = self.item(self.supplier_b, self.seller_b, 'B-001', '58411-1R000-G', 20)
        self.client.force_authenticate(self.buyer)

    def account(self, name, role, user):
        account = Account.objects.create(name=name)
        account.roles.add(Role.objects.get(code=role))
        Membership.objects.create(user=user, account=account)
        return account

    def item(self, account, actor, stable_id, code, quantity):
        return ingest_inventory(supplier=account, actor=actor,
                                data={'update_id': f'seed-{stable_id}', 'supplier_invent_id': stable_id,
                                      'codigo': code, 'brand': 'KSM', 'description': f'TAMBOR {stable_id}',
                                      'source': 'upload', 'quantity': quantity})

    def url(self, account=None):
        return f'/api/v1/accounts/{(account or self.client_account).pk}/requests/'

    def payload(self, lines=None, *, key=None, notes='', expected=False):
        rows = lines if lines is not None else [(self.item_a, 3), (self.item_a2, 2), (self.item_b, 4)]
        return {'submission_id': str(key or uuid.uuid4()), 'notes': notes,
                'lines': [{'supplier_item_id': str(item.pk), 'quantity': quantity,
                           **({'expected_part_id': str(item.part_id), 'expected_codigo': item.codigo,
                               'expected_brand': item.brand} if expected else {})} for item, quantity in rows]}

    def submit(self, data=None, *, account=None):
        return self.client.post(self.url(account), data or self.payload(), format='json')

    def stock_snapshot(self):
        return (list(SupplierItem.objects.order_by('pk').values()), list(StockEntry.objects.order_by('pk').values()),
                list(InventoryUpdate.objects.order_by('pk').values()))

    def test_client_part_request_states_count_units_and_review_breakdown(self):
        initial = self.submit()
        self.submit(self.payload([(self.item_a, 7), (self.item_b, 1)]))
        reviewed = next(row['id'] for row in initial.data['requests'] if row['supplier']['id'] == str(self.supplier_a.pk))
        self.client.force_authenticate(self.seller_a)
        self.client.post(f'{self.url(self.supplier_a)}{reviewed}/review/', {}, format='json')
        self.client.force_authenticate(self.buyer)
        before = self.stock_snapshot()
        path = f'/api/v1/accounts/{self.client_account.pk}/catalog/{self.part.pk}/request-state/'
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['totals'], {'sent_quantity': 17, 'pending_quantity': 5, 'reviewed_quantity': 12, 'quoted_quantity': 0, 'adjustment_quantity': 0, 'handshaked_quantity': 0})
        rows = {row['supplier_item_id']: row for row in response.data['items']}
        self.assertEqual({key: rows[str(self.item_a.pk)][key] for key in response.data['totals']},
                         {'sent_quantity': 10, 'pending_quantity': 0, 'reviewed_quantity': 10, 'quoted_quantity': 0, 'adjustment_quantity': 0, 'handshaked_quantity': 0})
        self.assertEqual(rows[str(self.item_b.pk)]['sent_quantity'], 5)
        for row in rows.values():
            self.assertEqual(set(row), {'supplier_item_id', 'supplier_id', 'codigo', 'brand', 'sent_quantity', 'pending_quantity', 'reviewed_quantity', 'quoted_quantity', 'adjustment_quantity', 'handshaked_quantity'})
        self.assertEqual(self.stock_snapshot(), before)
        self.assertEqual(self.client.post(path, {}, format='json').status_code, 405)

    def test_part_states_remain_client_scoped_and_include_merged_sku_snapshots(self):
        self.submit(self.payload([(self.item_a, 3)]))
        old = Part.objects.create(sku='OLD-SKU', active=False, merged_into=self.part)
        SupplierRequestLine.objects.update(part=old, sku=old.sku)
        other = self.account('OTRO CLIENTE', 'client_business', self.seller_b)
        self.client.force_authenticate(self.seller_b)
        self.submit(self.payload([(self.item_a, 100)]), account=other)
        path = f'/api/v1/accounts/{self.client_account.pk}/catalog/{self.part.pk}/request-state/'
        self.assertEqual(self.client.get(path).status_code, 404)
        self.client.force_authenticate(self.root)
        self.assertEqual(self.client.get(path).status_code, 404)
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/catalog/{self.part.pk}/request-state/').status_code, 403)
        self.client.force_authenticate(self.buyer)
        response = self.client.get(path)
        self.assertEqual(response.data['totals']['sent_quantity'], 3)
        self.item_a.codigo = 'NEW-CODE'
        self.item_a.save()
        self.assertEqual(self.client.get(path).data['items'][0]['codigo'], '58411-1R000-G')
        empty = Part.objects.create(sku='EMPTY')
        data = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/catalog/{empty.pk}/request-state/').data
        self.assertEqual(data['totals'], {'sent_quantity': 0, 'pending_quantity': 0, 'reviewed_quantity': 0, 'quoted_quantity': 0, 'adjustment_quantity': 0, 'handshaked_quantity': 0})
        self.assertEqual(data['items'], [])

    def test_mixed_supplier_submission_splits_lines_and_never_changes_stock(self):
        before = self.stock_snapshot()
        response = self.submit(self.payload(notes=' retiro por la mañana ', expected=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ClientRequestSubmission.objects.count(), 1)
        self.assertEqual(SupplierRequest.objects.count(), 2)
        self.assertEqual(SupplierRequestLine.objects.count(), 3)
        self.assertEqual(self.stock_snapshot(), before)
        summaries = {row['supplier']['id']: row for row in response.data['requests']}
        self.assertEqual((summaries[str(self.supplier_a.pk)]['line_count'], summaries[str(self.supplier_a.pk)]['unit_count']), (2, 5))
        self.assertEqual((summaries[str(self.supplier_b.pk)]['line_count'], summaries[str(self.supplier_b.pk)]['unit_count']), (1, 4))
        self.client.force_authenticate(self.seller_a)
        listed = self.client.get(self.url(self.supplier_a))
        self.assertEqual(listed.data['count'], 1)
        self.assertEqual(listed.data['results'][0]['client'], {'id': str(self.client_account.pk), 'name': 'CLIENTE ORIGINAL'})
        detail = self.client.get(f'{self.url(self.supplier_a)}{summaries[str(self.supplier_a.pk)]["id"]}/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data['notes'], 'RETIRO POR LA MAÑANA')
        self.assertEqual({row['supplier_item_id'] for row in detail.data['lines']}, {str(self.item_a.pk), str(self.item_a2.pk)})
        self.assertEqual(detail.data['status'], 'pending')
        self.assertIsNone(detail.data['reviewed_at'])
        for line in SupplierRequestLine.objects.select_related('request', 'supplier_item'):
            self.assertEqual(line.request.supplier_id, line.supplier_item.supplier_id)

    def test_supplier_cannot_list_read_or_review_another_suppliers_lines(self):
        response = self.submit()
        supplier_a_id = next(row['id'] for row in response.data['requests'] if row['supplier']['id'] == str(self.supplier_a.pk))
        self.client.force_authenticate(self.seller_b)
        self.assertEqual(self.client.get(self.url(self.supplier_b)).data['count'], 1)
        self.assertEqual(self.client.get(f'{self.url(self.supplier_b)}{supplier_a_id}/').status_code, 404)
        self.assertEqual(self.client.post(f'{self.url(self.supplier_b)}{supplier_a_id}/review/', {}, format='json').status_code, 404)
        self.assertEqual(self.client.get(self.url(self.supplier_a)).status_code, 404)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertEqual(self.client.get(f'{self.url()}{supplier_a_id}/').status_code, 403)
        self.assertEqual(SupplierRequest.objects.filter(status='reviewed').count(), 0)

    def test_client_sent_history_preserves_requested_items_and_hides_supplier_stock(self):
        response = self.submit(self.payload(expected=True, notes='ENTREGA'))
        base = f'/api/v1/accounts/{self.client_account.pk}/sent-requests/'
        listed = self.client.get(base)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data['count'], 2)
        self.assertEqual({row['supplier']['id'] for row in listed.data['results']}, {str(self.supplier_a.pk), str(self.supplier_b.pk)})
        self.assertEqual({row['submission_id'] for row in listed.data['results']}, {response.data['submission_id']})
        row = next(row for row in listed.data['results'] if row['supplier']['id'] == str(self.supplier_a.pk))
        path = f'{base}{row["id"]}/'
        before = self.stock_snapshot()
        detail = self.client.get(path)
        self.assertEqual(detail.data['notes'], 'ENTREGA')
        self.assertEqual((detail.data['line_count'], detail.data['unit_count']), (2, 5))
        for line in detail.data['lines']:
            self.assertEqual(set(line), {'id', 'part_id', 'sku', 'name', 'codigo', 'brand', 'description', 'quantity'})
        self.assertEqual(self.client.post(path, {}, format='json').status_code, 405)
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.post(f'{self.url(self.supplier_a)}{row["id"]}/review/', {}, format='json').status_code, 200)
        self.client.force_authenticate(self.buyer)
        reviewed = self.client.get(path)
        self.assertEqual(reviewed.data['status'], 'reviewed')
        self.assertIsNotNone(reviewed.data['reviewed_at'])
        self.assertEqual(reviewed.data['lines'], detail.data['lines'])
        self.assertEqual(self.client.get(base, {'status': 'reviewed', 'search': '58411-1R000-KSM'}).data['count'], 1)
        self.assertEqual(self.client.get(base, {'status': 'pending', 'search': 'PROVEEDOR B'}).data['count'], 1)
        self.assertEqual(self.client.get(base, {'status': 'invalid'}).status_code, 400)
        self.assertEqual(self.stock_snapshot(), before)

    def test_sent_history_requires_exact_active_client_membership_without_superuser_bypass(self):
        response = self.submit(self.payload([(self.item_a, 2)]))
        base = f'/api/v1/accounts/{self.client_account.pk}/sent-requests/'
        path = f'{base}{response.data["requests"][0]["id"]}/'
        for user, expected in [(None, 401), (self.root, 404), (self.seller_a, 404)]:
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(base).status_code, expected)
            self.assertEqual(self.client.get(path).status_code, expected)
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/sent-requests/').status_code, 403)
        other = self.account('OTRO CLIENTE', 'client_walkin', self.buyer)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.client.get(f'/api/v1/accounts/{other.pk}/sent-requests/').data['count'], 0)
        self.assertEqual(self.client.get(f'/api/v1/accounts/{other.pk}/sent-requests/{response.data["requests"][0]["id"]}/').status_code, 404)
        self.client_account.active = False
        self.client_account.save()
        self.assertEqual(self.client.get(base).status_code, 404)
        self.client_account.active = True
        self.client_account.save()
        self.client_account.roles.clear()
        self.assertEqual(self.client.get(base).status_code, 403)

    def test_sent_history_is_shared_with_client_employees_and_keeps_snapshot_names(self):
        response = self.submit(self.payload([(self.item_a, 2)]))
        base = f'/api/v1/accounts/{self.client_account.pk}/sent-requests/'
        Membership.objects.create(account=self.client_account, user=self.seller_b)
        Account.objects.filter(pk=self.supplier_a.pk).update(name='NOMBRE NUEVO')
        SupplierItem.objects.filter(pk=self.item_a.pk).update(codigo='CODIGO NUEVO', reported_quantity=0)
        self.client.force_authenticate(self.seller_b)
        listed = self.client.get(base)
        self.assertEqual(listed.data['results'][0]['supplier']['name'], 'PROVEEDOR A')
        detail = self.client.get(f'{base}{response.data["requests"][0]["id"]}/')
        self.assertEqual((detail.data['lines'][0]['codigo'], detail.data['lines'][0]['quantity']), ('58411-1R000-G', 2))

    def test_same_submission_key_is_idempotent_and_changed_payload_conflicts(self):
        data = self.payload(notes=' entrega ', expected=True)
        response = self.submit(data)
        self.assertEqual(response.status_code, 200)
        data['lines'].reverse()
        data['notes'] = 'ENTREGA'
        again = self.submit(data)
        self.assertEqual(again.data, response.data)
        self.assertEqual(SupplierRequest.objects.count(), 2)
        changed = {**data, 'notes': 'OTRA ENTREGA'}
        self.assertEqual(self.submit(changed).status_code, 409)
        changed = {**data, 'lines': [{**line, 'quantity': 1} for line in data['lines']]}
        self.assertEqual(self.submit(changed).status_code, 409)
        changed = {**data, 'lines': [{**line, 'expected_codigo': 'OTHER'} for line in data['lines']]}
        self.assertEqual(self.submit(changed).status_code, 409)
        SupplierItem.objects.filter(pk=self.item_a.pk).update(reported_quantity=0, codigo='CHANGED')
        # Replaying a successful, unchanged submission does not create a new
        # request or revalidate later stock against the historical snapshot.
        self.assertEqual(self.submit(data).data, response.data)
        self.assertEqual(ClientRequestSubmission.objects.count(), 1)

    def test_submission_id_is_scoped_to_client_account(self):
        second_client = self.account('SEGUNDO CLIENTE', 'client_walkin', self.buyer)
        data = self.payload([(self.item_a, 1)])
        first = self.submit(data)
        second = self.submit(data, account=second_client)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertNotEqual(first.data['requests'][0]['id'], second.data['requests'][0]['id'])
        self.assertEqual(ClientRequestSubmission.objects.count(), 2)

    def test_client_membership_capability_active_account_and_authentication_are_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.submit().status_code, 401)
        self.client.force_authenticate(self.root)
        self.assertEqual(self.submit().status_code, 404)
        self.assertEqual(self.client.get(self.url(self.supplier_a)).status_code, 404)
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.submit(account=self.supplier_a).status_code, 403)
        self.assertEqual(self.submit().status_code, 404)
        self.client.force_authenticate(self.buyer)
        self.client_account.active = False
        self.client_account.save()
        self.assertEqual(self.submit().status_code, 404)
        self.client_account.active = True
        self.client_account.save()
        self.client_account.roles.clear()
        self.assertEqual(self.submit().status_code, 403)
        self.assertFalse(ClientRequestSubmission.objects.exists())

    def test_invalid_quantities_duplicates_and_missing_items_are_rejected_without_partial_writes(self):
        for quantity in [0, -1, 10000, 1.5, True]:
            with self.subTest(quantity=quantity):
                self.assertEqual(self.submit(self.payload([(self.item_a, quantity)])).status_code, 400)
        duplicate = self.payload([(self.item_a, 1), (self.item_a, 2)])
        self.assertEqual(self.submit(duplicate).status_code, 400)
        missing = self.payload([(self.item_a, 1)])
        missing['lines'].append({'supplier_item_id': str(uuid.uuid4()), 'quantity': 1})
        self.assertEqual(self.submit(missing).status_code, 400)
        empty = self.payload([])
        self.assertEqual(self.submit(empty).status_code, 400)
        self.assertFalse(ClientRequestSubmission.objects.exists())
        self.assertFalse(SupplierRequestLine.objects.exists())

    def test_requests_can_exceed_stock_without_reserving_or_changing_ledger(self):
        for reported, reserved, requested in [(10, 2, 9), (0, 0, 4), (5, 8, 3)]:
            with self.subTest(reported=reported, reserved=reserved):
                SupplierItem.objects.filter(pk=self.item_a.pk).update(reported_quantity=reported, reserved_quantity=reserved)
                before = self.stock_snapshot()
                self.client.force_authenticate(self.buyer)
                response = self.submit(self.payload([(self.item_a2, 1), (self.item_a, requested)], expected=True))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data['requests'][0]['unit_count'], requested + 1)
                self.assertNotIn('stock', response.data['requests'][0])
                self.client.force_authenticate(self.seller_a)
                path = f'{self.url(self.supplier_a)}{response.data["requests"][0]["id"]}/'
                detail = self.client.get(path)
                line = next(row for row in detail.data['lines'] if row['supplier_item_id'] == str(self.item_a.pk))
                self.assertEqual(line['quantity'], requested)
                available = max(0, reported - reserved)
                self.assertEqual({key: line['stock'][key] for key in ['reported_quantity', 'reserved_quantity', 'available_quantity', 'shortfall']},
                                 {'reported_quantity': reported, 'reserved_quantity': reserved, 'available_quantity': available,
                                  'shortfall': max(0, requested - available)})
                reviewed = self.client.post(f'{path}review/', {}, format='json')
                self.assertEqual(reviewed.status_code, 200)
                self.assertEqual(reviewed.data['lines'], detail.data['lines'])
                self.assertEqual(self.stock_snapshot(), before)

    def test_supplier_detail_refreshes_current_stock_after_inventory_sync(self):
        response = self.submit(self.payload([(self.item_a, 3)]))
        path = f'{self.url(self.supplier_a)}{response.data["requests"][0]["id"]}/'
        self.client.force_authenticate(self.seller_a)
        initial = self.client.get(path).data['lines'][0]
        self.assertEqual(initial['stock']['available_quantity'], 8)
        self.assertEqual(initial['stock']['shortfall'], 0)
        updated = ingest_inventory(supplier=self.supplier_a, actor=self.seller_a,
                                   data={'update_id': 'stock-after-request', 'supplier_invent_id': self.item_a.supplier_invent_id,
                                         'codigo': self.item_a.codigo, 'brand': self.item_a.brand, 'source': 'upload', 'quantity': 1})
        before = self.stock_snapshot()
        current = self.client.get(path).data['lines'][0]
        self.assertEqual(current['quantity'], 3)
        self.assertEqual(current['stock'], {'reported_quantity': 1, 'reserved_quantity': 2, 'available_quantity': 0,
                                           'shortfall': 3, 'updated_at': updated.updated_at.isoformat()})
        self.assertEqual(self.stock_snapshot(), before)

    def test_changed_or_unmatched_supplier_item_has_no_misleading_stock_counts(self):
        response = self.submit(self.payload([(self.item_a, 3)]))
        path = f'{self.url(self.supplier_a)}{response.data["requests"][0]["id"]}/'
        self.client.force_authenticate(self.seller_a)
        original = {'part': self.part, 'codigo': self.item_a.codigo, 'brand': self.item_a.brand,
                    'supplier_invent_id': self.item_a.supplier_invent_id, 'matching_status': 'matched'}
        for changes in [{'part': None}, {'codigo': 'CHANGED'}, {'brand': 'OTHER'},
                        {'supplier_invent_id': 'NEW-ID'}, {'matching_status': 'pending'}]:
            with self.subTest(changes=changes):
                SupplierItem.objects.filter(pk=self.item_a.pk).update(**{**original, **changes})
                line = self.client.get(path).data['lines'][0]
                self.assertIsNone(line['stock'])
                self.assertEqual((line['codigo'], line['quantity']), (self.item_a.codigo, 3))
        SupplierItem.objects.filter(pk=self.item_a.pk).update(**original)
        self.assertEqual(self.client.get(path).data['lines'][0]['stock']['available_quantity'], 8)

    def test_inactive_supplier_without_supplier_role_pending_items_and_inactive_parts_are_unavailable(self):
        data = self.payload([(self.item_a, 1), (self.item_b, 1)])
        self.supplier_b.active = False
        self.supplier_b.save()
        self.assertEqual(self.submit(data).status_code, 400)
        self.supplier_b.active = True
        self.supplier_b.save()
        self.supplier_b.roles.clear()
        self.assertEqual(self.submit(data).status_code, 400)
        self.supplier_b.roles.add(Role.objects.get(code='supplier_wholesale'))
        for changes in [{'matching_status': 'pending'}, {'matching_status': 'review'}, {'part': None}]:
            SupplierItem.objects.filter(pk=self.item_b.pk).update(**{'part': self.part, 'reported_quantity': 20, 'matching_status': 'matched', **changes})
            self.assertEqual(self.submit(data).status_code, 400)
        SupplierItem.objects.filter(pk=self.item_b.pk).update(part=self.part, reported_quantity=20, matching_status='matched')
        self.part.active = False
        self.part.save()
        self.assertEqual(self.submit(data).status_code, 400)
        self.assertFalse(ClientRequestSubmission.objects.exists())

    def test_expected_code_brand_and_canonical_id_reject_stale_basket_selections(self):
        data = self.payload([(self.item_a, 1)], expected=True)
        for changes in [{'codigo': 'CHANGED'}, {'brand': 'OTHER'}]:
            SupplierItem.objects.filter(pk=self.item_a.pk).update(**{'codigo': '58411-1R000-G', 'brand': 'KSM', **changes})
            self.assertEqual(self.submit(data).status_code, 409)
        different = Part.objects.create(sku='DIFFERENT')
        SupplierItem.objects.filter(pk=self.item_a.pk).update(codigo='58411-1R000-G', brand='KSM', part=different)
        self.assertEqual(self.submit(data).status_code, 409)
        self.assertFalse(ClientRequestSubmission.objects.exists())
        self.assertEqual(StockEntry.objects.count(), 3)

    def test_line_snapshots_and_party_names_survive_later_catalog_and_supplier_changes(self):
        response = self.submit(self.payload([(self.item_a, 2)], notes='PENDIENTE'))
        request_id = response.data['requests'][0]['id']
        self.part.name, self.part.sku, self.part.description = 'NOMBRE NUEVO', 'SKU NUEVO', 'TEXTO NUEVO'
        self.part.save()
        SupplierItem.objects.filter(pk=self.item_a.pk).update(codigo='CODIGO NUEVO', brand='MARCA NUEVA', description='DESCRIPCION NUEVA')
        Account.objects.filter(pk=self.client_account.pk).update(name='CLIENTE NUEVO')
        Account.objects.filter(pk=self.supplier_a.pk).update(name='PROVEEDOR NUEVO')
        self.client.force_authenticate(self.seller_a)
        response = self.client.get(f'{self.url(self.supplier_a)}{request_id}/')
        self.assertEqual(response.data['client']['name'], 'CLIENTE ORIGINAL')
        line = response.data['lines'][0]
        self.assertEqual((line['sku'], line['name'], line['codigo'], line['brand'], line['description'], line['supplier_invent_id']),
                         ('58411-1R000', 'TAMBOR ORIGINAL', '58411-1R000-G', 'KSM', 'TAMBOR A-001', 'A-001'))
        self.assertEqual((line['supplier_item_id'], line['part_id']), (str(self.item_a.pk), str(self.part.pk)))
        self.assertIsNone(line['stock'])

    def test_review_is_idempotent_and_status_filters_and_search_preserve_total_counts(self):
        response = self.submit(self.payload([(self.item_a, 3), (self.item_a2, 2)]))
        request_id = response.data['requests'][0]['id']
        before = self.stock_snapshot()
        self.client.force_authenticate(self.seller_a)
        searched = self.client.get(self.url(self.supplier_a), {'search': '58411-1R000-KSM', 'status': 'pending'})
        self.assertEqual(searched.data['count'], 1)
        self.assertEqual((searched.data['results'][0]['line_count'], searched.data['results'][0]['unit_count']), (2, 5))
        path = f'{self.url(self.supplier_a)}{request_id}/review/'
        reviewed = self.client.post(path, {}, format='json')
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.data['status'], 'reviewed')
        self.assertIsNotNone(reviewed.data['reviewed_at'])
        again = self.client.post(path, {}, format='json')
        self.assertEqual(again.data, reviewed.data)
        self.assertEqual(SupplierRequest.objects.get(pk=request_id).reviewed_by_id, self.seller_a.pk)
        self.assertEqual(self.client.get(self.url(self.supplier_a), {'status': 'pending'}).data['count'], 0)
        self.assertEqual(self.client.get(self.url(self.supplier_a), {'status': 'reviewed'}).data['count'], 1)
        self.assertEqual(self.client.get(self.url(self.supplier_a), {'status': 'invalid'}).status_code, 400)
        self.assertEqual(self.stock_snapshot(), before)

    def test_supplier_request_list_paginates_without_exposing_other_supplier_requests(self):
        for _ in range(51):
            self.assertEqual(self.submit(self.payload([(self.item_a, 1), (self.item_b, 1)])).status_code, 200)
            # Reviewed orders cannot accept additions, so later carts create new orders.
            SupplierRequest.objects.filter(status='pending').update(status='reviewed')
        self.client.force_authenticate(self.seller_a)
        first = self.client.get(self.url(self.supplier_a))
        self.assertEqual(first.data['count'], 51)
        self.assertEqual(len(first.data['results']), 50)
        self.assertIsNotNone(first.data['next'])
        second = self.client.get(self.url(self.supplier_a), {'page': 2})
        self.assertEqual(len(second.data['results']), 1)
        self.assertIsNone(second.data['next'])
        self.assertEqual(SupplierRequest.objects.count(), 102)
        self.assertEqual(StockEntry.objects.count(), 3)
        self.client.force_authenticate(self.buyer)
        sent = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/sent-requests/')
        self.assertEqual((sent.data['count'], len(sent.data['results'])), (102, 50))
        self.assertIsNotNone(sent.data['next'])


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL account row locks.')
class ConcurrentRequestSubmissionTests(APITransactionTestCase):
    def test_simultaneous_identical_submissions_create_only_one_request_without_stock_mutation(self):
        buyer = User.objects.create_user('concurrent-buyer', 'concurrent-buyer@example.invalid', 'Request938!')
        seller = User.objects.create_user('concurrent-seller', 'concurrent-seller@example.invalid', 'Request938!')
        client = Account.objects.create(name='CLIENTE')
        client.roles.add(Role.objects.get_or_create(code='client_business', defaults={'name': 'CLIENTE', 'capability': 'client'})[0])
        Membership.objects.create(user=buyer, account=client)
        supplier = Account.objects.create(name='PROVEEDOR')
        supplier.roles.add(Role.objects.get_or_create(code='supplier_retail', defaults={'name': 'PROVEEDOR', 'capability': 'supplier'})[0])
        part = Part.objects.create(sku='CONCURRENT-SKU')
        item = ingest_inventory(supplier=supplier, actor=seller,
                                data={'update_id': 'seed', 'supplier_invent_id': 'STABLE', 'codigo': part.sku,
                                      'brand': '', 'source': 'upload', 'quantity': 5})
        payload = {'submission_id': str(uuid.uuid4()), 'lines': [{'supplier_item_id': str(item.pk), 'quantity': 2}]}
        url = f'/api/v1/accounts/{client.pk}/requests/'

        def submit():
            close_old_connections()
            try:
                api = APIClient()
                api.force_authenticate(User.objects.get(pk=buyer.pk))
                response = api.post(url, payload, format='json')
                return response.status_code, response.data
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: submit(), range(2)))
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(ClientRequestSubmission.objects.count(), 1)
        self.assertEqual(SupplierRequest.objects.count(), 1)
        self.assertEqual(SupplierRequestLine.objects.count(), 1)
        item.refresh_from_db()
        self.assertEqual((item.reported_quantity, item.reserved_quantity), (5, 0))
        self.assertEqual(StockEntry.objects.count(), 1)
        self.assertEqual(InventoryUpdate.objects.count(), 1)
