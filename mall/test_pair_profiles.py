import uuid
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, transaction
from rest_framework.test import APITestCase

from . import test_deals as deal_tests
from . import test_requests as request_tests
from .models import Membership, SupplierItem, User
from .pricing_models import ClientPricingProfile, PriceList, PricingAuditEvent, PricingRule, SupplierPricingSettings
from .pricing_services import set_prices


class PairProfileTests(APITestCase):
    setUp = request_tests.SupplierRequestWorkflowTests.setUp
    account = request_tests.SupplierRequestWorkflowTests.account
    item = request_tests.SupplierRequestWorkflowTests.item
    url = request_tests.SupplierRequestWorkflowTests.url
    payload = request_tests.SupplierRequestWorkflowTests.payload
    submit = request_tests.SupplierRequestWorkflowTests.submit
    order = deal_tests.DealWorkflowTests.order

    def base(self, account=None):
        return f'/api/v1/accounts/{(account or self.supplier_a).pk}'

    def profile_url(self, client=None, account=None):
        return f'{self.base(account)}/clients/{(client or self.client_account).pk}/profile/'

    def as_seller(self, user=None):
        self.client.force_authenticate(user or self.seller_a)

    def save_profile(self, version=0, operation_id=None, client=None, **fields):
        return self.client.post(self.profile_url(client), {'operation_id': str(operation_id or uuid.uuid4()), 'expected_version': version, **fields}, format='json')

    def create_rule(self, operation_id=None, **fields):
        body = {'operation_id': str(operation_id or uuid.uuid4()), 'name': 'regla', 'scope': 'all', 'target': 'all', 'kind': 'discount', 'value': '10.00', **fields}
        return self.client.post(f'{self.base()}/pricing-rules/', body, format='json')

    def related(self):
        """The client ordered from supplier A; another client and the supplier itself never did."""
        self.order()
        self.other_client = self.account('TALLER SIN ORDENES', 'client_business', User.objects.create_user('pp-other', 'pp-other@example.invalid', 'Request938!'))
        self.as_seller()

    def general(self):
        return PriceList.objects.create(supplier=self.supplier_a, code='GENERAL', name='LISTA GENERAL', is_default=True, created_by=self.seller_a)

    def test_clients_lists_only_accounts_that_ordered_with_their_profile_summary(self):
        self.related()
        mayorista = PriceList.objects.create(supplier=self.supplier_a, code='MAYORISTA', name='MAYORISTA', created_by=self.seller_a)
        self.general()
        second = self.account('TALLER NORTE', 'client_business', self.buyer)
        self.client.force_authenticate(self.buyer)
        self.submit(self.payload([(self.item_a, 1)]), account=second)
        self.submit(self.payload([(self.item_a2, 2)]))
        self.as_seller()
        response = self.client.get(f'{self.base()}/clients/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['default_list']['code'], 'GENERAL')
        rows = {row['name']: row for row in response.data['results']}
        self.assertEqual(set(rows), {'CLIENTE ORIGINAL', 'TALLER NORTE'})
        self.assertEqual(rows['CLIENTE ORIGINAL']['order_count'], 1, 'An appended basket joins the pending order.')
        self.assertEqual(rows['TALLER NORTE']['profile'], {'exists': False, 'version': 0, 'customer_code': '', 'price_list': None, 'discount_percent': '0.00',
                                                           'preferred_currency': '', 'rule_count': 0})
        self.assertEqual(self.save_profile(customer_code=' C-00123 ', price_list_id=str(mayorista.pk), discount_percent='5').status_code, 200)
        self.assertEqual(self.create_rule(scope='client', client_id=str(self.client_account.pk)).status_code, 201)
        summary = {row['name']: row for row in self.client.get(f'{self.base()}/clients/').data['results']}['CLIENTE ORIGINAL']['profile']
        self.assertEqual((summary['customer_code'], summary['price_list']['code'], summary['discount_percent'], summary['rule_count']), ('C-00123', 'MAYORISTA', '5.00', 1))
        self.assertEqual([row['name'] for row in self.client.get(f'{self.base()}/clients/?search=c-001').data['results']], ['CLIENTE ORIGINAL'])
        self.assertEqual([row['name'] for row in self.client.get(f'{self.base()}/clients/?search=norte').data['results']], ['TALLER NORTE'])
        # Competitors, clients and Oratek never list this supplier's clients.
        for user, account, expected in [(self.seller_b, self.supplier_a, 404), (self.root, self.supplier_a, 404), (self.buyer, self.client_account, 403)]:
            with self.subTest(user=user.username):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.get(f'{self.base(account)}/clients/').status_code, expected)
        self.client.force_authenticate(self.seller_b)
        self.assertEqual(self.client.get(f'{self.base(self.supplier_b)}/clients/').data['results'], [])

    def test_profile_reads_defaults_without_writing_and_only_for_related_clients(self):
        self.related()
        response = self.client.get(self.profile_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual({key: response.data[key] for key in ('exists', 'id', 'version', 'price_list_id', 'price_list', 'fallback_to_default', 'discount_percent',
                                                              'preferred_currency', 'default_terms', 'internal_notes', 'customer_code', 'effective_list',
                                                              'effective_currency', 'order_count', 'updated_by', 'can_configure')},
                         {'exists': False, 'id': None, 'version': 0, 'price_list_id': None, 'price_list': None, 'fallback_to_default': True, 'discount_percent': '0.00',
                          'preferred_currency': '', 'default_terms': '', 'internal_notes': '', 'customer_code': '', 'effective_list': None, 'effective_currency': 'USD',
                          'order_count': 1, 'updated_by': None, 'can_configure': True})
        self.assertEqual(response.data['client'], {'id': str(self.client_account.pk), 'name': 'CLIENTE ORIGINAL'})
        self.assertFalse(ClientPricingProfile.objects.exists())
        self.assertFalse(PricingAuditEvent.objects.exists())
        # Unrelated accounts, the supplier itself and made-up ids are all 404: no account enumeration.
        for client in [self.other_client, self.supplier_a, self.supplier_b]:
            with self.subTest(client=client.name):
                self.assertEqual(self.client.get(self.profile_url(client)).status_code, 404)
                self.assertEqual(self.save_profile(client=client, customer_code='X').status_code, 404)
        self.assertEqual(self.client.get(f'{self.base()}/clients/{uuid.uuid4()}/profile/').status_code, 404)
        for user, account, expected in [(self.seller_b, self.supplier_a, 404), (self.root, self.supplier_a, 404), (self.buyer, self.client_account, 403)]:
            with self.subTest(user=user.username):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.get(self.profile_url(account=account)).status_code, expected)
                self.assertEqual(self.client.post(self.profile_url(account=account), {'operation_id': str(uuid.uuid4()), 'expected_version': 0}, format='json').status_code, expected)
        self.assertFalse(ClientPricingProfile.objects.exists())
        # The database refuses a self-profile even outside the API.
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClientPricingProfile.objects.create(supplier=self.supplier_a, client=self.supplier_a, updated_by=self.seller_a)

    def test_profile_saves_are_versioned_retry_safe_validated_and_audited(self):
        self.related()
        general, mayorista = self.general(), PriceList.objects.create(supplier=self.supplier_a, code='MAYORISTA', name='MAYORISTA', created_by=self.seller_a)
        key = uuid.uuid4()
        created = self.save_profile(0, key, price_list_id=str(mayorista.pk), discount_percent='5.5', preferred_currency='PAB', customer_code=' c-00123 ',
                                    default_terms=' precios no incluyen itbms ', internal_notes=' paga a 30 días ')
        self.assertEqual(created.status_code, 200, created.data)
        self.assertEqual({key: created.data[key] for key in ('exists', 'version', 'discount_percent', 'preferred_currency', 'customer_code', 'default_terms',
                                                             'internal_notes', 'effective_currency')},
                         {'exists': True, 'version': 1, 'discount_percent': '5.50', 'preferred_currency': 'PAB', 'customer_code': 'c-00123',
                          'default_terms': 'PRECIOS NO INCLUYEN ITBMS', 'internal_notes': 'PAGA A 30 DÍAS', 'effective_currency': 'PAB'})
        self.assertEqual((created.data['price_list']['code'], created.data['effective_list']['code'], created.data['updated_by']), ('MAYORISTA', 'MAYORISTA', {'name': 'request-seller-a'}))
        event = PricingAuditEvent.objects.get(kind='profile_changed')
        self.assertEqual((event.client_id, event.object_id, event.payload['action'], event.payload['version'], event.payload['new']['discount_percent']),
                         (self.client_account.pk, created.data['id'], 'created', 1, '5.50'))
        # A lost-response retry returns the profile; the same key with other values is refused.
        replay = self.save_profile(0, key, price_list_id=str(mayorista.pk), discount_percent='5.5', preferred_currency='PAB', customer_code=' c-00123 ',
                                   default_terms=' precios no incluyen itbms ', internal_notes=' paga a 30 días ')
        self.assertEqual((replay.status_code, replay.data['version']), (200, 1))
        self.assertEqual(self.save_profile(0, key, discount_percent='6').status_code, 409)
        # Values already in place change nothing, even on an old version; a real change on an old version is a 409 with the current profile.
        self.assertEqual(self.save_profile(0, discount_percent='5.50').data['version'], 1)
        stale = self.save_profile(0, discount_percent='7')
        self.assertEqual((stale.status_code, stale.data['detail'], stale.data['profile']['version']), (409, 'Otro usuario actualizó este perfil. Revisa sus valores actuales.', 1))
        updated = self.save_profile(1, price_list_id=None, fallback_to_default=False)
        self.assertEqual((updated.data['version'], updated.data['price_list'], updated.data['effective_list']['code'], updated.data['fallback_to_default']), (2, None, 'GENERAL', False))
        self.assertEqual(PricingAuditEvent.objects.filter(kind='profile_changed').count(), 2)
        self.assertEqual(PricingAuditEvent.objects.filter(kind='profile_changed').first().payload['old'], {'price_list_id': str(mayorista.pk), 'fallback_to_default': True})
        # Validation is Spanish and nothing is written.
        PriceList.objects.filter(pk=mayorista.pk).update(active=False)
        foreign = PriceList.objects.create(supplier=self.supplier_b, code='AJENA', name='AJENA', is_default=True, created_by=self.seller_b)
        for field, value in [('discount_percent', '100'), ('discount_percent', '-1'), ('discount_percent', '5.555'), ('discount_percent', 'cinco'),
                             ('preferred_currency', 'EUR'), ('price_list_id', str(foreign.pk)), ('price_list_id', str(mayorista.pk)),
                             ('customer_code', 'X' * 41), ('default_terms', 'X' * 5001), ('internal_notes', 'X' * 4001), ('fallback_to_default', 'quizás')]:
            with self.subTest(field=field, value=value[:10]):
                refused = self.save_profile(2, **{field: value})
                self.assertEqual(refused.status_code, 400)
                self.assertIn(field, refused.data)
        self.assertEqual(ClientPricingProfile.objects.get().version, 2)
        self.assertEqual(self.save_profile(2, price_list_id=str(general.pk), preferred_currency='').data['effective_currency'], 'USD')

    def test_config_permission_gates_profile_and_rule_writes_and_403_comes_before_409(self):
        self.related()
        manager = User.objects.create_user('pp-manager', 'pp-manager@example.invalid', 'Request938!')
        Membership.objects.create(user=manager, account=self.supplier_a, permission='manager')
        SupplierPricingSettings.objects.create(supplier=self.supplier_a, config_min_permission='manager')
        for label, call in [('perfil', lambda: self.save_profile(5, customer_code='X')), ('regla', lambda: self.create_rule()),
                            ('editar regla', lambda: self.client.post(f'{self.base()}/pricing-rules/{uuid.uuid4()}/', {'expected_version': 9}, format='json')),
                            ('archivar', lambda: self.client.post(f'{self.base()}/pricing-rules/{uuid.uuid4()}/archive/', {'expected_version': 9}, format='json'))]:
            with self.subTest(label):
                self.assertEqual(call().status_code, 403)
        self.assertFalse(self.client.get(self.profile_url()).data['can_configure'])
        self.assertFalse(self.client.get(f'{self.base()}/pricing-rules/').data['can_configure'])
        self.assertEqual(self.client.post(f'{self.base()}/pricing/simulate/', {'items': [{'supplier_item_id': str(self.item_a.pk), 'quantity': 1}]}, format='json').status_code, 200)
        self.as_seller(manager)
        self.assertEqual(self.save_profile(0, customer_code='X').status_code, 200)
        self.assertEqual(self.save_profile(0, customer_code='Y').status_code, 409)

    def test_rules_are_coherent_unique_in_time_retry_safe_and_archived_not_deleted(self):
        self.related()
        foreign = SupplierItem.objects.get(pk=self.item_b.pk)
        client = str(self.client_account.pk)
        cases = [
            ('cliente sin cliente', {'scope': 'client'}, 'client_id'), ('general con cliente', {'client_id': client}, 'client_id'),
            ('artículo sin artículo', {'target': 'item'}, 'item_id'), ('artículo con marca', {'target': 'item', 'item_id': str(self.item_a.pk), 'target_value': 'KSM'}, 'target_value'),
            ('línea sin valor', {'target': 'line'}, 'target_value'), ('marca con artículo', {'target': 'brand', 'target_value': 'KSM', 'item_id': str(self.item_a.pk)}, 'item_id'),
            ('todos con valor', {'target_value': 'KSM'}, 'target'), ('neto en una línea', {'kind': 'net_price', 'target': 'line', 'target_value': 'FILTROS'}, 'kind'),
            ('descuento 100', {'value': '100'}, 'value'), ('descuento 0', {'value': '0'}, 'value'), ('descuento con moneda', {'currency': 'USD'}, 'currency'),
            ('tres decimales', {'value': '10.005'}, 'value'), ('fechas al revés', {'valid_from': '2026-10-10', 'valid_until': '2026-10-01'}, 'valid_until'),
            ('cantidad 0', {'min_quantity': 0}, 'min_quantity'), ('cantidad booleana', {'min_quantity': True}, 'min_quantity'), ('sin nombre', {'name': '  '}, 'name'),
            ('artículo ajeno', {'target': 'item', 'item_id': str(foreign.pk)}, 'item_id'), ('alcance desconocido', {'scope': 'grupo'}, 'scope')]
        for label, fields, error in cases:
            with self.subTest(label):
                response = self.create_rule(**fields)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(error, response.data)
        self.assertEqual(self.create_rule(scope='client', client_id=str(self.other_client.pk)).status_code, 404)
        self.assertFalse(PricingRule.objects.exists())
        self.assertFalse(ClientPricingProfile.objects.exists())
        # A client rule creates the pair's profile; a net price takes the supplier's default currency when none is sent.
        key = uuid.uuid4()
        body = {'name': ' neto taller ', 'scope': 'client', 'client_id': client, 'target': 'item', 'item_id': str(self.item_a.pk), 'kind': 'net_price', 'value': '16.80',
                'min_quantity': 10, 'note': ' acuerdo de octubre '}
        created = self.create_rule(key, **body)
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual({field: created.data[field] for field in ('name', 'scope', 'target', 'kind', 'value', 'currency', 'min_quantity', 'note', 'revision', 'validity', 'active')},
                         {'name': 'NETO TALLER', 'scope': 'client', 'target': 'item', 'kind': 'net_price', 'value': '16.80', 'currency': 'USD', 'min_quantity': 10,
                          'note': 'ACUERDO DE OCTUBRE', 'revision': 1, 'validity': 'active', 'active': True})
        self.assertEqual((created.data['client']['name'], created.data['item']['codigo']), ('CLIENTE ORIGINAL', '58411-1R000-G'))
        profile = ClientPricingProfile.objects.get()
        self.assertEqual((profile.client_id, profile.version), (self.client_account.pk, 1))
        self.assertEqual(list(PricingAuditEvent.objects.order_by('id').values_list('kind', flat=True)), ['profile_changed', 'rule_created'])
        self.assertEqual(self.create_rule(key, **body).status_code, 200, 'The same operation returns the rule it created.')
        self.assertEqual(self.create_rule(key, **{**body, 'value': '16.90'}).status_code, 409)
        # The same rule active in overlapping dates is refused; a window that does not touch it is accepted.
        october = {'name': 'promo ksm', 'target': 'brand', 'target_value': 'ksm', 'value': '15', 'valid_from': '2026-10-01', 'valid_until': '2026-10-31'}
        self.assertEqual(self.create_rule(**october).status_code, 201)
        duplicate = self.create_rule(**{**october, 'name': 'otra promo', 'value': '12', 'valid_from': '2026-10-31', 'valid_until': None})
        self.assertEqual((duplicate.status_code, str(duplicate.data[0])), (400, 'Ya tienes una regla igual vigente en esas fechas: PROMO KSM. Edítala o cambia sus fechas.'))
        self.assertEqual(self.create_rule(**{**october, 'name': 'promo ksm 2', 'valid_from': None, 'valid_until': '2026-09-30'}).status_code, 201)
        self.assertEqual(self.create_rule(**{**october, 'name': 'promo ksm 10+', 'min_quantity': 10}).status_code, 201, 'Another minimum quantity is a volume break.')
        promo = PricingRule.objects.get(name='PROMO KSM')
        url = f'{self.base()}/pricing-rules/{promo.pk}/'
        # Updates use the revision: values in place change nothing; an old revision is a 409; a real change bumps it and is audited.
        self.assertEqual(self.client.post(url, {'expected_version': 9, 'value': '15.00'}, format='json').data['revision'], 1)
        conflict = self.client.post(url, {'expected_version': 9, 'value': '14'}, format='json')
        self.assertEqual((conflict.status_code, conflict.data['rule']['revision']), (409, 1))
        moved = self.client.post(url, {'expected_version': 1, 'value': '14', 'valid_until': '2026-11-15'}, format='json')
        self.assertEqual((moved.status_code, moved.data['revision'], moved.data['value'], moved.data['valid_until']), (200, 2, '14.00', '2026-11-15'))
        self.assertEqual(PricingAuditEvent.objects.filter(kind='rule_updated').get().payload['old'], {'value': '15.00', 'valid_until': '2026-10-31'})
        self.assertEqual(self.client.post(url, {'expected_version': 2, 'kind': 'net_price'}, format='json').status_code, 400)
        retarget = self.client.post(url, {'expected_version': 2, 'target': 'item', 'item_id': str(self.item_a2.pk)}, format='json')
        self.assertEqual((retarget.status_code, retarget.data['target_value'], retarget.data['item']['codigo']), (200, '', '58411-1R000-KSM'))
        # Archiving never deletes; archiving twice changes nothing; reactivating checks duplicates again.
        archive = f'{self.base()}/pricing-rules/{promo.pk}/archive/'
        self.assertEqual(self.client.post(archive, {'expected_version': 1}, format='json').status_code, 409)
        archived = self.client.post(archive, {'expected_version': 3}, format='json')
        self.assertEqual((archived.status_code, archived.data['active'], archived.data['validity'], archived.data['revision']), (200, False, 'archived', 4))
        self.assertEqual(self.client.post(archive, {'expected_version': 1}, format='json').data['revision'], 4)
        self.assertEqual(PricingRule.objects.count(), 4)
        self.assertEqual(list(PricingAuditEvent.objects.filter(kind='rule_archived').values_list('object_id', flat=True)), [str(promo.pk)])
        clone = self.create_rule(name='clon', target='item', item_id=str(self.item_a2.pk), value='14')
        self.assertEqual(clone.status_code, 201)
        self.assertEqual(self.client.post(url, {'expected_version': 4, 'active': True}, format='json').status_code, 400)
        # Lists: active first, filters by status, scope, client and text; another client's rules are 404 for an unrelated account.
        listed = self.client.get(f'{self.base()}/pricing-rules/').data
        self.assertEqual((listed['count'], listed['can_configure'], listed['results'][-1]['name']), (5, True, 'PROMO KSM'))
        self.assertEqual([row['name'] for row in self.client.get(f'{self.base()}/pricing-rules/?status=archived').data['results']], ['PROMO KSM'])
        self.assertEqual([row['name'] for row in self.client.get(f'{self.base()}/pricing-rules/?client={client}').data['results']], ['NETO TALLER'])
        self.assertEqual(self.client.get(f'{self.base()}/pricing-rules/?scope=all&status=active').data['count'], 3)
        self.assertEqual([row['name'] for row in self.client.get(f'{self.base()}/pricing-rules/?search=octubre').data['results']], ['NETO TALLER'])
        self.assertEqual(self.client.get(f'{self.base()}/pricing-rules/?client={self.other_client.pk}').status_code, 404)
        self.assertEqual(self.client.get(f'{self.base()}/pricing-rules/?client=abc').status_code, 400)
        self.client.force_authenticate(self.seller_b)
        self.assertEqual(self.client.post(url, {'expected_version': 4, 'name': 'ROBADA'}, format='json').status_code, 404)
        self.assertEqual(self.client.get(f'{self.base(self.supplier_b)}/pricing-rules/').data['count'], 0)
        self.assertEqual(self.client.post(f'{self.base(self.supplier_b)}/pricing-rules/{promo.pk}/archive/', {'expected_version': 4}, format='json').status_code, 404)

    def test_active_rules_are_capped_per_supplier(self):
        self.related()
        with mock.patch('mall.pricing_views.MAX_ACTIVE_RULES', 2):
            self.assertEqual(self.create_rule(name='uno', value='1').status_code, 201)
            self.assertEqual(self.create_rule(name='dos', value='2', min_quantity=2).status_code, 201)
            refused = self.create_rule(name='tres', value='3', min_quantity=3)
            self.assertEqual((refused.status_code, str(refused.data[0])), (400, 'Llegaste al máximo de 2 reglas activas. Archiva las que ya no uses.'))

    def test_simulator_prices_with_the_pair_rules_and_writes_nothing(self):
        self.related()
        general = self.general()
        set_prices(self.supplier_a, self.seller_a, changes=[{'list_id': general.pk, 'supplier_item_id': self.item_a.pk, 'unit_price': Decimal('15.00'), 'expected_revision': None}],
                   items=[{'supplier_item_id': self.item_a.pk, 'discount_group': 'FILTROS', 'expected_revision': None}])
        self.assertEqual(self.save_profile(0, discount_percent='5', preferred_currency='PAB').status_code, 200)
        self.assertEqual(self.create_rule(name='promo ksm', target='brand', target_value='KSM', value='15', valid_until='2026-10-31').status_code, 201)
        self.assertEqual(self.create_rule(name='filtros taller', scope='client', client_id=str(self.client_account.pk), target='line', target_value='FILTROS',
                                          value='10').status_code, 201)
        self.assertEqual(self.create_rule(name='filtros volumen', scope='client', client_id=str(self.client_account.pk), target='line', target_value='FILTROS',
                                          value='15', min_quantity=10).status_code, 201)
        before = (PricingAuditEvent.objects.count(), PricingRule.objects.count(), ClientPricingProfile.objects.get().version)
        url = f'{self.base()}/pricing/simulate/'
        items = [{'supplier_item_id': str(self.item_a.pk), 'quantity': 4}, {'supplier_item_id': str(self.item_a2.pk), 'quantity': 1}]
        result = self.client.post(url, {'client_id': str(self.client_account.pk), 'on_date': '2026-10-05', 'items': items}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual((result.data['client']['name'], result.data['currency'], result.data['on_date'], result.data['profile']['discount_percent'], result.data['price_list']['code']),
                         ('CLIENTE ORIGINAL', 'PAB', '2026-10-05', '5.00', 'GENERAL'))
        first, second = result.data['lines']
        self.assertEqual((first['unit_price'], first['line_total'], first['discount_group'], first['explanation']['steps'][0]['name']), ('13.50', '54.00', 'FILTROS', 'FILTROS TALLER'))
        self.assertEqual([item['name'] for item in first['explanation']['discarded']], ['DESCUENTO GENERAL DEL CLIENTE', 'PROMO KSM'])
        self.assertEqual(first['explanation']['next_breaks'], [{'min_quantity': 10, 'unit_price': '12.75', 'rule_name': 'FILTROS VOLUMEN'}])
        self.assertEqual((second['unit_price'], second['status'], second['line_total']), (None, 'missing', None))
        # Without a client only general rules apply; after the promotion ends it no longer does.
        general_only = self.client.post(url, {'client_id': None, 'currency': 'USD', 'on_date': '2026-10-05', 'items': items[:1]}, format='json').data
        self.assertEqual((general_only['client'], general_only['profile'], general_only['lines'][0]['unit_price']), (None, None, '12.75'))
        self.assertEqual(self.client.post(url, {'currency': 'USD', 'on_date': '2026-11-01', 'items': items[:1]}, format='json').data['lines'][0]['unit_price'], '15.00')
        self.assertEqual(before, (PricingAuditEvent.objects.count(), PricingRule.objects.count(), ClientPricingProfile.objects.get().version))
        for body, expected in [({'client_id': str(self.other_client.pk), 'items': items}, 404), ({'items': [{'supplier_item_id': str(self.item_b.pk), 'quantity': 1}]}, 400),
                               ({'items': []}, 400), ({'items': items + items[:1]}, 400), ({'items': [{'supplier_item_id': str(self.item_a.pk), 'quantity': 0}]}, 400),
                               ({'items': [{'supplier_item_id': str(uuid.uuid4()), 'quantity': 1} for _ in range(51)]}, 400), ({'currency': 'EUR', 'items': items}, 400)]:
            with self.subTest(body=str(body)[:60]):
                self.assertEqual(self.client.post(url, body, format='json').status_code, expected)
        for user, account, expected in [(self.seller_b, self.supplier_a, 404), (self.root, self.supplier_a, 404), (self.buyer, self.client_account, 403)]:
            with self.subTest(user=user.username):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.post(f'{self.base(account)}/pricing/simulate/', {'items': items}, format='json').status_code, expected)
