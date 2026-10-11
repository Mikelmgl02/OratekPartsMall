import json
import uuid

from django.utils import timezone
from rest_framework.test import APITestCase

from . import test_deals as deal_tests
from .check_stock_ledger import check
from .models import Membership, Role, StockEntry, SupplierItem, User
from .pricing_models import SupplierPricingSettings
from .request_models import DealEvent, DealShortfall, StockReservation, SupplierRequest


class DealLifecycleTests(APITestCase):
    """After the handshake: delivery, a shortfall the client accepts or answers by cancelling, cancellations by either party or by
    MotionPartes, the supplier's reliability, and the ledger staying reconciled through all of it."""
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

    def test_delivery_closes_the_deal_without_moving_stock(self):
        order, quotation = self.handshake()
        before = (self.counts(self.item_a), self.counts(self.item_a2))
        self.assertEqual(self.buyer_does(order, 'deliver', quotation).status_code, 403)  # the supplier's decision
        delivered = self.supplier(order, 'deliver', quotation)
        self.assertEqual(delivered.status_code, 200, delivered.data)
        self.assertEqual((delivered.data['status'], bool(delivered.data['delivered_at'])), ('delivered', True))
        self.assertEqual((self.counts(self.item_a), self.counts(self.item_a2)), before)  # no second deduction at fulfilment
        self.assertEqual(set(StockReservation.objects.values_list('status', flat=True)), {'open'})  # until the supplier's count shows it
        self.assertEqual(order.events.last().description, 'EL PROVEEDOR MARCÓ EL ACUERDO COMO ENTREGADO: 5 UNIDADES.')
        self.assertEqual(self.supplier(order, 'deliver', quotation).status_code, 409)
        self.assertEqual(self.supplier(order, 'cancel', quotation, reason_code='no_stock').status_code, 409)
        self.client.force_authenticate(self.buyer)
        state = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/catalog/{self.part.pk}/request-state/').data['totals']
        self.assertEqual((state['handshaked_quantity'], state['delivered_quantity']), (0, 5))
        self.assertEqual(check()['problems'], {})

    def test_delivery_settles_what_apiag_cloud_items_hold(self):
        # apiag-cloud's count leaves the sale out once invoiced there, and the pull never settles: delivery does, for those items only.
        SupplierItem.objects.filter(pk=self.item_a.pk).update(source='apiag')
        order, quotation = self.handshake()
        self.assertEqual(self.counts(self.item_a), (10, 3, 7))
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
        Membership.objects.filter(user=self.seller_a, account=self.supplier_a).update(permission='owner')
        operation = uuid.uuid4()
        first = self.supplier(order, 'cancel', quotation, reason_code='cannot_deliver', operation_id=operation)
        self.assertEqual((first.status_code, first.data['cancelled_side']), (200, 'supplier'))
        self.assertIn('stock', first.data['lines'][0])  # the supplier's own view
        again = self.supplier(order, 'cancel', quotation, reason_code='cannot_deliver', operation_id=operation, expected_version=order.version)
        self.assertEqual(again.data, first.data)
        self.assertEqual(DealEvent.objects.filter(order=order, kind='cancel').count(), 1)
        self.assertEqual(check()['problems'], {})

    def test_an_account_on_both_sides_names_the_side_it_cancels_as(self):
        # Accounts can no longer order from themselves (request_views.submit_request); deals sent before that rule remain, so one is
        # made here from an ordinary order.
        self.supplier_a.roles.add(Role.objects.get(code='client_business'))
        self.client.force_authenticate(self.buyer)
        sent = self.submit(self.payload([(self.item_a, 2)]))
        SupplierRequest.objects.filter(pk=sent.data['requests'][0]['id']).update(client=self.supplier_a, client_name=self.supplier_a.name)
        order = SupplierRequest.objects.get(pk=sent.data['requests'][0]['id'])
        self.review(order)
        quotation = self.quote(order).data['quotation']['id']
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.action(order, 'accept', account=self.supplier_a, quotation_id=quotation).status_code, 200)
        refused = self.action(order, 'cancel', account=self.supplier_a, quotation_id=quotation, reason_code='not_needed')
        self.assertEqual((refused.status_code, refused.data), (400, ['Indica si cancelas como cliente o como proveedor.']))
        cancelled = self.action(order, 'cancel', account=self.supplier_a, quotation_id=quotation, reason_code='not_needed', side='client')
        self.assertEqual((cancelled.status_code, cancelled.data['cancelled_side']), (200, 'client'))
        self.assertNotIn('stock', cancelled.data['lines'][0])  # the client's view for the client's decision
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
        self.supplier(delivered, 'deliver', first)
        pending, second = self.handshake()
        self.supplier(pending, 'report_shortfall', second, shortfall=[{'order_line_id': self.lines(pending)[self.item_a.pk], 'missing': 1}], reason_code='damaged')
        self.client.force_authenticate(self.buyer)
        sent = f'/api/v1/accounts/{self.client_account.pk}/sent-requests/'
        rows = {row['reference']: row for row in self.client.get(sent, {'status': 'agreements'}).data['results']}
        self.assertEqual({key: (row['status'], row['shortfall_pending']) for key, row in rows.items()},
                         {delivered.reference: ('delivered', False), pending.reference: ('handshaked', True)})
        self.assertEqual([row['reference'] for row in self.client.get(sent, {'status': 'delivered'}).data['results']], [delivered.reference])
        self.client.force_authenticate(self.seller_a)
        listed = {row['reference']: row['shortfall_pending'] for row in self.client.get(self.url(self.supplier_a), {'status': 'agreements'}).data['results']}
        self.assertEqual(listed, {delivered.reference: False, pending.reference: True})
        detail = self.client.get(f'{self.url(self.supplier_a)}{pending.pk}/').data
        stock = next(line['stock'] for line in detail['lines'] if line['supplier_item_id'] == str(self.item_a.pk))
        self.assertEqual((stock['shortfall'], stock['held_for_deal']), (0, 2))  # its own reservation is not a shortfall of its lines


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
        listed = self.client.get('/api/v1/management/deals/', {'status': 'agreements', 'search': order.reference[-6:]})
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

    def test_an_administrators_decision_notifies_both_parties(self):
        from .push import other_side
        order, _ = self.handshake()
        self.assertEqual(other_side(order, None, self.root.pk), {self.buyer.pk, self.seller_a.pk})
        self.assertEqual(other_side(order, self.client_account.pk, self.buyer.pk), {self.seller_a.pk})  # a party's own decision: the other one
        from .email_notifications import their_account
        self.assertEqual((their_account(order, None, self.seller_a), their_account(order, None, self.buyer)), ('PROVEEDOR A', 'CLIENTE ORIGINAL'))

    def test_reliability_counts_shortfalls_and_supplier_cancellations_only(self):
        clean, first = self.handshake()
        self.supplier(clean, 'deliver', first)
        short, second = self.handshake()
        self.supplier(short, 'report_shortfall', second, shortfall=[{'order_line_id': self.lines(short)[self.item_a.pk], 'missing': 2}], reason_code='no_stock')
        self.buyer_does(short, 'accept_shortfall', second)
        dropped, third = self.handshake()
        self.supplier(dropped, 'cancel', third, reason_code='price_error')
        regret, fourth = self.handshake()
        self.buyer_does(regret, 'cancel', fourth, reason_code='not_needed')
        self.client.force_authenticate(self.root)
        rows = self.client.get('/api/v1/management/analytics/', {'days': 7}).data['reliability']
        self.assertEqual(rows, [{'id': str(self.supplier_a.pk), 'name': 'PROVEEDOR A', 'confirmed': 4, 'delivered': 1, 'with_shortfall': 1,
                                 'supplier_cancelled': 1, 'failed': 2, 'client_cancelled': 1, 'admin_cancelled': 0, 'missing_units': 2,
                                 'failure_rate': 0.5}])
        quotations = self.client.get('/api/v1/management/analytics/', {'days': 7}).data['quotations']
        self.assertEqual(quotations['handshaked_orders'], 4)  # confirmed deals stay confirmed after delivery or cancellation
        self.assertEqual(check()['problems'], {})
