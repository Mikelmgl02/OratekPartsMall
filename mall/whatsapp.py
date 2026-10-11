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

from . import push
from .email_notifications import ACTIVE
from .inbox import cursor_for, event_title
from . import login_hints
from .inbox_models import ConversationRead, InboxCursor, WhatsAppMessage
from .models import DealEvent

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
    """The template cannot be used: the same notice as plain text (delivered within the 24-hour window of the person's last message)."""
    name = payload['template']['name']
    try:
        accepted(log_id, graph(as_text(payload)), template=f'{name} (texto)'[:80], error=f'Plantilla no disponible ({code}); enviado como texto')
        log.info('WhatsApp notice %s sent as text: template %s unusable (%s)', log_id, name, code)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors='replace')[:200]
        WhatsAppMessage.objects.filter(pk=log_id).update(status='failed', error=f'Plantilla no disponible ({code}); texto: {error.code} {detail}'[:300],
                                                         updated_at=timezone.now())
        log.warning('WhatsApp notice %s refused as text too: %s %s', log_id, error.code, detail)


def deliver(jobs):
    for log_id, payload in jobs:
        try:
            accepted(log_id, graph(payload))
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


def recipients(order, account_id, actor_id, preference):
    """The other party's people who opted in with a number and want this kind of notice."""
    users = push.other_side(order, account_id, actor_id)
    return list(InboxCursor.objects.filter(user_id__in=users, user__is_active=True, whatsapp_opt_in_at__isnull=False, whatsapp_verified_at__isnull=False,
                                           **{preference: True})
                .exclude(whatsapp_number='').order_by('user_id'))


def queue(people, order, kind, name, body):
    jobs = []
    for cursor in people:
        row = WhatsAppMessage.objects.create(user_id=cursor.user_id, order=order, number=cursor.whatsapp_number, template=name, kind=kind)
        jobs.append((row.pk, template(cursor.whatsapp_number, name, body, login_hints.conversation(order.pk, cursor.user_id))))
    return jobs


def message_saved(sender, instance, created=False, raw=False, **kwargs):
    if not created or raw or not whatsapp_enabled():
        return
    message, order = instance, instance.order

    def collect():
        # Asking for an adjustment also writes its reason as a message: the adjustment's own notice covers it.
        if DealEvent.objects.filter(order=order, actor_id=message.actor_id, kind='request_adjustment', description=message.body).exists():
            return
        now = timezone.now()
        notified = set(ConversationRead.objects.filter(order=order, whatsapp_at__gte=now - COOLDOWN).values_list('user_id', flat=True))
        people = [cursor for cursor in recipients(order, message.account_id, message.actor_id, 'whatsapp_messages')
                  if not (cursor.active_at and cursor.active_at >= now - ACTIVE) and cursor.user_id not in notified]
        if not people:
            return
        for cursor in people:
            mark, _ = ConversationRead.objects.get_or_create(user_id=cursor.user_id, order=order)
            ConversationRead.objects.filter(pk=mark.pk).update(whatsapp_at=now)
        body = [param(message.account.name, 60), param(order.reference, 32), param(message.body, 120)]
        jobs = queue(people, order, 'message', settings.WHATSAPP_TEMPLATE_MESSAGE, body)
        push.dispatch(lambda: deliver(jobs))
    transaction.on_commit(collect)


def event_saved(sender, instance, created=False, raw=False, **kwargs):
    if not created or raw or not whatsapp_enabled():
        return
    event, order = instance, instance.order

    def collect():
        people = recipients(order, event.account_id, event.actor_id, 'whatsapp_updates')
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
        from .whatsapp_verification import CLEARED, start
        if not whatsapp_enabled():
            return Response({'detail': 'Los avisos por WhatsApp no están configurados.'}, status=409)
        data = WhatsAppPreferences(data=request.data)
        data.is_valid(raise_exception=True)
        values, cursor = data.validated_data, cursor_for(request.user)
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
            InboxCursor.objects.filter(pk=cursor.pk).update(**changes)
        cursor.refresh_from_db()
        return Response(preferences(cursor))


def reply(number, text):
    try:
        graph({'messaging_product': 'whatsapp', 'to': number.lstrip('+'), 'type': 'text', 'text': {'body': text, 'preview_url': True}})
    except Exception:
        log.warning('WhatsApp reply to %s failed', number, exc_info=True)


def handle(value):
    """One webhook change: delivery statuses of our notices and messages people wrote to the number."""
    for status in value.get('statuses') or []:
        state, row = status.get('status'), WhatsAppMessage.objects.filter(wamid=status.get('id', '')).first()
        if row is None or state not in PROGRESS + ['failed']:
            continue
        if state == 'failed':
            error = '; '.join(f"{item.get('code', '')} {item.get('title', '')}".strip() for item in status.get('errors') or [])
            WhatsAppMessage.objects.filter(pk=row.pk).update(status='failed', error=error[:300], updated_at=timezone.now())
        elif row.status in PROGRESS and PROGRESS.index(state) > PROGRESS.index(row.status):
            WhatsAppMessage.objects.filter(pk=row.pk).update(status=state, updated_at=timezone.now())
    now, site = timezone.now(), settings.SITE_URL.rstrip('/')
    for message in value.get('messages') or []:
        number = normalize_number(f"+{message.get('from', '')}")
        cursors = list(InboxCursor.objects.filter(whatsapp_number=number)) if number else []
        if not cursors:
            continue  # only people of the platform get an answer (and never a loop with another bot)
        text = re.sub(r'\s+', ' ', ((message.get('text') or {}).get('body') or '')).strip().upper().rstrip('.!')
        if text in OPT_OUT:
            InboxCursor.objects.filter(pk__in=[cursor.pk for cursor in cursors]).update(whatsapp_opt_in_at=None, whatsapp_replied_at=now)
            push.dispatch(lambda number=number: reply(number, 'Listo: ya no recibirás avisos de MotionPartes por WhatsApp. Puedes activarlos '
                                                              'de nuevo en la campana de notificaciones del sitio.'))
        elif not any(cursor.whatsapp_replied_at and cursor.whatsapp_replied_at > now - REPLY_EVERY for cursor in cursors):
            InboxCursor.objects.filter(pk__in=[cursor.pk for cursor in cursors]).update(whatsapp_replied_at=now)
            push.dispatch(lambda number=number: reply(number, 'Este número solo envía avisos de MotionPartes. Para responder, continúa la '
                                                              f'conversación en el sitio: {site}/ · Para dejar de recibir avisos, responde BAJA.'))


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
