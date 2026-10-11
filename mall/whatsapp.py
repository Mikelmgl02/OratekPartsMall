"""WhatsApp notices of the deal inbox through Meta's WhatsApp Cloud API: the third channel, next to the in-app inbox (with browser push)
and email. One MotionPartes number notifies every supplier's staff and every client with two approved Utility templates whose
"Ver en MotionPartes" button links to the deal, so the conversation goes on in the site:

- WHATSAPP_TEMPLATE_UPDATE (motionpartes_novedad_orden): "Novedad en tu orden {{1}}: {{2}}. Ábrela en MotionPartes para ver el detalle."
  with the order reference and the bell's title (GRUPO RAZOR PARTS publicó la cotización v2), sent right away for each deal event of
  the other party;
- WHATSAPP_TEMPLATE_MESSAGE (motionpartes_mensaje_nuevo): "Tienes un mensaje nuevo de {{1}} sobre la orden {{2}}: «{{3}}». Responde desde
  MotionPartes." with the account, the reference and the start of the message, sent only to people away from the app, at most once per
  conversation every COOLDOWN.
(Meta refuses a template body that starts or ends with a variable.)
Both buttons: URL https://<site>/?conversacion={{1}}, filled with the deal id and the recipient's sign-in hint (mall.login_hints).

When Meta refuses a template as unusable (missing or still under review, paused, disabled), the notice goes as plain text with the same
words and the link; WhatsApp delivers such a text only within 24 hours of the person's last message to the number.

Nobody gets anything without opting in (number and consent in the bell menu); replying BAJA or STOP opts out, and other replies get a
short answer pointing back to the site. Off until WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID are set. Sent after the commit from
a thread, best effort; every send is logged (WhatsAppMessage) and Meta's webhook keeps its status."""
import hashlib
import hmac
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .deal_attachments import describe
from . import push
from .email_notifications import ACTIVE
from .inbox import cursor_for, event_title
from . import login_hints
from .inbox_models import AccountWhatsApp, ConversationRead, InboxCursor, WhatsAppMessage
from .models import DealEvent, DealMessage

log = logging.getLogger(__name__)
GRAPH = 'https://graph.facebook.com/{version}/{phone}/messages'
COOLDOWN = timedelta(minutes=30)  # each WhatsApp costs: fewer than emails per conversation
REPLY_EVERY = timedelta(hours=12)
OPT_OUT = {'BAJA', 'STOP', 'SALIR', 'CANCELAR', 'NO MAS', 'NO MÁS'}
NUMBER = re.compile(r'^\+[1-9]\d{7,14}$')
PROGRESS = ['queued', 'accepted', 'sent', 'delivered', 'read']  # a late webhook never moves a status back


def whatsapp_enabled():
    return bool(settings.WHATSAPP_ACCESS_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID)


def normalize_number(raw):
    """+507 6123-4567, 0050761234567 and 61234567 (local, WHATSAPP_DEFAULT_COUNTRY_CODE) -> +50761234567; None when it is not a number."""
    digits = re.sub(r'[\s\-().]', '', raw or '')
    if digits.startswith('00'):
        digits = f'+{digits[2:]}'
    if digits and not digits.startswith('+'):
        digits = f'+{settings.WHATSAPP_DEFAULT_COUNTRY_CODE}{digits}' if len(digits) <= 8 else f'+{digits}'
    return digits if NUMBER.match(digits) else None


def param(text, limit):
    """A template parameter: Meta refuses newlines, tabs and runs of spaces, and empty values."""
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    return (text if len(text) <= limit else f'{text[:limit - 1]}…') or '—'


