from django.db import IntegrityError, transaction
from rest_framework.test import APITestCase

from .models import Account, InventoryUpdate, Membership, Role, StockEntry, SupplierItem, User


class PositiveStockLedgerTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('stock-ledger', password='TestLedger938!')
        self.account = Account.objects.create(name='LEDGER SUPPLIER')
        self.account.roles.add(Role.objects.get(code='supplier_retail'))
        Membership.objects.create(user=self.user, account=self.account, permission='owner')
        self.client.force_authenticate(self.user)
        self.url = f'/api/v1/accounts/{self.account.pk}/inventory/ingest/'

    def update(self, event, quantity, invent='permanent-ID', **changes):
        data = {'update_id': event, 'supplier_invent_id': invent, 'codigo': 'ABC-123', 'brand': '', 'quantity': quantity, 'source': 'upload'}
        data.update(changes)
        return self.client.post(self.url, data, format='json')

    def test_absolute_balances_record_positive_credit_and_debit_amounts(self):
        self.assertEqual(self.update('seed', 5).status_code, 200)
        item = SupplierItem.objects.get()
        original_id = item.pk
        self.assertEqual(self.update('increase', 10).status_code, 200)
        movement = item.ledger.order_by('-id').first()
        self.assertEqual((movement.quantity, movement.direction, movement.reported_after), (5, 'credit', 10))
        self.assertEqual(self.update('seed-decrease', 5, invent='decrease-ID').status_code, 200)
        self.assertEqual(self.update('clear', 0, invent='decrease-ID').status_code, 200)
        cleared = SupplierItem.objects.get(supplier_invent_id='decrease-ID')
        movement = cleared.ledger.order_by('-id').first()
        self.assertEqual((movement.quantity, movement.direction, movement.reported_after), (5, 'debit', 0))
        item.refresh_from_db()
        self.assertEqual(item.pk, original_id)
        self.assertEqual(item.reported_quantity, 10)
        response = self.client.get(f'/api/v1/accounts/{self.account.pk}/inventory/{cleared.pk}/ledger/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'][0]['quantity'], 5)
        self.assertEqual(response.data['results'][0]['direction'], 'debit')
        self.assertEqual(response.data['results'][0]['balance_type'], 'stock')
        self.assertNotIn('stock_delta', response.data['results'][0])

    def test_unchanged_balances_and_retries_do_not_add_movements(self):
        self.assertEqual(self.update('seed', 5).status_code, 200)
        self.assertEqual(self.update('seed', 5).status_code, 200)
        self.assertEqual(self.update('unchanged', 5).status_code, 200)
        self.assertEqual(StockEntry.objects.count(), 1)
        self.assertEqual(InventoryUpdate.objects.count(), 2)
        self.assertEqual(self.update('seed', 0).status_code, 400)
        self.assertEqual(SupplierItem.objects.get().reported_quantity, 5)

    def test_invalid_balances_and_source_conflicts_leave_stock_and_history_unchanged(self):
        self.update('seed', 5)
        self.assertEqual(self.update('negative', -1).status_code, 400)
        self.assertEqual(self.update('other-source', 0, source='apiag').status_code, 400)
        self.assertEqual(SupplierItem.objects.get().reported_quantity, 5)
        self.assertEqual(StockEntry.objects.count(), 1)
        self.assertEqual(InventoryUpdate.objects.count(), 1)

    def test_database_rejects_negative_movements_and_invalid_directions(self):
        self.update('seed', 5)
        item = SupplierItem.objects.get()
        for changes in [{'quantity': -1}, {'direction': 'bogus'}, {'reserved_delta': -1}, {'reserved_delta': 1, 'reserved_direction': ''}, {'reserved_delta': 0, 'reserved_direction': 'debit'}]:
            data = {'item': item, 'actor': self.user, 'kind': 'sync', 'quantity': 1, 'direction': 'credit', 'reported_after': 5, 'reserved_after': 0, 'reference': 'invalid'}
            data.update(changes)
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                StockEntry.objects.create(**data)
        self.assertEqual(StockEntry.objects.count(), 1)
