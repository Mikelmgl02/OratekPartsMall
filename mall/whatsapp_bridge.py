"""The WhatsApp bridge: a deal's conversation goes on in WhatsApp too, and stays in MotionPartes.

- The 24-hour window: WhatsApp lets a business send plain messages to a number only within WINDOW of that number's last message to it
  (WhatsAppContact keeps when each number last wrote). Outside it only approved templates go, so the fallback of an unusable template and
  the relays below are sent only while it is open.
- Inbound: a text from a verified number (a person's, or an account's own number) goes into the deal's chat as theirs (DealMessage channel
  "whatsapp"): the deal of the message it answers (WhatsApp's reply context), or the one the conversation is about (its last WhatsApp, within
  STRETCH); anything else goes to the shopping assistant (mall.whatsapp_assistant), or, with it off, to the last deal notified to that
  number within FIND_DEAL. Photos, documents, audio and video go too, with their caption, once downloaded from Meta (and a voice note transcribed,
  mall.deal_attachments). Meta's ✅ reaction confirms it. BAJA or STOP opts the number out; reactions pass silently; anything else gets a
  short pointer to the site, at most once every REPLY_EVERY. Meta may deliver a webhook twice: a message is brought in once.
- Outbound (mall.whatsapp.message_saved): a chat message to someone whose window is open, away from the app (or whose latest message in
  that conversation came by WhatsApp), goes in full as plain text right away, so they can answer it from WhatsApp; only the first of a
  stretch (STRETCH) names the account and the order, the rest read like any chat. Its files follow, each as its own WhatsApp message
  (uploaded to Meta when sent), or as a link when Meta will not take one. With the window closed, the template notice."""
import json
import logging
import re
import urllib.parse
import urllib.request
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import push
from .inbox_models import AccountWhatsApp, InboxCursor, WhatsAppContact, WhatsAppMessage
from .whatsapp import OPT_OUT, REPLY_EVERY, graph, normalize_number, reply

WINDOW = timedelta(hours=24)
FIND_DEAL = timedelta(days=7)
BODY_LIMIT = 3500  # WhatsApp takes 4096 characters; the header and the link come around it
STRETCH = timedelta(minutes=30)  # a relay this soon after the last WhatsApp about the same order continues that conversation
MEDIA = ('image', 'document', 'audio', 'video')
SILENT = ('reaction', 'system')  # nothing to bring in, nothing to answer
MEDIA_HOSTS = ('fbsbx.com', 'facebook.com', 'fbcdn.net', 'whatsapp.net')  # where Meta keeps the files people send
# What WhatsApp takes as each kind of message (images up to 5 MB); any other file goes as a document.
SENDABLE = {'image': ('image/jpeg', 'image/png'), 'audio': ('audio/aac', 'audio/amr', 'audio/mpeg', 'audio/mp4', 'audio/ogg'),
            'video': ('video/mp4', 'video/3gpp')}
IMAGE_LIMIT = 5 * 1024 * 1024
log = logging.getLogger(__name__)


def window_open(number, now=None):
    return WhatsAppContact.objects.filter(number=number, last_inbound_at__gt=(now or timezone.now()) - WINDOW).exists()


def continuing(number, order, now):
    """Whether the last WhatsApp with this number, either way (codes aside), was about this order within STRETCH: the conversation is
    going on there, and its next message needs no header."""
    last = (WhatsAppMessage.objects.filter(number=number).exclude(kind='verify').exclude(status='failed').order_by('-id')
            .values_list('order_id', 'created_at').first())
    return bool(last) and last[0] == order.pk and last[1] >= now - STRETCH


def conversation_link(conversation):
    return f"{settings.SITE_URL.rstrip('/')}/?conversacion={conversation}"


def text_message(number, body):
    return {'messaging_product': 'whatsapp', 'to': number.lstrip('+'), 'type': 'text', 'text': {'preview_url': False, 'body': body}}


def relay_text(number, message, order, conversation, first=True):
    """A chat message in full, to answer from WhatsApp, like any chat: the first of a stretch says who wrote it and about which order,
    with the link to its conversation (one number carries every order); the ones following it, only the text (None for a message of only
    files, which go on their own)."""
    body = message.body[:BODY_LIMIT]
    if first:
        heading = f'*{message.account.name}* · {order.reference}'
        body = (f'{heading}\n{body}' if body else heading) + f'\n\nEscribe aquí para contestar en la orden, o ábrela en MotionPartes: {conversation_link(conversation)}'
    return text_message(number, body) if body else None


