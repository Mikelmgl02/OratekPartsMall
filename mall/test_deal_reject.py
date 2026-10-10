from rest_framework.test import APITestCase

from .request_models import DealEvent, SupplierRequest
from . import test_deals as deal_tests


class DealRejectTests(APITestCase):
    """Rechazar: the client ends an order before any agreement, with a reason."""
    setUp = deal_tests.DealWorkflowTests.setUp
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

    def reject(self, order, **extra):
        self.client.force_authenticate(self.buyer)
        return self.action(order, 'reject', **extra)

    def test_the_client_rejects_an_order_before_any_quotation(self):
        order = self.order()
        response = self.reject(order, reason_code='not_needed', reason='lo compramos en tienda')
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual((order.status, order.cancelled_side, order.cancel_reason, order.cancel_note),
                         ('cancelled', 'client', 'not_needed', 'LO COMPRAMOS EN TIENDA'))
        event = DealEvent.objects.filter(order=order).latest('id')
        self.assertEqual((event.kind, event.description), ('reject', 'EL CLIENTE RECHAZÓ LA ORDEN: YA NO LO NECESITA. LO COMPRAMOS EN TIENDA'))
        self.client.force_authenticate(self.seller_a)
        titles = [row['title'] for row in self.client.get('/api/v1/inbox/notifications/').data['results']]
        self.assertIn('CLIENTE ORIGINAL rechazó la orden', titles)

    def test_a_quoted_order_is_rejected_only_against_the_quotation_the_client_saw(self):
        order = self.order()
        self.review(order)
        self.assertEqual(self.quote(order).status_code, 200)
        order.refresh_from_db()
        current = order.quotations.get()
        self.assertEqual(self.reject(order, reason_code='found_elsewhere').status_code, 409)  # without naming it
        self.assertEqual(self.reject(order, quotation_id=str(current.pk)).status_code, 400)  # without a reason
        self.assertEqual(self.reject(order, quotation_id=str(current.pk), reason_code='found_elsewhere').status_code, 200)
        self.assertEqual(SupplierRequest.objects.get(pk=order.pk).status, 'cancelled')

    def test_only_the_client_rejects_and_only_before_the_agreement(self):
        order = self.order()
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.action(order, 'reject', account=self.supplier_a, reason_code='other').status_code, 403)
        self.review(order)
        self.quote(order)
        order.refresh_from_db()
        quotation = str(order.quotations.get().pk)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'accept', quotation_id=quotation).status_code, 200)
        self.assertEqual(self.reject(order, quotation_id=quotation, reason_code='not_needed').status_code, 409)
