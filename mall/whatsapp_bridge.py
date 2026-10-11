"""The WhatsApp bridge: a deal's conversation goes on in WhatsApp too, and stays in MotionPartes.

- The 24-hour window: WhatsApp lets a business send plain messages to a number only within WINDOW of that number's last message to it
  (WhatsAppContact keeps when each number last wrote). Outside it only approved templates go, so the fallback of an unusable template and
  the relays below are sent only while it is open.
- Inbound: a text from a verified number (a person's, or an account's own number) goes into the deal's chat as theirs (DealMessage channel
  "whatsapp"): the deal of the message it answers (WhatsApp's reply context), or else the last deal notified to that number within
  FIND_DEAL. Meta's ✅ reaction confirms it. BAJA or STOP opts the number out; anything else gets a short pointer to the site, at most once
  every REPLY_EVERY. Meta may deliver a webhook twice: a message is brought in once.
- Outbound (mall.whatsapp.message_saved): a chat message to someone whose window is open, away from the app, goes in full as plain text
  right away, so they can answer it from WhatsApp; with the window closed, the template notice."""
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import push
from .inbox_models import AccountWhatsApp, InboxCursor, WhatsAppContact, WhatsAppMessage
from .whatsapp import OPT_OUT, REPLY_EVERY, graph, normalize_number, reply

WINDOW = timedelta(hours=24)
FIND_DEAL = timedelta(days=7)
BODY_LIMIT = 3500  # WhatsApp takes 4096 characters; the header and the link come around it


def window_open(number, now=None):
    return WhatsAppContact.objects.filter(number=number, last_inbound_at__gt=(now or timezone.now()) - WINDOW).exists()


def relay_text(number, message, order, conversation):
    """A chat message in full, to answer from WhatsApp: who wrote it, the order, and the link to its conversation."""
    link = f"{settings.SITE_URL.rstrip('/')}/?conversacion={conversation}"
    body = (f'*{message.account.name}* · {order.reference}\n{message.body[:BODY_LIMIT]}\n\n'
            f'Responde a este mensaje para contestar en la orden, o ábrela en MotionPartes: {link}')
    return {'messaging_product': 'whatsapp', 'to': number.lstrip('+'), 'type': 'text', 'text': {'preview_url': False, 'body': body}}


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
    unverified = InboxCursor.objects.filter(whatsapp_number=number).exists() or InboxCursor.objects.filter(whatsapp_pending_number=number).exists()
    if not people and not accounts and not unverified:
        return  # not someone of the platform (and never a loop with another bot)
    wamid = message.get('id', '')
    if wamid and WhatsAppMessage.objects.filter(wamid=wamid, status='received').exists():
        return  # Meta delivered it again
    text = ((message.get('text') or {}).get('body') or '').strip() if message.get('type', 'text') == 'text' else ''
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
    if not text:
        pointer(number, contact, now, f'Por ahora solo pasamos mensajes de texto a la orden. Para enviar fotos o archivos, usa MotionPartes: {site}/')
        return
    row = deal_row(number, (message.get('context') or {}).get('id'), now)
    who = author(row, people, accounts) if row else None
    if not who:
        pointer(number, contact, now, 'Para escribir en una orden, responde a uno de sus avisos (mantén presionado el aviso y elige Responder). '
                                      f'También puedes continuar en MotionPartes: {site}/ · Para dejar de recibir avisos, responde BAJA.')
        return
    user, account_id = who
    DealMessage.objects.get_or_create(order=row.order, account_id=account_id, message_id=uuid.uuid5(uuid.NAMESPACE_URL, f'whatsapp:{wamid or now}'),
                                      defaults={'actor': user, 'body': text[:4000], 'channel': 'whatsapp'})
    WhatsAppMessage.objects.create(user=user if row.user_id else None, account_id=None if row.user_id else account_id, order=row.order, number=number,
                                   template='', kind='reply', wamid=wamid, status='received')
    if wamid:
        push.dispatch(lambda: react(number, wamid))
