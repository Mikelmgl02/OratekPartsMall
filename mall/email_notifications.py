"""Email notifications of the deal inbox, sent through Resend's API. A deal event of the other party (order sent, quotation published,
adjustment requested, deal confirmed…) is emailed right away; a message of the other party only to people who are not using the app at
that moment (no inbox poll in the last ACTIVE minutes), at most once per conversation every COOLDOWN minutes, so a live chat does not
flood their mailbox. Each person turns either kind off in the bell menu (InboxCursor.email_messages / email_updates).

Off until RESEND_API_KEY and EMAIL_FROM_ADDRESS are set. Like the browser pushes (mall.push), recipients are read once the deal's
transaction commits and the HTTP calls run in a thread, best effort: a failure is logged. Emails carry no prices or stock."""
import json
import logging
import urllib.request
from datetime import timedelta
from html import escape

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .inbox import cursor_for, event_title
from .inbox_models import ConversationRead, InboxCursor
from .models import DealEvent, User
from .push import dispatch, other_side

log = logging.getLogger(__name__)
RESEND_URL = 'https://api.resend.com/emails'
ACTIVE = timedelta(minutes=2)
COOLDOWN = timedelta(minutes=15)
QUOTED = 1000  # characters of the message quoted in its email


def email_enabled():
    return bool(settings.RESEND_API_KEY and settings.EMAIL_FROM_ADDRESS)


def from_header():
    return f'{settings.EMAIL_FROM_NAME} <{settings.EMAIL_FROM_ADDRESS}>' if settings.EMAIL_FROM_NAME else settings.EMAIL_FROM_ADDRESS


def link(order):
    return f'{settings.SITE_URL.rstrip("/")}/?conversacion={order.pk}'


def recipients(order, account_id, actor_id, preference):
    """The other party's people with an address who did not turn this kind of email off."""
    users = other_side(order, account_id, actor_id)
    off = InboxCursor.objects.filter(user_id__in=users, **{preference: False}).values('user_id')
    return list(User.objects.filter(pk__in=users, is_active=True).exclude(pk__in=off).exclude(email='').order_by('pk'))


def their_account(order, account_id, user=None):
    """The name of the account the recipients act for: the deal's party that did not act. An administrator's decision (no account)
    reaches both parties, so each recipient gets the party they belong to."""
    if account_id is None and user is not None:
        from .models import Membership
        return order.supplier_name if Membership.objects.filter(user=user, account_id=order.supplier_id).exists() else order.client_name
    return order.supplier_name if account_id == order.client_id else order.client_name


def compose(user, order, account, subject, lead, quote, action):
    """(subject, text, html) for one person: the lead sentence, an optional quoted text, and the button to the conversation."""
    name = user.first_name or user.username
    url = link(order)
    footer = ('Recibes este correo porque participas en esta orden en MotionPartes. Puedes desactivar estos avisos en la campana de '
              'notificaciones de MotionPartes.')
    text = '\n\n'.join(part for part in [f'Hola {name}:', lead, f'"{quote}"' if quote else '', f'{action}: {url}', f'Cuenta: {account}', footer] if part)
    quoted = (f'<blockquote style="margin:16px 0;padding:12px 16px;border-left:4px solid #8db35a;background:#f4f6f2;color:#232b26;'
              f'white-space:pre-wrap">{escape(quote)}</blockquote>') if quote else ''
    body = f'''<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#232b26;font-size:14px;line-height:1.6">
<p style="margin:0 0 6px;color:#44652d;font-weight:700;letter-spacing:.5px">MotionPartes</p>
<p>Hola {escape(name)}:</p>
<p>{escape(lead)}</p>{quoted}
<p style="margin:24px 0"><a href="{escape(url)}" style="display:inline-block;padding:11px 18px;border-radius:8px;background:#232b26;color:#ffffff;text-decoration:none;font-weight:700">{escape(action)}</a></p>
<p style="color:#768078;font-size:12px">Cuenta: {escape(account)}</p>
<p style="color:#768078;font-size:12px;border-top:1px solid #e1e5dd;padding-top:12px">{escape(footer)}</p>
</div>'''
    return subject, text, body