def relay_files(number, message, conversation):
    """The message's files, each to go as its own WhatsApp message (mall.whatsapp.deliver uploads it to Meta then); the link to the
    conversation goes instead when Meta will not take one."""
    return [{'to': number.lstrip('+'), '_attachment': attachment.pk,
             '_fallback': text_message(number, f'📎 {attachment.name}: {conversation_link(conversation)}')}
            for attachment in message.attachments.all() if not attachment.failed]


def sent_as(attachment):
    """The kind of WhatsApp message a chat file goes as."""
    if attachment.kind in SENDABLE and attachment.content_type in SENDABLE[attachment.kind]:
        if attachment.kind != 'image' or attachment.size <= IMAGE_LIMIT:
            return attachment.kind
    return 'document'


def upload(attachment):
    """The file to Meta (POST /<phone>/media): the id to send it with, valid 30 days."""
    boundary = uuid.uuid4().hex
    with attachment.file.open('rb') as handle:
        content = handle.read()
    stored = attachment.file.name.rsplit('/', 1)[-1].replace('"', '')
    fields = ''.join(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
                     for name, value in (('messaging_product', 'whatsapp'), ('type', attachment.content_type)))
    body = (f'{fields}--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{stored}"\r\n'
            f'Content-Type: {attachment.content_type}\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
    request = urllib.request.Request(f'https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{settings.WHATSAPP_PHONE_NUMBER_ID}/media',
                                     data=body, method='POST', headers={'Authorization': f'Bearer {settings.WHATSAPP_ACCESS_TOKEN}',
                                                                        'Content-Type': f'multipart/form-data; boundary={boundary}'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)['id']


def send_file(payload, uploaded):
    """One chat file to WhatsApp (uploaded once per delivery, however many numbers get it): Meta's answer. When Meta refuses the file
    (its type or size), the link to the conversation goes instead."""
    from .models import DealAttachment
    try:
        attachment = DealAttachment.objects.get(pk=payload['_attachment'])
        kind = sent_as(attachment)
        if attachment.pk not in uploaded:
            uploaded[attachment.pk] = upload(attachment)
        media = {'id': uploaded[attachment.pk], **({'filename': attachment.name} if kind == 'document' else {})}
        return graph({'messaging_product': 'whatsapp', 'to': payload['to'], 'type': kind, kind: media})
    except Exception as error:  # never the file's content in the log
        log.warning('WhatsApp file %s sent as a link: %s', payload['_attachment'], type(error).__name__)
        return graph(payload['_fallback'])


class TooLarge(Exception):
    pass


def fetch_media(media_id):
    """A file someone sent by WhatsApp, from Meta: (content, content type). Meta gives its address for a few minutes, on its own servers,
    and the token must go with it."""
    if not re.fullmatch(r'\d{1,40}', media_id or ''):
        raise ValueError('media id')
    headers = {'Authorization': f'Bearer {settings.WHATSAPP_ACCESS_TOKEN}'}
    with urllib.request.urlopen(urllib.request.Request(f'https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{media_id}',
                                                       headers=headers), timeout=15) as response:
        info = json.load(response)
    if int(info.get('file_size') or 0) > settings.CHAT_MEDIA_MAX_BYTES:
        raise TooLarge()
    address = urllib.parse.urlparse(info.get('url', ''))
    host = address.hostname or ''
    if address.scheme != 'https' or not any(host == known or host.endswith(f'.{known}') for known in MEDIA_HOSTS):
        raise ValueError('media address')  # the token goes only to Meta
    with urllib.request.urlopen(urllib.request.Request(info['url'], headers=headers), timeout=60) as response:
        content = response.read(settings.CHAT_MEDIA_MAX_BYTES + 1)
    if len(content) > settings.CHAT_MEDIA_MAX_BYTES:
        raise TooLarge()
    return content, info.get('mime_type', '')


def message_key(wamid, now):
    return uuid.uuid5(uuid.NAMESPACE_URL, f'whatsapp:{wamid or now}')


def bring_in(order, account_id, user, number, wamid, kind, media, caption, now):
    """A file someone sent by WhatsApp, into the deal's chat with its caption once downloaded (and a voice note transcribed), so the chat,
    the relays and the notices get it whole. One Meta would not give, or too large, still shows, as not received, and either party can
    ask for it again (mall.deal_attachments.DealAttachmentRetry) by Meta's id."""
    from .deal_attachments import received, store, transcribe
    from .models import DealMessage
    content, content_type = b'', media.get('mime_type', '')
    try:
        content, content_type = fetch_media(media.get('id', ''))
    except Exception as error:
        log.warning('WhatsApp file %s not received: %s', wamid, type(error).__name__)
    kind, content_type, name = received(kind, content_type or media.get('mime_type', ''), media.get('filename'), timezone.localtime(now))
    voice = kind == 'audio' and bool(media.get('voice'))
    transcript = transcribe(content, content_type) if voice else ''
    with transaction.atomic():
        message, created = DealMessage.objects.get_or_create(order=order, account_id=account_id, message_id=message_key(wamid, now),
                                                             defaults={'actor': user, 'body': caption[:4000], 'channel': 'whatsapp'})
        if created:
            store(message, kind, content_type, name, content, voice=voice, failed=not content, transcript=transcript,
                  whatsapp_media_id=str(media.get('id', ''))[:64])
    if created and content and wamid:
        react(number, wamid)


def react(number, wamid, emoji='✅'):
    try:
        graph({'messaging_product': 'whatsapp', 'to': number.lstrip('+'), 'type': 'reaction', 'reaction': {'message_id': wamid, 'emoji': emoji}})
    except Exception:  # a missing confirmation never undoes the message
        pass


def pointer(number, contact, now, text):
    """A short answer pointing back to the site, at most once every REPLY_EVERY per number."""
    if contact.replied_at and contact.replied_at > now - REPLY_EVERY:
        return
    WhatsAppContact.objects.filter(pk=contact.pk).update(replied_at=now)
    push.dispatch(lambda: reply(number, text))


def deal_row(number, context_id, now):
    """The notice or relay a reply belongs to: the one it answers, or the last one to that number within FIND_DEAL (that Meta took; a failed
    one never reached them)."""
    rows = WhatsAppMessage.objects.filter(number=number, order__isnull=False).exclude(status__in=('received', 'failed')).select_related('order', 'user')
    return (rows.filter(wamid=context_id).first() if context_id else None) or rows.filter(created_at__gte=now - FIND_DEAL).order_by('-id').first()


def order_row(number, context_id, now):
    """With the assistant on: the order a message continues, by the notice or relay it answers (WhatsApp's reply context) or by the last
    WhatsApp with this number having been about that order within STRETCH. None for a reply to the assistant, or when the conversation was
    last with the assistant or is older: then the assistant takes it."""
    rows = WhatsAppMessage.objects.filter(number=number).exclude(kind__in=('verify', 'ask')).exclude(status='failed')
    if context_id:
        answered = rows.filter(wamid=context_id).first()
        if answered is not None:
            return deal_row(number, context_id, now) if answered.order_id else None
    last = rows.order_by('-id').first()
    if last is None or last.order_id is None or last.created_at < now - STRETCH:
        return None
    return (rows.filter(order_id=last.order_id).exclude(status='received').select_related('order', 'user').order_by('-id').first()
            or deal_row(number, None, now))


def answered(number, context_id):
    """Whether a message answers one of ours (WhatsApp's reply context): then it goes where that one came from, without asking."""
    return bool(context_id) and WhatsAppMessage.objects.filter(number=number, wamid=context_id).exists()


def open_orders(number, now, people, accounts):
    """The orders a plain message could be for, the latest first, that its writer may still write in: [(the order's latest notice or relay
    to it, (user, account id))]. Within STRETCH: the order the person last wrote in and those that wrote to them since, or, when they have
    not written in any, those that wrote to them. More than one, and only the person can say which."""
    found = []
    rows = (WhatsAppMessage.objects.filter(number=number, order__isnull=False, created_at__gte=now - STRETCH).exclude(kind='verify')
            .exclude(status='failed').order_by('-id'))
    mine = rows.filter(status='received').first()
    if mine is not None:
        rows = rows.filter(id__gte=mine.id)
    for order_id in dict.fromkeys(rows.values_list('order_id', flat=True)):
        row = (WhatsAppMessage.objects.filter(number=number, order_id=order_id).exclude(status__in=('received', 'failed'))
               .select_related('order', 'user').order_by('-id').first())
        who = author(row, people, accounts) if row else None
        if who:
            found.append((row, who))
    return found[:10]


def hold(number, message, found, now):
    """A plain message while several orders are open: kept (with any others still waiting) until the person taps which order it is for."""
    from .assistant_models import WhatsAppAssistantSession
    from . import whatsapp_assistant as assistant
    session, _ = WhatsAppAssistantSession.objects.get_or_create(number=number)
    waiting = [held for held in (session.pending.get('para_orden') or []) if held.get('id') != message.get('id')]
    session.pending = {**session.pending, 'para_orden': (waiting + [message])[-5:]}
    session.save(update_fields=['pending', 'updated_at'])
    lines = [f'{index}. {other(row.order, who[1])} · {row.order.reference}' for index, (row, who) in enumerate(found, 1)]
    body = 'Tienes conversaciones abiertas con varias órdenes. ¿Para cuál es tu mensaje?\n' + '\n'.join(lines)
    choices = [(f'order:{row.order_id}', other(row.order, who[1]), row.order.reference[-8:]) for row, who in found]
    payload = ({**assistant.buttons(body, [(identifier, title) for identifier, title, _ in choices]), '_kind': 'ask'} if len(choices) <= 3
               else {**assistant.choice_list(body, 'Ver órdenes', choices), '_kind': 'ask'})
    push.dispatch(lambda: assistant.send(number, None, payload))


def other(order, account_id):
    """The other party's name, for the one writing as account_id."""
    return order.supplier_name if account_id == order.client_id else order.client_name


def bring_text(order, account_id, user, number, wamid, body, now):
    from .models import DealMessage
    DealMessage.objects.get_or_create(order=order, account_id=account_id, message_id=message_key(wamid, now),
                                      defaults={'actor': user, 'body': body[:4000], 'channel': 'whatsapp'})
    if wamid:
        push.dispatch(lambda: react(number, wamid))


def chosen(number, order_id, wamid, now, people, accounts):
    """The person tapped which order their waiting messages are for: in they go, and the conversation goes on with that order."""
    from .assistant_models import WhatsAppAssistantSession
    row = (WhatsAppMessage.objects.filter(number=number, order_id=order_id).exclude(status__in=('received', 'failed'))
           .select_related('order', 'user').order_by('-id').first())
    who = author(row, people, accounts) if row else None
    if not who:
        push.dispatch(lambda: reply(number, f'Ya no puedes escribir en esa orden. Ábrela en MotionPartes: {settings.SITE_URL.rstrip("/")}/'))
        return
    user, account_id = who
    session = WhatsAppAssistantSession.objects.filter(number=number).first()
    waiting = (session.pending.pop('para_orden', None) or []) if session else []
    if session:
        session.save(update_fields=['pending', 'updated_at'])
    WhatsAppMessage.objects.create(user=user if row.user_id else None, account_id=None if row.user_id else account_id, order=row.order, number=number,
                                   template='', kind='reply', wamid=wamid, status='received')
    WhatsAppMessage.objects.filter(number=number, wamid__in=[held.get('id') for held in waiting if held.get('id')], status='received').update(order=row.order)
    for held in waiting:
        kind = held.get('type', 'text')
        media = held.get(kind) if kind in MEDIA and isinstance(held.get(kind), dict) else None
        if media:
            push.dispatch(lambda held=held, kind=kind, media=media: bring_in(row.order, account_id, user, number, held.get('id', ''), kind, media,
                                                                             (media.get('caption') or '').strip(), now))
        else:
            body = ((held.get(kind) or {}).get('body' if kind == 'text' else 'text') or '').strip()
            if body:
                bring_text(row.order, account_id, user, number, held.get('id', ''), body, now)
    if not waiting:
        push.dispatch(lambda: reply(number, f'Listo: lo que escribas ahora va a la orden {row.order.reference} con {other(row.order, account_id)}.'))


def author(row, people, accounts):
    """(user, account id) writing the reply: the person or the account the answered notice went to, while still a party to the deal."""
    from .inbox import my_deals
    from .models import Membership
    order = row.order
    if row.user_id and any(cursor.user_id == row.user_id for cursor in people):
        deal = my_deals(row.user).filter(pk=order.pk).first()
        if deal:
            return row.user, deal.supplier_id if deal.side == 'supplier' else deal.client_id
    holder = next((item for item in accounts if item.account_id == row.account_id), None) if row.account_id else None
    if holder and row.account_id in (order.client_id, order.supplier_id):
        user = holder.whatsapp_verified_by
        if user and user.is_active and Membership.objects.filter(user=user, account_id=row.account_id).exists():
            return user, row.account_id
    return None


def inbound(message, now):
    """One message someone wrote to the MotionPartes number."""
    from .models import DealMessage
    number = normalize_number(f"+{message.get('from', '')}")
    if not number:
        return
    contact, _ = WhatsAppContact.objects.update_or_create(number=number, defaults={'last_inbound_at': now})
    people = list(InboxCursor.objects.filter(whatsapp_number=number, whatsapp_verified_at__isnull=False, user__is_active=True))
    accounts = list(AccountWhatsApp.objects.filter(whatsapp_number=number, whatsapp_verified_at__isnull=False, account__active=True)
                    .select_related('whatsapp_verified_by'))
    unverified = (InboxCursor.objects.filter(whatsapp_number=number).exists() or InboxCursor.objects.filter(whatsapp_pending_number=number).exists()
                  or AccountWhatsApp.objects.filter(whatsapp_pending_number=number).exists())
    if not people and not accounts and not unverified:
        return  # not someone of the platform (and never a loop with another bot)
    wamid = message.get('id', '')
    if wamid and WhatsAppMessage.objects.filter(wamid=wamid, status='received').exists():
        return  # Meta delivered it again
    kind = message.get('type', 'text')
    if kind in SILENT:
        return  # a 👍 on a message, or a change Meta reports: nothing for the chat
    text = ((message.get(kind) or {}).get('body' if kind == 'text' else 'text') or '').strip() if kind in ('text', 'button') else ''
    media = message.get(kind) if kind in MEDIA and isinstance(message.get(kind), dict) else None
    caption = ((media or {}).get('caption') or '').strip()
    site = settings.SITE_URL.rstrip('/')
    if re.sub(r'\s+', ' ', text).upper().rstrip('.!') in OPT_OUT:
        InboxCursor.objects.filter(whatsapp_number=number).update(whatsapp_opt_in_at=None)
        AccountWhatsApp.objects.filter(whatsapp_number=number).update(whatsapp_opt_in_at=None)
        WhatsAppContact.objects.filter(pk=contact.pk).update(replied_at=now)
        push.dispatch(lambda: reply(number, 'Listo: ya no recibirás avisos de MotionPartes por WhatsApp. Puedes activarlos de nuevo en Mi perfil, '
                                            'en el sitio.'))
        return
    if not people and not accounts:
        pointer(number, contact, now, f'Para responder por WhatsApp, confirma tu número en MotionPartes (Mi perfil › Avisos): {site}/perfil')
        return
    from . import whatsapp_assistant
    context_id = (message.get('context') or {}).get('id')
    tapped = ((message.get('interactive') or {}).get('button_reply') or (message.get('interactive') or {}).get('list_reply') or {}).get('id', '')
    if kind == 'interactive' and tapped.startswith('order:'):  # which order the waiting messages are for
        chosen(number, tapped.split(':', 1)[1], wamid, now, people, accounts)
        return
    if kind != 'interactive' and not answered(number, context_id):
        found = open_orders(number, now, people, accounts)
        if len(found) > 1:  # several orders open: never a guess
            WhatsAppMessage.objects.create(user_id=people[0].user_id if people else None, account_id=None if people else accounts[0].account_id,
                                           number=number, template='', kind='reply', wamid=wamid, status='received')
            hold(number, message, found, now)
            return
    assistant = whatsapp_assistant.enabled()
    row = order_row(number, context_id, now) if assistant else deal_row(number, context_id, now)
    who = author(row, people, accounts) if row else None
    if not who and (assistant or kind == 'interactive'):  # not an order's conversation: the shopping assistant (mall.whatsapp_assistant)
        WhatsAppMessage.objects.create(user_id=people[0].user_id if people else None, account_id=None if people else accounts[0].account_id,
                                       number=number, template='', kind='assistant', wamid=wamid, status='received')
        push.dispatch(lambda: whatsapp_assistant.handle(number, people, accounts, message, now))
        return
    if not text and not media:
        pointer(number, contact, now, 'Por ahora pasamos a la orden textos, fotos, documentos, audios y videos. Para enviar otra cosa, usa '
                                      f'MotionPartes: {site}/')
        return
    if not who:
        pointer(number, contact, now, 'Para escribir en una orden, responde a uno de sus avisos (mantén presionado el aviso y elige Responder). '
                                      f'También puedes continuar en MotionPartes: {site}/ · Para dejar de recibir avisos, responde BAJA.')
        return
    user, account_id = who
    WhatsAppMessage.objects.create(user=user if row.user_id else None, account_id=None if row.user_id else account_id, order=row.order, number=number,
                                   template='', kind='reply', wamid=wamid, status='received')
    order = row.order
    if media:  # downloaded (and a voice note transcribed) before it reaches the chat: a few seconds
        push.dispatch(lambda: bring_in(order, account_id, user, number, wamid, kind, media, caption, now))
        return
    bring_text(order, account_id, user, number, wamid, text, now)
