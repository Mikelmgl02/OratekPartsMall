"""Browser push notifications (Web Push, VAPID) for the deal inbox: when one party writes or acts on a deal, every browser where a person
of the other party turned notifications on gets one, even with the site closed. Off until VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY and
VAPID_SUBJECT are set (python -m mall.vapid_keys prints a new pair).

The recipients and the payload are read once the deal's transaction commits; the HTTP calls to the push services then run in a thread,
so a slow service never holds the request. Delivery is best effort: a failure is logged, and an endpoint the service no longer knows
(404/410) is dropped. The page itself shows nothing extra: the service worker skips a push while the site is in front, where the inbox
toast already announced it."""
import json
import logging
import threading

from django.conf import settings
from django.db import connection, transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .inbox import event_title, snippet
from .inbox_models import PushSubscription
from .models import Account, Membership

log = logging.getLogger(__name__)
TTL = 24 * 3600  # a push nobody could receive within a day is no longer news
GONE = (404, 410)


def push_enabled():
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY and settings.VAPID_SUBJECT)


def dispatch(job):
    def run():
        try:
            job()
        finally:
            connection.close()  # the thread's own connection
    threading.Thread(target=run, daemon=True).start()


def other_side(order, account_id, actor_id):
    """The people who act for the party that did not act: active members of that account, which must hold that side's capability."""
    other, capability = (order.supplier_id, 'supplier') if account_id == order.client_id else (order.client_id, 'client')
    if not Account.objects.filter(pk=other, active=True, roles__capability=capability).exists():
        return []
    return set(Membership.objects.filter(account_id=other, user__is_active=True).exclude(user_id=actor_id).values_list('user_id', flat=True))


def payload(order, title, body):
    return {'title': title, 'body': body, 'tag': f'deal-{order.pk}', 'url': f'/?conversacion={order.pk}', 'order_id': str(order.pk)}


def send(subscriptions, data):
    from pywebpush import WebPushException, webpush
    gone = []
    for row in subscriptions:
        try:
            webpush({'endpoint': row['endpoint'], 'keys': {'p256dh': row['p256dh'], 'auth': row['auth']}}, json.dumps(data),
                    vapid_private_key=settings.VAPID_PRIVATE_KEY, vapid_claims={'sub': settings.VAPID_SUBJECT}, ttl=TTL, timeout=10)
        except WebPushException as error:
            status = getattr(error.response, 'status_code', None)
            if status in GONE:
                gone.append(row['pk'])
            else:
                log.warning('Push to subscription %s failed: %s', row['pk'], status or error)
        except Exception:  # a network error to one push service must not stop the others
            log.exception('Push to subscription %s failed', row['pk'])
    if gone:
        PushSubscription.objects.filter(pk__in=gone).delete()


def notify_later(order, account_id, actor_id, build):
    """After the commit: the other party's browsers, then the push (build gives title and body) in a thread."""
    def collect():
        users = other_side(order, account_id, actor_id)
        subscriptions = list(PushSubscription.objects.filter(user_id__in=users).values('pk', 'endpoint', 'p256dh', 'auth'))
        if subscriptions:
            data = payload(order, *build())
            dispatch(lambda: send(subscriptions, data))
    transaction.on_commit(collect)


def message_saved(sender, instance, created=False, raw=False, **kwargs):
    if created and not raw and push_enabled():
        notify_later(instance.order, instance.account_id, instance.actor_id,
                     lambda: (f'{instance.account.name} · {instance.order.reference}', snippet(instance.body)))


def event_saved(sender, instance, created=False, raw=False, **kwargs):
    if created and not raw and push_enabled():
        notify_later(instance.order, instance.account_id, instance.actor_id, lambda: (event_title(instance), f'Orden {instance.order.reference}'))


class Subscription(serializers.Serializer):
    endpoint = serializers.URLField(max_length=1000)
    keys = serializers.DictField(child=serializers.CharField(max_length=200))

    def validate_endpoint(self, value):
        if not value.startswith('https://'):
            raise serializers.ValidationError('La suscripción del navegador no es válida.')
        return value

    def validate_keys(self, value):
        if not value.get('p256dh') or not value.get('auth'):
            raise serializers.ValidationError('La suscripción del navegador no es válida.')
        return value


class Endpoint(serializers.Serializer):
    endpoint = serializers.URLField(max_length=1000)


class InboxPush(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='Si el servidor envía notificaciones del navegador, y la clave pública VAPID '
                   'con la que el navegador se suscribe.')
    def get(self, request):
        enabled = push_enabled()
        return Response({'enabled': enabled, 'public_key': settings.VAPID_PUBLIC_KEY if enabled else ''})

    @extend_schema(request=Subscription, responses=OpenApiTypes.OBJECT, description='Guarda la suscripción del navegador para la persona '
                   '(PushSubscription.toJSON()). Un navegador ya suscrito por otra persona pasa a esta.')
    def post(self, request):
        if not push_enabled():
            return Response({'detail': 'Las notificaciones del navegador no están configuradas.'}, status=409)
        data = Subscription(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        PushSubscription.objects.update_or_create(endpoint=values['endpoint'], defaults={
            'user': request.user, 'p256dh': values['keys']['p256dh'], 'auth': values['keys']['auth']})
        return Response({'subscribed': True})


class InboxPushRemove(APIView):
    @extend_schema(request=Endpoint, responses=OpenApiTypes.OBJECT, description='Deja de enviar notificaciones a este navegador (al '
                   'desactivarlas o al cerrar sesión).')
    def post(self, request):
        data = Endpoint(data=request.data)
        data.is_valid(raise_exception=True)
        PushSubscription.objects.filter(user=request.user, endpoint=data.validated_data['endpoint']).delete()
        return Response({'subscribed': False})
