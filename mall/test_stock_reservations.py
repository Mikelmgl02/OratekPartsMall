import io
import uuid

from django.db import IntegrityError, connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from . import test_deals as deal_tests
from . import test_supplier_import as import_tests
from datetime import timedelta

from django.utils import timezone

from .check_stock_ledger import check, main, render
from .models import DealEvent, StockEntry, SupplierItem
from .pricing_models import SupplierPricingSettings
from .request_models import StockReservation
from .services import ingest_inventory
from .stock_reservations import expire_due


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
                          entry.reported_after, entry.reserved_after, entry.reference, entry.actor, entry.reservation),
                         (self.item_a.pk, 3, 'credit', 'reservation', 3, 'credit', 10, 5, f'{order.reference} · cotización v1', self.seller_a, reservation))
        self.assertIsNone(reservation.expires_at)  # held since the quotation was sent; confirmed, it no longer lapses
        self.assertIn('EL ACUERDO RESERVA 3 UNIDADES DEL INVENTARIO DEL PROVEEDOR.', order.events.last().description)
        # An API feed has nobody to ask: the reservations keep holding their units against the new count.
        ingest_inventory(supplier=self.supplier_a, actor=self.seller_a, data={'update_id': 'feed-1', 'supplier_invent_id': 'A-001',
                                                                             'codigo': '58411-1R000-G', 'brand': 'KSM', 'description': 'TAMBOR A-001',
                                                                             'source': 'upload', 'quantity': 6})
        self.assertEqual(self.counts(self.item_a), (6, 5, 1))
        self.assertEqual(StockReservation.objects.get().status, 'open')

    def sent(self, quantities=None, order=None):
        """The supplier sends a quotation for 3 of A-001 and 2 of A-002 (or the given quantities) -> (order, quotation)."""
        self.client.force_authenticate(self.buyer)
        if order is None:
            order = self.order()
            self.review(order)
        quantities = quantities or {}
        lines = [{'order_line_id': str(line.pk), 'quantity': quantities.get(line.supplier_item_id, line.quantity), 'unit_price': '12.35'}
                 for line in order.lines.all()]
        self.client.force_authenticate(self.seller_a)
        # A new version of a sent quotation goes through its draft, as the supplier's editor sends it.
        response = self.action(order, 'quote', lines=lines, draft_version=self.drafted(order, lines)) if order.status == 'quoted' else self.quote(order, lines)
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        return order, response.data['quotation']

    def test_a_sent_quotation_holds_its_units_until_a_new_version_replaces_it_and_the_hold_lapses(self):
        SupplierItem.objects.filter(pk=self.item_a.pk).update(reserved_quantity=0)  # the fixture's 2 units were never recorded
        SupplierPricingSettings.objects.create(supplier=self.supplier_a, quote_hold_days=2)
        order, first = self.sent()
        # Sent, its units leave the available stock the supplier's other quotations see, for the supplier's 2 days.
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), ((10, 3, 7), (10, 2, 8)))
        holds = StockReservation.objects.filter(order=order, status='open')
        self.assertEqual(sorted(holds.values_list('quantity', flat=True)), [2, 3])
        self.assertTrue(all(timedelta(days=2) - timedelta(minutes=1) < hold.expires_at - timezone.now() <= timedelta(days=2) for hold in holds))
        self.assertRegex(order.events.last().description, r'RESERVA 5 UNIDADES DEL INVENTARIO DEL PROVEEDOR HASTA EL \d\d/\d\d/\d{4} \d\d:\d\d\.$')
        self.client.force_authenticate(self.seller_a)
        lines = {line['codigo']: line['stock'] for line in self.client.get(f'{self.url(self.supplier_a)}{order.pk}/').data['lines']}
        # Its own units count for it: nothing to confirm, and they show apart.
        self.assertEqual((lines['58411-1R000-G']['available_quantity'], lines['58411-1R000-G']['held_for_deal'], lines['58411-1R000-G']['shortfall']), (7, 3, 0))
        # Another order sees only what is left; the draft of a new version of this one counts its own hold.
        self.client.force_authenticate(self.buyer)
        other = self.order()
        self.review(other)
        self.client.force_authenticate(self.seller_a)
        draft = lambda row: {line['codigo']: line['stock']['available_quantity'] for line in self.client.get(
            f'/api/v1/accounts/{self.supplier_a.pk}/requests/{row.pk}/draft/').data['draft']['lines']}
        self.assertEqual(draft(other), {'58411-1R000-G': 7, '58411-1R000-KSM': 8})
        self.assertEqual(draft(order), {'58411-1R000-G': 10, '58411-1R000-KSM': 10})

        order, second = self.sent({self.item_a.pk: 1}, order=order)  # v2 offers only 1 of A-001
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), ((10, 1, 9), (10, 2, 8)))
        self.assertEqual(set(StockEntry.objects.filter(kind='release').values_list('reference', flat=True)), {f'{order.reference} · v2 la reemplaza'})
        self.assertEqual(StockReservation.objects.filter(order=order, status='open', quotation_line__quotation_id=second['id']).count(), 2)
        self.assertEqual(check()['problems'], {})

        self.assertEqual(expire_due(), 0)  # nothing due yet
        StockReservation.objects.filter(order=order, status='open').update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(expire_due(), 1)
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), ((10, 0, 10), (10, 0, 10)))
        expired = DealEvent.objects.get(order=order, kind='hold_expired')
        self.assertEqual((expired.account, str(expired.quotation_id)), (None, second['id']))
        self.assertIn('VENCIÓ LA RESERVA DE LA COTIZACIÓN V2: 3 UNIDADES VUELVEN A ESTAR DISPONIBLES.', expired.description)
        self.client.force_authenticate(self.buyer)
        self.assertNotIn('hold_expired', [row['kind'] for row in self.client.get('/api/v1/inbox/notifications/').data['results']])  # Actividad only
        order.refresh_from_db()
        self.assertEqual(order.status, 'quoted')  # the quotation stays
        self.assertEqual(check()['problems'], {})

        # Confirmed after the hold lapsed: the stock is checked again and the deal reserves what it agreed, without expiry.
        accepted = self.action(order, 'accept', quotation_id=second['id'])
        self.assertEqual(accepted.status_code, 200, accepted.data)
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), ((10, 1, 9), (10, 2, 8)))
        self.assertFalse(StockReservation.objects.filter(order=order, status='open', expires_at__isnull=False).exists())
        self.assertEqual(expire_due(), 0)
        self.assertEqual(check()['problems'], {})

    def test_a_rejected_order_frees_its_quotation_and_an_upload_never_asks_about_quotations(self):
        SupplierItem.objects.filter(pk=self.item_a.pk).update(reserved_quantity=0)
        order, quotation = self.sent()
        job = self.upload(a1=9, a2=10)
        self.assertEqual(job['reservations'], [])  # nothing was sold yet: the file's count cannot discount it
        self.assertEqual(self.commit(job).status_code, 200)
        self.assertEqual(self.counts(self.item_a), (9, 3, 6))  # still held
        self.client.force_authenticate(self.buyer)
        rejected = self.action(order, 'reject', quotation_id=quotation['id'], reason_code='price_too_high', reason='')
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), ((9, 0, 9), (10, 0, 10)))
        self.assertFalse(StockReservation.objects.filter(order=order, status='open').exists())
        self.assertEqual(check()['problems'], {})

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
        self.assertEqual({order['delivered_at'] for entry in job['reservations'] for order in entry['orders']}, {None})
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
        entry = StockEntry.objects.get(kind='settle')  # the order's units leave the reserved count under its own reference
        self.assertEqual((entry.item_id, entry.quantity, entry.direction, entry.balance_type, entry.reserved_after, entry.reported_after,
                          entry.reference, entry.reservation),
                         (self.item_a.pk, 3, 'debit', 'reservation', 5, 7, f'{first.reference} · excel-{job["job_id"]}-2', settled))
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

    def test_each_settled_order_gets_its_own_entry_and_a_count_below_the_orders_refuses_the_batch(self):
        SupplierItem.objects.filter(pk=self.item_a.pk).update(reserved_quantity=0)  # the fixture's 2 units were never recorded
        first, second = self.handshake(), self.handshake()
        job = self.upload(a1=4, a2=10)
        decisions = {order['id']: 'net' for entry in job['reservations'] for order in entry['orders']}
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.commit(job, decisions).status_code, 200)
        # Settling locks the reservation rows only, never their orders (which a deal action locks before items).
        locks = [query['sql'] for query in queries.captured_queries if 'FROM "mall_stockreservation"' in query['sql'] and 'FOR UPDATE' in query['sql']]
        self.assertTrue(locks)
        self.assertTrue(all(sql.endswith('FOR UPDATE OF "mall_stockreservation"') for sql in locks), locks)
        moves = list(StockEntry.objects.filter(item=self.item_a, kind='settle').order_by('id').values_list(
            'reference', 'quantity', 'reported_after', 'reserved_after', 'reservation__order'))
        row = f'excel-{job["job_id"]}-2'
        self.assertEqual(moves, [(f'{first.reference} · {row}', 3, 4, 3, first.pk), (f'{second.reference} · {row}', 3, 4, 0, second.pk)])
        self.assertEqual(self.counts(self.item_a), (4, 0, 4))
        self.assertEqual(check()['problems'], {})

        third = self.handshake()
        SupplierItem.objects.filter(pk=self.item_a2.pk).update(reserved_quantity=1)  # fewer than the 2 its open order holds
        before = self.stock_snapshot()
        job = self.upload(a1=4, a2=10)
        refused = self.commit(job, {order['id']: 'net' for entry in job['reservations'] for order in entry['orders']})
        self.assertEqual(refused.status_code, 422)
        self.assertIn('no cuadran', refused.data['detail'])
        self.assertEqual(self.stock_snapshot(), before)  # the whole batch rolled back, A-001 included
        self.assertEqual(StockReservation.objects.get(order=third, item=self.item_a2).status, 'open')

    def test_the_ledger_check_reconciles_a_deal_and_an_import_and_names_each_break(self):
        SupplierItem.objects.filter(pk=self.item_a.pk).update(reserved_quantity=0)  # the fixture's 2 units were never recorded
        first, second = self.handshake(), self.handshake()
        job = self.upload(a1=7, a2=10)
        ids = {(entry['supplier_invent_id'], order['reference']): order['id'] for entry in job['reservations'] for order in entry['orders']}
        committed = self.commit(job, {ids['A-001', first.reference]: 'net', ids['A-001', second.reference]: 'gross',
                                      ids['A-002', first.reference]: 'gross', ids['A-002', second.reference]: 'gross'})
        self.assertEqual(committed.status_code, 200, committed.data)
        self.assertEqual((self.counts(self.item_a), StockEntry.objects.filter(kind='settle').count()), ((7, 3, 4), 1))
        result = check()
        self.assertEqual((result['problems'], result['reservations']), ({}, 4))
        output = io.StringIO()
        self.assertEqual(main([], stdout=output), 0)
        self.assertIn('Todo cuadra.', output.getvalue())

        SupplierItem.objects.filter(pk=self.item_b.pk).update(reported_quantity=99)  # a count changed without an entry
        StockEntry.objects.filter(item=self.item_a, kind='reserve').order_by('id').filter(
            pk=StockEntry.objects.filter(item=self.item_a, kind='reserve').order_by('id').values('pk')[:1]).update(reserved_after=9)
        StockReservation.objects.filter(order=second, item=self.item_a2).update(status='settled')  # settled without its entry
        SupplierItem.objects.create(supplier=self.supplier_a, supplier_invent_id='SIN-HISTORIAL', codigo='X-1', source='upload', reported_quantity=4)
        problems = check(examples=5)['problems']
        # The edited balance breaks the link before it and the one after it.
        self.assertEqual({kind: detail['count'] for kind, detail in problems.items()},
                         {'chain': 2, 'final': 1, 'unrecorded': 1, 'reserved': 1, 'reservation': 1})
        self.assertIn('B-001: el historial termina en 20/0, el artículo tiene 99/0', problems['final']['examples'])
        self.assertIn(f'{second.reference} en A-002 (settled)', problems['reservation']['examples'][0])
        output = io.StringIO()
        self.assertEqual(main(['--examples', '1'], stdout=output), 1)
        self.assertIn('- Artículos cuyo saldo no coincide con su historial: 1\n    B-001', output.getvalue())

    def test_a_confirmation_takes_key_share_on_both_accounts_before_locking_items(self):
        self.client.force_authenticate(self.buyer)
        order = self.order()
        self.review(order)
        quotation = self.quote(order).data['quotation']
        self.client.force_authenticate(self.buyer)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.action(order, 'accept', quotation_id=quotation['id']).status_code, 200)
        sql = [query['sql'] for query in queries.captured_queries]
        share = next(index for index, text in enumerate(sql) if text.endswith('FOR KEY SHARE'))
        items = next(index for index, text in enumerate(sql) if 'FROM "mall_supplieritem"' in text and 'FOR UPDATE' in text)
        self.assertLess(share, items)  # an import or a send holding an account never waits for items this confirmation holds
        self.assertIn(str(self.supplier_a.pk).replace('-', ''), sql[share].replace('-', ''))
        self.assertIn(str(self.client_account.pk).replace('-', ''), sql[share].replace('-', ''))

    def test_the_database_rejects_each_malformed_entry_by_its_own_rule(self):
        self.handshake()
        reservation = StockReservation.objects.filter(item=self.item_a).first()
        base = {'item': self.item_a, 'actor': self.seller_a, 'reference': 'invalid', 'reported_after': 10, 'reserved_after': 5,
                'kind': 'reserve', 'balance_type': 'reservation', 'quantity': 1, 'direction': 'credit', 'reserved_delta': 1,
                'reserved_direction': 'credit', 'reservation': reservation}
        cases = [
            ({'kind': 'sync', 'balance_type': 'stock', 'reserved_delta': 0, 'reserved_direction': ''}, 'stock_entry_reservation_link'),
            ({'kind': 'settle', 'direction': 'debit', 'reserved_direction': 'debit', 'reservation': None}, 'stock_entry_reservation_link'),
            ({'kind': 'settle'}, 'stock_entry_kind_shape'),
            ({'direction': 'debit', 'reserved_direction': 'debit'}, 'stock_entry_kind_shape'),
            ({'kind': 'delivery'}, 'stock_entry_kind_shape'),
            ({'kind': 'release', 'direction': 'debit'}, 'stock_entry_reservation_amount'),
            ({'kind': 'release', 'direction': 'debit', 'reserved_direction': 'debit', 'reserved_delta': 2}, 'stock_entry_reservation_amount'),
            ({'kind': 'sync', 'balance_type': 'stock', 'reserved_delta': 0, 'reserved_direction': '', 'reservation': None, 'quantity': 0},
             'stock_entry_quantity_positive'),
            ({'kind': 'release', 'direction': 'debit', 'reserved_direction': 'debit', 'reservation': None}, 'stock_entry_reservation_link'),
            ({'kind': 'delivery', 'balance_type': 'stock', 'direction': 'debit', 'reserved_delta': 0, 'reserved_direction': ''}, 'stock_entry_kind_shape'),
            ({'kind': 'shortfall', 'balance_type': 'stock', 'direction': 'credit'}, 'stock_entry_kind_shape'),
        ]
        for changes, rule in cases:
            with self.subTest(rule=rule, changes=changes), self.assertRaises(IntegrityError) as raised, transaction.atomic():
                StockEntry.objects.create(**{**base, **changes})
            self.assertIn(rule, str(raised.exception))
        # A shortfall (or a delivery, which no writer uses yet) takes units from the count and the reservation together, or from the
        # reservation alone when the count has none left.
        for changes in [{'kind': 'release', 'direction': 'debit', 'reserved_direction': 'debit'},
                        {'kind': 'shortfall', 'balance_type': 'stock', 'direction': 'debit', 'reserved_delta': 2, 'reserved_direction': 'debit'},
                        {'kind': 'shortfall', 'direction': 'debit', 'reserved_direction': 'debit'},
                        {'kind': 'delivery', 'balance_type': 'stock', 'direction': 'debit', 'reserved_direction': 'debit'}]:
            with self.subTest(accepted=changes), transaction.atomic():
                StockEntry.objects.create(**{**base, **changes})
                transaction.set_rollback(True)

    def test_the_ledger_check_edges_items_at_zero_reserved_only_items_dates_and_the_examples_cap(self):
        SupplierItem.objects.filter(pk=self.item_a.pk).update(reserved_quantity=0)  # the fixture's 2 units were never recorded
        SupplierItem.objects.create(supplier=self.supplier_a, supplier_invent_id='EN-CERO', codigo='X-0', source='upload')
        self.assertEqual(check()['problems'], {})  # an item created at zero writes no entry and is fine
        SupplierItem.objects.create(supplier=self.supplier_a, supplier_invent_id='SOLO-RESERVA', codigo='X-2', source='upload', reserved_quantity=2)
        self.assertEqual(check()['problems']['unrecorded']['examples'], ['SOLO-RESERVA: 0 existencias, 2 reservadas'])
        SupplierItem.objects.filter(supplier_invent_id='SOLO-RESERVA').update(reserved_quantity=0)
        ingest_inventory(supplier=self.supplier_a, actor=self.seller_a, data={'update_id': 'later', 'supplier_invent_id': 'A-001',
                                                                             'codigo': '58411-1R000-G', 'brand': 'KSM', 'description': 'TAMBOR A-001',
                                                                             'source': 'upload', 'quantity': 6})
        first, later = StockEntry.objects.filter(item=self.item_a).order_by('id')
        StockEntry.objects.filter(pk=later.pk).update(created_at=first.created_at.replace(year=first.created_at.year - 1))
        result = check()
        self.assertEqual((result['problems'], result['out_of_order']), ({}, 1))  # a clock step back is a warning, not a break
        self.assertIn('Aviso: 1 movimientos tienen una fecha anterior', render(result))
        for item in (self.item_a2, self.item_b):
            SupplierItem.objects.filter(pk=item.pk).update(reported_quantity=77)
        problems = check(examples=1)['problems']
        self.assertEqual((problems['final']['count'], len(problems['final']['examples'])), (2, 1))


class LedgerCheckSnapshotTests(TransactionTestCase):
    """Outside a caller's transaction the check opens its own read-only REPEATABLE READ snapshot."""

    def test_the_check_reads_one_read_only_snapshot(self):
        self.assertFalse(connection.in_atomic_block)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(check()['problems'], {})
        self.assertEqual([query['sql'] for query in queries.captured_queries[:2]], ['BEGIN', 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'])
