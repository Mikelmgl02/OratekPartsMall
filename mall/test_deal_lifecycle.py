import importlib
import json
import uuid
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APITestCase

from . import test_deals as deal_tests
from .check_stock_ledger import check
from .deal_views import DECIDE_DENIED, DealActionSerializer
from .models import Membership, Part, Role, StockEntry, SupplierItem, User
from .pricing_models import SupplierPricingSettings
from .request_models import DealEvent, DealShortfall, StockReservation, SupplierRequest, SupplierRequestLine
from .test_factories import make_line, make_variant


class DealLifecycleTests(APITestCase):
    """After the handshake: the supplier's steps up to Entregado and the client's Recibido, a shortfall the client accepts or answers by
    cancelling, cancellations by either party or by MotionPartes, the supplier's reliability, and the ledger staying reconciled through all
    of it."""
    account = deal_tests.DealWorkflowTests.account
    item = deal_tests.DealWorkflowTests.item
    url = deal_tests.DealWorkflowTests.url
    payload = deal_tests.DealWorkflowTests.payload
    submit = deal_tests.DealWorkflowTests.submit
    order = deal_tests.DealWorkflowTests.order
    review = deal_tests.DealWorkflowTests.review
    action = deal_tests.DealWorkflowTests.action
    drafted = deal_tests.DealWorkflowTests.drafted
    quote = deal_tests.DealWorkflowTests.quote

    def setUp(self):
        deal_tests.DealWorkflowTests.setUp(self)
        SupplierItem.objects.filter(pk=self.item_a.pk).update(reserved_quantity=0)  # the fixture's 2 units were never recorded

    def handshake(self):
        """A confirmed deal for 3 of A-001 and 2 of A-002 at 12.35 each."""
        self.client.force_authenticate(self.buyer)
        order = self.order()
        self.review(order)
        quotation = self.quote(order).data['quotation']
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'accept', quotation_id=quotation['id']).status_code, 200)
        order.refresh_from_db()
        return order, quotation['id']

    def supplier(self, order, action, quotation, **extra):
        self.client.force_authenticate(self.seller_a)
        return self.action(order, action, account=self.supplier_a, quotation_id=quotation, **extra)

    def buyer_does(self, order, action, quotation, **extra):
        self.client.force_authenticate(self.buyer)
        return self.action(order, action, account=self.client_account, quotation_id=quotation, **extra)

    def lines(self, order):
        return {line.supplier_item_id: str(line.pk) for line in order.lines.all()}

    def counts(self, item):
        item = SupplierItem.objects.get(pk=item.pk)
        return item.reported_quantity, item.reserved_quantity, item.available_quantity

    def state(self):
        """The client's request-state totals of the deals' part (both items are of it)."""
        self.client.force_authenticate(self.buyer)
        return self.client.get(f'/api/v1/accounts/{self.client_account.pk}/catalog/{self.part.pk}/request-state/').data['totals']

    def test_open_units_count_what_was_asked_then_what_was_agreed_until_the_client_receives_it(self):
        def open_units():
            self.client.force_authenticate(self.buyer)
            return self.client.get(f'/api/v1/accounts/{self.client_account.pk}/open-units/').data['parts']
        part = str(self.part.pk)
        self.assertEqual(open_units(), {})
        order = self.order()
        self.assertEqual(open_units(), {part: {'units': 5, 'orders': 1}})  # what was asked for (both items are of the part)
        self.review(order)
        lines = self.lines(order)
        quoted = self.quote(order, lines=[{'order_line_id': lines[self.item_a.pk], 'quantity': 1, 'unit_price': '12.35'},
                                          {'order_line_id': lines[self.item_a2.pk], 'quantity': 2, 'unit_price': '12.35'}])
        self.assertEqual(quoted.status_code, 200, quoted.data)
        quotation = quoted.data['quotation']['id']
        self.assertEqual(open_units(), {part: {'units': 5, 'orders': 1}})  # until the handshake, still what was asked for
        catalog = f'/api/v1/catalog/?account={self.client_account.pk}'
        self.assertEqual([row['id'] for row in self.client.get(f'{catalog}&mine=ordenes').data['results']], [part])
        self.assertEqual(self.client.get(f'{catalog}&mine=cesta').data['count'], 0)
        self.assertEqual(self.client.get(f'{catalog}&mine=cesta&mine=ordenes').data['count'], 1)  # either one
        self.assertEqual(self.client.get(f'{catalog}&mine=otro').status_code, 400)
        self.assertEqual(self.client.get('/api/v1/catalog/?mine=ordenes').status_code, 400)  # the orders need a client account
        self.assertEqual(self.client.get('/api/v1/catalog/?mine=favoritos').data['count'], 0)  # favoritos need none
        self.assertEqual(self.client.put(f'/api/v1/wishlist/{self.part.pk}/').status_code, 200)
        self.assertEqual([row['id'] for row in self.client.get('/api/v1/catalog/?mine=favoritos').data['results']], [part])

        self.assertEqual(self.buyer_does(order, 'accept', quotation).status_code, 200)
        self.assertEqual(open_units(), {part: {'units': 3, 'orders': 1}})  # then what the quotation agreed
        self.assertEqual(self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': lines[self.item_a2.pk], 'missing': 2}],
                                       reason_code='no_stock').status_code, 200)
        self.assertEqual(open_units()[part]['units'], 3)  # a shortfall counts once the client accepts it
        self.assertEqual(self.buyer_does(order, 'accept_shortfall', quotation).status_code, 200)
        self.assertEqual(open_units(), {part: {'units': 1, 'orders': 1}})
        [entry] = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/open-units/{part}/').data['orders']
        self.assertEqual((entry['units'], entry['requested'], sorted((line['codigo'], line['requested'], line['units']) for line in entry['lines'])),
                         (1, 5, [('58411-1R000-G', 3, 1), ('58411-1R000-KSM', 2, 0)]))  # agreed less the accepted shortfall, beside what was asked
        self.assertEqual(self.supplier(order, 'process', quotation).status_code, 200)
        self.assertEqual(self.supplier(order, 'deliver', quotation).status_code, 200)
        self.assertEqual(open_units()[part]['units'], 1)  # delivered still waits for the client's Recibido
        self.assertEqual(self.buyer_does(order, 'receive', quotation).status_code, 200)
        self.assertEqual(open_units(), {})
        self.assertEqual(self.client.get(f'/api/v1/accounts/{self.client_account.pk}/open-units/{part}/').data['orders'], [])  # received: none open
        self.assertEqual(self.client.get(f'{catalog}&mine=ordenes').data['count'], 0)
        self.client.force_authenticate(self.seller_a)  # a client's own figures only
        self.assertEqual(self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/open-units/').status_code, 403)
        self.assertEqual(self.client.get(f'/api/v1/catalog/?account={self.supplier_a.pk}&mine=ordenes').status_code, 403)

    def test_open_units_count_only_orders_that_hold_units_and_only_skus_the_catalog_lists(self):
        def open_units():
            self.client.force_authenticate(self.buyer)
            return self.client.get(f'/api/v1/accounts/{self.client_account.pk}/open-units/').data['parts']
        other = make_line('OTRO-SKU', name='OTRO', description='OTRO')
        item_o = make_variant(self.supplier_a, 'O-001', 'OTRO-1', 10, actor=self.seller_a, brand='KSM', description='OTRO', line=other)
        first, _ = self.handshake()  # 3 + 2 of the part, agreed
        self.client.force_authenticate(self.buyer)
        response = self.submit(self.payload([(self.item_a, 2), (item_o, 1)]))
        self.assertEqual(response.status_code, 200, response.data)
        second = SupplierRequest.objects.get(pk=response.data['requests'][0]['id'])
        self.assertNotEqual(second.pk, first.pk)
        self.review(second)
        lines = self.lines(second)
        quoted = self.quote(second, lines=[{'order_line_id': lines[self.item_a.pk], 'quantity': 0, 'unit_price': '12.35'},
                                           {'order_line_id': lines[item_o.pk], 'quantity': 1, 'unit_price': '12.35'}])
        self.assertEqual(quoted.status_code, 200, quoted.data)
        self.assertEqual(self.buyer_does(second, 'accept', quoted.data['quotation']['id']).status_code, 200)
        # The second order quoted none of the part: it holds no unit of it, so it is not one of its orders.
        self.assertEqual(open_units(), {str(self.part.pk): {'units': 5, 'orders': 1}, str(other.pk): {'units': 1, 'orders': 1}})
        # Its summary: the open orders that hold units of the SKU, newest first, with the lines they come from.
        summary = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/open-units/{self.part.pk}/').data
        [entry] = summary['orders']
        self.assertEqual({key: entry[key] for key in ('id', 'reference', 'status', 'units', 'requested')},
                         {'id': str(first.pk), 'reference': first.reference, 'status': 'handshaked', 'units': 5, 'requested': 5})
        self.assertEqual((entry['supplier']['name'], sorted((line['codigo'], line['requested'], line['units']) for line in entry['lines'])),
                         ('PROVEEDOR A', [('58411-1R000-G', 3, 3), ('58411-1R000-KSM', 2, 2)]))
        # A line on a SKU that merged into the card's SKU counts under the card's SKU, in the badge and in its summary.
        old = Part.objects.create(sku='OLD-SKU', name='VIEJO', active=False, merged_into=self.part)
        SupplierRequestLine.objects.filter(request=first, supplier_item=self.item_a2).update(part=old, sku=old.sku)
        self.assertEqual(open_units()[str(self.part.pk)], {'units': 5, 'orders': 1})
        [entry] = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/open-units/{self.part.pk}/').data['orders']
        self.assertEqual((entry['id'], entry['units']), (str(first.pk), 5))
        [entry] = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/open-units/{other.pk}/').data['orders']
        self.assertEqual((entry['id'], entry['units']), (str(second.pk), 1))
        # A newer order, still pending, holds what it asked for and comes first; the second order (none of the part) stays out.
        response = self.submit(self.payload([(self.item_a, 4)]))
        self.assertEqual(response.status_code, 200, response.data)
        third = SupplierRequest.objects.get(pk=response.data['requests'][0]['id'])
        orders = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/open-units/{self.part.pk}/').data['orders']
        self.assertEqual([(order['id'], order['status'], order['units'], order['requested']) for order in orders],
                         [(str(third.pk), 'pending', 4, 4), (str(first.pk), 'handshaked', 5, 5)])
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/open-units/{self.part.pk}/').status_code, 403)  # a client's own
        self.assertEqual(self.client.get(f'/api/v1/accounts/{self.client_account.pk}/open-units/{self.part.pk}/').status_code, 404)  # not a member
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.client.put(f'/api/v1/wishlist/{other.pk}/').status_code, 200)
        other.estado = 'retirada'
        other.save()
        self.assertNotIn(str(other.pk), open_units())  # only SKUs the catalog lists
        state = self.client.get('/api/v1/wishlist/state/').data
        self.assertEqual(([str(pk) for pk in state['part_ids']], state['listed_ids']), ([str(other.pk)], []))  # a heart, not a Favoritos result

    def test_sent_requests_summary_counts_each_stage_and_what_the_client_waits_on(self):
        url = f'/api/v1/accounts/{self.client_account.pk}/sent-requests/summary/'

        def summary():
            self.client.force_authenticate(self.buyer)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.data)
            return response.data
        empty = summary()
        self.assertEqual((empty['total'], set(empty['statuses'].values()), empty['oldest'], empty['to_confirm'], empty['in_progress'], empty['to_receive'],
                          empty['shortfall_pending'], empty['sent_this_month'], empty['received_this_month']), (0, {0}, {}, [], [], [], 0, 0, 0))
        first, quotation = self.handshake()  # 3 + 2 at 12.35
        lines = self.lines(first)
        self.assertEqual(self.supplier(first, 'report_shortfall', quotation, shortfall=[{'order_line_id': lines[self.item_a2.pk], 'missing': 2}],
                                       reason_code='no_stock').status_code, 200)
        data = summary()
        # A shortfall waiting for the client is flagged; the agreement still counts its units until the client accepts it.
        self.assertEqual((data['statuses']['handshaked'], data['shortfall_pending'], data['in_progress']), (1, 1, [{'currency': 'USD', 'total': '61.75'}]))
        self.assertEqual(self.buyer_does(first, 'accept_shortfall', quotation).status_code, 200)
        self.assertEqual((summary()['shortfall_pending'], summary()['in_progress']), (0, [{'currency': 'USD', 'total': '37.05'}]))  # 3 × 12.35
        # A second order quoted and waiting for the client (its latest revision), a third still with the supplier.
        self.client.force_authenticate(self.buyer)
        second = self.order()
        self.review(second)
        quoted = self.quote(second)
        self.assertEqual(quoted.status_code, 200)
        self.client.force_authenticate(self.buyer)
        third = self.order()
        SupplierRequest.objects.filter(pk=third.pk).update(created_at=timezone.now() - timedelta(days=3))
        data = summary()
        self.assertEqual({status: count for status, count in data['statuses'].items() if count}, {'pending': 1, 'quoted': 1, 'handshaked': 1})
        self.assertEqual((data['total'], data['sent_this_month'] >= 2, data['to_confirm']), (3, True, [{'currency': 'USD', 'total': '61.75'}]))
        third.refresh_from_db()
        self.assertEqual(data['oldest'], {'pending': third.created_at.isoformat()})
        # An adjustment waits on the supplier since the client asked for it, however long ago the order was sent.
        SupplierRequest.objects.filter(pk=second.pk).update(created_at=timezone.now() - timedelta(days=9))
        self.assertEqual(self.buyer_does(second, 'request_adjustment', quoted.data['quotation']['id'], reason='SOLO DOS').status_code, 200)
        asked = DealEvent.objects.filter(order=second, kind='request_adjustment').latest('id').created_at
        data = summary()
        self.assertEqual((data['oldest']['adjustment'], data['statuses']['adjustment'], data['to_confirm']), (asked.isoformat(), 1, []))
        # Delivered, it waits for the client's Recibido; received, it counts in this month's.
        self.assertEqual(self.supplier(first, 'process', quotation).status_code, 200)
        self.assertEqual(self.supplier(first, 'deliver', quotation).status_code, 200)
        data = summary()
        self.assertEqual((data['statuses']['delivered'], data['in_progress'], data['to_receive']), (1, [], [{'currency': 'USD', 'total': '37.05'}]))
        self.assertEqual(self.buyer_does(first, 'receive', quotation).status_code, 200)
        data = summary()
        self.assertEqual((data['statuses']['received'], data['received_this_month'], data['to_receive']), (1, 1, []))
        # A client's own figures: a supplier member is refused, and another account's figures stay out.
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/sent-requests/summary/').status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_delivery_closes_the_deal_without_moving_stock(self):
        order, quotation = self.handshake()
        before = (self.counts(self.item_a), self.counts(self.item_a2))
        self.assertEqual(self.supplier(order, 'process', quotation).status_code, 200)
        self.assertEqual(self.buyer_does(order, 'deliver', quotation).status_code, 403)  # the supplier's decision
        delivered = self.supplier(order, 'deliver', quotation)
        self.assertEqual(delivered.status_code, 200, delivered.data)
        self.assertEqual((delivered.data['status'], bool(delivered.data['delivered_at']), delivered.data['received_at']), ('delivered', True, None))
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), before)  # no second deduction at fulfilment
        self.assertEqual(set(StockReservation.objects.values_list('status', flat=True)), {'open'})  # until the supplier's count shows it
        self.assertEqual(order.events.last().description,
                         'EL PROVEEDOR MARCÓ LA ORDEN COMO ENTREGADA: 5 UNIDADES. FALTA QUE EL CLIENTE CONFIRME QUE LA RECIBIÓ.')
        self.assertEqual(self.supplier(order, 'deliver', quotation).status_code, 409)
        self.assertEqual(self.supplier(order, 'cancel', quotation, reason_code='no_stock').status_code, 409)
        self.assertEqual(self.buyer_does(order, 'cancel', quotation, reason_code='not_needed').status_code, 409)
        received = self.buyer_does(order, 'receive', quotation)
        self.assertEqual((received.status_code, received.data['status']), (200, 'received'))
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), before)  # the client's Recibido moves no stock either
        state = self.state()
        self.assertEqual((state['handshaked_quantity'], state['delivered_quantity']), (0, 5))
        self.assertEqual(check()['problems'], {})

    def test_delivery_settles_what_apiag_cloud_items_hold(self):
        # apiag-cloud's count leaves the sale out once invoiced there, and the pull never settles: delivery does, for those items only.
        SupplierItem.objects.filter(pk=self.item_a.pk).update(source='apiag')
        order, quotation = self.handshake()
        self.assertEqual(self.counts(self.item_a), (10, 3, 7))
        self.assertEqual(self.supplier(order, 'process', quotation).status_code, 200)
        self.assertEqual(self.supplier(order, 'deliver', quotation).status_code, 200)
        self.assertEqual(self.counts(self.item_a), (10, 0, 10))
        self.assertEqual(dict(StockReservation.objects.values_list('item_id', 'status')), {self.item_a.pk: 'settled', self.item_a2.pk: 'open'})
        settle = StockEntry.objects.filter(item=self.item_a, kind='settle').get()
        self.assertEqual((settle.quantity, settle.reference), (3, f'{order.reference} · entregado · apiag-cloud'))
        self.assertEqual(check()['problems'], {})

    def test_a_shortfall_on_an_apiag_cloud_item_drops_its_sucursal_rows(self):
        from .apiag_sync_models import ApiagSucursal, SupplierItemBranchStock
        SupplierItem.objects.filter(pk=self.item_a.pk).update(source='apiag')
        matriz = ApiagSucursal.objects.create(account=self.supplier_a, apiag_id='1', nombre='MATRIZ', seen_at=timezone.now())
        david = ApiagSucursal.objects.create(account=self.supplier_a, apiag_id='2', nombre='DAVID', seen_at=timezone.now())
        SupplierItemBranchStock.objects.create(item=self.item_a, sucursal=matriz, quantity=7)
        SupplierItemBranchStock.objects.create(item=self.item_a, sucursal=david, quantity=3)
        order, quotation = self.handshake()
        lines = self.lines(order)
        reported = self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': lines[self.item_a.pk], 'missing': 2}], reason_code='no_stock')
        self.assertEqual(reported.status_code, 200, reported.data)
        self.assertEqual(self.counts(self.item_a), (8, 1, 7))
        self.assertFalse(SupplierItemBranchStock.objects.filter(item=self.item_a).exists())  # no sucursal named: the next pull rebuilds them
        self.assertEqual(check()['problems'], {})

    def test_a_shortfall_corrects_stock_at_once_and_changes_the_agreement_only_when_the_client_accepts(self):
        order, quotation = self.handshake()
        lines = self.lines(order)
        self.assertEqual(self.counts(self.item_a), (10, 3, 7))
        self.assertEqual(self.buyer_does(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': lines[self.item_a.pk], 'missing': 2}],
                                         reason_code='no_stock').status_code, 403)
        too_many = self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': lines[self.item_a.pk], 'missing': 4}], reason_code='no_stock')
        self.assertEqual(too_many.status_code, 400)
        self.assertEqual(self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': lines[self.item_a.pk], 'missing': 2}]).status_code, 400)
        reported = self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': lines[self.item_a.pk], 'missing': 2}],
                                 reason_code='no_stock', reason='se rompió en bodega')
        self.assertEqual(reported.status_code, 200, reported.data)
        # The two units never existed: they leave the count and the reservation together, so available stock does not move.
        self.assertEqual(self.counts(self.item_a), (8, 1, 7))
        entry = StockEntry.objects.get(kind='shortfall')
        self.assertEqual((entry.quantity, entry.balance_type, entry.reserved_delta, entry.reported_after, entry.reserved_after, entry.reference),
                         (2, 'stock', 2, 8, 1, f'{order.reference} · faltante'))
        self.assertEqual(StockReservation.objects.get(item=self.item_a).quantity, 1)
        line = next(row for row in reported.data['agreement']['lines'] if row['order_line_id'] == lines[self.item_a.pk])
        self.assertEqual((line['agreed'], line['pending_missing'], line['final']), (3, 2, 3))  # waiting for the client
        self.assertEqual((reported.data['status'], reported.data['shortfall_pending']), ('handshaked', True))
        self.assertIn('FALTAN 2 DE 3', order.events.last().description)
        self.assertEqual(self.supplier(order, 'deliver', quotation).status_code, 409)
        self.assertEqual(self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': lines[self.item_a2.pk], 'missing': 1}],
                                       reason_code='damaged').status_code, 409)

        accepted = self.buyer_does(order, 'accept_shortfall', quotation)
        self.assertEqual(accepted.status_code, 200, accepted.data)
        line = next(row for row in accepted.data['agreement']['lines'] if row['order_line_id'] == lines[self.item_a.pk])
        self.assertEqual((line['missing'], line['final'], line['total']), (2, 1, '12.35'))
        self.assertEqual((accepted.data['agreement']['units'], accepted.data['agreement']['total']), (3, '37.05'))
        self.assertEqual(accepted.data['quotation']['total'], '61.75')  # the agreement both confirmed stays as it was
        self.assertEqual(self.counts(self.item_a), (8, 1, 7))  # accepting changes the agreement, not the stock
        state = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/catalog/{self.part.pk}/request-state/').data['totals']
        self.assertEqual(state['handshaked_quantity'], 3)
        self.assertEqual(self.supplier(order, 'process', quotation).status_code, 200)
        self.assertEqual(self.supplier(order, 'deliver', quotation).status_code, 200)
        self.assertEqual(check()['problems'], {})

    def test_a_shortfall_of_every_unit_accepted_ends_as_a_supplier_cancellation(self):
        order, quotation = self.handshake()
        lines = self.lines(order)
        self.assertEqual(self.supplier(order, 'report_shortfall', quotation, reason_code='no_stock', shortfall=[
            {'order_line_id': lines[self.item_a.pk], 'missing': 3}, {'order_line_id': lines[self.item_a2.pk], 'missing': 2}]).status_code, 200)
        self.assertEqual(set(StockReservation.objects.values_list('status', flat=True)), {'released'})
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), ((7, 0, 7), (8, 0, 8)))
        done = self.buyer_does(order, 'accept_shortfall', quotation)
        self.assertEqual((done.data['status'], done.data['cancellation']['side'], done.data['cancellation']['reason']), ('cancelled', 'supplier', 'no_stock'))
        self.assertEqual(check()['problems'], {})

    def test_the_client_cancels_instead_of_accepting_and_the_missing_units_stay_out(self):
        order, quotation = self.handshake()
        lines = self.lines(order)
        self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': lines[self.item_a.pk], 'missing': 2}], reason_code='no_stock')
        self.assertEqual(self.buyer_does(order, 'cancel', quotation).status_code, 400)  # needs a reason
        cancelled = self.buyer_does(order, 'cancel', quotation, reason_code='shortfall', reason='lo busco en otro lado')
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual((cancelled.data['status'], cancelled.data['cancelled_side'], cancelled.data['cancellation']['note']),
                         ('cancelled', 'client', 'LO BUSCO EN OTRO LADO'))
        self.assertEqual(DealShortfall.objects.get().status, 'declined')
        # What the deal still held comes back; the two units that never existed do not.
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), ((8, 0, 8), (10, 0, 10)))
        self.assertEqual(sorted(StockEntry.objects.filter(kind='release').values_list('quantity', flat=True)), [1, 2])
        self.assertEqual(order.events.last().description, 'EL CLIENTE CANCELÓ EL ACUERDO: NO ACEPTA EL FALTANTE. LO BUSCO EN OTRO LADO')
        self.assertEqual(self.buyer_does(order, 'accept_shortfall', quotation).status_code, 409)
        self.assertEqual(check()['problems'], {})

    def test_supplier_decisions_need_the_publish_permission_and_replays_return_the_stored_result(self):
        order, quotation = self.handshake()
        SupplierPricingSettings.objects.update_or_create(supplier=self.supplier_a, defaults={'publish_min_permission': 'owner'})
        Membership.objects.filter(user=self.seller_a, account=self.supplier_a).update(permission='staff')
        self.assertEqual(self.supplier(order, 'cancel', quotation, reason_code='no_stock', expected_version=99).status_code, 403)  # 403 before 409
        for action in ('process', 'ship'):
            refused = self.supplier(order, action, quotation, expected_version=99)
            self.assertEqual((refused.status_code, str(refused.data['detail'])), (403, DECIDE_DENIED))
        Membership.objects.filter(user=self.seller_a, account=self.supplier_a).update(permission='owner')
        step = uuid.uuid4()
        processed = self.supplier(order, 'process', quotation, operation_id=step)
        self.assertEqual((processed.status_code, processed.data['status']), (200, 'processing'))
        self.assertEqual(self.supplier(order, 'process', quotation, operation_id=step, expected_version=order.version).data, processed.data)
        self.assertEqual(DealEvent.objects.filter(order=order, kind='process').count(), 1)
        operation = uuid.uuid4()
        first = self.supplier(order, 'cancel', quotation, reason_code='cannot_deliver', operation_id=operation)
        self.assertEqual((first.status_code, first.data['cancelled_side']), (200, 'supplier'))
        self.assertIn('stock', first.data['lines'][0])  # the supplier's own view
        again = self.supplier(order, 'cancel', quotation, reason_code='cannot_deliver', operation_id=operation, expected_version=order.version)
        self.assertEqual(again.data, first.data)
        self.assertEqual(DealEvent.objects.filter(order=order, kind='cancel').count(), 1)
        self.assertEqual(check()['problems'], {})

    def test_an_account_on_both_sides_names_the_side_it_cancels_as(self):
        # Accounts can no longer order from themselves (request_views.submit_request); deals sent before that rule remain, so they are
        # made here from ordinary orders.
        self.supplier_a.roles.add(Role.objects.get(code='client_business'))

        def own_deal():
            self.client.force_authenticate(self.buyer)
            sent = self.submit(self.payload([(self.item_a, 2)]))
            SupplierRequest.objects.filter(pk=sent.data['requests'][0]['id']).update(client=self.supplier_a, client_name=self.supplier_a.name)
            order = SupplierRequest.objects.get(pk=sent.data['requests'][0]['id'])
            self.review(order)
            quotation = self.quote(order).data['quotation']['id']
            self.client.force_authenticate(self.seller_a)
            self.assertEqual(self.action(order, 'accept', account=self.supplier_a, quotation_id=quotation).status_code, 200)
            return order, quotation
        order, quotation = own_deal()
        refused = self.action(order, 'cancel', account=self.supplier_a, quotation_id=quotation, reason_code='not_needed')
        self.assertEqual((refused.status_code, refused.data), (400, ['Indica si cancelas como cliente o como proveedor.']))
        cancelled = self.action(order, 'cancel', account=self.supplier_a, quotation_id=quotation, reason_code='not_needed', side='client')
        self.assertEqual((cancelled.status_code, cancelled.data['cancelled_side']), (200, 'client'))
        self.assertNotIn('stock', cancelled.data['lines'][0])  # the client's view for the client's decision
        # Each step names its side: the supplier's progress, then the client's Recibido.
        order, quotation = own_deal()
        for step in ('process', 'deliver'):
            self.assertEqual(self.action(order, step, account=self.supplier_a, quotation_id=quotation).status_code, 200)
        received = self.action(order, 'receive', account=self.supplier_a, quotation_id=quotation)
        self.assertEqual((received.status_code, received.data['status']), (200, 'received'))
        self.assertNotIn('stock', received.data['lines'][0])
        self.assertEqual(check()['problems'], {})

    def test_supplier_reads_say_whether_the_member_may_decide_and_a_refusal_names_the_decision(self):
        order, quotation = self.handshake()
        self.client.force_authenticate(self.seller_a)
        self.assertTrue(self.client.get(f'{self.url(self.supplier_a)}{order.pk}/').data['can_decide'])
        SupplierPricingSettings.objects.update_or_create(supplier=self.supplier_a, defaults={'publish_min_permission': 'owner'})
        Membership.objects.filter(user=self.seller_a, account=self.supplier_a).update(permission='staff')
        self.assertFalse(self.client.post(f'{self.url(self.supplier_a)}{order.pk}/review/', {}, format='json').data['can_decide'])
        refused = self.supplier(order, 'deliver', quotation)
        self.assertEqual(refused.status_code, 403)
        self.assertIn('decidir sobre acuerdos confirmados', refused.data['detail'])

    def test_purchase_history_counts_the_units_the_agreement_keeps(self):
        order, quotation = self.handshake()
        self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': self.lines(order)[self.item_a.pk], 'missing': 2}], reason_code='no_stock')
        self.client.force_authenticate(self.buyer)
        sent = f'/api/v1/accounts/{self.client_account.pk}/sent-requests/'
        self.assertEqual(self.client.get(sent, {'status': 'agreements'}).data['results'][0]['agreed_unit_count'], 5)  # pending: not yet
        self.buyer_does(order, 'accept_shortfall', quotation)
        self.assertEqual(self.client.get(sent, {'status': 'agreements'}).data['results'][0]['agreed_unit_count'], 3)

    def test_purchase_history_lists_every_confirmed_deal_and_lists_flag_pending_shortfalls(self):
        delivered, first = self.handshake()
        for step in ('process', 'deliver'):
            self.supplier(delivered, step, first)
        pending, second = self.handshake()
        self.supplier(pending, 'report_shortfall', second, shortfall=[{'order_line_id': self.lines(pending)[self.item_a.pk], 'missing': 1}], reason_code='damaged')
        received, third = self.handshake()
        for step in ('process', 'deliver'):
            self.supplier(received, step, third)
        self.buyer_does(received, 'receive', third)
        self.client.force_authenticate(self.buyer)
        sent = f'/api/v1/accounts/{self.client_account.pk}/sent-requests/'
        rows = {row['reference']: row for row in self.client.get(sent, {'status': 'agreements'}).data['results']}
        self.assertEqual({key: (row['status'], row['shortfall_pending']) for key, row in rows.items()},
                         {delivered.reference: ('delivered', False), pending.reference: ('handshaked', True), received.reference: ('received', False)})
        listed = {status: {row['reference'] for row in self.client.get(sent, {'status': status}).data['results']}
                  for status in ('delivered', 'received', 'active_agreements', 'open_agreements')}
        self.assertEqual(listed, {'delivered': {delivered.reference}, 'received': {received.reference}, 'active_agreements': {pending.reference},
                                  'open_agreements': {delivered.reference, pending.reference}})
        self.assertEqual(self.client.get(sent, {'status': 'closed'}).status_code, 400)
        self.client.force_authenticate(self.seller_a)
        listed = {row['reference']: row['shortfall_pending'] for row in self.client.get(self.url(self.supplier_a), {'status': 'agreements'}).data['results']}
        self.assertEqual(listed, {delivered.reference: False, pending.reference: True, received.reference: False})
        detail = self.client.get(f'{self.url(self.supplier_a)}{pending.pk}/').data
        stock = next(line['stock'] for line in detail['lines'] if line['supplier_item_id'] == str(self.item_a.pk))
        self.assertEqual((stock['shortfall'], stock['held_for_deal']), (0, 2))  # its own reservation is not a shortfall of its lines


