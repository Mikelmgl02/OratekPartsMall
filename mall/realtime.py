"""Realtime nudges for the deal inbox over a websocket (Django Channels: daphne serves /ws/inbox/, Redis carries messages between the
web workers and the socket server). The socket carries no deal content, only "deal X changed" (a message, an event, a read mark; with its conversation's id): the
page then fetches what changed through the usual API, which keeps every permission check in one place, and polls less while connected.
The socket signs in with the site's session cookie (the API token, SameSite=strict). Without REDIS_URL nothing is published and pages
keep polling as before."""
import logging
from http.cookies import CookieError, SimpleCookie

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from rest_framework.authtoken.models import Token

from .models import Membership
from . import push

log = logging.getLogger(__name__)
SESSION_COOKIE = 'partsmall_session'  # frontend/lib/server-api.ts
GROUP = 'inbox.user.{}'


def session_user(scope):
    """The active user whose API token is the session cookie of the handshake, or None."""
    header = dict(scope.get('headers') or []).get(b'cookie', b'').decode('latin-1')
    try:
        morsel = SimpleCookie(header).get(SESSION_COOKIE)
    except CookieError:
        return None
    token = Token.objects.select_related('user').filter(key=morsel.value).first() if morsel and morsel.value else None
    return token.user if token and token.user.is_active else None


class InboxSocket(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = await database_sync_to_async(session_user)(self.scope)
        if user is None:
            await self.close()  # before accept: the handshake is refused
            return
        self.group = GROUP.format(user.pk)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if getattr(self, 'group', None):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        pass  # the page only listens

    async def inbox_changed(self, event):
        await self.send_json({'kind': event['kind'], 'order_id': event.get('order_id'), 'thread_id': event.get('thread_id')})


def publish(user_ids, kind, order_id=None, thread_id=None):
    """Tells these people's open pages that something changed (an order, a conversation); best effort (Redis down only means they wait for
    their next poll)."""
    layer = get_channel_layer() if settings.CHANNEL_LAYERS else None
    if layer is None or not user_ids:
        return
    try:
        for user_id in user_ids:
            async_to_sync(layer.group_send)(GROUP.format(user_id), {'type': 'inbox.changed', 'kind': kind,
                                                                   'order_id': str(order_id) if order_id else None,
                                                                   'thread_id': str(thread_id) if thread_id else None})
    except Exception:
        log.warning('Realtime publish failed', exc_info=True)


def publish_later(user_ids, kind, order_id=None, thread_id=None):
    """After the commit, from a thread, so a slow Redis never holds the request."""
    if settings.CHANNEL_LAYERS and user_ids:
        transaction.on_commit(lambda: push.dispatch(lambda: publish(user_ids, kind, order_id, thread_id)))


def participants(deal):
    """Everyone who acts for either party of an order or a conversation (the deal-access rule), the author's other tabs included."""
    sides = Q(account_id=deal.client_id, account__roles__capability='client') | Q(account_id=deal.supplier_id, account__roles__capability='supplier')
    return set(Membership.objects.filter(sides, account__active=True, user__is_active=True).values_list('user_id', flat=True))


def message_saved(sender, instance, created=False, raw=False, **kwargs):
    if created and not raw and settings.CHANNEL_LAYERS:
        publish_later(participants(instance.thread), 'message', instance.order_id, instance.thread_id)


def event_saved(sender, instance, created=False, raw=False, **kwargs):
    if created and not raw and settings.CHANNEL_LAYERS:
        publish_later(participants(instance.order), 'event', instance.order_id, instance.order.thread_id)
