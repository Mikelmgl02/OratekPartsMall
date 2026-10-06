"""S6 · publication control: the opt-in publish permission, approval requests on drafts and the supplier list badges."""
import uuid
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from . import test_deals as deal_tests
from . import test_pricing_engine as engine_tests
from . import test_quote_drafts as draft_tests
from . import test_requests as request_tests
from .deal_views import PUBLISH_DENIED
from .models import Membership, User
from .pricing_models import PricingAuditEvent, SupplierPricingSettings
from .quote_draft_models import DealQuotationAudit, DealQuotationDraft
from .request_models import DealCommand, DealEvent, DealQuotation, SupplierRequest
from .test_pricing_privacy import SENT_SUMMARY_KEYS, SUMMARY_KEYS, DEAL_KEYS


class QuoteControlTests(APITestCase):
    account = request_tests.SupplierRequestWorkflowTests.account
    item = request_tests.SupplierRequestWorkflowTests.item
    url = request_tests.SupplierRequestWorkflowTests.url
    payload = request_tests.SupplierRequestWorkflowTests.payload
    submit = request_tests.SupplierRequestWorkflowTests.submit
    order = deal_tests.DealWorkflowTests.order
    review = deal_tests.DealWorkflowTests.review
    action = deal_tests.DealWorkflowTests.action
    draft_url = draft_tests.QuoteDraftTests.draft_url
    reviewed = draft_tests.QuoteDraftTests.reviewed
    save = draft_tests.QuoteDraftTests.save
    get_draft = draft_tests.QuoteDraftTests.get_draft
    by_code = draft_tests.QuoteDraftTests.by_code
    price = engine_tests.PricedDraftTests.price
    general = engine_tests.PricedDraftTests.general
    reprice = engine_tests.PricedDraftTests.reprice

    def setUp(self):
        request_tests.SupplierRequestWorkflowTests.setUp(self)
        # seller_a keeps the default membership (staff); the supplier also has a manager and an owner.
        self.manager, self.owner = [User.objects.create_user(name, f'{name}@example.invalid', 'Request938!') for name in ['control-manager', 'control-owner']]
        Membership.objects.create(user=self.manager, account=self.supplier_a, permission='manager')
        Membership.objects.create(user=self.owner, account=self.supplier_a, permission='owner')

    def reviewed(self, lines=None):
        self.client.force_authenticate(self.buyer)
        return draft_tests.QuoteDraftTests.reviewed(self, lines)

    def require(self, permission):
        self.client.force_authenticate(self.owner)
        response = self.client.post(f'/api/v1/accounts/{self.supplier_a.pk}/pricing/settings/', {
            'expected_version': SupplierPricingSettings.objects.filter(supplier=self.supplier_a).values_list('version', flat=True).first() or 1,
            'publish_min_permission': permission}, format='json')
        self.assertEqual((response.status_code, response.data['publish_min_permission']), (200, permission))

    drafted = deal_tests.DealWorkflowTests.drafted

    def quote_as(self, user, order, **extra):
        """Publishes as user through the reviewed draft, which that member saves first unless draft_version is given."""
        self.client.force_authenticate(user)
        order.refresh_from_db()
        lines = [{'order_line_id': str(line.pk), 'quantity': line.quantity, 'unit_price': '12.35'} for line in order.lines.all()]
        if order.status in ('reviewed', 'adjustment') and 'draft_version' not in extra:
            extra['draft_version'] = self.drafted(order, lines, extra.get('currency', 'USD'), 'RETIRO MAÑANA')
        return self.action(order, 'quote', lines=lines, terms='RETIRO MAÑANA', **extra)

    def state(self, order):
        order.refresh_from_db()
        return order.status, order.version, DealQuotation.objects.filter(order=order).count(), DealCommand.objects.filter(order=order).count(), \
            DealEvent.objects.filter(order=order).count()

    def listed(self, user=None):
        self.client.force_authenticate(user or self.seller_a)
        return {row['reference']: row['draft_state'] for row in self.client.get(self.url(self.supplier_a)).data['results']}

    def test_default_settings_let_every_supplier_member_publish(self):
        order = self.reviewed()
        draft = self.get_draft(order)
        self.assertEqual(draft['permissions'], {'can_publish': True, 'publish_requires': 'staff'})
        response = self.quote_as(self.seller_a, order)
        self.assertEqual((response.status_code, response.data['status']), (200, 'quoted'))
        self.assertEqual(DealQuotationAudit.objects.get().publisher_permission, 'staff')
        self.assertFalse(SupplierPricingSettings.objects.exists(), 'Publishing reads the default settings without creating them.')

    def test_opt_in_manager_gate_refuses_staff_with_403_before_any_version_conflict(self):
        self.require('manager')
        order = self.reviewed()
        draft = self.get_draft(order)
        self.assertEqual(draft['permissions'], {'can_publish': False, 'publish_requires': 'manager'})
        before = self.state(order)
        for label, extra in [('versión vigente', {}), ('versión vieja', {'expected_version': order.version - 1})]:
            with self.subTest(label):
                refused = self.quote_as(self.seller_a, order, **extra)
                self.assertEqual((refused.status_code, refused.data['detail']), (403, PUBLISH_DENIED))
        self.assertEqual(self.state(order), before)
        self.assertFalse(DealQuotationAudit.objects.exists())
        self.assertEqual(self.quote_as(self.manager, order, expected_version=order.version - 1).status_code, 409)
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.get_draft(order)['permissions'], {'can_publish': True, 'publish_requires': 'manager'})
        published = self.quote_as(self.manager, order)
        self.assertEqual((published.status_code, published.data['status']), (200, 'quoted'))
        self.assertEqual(DealQuotationAudit.objects.get().publisher_permission, 'manager')
        self.client.force_authenticate(self.buyer)
        quotation = published.data['quotation']['id']
        self.assertEqual(self.action(order, 'request_adjustment', quotation_id=quotation, reason='MENOS UNIDADES').status_code, 200)
        # return_quote republishes the same revision, so it needs the same permission.
        self.client.force_authenticate(self.seller_a)
        returned = self.action(order, 'return_quote', quotation_id=quotation)
        self.assertEqual((returned.status_code, returned.data['detail'], SupplierRequest.objects.get(pk=order.pk).status), (403, PUBLISH_DENIED, 'adjustment'))
        self.require('owner')
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.action(order, 'return_quote', quotation_id=quotation).status_code, 403)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.action(order, 'return_quote', quotation_id=quotation).data['status'], 'quoted')

    def test_same_account_operation_id_replay_returns_the_stored_decision_before_the_gate(self):
        """Documented behaviour: the replay key is (order, account, operation_id) and is checked before every gate. A member without the
        publish permission who sends the very same payload with the same unguessable id receives the result of the decision a manager
        already took (it repeats nothing and grants nothing); a different payload is refused; a refused attempt stores nothing."""
        self.require('manager')
        order = self.reviewed()
        key, version = uuid.uuid4(), order.version
        published = self.quote_as(self.manager, order, operation_id=key, expected_version=version)
        self.assertEqual(published.status_code, 200)
        before = self.state(order)
        replayed = self.quote_as(self.seller_a, order, operation_id=key, expected_version=version, draft_version=self.draft_version)
        self.assertEqual((replayed.status_code, replayed.data), (200, published.data))
        self.assertEqual(self.state(order), before)
        changed = self.quote_as(self.seller_a, order, operation_id=key, expected_version=version, currency='PAB')
        self.assertEqual((changed.status_code, changed.data['detail']), (409, 'Este identificador ya se usó para otra decisión.'))
        # Another account's command never matches: the client gets the side check, not the supplier's stored result.
        self.client.force_authenticate(self.buyer)
        other = self.action(order, 'quote', account=self.client_account, operation_id=key, expected_version=version,
                            lines=[{'order_line_id': str(line.pk), 'quantity': line.quantity, 'unit_price': '12.35'} for line in order.lines.all()], terms='RETIRO MAÑANA')
        self.assertEqual(other.status_code, 403)
        # A refusal is not a stored decision: once the owner relaxes the gate, the same id and payload go through.
        second = self.reviewed([(self.item_a, 1)])
        retry_key = uuid.uuid4()
        self.assertEqual(self.quote_as(self.seller_a, second, operation_id=retry_key).status_code, 403)
        self.assertFalse(DealCommand.objects.filter(order=second).exists())
        self.require('staff')
        self.assertEqual(self.quote_as(self.seller_a, second, operation_id=retry_key, draft_version=self.draft_version).status_code, 200)

    def test_client_decisions_ignore_publish_permissions(self):
        self.require('owner')
        # Even a client account that also stores pricing settings: the gate applies to the supplier's quote and return_quote only.
        SupplierPricingSettings.objects.create(supplier=self.client_account, publish_min_permission='owner')
        order = self.reviewed()
        quotation = self.quote_as(self.owner, order).data['quotation']['id']
        self.client.force_authenticate(self.buyer)
        self.assertEqual(Membership.objects.get(user=self.buyer, account=self.client_account).permission, 'staff')
        self.assertEqual(self.action(order, 'request_adjustment', quotation_id=quotation, reason='OTRA FECHA').status_code, 200)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.action(order, 'return_quote', quotation_id=quotation).status_code, 200)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'accept', quotation_id=quotation).data['status'], 'handshaked')

    def test_staff_request_approval_and_a_manager_publishes_the_reviewed_draft(self):
        self.require('manager')
        order = self.reviewed()
        ids = {line.supplier_item_id: str(line.pk) for line in order.lines.all()}
        before = (SupplierRequest.objects.values('version', 'updated_at').get(pk=order.pk), DealEvent.objects.count())
        self.client.force_authenticate(self.seller_a)
        key = uuid.uuid4()
        body = {'lines': [{'order_line_id': ids[self.item_a.pk], 'unit_price': '10.00'}, {'order_line_id': ids[self.item_a2.pk], 'unit_price': '4.00'}],
                'request_review': True}
        requested = self.save(order, 0, save_id=key, **body)
        self.assertEqual(requested.status_code, 200)
        self.assertEqual({field: requested.data[field] for field in ['persisted', 'draft_version', 'status', 'review_requested_by']},
                         {'persisted': True, 'draft_version': 1, 'status': 'review_requested', 'review_requested_by': {'name': 'request-seller-a'}})
        self.assertIsNotNone(requested.data['review_requested_at'])
        event = PricingAuditEvent.objects.get(kind='draft_review_requested')
        self.assertEqual((event.supplier, event.actor, event.client, event.order, event.payload),
                         (self.supplier_a, self.seller_a, self.client_account, order, {'draft_version': 1, 'revision': 1}))
        self.assertEqual(self.save(order, 0, save_id=key, **body).data, requested.data)
        self.assertEqual(PricingAuditEvent.objects.filter(kind='draft_review_requested').count(), 1)
        self.assertEqual(self.listed(), {order.reference: 'review_requested'})
        # The manager sees who asked and publishes exactly that draft; the request never touched the order, its events or the client.
        self.client.force_authenticate(self.manager)
        seen = self.get_draft(order)
        self.assertEqual((seen['status'], seen['review_requested_by'], seen['permissions']['can_publish']), ('review_requested', {'name': 'request-seller-a'}, True))
        self.assertEqual((SupplierRequest.objects.values('version', 'updated_at').get(pk=order.pk), DealEvent.objects.count()), before)
        self.client.force_authenticate(self.manager)
        published = self.action(order, 'quote', draft_version=1, terms='', lines=[{'order_line_id': ids[self.item_a.pk], 'quantity': 3, 'unit_price': '10.00'},
                                                                                  {'order_line_id': ids[self.item_a2.pk], 'quantity': 2, 'unit_price': '4.00'}])
        self.assertEqual((published.status_code, published.data['status']), (200, 'quoted'), published.data)
        self.assertFalse(DealQuotationDraft.objects.exists())
        self.assertEqual(self.listed(), {order.reference: None})

    def test_editing_what_the_client_receives_withdraws_the_request_and_notes_or_confirmations_keep_it(self):
        order = self.reviewed()
        ids = {line.supplier_item_id: str(line.pk) for line in order.lines.all()}
        first, second = ids[self.item_a.pk], ids[self.item_a2.pk]
        self.client.force_authenticate(self.seller_a)
        draft = self.save(order, 0, request_review=True).data
        self.assertEqual(draft['status'], 'review_requested')
        steps = [('nota interna', {'lines': [{'order_line_id': first, 'note': 'revisar empaque'}]}, 'review_requested'),
                 ('cantidad', {'lines': [{'order_line_id': first, 'quantity': 9}]}, 'editing'),
                 ('pedirla de nuevo', {'request_review': True}, 'review_requested'),
                 ('confirmar alerta', {'lines': [{'order_line_id': first, 'acknowledge': [{'code': 'offered_gt_available', 'context': '9>8'}]}]}, 'review_requested'),
                 ('precio', {'lines': [{'order_line_id': second, 'unit_price': '4.00'}]}, 'editing'),
                 ('precio y solicitud', {'lines': [{'order_line_id': second, 'unit_price': '4.50'}], 'request_review': True}, 'review_requested'),
                 ('misma moneda', {'currency': 'USD'}, 'review_requested'),
                 ('moneda', {'currency': 'PAB'}, 'editing'),
                 ('solicitud', {'request_review': True}, 'review_requested'),
                 ('condiciones', {'terms': ' entrega hoy '}, 'editing'),
                 ('solicitud otra vez', {'request_review': True}, 'review_requested'),
                 ('retirar', {'request_review': False}, 'editing'),
                 ('retirar sin solicitud', {'request_review': False}, 'editing')]
        for label, body, status in steps:
            with self.subTest(label):
                response = self.save(order, draft['draft_version'], **body)
                self.assertEqual(response.status_code, 200, response.data)
                draft = response.data
                self.assertEqual(draft['status'], status)
                self.assertEqual(draft['review_requested_by'] is None, status == 'editing')
        self.assertEqual(self.by_code(draft)['58411-1R000-G']['note'], 'REVISAR EMPAQUE')
        self.assertEqual(PricingAuditEvent.objects.filter(kind='draft_review_requested').count(), 5)
        # Every withdrawal is audited: by a change to what the client would receive, or by the member; withdrawing nothing records nothing.
        withdrawn = PricingAuditEvent.objects.filter(kind='draft_review_withdrawn').order_by('id')
        self.assertEqual([(event.actor, event.order, event.payload['reason']) for event in withdrawn],
                         [(self.seller_a, order, reason) for reason in ['edited', 'edited', 'edited', 'edited', 'withdrawn']])
        self.assertEqual(set(withdrawn.last().payload), {'draft_version', 'revision', 'reason'})
        self.assertEqual(self.save(order, draft['draft_version'], request_review='quizás').status_code, 400)

    def test_a_reprice_that_moves_a_price_withdraws_the_request(self):
        price_list = self.general()
        self.price(price_list, self.item_a, Decimal('13.50'))
        order = self.reviewed()
        self.client.force_authenticate(self.seller_a)
        draft = self.save(order, 0, request_review=True).data
        self.assertEqual((self.by_code(draft)['58411-1R000-G']['unit_price'], draft['status']), ('13.50', 'review_requested'))
        unchanged = self.reprice(order, 1, 'engine').data
        self.assertEqual((unchanged['repriced'], unchanged['status']), ([], 'review_requested'))
        self.price(price_list, self.item_a, Decimal('13.10'))
        moved = self.reprice(order, 2, 'engine').data
        self.assertEqual(([row['unit_price'] for row in moved['repriced']], moved['status'], moved['review_requested_by']), (['13.10'], 'editing', None))
        self.assertEqual(list(PricingAuditEvent.objects.filter(kind='draft_review_withdrawn').values_list('payload', flat=True)),
                         [{'draft_version': 3, 'revision': 1, 'reason': 'edited'}])

    def test_draft_state_appears_only_in_the_supplier_list_for_the_current_revision(self):
        order = self.reviewed()
        self.assertEqual(self.listed(), {order.reference: None})
        self.assertEqual(self.save(order, 0).status_code, 200)
        self.assertEqual(self.listed(), {order.reference: 'editing'})
        self.save(order, 1, request_review=True)
        self.assertEqual(self.listed(self.manager), {order.reference: 'review_requested'})
        self.client.force_authenticate(self.seller_a)
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.url(self.supplier_a))
        for index in range(3):
            self.assertEqual(self.save(self.reviewed([(self.item_a2, index + 1)]), 0).status_code, 200)
        self.client.force_authenticate(self.buyer)
        pending = self.order()
        self.client.force_authenticate(self.seller_a)
        with CaptureQueriesContext(connection) as large:
            rows = self.client.get(self.url(self.supplier_a)).data['results']
        self.assertEqual(len(large), len(small))
        self.assertEqual(len(rows), 5)
        self.assertEqual(sorted(row['draft_state'] or '' for row in rows), ['', 'editing', 'editing', 'editing', 'review_requested'])
        self.assertEqual(self.listed()[pending.reference], None)
        # The draft is published: no badge. A draft row left on an older revision is stale and shows nothing either.
        draft = self.get_draft(order)
        lines = [{'order_line_id': line['order_line_id'], 'quantity': line['quantity'], 'unit_price': '5.00'} for line in draft['lines']]
        draft = self.save(order, 2, lines=[{'order_line_id': line['order_line_id'], 'unit_price': '5.00'} for line in lines]).data
        self.assertEqual(self.action(order, 'quote', draft_version=draft['draft_version'], terms='', lines=lines).status_code, 200)
        self.assertEqual(self.listed()[order.reference], None)
        self.client.force_authenticate(self.buyer)
        quotation = DealQuotation.objects.get(order=order)
        self.assertEqual(self.action(order, 'request_adjustment', quotation_id=quotation.pk, reason='MENOS').status_code, 200)
        DealQuotationDraft.objects.create(order=order, supplier=self.supplier_a, base_quotation=None, created_by=self.seller_a, updated_by=self.seller_a)
        self.assertEqual(self.listed()[order.reference], None)
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.save(order, 0, request_review=True).data['status'], 'review_requested')
        self.assertEqual(self.listed()[order.reference], 'review_requested')
        # Client payloads and the supplier detail gain no key.
        detail = self.client.get(f'{self.url(self.supplier_a)}{order.pk}/').data
        self.assertEqual(set(detail), SUMMARY_KEYS | DEAL_KEYS | {'client', 'notes', 'lines'})
        self.client.force_authenticate(self.buyer)
        base = f'/api/v1/accounts/{self.client_account.pk}'
        self.assertEqual({key for row in self.client.get(f'{base}/sent-requests/').data['results'] for key in row}, SENT_SUMMARY_KEYS)
        self.assertEqual(set(self.client.get(f'{base}/sent-requests/{order.pk}/').data), SENT_SUMMARY_KEYS | DEAL_KEYS | {'notes', 'lines'})
        self.assertEqual(self.client.get(self.url(self.client_account)).status_code, 403)
        for command in DealCommand.objects.all():
            self.assertNotIn('draft_state', command.result)
            self.assertNotIn('review_requested', str(command.result))

    def test_owner_only_settings_return_403_for_a_manager_and_the_form_sends_only_changed_fields(self):
        settings_url = f'/api/v1/accounts/{self.supplier_a.pk}/pricing/settings/'
        self.client.force_authenticate(self.manager)
        current = self.client.get(settings_url).data
        self.assertEqual((current['permission'], current['can_configure'], current['can_manage_permissions']), ('manager', True, False))
        for field, value in [('publish_min_permission', 'manager'), ('config_min_permission', 'owner'), ('assistant_enabled', True)]:
            with self.subTest(field):
                refused = self.client.post(settings_url, {'expected_version': 1, field: value}, format='json')
                self.assertEqual(refused.status_code, 403)
                self.assertIn('propietario', refused.data['detail'])
        policy = self.client.post(settings_url, {'expected_version': 1, 'over_stock_policy': 'block'}, format='json')
        self.assertEqual((policy.status_code, policy.data['version']), (200, 2))
        self.require('manager')
        # A stale full form still carries the old owner-only value, which now counts as a change: 403 comes before the 409.
        stale_form = {key: current[key] for key in ['default_currency', 'usd_pab_parity', 'config_min_permission', 'publish_min_permission',
                                                    'over_request_policy', 'over_stock_policy', 'accept_shortfall_policy', 'prefill_quantity', 'assistant_enabled']}
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.post(settings_url, {**stale_form, 'expected_version': 2, 'prefill_quantity': 'available'}, format='json').status_code, 403)
        conflict = self.client.post(settings_url, {'expected_version': 2, 'prefill_quantity': 'available'}, format='json')
        self.assertEqual((conflict.status_code, conflict.data['settings']['version'], conflict.data['settings']['publish_min_permission']), (409, 3, 'manager'))
        saved = self.client.post(settings_url, {'expected_version': 3, 'prefill_quantity': 'available'}, format='json')
        self.assertEqual((saved.status_code, saved.data['version'], saved.data['prefill_quantity']), (200, 4, 'available'))
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.post(settings_url, {'expected_version': 4, 'usd_pab_parity': False}, format='json').status_code, 200)
