import uuid

from rest_framework.test import APITestCase

from . import test_deals as deal_tests
from . import test_supplier_import as import_tests
from .models import StockEntry, SupplierItem
from .request_models import StockReservation
from .services import ingest_inventory


class StockReservationTests(APITestCase):
    """A confirmed deal reserves its units on the supplier items; a stock upload asks, per confirmed order, whether the file's count
    already discounts it (the reservation is settled) or still includes it (it keeps holding the units)."""
    setUp = deal_tests.DealWorkflowTests.setUp
    account = deal_tests.DealWorkflowTests.account
    item = deal_tests.DealWorkflowTests.item
    url = deal_tests.DealWorkflowTests.url
    payload = deal_tests.DealWorkflowTests.payload
    submit = deal_tests.DealWorkflowTests.submit
    stock_snapshot = deal_tests.DealWorkflowTests.stock_snapshot
    order = deal_tests.DealWorkflowTests.order
    review = deal_tests.DealWorkflowTests.review
    action = deal_tests.DealWorkflowTests.action
    drafted = deal_tests.DealWorkflowTests.drafted
    quote = deal_tests.DealWorkflowTests.quote
    headers = import_tests.SupplierExcelImportTests.headers
    file = import_tests.SupplierExcelImportTests.file

    def handshake(self, quantities=None):
        """A confirmed deal for 3 of A-001 and 2 of A-002 (or the given quantities) -> the order."""
        self.client.force_authenticate(self.buyer)
        order = self.order()
        self.review(order)
        quantities = quantities or {}
        lines = [{'order_line_id': str(line.pk), 'quantity': quantities.get(line.supplier_item_id, line.quantity), 'unit_price': '12.35'}
                 for line in order.lines.all()]
        quotation = self.quote(order, lines).data['quotation']
        self.client.force_authenticate(self.buyer)
        accepted = self.action(order, 'accept', quotation_id=quotation['id'])
        self.assertEqual(accepted.status_code, 200, accepted.data)
        order.refresh_from_db()
        return order

    def counts(self, item):
        item = SupplierItem.objects.get(pk=item.pk)
        return item.reported_quantity, item.reserved_quantity, item.available_quantity

    def upload(self, a1=7, a2=10):
        self.client.force_authenticate(self.seller_a)
        rows = [['A-001', '58411-1R000-G', 'KSM', 'TAMBOR A-001', a1], ['A-002', '58411-1R000-KSM', 'KSM', 'TAMBOR A-002', a2]]
        response = self.client.post(f'/api/v1/accounts/{self.supplier_a.pk}/inventory/import/', {'file': self.file(rows)}, format='multipart')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def job_url(self, job):
        return f'/api/v1/accounts/{self.supplier_a.pk}/inventory/import/jobs/{job["job_id"]}/'

    def commit(self, job, decisions=None):
        return self.client.post(self.job_url(job), {'batch_index': 0, **({'decisions': decisions} if decisions is not None else {})}, format='json')

    def test_accepting_reserves_the_agreed_units_and_a_feed_keeps_them_held(self):
        order = self.handshake({self.item_a2.pk: 0})  # a line quoted at zero reserves nothing
        self.assertEqual(self.counts(self.item_a), (10, 5, 5))  # 2 held before + the 3 agreed
        self.assertEqual(self.counts(self.item_a2), (10, 0, 10))
        reservation = StockReservation.objects.get()
        self.assertEqual((reservation.item_id, reservation.order_id, reservation.quantity, reservation.status), (self.item_a.pk, order.pk, 3, 'open'))
        entry = StockEntry.objects.get(kind='reserve')
        self.assertEqual((entry.item_id, entry.quantity, entry.direction, entry.balance_type, entry.reserved_delta, entry.reserved_direction,
                          entry.reported_after, entry.reserved_after, entry.reference, entry.actor),
                         (self.item_a.pk, 3, 'credit', 'reservation', 3, 'credit', 10, 5, order.reference, self.buyer))
        self.assertIn('SE RESERVARON 3 UNIDADES DEL INVENTARIO DEL PROVEEDOR.', order.events.last().description)
        # An API feed has nobody to ask: the reservations keep holding their units against the new count.
        ingest_inventory(supplier=self.supplier_a, actor=self.seller_a, data={'update_id': 'feed-1', 'supplier_invent_id': 'A-001',
                                                                             'codigo': '58411-1R000-G', 'brand': 'KSM', 'description': 'TAMBOR A-001',
                                                                             'source': 'upload', 'quantity': 6})
        self.assertEqual(self.counts(self.item_a), (6, 5, 1))
        self.assertEqual(StockReservation.objects.get().status, 'open')

    def test_the_upload_asks_order_by_order_and_settles_only_what_the_file_discounts(self):
        first, second = self.handshake(), self.handshake()
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), ((10, 8, 2), (10, 4, 6)))
        job = self.upload(a1=7, a2=10)
        rows = {entry['supplier_invent_id']: entry for entry in job['reservations']}
        self.assertEqual({key: (entry['row'], entry['current_quantity'], entry['uploaded_quantity'], entry['reserved'], entry['reserved_quantity'])
                          for key, entry in rows.items()}, {'A-001': (2, 10, 7, 6, 8), 'A-002': (3, 10, 10, 4, 4)})
        self.assertEqual([(order['reference'], order['client_name'], order['quantity']) for order in rows['A-001']['orders']],
                         [(first.reference, 'CLIENTE ORIGINAL', 3), (second.reference, 'CLIENTE ORIGINAL', 3)])
        self.assertEqual({row['supplier_invent_id']: row['reserved'] for row in job['preview']}, {'A-001': 6, 'A-002': 4})
        self.assertEqual(job['summary']['reserved_rows'], 2)
        ids = {(key, order['reference']): order['id'] for key, entry in rows.items() for order in entry['orders']}

        before = self.stock_snapshot()
        refused = self.commit(job, {ids['A-001', first.reference]: 'net', ids['A-001', second.reference]: 'gross'})
        self.assertEqual(refused.status_code, 400)
        self.assertIn('filas 3', refused.data['detail'])  # A-002's orders have no answer
        self.assertEqual(self.commit(job).status_code, 400)
        self.assertEqual(self.stock_snapshot(), before)

        decisions = {ids['A-001', first.reference]: 'net', ids['A-001', second.reference]: 'gross',
                     ids['A-002', first.reference]: 'gross', ids['A-002', second.reference]: 'gross', str(uuid.uuid4()): 'net'}
        done = self.commit(job, decisions)
        self.assertEqual(done.status_code, 200, done.data)
        # A-001: the file's 7 already discounts the first order, which stops holding units; the second still holds its 3 (and the 2 held before).
        self.assertEqual(self.counts(self.item_a), (7, 5, 2))
        self.assertEqual(self.counts(self.item_a2), (10, 4, 6))
        self.assertEqual(sorted(StockReservation.objects.filter(status='settled').values_list('order__reference', 'item__supplier_invent_id')),
                         [(first.reference, 'A-001')])
        settled = StockReservation.objects.get(status='settled')
        self.assertEqual((settled.settled_by, settled.settled_reference), (self.seller_a, f'excel-{job["job_id"]}-2'))
        entry = StockEntry.objects.get(kind='settle')
        self.assertEqual((entry.item_id, entry.quantity, entry.direction, entry.balance_type, entry.reserved_after, entry.reported_after),
                         (self.item_a.pk, 3, 'debit', 'reservation', 5, 7))
        self.assertEqual((done.data['applied_summary']['settled_units'], done.data['applied_summary']['held_units']), (3, 7))
        # The job keeps the answers: a resumed import shows them instead of asking again.
        answered = {entry['supplier_invent_id']: entry for entry in self.client.get(self.job_url(job)).data['reservations']}
        self.assertEqual([(entry['applied'], [order['decision'] for order in entry['orders']]) for entry in (answered['A-001'], answered['A-002'])],
                         [(True, ['net', 'gross']), (True, ['gross', 'gross'])])
        # The next upload asks only about the orders still held.
        again = {entry['supplier_invent_id']: [order['reference'] for order in entry['orders']] for entry in self.upload()['reservations']}
        self.assertEqual(again, {'A-001': [second.reference], 'A-002': [first.reference, second.reference]})

    def test_a_deal_confirmed_after_the_review_makes_the_preview_stale(self):
        self.client.force_authenticate(self.buyer)
        order = self.order()
        self.review(order)
        quotation = self.quote(order).data['quotation']
        job = self.upload()
        self.assertEqual(job['reservations'], [])
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'accept', quotation_id=quotation['id']).status_code, 200)
        self.client.force_authenticate(self.seller_a)
        stale = self.commit(job, {})
        self.assertEqual(stale.status_code, 409)  # the new order must be in the heads-up: review the file again
        self.assertEqual(self.counts(self.item_a), (10, 5, 5))
        fresh = self.upload()
        self.assertEqual([entry['supplier_invent_id'] for entry in fresh['reservations']], ['A-001', 'A-002'])