def deliver(emails):
    for to, subject, text, body in emails:
        data = json.dumps({'from': from_header(), 'to': [to], 'subject': subject, 'text': text, 'html': body}).encode()
        request = urllib.request.Request(RESEND_URL, data=data, method='POST', headers={
            'Authorization': f'Bearer {settings.RESEND_API_KEY}', 'Content-Type': 'application/json', 'User-Agent': 'MotionPartes/1.0'})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
        except Exception:  # one failed address must not stop the others
            log.exception('Email "%s" failed', subject)


def message_saved(sender, instance, created=False, raw=False, **kwargs):
    if not created or raw or not email_enabled():
        return
    message, order = instance, instance.order

    def collect():
        # Asking for an adjustment also writes its reason as a message: the adjustment's own email already quotes it.
        if DealEvent.objects.filter(order=order, actor_id=message.actor_id, kind='request_adjustment', description=message.body).exists():
            return
        now = timezone.now()
        using_app = set(InboxCursor.objects.filter(active_at__gte=now - ACTIVE).values_list('user_id', flat=True))
        emailed = set(ConversationRead.objects.filter(order=order, emailed_at__gte=now - COOLDOWN).values_list('user_id', flat=True))
        people = [user for user in recipients(order, message.account_id, message.actor_id, 'email_messages') if user.pk not in using_app | emailed]
        if not people:
            return
        for user in people:
            mark, _ = ConversationRead.objects.get_or_create(user=user, order=order)
            ConversationRead.objects.filter(pk=mark.pk).update(emailed_at=now)
        author = message.actor.get_full_name() or message.actor.username
        quote = message.body if len(message.body) <= QUOTED else f'{message.body[:QUOTED - 1]}…'
        account = their_account(order, message.account_id)
        emails = [(user.email, *compose(user, order, account, f'Nuevo mensaje de {message.account.name} · {order.reference}',
                                        f'{author} de {message.account.name} te escribió en la orden {order.reference}. Puede haber más '
                                        'mensajes en la conversación.', quote, 'Responder en MotionPartes')) for user in people]
        dispatch(lambda: deliver(emails))
    transaction.on_commit(collect)


def event_saved(sender, instance, created=False, raw=False, **kwargs):
    if not created or raw or not email_enabled():
        return
    event, order = instance, instance.order

    def collect():
        people = recipients(order, event.account_id, event.actor_id, 'email_updates')
        if not people:
            return
        title = event_title(event)
        emails = [(user.email, *compose(user, order, their_account(order, event.account_id, user), f'{title} · {order.reference}',
                                        f'{title} (orden {order.reference}).', event.description if event.kind == 'request_adjustment' else '',
                                        'Ver la orden')) for user in people]
        dispatch(lambda: deliver(emails))
    transaction.on_commit(collect)


class EmailPreferences(serializers.Serializer):
    messages = serializers.BooleanField(required=False)
    updates = serializers.BooleanField(required=False)


def preferences(user, cursor):
    return {'enabled': email_enabled(), 'address': user.email, 'messages': cursor.email_messages, 'updates': cursor.email_updates}


class InboxEmail(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='Si el servidor envía correos, a qué dirección, y qué avisos quiere la persona: '
                   'messages (mensajes nuevos cuando no está usando MotionPartes) y updates (cambios en sus órdenes).')
    def get(self, request):
        return Response(preferences(request.user, cursor_for(request.user)))

    @extend_schema(request=EmailPreferences, responses=OpenApiTypes.OBJECT, description='Activa o desactiva los correos de mensajes y de cambios.')
    def post(self, request):
        data = EmailPreferences(data=request.data)
        data.is_valid(raise_exception=True)
        cursor = cursor_for(request.user)
        changes = {field: data.validated_data[key] for key, field in [('messages', 'email_messages'), ('updates', 'email_updates')] if key in data.validated_data}
        if changes:
            InboxCursor.objects.filter(pk=cursor.pk).update(**changes)
            cursor.refresh_from_db()
        return Response(preferences(request.user, cursor))
