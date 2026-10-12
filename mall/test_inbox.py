import uuid

from rest_framework.test import APITestCase

from .inbox_models import InboxCursor
from .models import Membership
from .request_models import DealMessage, SupplierRequest
from . import test_deals as deal_tests


class InboxTests(APITestCase):
    setUp = deal_tests.DealWorkflowTests.setUp
    account = deal_tests.DealWorkflowTests.account
    item = deal_tests.DealWorkflowTests.item
    url = deal_tests.DealWorkflowTests.url
    payload = deal_tests.DealWorkflowTests.payload
    submit = deal_tests.DealWorkflowTests.submit
    review = deal_tests.DealWorkflowTests.review

    def order(self):
        self.client.force_authenticate(self.buyer)
        return deal_tests.DealWorkflowTests.order(self)

    def say(self, user, account, order, body):
        self.client.force_authenticate(user)
        response = self.client.post(f'/api/v1/accounts/{account.pk}/deals/{order.pk}/messages/', {'message_id': str(uuid.uuid4()), 'body': body},
                                    format='json')
        self.assertEqual(response.status_code, 200)
        return response.data['id']

    def inbox(self, user, path='', **params):
        self.client.force_authenticate(user)
        response = self.client.get(f'/api/v1/inbox/{path}', params)
        self.assertEqual(response.status_code, 200)
        return response.data

    def mark(self, user, path, payload):
        self.client.force_authenticate(user)
        return self.client.post(f'/api/v1/inbox/{path}', payload, format='json')

    def test_history_from_before_the_first_poll_is_not_new_and_own_messages_never_are(self):
        order = self.order()
        self.say(self.buyer, self.client_account, order, 'mensaje antiguo')
        self.assertEqual(self.inbox(self.seller_a), {'unread_messages': 0, 'unread_conversations': 0, 'unseen_notifications': 0,
                                                     'latest_message': None, 'latest_notification': None})
        self.inbox(self.buyer)
        latest = self.say(self.buyer, self.client_account, order, '¿Pueden entregar el lunes?')
        summary = self.inbox(self.seller_a)
        self.assertEqual((summary['unread_messages'], summary['unread_conversations']), (1, 1))
        self.assertEqual({key: summary['latest_message'][key] for key in ['id', 'body', 'reference', 'side', 'mine']},
                         {'id': latest, 'body': '¿Pueden entregar el lunes?', 'reference': order.reference, 'side': 'supplier', 'mine': False})
        self.assertEqual((summary['latest_message']['account']['name'], summary['latest_message']['counterpart']['name']), ('PROVEEDOR A', 'CLIENTE ORIGINAL'))
        self.assertEqual(self.inbox(self.buyer)['unread_messages'], 0)

    def test_conversations_list_each_deal_with_its_side_last_message_and_unread_count_until_read(self):
        order = self.order()
        self.inbox(self.seller_a)
        self.say(self.buyer, self.client_account, order, 'primero')
        second = self.say(self.buyer, self.client_account, order, 'segundo')
        [row] = self.inbox(self.seller_a, 'conversations/')['results']
        self.assertEqual({key: row[key] for key in ['id', 'reference', 'side', 'unread']}, {'id': str(order.thread_id), 'reference': order.reference, 'side': 'supplier', 'unread': 2})  # the conversation, on its order
        self.assertEqual((row['account']['id'], row['counterpart']['name']), (str(self.supplier_a.pk), 'CLIENTE ORIGINAL'))
        self.assertEqual((row['last_message']['id'], row['last_message']['body'], row['last_message']['mine']), (second, 'segundo', False))
        [mine] = self.inbox(self.buyer, 'conversations/')['results']
        self.assertEqual((mine['side'], mine['counterpart']['name'], mine['unread'], mine['last_message']['mine']), ('client', 'PROVEEDOR A', 0, True))

        response = self.mark(self.seller_a, f'conversations/{order.pk}/read/', {'last_message_id': second - 1})
        self.assertEqual((response.status_code, self.inbox(self.seller_a, 'conversations/')['results'][0]['unread']), (200, 1))
        self.assertEqual(self.mark(self.seller_a, f'conversations/{order.pk}/read/', {'last_message_id': second + 500}).data['last_message_id'], second)
        self.assertEqual(self.mark(self.seller_a, f'conversations/{order.pk}/read/', {'last_message_id': 0}).data['last_message_id'], second)  # never back
        self.assertEqual(self.inbox(self.seller_a)['unread_messages'], 0)
        self.assertEqual(self.inbox(self.seller_a, 'conversations/', unread='1')['results'], [])
        self.assertEqual(len(self.inbox(self.seller_a, 'conversations/', search=order.reference[-4:])['results']), 1)
        self.assertEqual(len(self.inbox(self.seller_a, 'conversations/', search='cliente original')['results']), 1)
        self.assertEqual(self.inbox(self.seller_a, 'conversations/', search='NADA')['results'], [])

    def test_the_most_recent_activity_lists_first(self):
        self.client.force_authenticate(self.buyer)
        response = self.submit()  # one order for each supplier
        orders = {row.supplier_id: row for row in SupplierRequest.objects.filter(pk__in=[item['id'] for item in response.data['requests']])}
        order_a, order_b = orders[self.supplier_a.pk], orders[self.supplier_b.pk]
        self.say(self.seller_b, self.supplier_b, order_b, 'b')
        self.assertEqual([row['id'] for row in self.inbox(self.buyer, 'conversations/')['results']], [str(order_b.thread_id), str(order_a.thread_id)])
        self.say(self.seller_a, self.supplier_a, order_a, 'a')
        self.assertEqual([row['id'] for row in self.inbox(self.buyer, 'conversations/')['results']], [str(order_a.thread_id), str(order_b.thread_id)])

    def test_people_pin_and_group_their_conversations_and_the_list_keeps_that_order(self):
        self.client.force_authenticate(self.buyer)
        response = self.submit()  # one order for each supplier
        orders = {row.supplier_id: row for row in SupplierRequest.objects.filter(pk__in=[item['id'] for item in response.data['requests']])}
        order_a, order_b = orders[self.supplier_a.pk], orders[self.supplier_b.pk]
        self.say(self.seller_b, self.supplier_b, order_b, 'b')  # the most recent
        listed = lambda: [(row['id'], row['pinned'], row['group']) for row in self.inbox(self.buyer, 'conversations/')['results']]
        created = self.mark(self.buyer, 'groups/', {'name': ' Urgentes '})
        self.assertEqual((created.status_code, created.data['name'], created.data['count']), (201, 'Urgentes', 0))
        group = created.data['id']
        self.assertEqual(self.mark(self.buyer, 'groups/', {'name': 'URGENTES'}).status_code, 400)  # names never repeat
        self.assertEqual(self.mark(self.buyer, 'groups/', {'name': '  '}).status_code, 400)
        self.assertEqual(self.mark(self.buyer, f'conversations/{order_a.pk}/place/', {'group': group}).data,
                         {'thread_id': str(order_a.thread_id), 'pinned': False, 'group': group})
        self.assertEqual(listed(), [(str(order_a.thread_id), False, group), (str(order_b.thread_id), False, None)])  # a group lists before Sin grupo
        self.assertEqual(self.inbox(self.buyer, 'conversations/')['groups'], [{'id': group, 'name': 'Urgentes', 'count': 1}])
        self.mark(self.buyer, f'conversations/{order_b.pk}/place/', {'pinned': True})
        self.assertEqual(listed(), [(str(order_b.thread_id), True, None), (str(order_a.thread_id), False, group)])  # Fijadas first
        self.mark(self.buyer, f'conversations/{order_a.pk}/place/', {'pinned': True})
        self.assertEqual(listed(), [(str(order_b.thread_id), True, None), (str(order_a.thread_id), True, group)])  # by activity, its group remembered
        self.assertEqual(self.inbox(self.buyer, 'conversations/')['groups'][0]['count'], 0)
        self.mark(self.buyer, f'conversations/{order_a.pk}/place/', {'pinned': False})
        self.assertEqual(listed(), [(str(order_b.thread_id), True, None), (str(order_a.thread_id), False, group)])  # back in its group
        self.assertEqual(self.mark(self.buyer, f'conversations/{order_a.pk}/place/', {}).status_code, 400)
        self.assertEqual(self.mark(self.buyer, f'groups/{group}/', {'name': 'Por cobrar'}).data, {'id': group, 'name': 'Por cobrar', 'count': 1})
        self.assertEqual(self.client.delete(f'/api/v1/inbox/groups/{group}/').status_code, 204)
        self.assertEqual(listed(), [(str(order_b.thread_id), True, None), (str(order_a.thread_id), False, None)])  # its conversations stay, ungrouped
        self.assertEqual(self.inbox(self.buyer, 'conversations/')['groups'], [])

    def test_groups_and_places_are_each_persons_own_and_never_move_the_read_mark(self):
        order = self.order()
        self.inbox(self.buyer)
        group = self.mark(self.buyer, 'groups/', {'name': 'Mía'}).data['id']
        self.assertEqual(self.mark(self.buyer, f'conversations/{order.pk}/place/', {'group': group, 'pinned': True}).status_code, 200)
        listing = self.inbox(self.seller_a, 'conversations/')
        self.assertEqual(([(row['pinned'], row['group']) for row in listing['results']], listing['groups']), ([(False, None)], []))
        self.assertEqual(self.mark(self.seller_a, f'conversations/{order.pk}/place/', {'group': group}).status_code, 404)
        self.assertEqual(self.mark(self.seller_a, f'groups/{group}/', {'name': 'Ajena'}).status_code, 404)
        self.assertEqual(self.client.delete(f'/api/v1/inbox/groups/{group}/').status_code, 404)
        self.assertEqual(self.mark(self.seller_b, f'conversations/{order.pk}/place/', {'pinned': True}).status_code, 404)  # not their deal
        self.say(self.seller_a, self.supplier_a, order, 'hola')
        self.mark(self.buyer, f'conversations/{order.pk}/place/', {'pinned': False})
        self.assertEqual(self.inbox(self.buyer)['unread_messages'], 1)
        for number in range(29):
            self.assertEqual(self.mark(self.buyer, 'groups/', {'name': f'Grupo {number}'}).status_code, 201)
        self.assertEqual(self.mark(self.buyer, 'groups/', {'name': 'Uno más'}).status_code, 400)  # 30 at most

    def test_someone_who_buys_and_sells_lists_purchases_and_sales_apart(self):
        Membership.objects.create(user=self.seller_a, account=self.client_account)  # also buys, for the client account
        self.client.force_authenticate(self.buyer)
        response = self.submit()  # one order for each supplier: a sale of supplier A, a purchase from supplier B
        orders = {row.supplier_id: row for row in SupplierRequest.objects.filter(pk__in=[item['id'] for item in response.data['requests']])}
        sale, purchase = orders[self.supplier_a.pk], orders[self.supplier_b.pk]
        self.inbox(self.seller_a)
        self.say(self.seller_b, self.supplier_b, purchase, 'hola')
        listing = self.inbox(self.seller_a, 'conversations/')
        self.assertEqual(listing['sides'], {'client': {'conversations': 1, 'unread': 1}, 'supplier': {'conversations': 1, 'unread': 0}})
        self.assertEqual([(row['id'], row['side']) for row in self.inbox(self.seller_a, 'conversations/', side='client')['results']], [(str(purchase.thread_id), 'client')])
        filtered = self.inbox(self.seller_a, 'conversations/', side='supplier')
        self.assertEqual(([row['id'] for row in filtered['results']], filtered['sides']['client']['conversations']), ([str(sale.thread_id)], 1))  # counts ignore the filter
        self.assertEqual(len(self.inbox(self.seller_a, 'conversations/', side='nada')['results']), 2)
        self.assertEqual(self.inbox(self.buyer, 'conversations/')['sides']['supplier'], {'conversations': 0, 'unread': 0})  # only buys

    def test_notifications_list_what_the_other_party_did_until_seen(self):
        order = self.order()
        self.inbox(self.buyer)
        self.inbox(self.seller_a)
        self.review(order)
        summary = self.inbox(self.buyer)
        self.assertEqual(summary['unseen_notifications'], 1)
        self.assertEqual({key: summary['latest_notification'][key] for key in ['kind', 'title', 'reference', 'side', 'unseen']},
                         {'kind': 'review_started', 'title': 'PROVEEDOR A empezó a revisar tu orden', 'reference': order.reference, 'side': 'client', 'unseen': True})
        self.assertEqual(self.inbox(self.seller_a)['unseen_notifications'], 0)  # their own action
        seller_list = self.inbox(self.seller_a, 'notifications/')['results']
        self.assertEqual([(row['kind'], row['unseen']) for row in seller_list], [('order_sent', False)])  # sent before their first poll
        self.assertEqual(seller_list[0]['title'], 'CLIENTE ORIGINAL te envió una orden')

        [row] = self.inbox(self.buyer, 'notifications/')['results']
        self.assertEqual(self.mark(self.buyer, 'notifications/seen/', {'last_event_id': row['id']}).data['seen_event_id'], row['id'])
        self.assertEqual(self.inbox(self.buyer)['unseen_notifications'], 0)
        self.assertEqual(self.inbox(self.buyer, 'notifications/')['results'][0]['unseen'], False)
        self.assertEqual(self.mark(self.buyer, 'notifications/seen/', {'last_event_id': 0}).data['seen_event_id'], row['id'])  # never back
        self.assertEqual(self.inbox(self.buyer, 'notifications/', before=row['id'])['results'], [])

    def test_people_outside_the_deal_see_nothing_and_cannot_mark_it(self):
        order = self.order()
        self.say(self.buyer, self.client_account, order, 'privado')
        for user in [self.seller_b, self.root]:
            self.assertEqual(self.inbox(user, 'conversations/')['results'], [])
            self.assertEqual(self.inbox(user, 'notifications/')['results'], [])
            self.assertEqual(self.mark(user, f'conversations/{order.pk}/read/', {'last_message_id': 1}).status_code, 404)
        self.supplier_a.roles.clear()  # an account without the supplier capability no longer acts for the deal
        self.assertEqual(self.inbox(self.seller_a, 'conversations/')['results'], [])
        self.client.force_authenticate(None)
        self.assertIn(self.client.get('/api/v1/inbox/').status_code, (401, 403))

    def test_a_new_person_starts_with_the_inbox_caught_up(self):
        order = self.order()
        message = self.say(self.buyer, self.client_account, order, 'hola')
        self.inbox(self.seller_a)
        cursor = InboxCursor.objects.get(user=self.seller_a)
        self.assertEqual(cursor.message_floor, DealMessage.objects.latest('id').pk)
        self.assertEqual(cursor.message_floor, message)

    def test_orders_with_the_same_supplier_are_one_conversation_with_each_message_tagged(self):
        self.client.force_authenticate(self.buyer)
        first = SupplierRequest.objects.get(pk=self.submit(self.payload([(self.item_a, 1)])).data['requests'][0]['id'])
        self.review(first)  # reviewed: the next order to PROVEEDOR A is another order
        self.client.force_authenticate(self.buyer)
        second = SupplierRequest.objects.get(pk=self.submit(self.payload([(self.item_a2, 2)])).data['requests'][0]['id'])
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.thread_id, second.thread_id)  # one conversation per client and supplier
        self.inbox(self.seller_a)
        self.say(self.buyer, self.client_account, first, 'sobre la primera')
        self.say(self.buyer, self.client_account, second, 'sobre la segunda')
        [row] = self.inbox(self.seller_a, 'conversations/')['results']
        self.assertEqual((row['id'], row['unread'], row['order_count'], row['open_orders']), (str(first.thread_id), 2, 2, 2))
        self.assertEqual({order['reference'] for order in row['orders']}, {first.reference, second.reference})
        self.assertEqual((row['last_message']['body'], row['last_message']['reference']), ('sobre la segunda', second.reference))
        self.assertEqual(self.inbox(self.seller_a)['unread_conversations'], 1)
        self.client.force_authenticate(self.seller_a)
        chat = self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/deals/{first.pk}/messages/').data
        self.assertEqual([(message['body'], message['order_reference']) for message in chat['results']],
                         [('sobre la primera', first.reference), ('sobre la segunda', second.reference)])  # the whole conversation from either order
        self.assertEqual(chat['thread_id'], str(first.thread_id))
        solo = self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/deals/{first.pk}/messages/', {'solo': '1'}).data['results']
        self.assertEqual([message['body'] for message in solo], ['sobre la primera'])
        general = self.client.post(f'/api/v1/accounts/{self.supplier_a.pk}/deals/{first.pk}/messages/',
                                   {'message_id': str(uuid.uuid4()), 'body': '¿Algo más?', 'general': True}, format='json').data
        self.assertEqual((general['order_id'], general['order_reference']), (None, None))  # about the relationship, no order
        self.assertEqual(self.mark(self.seller_a, f'conversations/{second.pk}/read/', {'last_message_id': 10 ** 9}).data['thread_id'], str(first.thread_id))
        self.assertEqual(self.inbox(self.seller_a)['unread_messages'], 0)  # read from either order, the conversation is read
        self.assertEqual(len(self.inbox(self.buyer, 'conversations/', search=second.reference)['results']), 1)
        self.assertEqual(self.inbox(self.buyer, 'conversations/', order=str(second.pk))['results'][0]['id'], str(first.thread_id))
