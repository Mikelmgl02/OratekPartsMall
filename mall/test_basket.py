import uuid

from rest_framework.test import APITestCase

from .basket_models import BasketEvent, BasketItem
from .catalog_availability import stock_band
from .models import Membership, Role, User
from .request_models import SupplierRequestLine
from .test_factories import make_client, make_line
from . import test_requests as request_tests


class BasketTests(APITestCase):
    """The cesta on the server: one per person and client account, the same on every device; only requestable articles go in, and
    sending a solicitud takes the sent units out."""
    setUp = request_tests.SupplierRequestWorkflowTests.setUp
    account = request_tests.SupplierRequestWorkflowTests.account
    item = request_tests.SupplierRequestWorkflowTests.item
    payload = request_tests.SupplierRequestWorkflowTests.payload
    submit = request_tests.SupplierRequestWorkflowTests.submit
    url = request_tests.SupplierRequestWorkflowTests.url
    merge_part = request_tests.SupplierRequestWorkflowTests.merge_part

    def basket(self, account=None):
        return f'/api/v1/accounts/{(account or self.client_account).pk}/basket/'

    def change(self, *changes, account=None):
        return self.client.post(self.basket(account), {'changes': [{'supplier_item_id': str(item.pk), 'quantity': quantity, 'mode': mode,
                                                                    **extra} for item, quantity, mode, *rest in changes for extra in [rest[0] if rest else {}]]},
                                format='json')

    def quantities(self, response):
        return {(line['offer']['supplier_name'], line['offer']['selected_item']['codigo']): line['quantity'] for line in response.data['lines']}

    def test_articles_are_added_set_and_taken_out_and_the_cesta_lists_them_as_the_catalog_does(self):
        self.assertEqual(self.client.get(self.basket()).data, {'lines': [], 'count': 0})
        response = self.change((self.item_a, 2, 'add', {'expected_part_id': str(self.part.pk)}), (self.item_b, 1, 'add'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.quantities(response), {('PROVEEDOR A', '58411-1R000-G'): 2, ('PROVEEDOR B', '58411-1R000-G'): 1})
        first = next(line for line in response.data['lines'] if line['offer']['supplier_id'] == str(self.supplier_a.pk))
        self.assertEqual(first['key'], f'{self.part.pk}:{self.supplier_a.pk}:{self.item_a.pk}')
        self.assertEqual(first['offer'], {'supplier_id': str(self.supplier_a.pk), 'supplier_name': 'PROVEEDOR A', 'in_stock': True,
                                          'stock': stock_band(self.item_a.available_quantity),
                                          'selected_item': {'id': str(self.item_a.pk), 'codigo': '58411-1R000-G', 'brand': 'KSM', 'description': 'TAMBOR A-001'}})
        self.assertEqual((first['part']['sku'], first['quantity'], first['source'], first['available'], first['changed']),
                         ('58411-1R000', 2, 'web', True, False))
        self.assertIn('codes', first['part'])
        self.assertEqual(self.change((self.item_a, 3, 'add')).data['lines'][0]['quantity'], 5)  # adds up
        self.assertEqual(self.change((self.item_a, 2, 'max')).data['lines'][0]['quantity'], 5)  # keeps the larger
        self.assertEqual(self.change((self.item_a, 9000, 'add')).data['lines'][0]['quantity'], 9005)
        self.assertEqual(self.change((self.item_a, 5000, 'add')).data['lines'][0]['quantity'], 9999)  # never above 9.999
        self.assertEqual(self.change((self.item_a, 4, 'set')).data['lines'][0]['quantity'], 4)
        response = self.change((self.item_a, 0, 'set'))
        self.assertEqual([line['offer']['supplier_id'] for line in response.data['lines']], [str(self.supplier_b.pk)])
        self.assertEqual(response.data['count'], 1)

    def test_each_person_and_account_has_its_own_and_only_client_members_reach_it(self):
        self.change((self.item_a, 2, 'add'))
        colleague = User.objects.create_user('request-colleague', 'colleague@example.invalid', 'Request938!')
        Membership.objects.create(user=colleague, account=self.client_account)
        self.client.force_authenticate(colleague)
        self.assertEqual(self.client.get(self.basket()).data['count'], 0)  # a colleague's cesta is their own
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.get(self.basket(self.supplier_a)).status_code, 403)  # not a client account
        self.assertEqual(self.client.get(self.basket()).status_code, 404)  # not a member
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.basket()).status_code, 401)

    def test_only_requestable_articles_go_in_never_the_accounts_own(self):
        self.client_account.roles.add(Role.objects.get(code='supplier_retail'))  # it also sells
        own = self.item(self.client_account, self.buyer, 'C-001', '58411-1R000-G', 5)
        response = self.change((own, 1, 'add'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('es de tu propia cuenta', str(response.data))
        self.item_b.matching_status = 'review'
        self.item_b.save()
        response = self.change((self.item_a, 1, 'add'), (self.item_b, 1, 'add'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('ya no está disponible', str(response.data))
        self.assertFalse(BasketItem.objects.exists())  # all or none
        response = self.change((self.item_a, 1, 'add', {'expected_part_id': str(uuid.uuid4())}))
        self.assertEqual(response.status_code, 409)  # chosen under another SKU

    def test_a_line_says_when_its_article_changed_or_can_no_longer_be_asked_for(self):
        self.change((self.item_a, 1, 'add'), (self.item_b, 1, 'add'))
        self.item_a.codigo = '58411-1R000-X'
        self.item_a.save()
        self.supplier_b.active = False
        self.supplier_b.save()
        lines = {line['offer']['supplier_id']: line for line in self.client.get(self.basket()).data['lines']}
        self.assertEqual((lines[str(self.supplier_a.pk)]['changed'], lines[str(self.supplier_a.pk)]['available']), (True, True))
        self.assertEqual(lines[str(self.supplier_a.pk)]['offer']['selected_item']['codigo'], '58411-1R000-G')  # as it was added
        self.assertEqual((lines[str(self.supplier_b.pk)]['changed'], lines[str(self.supplier_b.pk)]['available']), (False, False))

    def test_adding_a_changed_article_again_from_the_catalog_brings_its_line_up_to_date(self):
        self.change((self.item_a, 2, 'add', {'expected_part_id': str(self.part.pk)}))
        self.item_a.codigo, self.item_a.brand = '58411-1R000-X', 'OTRA'
        self.item_a.save()
        self.assertTrue(self.client.get(self.basket()).data['lines'][0]['changed'])
        [line] = self.change((self.item_a, 1, 'add', {'expected_part_id': str(self.part.pk)})).data['lines']
        self.assertEqual((line['quantity'], line['changed'], line['offer']['selected_item']['codigo'], line['offer']['selected_item']['brand']),
                         (3, False, '58411-1R000-X', 'OTRA'))
        self.assertEqual(self.change((self.item_a, 1, 'add', {'expected_part_id': str(uuid.uuid4())})).status_code, 409)

    def test_a_merge_keeps_the_cesta_line_and_the_sku_an_article_was_chosen_under(self):
        self.change((self.item_a, 2, 'add', {'expected_part_id': str(self.part.pk)}))
        self.merge_part()
        [line] = self.client.get(self.basket()).data['lines']
        self.assertEqual((line['changed'], line['available']), (False, True))  # its article moved with the SKU it was added under
        response = self.change((self.item_a, 1, 'add', {'expected_part_id': str(self.part.pk)}),  # an old page: the merged SKU
                               (self.item_b, 1, 'add', {'expected_part_id': str(self.part.pk)}))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.quantities(response), {('PROVEEDOR A', '58411-1R000-G'): 3, ('PROVEEDOR B', '58411-1R000-G'): 1})
        other = make_line('OTRO-SKU')
        self.assertEqual(self.change((self.item_a2, 1, 'add', {'expected_part_id': str(other.pk)})).status_code, 409)

    def test_en_cesta_lists_this_persons_cesta_for_this_account_under_the_sku_the_catalog_lists(self):
        def listed(account=None):
            return [part['id'] for part in self.client.get(f'/api/v1/catalog/?account={(account or self.client_account).pk}&mine=cesta').data['results']]
        self.assertEqual(listed(), [])
        colleague = User.objects.create_user('cesta-colleague', 'cesta-colleague@example.invalid', 'Request938!')
        Membership.objects.create(user=colleague, account=self.client_account)
        self.client.force_authenticate(colleague)
        self.assertEqual(self.change((self.item_a, 2, 'add')).status_code, 200)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(listed(), [])  # a colleague's cesta is theirs
        other = make_client('OTRO CLIENTE', self.buyer)
        self.assertEqual(self.change((self.item_a, 1, 'add'), account=other).status_code, 200)
        self.assertEqual((listed(), listed(other)), ([], [str(self.part.pk)]))  # and each account's its own
        self.change((self.item_a, 2, 'add'))
        self.assertEqual(listed(), [str(self.part.pk)])
        target = self.merge_part()
        self.assertEqual(listed(), [str(target.pk)])  # after a merge, under the SKU the catalog lists
        [line] = self.client.get(self.basket()).data['lines']
        self.assertEqual((line['part']['id'], line['catalog_part_id'], line['listed']), (str(self.part.pk), str(target.pk), True))  # the badge's SKU
        target.estado = 'retirada'
        target.save()
        [line] = self.client.get(self.basket()).data['lines']
        self.assertEqual((line['listed'], listed()), (False, []))  # out of the catalog: En cesta neither lists nor counts it

    def test_sending_takes_the_sent_units_out_and_leaves_what_was_added_since(self):
        self.change((self.item_a, 3, 'add'), (self.item_a2, 2, 'add'), (self.item_b, 6, 'add'))
        key = uuid.uuid4()
        sent = self.payload([(self.item_a, 3), (self.item_b, 4)], key=key, expected=True)  # item_b: 2 more were added meanwhile
        self.assertEqual(self.submit(sent).status_code, 200)
        self.assertEqual(self.quantities(self.client.get(self.basket())), {('PROVEEDOR A', '58411-1R000-KSM'): 2, ('PROVEEDOR B', '58411-1R000-G'): 2})
        self.assertEqual(self.submit(sent).status_code, 200)  # the same send retried: nothing more leaves
        self.assertEqual(self.client.get(self.basket()).data['count'], 2)

    def test_every_change_is_in_the_ledger_and_only_its_owner_reads_it(self):
        self.change((self.item_a, 2, 'add'))
        self.change((self.item_a, 3, 'add'), (self.item_b, 4, 'max'))  # 2 → 5, and a browser's cesta moving in
        self.change((self.item_a, 5, 'set'))  # the same quantity: nothing to record
        self.change((self.item_a2, 1, 'add'))
        self.change((self.item_a2, 0, 'set'))
        sent = self.payload([(self.item_a, 5), (self.item_b, 1)], expected=True)
        self.assertEqual(self.submit(sent).status_code, 200)
        reference = {line.supplier_item_id: line.request.reference for line in SupplierRequestLine.objects.select_related('request')}
        activity = self.client.get(f'{self.basket()}activity/').data
        self.assertFalse(activity['has_more'])
        rows = [(entry['kind'], entry['codigo'], entry['supplier_name'], entry['quantity_before'], entry['quantity_after'], entry['reference'])
                for entry in activity['results']]
        self.assertEqual(set(rows[:2]), {('sent', '58411-1R000-G', 'PROVEEDOR B', 4, 3, reference[self.item_b.pk]),  # one send, in any order
                                         ('sent', '58411-1R000-G', 'PROVEEDOR A', 5, 0, reference[self.item_a.pk])})
        self.assertEqual(rows[2:], [
            ('removed', '58411-1R000-KSM', 'PROVEEDOR A', 1, 0, ''),
            ('added', '58411-1R000-KSM', 'PROVEEDOR A', 0, 1, ''),
            ('moved_in', '58411-1R000-G', 'PROVEEDOR B', 0, 4, ''),
            ('changed', '58411-1R000-G', 'PROVEEDOR A', 2, 5, ''),
            ('added', '58411-1R000-G', 'PROVEEDOR A', 0, 2, '')])
        self.assertEqual({entry['sku'] for entry in activity['results']}, {'58411-1R000'})
        self.assertEqual(set(activity['results'][0]), {'id', 'kind', 'sku', 'codigo', 'brand', 'supplier_name', 'quantity_before', 'quantity_after',
                                                       'source', 'reference', 'created_at'})
        older = self.client.get(f'{self.basket()}activity/', {'before': activity['results'][4]['id']}).data['results']
        self.assertEqual([entry['kind'] for entry in older], ['changed', 'added'])
        self.assertEqual(self.client.get(f'{self.basket()}activity/', {'before': 'x'}).status_code, 400)
        colleague = User.objects.create_user('request-colleague', 'colleague@example.invalid', 'Request938!')
        Membership.objects.create(user=colleague, account=self.client_account)
        self.client.force_authenticate(colleague)
        self.assertEqual(self.client.get(f'{self.basket()}activity/').data['results'], [])  # a colleague reads their own only
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.get(f'{self.basket()}activity/').status_code, 404)
        self.assertEqual(BasketEvent.objects.count(), 7)

    def test_the_admin_stats_count_cestas_from_the_ledger_whatsapp_included(self):
        from .basket import apply_changes
        self.change((self.item_a, 2, 'add'))
        apply_changes(self.buyer, self.client_account, [{'supplier_item_id': self.item_b.pk, 'quantity': 3, 'mode': 'add'}], source='whatsapp')
        self.change((self.item_a, 4, 'set'))
        self.change((self.item_b, 0, 'set'))
        self.assertEqual(self.submit(self.payload([(self.item_a, 1)], expected=True)).status_code, 200)
        self.client.force_authenticate(self.root)
        baskets = self.client.get('/api/v1/management/analytics/', {'days': 7}).data['baskets']
        self.assertEqual(baskets, {'added': 2, 'added_whatsapp': 1, 'added_units': 5, 'changed': 1, 'removed': 1, 'sent': 1, 'sent_units': 1,
                                   'open_cestas': 1, 'idle_cestas': 0, 'open_lines': 1})
