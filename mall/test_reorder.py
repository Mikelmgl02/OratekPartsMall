import uuid

from rest_framework.test import APITestCase

from . import test_deal_lifecycle as lifecycle_tests
from . import test_requests as request_tests
from .basket_models import BasketEvent, BasketItem
from .models import SupplierItem


class ReorderMissingTests(APITestCase):
    """Pedir lo que falta a otro proveedor: what an order will not bring, offered to the other suppliers with its SKU in stock."""
    locals().update({name: getattr(lifecycle_tests.DealLifecycleTests, name) for name in
                     ('setUp', 'account', 'item', 'url', 'payload', 'submit', 'order', 'review', 'action', 'drafted', 'quote', 'handshake',
                      'supplier', 'buyer_does', 'lines')})

    def missing_url(self, order, account=None):
        return f'/api/v1/accounts/{(account or self.client_account).pk}/sent-requests/{order.pk}/missing/'

    def missing(self, order):
        self.client.force_authenticate(self.buyer)
        response = self.client.get(self.missing_url(order))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def short_quote(self):
        """An order for 3 of A-001 and 2 of A-002 whose quotation offers 1 of A-001 and none of A-002 -> (order, its lines by item)."""
        self.client.force_authenticate(self.buyer)
        order = self.order()
        self.assertEqual(self.missing(order)['lines'], [])  # nothing is known before a quotation
        self.review(order)
        lines = self.lines(order)
        self.assertEqual(self.quote(order, [{'order_line_id': lines[self.item_a.pk], 'quantity': 1, 'unit_price': '12.35'},
                                            {'order_line_id': lines[self.item_a2.pk], 'quantity': 0, 'unit_price': '12.35'}]).status_code, 200)
        order.refresh_from_db()
        return order, lines

    def test_a_short_quotation_offers_what_is_missing_to_the_other_suppliers_in_stock(self):
        order, lines = self.short_quote()
        data = self.missing(order)
        offer = {'supplier_id': str(self.supplier_b.pk), 'supplier_name': 'PROVEEDOR B', 'known': False,
                 'items': [{'id': str(self.item_b.pk), 'codigo': '58411-1R000-G', 'brand': 'KSM', 'description': 'TAMBOR B-001'}]}
        self.assertEqual([(line['order_line_id'], line['codigo'], line['requested'], line['missing'], line['suggested'], line['offers'], line['in_basket'])
                          for line in data['lines']],
                         [(lines[self.item_a.pk], '58411-1R000-G', 3, 2, str(self.item_b.pk), [offer], 0),
                          (lines[self.item_a2.pk], '58411-1R000-KSM', 2, 2, str(self.item_b.pk), [offer], 0)])
        self.assertEqual((data['order']['reference'], data['added_at']), (order.reference, None))
        # Out of stock, a supplier is no offer (and no quantity ever shows).
        SupplierItem.objects.filter(pk=self.item_b.pk).update(reserved_quantity=20)
        self.assertEqual({(line['suggested'], len(line['offers'])) for line in self.missing(order)['lines']}, {(None, 0)})
        SupplierItem.objects.filter(pk=self.item_b.pk).update(reserved_quantity=0)

        added = self.client.post(self.missing_url(order), {'lines': [{'order_line_id': lines[self.item_a.pk], 'supplier_item_id': str(self.item_b.pk), 'quantity': 2},
                                                                     {'order_line_id': lines[self.item_a2.pk], 'supplier_item_id': str(self.item_b.pk), 'quantity': 2}]},
                                 format='json')
        self.assertEqual(added.status_code, 200, added.data)
        self.assertEqual([(line['offer']['supplier_name'], line['quantity'], line['source']) for line in added.data['lines']], [('PROVEEDOR B', 4, 'faltante')])
        self.assertEqual(list(BasketEvent.objects.order_by('id').values_list('kind', 'quantity_before', 'quantity_after', 'source', 'reference')),
                         [('added', 0, 2, 'faltante', order.reference), ('changed', 2, 4, 'faltante', order.reference)])
        again = self.missing(order)
        self.assertEqual(({line['in_basket'] for line in again['lines']}, again['added_at'] is not None), ({4}, True))

        refused = self.client.post(self.missing_url(order), {'lines': [{'order_line_id': lines[self.item_a.pk], 'supplier_item_id': str(self.item_a.pk), 'quantity': 1}]},
                                   format='json')
        self.assertEqual(refused.status_code, 409)  # the order's own supplier is no alternative
        self.assertEqual(self.client.post(self.missing_url(order), {'lines': [{'order_line_id': str(uuid.uuid4()), 'supplier_item_id': str(self.item_b.pk),
                                                                               'quantity': 1}]}, format='json').status_code, 400)
        self.assertEqual(BasketItem.objects.get().quantity, 4)  # refused: nothing changed
        self.client.force_authenticate(self.seller_b)
        self.assertEqual(self.client.get(self.missing_url(order)).status_code, 404)  # a stranger to the client account reads nothing
        self.assertEqual(self.client.get(self.missing_url(order, self.supplier_b)).status_code, 403)  # only a client account asks

    def test_after_agrupar_sku_what_is_missing_is_offered_under_the_sku_it_went_into(self):
        order, lines = self.short_quote()
        target = request_tests.SupplierRequestWorkflowTests.merge_part(self)  # the order lines keep the merged SKU
        data = self.missing(order)
        self.assertEqual({(line['part_id'], line['sku'], line['suggested']) for line in data['lines']},
                         {(str(target.pk), target.sku, str(self.item_b.pk))})

    def test_after_the_deal_a_shortfall_is_missing_and_a_cancelled_deal_misses_everything(self):
        order, quotation = self.handshake()
        self.assertEqual(self.missing(order)['lines'], [])  # everything agreed is coming
        lines = self.lines(order)
        self.assertEqual(self.supplier(order, 'report_shortfall', quotation, shortfall=[{'order_line_id': lines[self.item_a.pk], 'missing': 2}],
                                       reason_code='no_stock').status_code, 200)
        self.assertEqual([(line['codigo'], line['missing']) for line in self.missing(order)['lines']], [('58411-1R000-G', 2)])  # waiting for the client counts
        self.assertEqual(self.buyer_does(order, 'cancel', quotation, reason_code='shortfall', reason='').status_code, 200)
        self.assertEqual([(line['codigo'], line['missing']) for line in self.missing(order)['lines']], [('58411-1R000-G', 3), ('58411-1R000-KSM', 2)])