class DealProgressTests(APITestCase):
    """The order's progress after the handshake: the supplier's Procesando, En camino (skipped on a counter pickup) and Entregado, and the
    client's Recibido that closes it. Nothing completes on its own, each step is its party's, and the ledger stays reconciled."""
    setUp, state = DealLifecycleTests.setUp, DealLifecycleTests.state
    account, item, url, payload, submit = (DealLifecycleTests.account, DealLifecycleTests.item, DealLifecycleTests.url,
                                           DealLifecycleTests.payload, DealLifecycleTests.submit)
    order, review, action, drafted, quote = (DealLifecycleTests.order, DealLifecycleTests.review, DealLifecycleTests.action,
                                             DealLifecycleTests.drafted, DealLifecycleTests.quote)
    handshake, supplier, buyer_does, lines, counts = (DealLifecycleTests.handshake, DealLifecycleTests.supplier, DealLifecycleTests.buyer_does,
                                                      DealLifecycleTests.lines, DealLifecycleTests.counts)
    DISPATCHED = ('La orden ya salió del proveedor y no se puede cancelar aquí. Escríbele a la otra parte; si no se ponen de acuerdo, '
                  'MotionPartes puede intervenir.')

    def admin_cancel(self, order):
        self.client.force_authenticate(self.root)
        order.refresh_from_db()
        return self.client.post(f'/api/v1/management/deals/{order.pk}/cancel/', {'expected_version': order.version, 'reason_code': 'dispute'},
                                format='json')

    def test_the_supplier_advances_and_the_client_closes(self):
        order, quotation = self.handshake()
        steps = [('process', self.supplier, self.supplier_a, 'processing', 'processing_at', 'EL PROVEEDOR ESTÁ PROCESANDO LA ORDEN.', (5, 0)),
                 ('ship', self.supplier, self.supplier_a, 'in_transit', 'in_transit_at', 'EL PROVEEDOR ENVIÓ LA ORDEN: VA EN CAMINO CON 5 UNIDADES.', (5, 0)),
                 ('deliver', self.supplier, self.supplier_a, 'delivered', 'delivered_at',
                  'EL PROVEEDOR MARCÓ LA ORDEN COMO ENTREGADA: 5 UNIDADES. FALTA QUE EL CLIENTE CONFIRME QUE LA RECIBIÓ.', (0, 5)),
                 ('receive', self.buyer_does, self.client_account, 'received', 'received_at', 'EL CLIENTE CONFIRMÓ QUE RECIBIÓ LA ORDEN: 5 UNIDADES.', (0, 5))]
        for action, who, account, status, stamp, description, quantities in steps:
            with self.subTest(action=action):
                version, entries = SupplierRequest.objects.get(pk=order.pk).version, StockEntry.objects.count()
                done = who(order, action, quotation)
                self.assertEqual((done.status_code, done.data['status'], done.data['version']), (200, status, version + 1), done.data)
                self.assertIsNotNone(done.data[stamp])
                event = order.events.last()
                self.assertEqual((event.kind, event.account_id, event.description), (action, account.pk, description))
                self.assertEqual(StockEntry.objects.count(), entries)  # Excel items: even Entregado waits for the supplier's next upload
                state = self.state()
                self.assertEqual((state['handshaked_quantity'], state['delivered_quantity']), quantities)
                self.assertEqual(check()['problems'], {})
        row = SupplierRequest.objects.get(pk=order.pk)
        self.assertEqual((row.processing_by, row.in_transit_by, row.delivered_by, row.received_by), (self.seller_a, self.seller_a, self.seller_a, self.buyer))
        self.assertTrue(row.processing_at <= row.in_transit_at <= row.delivered_at <= row.received_at)

    def test_entregado_needs_procesando_and_en_camino_may_be_skipped(self):
        order, quotation = self.handshake()
        early = self.supplier(order, 'deliver', quotation)
        self.assertEqual((early.status_code, str(early.data['detail'])), (409, 'Marca la orden como Procesando antes de marcarla como entregada.'))
        early = self.supplier(order, 'ship', quotation)
        self.assertEqual((early.status_code, str(early.data['detail'])), (409, 'Marca la orden como Procesando antes de enviarla.'))
        self.assertEqual(self.supplier(order, 'process', quotation).status_code, 200)
        self.assertEqual(self.supplier(order, 'process', quotation).status_code, 409)
        pickup = self.supplier(order, 'deliver', quotation)  # picked up at the counter: Procesando straight to Entregado
        self.assertEqual((pickup.status_code, pickup.data['status'], pickup.data['in_transit_at']), (200, 'delivered', None))
        for action in ('process', 'ship', 'deliver'):
            self.assertEqual(self.supplier(order, action, quotation).status_code, 409)
        self.assertEqual(self.buyer_does(order, 'receive', quotation).status_code, 200)
        for action in ('process', 'ship', 'deliver'):
            self.assertEqual(self.supplier(order, action, quotation).status_code, 409)
        self.assertEqual(SupplierRequest.objects.get(pk=order.pk).in_transit_at, None)
        self.assertEqual(check()['problems'], {})

    def test_the_client_cannot_confirm_before_the_suppliers_entregado(self):
        order, quotation = self.handshake()
        for step in (None, 'process', 'ship'):
            if step:
                self.assertEqual(self.supplier(order, step, quotation).status_code, 200)
            early = self.buyer_does(order, 'receive', quotation)
            self.assertEqual((early.status_code, str(early.data['detail'])),
                             (409, 'El proveedor aún no marca la orden como entregada. Podrás confirmar que la recibiste cuando lo haga.'))
        self.assertEqual(self.supplier(order, 'deliver', quotation).status_code, 200)
        self.assertEqual(self.supplier(order, 'receive', quotation).status_code, 403)  # a supplier member: the client's decision
        self.assertEqual(self.buyer_does(order, 'receive', quotation).status_code, 200)
        again = self.buyer_does(order, 'receive', quotation)
        self.assertEqual((again.status_code, str(again.data['detail'])), (409, 'Ya confirmaste que recibiste esta orden.'))
        self.assertEqual(SupplierRequest.objects.get(pk=order.pk).received_by, self.buyer)

    def test_the_client_cannot_take_the_suppliers_steps_and_unknown_sides_are_refused(self):
        order, quotation = self.handshake()
        for action in ('process', 'ship', 'deliver'):
            self.assertEqual(self.buyer_does(order, action, quotation).status_code, 403)
        SupplierPricingSettings.objects.update_or_create(supplier=self.supplier_a, defaults={'publish_min_permission': 'owner'})
        Membership.objects.filter(user=self.seller_a, account=self.supplier_a).update(permission='staff')
        for action in ('process', 'ship'):
            refused = self.supplier(order, action, quotation, expected_version=99)  # 403 before the version's 409
            self.assertEqual((refused.status_code, str(refused.data['detail'])), (403, DECIDE_DENIED))
        # An action the serializer accepts but neither side owns is nobody's decision (each request's serializer rebuilds its fields
        # from their constructor arguments).
        field = DealActionSerializer._declared_fields['action']
        with mock.patch.dict(field._kwargs, {'choices': [*field.choices, 'archive']}):
            for party in (self.supplier, self.buyer_does):
                refused = party(order, 'archive', quotation)
                self.assertEqual((refused.status_code, str(refused.data['detail'])), (403, 'Esta decisión corresponde a la otra parte del acuerdo.'))
        self.assertEqual(SupplierRequest.objects.get(pk=order.pk).status, 'handshaked')

    def test_a_shortfall_while_processing_blocks_shipping_until_the_client_answers(self):
        order, quotation = self.handshake()
        lines = self.lines(order)

        def report(item, missing):
            return self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': lines[item.pk], 'missing': missing}], reason_code='no_stock')
        self.assertEqual(report(self.item_a, 1).status_code, 200)
        processed = self.supplier(order, 'process', quotation)  # a pending shortfall does not stop Procesando
        self.assertEqual((processed.status_code, processed.data['status'], processed.data['shortfall_pending']), (200, 'processing', True))
        for action in ('ship', 'deliver'):
            refused = self.supplier(order, action, quotation)
            self.assertEqual((refused.status_code, str(refused.data['detail'])), (409, 'El cliente todavía debe responder al faltante reportado.'))
        accepted = self.buyer_does(order, 'accept_shortfall', quotation)
        self.assertEqual((accepted.status_code, accepted.data['status']), (200, 'processing'))
        reported = report(self.item_a2, 1)  # while processing: the status stays
        self.assertEqual((reported.status_code, reported.data['status']), (200, 'processing'))
        self.assertEqual(self.buyer_does(order, 'accept_shortfall', quotation).data['status'], 'processing')
        shipped = self.supplier(order, 'ship', quotation)
        self.assertEqual((shipped.status_code, shipped.data['agreement']['units']), (200, 3))
        late = report(self.item_a, 1)
        self.assertEqual((late.status_code, str(late.data['detail'])),
                         (409, 'Solo puedes reportar un faltante antes de enviar la orden (Acuerdo confirmado o Procesando).'))
        self.assertEqual(check()['problems'], {})
        # Every unit missing while processing: the client's acceptance ends it as the supplier's cancellation.
        other, second = self.handshake()
        self.supplier(other, 'process', second)
        lines = self.lines(other)
        self.assertEqual(self.supplier(other, 'report_shortfall', second, reason_code='no_stock', shortfall=[
            {'order_line_id': lines[self.item_a.pk], 'missing': 3}, {'order_line_id': lines[self.item_a2.pk], 'missing': 2}]).status_code, 200)
        done = self.buyer_does(other, 'accept_shortfall', second)
        self.assertEqual((done.status_code, done.data['status'], done.data['cancellation']['side'], bool(done.data['processing_at'])),
                         (200, 'cancelled', 'supplier', True))
        self.assertEqual(check()['problems'], {})

    def test_who_cancels_where(self):
        # Before the agreement there is nothing to cancel: the client rejects the order instead.
        self.client.force_authenticate(self.buyer)
        quoted = self.order()
        self.review(quoted)
        offered = self.quote(quoted).data['quotation']['id']
        refused = self.buyer_does(quoted, 'cancel', offered, reason_code='not_needed')
        self.assertEqual((refused.status_code, str(refused.data['detail'])), (409, 'Solo se puede cancelar un acuerdo confirmado.'))
        self.assertEqual(self.admin_cancel(quoted).status_code, 409)
        # Either party, while the supplier still has the goods.
        for steps in ((), ('process',)):
            order, quotation = self.handshake()
            for step in steps:
                self.supplier(order, step, quotation)
            cancelled = self.buyer_does(order, 'cancel', quotation, reason_code='not_needed')
            self.assertEqual((cancelled.status_code, cancelled.data['status']), (200, 'cancelled'))
            self.assertEqual(sorted(StockEntry.objects.filter(kind='release', reservation__order=order).values_list('reference', flat=True)),
                             [f'{order.reference} · cancelado'] * 2)
        # Once it left the supplier only MotionPartes cancels, up to the client's Recibido. apiag-cloud's units settled at Entregado stay
        # settled; what the deal still held comes back, and the supplier corrects its count if the goods did not.
        SupplierItem.objects.filter(pk=self.item_a.pk).update(source='apiag')
        for steps in (('process', 'ship'), ('process', 'ship', 'deliver'), ('process', 'deliver')):
            with self.subTest(steps=steps):
                order, quotation = self.handshake()
                for step in steps:
                    self.assertEqual(self.supplier(order, step, quotation).status_code, 200)
                for refused in (self.supplier(order, 'cancel', quotation, reason_code='no_stock'), self.buyer_does(order, 'cancel', quotation, reason_code='not_needed')):
                    self.assertEqual((refused.status_code, str(refused.data['detail'])), (409, self.DISPATCHED))
                cancelled = self.admin_cancel(order)
                self.assertEqual((cancelled.status_code, cancelled.data['status'], cancelled.data['cancelled_side']), (200, 'cancelled', 'admin'), cancelled.data)
                row = SupplierRequest.objects.get(pk=order.pk)
                self.assertEqual((bool(row.in_transit_at), bool(row.delivered_at)), ('ship' in steps, 'deliver' in steps))  # kept
                delivered = 'deliver' in steps
                self.assertEqual(dict(StockReservation.objects.filter(order=order).values_list('item_id', 'status')),
                                 {self.item_a.pk: 'settled' if delivered else 'released', self.item_a2.pk: 'released'})
                self.assertEqual(sorted(StockEntry.objects.filter(kind='release', reservation__order=order).values_list('quantity', 'reference')),
                                 [(2, f'{order.reference} · cancelado tras el envío')] + ([] if delivered else [(3, f'{order.reference} · cancelado tras el envío')]))
                released = 2 if delivered else 5
                self.assertTrue(order.events.last().description.endswith(
                    f'{released} UNIDADES QUE EL ACUERDO AÚN RESERVABA VUELVEN A ESTAR DISPONIBLES; SI LA MERCANCÍA NO REGRESÓ, EL PROVEEDOR DEBE CORREGIR SU INVENTARIO.'))
                self.client.force_authenticate(self.seller_a)
                self.assertEqual(self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/requests/{order.pk}/').data['stock_units'],
                                 {'held': 0, 'settled': 3 if delivered else 0, 'released_after_dispatch': released})
                self.assertEqual(check()['problems'], {})
        # Everything already discounted (apiag-cloud settles at Entregado): nothing comes back, and the supplier adds it back only if the goods do.
        SupplierItem.objects.filter(pk=self.item_a2.pk).update(source='apiag')
        order, quotation = self.handshake()
        for step in ('process', 'deliver'):
            self.supplier(order, step, quotation)
        self.assertEqual(self.admin_cancel(order).status_code, 200)
        self.assertTrue(order.events.last().description.endswith(
            'SUS UNIDADES YA SE HABÍAN DESCONTADO DE LAS EXISTENCIAS DEL PROVEEDOR; SI LA MERCANCÍA REGRESA, DEBE SUMARLAS A SU INVENTARIO.'))
        self.assertEqual(check()['problems'], {})
        SupplierItem.objects.filter(pk=self.item_a2.pk).update(source='upload')
        # Received closes it for everyone.
        order, quotation = self.handshake()
        for step in ('process', 'deliver'):
            self.supplier(order, step, quotation)
        self.buyer_does(order, 'receive', quotation)
        refused = self.buyer_does(order, 'cancel', quotation, reason_code='not_needed')
        self.assertEqual((refused.status_code, str(refused.data['detail'])), (409, 'Esta orden ya está cerrada.'))
        self.assertEqual(self.admin_cancel(order).status_code, 409)
        self.assertEqual(check()['problems'], {})

    def test_receive_moves_no_stock_and_apiag_settles_at_entregado(self):
        SupplierItem.objects.filter(pk=self.item_a.pk).update(source='apiag')
        order, quotation = self.handshake()
        for step in ('process', 'ship'):
            self.supplier(order, step, quotation)
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), ((10, 3, 7), (10, 2, 8)))  # nothing settles before Entregado
        self.supplier(order, 'deliver', quotation)
        self.assertEqual(self.counts(self.item_a), (10, 0, 10))
        entries, held = StockEntry.objects.count(), dict(StockReservation.objects.values_list('item_id', 'status'))
        self.assertEqual(held, {self.item_a.pk: 'settled', self.item_a2.pk: 'open'})
        self.assertEqual(self.buyer_does(order, 'receive', quotation).status_code, 200)
        # The Excel item keeps holding its units until the supplier's next upload says its count discounts them.
        self.assertEqual((StockEntry.objects.count(), dict(StockReservation.objects.values_list('item_id', 'status'))), (entries, held))
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), ((10, 0, 10), (10, 2, 8)))
        self.assertEqual(check()['problems'], {})

    def test_the_database_rejects_inconsistent_progress(self):
        order, _ = self.handshake()
        rows, now = SupplierRequest.objects.filter(pk=order.pk), timezone.now()
        earlier = now - timedelta(hours=1)
        cancelled = {'status': 'cancelled', 'cancelled_at': now, 'cancel_reason': 'not_needed'}
        refused = [{'status': 'processing'}, {'processing_at': now}, {'status': 'in_transit', 'processing_at': now},
                   {'status': 'delivered', 'processing_at': now}, {'status': 'delivered', 'delivered_at': now},
                   {'status': 'received', 'delivered_at': now}, {'status': 'received', 'delivered_at': now, 'received_at': earlier},
                   {'status': 'received', 'received_at': now},
                   {'status': 'delivered', 'processing_at': earlier, 'delivered_at': now, 'received_at': now},
                   {**cancelled, 'cancelled_side': 'client', 'delivered_at': now}, {**cancelled, 'cancelled_side': 'supplier', 'in_transit_at': now}]
        for changes in refused:
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                rows.update(**changes)
        # MotionPartes' cancellation keeps the dispatch dates; a delivery from before Procesando existed closes without processing_at.
        allowed = [{**cancelled, 'cancelled_side': 'admin', 'cancel_reason': 'dispute', 'processing_at': earlier, 'in_transit_at': earlier, 'delivered_at': now},
                   {**cancelled, 'cancelled_side': 'client', 'processing_at': earlier},
                   {'status': 'received', 'delivered_at': earlier, 'received_at': now}]
        for changes in allowed:
            with self.subTest(changes=changes), transaction.atomic():
                self.assertEqual(rows.update(**changes), 1)
                transaction.set_rollback(True)

    def test_migration_closes_legacy_deliveries(self):
        from .inbox import awaiting_receipt
        migration = importlib.import_module('mall.migrations.0070_deal_progress')
        legacy, first = self.handshake()
        for step in ('process', 'deliver'):
            self.supplier(legacy, step, first)
        waiting, second = self.handshake()
        before = SupplierRequest.objects.get(pk=legacy.pk)
        self.assertEqual(awaiting_receipt(self.buyer)['count'], 1)
        migration.legacy_deliveries(apps, None)
        after = SupplierRequest.objects.get(pk=legacy.pk)
        self.assertEqual((after.status, after.received_at, after.received_by, after.updated_at), ('received', before.delivered_at, None, before.updated_at))
        self.assertEqual(SupplierRequest.objects.get(pk=waiting.pk).status, 'handshaked')  # waits for the supplier's Procesando
        self.assertEqual(awaiting_receipt(self.buyer)['count'], 0)
        self.assertEqual(check()['problems'], {})
        # Back (lossy): a single Entregado again, and every row as the old constraints want it.
        for step in ('process', 'ship'):
            self.supplier(waiting, step, second)
        disputed, third = self.handshake()
        for step in ('process', 'ship', 'deliver'):
            self.supplier(disputed, step, third)
        self.assertEqual(self.admin_cancel(disputed).status_code, 200)
        migration.restore_legacy(apps, None)
        rows = {row.pk: row for row in SupplierRequest.objects.all()}
        self.assertEqual((rows[legacy.pk].status, rows[waiting.pk].status, rows[disputed.pk].status), ('delivered', 'handshaked', 'cancelled'))
        for row in rows.values():
            self.assertIn(row.status, ['pending', 'reviewed', 'quoted', 'adjustment', 'handshaked', 'delivered', 'cancelled'])
            self.assertEqual(row.status == 'delivered', row.delivered_at is not None)
            self.assertEqual((row.received_at, row.received_by, row.in_transit_at), (None, None, None))
            self.assertEqual(row.processing_at is None, row.status != 'delivered')