def graph(payload):
    request = urllib.request.Request(GRAPH.format(version=settings.WHATSAPP_API_VERSION, phone=settings.WHATSAPP_PHONE_NUMBER_ID),
                                     data=json.dumps(payload).encode(), method='POST',
                                     headers={'Authorization': f'Bearer {settings.WHATSAPP_ACCESS_TOKEN}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


# The two templates' bodies as Meta approves them (python -m mall.whatsapp_templates creates them from these), and the plain-text fallback.
BODIES = {'update': 'Novedad en tu orden {{1}}: {{2}}. Ábrela en MotionPartes para ver el detalle.',
          'message': 'Tienes un mensaje nuevo de {{1}} sobre la orden {{2}}: «{{3}}». Responde desde MotionPartes.'}
# Meta's errors for a template that cannot be used (does not exist or awaits review, wrong format, too long, paused, disabled).
TEMPLATE_UNUSABLE = {132000, 132001, 132005, 132007, 132012, 132015, 132016}


def body_of(name):
    return {settings.WHATSAPP_TEMPLATE_UPDATE: BODIES['update'], settings.WHATSAPP_TEMPLATE_MESSAGE: BODIES['message']}.get(name)


def as_text(payload):
    """A template notice as a plain text message: the template's words with its values, and the link of its button."""
    notice = payload['template']
    values = [item['text'] for item in notice['components'][0]['parameters']]
    conversation = notice['components'][1]['parameters'][0]['text']  # the deal and the recipient's sign-in hint
    text = re.sub(r'\{\{(\d+)\}\}', lambda match: values[int(match.group(1)) - 1], body_of(notice['name']))
    link = f"{settings.SITE_URL.rstrip('/')}/?conversacion={conversation}"
    return {'messaging_product': 'whatsapp', 'to': payload['to'], 'type': 'text', 'text': {'preview_url': True, 'body': f'{text}\n\nVer en MotionPartes: {link}'}}


def meta_code(detail):
    try:
        return json.loads(detail).get('error', {}).get('code')
    except (ValueError, AttributeError):
        return None


def template(number, name, body, conversation):
    return {'messaging_product': 'whatsapp', 'to': number.lstrip('+'), 'type': 'template', 'template': {
        'name': name, 'language': {'code': settings.WHATSAPP_TEMPLATE_LANGUAGE}, 'components': [
            {'type': 'body', 'parameters': [{'type': 'text', 'text': value} for value in body]},
            {'type': 'button', 'sub_type': 'url', 'index': '0', 'parameters': [{'type': 'text', 'text': str(conversation)}]}]}}


def accepted(log_id, result, **extra):
    WhatsAppMessage.objects.filter(pk=log_id).update(status='accepted', wamid=(result.get('messages') or [{}])[0].get('id', ''),
                                                     updated_at=timezone.now(), **extra)


def fallback(log_id, payload, code):
    """The template cannot be used: the same notice as plain text, only within the number's 24-hour window (WhatsApp drops it otherwise)."""
    from .whatsapp_bridge import window_open
    name = payload['template']['name']
    if not window_open(f"+{payload['to']}"):
        WhatsAppMessage.objects.filter(pk=log_id).update(status='failed', error=f'Plantilla no disponible ({code}); fuera de la ventana de 24 horas, '
                                                         'no se envió como texto', updated_at=timezone.now())
        return
    try:
        accepted(log_id, graph(as_text(payload)), template=f'{name} (texto)'[:80], error=f'Plantilla no disponible ({code}); enviado como texto')
        log.info('WhatsApp notice %s sent as text: template %s unusable (%s)', log_id, name, code)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors='replace')[:200]
        WhatsAppMessage.objects.filter(pk=log_id).update(status='failed', error=f'Plantilla no disponible ({code}); texto: {error.code} {detail}'[:300],
                                                         updated_at=timezone.now())
        log.warning('WhatsApp notice %s refused as text too: %s %s', log_id, error.code, detail)


def deliver(jobs):
    from .whatsapp_bridge import send_file
    uploaded = {}  # chat files already uploaded to Meta in this delivery
    for log_id, payload in jobs:
        try:
            accepted(log_id, send_file(payload, uploaded) if '_attachment' in payload else graph(payload))
        except urllib.error.HTTPError as error:  # Meta explains in the body (template not approved, number not on WhatsApp…)
            detail = error.read().decode(errors='replace')[:300]
            code = meta_code(detail)
            if code in TEMPLATE_UNUSABLE and payload.get('type') == 'template' and body_of(payload['template']['name']):
                fallback(log_id, payload, code)
                continue
            WhatsAppMessage.objects.filter(pk=log_id).update(status='failed', error=f'{error.code} {detail}'[:300], updated_at=timezone.now())
            log.warning('WhatsApp notice %s refused: %s %s', log_id, error.code, detail)
        except Exception as error:  # one failure must not stop the others
            WhatsAppMessage.objects.filter(pk=log_id).update(status='failed', error=str(error)[:300], updated_at=timezone.now())
            log.exception('WhatsApp notice %s failed', log_id)


@dataclass
class Target:
    """Where a notice goes: a person's number (with their InboxCursor) or an account's own number ("WhatsApp de la cuenta")."""
    number: str
    user_id: int | None = None
    account_id: object = None
    cursor: InboxCursor | None = None


def targets(order, account_id, actor_id, preference):
    """The other party's people who opted in with a verified number and want this kind of notice, and its account's own number when it has
    one; a number shared by both gets one notice."""
    users = push.other_side(order, account_id, actor_id)
    people = [Target(cursor.whatsapp_number, user_id=cursor.user_id, cursor=cursor) for cursor in InboxCursor.objects.filter(
        user_id__in=users, user__is_active=True, whatsapp_opt_in_at__isnull=False, whatsapp_verified_at__isnull=False, **{preference: True})
        .exclude(whatsapp_number='').order_by('user_id')]
    sides = [order.supplier_id, order.client_id] if account_id is None else [order.supplier_id if account_id == order.client_id else order.client_id]
    numbers = {target.number for target in people}
    accounts = [Target(holder.whatsapp_number, account_id=holder.account_id) for holder in AccountWhatsApp.objects.filter(
        account_id__in=sides, account__active=True, whatsapp_opt_in_at__isnull=False, whatsapp_verified_at__isnull=False, **{preference: True})
        .exclude(whatsapp_number='').order_by('account_id') if holder.whatsapp_number not in numbers]
    return people + accounts


def conversation_of(order, target):
    """The ?conversacion= value of a link for this target: a person's carries their sign-in hint (mall.login_hints)."""
    return login_hints.conversation(order.pk, target.user_id) if target.user_id else str(order.pk)


def log_row(target, order, kind, name):
    return WhatsAppMessage.objects.create(user_id=target.user_id, account_id=target.account_id, order=order, number=target.number, template=name, kind=kind)


def queue(people, order, kind, name, body):
    return [(log_row(target, order, kind, name).pk, template(target.number, name, body, conversation_of(order, target))) for target in people]


def message_saved(sender, instance, created=False, raw=False, **kwargs):
    if not created or raw or not whatsapp_enabled():
        return
    message, order = instance, instance.order

    def collect():
        from .whatsapp_bridge import continuing, relay_files, relay_text, window_open
        # Asking for an adjustment also writes its reason as a message: the adjustment's own notice covers it.
        if DealEvent.objects.filter(order=order, actor_id=message.actor_id, kind='request_adjustment', description=message.body).exists():
            return
        now = timezone.now()
        notified = set(ConversationRead.objects.filter(order=order, whatsapp_at__gte=now - COOLDOWN).values_list('user_id', flat=True))

        def recently_notified(target):  # one template notice per conversation every COOLDOWN
            if target.user_id:
                return target.user_id in notified
            return WhatsAppMessage.objects.filter(account_id=target.account_id, order=order, kind='message', created_at__gte=now - COOLDOWN).exists()
        relays, notices = [], []
        recipients = targets(order, message.account_id, message.actor_id, 'whatsapp_messages')
        # Someone whose latest message here came by WhatsApp is talking from there: the replies follow, even with the site open somewhere.
        latest = dict(DealMessage.objects.filter(order=order, actor_id__in={target.user_id for target in recipients if target.user_id})
                      .order_by('actor_id', '-id').distinct('actor_id').values_list('actor_id', 'channel'))
        for target in recipients:
            if target.cursor and target.cursor.active_at and target.cursor.active_at >= now - ACTIVE and latest.get(target.user_id) != 'whatsapp':
                continue  # in MotionPartes right now: the chat shows it
            if window_open(target.number, now):
                relays.append(target)  # the conversation goes on in WhatsApp: each message, in full
            elif not recently_notified(target):
                notices.append(target)
        for target in notices:
            if target.user_id:
                mark, _ = ConversationRead.objects.get_or_create(user_id=target.user_id, order=order)
                ConversationRead.objects.filter(pk=mark.pk).update(whatsapp_at=now)
        first = {target.number: not continuing(target.number, order, now) for target in relays}  # before this relay is logged
        jobs = []
        for target in relays:  # the text (named at the start of a stretch), then each file as its own message
            conversation = conversation_of(order, target)
            payloads = [relay_text(target.number, message, order, conversation, first[target.number]), *relay_files(target.number, message, conversation)]
            jobs += [(log_row(target, order, 'message', f'{settings.WHATSAPP_TEMPLATE_MESSAGE} ({"archivo" if "_attachment" in payload else "relevo"})'[:80]).pk,
                      payload) for payload in payloads if payload]
        body = [param(message.account.name, 60), param(order.reference, 32), param(describe(message), 120)]
        jobs += queue(notices, order, 'message', settings.WHATSAPP_TEMPLATE_MESSAGE, body)
        if jobs:
            push.dispatch(lambda: deliver(jobs))
    transaction.on_commit(collect)


def event_saved(sender, instance, created=False, raw=False, **kwargs):
    if not created or raw or not whatsapp_enabled():
        return
    event, order = instance, instance.order

    def collect():
        people = targets(order, event.account_id, event.actor_id, 'whatsapp_updates')
        if people:
            jobs = queue(people, order, event.kind, settings.WHATSAPP_TEMPLATE_UPDATE, [param(order.reference, 32), param(event_title(event), 120)])
            push.dispatch(lambda: deliver(jobs))
    transaction.on_commit(collect)


class WhatsAppPreferences(serializers.Serializer):
    number = serializers.CharField(max_length=30, required=False, allow_blank=True)
    opt_in = serializers.BooleanField(required=False)
    messages = serializers.BooleanField(required=False)
    updates = serializers.BooleanField(required=False)
    cancel = serializers.BooleanField(required=False)  # drops the number waiting for its code


def preferences(cursor):
    """active: notices go to number (consent given and the number verified). pending_number waits for the code sent at code_sent_at."""
    from .whatsapp_verification import CODE_TTL, RESEND_AFTER
    sent = cursor.whatsapp_code_sent_at if cursor.whatsapp_pending_number else None
    return {'enabled': whatsapp_enabled(), 'number': cursor.whatsapp_number, 'opted_in': cursor.whatsapp_opt_in_at is not None,
            'verified': cursor.whatsapp_verified_at is not None, 'active': cursor.whatsapp_opt_in_at is not None and cursor.whatsapp_verified_at is not None,
            'pending_number': cursor.whatsapp_pending_number, 'code_sent_at': sent.isoformat() if sent else None,
            'code_expires_at': (sent + CODE_TTL).isoformat() if sent and cursor.whatsapp_code_hash else None,
            'resend_at': (sent + RESEND_AFTER).isoformat() if sent else None,
            'messages': cursor.whatsapp_messages, 'updates': cursor.whatsapp_updates}


class InboxWhatsApp(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='Si el servidor envía avisos por WhatsApp, el número de la persona, si aceptó '
                   'recibirlos y qué avisos quiere: messages (mensajes nuevos cuando no está en MotionPartes) y updates (cambios en sus órdenes).')
    def get(self, request):
        return Response(preferences(cursor_for(request.user)))

    @extend_schema(request=WhatsAppPreferences, responses=OpenApiTypes.OBJECT, description='opt_in=true con number da el consentimiento: un '
                   'número no verificado recibe un código por WhatsApp y queda pendiente hasta confirmarlo en inbox/whatsapp/verify/ (number '
                   'sin cambios y ya verificado se reactiva sin código). opt_in=false retira el consentimiento; cancel=true descarta el número '
                   'pendiente; messages y updates eligen los avisos.')
    def post(self, request):
        return change(request, cursor_for(request.user))


def change(request, holder):
    """A person's (InboxCursor) or an account's (AccountWhatsApp) WhatsApp choices: the same rules for both."""
    from .whatsapp_verification import CLEARED, start
    if not whatsapp_enabled():
        return Response({'detail': 'Los avisos por WhatsApp no están configurados.'}, status=409)
    data = WhatsAppPreferences(data=request.data)
    data.is_valid(raise_exception=True)
    values, cursor = data.validated_data, holder
    changes = {field: values[key] for key, field in [('messages', 'whatsapp_messages'), ('updates', 'whatsapp_updates')] if key in values}
    if values.get('cancel'):
        changes.update(CLEARED)
    if 'number' in values or values.get('opt_in') is True:
        number = normalize_number(values['number']) if 'number' in values else cursor.whatsapp_number or None
        if number is None:
            raise serializers.ValidationError({'number': 'Escribe tu número de WhatsApp con el código de país, por ejemplo +507 6123 4567.'})
        if not values.get('opt_in'):
            raise serializers.ValidationError({'opt_in': 'Acepta recibir avisos en el número.'})
        if number == cursor.whatsapp_number and cursor.whatsapp_verified_at:
            if cursor.whatsapp_opt_in_at is None:
                changes['whatsapp_opt_in_at'] = timezone.now()  # back on, in a number already proven theirs
        else:
            start(request.user, cursor, number)  # a code to the number; it waits until the person confirms it
    elif values.get('opt_in') is False:
        changes.update(whatsapp_opt_in_at=None, **CLEARED)
    if changes:
        type(cursor).objects.filter(pk=cursor.pk).update(**changes)
    cursor.refresh_from_db()
    return Response(preferences(cursor))


def reply(number, text):
    try:
        graph({'messaging_product': 'whatsapp', 'to': number.lstrip('+'), 'type': 'text', 'text': {'body': text, 'preview_url': True}})
    except Exception:
        log.warning('WhatsApp reply to %s failed', number, exc_info=True)


def handle(value):
    """One webhook change: delivery statuses of our notices and relays, and messages people wrote to the number (the bridge)."""
    for status in value.get('statuses') or []:
        state, row = status.get('status'), WhatsAppMessage.objects.filter(wamid=status.get('id', '')).first()
        if row is None or state not in PROGRESS + ['failed']:
            continue
        if state == 'failed':
            error = '; '.join(f"{item.get('code', '')} {item.get('title', '')}".strip() for item in status.get('errors') or [])
            WhatsAppMessage.objects.filter(pk=row.pk).update(status='failed', error=error[:300], updated_at=timezone.now())
        elif row.status in PROGRESS and PROGRESS.index(state) > PROGRESS.index(row.status):
            WhatsAppMessage.objects.filter(pk=row.pk).update(status=state, updated_at=timezone.now())
    from .whatsapp_bridge import inbound  # replies into the deal's chat, the 24-hour window, BAJA (mall.whatsapp_bridge)
    now = timezone.now()
    for message in value.get('messages') or []:
        inbound(message, now)


class WhatsAppWebhook(APIView):
    """Meta's webhook (configure https://<site>/api/v1/whatsapp/webhook/ with WHATSAPP_VERIFY_TOKEN): signed with the app secret."""
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = []  # each notice brings several status updates

    @extend_schema(exclude=True)
    def get(self, request):
        params = request.query_params
        if settings.WHATSAPP_VERIFY_TOKEN and params.get('hub.mode') == 'subscribe' \
                and hmac.compare_digest(params.get('hub.verify_token', ''), settings.WHATSAPP_VERIFY_TOKEN):
            return HttpResponse(params.get('hub.challenge', ''), content_type='text/plain')
        return HttpResponse(status=403)

    @extend_schema(exclude=True)
    def post(self, request):
        body = request.body
        expected = 'sha256=' + hmac.new(settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not settings.WHATSAPP_APP_SECRET or not hmac.compare_digest(request.headers.get('X-Hub-Signature-256', ''), expected):
            return HttpResponse(status=403)
        try:
            for entry in json.loads(body).get('entry') or []:
                for change in entry.get('changes') or []:
                    handle(change.get('value') or {})
        except Exception:  # answering 200 anyway: Meta retries failed deliveries for days
            log.exception('WhatsApp webhook could not be processed')
        return HttpResponse(status=200)