class DealAdminTests(APITestCase):
    """Acuerdos for superusers: deals without prices, and a dispute cancellation that never counts against the supplier."""
    setUp = DealLifecycleTests.setUp
    account, item, url, payload, submit = (DealLifecycleTests.account, DealLifecycleTests.item, DealLifecycleTests.url,
                                           DealLifecycleTests.payload, DealLifecycleTests.submit)
    order, review, action, drafted, quote = (DealLifecycleTests.order, DealLifecycleTests.review, DealLifecycleTests.action,
                                             DealLifecycleTests.drafted, DealLifecycleTests.quote)
    handshake, supplier, buyer_does, lines, counts = (DealLifecycleTests.handshake, DealLifecycleTests.supplier, DealLifecycleTests.buyer_does,
                                                      DealLifecycleTests.lines, DealLifecycleTests.counts)

    def test_superusers_list_deals_without_prices_and_cancel_a_dispute_as_motionpartes(self):
        order, quotation = self.handshake()
        self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': self.lines(order)[self.item_a.pk], 'missing': 1}], reason_code='other',
                      reason='caja equivocada')
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.client.get('/api/v1/management/deals/').status_code, 403)
        self.client.force_authenticate(self.root)
        for status in ('agreements', 'open_agreements'):
            listed = self.client.get('/api/v1/management/deals/', {'status': status, 'search': order.reference[-6:]})
            self.assertEqual([(row['reference'], row['shortfall_pending'], row['shortfall_count']) for row in listed.data['results']],
                             [(order.reference, True, 1)])
        detail = self.client.get(f'/api/v1/management/deals/{order.pk}/').data
        text = json.dumps(detail)
        self.assertNotIn('12.35', text)
        self.assertFalse({'total', 'unit_price', 'currency', 'terms', 'quotation'} & ({key for line in detail['lines'] for key in line} | set(detail)))
        line = next(row for row in detail['lines'] if row['codigo'] == self.item_a.codigo)
        self.assertEqual((line['requested'], line['agreed'], line['pending_missing'], line['final']), (3, 3, 1, 3))
        self.assertEqual(detail['shortfalls'][0]['lines'], [{'codigo': self.item_a.codigo, 'agreed': 3, 'missing': 1}])
        self.assertEqual((detail['unit_count'], detail['line_count']), (5, 2))
        # A second shortfall (after the first is accepted) must not multiply the requested units.
        self.buyer_does(order, 'accept_shortfall', quotation)
        self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': self.lines(order)[self.item_a2.pk], 'missing': 1}], reason_code='damaged')
        # The parties' own words (an adjustment request) stay out of the administrator's timeline.
        DealEvent.objects.create(order=order, account=self.client_account, actor=self.buyer, kind='request_adjustment', description='PAGO 999.99 POR UNIDAD')
        self.client.force_authenticate(self.root)
        detail = self.client.get(f'/api/v1/management/deals/{order.pk}/').data
        self.assertEqual((detail['unit_count'], detail['shortfall_count']), (5, 2))
        self.assertNotIn('999.99', json.dumps(detail))
        self.assertEqual(detail['events'][-1]['description'], 'EL CLIENTE PIDIÓ UN AJUSTE.')
        self.assertEqual(self.client.post(f'/api/v1/management/deals/{order.pk}/cancel/', {'expected_version': 1, 'reason_code': 'dispute'},
                                          format='json').status_code, 409)
        cancelled = self.client.post(f'/api/v1/management/deals/{order.pk}/cancel/', {'expected_version': detail['version'], 'reason_code': 'dispute',
                                                                                      'note': 'las partes no se ponen de acuerdo'}, format='json')
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual((cancelled.data['status'], cancelled.data['cancelled_side'], cancelled.data['cancel_note']),
                         ('cancelled', 'admin', 'LAS PARTES NO SE PONEN DE ACUERDO'))
        self.assertEqual(cancelled.data['events'][-1]['account_name'], 'MOTIONPARTES')
        self.assertEqual(sorted(DealShortfall.objects.values_list('status', flat=True)), ['accepted', 'declined'])
        self.client.force_authenticate(self.buyer)
        seen = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/sent-requests/{order.pk}/').data
        self.assertEqual((seen['events'][-1]['account_name'], seen['events'][-1]['actor_name'], seen['cancellation']['side']),
                         ('MOTIONPARTES', 'MOTIONPARTES', 'admin'))
        self.assertEqual(self.counts(self.item_a), (9, 0, 9))
        self.assertEqual(self.counts(self.item_a2), (9, 0, 9))
        self.assertEqual(check()['problems'], {})

        # A deal the client received: its timeline shows every step, and it is closed for MotionPartes too.
        done, quotation = self.handshake()
        for step in ('process', 'ship', 'deliver'):
            self.supplier(done, step, quotation)
        self.buyer_does(done, 'receive', quotation)
        self.client.force_authenticate(self.root)
        detail = self.client.get(f'/api/v1/management/deals/{done.pk}/').data
        self.assertEqual([(event['kind'], event['description']) for event in detail['events'][-4:]], [
            ('process', 'EL PROVEEDOR ESTÁ PROCESANDO LA ORDEN.'), ('ship', 'EL PROVEEDOR ENVIÓ LA ORDEN: VA EN CAMINO CON 5 UNIDADES.'),
            ('deliver', 'EL PROVEEDOR MARCÓ LA ORDEN COMO ENTREGADA: 5 UNIDADES. FALTA QUE EL CLIENTE CONFIRME QUE LA RECIBIÓ.'),
            ('receive', 'EL CLIENTE CONFIRMÓ QUE RECIBIÓ LA ORDEN: 5 UNIDADES.')])
        self.assertTrue(all(detail[key] for key in ('processing_at', 'in_transit_at', 'delivered_at', 'received_at')))
        self.assertEqual(self.client.get('/api/v1/management/deals/', {'status': 'open_agreements', 'search': done.reference[-6:]}).data['results'], [])
        refused = self.client.post(f'/api/v1/management/deals/{done.pk}/cancel/', {'expected_version': detail['version'], 'reason_code': 'dispute'},
                                   format='json')
        self.assertEqual((refused.status_code, str(refused.data['detail'])), (409, 'Este acuerdo ya está cerrado.'))

    def test_an_administrators_decision_notifies_both_parties(self):
        from .push import other_side
        order, _ = self.handshake()
        self.assertEqual(other_side(order, None, self.root.pk), {self.buyer.pk, self.seller_a.pk})
        self.assertEqual(other_side(order, self.client_account.pk, self.buyer.pk), {self.seller_a.pk})  # a party's own decision: the other one
        from .email_notifications import their_account
        self.assertEqual((their_account(order, None, self.seller_a), their_account(order, None, self.buyer)), ('PROVEEDOR A', 'CLIENTE ORIGINAL'))

    def test_reliability_counts_shortfalls_and_supplier_cancellations_only(self):
        clean, first = self.handshake()
        for step in ('process', 'deliver'):
            self.supplier(clean, step, first)
        self.buyer_does(clean, 'receive', first)
        short, second = self.handshake()
        self.supplier(short, 'report_shortfall', second, shortfall=[{'order_line_id': self.lines(short)[self.item_a.pk], 'missing': 2}], reason_code='no_stock')
        self.buyer_does(short, 'accept_shortfall', second)
        dropped, third = self.handshake()
        self.supplier(dropped, 'cancel', third, reason_code='price_error')
        regret, fourth = self.handshake()
        self.buyer_does(regret, 'cancel', fourth, reason_code='not_needed')
        waiting, fifth = self.handshake()  # delivered by the supplier, not yet received: delivered counts it too
        for step in ('process', 'ship', 'deliver'):
            self.supplier(waiting, step, fifth)
        self.client.force_authenticate(self.root)
        rows = self.client.get('/api/v1/management/analytics/', {'days': 7}).data['reliability']
        self.assertEqual(rows, [{'id': str(self.supplier_a.pk), 'name': 'PROVEEDOR A', 'confirmed': 5, 'delivered': 2, 'received': 1,
                                 'with_shortfall': 1, 'supplier_cancelled': 1, 'failed': 2, 'client_cancelled': 1, 'admin_cancelled': 0,
                                 'missing_units': 2, 'failure_rate': 0.4}])
        quotations = self.client.get('/api/v1/management/analytics/', {'days': 7}).data['quotations']
        self.assertEqual(quotations['handshaked_orders'], 5)  # confirmed deals stay confirmed after delivery or cancellation
        self.assertEqual(check()['problems'], {})
